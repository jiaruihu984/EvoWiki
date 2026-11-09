"""Human-reviewed perspective presumption revisions (Plan 4b-1d Task 4; design §4).

`presumes_required` = concepts without which the perspective cannot be understood correctly;
`presumes_helpful` = useful but not necessary. A revision only *moves* concepts between the two
lists: the union is unchanged (nothing invented or dropped), and no other field is touched. It
is a review item (target PERSPECTIVE_PRESUMES) bound to the perspective's metadata + text
fingerprint; the snapshot applies it only once a human approved it, and refuses to build if an
approved revision no longer matches the current perspective (fail closed: re-review)."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evowiki.content.models import Perspective, Segment
from evowiki.pipeline.covers import perspective_text_fingerprint
from evowiki.pipeline.review import ReviewItem, ReviewQueue, ReviewState, ReviewTarget

PRESUMES_REVISION_REASON = "presumes_revision"
# A separate, explicitly additive correction for a perspective whose presumption union misses a
# concept its text needs (H1 ruling: never smuggled in through a move). Applied after the move.
PRESUMES_CORRECTION_REASON = "presumes_union_correction"


class PresumesRevisionError(ValueError):
    pass


class PresumesRevision(BaseModel):
    """One approved revision, as the snapshot reads it from the review queue."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    segment_id: str
    fingerprint: str
    presumes_required: tuple[str, ...]
    presumes_helpful: tuple[str, ...]
    reason: str = Field(min_length=1)
    decided_by: str = Field(min_length=1)

    @model_validator(mode="after")
    def _disjoint(self) -> "PresumesRevision":
        if set(self.presumes_required) & set(self.presumes_helpful):
            raise ValueError("presumes_required and presumes_helpful overlap")
        return self


def presumes_fingerprint(p: Perspective, seg: Segment) -> str:
    """The perspective as produced by the pipeline (metadata incl. both presumes lists) + its
    text. Computed on the bundle's perspective, before snapshot-time review folding."""
    return perspective_text_fingerprint(p, seg)


def propose_presumes_revision(queue: ReviewQueue, p: Perspective, seg: Segment, *,
                              required: list[str], helpful: list[str],
                              reason: str) -> ReviewItem:
    """Enqueue a revision for human decision (`review approve <id> --by <name>`). Validated
    here as well as at snapshot: a move only."""
    _check_move(p, required, helpful)
    if not reason.strip():
        raise PresumesRevisionError("a revision needs a reason")
    return queue.enqueue(ReviewTarget.PERSPECTIVE_PRESUMES, p.segment_id,
                         PRESUMES_REVISION_REASON,
                         payload={"presumes_required": sorted(required),
                                  "presumes_helpful": sorted(helpful), "reason": reason},
                         fingerprint=presumes_fingerprint(p, seg))


def _check_move(p: Perspective, required, helpful) -> None:
    before = set(p.presumes_required) | set(p.presumes_helpful)
    after = set(required) | set(helpful)
    if after != before:
        raise PresumesRevisionError(
            f"{p.segment_id}: a revision must keep required ∪ helpful "
            f"(added {sorted(after - before)}, dropped {sorted(before - after)})")
    if set(required) & set(helpful):
        raise PresumesRevisionError(f"{p.segment_id}: required and helpful overlap")
    if len(set(required)) != len(list(required)) or len(set(helpful)) != len(list(helpful)):
        raise PresumesRevisionError(f"{p.segment_id}: a list repeats a concept")


# A replacement decision (Plan 4b-2 ruling 3) re-records an approved decision on a new
# perspective fingerprint; it names the decision it supersedes, which stays in the queue as
# immutable history and no longer applies. A replacement must have the same target and reason.
SUPERSEDES_KEY = "supersedes_decision_id"


def _superseded(queue: ReviewQueue) -> set[str]:
    approved = {i.id: i for i in queue.items(target=ReviewTarget.PERSPECTIVE_PRESUMES,
                                             state=ReviewState.APPROVED)}
    out: set[str] = set()
    for i in approved.values():
        old_id = i.payload.get(SUPERSEDES_KEY)
        if old_id is None:
            continue
        old = approved.get(old_id)
        if old is None or (old.target_id, old.reason) != (i.target_id, i.reason):
            raise PresumesRevisionError(
                f"{i.id}: supersedes {old_id}, which is not an approved decision of the same "
                "target and reason")
        if old_id in out:
            raise PresumesRevisionError(f"{old_id} is superseded twice")
        out.add(old_id)
    return out


def approved_revisions(queue: ReviewQueue, segment_id: str) -> list[PresumesRevision]:
    out = []
    gone = _superseded(queue)
    for i in queue.items(target=ReviewTarget.PERSPECTIVE_PRESUMES, state=ReviewState.APPROVED):
        if (i.target_id != segment_id or i.reason != PRESUMES_REVISION_REASON
                or i.id in gone):
            continue
        out.append(PresumesRevision(
            segment_id=i.target_id, fingerprint=i.fingerprint or "",
            presumes_required=tuple(i.payload["presumes_required"]),
            presumes_helpful=tuple(i.payload["presumes_helpful"]),
            reason=i.payload["reason"], decided_by=i.decided_by or ""))
    return out


def propose_presumes_correction(queue: ReviewQueue, p: Perspective, seg: Segment, *,
                                add_required: list[str], add_helpful: list[str],
                                reason: str) -> ReviewItem:
    """Enqueue an additive union correction (concepts not presumed at all so far)."""
    _check_addition(p, add_required, add_helpful, None)
    if not reason.strip():
        raise PresumesRevisionError("a correction needs a reason")
    return queue.enqueue(ReviewTarget.PERSPECTIVE_PRESUMES, p.segment_id,
                         PRESUMES_CORRECTION_REASON,
                         payload={"add_required": sorted(add_required),
                                  "add_helpful": sorted(add_helpful), "reason": reason},
                         fingerprint=presumes_fingerprint(p, seg))


def _check_addition(p: Perspective, add_required, add_helpful,
                    known: frozenset[str] | None) -> None:
    added = list(add_required) + list(add_helpful)
    if not added:
        raise PresumesRevisionError(f"{p.segment_id}: a correction must add a concept")
    if len(set(added)) != len(added):
        raise PresumesRevisionError(f"{p.segment_id}: a correction repeats a concept")
    present = set(p.presumes_required) | set(p.presumes_helpful)
    if set(added) & present:
        raise PresumesRevisionError(f"{p.segment_id}: a correction only adds concepts not yet "
                                    f"presumed ({sorted(set(added) & present)} already are)")
    if p.concept_id in added:
        raise PresumesRevisionError(f"{p.segment_id}: a perspective cannot presume its concept")
    if known is not None and not set(added) <= known:
        raise PresumesRevisionError(f"{p.segment_id}: unknown concepts "
                                    f"{sorted(set(added) - known)}")


def _approved(queue: ReviewQueue, segment_id: str, reason: str) -> list[ReviewItem]:
    gone = _superseded(queue)
    return [i for i in queue.items(target=ReviewTarget.PERSPECTIVE_PRESUMES,
                                   state=ReviewState.APPROVED)
            if i.target_id == segment_id and i.reason == reason and i.id not in gone]


def apply_presumes_correction(queue: ReviewQueue, original: Perspective, current: Perspective,
                              seg: Segment, known: frozenset[str]) -> Perspective:
    """`current` (after any move) plus the approved additive correction, if there is one."""
    items = _approved(queue, original.segment_id, PRESUMES_CORRECTION_REASON)
    if not items:
        return current
    if len(items) > 1:
        raise PresumesRevisionError(f"{original.segment_id}: {len(items)} approved corrections")
    [item] = items
    fp = presumes_fingerprint(original, seg)
    if item.fingerprint != fp:
        raise PresumesRevisionError(
            f"{original.segment_id}: approved correction fingerprint {(item.fingerprint or '')[:12]}"
            f" does not match the current perspective {fp[:12]}; re-review it")
    add_r, add_h = item.payload["add_required"], item.payload["add_helpful"]
    _check_addition(current, add_r, add_h, known)
    return current.model_copy(update={
        "presumes_required": [*current.presumes_required, *add_r],
        "presumes_helpful": [*current.presumes_helpful, *add_h]})


def apply_presumes_revision(queue: ReviewQueue, original: Perspective, current: Perspective,
                            seg: Segment) -> Perspective:
    """`current` with the approved revision's two lists, if there is one. `original` is the
    bundle's perspective (the fingerprint basis). Open or rejected revisions are ignored; an
    approved one that does not match the current fingerprint, or that is not a pure move,
    raises PresumesRevisionError; two approved revisions for one perspective also raise."""
    revs = approved_revisions(queue, original.segment_id)
    if not revs:
        return current
    if len(revs) > 1:
        raise PresumesRevisionError(f"{original.segment_id}: {len(revs)} approved revisions")
    [rev] = revs
    fp = presumes_fingerprint(original, seg)
    if rev.fingerprint != fp:
        raise PresumesRevisionError(
            f"{original.segment_id}: approved revision fingerprint {rev.fingerprint[:12]} does "
            f"not match the current perspective {fp[:12]}; re-review it")
    _check_move(original, list(rev.presumes_required), list(rev.presumes_helpful))
    return current.model_copy(update={"presumes_required": list(rev.presumes_required),
                                      "presumes_helpful": list(rev.presumes_helpful)})
