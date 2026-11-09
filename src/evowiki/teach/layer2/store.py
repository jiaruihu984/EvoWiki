"""Layer-2 storage: each change's outcome as an append-only series of points ("effect over time"),
and the method registry of the layer-2 entries (A2 = FitModel versions;
rules in `method_registry`) with the method changes shown in 参数.
`store.rsi_layer2.PgLayer2Store` (migrations 0041, 0042) has the same semantics as
`MemoryLayer2Store`: points are never updated or deleted; `outcome_points` returns them oldest first
(by `at`, then insertion), timestamps in UTC. Method versions are append-only and numbered
1, 2, 3 … per (user, kind) — `add_method_version` refuses any other number; `method_versions`
returns them by version. Method changes are upserted by change id (a change id belongs to one
user) and returned oldest first (created_at, then change id). Several writes that belong together
(a version and its change row) go in `transaction(store)`."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Protocol

from evowiki.teach import anchors
from evowiki.teach.contracts import MethodChange, MethodKind, MethodVersion, OutcomeOut


class Layer2Store(Protocol):
    def outcome_points(self, user_id: str, change_id: str | None = None) -> list[OutcomeOut]: ...

    def add_outcome_point(self, user_id: str, o: OutcomeOut) -> None: ...

    def method_versions(self, user_id: str, kind: MethodKind) -> list[MethodVersion]: ...

    def add_method_version(self, user_id: str, v: MethodVersion) -> None: ...

    def method_changes(self, user_id: str, kind: MethodKind | None = None) -> list[MethodChange]: ...

    def put_method_change(self, user_id: str, c: MethodChange) -> None: ...


@contextmanager
def transaction(store: Layer2Store) -> Iterator[None]:
    """The store's own transaction when it has one (`PgLayer2Store.transaction`, shared with the
    other stores on the same pool), so a version and its change row are written together; nothing
    for a store without one (`MemoryLayer2Store`)."""
    tx = getattr(store, "transaction", None)
    if tx is None:
        yield
    else:
        with tx():
            yield


def _utc(o: OutcomeOut) -> OutcomeOut:
    return o.model_copy(update={"at": o.at.astimezone(UTC)})


def _need_user(user_id: str) -> None:
    if not user_id:
        raise ValueError("user_id must be non-empty")


class MemoryLayer2Store:
    def __init__(self) -> None:
        self._points: dict[str, list[OutcomeOut]] = {}
        self._versions: dict[tuple[str, str], list[MethodVersion]] = {}
        self._changes: dict[str, tuple[str, MethodChange]] = {}

    def outcome_points(self, user_id: str, change_id: str | None = None) -> list[OutcomeOut]:
        pts = [o for o in self._points.get(user_id, []) if change_id is None or o.change_id == change_id]
        return sorted(pts, key=lambda o: o.at)                 # stable: insertion order within `at`

    def add_outcome_point(self, user_id: str, o: OutcomeOut) -> None:
        _need_user(user_id)
        self._points.setdefault(user_id, []).append(_utc(o))

    def method_versions(self, user_id: str, kind: MethodKind) -> list[MethodVersion]:
        return list(self._versions.get((user_id, kind), []))

    def add_method_version(self, user_id: str, v: MethodVersion) -> None:
        _need_user(user_id)
        rows = self._versions.setdefault((user_id, v.kind), [])
        v.require_next(user_id, rows[-1].version if rows else 0)
        rows.append(v.model_copy(update={"at": v.at.astimezone(UTC)}))

    def method_changes(self, user_id: str, kind: MethodKind | None = None) -> list[MethodChange]:
        out = [c for u, c in self._changes.values()
               if u == user_id and (kind is None or c.kind == kind)]
        return sorted(out, key=lambda c: (c.created_at, c.change_id))

    def put_method_change(self, user_id: str, c: MethodChange) -> None:
        _need_user(user_id)
        held = self._changes.get(c.change_id)
        if held is not None and held[0] != user_id:
            raise ValueError(f"method change {c.change_id} belongs to another user")
        self._changes[c.change_id] = (user_id, c)


MAX_SHOWN_POINTS = 20         # ChangeOut.effect carries at most the newest this many points


def n_after(o: OutcomeOut | None) -> int:
    """A point's evidence after the change: its largest per-metric n_after (0 for none)."""
    return 0 if o is None else max((d.n_after for d in o.deltas), default=0)


def due_for_point(points: Sequence[OutcomeOut], n_now: int) -> bool:
    """Bounded per-cycle work: a change's outcome is recomputed (replays, bootstraps) only when
    its after-side evidence (`n_now`, a cheap count of what the outcome would report as n_after)
    grew by at least `anchors.MIN_EVENTS` since its last stored point (none: since 0)."""
    return n_now - n_after(points[-1] if points else None) >= anchors.MIN_EVENTS


def is_new_point(last: OutcomeOut | None, o: OutcomeOut) -> bool:
    """A point is worth appending when the verdict changed or the evidence after the change grew
    by at least `anchors.MIN_EVENTS` since the last point (the first point: once it has that much
    evidence or a verdict)."""
    if last is None:
        return n_after(o) >= anchors.MIN_EVENTS or o.verdict != "pending"
    if o.verdict != last.verdict:
        return True
    return n_after(o) - n_after(last) >= anchors.MIN_EVENTS


def record_point(store: Layer2Store, user_id: str, o: OutcomeOut) -> bool:
    """Append `o` to its change's series when it is a new point; True when appended."""
    pts: Sequence[OutcomeOut] = store.outcome_points(user_id, o.change_id)
    if not is_new_point(pts[-1] if pts else None, o):
        return False
    store.add_outcome_point(user_id, o)
    return True


def add_final_point(store: Layer2Store, user_id: str, change_id: str, note: str,
                    now: datetime) -> None:
    """The point written when a change stops being applied (or its rollback is blocked): a copy of
    its last point (verdict, deltas; a pending one when there is none) at `now` with `note`
    (`outcomes.NOTE_USER_UNDO` / `NOTE_AUTO_ROLLBACK` / `NOTE_ROLLBACK_BLOCKED`). Same evidence,
    so it never confirms a harmful verdict by itself."""
    pts = store.outcome_points(user_id, change_id)
    last = pts[-1] if pts else OutcomeOut(change_id=change_id, at=now, verdict="pending")
    store.add_outcome_point(user_id, last.model_copy(update={"at": now, "note": note}))


def _n(o: OutcomeOut) -> int:
    return sum(d.n_after for d in o.deltas)


def confirmed_harmful(points: Sequence[OutcomeOut]) -> bool:
    """The automatic-rollback rule: the last two recorded points are both harmful and the newer one
    has more evidence after the change (one harmful look is never enough)."""
    return (len(points) >= 2 and points[-1].verdict == "harmful"
            and points[-2].verdict == "harmful" and _n(points[-1]) > _n(points[-2]))
