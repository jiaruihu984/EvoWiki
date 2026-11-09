import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field

from evowiki.content.models import ReviewStatus


class ReviewTarget(StrEnum):
    CONCEPT = "concept"
    EDGE = "edge"
    SEGMENT = "segment"
    CONNECTION = "connection"
    QUIZ_ANSWER = "quiz_answer"
    TRANSLATION = "translation"
    PERSPECTIVE = "perspective"
    # Plan 4b-1d: a human-reviewed move of concepts between presumes_required and
    # presumes_helpful (pipeline/presumes.py), applied at snapshot.
    PERSPECTIVE_PRESUMES = "perspective_presumes"


class ReviewState(StrEnum):
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewItem(BaseModel):
    id: str
    target: ReviewTarget
    target_id: str
    reason: str
    spot_check: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)
    state: ReviewState = ReviewState.OPEN
    decided_by: str | None = None
    decided_at: AwareDatetime | None = None
    note: str = ""
    fingerprint: str | None = None


_FINGERPRINT_EXCLUDE = {"review_status", "content_version", "answer_verified_by_human",
                        "covers_review_status", "framing_phase_status"}
# Stage I-B fields enter the hash only when set, so content written before they existed keeps
# its fingerprint (and the human decisions bound to it).
_FINGERPRINT_DROP_WHEN_DEFAULT = {"covers_relation_ids": [], "transfer_kind": None,
                                  "framing_phase": None}


def review_fingerprint(model: BaseModel) -> str:
    """Content hash a human decision binds to; review bookkeeping fields are excluded."""
    data = model.model_dump(mode="json", exclude=_FINGERPRINT_EXCLUDE)
    for name, default in _FINGERPRINT_DROP_WHEN_DEFAULT.items():
        if name in data and data[name] == default:
            del data[name]
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def review_item_id(target: ReviewTarget, target_id: str, reason: str,
                   fingerprint: str | None = None) -> str:
    key = f"{target}|{target_id}|{reason}"
    if fingerprint is not None:
        key += f"|{fingerprint}"
    return "rv_" + hashlib.sha256(key.encode()).hexdigest()[:12]


def spot_check_sample(ids: Iterable[str], fraction: float, seed: str = "evowiki") -> set[str]:
    out = set()
    for i in ids:
        h = int(hashlib.sha256(f"{seed}|{i}".encode()).hexdigest()[:8], 16)
        if h / 2**32 < fraction:
            out.add(i)
    return out


class ReviewQueue:
    """Human review queue. Every human decision about content lives here."""

    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path is not None else None
        self._items: dict[str, ReviewItem] = {}
        if self._path is not None and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    item = ReviewItem.model_validate_json(line)
                    self._items[item.id] = item

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text("".join(i.model_dump_json() + "\n" for i in self._items.values()),
                       encoding="utf-8")
        tmp.replace(self._path)

    def enqueue(self, target: ReviewTarget, target_id: str, reason: str, *,
                payload: dict[str, Any] | None = None, spot_check: bool = False,
                fingerprint: str | None = None) -> ReviewItem:
        iid = review_item_id(target, target_id, reason, fingerprint)
        if iid in self._items:
            return self._items[iid]
        item = ReviewItem(id=iid, target=target, target_id=target_id, reason=reason,
                          spot_check=spot_check, payload=payload or {},
                          fingerprint=fingerprint)
        self._items[iid] = item
        self._save()
        return item

    def get(self, item_id: str) -> ReviewItem:
        return self._items[item_id]

    def items(self, *, state: ReviewState | None = None,
              target: ReviewTarget | None = None) -> list[ReviewItem]:
        return [i for i in self._items.values()
                if (state is None or i.state is state) and (target is None or i.target is target)]

    def decide(self, item_id: str, state: ReviewState, *, decided_by: str, now: datetime,
               note: str = "") -> ReviewItem:
        if state is ReviewState.OPEN:
            raise ValueError("a decision must be approved or rejected, not open")
        if not decided_by.strip():
            raise ValueError("decided_by must name the human reviewer")
        item = self._items[item_id]
        if item.state is not ReviewState.OPEN:
            raise ValueError(f"review item {item_id} is already decided ({item.state})")
        done = ReviewItem.model_validate({**item.model_dump(), "state": state,
                                          "decided_by": decided_by, "decided_at": now,
                                          "note": note})
        self._items[item_id] = done
        self._save()
        return done

    def resolve(self, target: ReviewTarget, target_id: str,
                current: ReviewStatus, fingerprint: str | None = None, *,
                reason: str | None = None) -> ReviewStatus | None:
        """Fold decisions into a status. With a fingerprint, only items about that exact
        content (or fingerprint-less items) count, so decisions never carry to new content.
        With a reason, only items of that reason count (independent reviews of one target)."""
        items = [i for i in self._items.values()
                 if i.target is target and i.target_id == target_id
                 and (fingerprint is None or i.fingerprint in (None, fingerprint))
                 and (reason is None or i.reason == reason)]
        if any(i.state is ReviewState.REJECTED for i in items):
            return None
        if any(i.state is ReviewState.OPEN and not i.spot_check for i in items):
            return ReviewStatus.FLAGGED
        if any(i.state is ReviewState.APPROVED for i in items):
            return ReviewStatus.HUMAN_APPROVED
        return current
