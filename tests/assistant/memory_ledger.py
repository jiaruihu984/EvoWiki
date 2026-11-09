"""An in-memory stand-in for `bot.actions.ActionLedger`: the calls the assistant's action tools,
`confirm` and `undo` make, with the real executors' `execute` / `undo` (no database: the effect
gets no connection). For tests of the stage 3 flow without Postgres."""

from dataclasses import replace
from datetime import datetime
from typing import Any

from evowiki.agent.effects import payload_hash
from evowiki.bot.actions import NotDecidable, can_undo
from evowiki.store.bot import ActionRow
from tests.assistant.fakes import T0


class MemoryLedger:
    def __init__(self, executors: dict[str, Any] | None = None, clock=lambda: T0):
        self._ex = dict(executors or {})
        self._clock = clock
        self.rows: dict[str, ActionRow] = {}
        self._n = 0

    def register(self, kind: str, executor: Any) -> None:
        self._ex[kind] = executor

    def _new(self, user_id: str, *, kind: str, scope: str, source: str, summary: str,
             content: dict, key: str, status: str, level: str, why: str = "",
             page_id: str | None = None, page_version: str | None = None,
             result: dict | None = None) -> ActionRow:
        self._n += 1
        row = ActionRow(action_id=f"act_{self._n}", user_id=user_id, kind=kind, scope=scope,
                        level=level, status=status, source=source, summary=summary,
                        content=dict(content), content_hash=payload_hash(content),
                        idempotency_key=key, created_at=self._clock(), why=why,
                        page_id=page_id, page_version=page_version, result=result)
        self.rows[row.action_id] = row
        return row

    def get_by_key(self, key: str) -> ActionRow | None:
        return next((r for r in self.rows.values() if r.idempotency_key == key), None)

    def get(self, user_id: str, action_id: str) -> ActionRow | None:
        r = self.rows.get(action_id)
        return r if r is not None and r.user_id == user_id else None

    def list(self, user_id: str, *, status: str | None = None, limit: int = 100,
             writes: bool = False) -> list[ActionRow]:
        rows = [r for r in reversed(list(self.rows.values()))
                if r.user_id == user_id and (status is None or r.status == status)]
        return rows[:limit]

    def record(self, user_id: str, **kw: Any) -> None:
        kw.pop("wiki_event_ids", None)
        kw.pop("undo", None)
        status = kw.pop("status", "executed")
        level = "auto" if kw.get("scope") == "suggestion" else "auto_undo"
        self._new(user_id, status=status, level=level, **kw)

    def propose(self, user_id: str, *, level: str, approved_now: bool = False,
                **kw: Any) -> ActionRow:
        return self._new(user_id, status="approved" if approved_now else "proposed",
                         level=level, **kw)

    def approve(self, user_id: str, action_id: str, *, draft: dict | None = None) -> ActionRow:
        row = self.get(user_id, action_id)
        if row is None:
            raise LookupError(action_id)
        if row.status == "executed":
            return row
        if row.status not in ("proposed", "approved"):
            raise NotDecidable(f"这条改动{row.status}，不能确认")
        out = self._ex[row.kind].execute(user_id, row, lambda conn: None)
        row = replace(row, status="executed", result={**(row.result or {}), **(out or {})},
                      decided_at=datetime.now(T0.tzinfo))
        self.rows[action_id] = row
        return row

    def reject(self, user_id: str, action_id: str) -> ActionRow:
        row = replace(self.rows[action_id], status="rejected")
        self.rows[action_id] = row
        return row

    def undo(self, user_id: str, action_id: str) -> ActionRow:
        row = self.get(user_id, action_id)
        if row is None or row.status != "executed" or not can_undo(row):
            raise NotDecidable("不能撤销")
        self._ex[row.kind].undo(user_id, row)
        row = replace(row, status="undone")
        self.rows[action_id] = row
        return row

    def add_result_keys(self, user_id: str, action_id: str, keys: dict) -> bool:
        row = self.get(user_id, action_id)
        if row is None:
            return False
        self.rows[action_id] = replace(row, result={**(row.result or {}), **keys})
        return True


class RecordingExecutor:
    """A kind's executor that only records what ran (and can undo it)."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.undone: list[str] = []

    def execute(self, user_id: str, action: ActionRow, effect) -> dict:
        effect(None)
        self.executed.append(action.action_id)
        return {"done": True}

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    def undo(self, user_id: str, action: ActionRow) -> dict:
        self.undone.append(action.action_id)
        return {}
