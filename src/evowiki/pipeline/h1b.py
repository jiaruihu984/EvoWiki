"""H1b quiz-content review records (Plan 4b-1d). One human disposition per item:

- approve: the answer key is correct and source-supported as written. Every open answer
  review of the item's current fingerprint is approved, and an `h1b_review` item is approved
  (human verification of the key);
- reject: wrong, ambiguous or unsupported. An `h1b_review` item is rejected, so the snapshot
  drops the item (open reviews of it are rejected too);
- fix: the reviewer gives the corrected answer key (and optionally stem). The workspace item is
  rewritten (new fingerprint); old reviews no longer bind to it; an `h1b_review` item on the new
  fingerprint is approved.

Idempotent. Every record carries `decided_by` and the reviewer's reason verbatim."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evowiki.content.models import QuizItem
from evowiki.pipeline.quiz import H1B_REASON
from evowiki.pipeline.review import ReviewQueue, ReviewState, ReviewTarget, review_fingerprint


class H1bDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    disposition: Literal["approve", "reject", "fix"]
    reason: str = ""
    answer_key: str | None = None      # fix only
    stem: str | None = None            # fix only (optional)

    @model_validator(mode="after")
    def _fix_fields(self) -> "H1bDecision":
        if self.disposition == "fix" and not self.answer_key:
            raise ValueError(f"{self.item_id}: fix needs the corrected answer_key")
        if self.disposition != "fix" and (self.answer_key or self.stem):
            raise ValueError(f"{self.item_id}: only a fix may change the key or stem")
        if self.disposition in ("reject", "fix") and not self.reason.strip():
            raise ValueError(f"{self.item_id}: {self.disposition} needs a reason")
        return self


class H1bBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch: str
    decided_by: str = Field(min_length=1)
    decided_at: datetime
    decisions: tuple[H1bDecision, ...]


def _decide_open(queue: ReviewQueue, item_id: str, fp: str, state: ReviewState, *,
                 by: str, now: datetime, note: str) -> None:
    for it in queue.items(target=ReviewTarget.QUIZ_ANSWER, state=ReviewState.OPEN):
        if it.target_id == item_id and it.fingerprint in (None, fp):
            queue.decide(it.id, state, decided_by=by, now=now, note=note)


def apply_decision(queue: ReviewQueue, items: dict[str, QuizItem], d: H1bDecision, *,
                   by: str, now: datetime, batch: str) -> QuizItem:
    """Record one decision; returns the (possibly rewritten) workspace item."""
    q = items[d.item_id]
    note = f"H1b {batch}: {d.reason}".strip()
    if d.disposition == "fix":
        update = {"answer_key": d.answer_key}
        if d.stem:
            update["stem"] = d.stem
        q = q.model_copy(update=update)
        items[d.item_id] = q
    fp = review_fingerprint(q)
    state = ReviewState.REJECTED if d.disposition == "reject" else ReviewState.APPROVED
    _decide_open(queue, q.id, fp, state, by=by, now=now, note=note)
    rec = queue.enqueue(ReviewTarget.QUIZ_ANSWER, q.id, H1B_REASON,
                        payload={"disposition": d.disposition, "reason": d.reason,
                                 "batch": batch, "stem": q.stem, "answer_key": q.answer_key},
                        fingerprint=fp)
    if rec.state is ReviewState.OPEN:
        queue.decide(rec.id, state, decided_by=by, now=now, note=note)
    elif rec.state is not state:
        raise ValueError(f"{q.id}: already decided {rec.state.value}, now {state.value}")
    return q
