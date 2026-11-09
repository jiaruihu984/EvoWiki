"""Forged tools in the one RSI change list as track "H" (design 2026-10-22 §6.1): a
`teach.change_source.ChangeSource` over `forge.undo`. Ids are the registry's `forge:<scope>:<n>`.
The plugin accepts track "H" since harness phase 2 merged (`PLUGIN_ACCEPTS_H`; False hides every
row: `forge_change_sources` returns ()). Text for the learner is plain:
reasons, evidence and cost live in `why` (the ? tooltip) only."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from psycopg_pool import ConnectionPool

from evowiki.forge import undo as forge_undo
from evowiki.forge.models import ForgeForbidden, PromoteRefused
from evowiki.teach.contracts import ChangeOut

PLUGIN_ACCEPTS_H = True
PREFIX = "forge:"
TARGET = "工具"
PATCH_TARGET = "代码"


def _out(row: dict[str, Any]) -> ChangeOut:
    cost = row.get("cost_usd")
    cost_line = f"花费 ${cost:.2f}" if cost is not None else "花费未知"
    why = "；".join(x for x in (row.get("why") or "", cost_line) if x)
    status = row.get("status") or ("rolled_back" if row["undone"] else "personal_accepted")
    return ChangeOut(patch_id=row["change_id"], track="H", layer=0, target=TARGET, status=status,
                     summary=row["text"], why=why, signals=(), at=row["at"],
                     can_undo=row["can_undo"])


class ForgeChangeSource:
    rsi_related = True
    prefix = PREFIX

    def __init__(self, pool: ConnectionPool, clock: Callable[[], datetime]) -> None:
        self.pool = pool
        self.clock = clock

    def list(self, user_id: str) -> list[ChangeOut]:
        with self.pool.connection() as conn:
            return [_out(r) for r in forge_undo.changes(conn, user_id)]

    def undo(self, user_id: str, change_id: str, *, now: datetime) -> ChangeOut:
        with self.pool.connection() as conn:
            row = next((r for r in forge_undo.changes(conn, user_id)
                        if r["change_id"] == change_id), None)
            if row is None:
                raise LookupError(f"没有这次工具改动：{change_id}")
            if not row["can_undo"]:
                raise ValueError("这次改动已经撤销过了")
            try:
                forge_undo.undo(conn, user_id, change_id)   # masking undo returns None: not a version
            except ForgeForbidden as e:
                raise ValueError("这项改动不能由你撤销") from e
            row = next(r for r in forge_undo.changes(conn, user_id)
                       if r["change_id"] == change_id)
            return _out(row)


class Refused(ValueError):
    """A refused approve / discard (the route answers 409); `error_code` is the response's machine
    `code` when there is one (`patches_off`: approve while code patches are switched off)."""

    def __init__(self, *args: object, error_code: str | None = None) -> None:
        super().__init__(*args)
        self.error_code = error_code


class ForgePatchChangeSource:
    rsi_related = True
    """L2 code patches (`forgep:<candidate_id>`), review mode (ruling 2026-10-23): a built patch is
    one row awaiting the user's review (`approve` / `discard`); a landed one can be undone."""
    prefix = forge_undo.PATCH_PREFIX

    def __init__(self, pool: ConnectionPool, clock: Callable[[], datetime]) -> None:
        self.pool = pool
        self.clock = clock

    @staticmethod
    def _out(conn: Any, user_id: str, row: dict[str, Any]) -> ChangeOut:
        """A patch a harness round evaluated carries its observation points like an h_ row, so a
        patch the system reverted as harmful reads 已自动撤销, not as the learner's undo."""
        from evowiki.harness.change_source import outcome_fields
        from evowiki.store.harness_rsi import patch_observations
        out = _out(row)
        cid = row["change_id"].removeprefix(forge_undo.PATCH_PREFIX)
        obs = outcome_fields(patch_observations(conn, user_id, cid), prefix=forge_undo.PATCH_PREFIX,
                             rolled_back=out.status == "rolled_back")
        return out.model_copy(update={"target": PATCH_TARGET, **obs})

    def list(self, user_id: str) -> list[ChangeOut]:
        with self.pool.connection() as conn:
            return [self._out(conn, user_id, r) for r in forge_undo.patch_changes(conn, user_id)]

    def _row(self, conn: Any, user_id: str, change_id: str) -> dict[str, Any]:
        row = next((r for r in forge_undo.patch_changes(conn, user_id)
                    if r["change_id"] == change_id), None)
        if row is None:
            raise LookupError(f"没有这次代码改动：{change_id}")
        return row

    def undo(self, user_id: str, change_id: str, *, now: datetime) -> ChangeOut:
        with self.pool.connection() as conn:
            if not self._row(conn, user_id, change_id)["can_undo"]:
                raise ValueError("这次改动已经撤销过了")
            forge_undo.undo_patch(conn, user_id, change_id, now=now)
            return self._out(conn, user_id, self._row(conn, user_id, change_id))

    def _decide(self, user_id: str, change_id: str, *, approve: bool, now: datetime) -> ChangeOut:
        with self.pool.connection() as conn:
            if self._row(conn, user_id, change_id)["status"] != "awaiting_user":
                raise ValueError("这项改动不在等你审阅")
            try:
                forge_undo.decide_patch(conn, user_id, change_id, approve=approve, now=now)
            except PromoteRefused as e:
                raise Refused(str(e), error_code=getattr(e, "error_code", None)) from e
            return self._out(conn, user_id, self._row(conn, user_id, change_id))

    def approve(self, user_id: str, change_id: str, *, now: datetime) -> ChangeOut:
        return self._decide(user_id, change_id, approve=True, now=now)

    def discard(self, user_id: str, change_id: str, *, now: datetime) -> ChangeOut:
        return self._decide(user_id, change_id, approve=False, now=now)


def forge_change_sources(pool: ConnectionPool, clock: Callable[[], datetime]
                         ) -> tuple[ForgeChangeSource | ForgePatchChangeSource, ...]:
    if not PLUGIN_ACCEPTS_H:
        return ()
    return (ForgeChangeSource(pool, clock), ForgePatchChangeSource(pool, clock))
