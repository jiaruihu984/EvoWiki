"""Policy Registry (architecture spec §7.5): versioned θ_infer,u (track A) and θ_rep,u (track B) per user.

A user with no rows is on an implicit v1: `InferParams()` for track A; for track B, whatever the user's
`SpecStore` holds now (imported / hand-written rules), or `TeachingSpec(version=1, items=())` when reading
without a spec store. `current` returns it without writing. The first `commit` / `rollback` writes that v1
row first, so the chain is complete and a rollback to 1 restores the user's real starting spec, then appends
last + 1. Rollback never deletes: it appends a new version that copies an older one's content (patch_id
None). For track B the stored spec's own `version` is set to the policy version, and the spec is also
written to the `SpecStore` every writer reads; a B write without a spec store fails closed.

The v1 write and the vN write are two store calls, not one transaction: if the second fails, v1 stays
(harmless — it is the user's current policy). A SQL store should wrap both in one transaction."""

from datetime import UTC, datetime

from evowiki.teach.contracts import InferParams, PolicyVersion, TeachingSpec, Track
from evowiki.teach.rsi_store import RsiStore, SpecStore

_IMPLICIT_AT = datetime(1970, 1, 1, tzinfo=UTC)


def _implicit_v1(user_id: str, track: Track, at: datetime, spec_store: SpecStore | None) -> PolicyVersion:
    if track == "A":
        return PolicyVersion(track="A", version=1, at=at, infer=InferParams())
    spec = (spec_store.get_spec(user_id).model_copy(update={"version": 1}) if spec_store is not None
            else TeachingSpec(version=1, items=()))
    return PolicyVersion(track="B", version=1, at=at, spec=spec)


def current(store: RsiStore, user_id: str, track: Track, spec_store: SpecStore | None = None) -> PolicyVersion:
    rows = store.versions(user_id, track)
    return rows[-1] if rows else _implicit_v1(user_id, track, _IMPLICIT_AT, spec_store)


def _append(store: RsiStore, user_id: str, track: Track, *, infer: InferParams | None,
            spec: TeachingSpec | None, patch_id: str | None, now: datetime,
            spec_store: SpecStore | None) -> PolicyVersion:
    rows = store.versions(user_id, track)
    if not rows:
        store.add_version(user_id, _implicit_v1(user_id, track, now, spec_store))
        last = 1
    else:
        last = rows[-1].version
    new = last + 1
    if spec is not None:
        spec = spec.model_copy(update={"version": new})
    v = PolicyVersion(track=track, version=new, at=now, patch_id=patch_id, infer=infer, spec=spec)
    store.add_version(user_id, v)
    if spec is not None and spec_store is not None:
        spec_store.put_spec(user_id, spec)
    return v


TRACK_B_CLOSED = ("track B writes go through wikiapp.teach.commit_spec (rule provenance contract "
                  "§7.1 #20): this registry no longer writes teaching specs")


def _need_spec_store(track: Track, spec_store: SpecStore | None) -> None:
    """Track B is closed here: every teaching-spec write must pass the rule checkpoint, which this
    registry (a `SpecStore.put_spec` beside it) cannot; no product code wrote B through it."""
    if track == "B":
        raise ValueError(TRACK_B_CLOSED)


def commit(store: RsiStore, user_id: str, track: Track, *, infer: InferParams | None = None,
           spec: TeachingSpec | None = None, patch_id: str | None, now: datetime,
           spec_store: SpecStore | None = None) -> PolicyVersion:
    if track == "A" and (infer is None or spec is not None):
        raise ValueError("track A commits carry infer only")
    if track == "B" and (spec is None or infer is not None):
        raise ValueError("track B commits carry spec only")
    _need_spec_store(track, spec_store)
    return _append(store, user_id, track, infer=infer, spec=spec, patch_id=patch_id, now=now,
                   spec_store=spec_store)


def rollback(store: RsiStore, user_id: str, track: Track, version: int, *, now: datetime,
             spec_store: SpecStore | None = None) -> PolicyVersion:
    _need_spec_store(track, spec_store)
    rows = store.versions(user_id, track) or [_implicit_v1(user_id, track, _IMPLICIT_AT, spec_store)]
    target = next((r for r in rows if r.version == version), None)
    if target is None:
        raise ValueError(f"{user_id}/{track}: no version {version}")
    return _append(store, user_id, track, infer=target.infer, spec=target.spec, patch_id=None, now=now,
                   spec_store=spec_store)


def infer_for(store: RsiStore, user_id: str) -> InferParams:
    infer = current(store, user_id, "A").infer
    if infer is None:                            # PolicyVersion's validator makes this unreachable
        raise ValueError("track A version without infer")
    return infer
