"""The out-of-database forget ledger and the startup reconcile (plan Task 14; memory spec §4.3).

The ledger (`EVOWIKI_FORGET_LEDGER`, default `.evowiki-run/forget-ledger.jsonl` of the
checkout, outside any database backup) is append-only JSON lines, fsynced per row: a request's
`requested` row says the learner's request is durably recorded (not that it is done); `committed`
follows the database commit; `superseded` closes a request whose database row was lost (an old
backup restored) and that was made again under a new id (`replaces`), since the 0082 channel
mints request ids itself.

`reconcile` runs before the service answers the assistant: every ledger request is brought back
and run until committed, committed ones are checked again (a restored backup brings deleted rows
back; a clean database makes that a no-op), and any failure raises ReconcileError: the
assistant stays closed (503) and the reconcile is tried again."""

import json
import logging
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evowiki.memory.forget import ForgetService, residue
from evowiki.store.db import fetch_all, fetch_one

FORGET_LEDGER_ENV = "EVOWIKI_FORGET_LEDGER"
DEFAULT_LEDGER = Path(__file__).resolve().parents[3] / ".evowiki-run" / "forget-ledger.jsonl"
RETRY_SECONDS = 60.0
CLOSED_DETAIL = "正在核对删除记录"

log = logging.getLogger(__name__)


class LedgerError(RuntimeError):
    """The ledger file cannot be written, or a line of it cannot be read."""


class ReconcileError(RuntimeError):
    pass


@dataclass(frozen=True)
class LedgerRow:
    request_id: str
    user_id: str
    session_id: str | None
    item_id: str | None
    state: str                                   # requested | committed | superseded
    at: str
    revoke_rule_ids: tuple[str, ...] = ()
    revoke_fact_keys: tuple[str, ...] = ()
    delete_source_ids: tuple[str, ...] = ()
    replaces: str | None = None


def ledger_path(env: Any = os.environ) -> Path:
    return Path(env.get(FORGET_LEDGER_ENV) or DEFAULT_LEDGER)


class Ledger:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, request_id: str, user_id: str, session_id: str | None,
               item_id: str | None, state: str, *, revoke_rule_ids: Sequence[str] = (),
               revoke_fact_keys: Sequence[str] = (), delete_source_ids: Sequence[str] = (),
               replaces: str | None = None) -> None:
        row = {"request_id": request_id, "user_id": user_id, "session_id": session_id,
               "item_id": item_id, "state": state, "at": datetime.now(UTC).isoformat()}
        if revoke_rule_ids:
            row["revoke_rule_ids"] = list(revoke_rule_ids)
        if revoke_fact_keys:
            row["revoke_fact_keys"] = list(revoke_fact_keys)
        if delete_source_ids:
            row["delete_source_ids"] = list(delete_source_ids)
        if replaces is not None:
            row["replaces"] = replaces
        line = json.dumps(row, ensure_ascii=False) + "\n"
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
        except OSError as exc:
            raise LedgerError(f"forget ledger {self._path} not written: {exc}") from exc

    def rows(self) -> list[LedgerRow]:
        if not self._path.exists():
            return []
        out = []
        with self._lock, open(self._path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    out.append(LedgerRow(
                        request_id=d["request_id"], user_id=d["user_id"],
                        session_id=d.get("session_id"), item_id=d.get("item_id"),
                        state=d["state"], at=d["at"],
                        revoke_rule_ids=tuple(d.get("revoke_rule_ids") or ()),
                        revoke_fact_keys=tuple(d.get("revoke_fact_keys") or ()),
                        delete_source_ids=tuple(d.get("delete_source_ids") or ()),
                        replaces=d.get("replaces")))
                except (ValueError, KeyError, TypeError) as exc:
                    raise LedgerError(f"forget ledger {self._path} line {n} unreadable") from exc
        return out

    def requests(self) -> dict[str, LedgerRow]:
        """Each request's latest state; the choices (rules / facts to revoke, materials to
        delete) are the ones its rows first carried."""
        out: dict[str, LedgerRow] = {}
        for r in self.rows():
            prev = out.get(r.request_id)
            if prev is not None:
                r = LedgerRow(**{**r.__dict__,
                                 "revoke_rule_ids": r.revoke_rule_ids or prev.revoke_rule_ids,
                                 "revoke_fact_keys": r.revoke_fact_keys or prev.revoke_fact_keys,
                                 "delete_source_ids": (r.delete_source_ids
                                                       or prev.delete_source_ids),
                                 "replaces": r.replaces or prev.replaces})
            out[r.request_id] = r
        return out

    def revoke_of(self, request_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        r = self.requests().get(request_id)
        return ((), ()) if r is None else (r.revoke_rule_ids, r.revoke_fact_keys)

    def sources_of(self, request_id: str) -> tuple[str, ...]:
        r = self.requests().get(request_id)
        return () if r is None else r.delete_source_ids


@dataclass
class ReconcileReport:
    recreated: list[str] = field(default_factory=list)      # new request ids
    run: list[str] = field(default_factory=list)            # request ids run (all committed)
    verified: int = 0


def _exists(conn, row: LedgerRow) -> bool:
    if row.session_id is not None:
        return fetch_one(conn, "select 1 from assistant_session where session_id = %s "
                               "and user_id = %s", (row.session_id, row.user_id)) is not None
    return fetch_one(conn, "select 1 from memory_item where item_id = %s and user_id = %s",
                     (row.item_id, row.user_id)) is not None


def _item_left(conn, row: LedgerRow) -> list[str]:
    return ["memory_item"] if _exists(conn, row) else []


def _remake(pool, ledger: Ledger, row: LedgerRow, report: ReconcileReport) -> str | None:
    """A new request for the same session / item when something of it is stored; None when the
    database holds nothing of it. Raises ReconcileError when rows remain but the session row
    itself is gone (the channel cannot name it)."""
    with pool.connection() as conn:
        left = (residue(conn, row.user_id, row.session_id) if row.session_id is not None
                else _item_left(conn, row))
        if not left:
            return None
        if not _exists(conn, row):
            raise ReconcileError(f"forget request {row.request_id}: rows left ({left}) "
                                 f"but its session is gone")
    with pool.connection() as conn, conn.transaction():
        rid = conn.execute("select evowiki_forget_request(%s, %s, %s, 'reconcile') as rid",
                           (row.user_id, row.session_id,
                            row.item_id if row.session_id is None else None)
                           ).fetchone()["rid"]
        ledger.append(rid, row.user_id, row.session_id, row.item_id, "requested",
                      revoke_rule_ids=row.revoke_rule_ids,
                      revoke_fact_keys=row.revoke_fact_keys,
                      delete_source_ids=row.delete_source_ids, replaces=row.request_id)
        ledger.append(row.request_id, row.user_id, row.session_id, row.item_id, "superseded",
                      replaces=None)
    report.recreated.append(rid)
    return rid


def reconcile(pool, ledger: Ledger, service: ForgetService) -> ReconcileReport:
    """Bring the database in line with the ledger (module doc); ReconcileError on any failure."""
    report = ReconcileReport()
    try:
        rows = ledger.requests()
    except LedgerError as exc:
        raise ReconcileError(str(exc)) from exc
    with pool.connection() as conn:
        db = {r["request_id"]: r for r in fetch_all(
            conn, "select request_id, user_id, session_id, item_id, state from forget_request")}
    todo: list[str] = []
    for row in rows.values():
        if row.state == "superseded":
            continue
        got = db.get(row.request_id)
        if got is None or got["state"] == "committed":
            # lost (a backup from before it) or committed: anything stored again is redone
            rid = _remake(pool, ledger, row, report)
            if rid is not None:
                todo.append(rid)
            elif got is not None and row.state != "committed":
                ledger.append(row.request_id, row.user_id, row.session_id, row.item_id,
                              "committed")
        else:
            todo.append(row.request_id)
    # a request the ledger does not know (its committed row was not written, or the ledger
    # was lost): run it too, and record it
    for rid, r in db.items():
        if r["state"] != "committed" and rid not in rows and rid not in todo:
            ledger.append(rid, r["user_id"], r["session_id"], r["item_id"], "requested")
            todo.append(rid)
    for rid in dict.fromkeys(todo):
        out = service.run(rid)
        if out.state != "committed":
            raise ReconcileError(f"forget request {rid} did not commit: {out.error}")
        report.run.append(rid)
    with pool.connection() as conn:
        for row in ledger.requests().values():
            if row.state == "superseded":
                continue
            left = (residue(conn, row.user_id, row.session_id) if row.session_id is not None
                    else _item_left(conn, row))
            if left:
                raise ReconcileError(f"forget request {row.request_id}: still stored {left}")
            report.verified += 1
    return report


def start_background(state: Any, pool, ledger: Ledger, service: ForgetService,
                     *, every: float = RETRY_SECONDS) -> tuple[threading.Thread, threading.Event]:
    """Until `reconcile` passes, try it every `every` seconds (`state.memory_ready` stays
    False); after that, retry the requests that have not committed (a DELETE answered 202)."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                if not state.memory_ready:
                    reconcile(pool, ledger, service)
                    state.memory_ready = True
                    log.info("forget reconcile passed; the assistant is open")
                else:
                    for out in service.retry_open():
                        if out.state != "committed":
                            log.warning("forget request %s still failing: %s", out.request_id,
                                        out.error)
            except Exception:
                log.exception("forget reconcile failed; the assistant stays closed")
            stop.wait(every)

    t = threading.Thread(target=loop, name="forget-reconcile", daemon=True)
    t.start()
    return t, stop
