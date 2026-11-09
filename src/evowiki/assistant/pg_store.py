"""`SessionStore` on Postgres (migration 0020). Each method is one short transaction.

Reads leave out what an open forget request hides (`memory.forget.hidden_of`): the session being
deleted is unknown (LookupError / not listed), and a turn of another session that its content
reached reads as it will after the delete (`mask_entries`)."""

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from evowiki.assistant.session import (
    PAGE_TAPS,
    Entry,
    EntryConflict,
    EntryType,
    SessionInfo,
    same_entry,
)
from evowiki.store.db import fetch_all, fetch_one

_ENTRY_COLS = "session_id, seq, entry_id, type, payload, occurred_at as at"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_US = timedelta(microseconds=1)
_POSITION = re.compile(r"(\d+)-((?:[0-9a-f]{2})+)")


def _position(ingested_at: datetime, entry_id: str) -> str:
    """URL-safe position of an entry: exact epoch microseconds (timestamptz precision)."""
    return f"{(ingested_at - _EPOCH) // _US}-{entry_id.encode().hex()}"


def _parse_position(pos: str | None) -> tuple[datetime, str] | None:
    if pos is None or pos == "0":
        return None
    m = _POSITION.fullmatch(pos)
    if m is None:
        raise ValueError(f"bad assistant position {pos!r}")
    try:
        return _EPOCH + int(m[1]) * _US, bytes.fromhex(m[2]).decode()
    except (ValueError, OverflowError) as e:
        raise ValueError(f"bad assistant position {pos!r}") from e


if TYPE_CHECKING:
    from evowiki.bot.actions import ActionLedger
    from evowiki.memory.forget import Hidden


log = logging.getLogger(__name__)


def lock_consolidation(conn, session_id: str) -> None:
    """What a background write about a session and a forget request of it serialise on (inside the
    caller's transaction): whichever comes second sees the other's commit."""
    conn.execute("select pg_advisory_xact_lock(hashtext('consolidate:' || %s))", (session_id,))


def lock_session(conn, session_id: str) -> dict:
    """The session row lock (inside the caller's transaction): it serialises appenders, so
    max(seq) + 1 stays dense."""
    locked = fetch_one(conn, "select user_id from assistant_session "
                             "where session_id = %s for update", (session_id,))
    if locked is None:
        raise LookupError(f"unknown assistant session {session_id}")
    return locked


def insert_entry(conn, session_id: str, entry_id: str, type: EntryType, payload: dict[str, Any],
                 at: datetime) -> tuple[Entry, bool]:
    """Insert one entry at seq = last + 1, under the session row lock the caller holds; (entry,
    True) when stored now, (the stored entry, False) for a retry of the same entry. Raises
    EntryConflict for the same id with other content. Shared by `PgSessionStore.append` and the
    memory consolidation commit (memory/store.py)."""
    r = fetch_one(
        conn,
        "insert into assistant_entry (session_id, seq, entry_id, type, payload, "
        "occurred_at) select %s, coalesce(max(seq), 0) + 1, %s, %s, %s, %s "
        "from assistant_entry where session_id = %s "
        f"on conflict (entry_id) do nothing returning {_ENTRY_COLS}",
        (session_id, entry_id, type, Jsonb(payload), at, session_id))
    if r is not None:
        return Entry.model_validate(r), True
    r = fetch_one(conn, f"select {_ENTRY_COLS} from assistant_entry where entry_id = %s",
                  (entry_id,))
    stored = Entry.model_validate(r)
    if not same_entry(stored, session_id, type, payload):
        raise EntryConflict(f"entry id {entry_id} is already stored with other content")
    return stored, False


class PgSessionStore:
    """With a `ledger`, a stored `proposal` entry is mirrored as a pending `assistant_edit` action
    in the same transaction (so the item shows in 参数 at proposal time) when the user's
    `page_body` level is `ask`."""

    def __init__(self, pool: ConnectionPool, *, ledger: "ActionLedger | None" = None,
                 page_title: "Callable[[str, str], str | None] | None" = None):
        self._pool = pool
        self._ledger = ledger
        self._page_title = page_title       # (user_id, page_id) -> title, for the ledger summary

    def create(self, info: SessionInfo) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "insert into assistant_session (session_id, user_id, page_id, note_concept_id, "
                "created_at) values (%s, %s, %s, %s, %s)",
                (info.session_id, info.user_id, info.page_id, info.note_concept_id,
                 info.created_at))

    def _hidden(self, user_id: str) -> "Hidden":
        from evowiki.memory.forget import hidden_of
        with self._pool.connection() as conn:
            return hidden_of(conn, user_id)

    def info(self, session_id: str) -> SessionInfo:
        with self._pool.connection() as conn:
            r = fetch_one(conn, "select * from assistant_session where session_id = %s",
                          (session_id,))
        if r is None or self._hidden(r["user_id"]).session(session_id):
            raise LookupError(f"unknown assistant session {session_id}")
        return SessionInfo.model_validate(r)

    def append(self, session_id: str, entry_id: str, type: EntryType, payload: dict[str, Any],
               at: datetime) -> Entry:
        title = self._title_of(session_id, payload) if type == "proposal" else None
        with self._pool.connection() as conn, conn.transaction():
            return self._append(conn, session_id, entry_id, type, payload, at, title)

    def append_if_current(self, session_id: str, entry_id: str, type: EntryType,
                          payload: dict[str, Any], at: datetime, *, version: int) -> bool:
        from evowiki.memory.forget import hidden_of
        with self._pool.connection() as conn, conn.transaction():
            lock_consolidation(conn, session_id)
            uid = lock_session(conn, session_id)["user_id"]
            h = hidden_of(conn, uid)
            if h.paused or h.session(session_id):
                return False
            last = fetch_one(conn, "select coalesce(max(seq), 0) as seq from assistant_entry "
                                   "where session_id = %s and type in "
                                   "('user', 'assistant', 'turn_error')", (session_id,))["seq"]
            if last != version:
                return False
            self._append(conn, session_id, entry_id, type, payload, at)
            return True

    def _title_of(self, session_id: str, payload: dict[str, Any]) -> str | None:
        """The proposal's page title, resolved BEFORE the entry transaction opens (it takes its
        own connection); any failure -> None (the summary then names the section heading)."""
        if self._ledger is None or self._page_title is None:
            return None
        try:
            uid = self.info(session_id).user_id
            return self._page_title(uid, payload["draft"].get("page_id", ""))
        except Exception:
            log.warning("page title lookup failed for session %s", session_id, exc_info=True)
            return None

    def append_decision_once(self, session_id: str, entry_id: str, payload: dict[str, Any],
                             at: datetime) -> bool:
        """Append a `decision` unless the session already holds one for the proposal; the check
        and the insert run under the session row lock, so two writers cannot both append."""
        with self._pool.connection() as conn, conn.transaction():
            self._lock(conn, session_id)
            if fetch_one(conn, "select 1 from assistant_entry where session_id = %s "
                               "and type = 'decision' and payload ->> 'proposal_id' = %s",
                         (session_id, payload["proposal_id"])) is not None:
                return False
            self._append(conn, session_id, entry_id, "decision", payload, at)
            return True

    @staticmethod
    def _lock(conn, session_id: str) -> dict:
        return lock_session(conn, session_id)

    def _append(self, conn, session_id: str, entry_id: str, type: EntryType,
                payload: dict[str, Any], at: datetime, title: str | None = None) -> Entry:
        locked = lock_session(conn, session_id)
        entry, new = insert_entry(conn, session_id, entry_id, type, payload, at)
        if not new:
            return entry         # a retry (a durable step re-run): its side effects already ran
        if type in ("assistant", "compaction"):      # artifacts (0091), in this transaction
            from evowiki.assistant.lineage import entry_lineage
            from evowiki.store.artifacts import put_lineage
            prev = None
            if type == "compaction":
                r = fetch_one(conn, f"select {_ENTRY_COLS} from assistant_entry where "
                                    "session_id = %s and type = 'compaction' and seq < %s "
                                    "order by seq desc limit 1", (session_id, entry.seq))
                prev = Entry.model_validate(r) if r is not None else None
            put_lineage(conn, entry_lineage(locked["user_id"], entry, prev))
        if type == "proposal" and self._ledger is not None:
            from evowiki.assistant.edit_executor import proposal_action
            uid = locked["user_id"]
            # level `ask`: pending in 参数 from now on; `auto_undo`: the panel's 加进去 is the
            # approval, the row is created when the user accepts
            if self._ledger.level_in(conn, uid, "page_body") == "ask":
                self._ledger.propose_in(conn, uid, **proposal_action(payload, session_id,
                                                                     page_title=title))
        return entry

    def request_stop(self, session_id: str, message_id: str, at: datetime) -> None:
        with self._pool.connection() as conn:
            conn.execute("insert into assistant_turn_stop (session_id, message_id, requested_at) "
                         "values (%s, %s, %s) on conflict do nothing",
                         (session_id, message_id, at))

    def stop_requested(self, session_id: str, message_id: str) -> bool:
        with self._pool.connection() as conn:
            return fetch_one(conn, "select 1 as hit from assistant_turn_stop "
                                   "where session_id = %s and message_id = %s",
                             (session_id, message_id)) is not None

    def entries(self, session_id: str) -> list[Entry]:
        from evowiki.memory.forget import mask_entries
        with self._pool.connection() as conn:
            owner = fetch_one(conn, "select user_id from assistant_session where session_id = %s",
                              (session_id,))
            rows = fetch_all(conn, f"select {_ENTRY_COLS} from assistant_entry "
                                   "where session_id = %s order by seq", (session_id,))
        entries = [Entry.model_validate(r) for r in rows]
        if owner is None:
            return entries
        h = self._hidden(owner["user_id"])
        return [] if h.session(session_id) else mask_entries(entries, h)

    def find_proposal(self, proposal_id: str) -> tuple[SessionInfo, Entry]:
        with self._pool.connection() as conn:
            r = fetch_one(conn, f"select {_ENTRY_COLS} from assistant_entry "
                                "where type = 'proposal' and payload ->> 'proposal_id' = %s",
                          (proposal_id,))
        if r is None:
            raise LookupError(f"unknown proposal {proposal_id}")
        entry = Entry.model_validate(r)
        return self.info(entry.session_id), entry

    def find_message(self, session_id: str, message_id: str) -> tuple[SessionInfo, Entry] | None:
        with self._pool.connection() as conn:
            r = fetch_one(conn, f"select {_ENTRY_COLS} from assistant_entry "
                                "where session_id = %s and type in ('user', 'assistant') "
                                "and payload ->> 'message_id' = %s order by seq limit 1",
                          (session_id, message_id))
        if r is None:
            return None
        from evowiki.memory.forget import mask_entries
        try:
            info = self.info(session_id)
        except LookupError:           # being forgotten
            return None
        return info, mask_entries([Entry.model_validate(r)], self._hidden(info.user_id))[0]

    def user_sessions(self, user_id: str) -> list[SessionInfo]:
        with self._pool.connection() as conn:
            rows = fetch_all(
                conn,
                "select s.* from assistant_session s where s.user_id = %s and exists "
                "(select 1 from assistant_entry e where e.session_id = s.session_id "
                "and e.type = 'user') order by s.created_at, s.session_id", (user_id,))
        h = self._hidden(user_id)
        return [SessionInfo.model_validate(r) for r in rows if not h.session(r["session_id"])]

    def recent_sessions(self, user_id: str, *, limit: int, page_id: str | None = None,
                        exclude: str | None = None) -> list[SessionInfo]:
        with self._pool.connection() as conn:
            rows = fetch_all(
                conn,
                "select * from assistant_session where user_id = %s "
                "and (%s::text is null or page_id = %s) "
                "and (%s::text is null or session_id <> %s) "
                "order by created_at desc, session_id desc limit %s",
                (user_id, page_id, page_id, exclude, exclude, limit))
        h = self._hidden(user_id)
        return [SessionInfo.model_validate(r) for r in rows if not h.session(r["session_id"])]

    def user_selections(self, user_id: str,
                        after: str | None = None) -> list[tuple[str, SessionInfo, Entry]]:
        """Ordered by (ingested_at, entry_id). `ingested_at` is clock_timestamp() taken inside
        `append`'s one-statement transaction, which commits right after it, so this order is
        the commit order (up to that sub-millisecond insert-to-commit gap). Position =
        "{ingested_at as epoch microseconds}-{entry_id as utf-8 hex}"; "0" = the start."""
        key = _parse_position(after)
        with self._pool.connection() as conn:
            rows = fetch_all(
                conn,
                "select s.session_id as s_session_id, s.user_id, s.page_id, s.note_concept_id, "
                "s.created_at, e.session_id, e.seq, e.entry_id, e.type, e.payload, "
                "e.occurred_at as at, e.ingested_at from assistant_entry e "
                "join assistant_session s on s.session_id = e.session_id "
                "where s.user_id = %s and e.type = 'user' "
                "and (jsonb_typeof(e.payload -> 'selection') = 'object' "
                "or e.payload ->> 'text' = any(%s)) "
                "and (%s::timestamptz is null "
                "or (e.ingested_at, e.entry_id) > (%s::timestamptz, %s::text)) "
                "order by e.ingested_at, e.entry_id",
                (user_id, list(PAGE_TAPS),
                 *((key[0], key[0], key[1]) if key else (None, None, None))))
        h = self._hidden(user_id)
        rows = [r for r in rows if not h.session(r["s_session_id"])]
        return [(_position(r["ingested_at"], r["entry_id"]),
                 SessionInfo(session_id=r["s_session_id"], user_id=r["user_id"],
                             page_id=r["page_id"], note_concept_id=r["note_concept_id"],
                             created_at=r["created_at"]),
                 Entry(session_id=r["session_id"], seq=r["seq"], entry_id=r["entry_id"],
                       type=r["type"], payload=r["payload"], at=r["at"]))
                for r in rows]
