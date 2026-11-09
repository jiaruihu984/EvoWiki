"""Memory tables (migration 0081): `memory_episode` (one answered turn), `memory_item` (what
consolidation took from a session) and `memory_suppression` (a forgotten item's ids, no text).

Every read starts from `user_id`. `PgMemoryStore` reads also leave out what an open forget
request hides (`memory.forget.hidden_of`: the session being deleted and its taint closure), and
indexes nothing of it. Status changes of an item are appended to its
`status_history`; `status` is only the latest one, and `items(as_of=T)` rebuilds each item as it
stood at T. `InMemoryMemoryStore` has the same semantics as `PgMemoryStore`."""

import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import AwareDatetime, BaseModel, ConfigDict

from evowiki.memory.contracts import EvidenceRef, ItemKind, ItemStatus, Provenance, ScopeType
from evowiki.retrieval.embed import Embedder
from evowiki.store.db import fetch_all, fetch_one

if TYPE_CHECKING:
    from evowiki.memory.forget import Hidden

Tag = tuple[str, str, int]          # (embedder_id, model_version, dim)
_NO_EXCLUDE: Mapping[str, int] = MappingProxyType({})


def tag(e: Embedder) -> Tag:
    return (e.id, e.model_version, e.dim)


def episode_key(session_id: str, message_id: str) -> str:
    return f"{session_id}/{message_id}"


class Episode(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    session_id: str
    message_id: str                  # the user message of the turn
    answer_message_id: str
    trace_run_id: str                # assistant:{session}:{turn}
    turn_index: int                  # index into SessionState.turns
    page_id: str | None
    concept_ids: tuple[str, ...] = ()
    at: AwareDatetime
    user_text: str
    answer_text: str

    @property
    def key(self) -> str:
        return episode_key(self.session_id, self.message_id)


class Item(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    user_id: str
    kind: ItemKind
    content: str
    provenance: Provenance
    evidence: tuple[EvidenceRef, ...]
    scope_type: ScopeType
    scope_id: str | None = None
    page_id: str | None = None
    concept_ids: tuple[str, ...] = ()
    session_id: str                  # the session it was consolidated from
    valid_from: AwareDatetime
    created_at: AwareDatetime
    status: ItemStatus | None = None     # None for preference / about_me
    superseded_by: str | None = None
    status_history: tuple[dict[str, Any], ...] = ()   # [{status, at, by, superseded_by}]
    spec_item_id: str | None = None
    profile_ref: dict[str, Any] | None = None
    pending_action_id: str | None = None


@dataclass(frozen=True)
class StatusChange:
    item_id: str
    status: ItemStatus
    by: str
    superseded_by: str | None = None


@dataclass(frozen=True)
class Suppression:
    item_id: str
    kind: str
    evidence_message_ids: frozenset[str]


def suppression_of(it: "Item") -> Suppression:
    return Suppression(item_id=it.item_id, kind=it.kind, evidence_message_ids=frozenset(
        e.message_id for e in it.evidence if e.message_id is not None))


def _history_entry(status: str, at: datetime, by: str, superseded_by: str | None) -> dict:
    return {"status": status, "at": at.isoformat(), "by": by, "superseded_by": superseded_by}


def created(item: Item) -> Item:
    """`item` with its creation recorded as the first status_history entry (decision /
    open_question), so `as_of` before any later change reads the creation status."""
    if item.status is None or item.status_history:
        return item
    return item.model_copy(update={"status_history": (
        _history_entry(item.status, item.created_at, "create", item.superseded_by),)})


def as_of_view(item: Item, as_of: datetime | None) -> Item | None:
    """The item as it stood at `as_of` (None: as it is now); None when it did not exist yet."""
    if as_of is None:
        return item
    if item.created_at > as_of:
        return None
    if item.status is None:
        return item
    past = [h for h in item.status_history if datetime.fromisoformat(h["at"]) <= as_of]
    if not past:
        return item.model_copy(update={"status": "active", "superseded_by": None,
                                       "status_history": ()})
    last = past[-1]
    return item.model_copy(update={"status": last["status"],
                                   "superseded_by": last.get("superseded_by"),
                                   "status_history": tuple(past)})


def _filter_items(rows: Iterable[Item], *, kinds, statuses, as_of) -> list[Item]:
    out = []
    for it in rows:
        if kinds is not None and it.kind not in kinds:
            continue
        view = as_of_view(it, as_of)
        if view is None or (statuses is not None and view.status not in statuses):
            continue
        out.append(view)
    return sorted(out, key=lambda i: (i.created_at, i.item_id))


def _keep_episode(e: Episode, *, since, until, as_of, exclude_sessions_from) -> bool:
    if since is not None and e.at < since:
        return False
    if until is not None and e.at >= until:
        return False
    if as_of is not None and e.at > as_of:
        return False
    first = exclude_sessions_from.get(e.session_id)
    return first is None or e.turn_index < first


class MemoryStore(Protocol):
    def upsert_episode(self, ep: Episode) -> None:
        """Idempotent on (session_id, message_id); a re-index keeps the stored vector."""
        ...

    def set_episode_vector(self, session_id: str, message_id: str, vec: Sequence[float],
                           embedder: Embedder) -> None: ...

    def clear_episode_vector(self, session_id: str, message_id: str) -> None:
        """Drop the episode's vector (its text changed and could not be embedded again):
        `backfill_vectors` fills it in."""
        ...

    def episodes(self, user_id: str, *, since: datetime | None = None,
                 until: datetime | None = None, as_of: datetime | None = None,
                 exclude_sessions_from: Mapping[str, int] = _NO_EXCLUDE, include_hidden: bool = False) -> list[Episode]:
        """`exclude_sessions_from` = {session_id: first turn index kept verbatim in the
        context}: that session's turns from there on are left out, its folded turns stay."""
        ...

    def episode_vectors(self, user_id: str, embedder_tag: Tag) -> dict[str, list[float]]:
        """`session_id/message_id` -> vector, only vectors of this same tag."""
        ...

    def episodes_without_vector(self, user_id: str, embedder_tag: Tag,
                                limit: int) -> list[Episode]: ...

    def put_item(self, item: Item) -> None: ...

    def get_item(self, user_id: str, item_id: str) -> Item | None: ...

    def items(self, user_id: str, *, kinds: Sequence[str] | None = None,
              statuses: Sequence[str] | None = None,
              as_of: datetime | None = None, include_hidden: bool = False) -> list[Item]: ...

    def set_item_status(self, item_id: str, status: ItemStatus, *, at: datetime, by: str,
                        superseded_by: str | None = None) -> None: ...

    def set_item_vector(self, item_id: str, vec: Sequence[float], embedder: Embedder) -> None: ...

    def item_vectors(self, user_id: str, embedder_tag: Tag) -> dict[str, list[float]]: ...

    def delete_item(self, user_id: str, item_id: str) -> bool: ...

    def forget_item(self, user_id: str, item_id: str, *, at: datetime) -> bool:
        """Delete this user's item and record its suppression (ids, kind, scope; no text) in
        one transaction, so consolidating the same turns again does not bring it back. False
        when there is no such item of this user."""
        ...

    def suppressions(self, user_id: str) -> list[Suppression]: ...

    def forgotten_words(self, user_id: str) -> list[str]: ...

    def hidden(self, user_id: str) -> "Hidden":
        """What this user's open forget requests hide (nothing in memory)."""
        ...

    def unless_forgotten(self, user_id: str, session_id: str, *, evidence: Sequence[EvidenceRef] = (), kind: str | None = None) -> AbstractContextManager[bool]:
        """Held around a write about the session outside its consolidation commit (a preference
        landing): True when it is not being forgotten; a forget request of it waits until the
        block ends, so that write is either before the request or never."""
        ...

    def commit_consolidation(self, sessions: Any, *, session_id: str, entry_id: str,
                             payload: dict[str, Any], items: Sequence[Item],
                             status_changes: Sequence[StatusChange], at: datetime,
                             read_watermark: int | None = None) -> bool:
        """One transaction under the session's consolidation lock. A consolidation (payload
        with through_message_id): re-read the watermark (`watermark_seq`); when it is not the
        `read_watermark` the run started from (another run committed meanwhile, whatever range
        it covered) or already at / past through_message_id, write nothing and return False. A
        retry's landings (payload with "landed" / "refused"): when any of its keys is already
        settled in the session, write nothing and return False. Else insert `items`,
        apply `status_changes` (appended to status_history) and append the `consolidation`
        entry, all or nothing. `sessions` is the session store of the in-memory pair (Postgres
        writes the entry itself)."""
        ...


def settled_keys(entries: Sequence[Any]) -> set[str]:
    """Pending-preference keys a retry already landed or refused."""
    out: set[str] = set()
    for e in entries:
        if e.type == "consolidation":
            out.update(e.payload.get("landed") or ())
            out.update(k for k, _ in e.payload.get("refused") or ())
    return out


def _payload_keys(payload: dict[str, Any]) -> set[str]:
    return {*(payload.get("landed") or ()), *(k for k, _ in payload.get("refused") or ())}


def watermark_seq(entries: Sequence[Any]) -> int:
    """Seq of the user entry the latest consolidation covered up to (0: none)."""
    seq_of = {e.payload.get("message_id"): e.seq for e in entries if e.type == "user"}
    done = 0
    for e in entries:
        if e.type == "consolidation" and e.payload.get("through_message_id"):
            done = max(done, seq_of.get(e.payload["through_message_id"], 0))
    return done


# ---- in memory ----

class InMemoryMemoryStore:
    def __init__(self) -> None:
        self._episodes: dict[str, Episode] = {}
        self._ep_vec: dict[str, tuple[Tag, list[float]]] = {}
        self._items: dict[str, Item] = {}
        self._item_vec: dict[str, tuple[Tag, list[float]]] = {}
        self._suppressions: dict[str, tuple[str, Suppression]] = {}   # item_id -> (user, s)
        self.lock = threading.Lock()     # stands in for the per-session advisory lock

    def upsert_episode(self, ep: Episode) -> None:
        self._episodes[ep.key] = ep

    def set_episode_vector(self, session_id, message_id, vec, embedder) -> None:
        key = episode_key(session_id, message_id)
        if key not in self._episodes:
            raise LookupError(f"no episode {key}")
        self._ep_vec[key] = (tag(embedder), [float(x) for x in vec])

    def clear_episode_vector(self, session_id, message_id) -> None:
        self._ep_vec.pop(episode_key(session_id, message_id), None)

    def episodes(self, user_id, *, since=None, until=None, as_of=None,
                 exclude_sessions_from=_NO_EXCLUDE, include_hidden=False) -> list[Episode]:
        out = [e for e in self._episodes.values() if e.user_id == user_id
               and _keep_episode(e, since=since, until=until, as_of=as_of,
                                 exclude_sessions_from=exclude_sessions_from)]
        return sorted(out, key=lambda e: (e.at, e.key))

    def episode_vectors(self, user_id, embedder_tag) -> dict[str, list[float]]:
        return {k: v for k, (t, v) in self._ep_vec.items()
                if t == tuple(embedder_tag) and self._episodes[k].user_id == user_id}

    def episodes_without_vector(self, user_id, embedder_tag, limit) -> list[Episode]:
        have = self.episode_vectors(user_id, embedder_tag)
        return [e for e in self.episodes(user_id) if e.key not in have][:limit]

    def put_item(self, item: Item) -> None:
        if item.item_id in self._items:
            raise ValueError(f"memory item {item.item_id} already exists")
        self._items[item.item_id] = created(item)

    def get_item(self, user_id, item_id) -> Item | None:
        it = self._items.get(item_id)
        return it if it is not None and it.user_id == user_id else None

    def items(self, user_id, *, kinds=None, statuses=None, as_of=None, include_hidden=False) -> list[Item]:
        return _filter_items((i for i in self._items.values() if i.user_id == user_id),
                             kinds=kinds, statuses=statuses, as_of=as_of)

    def set_item_status(self, item_id, status, *, at, by, superseded_by=None) -> None:
        it = self._items.get(item_id)
        if it is None:
            raise LookupError(f"no memory item {item_id}")
        if it.status is None:
            raise ValueError(f"{it.kind} items carry no status")
        self._items[item_id] = it.model_copy(update={
            "status": status, "superseded_by": superseded_by,
            "status_history": (*it.status_history,
                               _history_entry(status, at, by, superseded_by))})

    def set_item_vector(self, item_id, vec, embedder) -> None:
        if item_id not in self._items:
            raise LookupError(f"no memory item {item_id}")
        self._item_vec[item_id] = (tag(embedder), [float(x) for x in vec])

    def item_vectors(self, user_id, embedder_tag) -> dict[str, list[float]]:
        return {k: v for k, (t, v) in self._item_vec.items()
                if t == tuple(embedder_tag) and self._items[k].user_id == user_id}

    def delete_item(self, user_id, item_id) -> bool:
        if self.get_item(user_id, item_id) is None:
            return False
        del self._items[item_id]
        self._item_vec.pop(item_id, None)
        for k, other in list(self._items.items()):
            if other.superseded_by == item_id:
                self._items[k] = other.model_copy(update={"superseded_by": None})
        return True

    def forget_item(self, user_id, item_id, *, at) -> bool:
        it = self.get_item(user_id, item_id)
        if it is None:
            return False
        self.delete_item(user_id, item_id)
        self._suppressions[item_id] = (user_id, suppression_of(it))
        return True

    def suppressions(self, user_id) -> list[Suppression]:
        return [s for u, s in self._suppressions.values() if u == user_id]

    def forgotten_words(self, user_id) -> list[str]:
        return []                     # they live on the ledger's rows (`store.bot.forgotten_words`)

    def hidden(self, user_id) -> "Hidden":
        from evowiki.memory.forget import NO_HIDDEN
        return NO_HIDDEN

    @contextmanager
    def unless_forgotten(self, user_id, session_id, *, evidence=(), kind=None) -> Iterator[bool]:
        h = self.hidden(user_id)
        yield (not h.paused and not h.session(session_id)
                   and not any(h.episode(e.session_id or session_id, e.message_id)
                               for e in evidence if e.message_id)
                   and not any(s.kind in (kind, "episode") and
                               s.evidence_message_ids.intersection(
                                   e.message_id for e in evidence if e.message_id)
                               for s in self.suppressions(user_id)))

    def commit_consolidation(self, sessions, *, session_id, entry_id, payload, items,
                             status_changes, at, read_watermark=None) -> bool:
        with self.lock:
            entries = sessions.entries(session_id)
            if "through_message_id" in payload:
                through = next((e.seq for e in entries if e.type == "user" and
                                e.payload.get("message_id") == payload["through_message_id"]),
                               None)
                if through is None:
                    raise LookupError(f"no user message {payload['through_message_id']}")
                done = watermark_seq(entries)
                if done >= through or (read_watermark is not None and done != read_watermark):
                    return False
            elif _payload_keys(payload) & settled_keys(entries):
                return False
            before = (dict(self._items), dict(self._item_vec))
            try:
                for it in items:
                    self.put_item(it)
                for c in status_changes:
                    self.set_item_status(c.item_id, c.status, at=at, by=c.by,
                                         superseded_by=c.superseded_by)
                sessions.append(session_id, entry_id, "consolidation", payload, at)
            except BaseException:
                self._items, self._item_vec = before
                raise
            return True


# ---- Postgres ----

_EP_COLS = ("user_id, session_id, message_id, answer_message_id, trace_run_id, turn_index, "
            "page_id, concept_ids, at, user_text, answer_text")
_ITEM_COLS = ("item_id, user_id, kind, content, provenance, evidence, scope_type, scope_id, "
              "page_id, concept_ids, session_id, valid_from, created_at, status, superseded_by, "
              "status_history, spec_item_id, profile_ref, pending_action_id")


def _vec(v: Sequence[float]) -> np.ndarray:
    return np.asarray(v, dtype=np.float32)


def _episode(r: dict) -> Episode:
    return Episode.model_validate({**r, "concept_ids": tuple(r["concept_ids"] or ())})


def _item(r: dict) -> Item:
    return Item.model_validate({**r, "concept_ids": tuple(r["concept_ids"] or ()),
                                "status_history": tuple(r["status_history"] or ())})


def insert_item_sql(conn, item: Item) -> None:
    """One `memory_item` row (its creation entry included); shared by `put_item` and the
    consolidation commit."""
    item = created(item)
    conn.execute(
        f"insert into memory_item ({_ITEM_COLS}) values "
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (item.item_id, item.user_id, item.kind, item.content, item.provenance,
         Jsonb([e.model_dump(mode="json", exclude_none=True) for e in item.evidence]),
         item.scope_type, item.scope_id, item.page_id, list(item.concept_ids), item.session_id,
         item.valid_from, item.created_at, item.status, item.superseded_by,
         Jsonb(list(item.status_history)), item.spec_item_id,
         Jsonb(item.profile_ref) if item.profile_ref is not None else None,
         item.pending_action_id))


def set_status_sql(conn, item_id: str, status: str, *, at: datetime, by: str,
                   superseded_by: str | None) -> None:
    cur = conn.execute(
        "update memory_item set status = %s, superseded_by = %s, "
        "status_history = status_history || %s where item_id = %s and status is not null",
        (status, superseded_by, Jsonb([_history_entry(status, at, by, superseded_by)]), item_id))
    if cur.rowcount != 1:
        raise LookupError(f"no memory item {item_id} with a status")


class PgMemoryStore:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def hidden(self, user_id) -> "Hidden":
        from evowiki.memory.forget import hidden_of
        with self._pool.connection() as conn:
            return hidden_of(conn, user_id)

    @contextmanager
    def unless_forgotten(self, user_id, session_id, *, evidence=(), kind=None) -> Iterator[bool]:
        from evowiki.assistant.pg_store import lock_consolidation
        from evowiki.memory.forget import hidden_of
        with self._pool.connection() as conn, conn.transaction():
            lock_consolidation(conn, session_id)
            conn.execute("select pg_advisory_xact_lock(hashtext('memory:user:' || %s))", (user_id,))
            h = hidden_of(conn, user_id)
            yield (not h.paused and not h.session(session_id)
                   and not any(h.episode(e.session_id or session_id, e.message_id)
                               for e in evidence if e.message_id)
                   and not any(s.kind in (kind, "episode") and
                               s.evidence_message_ids.intersection(
                                   e.message_id for e in evidence if e.message_id)
                               for s in self.suppressions(user_id)))

    def upsert_episode(self, ep: Episode) -> None:
        if self.hidden(ep.user_id).episode(ep.session_id, ep.message_id):
            return                    # its session is being forgotten: nothing to index
        with self._pool.connection() as conn:
            conn.execute(
                f"insert into memory_episode ({_EP_COLS}) values "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (session_id, message_id) do update set "
                "answer_message_id = excluded.answer_message_id, "
                "trace_run_id = excluded.trace_run_id, turn_index = excluded.turn_index, "
                "page_id = excluded.page_id, concept_ids = excluded.concept_ids, "
                "at = excluded.at, user_text = excluded.user_text, "
                "answer_text = excluded.answer_text where memory_episode.user_id = excluded.user_id",
                (ep.user_id, ep.session_id, ep.message_id, ep.answer_message_id, ep.trace_run_id,
                 ep.turn_index, ep.page_id, list(ep.concept_ids), ep.at, ep.user_text,
                 ep.answer_text))

    def set_episode_vector(self, session_id, message_id, vec, embedder) -> None:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "update memory_episode set embedder_id = %s, model_version = %s, dim = %s, "
                "embedding = %s where session_id = %s and message_id = %s",
                (embedder.id, embedder.model_version, embedder.dim, _vec(vec), session_id,
                 message_id))
            if cur.rowcount != 1:
                raise LookupError(f"no episode {episode_key(session_id, message_id)}")

    def clear_episode_vector(self, session_id, message_id) -> None:
        with self._pool.connection() as conn:
            conn.execute("update memory_episode set embedder_id = null, model_version = null, "
                         "dim = null, embedding = null where session_id = %s "
                         "and message_id = %s", (session_id, message_id))

    def episodes(self, user_id, *, since=None, until=None, as_of=None,
                 exclude_sessions_from=_NO_EXCLUDE, include_hidden=False) -> list[Episode]:
        with self._pool.connection() as conn:
            rows = fetch_all(
                conn,
                f"select {_EP_COLS} from memory_episode where user_id = %(u)s "
                "and (%(since)s::timestamptz is null or at >= %(since)s) "
                "and (%(until)s::timestamptz is null or at < %(until)s) "
                "and (%(asof)s::timestamptz is null or at <= %(asof)s) "
                "order by at, session_id, message_id",
                {"u": user_id, "since": since, "until": until, "asof": as_of})
        h = self.hidden(user_id)
        return [e for e in map(_episode, rows)
                if _keep_episode(e, since=None, until=None, as_of=None,
                                 exclude_sessions_from=exclude_sessions_from)
                and (include_hidden or not h.episode(e.session_id, e.message_id))]

    def episode_vectors(self, user_id, embedder_tag) -> dict[str, list[float]]:
        eid, ver, dim = embedder_tag
        with self._pool.connection() as conn:
            rows = fetch_all(
                conn, "select session_id, message_id, embedding from memory_episode "
                      "where user_id = %s and embedding is not null and embedder_id = %s "
                      "and model_version = %s and dim = %s", (user_id, eid, ver, dim))
        h = self.hidden(user_id)
        return {episode_key(r["session_id"], r["message_id"]):
                [float(x) for x in r["embedding"].to_list()] for r in rows
                if not h.episode(r["session_id"], r["message_id"])}

    def episodes_without_vector(self, user_id, embedder_tag, limit) -> list[Episode]:
        eid, ver, dim = embedder_tag
        with self._pool.connection() as conn:
            rows = fetch_all(
                conn, f"select {_EP_COLS} from memory_episode where user_id = %s "
                      "and (embedding is null or embedder_id <> %s or model_version <> %s "
                      "or dim <> %s) order by at, session_id, message_id limit %s",
                (user_id, eid, ver, dim, limit))
        h = self.hidden(user_id)
        return [e for e in map(_episode, rows) if not h.episode(e.session_id, e.message_id)]

    def put_item(self, item: Item) -> None:
        with self._pool.connection() as conn:
            insert_item_sql(conn, item)

    def get_item(self, user_id, item_id) -> Item | None:
        with self._pool.connection() as conn:
            r = fetch_one(conn, f"select {_ITEM_COLS} from memory_item "
                                "where user_id = %s and item_id = %s", (user_id, item_id))
        if r is None:
            return None
        it = _item(r)
        return None if self.hidden(user_id).item(it) else it

    def items(self, user_id, *, kinds=None, statuses=None, as_of=None, include_hidden=False) -> list[Item]:
        with self._pool.connection() as conn:
            rows = fetch_all(
                conn, f"select {_ITEM_COLS} from memory_item where user_id = %(u)s "
                      "and (%(kinds)s::text[] is null or kind = any(%(kinds)s::text[])) "
                      "and (%(asof)s::timestamptz is null or created_at <= %(asof)s)",
                {"u": user_id, "kinds": list(kinds) if kinds is not None else None,
                 "asof": as_of})
        h = self.hidden(user_id)
        return _filter_items((it for it in map(_item, rows) if include_hidden or not h.item(it)), kinds=kinds,
                             statuses=statuses, as_of=as_of)

    def set_item_status(self, item_id, status, *, at, by, superseded_by=None) -> None:
        with self._pool.connection() as conn:
            set_status_sql(conn, item_id, status, at=at, by=by, superseded_by=superseded_by)

    def set_item_vector(self, item_id, vec, embedder) -> None:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "update memory_item set embedder_id = %s, model_version = %s, dim = %s, "
                "embedding = %s where item_id = %s",
                (embedder.id, embedder.model_version, embedder.dim, _vec(vec), item_id))
            if cur.rowcount != 1:
                raise LookupError(f"no memory item {item_id}")

    def item_vectors(self, user_id, embedder_tag) -> dict[str, list[float]]:
        eid, ver, dim = embedder_tag
        with self._pool.connection() as conn:
            rows = fetch_all(
                conn, "select item_id, embedding from memory_item where user_id = %s "
                      "and embedding is not null and embedder_id = %s and model_version = %s "
                      "and dim = %s", (user_id, eid, ver, dim))
        h = self.hidden(user_id)
        visible = {it.item_id for it in self.items(user_id)} if h else None
        return {r["item_id"]: [float(x) for x in r["embedding"].to_list()] for r in rows
                if visible is None or r["item_id"] in visible}

    def delete_item(self, user_id, item_id) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute("delete from memory_item where user_id = %s and item_id = %s",
                               (user_id, item_id))
            return cur.rowcount == 1

    def forget_item(self, user_id, item_id, *, at) -> bool:
        with self._pool.connection() as conn, conn.transaction():
            r = fetch_one(conn, f"delete from memory_item where user_id = %s and item_id = %s "
                                f"returning {_ITEM_COLS}", (user_id, item_id))
            if r is None:
                return False
            it = _item(r)
            conn.execute(
                "insert into memory_suppression (user_id, item_id, kind, evidence_message_ids, "
                "scope_type, scope_id, at) values (%s, %s, %s, %s, %s, %s, %s) "
                "on conflict (item_id) do nothing",
                (user_id, it.item_id, it.kind, sorted(suppression_of(it).evidence_message_ids),
                 it.scope_type, it.scope_id, at))
            return True

    def commit_consolidation(self, sessions, *, session_id, entry_id, payload, items,
                             status_changes, at, read_watermark=None) -> bool:
        from evowiki.assistant.pg_store import insert_entry, lock_consolidation, lock_session
        from evowiki.memory.forget import hidden_of

        with self._pool.connection() as conn, conn.transaction():
            row = fetch_one(conn, "select user_id from assistant_session where session_id=%s",
                            (session_id,))
            if row is None:
                raise LookupError(f"unknown assistant session {session_id}")
            uid = row["user_id"]
            lock_consolidation(conn, session_id)
            conn.execute("select pg_advisory_xact_lock(hashtext('memory:user:' || %s))", (uid,))
            lock_session(conn, session_id)
            h = hidden_of(conn, uid)
            if h.paused or h.session(session_id) or any(h.item(it) for it in items):
                return False       # a forget request came in while the model call ran
            r = None if "through_message_id" not in payload else fetch_one(
                conn,
                "select (select seq from assistant_entry where session_id = %(s)s "
                "and type = 'user' and payload ->> 'message_id' = %(m)s) as through, "
                "(select coalesce(max(u.seq), 0) from assistant_entry c join assistant_entry u "
                "on u.session_id = c.session_id and u.type = 'user' "
                "and u.payload ->> 'message_id' = c.payload ->> 'through_message_id' "
                "where c.session_id = %(s)s and c.type = 'consolidation') as done",
                {"s": session_id, "m": payload["through_message_id"]})
            if r is not None:
                if r["through"] is None:
                    raise LookupError(f"no user message {payload['through_message_id']}")
                if r["done"] >= r["through"] or (read_watermark is not None
                                                 and r["done"] != read_watermark):
                    return False
            else:
                keys = _payload_keys(payload)
                if keys and fetch_one(
                        conn,
                        "select 1 from assistant_entry where session_id = %s "
                        "and type = 'consolidation' and ("
                        "payload -> 'landed' ?| %s::text[] or exists (select 1 from "
                        "jsonb_array_elements(coalesce(payload -> 'refused', '[]')) x "
                        "where x ->> 0 = any(%s::text[])))",
                        (session_id, sorted(keys), sorted(keys))) is not None:
                    return False
            for it in items:
                insert_item_sql(conn, it)
            for c in status_changes:
                set_status_sql(conn, c.item_id, c.status, at=at, by=c.by,
                               superseded_by=c.superseded_by)
            insert_entry(conn, session_id, entry_id, "consolidation", payload, at)
            return True

    def suppressions(self, user_id) -> list[Suppression]:
        with self._pool.connection() as conn:
            rows = fetch_all(conn, "select item_id, kind, evidence_message_ids "
                                   "from memory_suppression where user_id = %s", (user_id,))
        return [Suppression(item_id=r["item_id"], kind=r["kind"],
                            evidence_message_ids=frozenset(r["evidence_message_ids"]))
                for r in rows]

    def forgotten_words(self, user_id) -> list[str]:
        from evowiki.store.bot import forgotten_words
        with self._pool.connection() as conn:
            return forgotten_words(conn, user_id)
