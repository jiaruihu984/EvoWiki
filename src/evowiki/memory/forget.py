"""Deleting an assistant session or one memory item (plan Task 13; memory spec §4.1–§4.3).

State machine of a request (migration 0082's channel; every step audited in `forget_audit`):

  requested  the server checked the user owns the session and called `evowiki_forget_request`;
             in the same transaction the out-of-database ledger (`tombstone.Ledger`) got the row
             and was fsynced, so a ledger that cannot be written leaves no request behind.
  deleting   one transaction: `evowiki_forget_open`, then every step below, then
             `evowiki_forget_close`; it commits as a whole or not at all.
  committed  the ledger gets a `committed` row after the commit.
  failed     the transaction rolled back (nothing deleted); `evowiki_forget_fail` records the
             error; the ledger stays `requested` and `retry_open` / `tombstone.reconcile` run it
             again until it commits: a real request of the learner, a failure does not cancel it.

From the moment a request exists until it commits, its session and its taint closure are hidden
from every read (`hidden_of`: the stores filter on it): search, recall, read_past_turn,
verify_quote, carry_over, the session list and the session itself, indexing and consolidation.

The taint closure (`taint_closure`) is what the session's content reached: a turn of another
session that recalled one of its memory ids, or whose search_memory / read_past_turn call read
one (`memory_ids`, the call's memory_id / session+message args, a result quoting the session),
is tainted; its episode and the items whose evidence cites it join the set, and the search goes
on until the set stops growing (breadth first, each node once, with persistent bounded batches). A
tainted turn keeps the learner's words; its answer becomes PLACEHOLDER, its tool results are
redacted, its trace and model calls are deleted, a compaction that folded it is voided.

The session's own rows go: memory rows, traces and model calls, its non-preference ledger rows,
then S3 redacts its words in the preferences (the rules he ticked are revoked through S3's normal
path, in the same commit), harness RSI forgets it (`harness.forget.forget_session`, with the
closure's user words as quotes, before the entries go), and the entries and the session row
are deleted and `forgotten_session` written. Before closing, the integrity check: every node of
the closure was handled and every untainted entry of the touched sessions is byte for byte as it
was."""

import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from evowiki.assistant.pg_store import lock_consolidation
from evowiki.assistant.session import Entry, SessionInfo, conversation_title, fold
from evowiki.store.db import fetch_all, fetch_one

PLACEHOLDER = "这条回答引用了已删除的会话内容，已移除"
TOOL_PLACEHOLDER = "（引用了已删除的会话内容，已移除）"
CLOSURE_BATCH = 512
WRAPUP_WAIT_S = 30.0
WRAPUP_POLL_S = 0.2
MEMORY_TOOLS = frozenset({"search_memory", "read_past_turn"})
UNAVAILABLE = "删除没完成，请再试一次"
RETRY_DETAIL = "删除没有完成，会自动重试"
KEEP_NOTE = "没有勾选的规则和档案会继续生效，只是不再显示它们来自这段对话的原话。"
ELSEWHERE = "这条在参数或档案里，请在那里删除"
# what a voided compaction loses: every summary part (assistant/summary.py, and older texts)
VOIDED_KEYS = ("version", "summary", "fields", "handoff", "learning", "edits", "dropped")

log = logging.getLogger(__name__)

State = Literal["requested", "deleting", "committed", "failed"]


class ForgetUnavailable(RuntimeError):
    """A dependency of the delete flow is missing: nothing was requested (503)."""


class ForgetIntegrityError(RuntimeError):
    """The pre-commit check found a node not handled or an untainted entry changed."""


class ForgetBusy(RuntimeError):
    """A DBOS workflow of what is being forgotten is still running or queued: deleting it now
    would let it write again. The request fails and is retried."""


# ---------- the closure ----------

@dataclass
class _Turn:
    session_id: str
    message_id: str                       # the user message
    user_text: str
    answer_ids: list[str] = field(default_factory=list)
    refs: set[str] = field(default_factory=set)       # memory ids / session:<id> it read
    entries: list[dict[str, Any]] = field(default_factory=list)   # rows of this turn

    @property
    def key(self) -> str:
        return f"{self.session_id}/{self.message_id}"


def _tool_refs(p: Mapping[str, Any]) -> set[str]:
    out = {str(i) for i in p.get("memory_ids") or ()}
    args = p.get("args") if isinstance(p.get("args"), dict) else {}
    if isinstance(args.get("memory_id"), str):
        out.add(args["memory_id"])
    if isinstance(args.get("session_id"), str) and isinstance(args.get("message_id"), str):
        out.add(f"ep:{args['session_id']}/{args['message_id']}")
    out |= {f"session:{s}" for s in p.get("sources_sessions") or ()}
    return out


def _load_turns(conn, user_id: str, sessions: Sequence[str]) -> tuple[list[_Turn], dict[str, list[dict[str, Any]]]]:
    """Every turn of the user's sessions, and every entry row by session (seq order)."""
    rows = fetch_all(
        conn, "select e.session_id, e.seq, e.entry_id, e.type, e.payload, e.occurred_at as at "
              "from assistant_entry e join assistant_session s on s.session_id = e.session_id "
              "where s.user_id = %s and e.session_id = any(%s) "
              "order by e.session_id, e.seq", (user_id, list(sessions)))
    by_session: dict[str, list[dict[str, Any]]] = {}
    turns: list[_Turn] = []
    cur: dict[str, _Turn] = {}
    by_mid: dict[tuple[str, str], _Turn] = {}
    for r in rows:
        sid, p = r["session_id"], r["payload"]
        by_session.setdefault(sid, []).append(r)
        if r["type"] == "user":
            t = _Turn(sid, p["message_id"], p.get("text") or "")
            turns.append(t)
            cur[sid] = by_mid[(sid, t.message_id)] = t
            t.entries.append(r)
        elif r["type"] == "assistant" and sid in cur:
            t = cur[sid]
            t.answer_ids.append(p["message_id"])
            t.refs |= {str(x["id"]) for x in p.get("recalled") or () if isinstance(x, dict)}
            t.entries.append(r)
        elif r["type"] in ("tool", "proposal", "observed", "turn_error"):
            t = by_mid.get((sid, p.get("message_id")))
            if t is None:
                continue
            if r["type"] == "tool" and p.get("name") in MEMORY_TOOLS:
                t.refs |= _tool_refs(p)
            t.entries.append(r)
    return turns, by_session


@dataclass(frozen=True)
class Taint:
    session_id: str
    turns: frozenset[tuple[str, str]]          # tainted (session, user message) outside it
    episodes: frozenset[str]                   # "<session>/<message>" keys, its own included
    items: frozenset[str]
    paths: Mapping[str, str]                   # node -> the node that tainted it
    quotes: tuple[str, ...]                    # the learner's words of its turns and the tainted
    sessions: frozenset[str] = frozenset()     # other sessions with a tainted turn
    _turns: tuple[_Turn, ...] = ()             # the tainted turns, loaded
    _rows: Mapping[str, Sequence[dict[str, Any]]] = field(default_factory=dict)

    @property
    def messages(self) -> frozenset[tuple[str, str]]:
        """(session, message id) of every user message and answer of the tainted turns."""
        return frozenset((t.session_id, m) for t in self._turns
                         for m in (t.message_id, *t.answer_ids))


def _invalidate_derived(conn, user_id: str, nodes: Sequence[str], *, purge: bool = False) -> None:
    from evowiki.store.dreaming import invalidate_sources

    invalidate_sources(conn, user_id, nodes)
    if purge:
        signals = [n.removeprefix('signal:') for n in nodes if n.startswith('signal:')]
        if signals:
            removed = conn.execute("delete from wiki_event where user_id=%s and type='signal_recorded' "
                                   "and payload->'signal'->>'signal_id'=any(%s)", (user_id, signals)).rowcount
            if removed:
                conn.execute('delete from wiki_snapshot where user_id=%s', (user_id,))
        runs = [n.removeprefix("run:") for n in nodes if n.startswith("run:")]
        attempts = [n.removeprefix("attempt:") for n in nodes if n.startswith("attempt:")]
        if runs:
            conn.execute("select pg_advisory_xact_lock(hashtext('trace:prune'))")
            delete_runs(conn, user_id, runs)
        if attempts:
            conn.execute("delete from llm_attempt where id=any(%s)", (attempts,))
        rounds = [n.removeprefix("round:") for n in nodes if n.startswith("round:")]
        if rounds:
            from evowiki.harness.forget import forget_rounds

            rid = fetch_one(conn, "select current_setting('evowiki.forget') as rid")["rid"]
            for table, count in forget_rounds(conn, user_id, rounds).items():
                _note(conn, rid, "derived round." + table, count)
        sessions = [n.removeprefix("session:") for n in nodes if n.startswith("session:")]
        conn.execute("delete from dream_pending where user_id=%s and session_id=any(%s)",
                     (user_id, sessions))
        calls = [n.removeprefix("call:") for n in nodes if n.startswith("call:")]
        if calls:
            conn.execute("select pg_advisory_xact_lock(hashtext('trace:prune'))")
            runs = [r["run_id"] for r in fetch_all(conn,
                "select distinct s.run_id from agent_step s join agent_run r using(run_id) "
                "where r.user_id=%s and s.call_id=any(%s)", (user_id, calls))]
            delete_runs(conn, user_id, runs, calls=calls)
        conn.execute("delete from dream_source where user_id = %s and "
                     "('dream:' || kind || ':' || key || ':' || version) = any(%s)",
                     (user_id, list(nodes)))
        conn.execute("delete from memory_dependency where user_id = %s "
                     "and (source = any(%s) or target = any(%s) or owner = any(%s))",
                     (user_id, list(nodes), list(nodes), list(nodes)))


def sources_suppressed(conn, user_id: str, nodes: Sequence[str]) -> bool:
    return fetch_one(conn, "select 1 from memory_suppression_closure where user_id = %s "
                           "and node = any(%s) limit 1", (user_id, list(nodes))) is not None


def suppress_sources(conn, user_id: str, owner: str, roots: Sequence[str]) -> list[str]:
    """Store complete reversible provenance suppression, retaining all original text."""
    conn.execute("with recursive reached(node) as (select unnest(%s::text[]) union "
                 "select d.target from memory_dependency d join reached r on d.source=r.node "
                 "where d.user_id=%s) insert into memory_suppression_closure(user_id,owner,node) "
                 "select %s,%s,node from reached on conflict do nothing",
                 (list(roots), user_id, user_id, owner))
    nodes = [r["node"] for r in fetch_all(
        conn, "select node from memory_suppression_closure where user_id=%s and owner=%s",
        (user_id, owner))]
    _invalidate_derived(conn, user_id, nodes)
    return nodes


@contextmanager
def generation_guard(pool: ConnectionPool, user_id: str) -> Iterator[None]:
    """Keep an in-flight generation discoverable without an open network-long transaction.

    A session advisory lock survives the short acquisition transaction, and connection loss
    releases it automatically. Forget requests remain immediate; final deletion retries while
    any generation still has content or provenance to persist.
    """
    key = "memory:generation:" + user_id
    with pool.connection() as conn:
        conn.execute("select pg_advisory_lock_shared(hashtext(%s))", (key,))
        conn.commit()
        try:
            if memory_reads_paused(conn, user_id):
                raise ForgetBusy("memory generation paused while forgetting")
            conn.commit()
            yield
        finally:
            conn.rollback()
            conn.execute("select pg_advisory_unlock_shared(hashtext(%s))", (key,))
            conn.commit()


def memory_reads_paused(conn, user_id: str) -> bool:
    """Fail closed while a durable closure is incomplete or its delete is retrying."""
    return fetch_one(conn, "select 1 from forget_request where user_id = %s "
                           "and state <> 'committed' limit 1", (user_id,)) is not None


def _expand_nodes(conn, user_id: str, nodes: Sequence[str]) -> dict[str, str]:
    return {r["target"]: r["source"] for r in fetch_all(
        conn, "select distinct on (target) target, source from memory_dependency "
              "where user_id = %s and source = any(%s) order by target, source",
        (user_id, list(nodes)))}


def closure_batch(conn, request_id: str, *, batch_size: int = CLOSURE_BATCH) -> bool:
    """Commit this transaction to checkpoint one bounded BFS step. True means complete.

    A request advisory lock serializes workers. Expansion and the expanded markers commit together;
    a crash replays at most one batch. The request's write fence keeps the graph stable.
    """
    conn.execute("select pg_advisory_xact_lock(hashtext('forget:closure:' || %s))", (request_id,))
    req = fetch_one(conn, "select * from forget_request where request_id = %s",
                    (request_id,))
    if req is None:
        raise LookupError(request_id)
    if req["state"] == "committed":
        return True
    root = ("session:" + req["session_id"] if req["session_id"] is not None
            else "item:" + req["item_id"])
    conn.execute("insert into forget_frontier(request_id,node,cause) values (%s,%s,'request') "
                 "on conflict do nothing", (request_id, root))
    rows = fetch_all(conn, "select node from forget_frontier where request_id = %s "
                           "and not expanded order by node limit %s", (request_id, batch_size))
    nodes = [r["node"] for r in rows]
    if not nodes:
        return True
    found = _expand_nodes(conn, req["user_id"], nodes)
    with conn.cursor() as cur:
        cur.executemany("insert into forget_frontier(request_id,node,cause) values (%s,%s,%s) "
                        "on conflict do nothing",
                        [(request_id, node, cause) for node, cause in found.items()])
    conn.execute("update forget_frontier set expanded = true where request_id = %s "
                 "and node = any(%s)", (request_id, nodes))
    return fetch_one(conn, "select 1 from forget_frontier where request_id = %s "
                           "and not expanded limit 1", (request_id,)) is None


def _taint_from_paths(conn, user_id: str, session_id: str, paths: Mapping[str, str]) -> Taint:
    affected = {session_id} | {n.removeprefix("turn:").partition("/")[0]
                               for n in paths if n.startswith("turn:")}
    turns, by_session = _load_turns(conn, user_id, sorted(affected))
    tainted = [t for t in turns if f"turn:{t.key}" in paths]
    outside = [t for t in tainted if t.session_id != session_id]
    sessions = frozenset(t.session_id for t in outside)
    return Taint(
        session_id=session_id,
        turns=frozenset((t.session_id, t.message_id) for t in outside),
        episodes=frozenset(n.removeprefix("ep:") for n in paths if n.startswith("ep:")),
        items=frozenset(n.removeprefix("item:") for n in paths if n.startswith("item:")),
        paths=dict(paths), quotes=tuple(t.user_text for t in tainted if t.user_text.strip()),
        sessions=sessions, _turns=tuple(outside),
        _rows={sid: by_session.get(sid, []) for sid in sessions})


def taint_closure(conn, user_id: str, session_id: str) -> Taint:
    """Indexed uncapped traversal for read-only preview; execution uses durable batches."""
    paths = {f"session:{session_id}": "request"}
    pending = list(paths)
    while pending:
        found = _expand_nodes(conn, user_id, pending[:CLOSURE_BATCH])
        pending = pending[CLOSURE_BATCH:]
        for node, cause in found.items():
            if node not in paths:
                paths[node] = cause
                pending.append(node)
    return _taint_from_paths(conn, user_id, session_id, paths)


def _persisted_taint(conn, user_id: str, session_id: str, request_id: str) -> Taint:
    locked = fetch_one(conn, "select pg_try_advisory_xact_lock(hashtext(%s)) as locked",
                       ("memory:generation:" + user_id,))["locked"]
    if not locked:
        raise ForgetBusy("a memory generation is still persisting its call provenance")
    conn.execute("select pg_advisory_xact_lock(hashtext('memory:user:' || %s))", (user_id,))
    # Late in-flight call provenance is allowed, and the graph trigger persists its new frontier.
    # Under the final publication lock no more edges can arrive before the deletion commits.
    while not closure_batch(conn, request_id):
        pass
    rows = fetch_all(conn, "select node,cause from forget_frontier where request_id = %s",
                     (request_id,))
    return _taint_from_paths(conn, user_id, session_id,
                             {r["node"]: r["cause"] for r in rows})


# ---------- what is hidden while a request is open ----------

@dataclass(frozen=True)
class Hidden:
    sessions: frozenset[str] = frozenset()
    messages: frozenset[tuple[str, str]] = frozenset()   # tainted turns: user msg + answers
    items: frozenset[str] = frozenset()
    paused: bool = False

    def __bool__(self) -> bool:
        return bool(self.paused or self.sessions or self.messages or self.items)

    def session(self, session_id: str) -> bool:
        return session_id in self.sessions

    def episode(self, session_id: str, message_id: str) -> bool:
        return self.paused or session_id in self.sessions or (session_id, message_id) in self.messages

    def item(self, item: Any) -> bool:
        if self.paused or item.item_id in self.items or item.session_id in self.sessions:
            return True
        return any(e.session_id is not None and (e.session_id in self.sessions or (
            e.message_id is not None and (e.session_id, e.message_id) in self.messages))
            for e in item.evidence)

    def memory_id(self, memory_id: str) -> bool:
        """`ep:<session>/<message>` or `item:<id>` (an item's session is not known here)."""
        if self.paused:
            return True
        if memory_id.startswith("item:"):
            return memory_id.removeprefix("item:") in self.items
        sid, _, mid = memory_id.removeprefix("ep:").partition("/")
        return self.episode(sid, mid)


NO_HIDDEN = Hidden()


def hidden_of(conn, user_id: str) -> Hidden:
    """Hide uncertain derived reads immediately, without computing a closure on each read.

    Other original conversations remain visible; model outputs are temporarily masked until
    the indexed durable traversal and atomic deletion establish which ones are safe.
    """
    rows = fetch_all(conn, "select session_id, item_id from forget_request where user_id = %s "
                           "and state <> 'committed'", (user_id,))
    suppressed = [r["node"] for r in fetch_all(
        conn, "select distinct node from memory_suppression_closure where user_id=%s", (user_id,))]
    if not rows and not suppressed:
        return NO_HIDDEN
    turn_keys = [n.removeprefix("turn:") for n in suppressed if n.startswith("turn:")]
    messages = {tuple(n.removeprefix("message:").split("/", 1)) for n in suppressed
                if n.startswith("message:")}
    messages.update(tuple(k.split("/", 1)) for k in turn_keys)
    messages.update(tuple(n.removeprefix("ep:").split("/", 1)) for n in suppressed
                    if n.startswith("ep:"))
    return Hidden(sessions=frozenset(r["session_id"] for r in rows if r["session_id"]),
                  messages=frozenset(messages),
                  items=frozenset({r["item_id"] for r in rows if r["item_id"]}
                                  | {n.removeprefix("item:") for n in suppressed
                                     if n.startswith("item:")}), paused=bool(rows))


def mask_entries(entries: Sequence[Entry], hidden: Hidden) -> list[Entry]:
    """A session's entries as reads see them while a request is open: the tainted turns' answers
    and tool results read as they will after the delete; the learner's words stay."""
    if not hidden.paused and not hidden.messages:
        return list(entries)
    out: list[Entry] = []
    current: str | None = None
    for e in entries:
        p = e.payload
        if e.type == "user":
            current = p.get("message_id")
        mids = {current, p.get("message_id")} if e.type == "assistant" else {p.get("message_id")}
        if e.type in ("assistant", "tool") and (hidden.paused or any(
                (e.session_id, m) in hidden.messages for m in mids)):
            p = _redacted(e.type, p, frozenset())
            e = e.model_copy(update={"payload": p})
        if e.type == "compaction" and (hidden.paused or any(
                sid == e.session_id for sid, _ in hidden.messages)):
            continue  # fold original turns again rather than reuse an unsafe compaction
        out.append(e)
    return out


def _redacted(type_: str, p: Mapping[str, Any], gone: frozenset[str]) -> dict[str, Any]:
    q = json.loads(json.dumps(p))
    if type_ == "assistant":
        q["text"] = PLACEHOLDER
        q["recalled"] = [{**r, "deleted": True} if isinstance(r, dict) and r.get("id") in gone
                         else r for r in q.get("recalled") or ()]
        if not q["recalled"]:
            q.pop("recalled")
    else:
        q["content"] = TOOL_PLACEHOLDER
        q["summary"] = ""
        q["args"] = {}
        q.pop("memory_ids", None)
    return q


# ---------- the ports ----------

@dataclass(frozen=True)
class RuleRow:
    item_id: str
    text: str
    authority: str


@dataclass(frozen=True)
class FactRow:
    key: str
    section: str
    text: str


@dataclass(frozen=True)
class SessionPrefs:
    rules: tuple[RuleRow, ...] = ()
    facts: tuple[FactRow, ...] = ()


class HarnessForget(Protocol):
    def forget(self, conn, user_id: str, session_id: str, *,
               quotes: Sequence[str]) -> dict[str, int]: ...


class SpecForget(Protocol):
    def items_from_session(self, user_id: str, session_id: str) -> SessionPrefs: ...

    def redact_session_evidence(self, conn, user_id: str, session_id: str, *,
                                request_id: str) -> dict[str, int]: ...

    def revoking(self, user_id: str, rule_ids: Sequence[str], fact_keys: Sequence[str],
                 body: Callable[[Any], None], *, now: datetime) -> None:
        """Run `body(conn)` in the one transaction that also revokes the ticked rules and facts
        (S3's normal paths). Raises whatever `body` raises; nothing is committed then."""
        ...


class LedgerPort(Protocol):
    def append(self, request_id: str, user_id: str, session_id: str | None,
               item_id: str | None, state: str, *, revoke_rule_ids: Sequence[str] = (),
               revoke_fact_keys: Sequence[str] = (), delete_source_ids: Sequence[str] = (),
               replaces: str | None = None) -> None: ...

    def revoke_of(self, request_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]: ...

    def sources_of(self, request_id: str) -> tuple[str, ...]: ...


class RsiHarnessForget:
    """`harness.forget.forget_session`."""

    def forget(self, conn, user_id, session_id, *, quotes) -> dict[str, int]:
        from evowiki.harness.forget import forget_session
        return forget_session(conn, user_id, session_id, quotes=quotes)


class S3SpecForget:
    """S3's `items_from_session` / `redact_session_evidence`; a ticked rule is removed the way the
    guidelines page removes it (`commit_spec` + RemoveRule + its pending card closed), a ticked
    fact the way its ledger undo removes it."""

    def __init__(self, repo: Any, profiles: Any, pool: ConnectionPool,
                 new_id: Callable[[str], str]):
        self._repo, self._profiles, self._pool, self._new_id = repo, profiles, pool, new_id

    def items_from_session(self, user_id, session_id) -> SessionPrefs:
        from evowiki.wikiapp.preferences import items_from_session
        got = items_from_session(self._repo.load(user_id).state, self._profiles.get(user_id),
                                 session_id)
        return SessionPrefs(
            rules=tuple(RuleRow(i.item_id, i.text, i.authority) for i in got.rules),
            facts=tuple(FactRow(key, sec, line) for sec, line, key in got.facts))

    def redact_session_evidence(self, conn, user_id, session_id, *, request_id) -> dict[str, int]:
        from evowiki.wikiapp.preferences import redact_session_evidence
        return redact_session_evidence(conn, user_id, session_id, request_id=request_id)

    def revoking(self, user_id, rule_ids, fact_keys, body, *, now) -> None:
        from evowiki.bot.executors_profile import _without
        from evowiki.profile.note import line_key
        from evowiki.wikiapp import teachspec as tspec
        from evowiki.wikiapp.preferences import close_pending
        from evowiki.wikiapp.teach import commit_spec

        facts = set(fact_keys)

        def drop_facts(conn) -> None:
            if not facts:
                return

            def change(p):
                for sec, lines in list(p.owned.items()):
                    for line in lines:
                        if line_key(sec, line) in facts and line in p.owned.get(sec, ()):
                            p = _without(p, sec, line, "")
                return p
            self._profiles.update_in(conn, user_id, change)

        snap = self._repo.load(user_id)
        live = {i.item_id for i in tspec.effective_spec(snap.state).items}
        rules = [r for r in rule_ids if r in live]     # a retry: already revoked ones are gone
        if not rules:
            with self._pool.connection() as conn, conn.transaction():
                drop_facts(conn)
                body(conn)
            return

        def change(s):        # every ticked rule in ONE version (commit_spec: previous + 1)
            from evowiki.memory.conflicts import one_version
            return one_version(s, [tspec.RemoveRule(r) for r in rules])
        commit_spec(self._repo, snap, change, actor="user", action="guideline", now=now,
                    new_id=self._new_id,
                    commit_extra=[*(close_pending(user_id, r, now) for r in rules), drop_facts,
                                  body])


# ---------- the service ----------

@dataclass(frozen=True)
class TaintedSession:
    session_id: str
    title: str
    turns: int


@dataclass(frozen=True)
class ForgetPreview:
    rules: tuple[RuleRow, ...]
    facts: tuple[FactRow, ...]
    tainted_sessions: tuple[TaintedSession, ...]
    memory_counts: Mapping[str, int]
    materials: "tuple[MaterialRow, ...]" = ()
    note: str = KEEP_NOTE


@dataclass(frozen=True)
class ForgetOutcome:
    request_id: str
    state: State
    error: str | None = None


ItemOutcome = Literal["committed", "failed", "not_found", "elsewhere"]


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def _note(conn, rid: str, step: str, rows: int) -> None:
    conn.execute("select evowiki_forget_note(%s, %s, %s)", (rid, step[:500], rows))


def delete_runs(conn, user_id: str, runs: Sequence[str], subjects: Sequence[str] = (),
                *, calls: Sequence[str] = ()) -> dict[str, int]:
    """The trace runs (with their steps) and their model calls (the steps' calls and every call
    / attempt whose subject is one of `subjects`), their requests and the texts only they used."""
    runs = list(runs)
    subjects = list(subjects)
    calls = list(calls) + [r["call_id"] for r in fetch_all(
        conn, "select distinct call_id from agent_step where run_id = any(%s) "
              "and call_id is not null", (runs,))]
    calls += [r["id"] for r in fetch_all(
        conn, "select id from llm_call where subject = any(%s) and id <> all(%s)",
        (subjects, calls))]
    hashes = [r["h"] for r in fetch_all(
        conn, "select payload->>'system_hash' as h from agent_step where run_id = any(%s) "
              "union select payload->>'tools_hash' from agent_step where run_id = any(%s) "
              "union select jsonb_array_elements_text(payload->'messages') from agent_step "
              "where run_id = any(%s) and jsonb_typeof(payload->'messages') = 'array' "
              "union select system_hash from llm_request where call_id = any(%s) "
              "union select prompt_hash from llm_request where call_id = any(%s)",
        (runs, runs, runs, calls, calls)) if r["h"]]
    n = {"agent_step": conn.execute("delete from agent_step where run_id = any(%s)",
                                    (runs,)).rowcount,
         "agent_run": conn.execute("delete from agent_run where user_id = %s "
                                   "and run_id = any(%s)", (user_id, runs)).rowcount,
         "llm_request": conn.execute("delete from llm_request where call_id = any(%s)",
                                     (calls,)).rowcount,
         "llm_attempt": conn.execute(
             "delete from llm_attempt where subject = any(%s) or input_hash in "
             "(select input_hash from llm_call where id = any(%s))", (subjects, calls)).rowcount,
         "llm_call": conn.execute("delete from llm_call where id = any(%s)", (calls,)).rowcount}
    n["llm_text"] = conn.execute(
        "delete from llm_text t where t.hash = any(%s) and not exists (select 1 from ("
        "select payload->>'system_hash' as h from agent_step "
        "union all select payload->>'tools_hash' from agent_step "
        "union all select jsonb_array_elements_text(payload->'messages') from agent_step "
        "where jsonb_typeof(payload->'messages') = 'array' "
        "union all select system_hash from llm_request "
        "union all select prompt_hash from llm_request) refs where refs.h = t.hash)",
        (hashes,)).rowcount
    return n


# a run (its trace run id) and every run nested in it: a step opened inside a run gets a run of
# its own whose input names the outer one (`input.parent_run`, agent.trace traced_step), and so on
_DESCENDANTS = ("with recursive r as (select run_id from agent_run where user_id = %(u)s and ({roots}) "
                "union select c.run_id from agent_run c join r on c.input ->> 'parent_run' = r.run_id "
                "where c.user_id = %(u)s) select run_id from r order by run_id")


def _session_runs(conn, user_id: str, session_id: str) -> list[str]:
    """The session's turn runs (`assistant:<session>:…`), the runs the gateway opened for a call
    of the session made outside any run (subject = the session; e.g. its consolidation), and the
    runs nested in them."""
    return [r["run_id"] for r in fetch_all(
        conn, _DESCENDANTS.format(roots="starts_with(run_id, %(p)s) or subject = %(s)s"),
        {"u": user_id, "p": f"assistant:{session_id}:", "s": session_id})]


def _turn_runs(conn, user_id: str, session_id: str, message_id: str) -> list[str]:
    """One turn's run, its hook runs (`…:hook:<i>`) and the runs nested in them."""
    run = f"assistant:{session_id}:{message_id}"
    return [r["run_id"] for r in fetch_all(
        conn, _DESCENDANTS.format(roots="run_id = %(r)s or starts_with(run_id, %(p)s)"),
        {"u": user_id, "r": run, "p": run + ":"})]


ACTION_REDACTED = "（来源已删除）"
# content keys kept as they are when a ledger row is redacted: ids, versions, hashes and the
# enum-like fields its executor and undo match on; every other string becomes ACTION_REDACTED
_ACTION_KEEP = frozenset({"kind", "op", "mode", "scope", "status", "level", "state", "section",
                          "confirm"})


def _keep_key(key: str | None) -> bool:
    return key is not None and (key in _ACTION_KEEP or key == "id"
                                or key.endswith(("_id", "_ids", "_version", "_hash", "_key")))


def _scrub_action(v: Any, key: str | None = None) -> Any:
    if isinstance(v, dict):
        return {k: _scrub_action(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub_action(x, key) for x in v]
    if isinstance(v, str) and v and not _keep_key(key):
        return ACTION_REDACTED
    return v


def redact_turn_actions(conn, user_id: str, session_id: str, message_id: str) -> int:
    """The ledger rows a tainted turn wrote (its trace run, or naming its message): content,
    summary and why redacted; id, status, undo and result kept, so the row still undoes. A
    preference row is S3's (his own words) and left to it."""
    from evowiki.agent.effects import payload_hash
    run = f"assistant:{session_id}:{message_id}"
    rows = fetch_all(
        conn, "select action_id, content from agent_action where user_id = %s "
              "and scope <> 'preference' and (run_id = %s or starts_with(run_id, %s) "
              "or (content ->> 'session_id' = %s and content ->> 'message_id' = %s))",
        (user_id, run, run + ":", session_id, message_id))
    for r in rows:
        c = _scrub_action(r["content"])
        conn.execute("update agent_action set content = %s, content_hash = %s, summary = %s, "
                     "why = %s where action_id = %s",
                     (Jsonb(c), payload_hash(c), ACTION_REDACTED, ACTION_REDACTED,
                      r["action_id"]))
    return len(rows)


def _candidates_of(conn, user_id: str, session_id: str, message_id: str | None) -> list[str]:
    """Forge tool candidates a session (message_id None: all of it, its request_tool overlay
    `session:<id>` included) or one of its turns asked for (their evidence cites the turn's
    trace run `assistant:<session>:<message>`)."""
    cites = ("select 1 from jsonb_array_elements_text(coalesce(evidence -> 'citations', "
             "'[]'::jsonb)) c where c = %s or starts_with(c, %s)")
    if message_id is None:
        run = f"trace:assistant:{session_id}:"
        sql, args = (f"origin = %s or exists ({cites})",
                     (f"session:{session_id}", run, run))
    else:
        run = f"trace:assistant:{session_id}:{message_id}"
        sql, args = f"exists ({cites})", (run, run + ":")
    return [r["id"] for r in fetch_all(
        conn, f"select id from forge_candidate where user_id = %s and ({sql})",
        (user_id, *args))]


SOURCE_DELETED = "source_deleted"
CITE_DELETED = "trace:source_deleted"     # still a valid citation (forge.models.Evidence)
# the free-text fields of a candidate's spec: a tool's purpose, a patch's goal / failing case
_SPEC_TEXT = ("purpose", "goal", "failing_case")


def forget_candidates(conn, user_id: str, ids: Sequence[str], *, cites: str,
                      session_id: str | None = None,
                      quotes: Sequence[str] = ()) -> dict[str, int]:
    """Forge candidates a forgotten session / turn asked for (`cites`: the prefix of its trace
    citations). One that is still in use (named by the tool registry or a live event, or the
    parent of another candidate) is kept, code and schema as they are: its references to the
    session (`origin` `session:<id>`, the citations) become source_deleted and its free text
    (the spec's purpose / goal / failing case, the evidence note, a patch's reason) is redacted.
    Its examples stay, minus the learner's words: `quotes` (the closure's user messages) cut
    into fragments and replaced where an example string holds one, as harness RSI does
    (`harness.forget.fragments` / `scrub`); a changed spec gets `examples_redacted: true`.
    Every other one is deleted with its vectors and patch row. The build traces of both go."""
    ids = sorted(set(ids))
    if not ids:
        return {}
    used = sorted(r["cid"] for r in fetch_all(
        conn, "select x.cid from unnest(%s::text[]) as x(cid) where exists (select 1 "
              "from forge_registry g where exists (select 1 from jsonb_each_text(g.entries) e "
              "where e.value = x.cid) or g.change ->> 'candidate_id' = x.cid) "
              "or exists (select 1 from forge_live_event v where v.candidate_id = x.cid) "
              "or exists (select 1 from forge_candidate k where k.parent_id = x.cid "
              "and k.id <> all(%s::text[]))", (ids, ids)))
    gone = [i for i in ids if i not in used]
    from evowiki.harness.forget import fragments, scrub
    frags = fragments(quotes)
    n = delete_runs(conn, user_id, [f"forge:{i}" for i in ids])
    for r in fetch_all(conn, "select id, origin, spec, evidence from forge_candidate "
                             "where id = any(%s)", (used,)):
        spec = {k: (ACTION_REDACTED if k in _SPEC_TEXT and isinstance(v, str) else v)
                for k, v in (r["spec"] or {}).items()}
        for k in ("examples", "holdout"):
            if k in spec and (new := scrub(spec[k], frags)) != spec[k]:
                spec[k] = new
                spec["examples_redacted"] = True
        ev = dict(r["evidence"] or {})
        ev["citations"] = [CITE_DELETED if isinstance(c, str) and c.startswith(cites) else c
                           for c in ev.get("citations") or ()]
        if ev.get("note"):
            ev["note"] = ACTION_REDACTED
        origin = (SOURCE_DELETED if session_id is not None
                  and r["origin"] == f"session:{session_id}" else r["origin"])
        conn.execute("update forge_candidate set origin = %s, spec = %s, evidence = %s "
                     "where id = %s", (origin, Jsonb(spec), Jsonb(ev), r["id"]))
    n["forge_candidate_redacted"] = len(used)
    n["forge_patch_redacted"] = conn.execute(
        "update forge_patch set reason = %s where candidate_id = any(%s) and reason <> ''",
        (ACTION_REDACTED, used)).rowcount
    n["forge_tool_vec"] = conn.execute("delete from forge_tool_vec where candidate_id = any(%s)",
                                       (gone,)).rowcount
    n["forge_patch"] = conn.execute("delete from forge_patch where candidate_id = any(%s)",
                                    (gone,)).rowcount
    n["forge_candidate"] = conn.execute("delete from forge_candidate where id = any(%s)",
                                        (gone,)).rowcount
    return n


@dataclass(frozen=True)
class MaterialRow:
    source_id: str
    title: str
    used_by: tuple[str, ...]          # elsewhere: session:<id> / page:<id> / goal:<id> / node:<id>

    @property
    def in_use_elsewhere(self) -> bool:
        return bool(self.used_by)


def session_materials(conn, user_id: str, session_id: str) -> list[MaterialRow]:
    """The live materials the session used (`source_use.session_id`), each with where else it is
    used: another session, a page, a goal (its other uses) or a knowledge node (its links)."""
    out = []
    for r in fetch_all(
            conn, "select s.source_id, s.title from source s where s.user_id = %s "
                  "and s.deleted_at is null and exists (select 1 from source_use u "
                  "where u.source_id = s.source_id and u.session_id = %s) "
                  "order by s.created_at, s.source_id", (user_id, session_id)):
        used: list[str] = []
        for u in fetch_all(
                conn, "select session_id, page_id, goal_id from source_use where source_id = %s "
                      "and session_id is distinct from %s order by created_at, use_id",
                (r["source_id"], session_id)):
            used += [f"{k}:{u[k + '_id']}" for k in ("session", "page", "goal")
                     if u[k + "_id"] is not None]
        used += [f"node:{x['node_id']}" for x in fetch_all(
            conn, "select distinct l.node_id from source_link l join source_section c "
                  "on c.section_id = l.section_id where c.source_id = %s order by l.node_id",
            (r["source_id"],))]
        out.append(MaterialRow(r["source_id"], r["title"], tuple(dict.fromkeys(used))))
    return out


def forget_materials(conn, user_id: str, session_id: str, delete_ids: Sequence[str],
                     now: datetime) -> dict[str, int]:
    """The ticked materials go the library's way (`sources.store.soft_delete`, under the user's
    writer lock, like DELETE /sources/{id}); every other material stays as it is, only the
    session's uses of it (`source_use` rows naming the session) are removed."""
    from evowiki.sources.store import soft_delete
    from evowiki.store.locks import xact_lock
    n = {"source": 0}
    if delete_ids:
        xact_lock(conn, f"evowiki.user:{user_id}")
        for sid in delete_ids:             # a retry finds an already deleted one: no-op
            n["source"] += int(soft_delete(conn, user_id, sid, now))
    n["source_use"] = conn.execute("delete from source_use where user_id = %s "
                                   "and session_id = %s", (user_id, session_id)).rowcount
    return n


# ---------- DBOS's records (workflow inputs, step outputs, streams) ----------
# The service's DBOS system tables live in schema `dbos` of this same database, owned by
# evowiki_app (0082), so they are deleted inside the forget transaction. This mirrors DBOS's own
# `SystemDatabase.delete_workflows` (dbos 3.2: workflow_input, workflow_output,
# operation_outputs, then workflow_status, whose delete cascades into notifications,
# workflow_events(_history) and streams) with `delete_children=True` (children by
# parent_workflow_id). tests/memory/test_forget_dbos.py pins that list against DBOS's schema.
DBOS_SCHEMA = "dbos"
DBOS_PAYLOAD_TABLES = ("workflow_input", "workflow_output", "operation_outputs")
DBOS_ACTIVE = ("PENDING", "ENQUEUED")


def _dbos_present(conn) -> bool:
    return fetch_one(conn, "select to_regclass(%s) is not null as ok",
                     (f"{DBOS_SCHEMA}.workflow_status",))["ok"]


def turn_workflow_prefix(session_id: str) -> str:
    """assistant.turn_runs.TurnJob.workflow_id is `turn:<session>:<message>`."""
    return f"turn:{session_id}:"


def wrapup_workflow_prefix(user_id: str) -> str:
    """assistant.wrapup.Job.workflow_id: every background wrap-up of the user. A wrap-up of another
    session may hold what this one's content reached, so a delete waits for all of them."""
    return f"wrapup:{user_id}:"


def source_workflow_ids(user_id: str, source_id: str) -> tuple[list[str], list[str]]:
    """(exact ids, id prefixes) of a material's import workflows (sources.runs.workflow_id /
    start_workflow_id: `source:<user>:<source>[:<gen>]`, `start:` + that)."""
    base = f"source:{user_id}:{source_id}"
    return [base, "start:" + base], [base + ":", "start:" + base + ":"]


def dbos_workflows(conn, exact: Sequence[str], prefixes: Sequence[str]) -> list[dict[str, Any]]:
    """The workflows with one of these ids or id prefixes, and all their descendants:
    (workflow_uuid, status). [] when DBOS has no tables here (never launched on this DB)."""
    if not (exact or prefixes) or not _dbos_present(conn):
        return []
    return fetch_all(
        conn, f"with recursive w as (select workflow_uuid, status from {DBOS_SCHEMA}.workflow_status "
              "where workflow_uuid = any(%s) or exists (select 1 from unnest(%s::text[]) p "
              "where starts_with(workflow_uuid, p)) union select s.workflow_uuid, s.status "
              f"from {DBOS_SCHEMA}.workflow_status s join w on s.parent_workflow_id = "
              "w.workflow_uuid) select * from w order by workflow_uuid",
        (list(exact), list(prefixes)))


def delete_dbos_workflows(conn, exact: Sequence[str], prefixes: Sequence[str]) -> dict[str, int]:
    """Delete those workflows with their children and every record DBOS keeps of them. A
    workflow still PENDING / ENQUEUED raises ForgetBusy (nothing deleted)."""
    rows = dbos_workflows(conn, exact, prefixes)
    if busy := [r["workflow_uuid"] for r in rows if r["status"] in DBOS_ACTIVE]:
        raise ForgetBusy(f"DBOS workflows still running: {busy}")
    ids = [r["workflow_uuid"] for r in rows]
    if not ids:
        return {}
    n = {f"dbos.{t}": conn.execute(f"delete from {DBOS_SCHEMA}.{t} where workflow_uuid = any(%s)",
                                   (ids,)).rowcount for t in DBOS_PAYLOAD_TABLES}
    n["dbos.workflow_status"] = conn.execute(
        f"delete from {DBOS_SCHEMA}.workflow_status where workflow_uuid = any(%s)",
        (ids,)).rowcount
    return n


def forget_artifacts(conn, user_id: str, answer_ids: Sequence[str],
                     compaction_ids: Sequence[str]) -> dict[str, int]:
    """The lineage rows (observability 0091 `artifact`, ids and content hashes only) of the
    answers and compaction summaries that go: kind 'answer' (ref = the answer's message id) and
    kind 'compaction' (ref = the compaction entry id); their `artifact_dependency` edges cascade.
    Pages, rules, materials and the rest keep theirs (their own products). {} before 0091."""
    if not (answer_ids or compaction_ids) or fetch_one(
            conn, "select to_regclass('public.artifact') is null as missing")["missing"]:
        return {}
    edges = fetch_one(
        conn, "select count(distinct (d.child_id, d.parent_id, d.role)) as n "
              "from artifact_dependency d join artifact a "
              "on a.artifact_id in (d.child_id, d.parent_id) where a.user_id = %(u)s and ("
              "(a.kind = 'answer' and a.ref = any(%(ans)s)) or "
              "(a.kind = 'compaction' and a.ref = any(%(comp)s)))",
        {"u": user_id, "ans": list(answer_ids), "comp": list(compaction_ids)})["n"]
    n = conn.execute("delete from artifact where user_id = %(u)s and ("
                     "(kind = 'answer' and ref = any(%(ans)s)) or "
                     "(kind = 'compaction' and ref = any(%(comp)s)))",
                     {"u": user_id, "ans": list(answer_ids),
                      "comp": list(compaction_ids)}).rowcount
    return {"artifact": n, "artifact_dependency": int(edges)}


def residue(conn, user_id: str, session_id: str) -> list[str]:
    """What of a forgotten session is still stored (empty: nothing): its rows anywhere, or a
    tainted turn not handled."""
    left = []
    checks = (
        ("assistant_session", "select 1 from assistant_session where session_id = %s"),
        ("assistant_entry", "select 1 from assistant_entry where session_id = %s"),
        ("memory_episode", "select 1 from memory_episode where session_id = %s"),
        ("memory_item", ("select 1 from memory_item where session_id = %s or exists ("
                         "select 1 from jsonb_array_elements(evidence) e "
                         "where e ->> 'session_id' = %s)")),
        ("llm_call", "select 1 from llm_call where subject = %s"),
        ("source_use", "select 1 from source_use where session_id = %s"),
    )
    for name, sql in checks:
        args = (session_id, session_id) if sql.count("%s") == 2 else (session_id,)
        if fetch_one(conn, sql + " limit 1", args) is not None:
            left.append(name)
    if _session_runs(conn, user_id, session_id):
        left.append("agent_run")
    if _candidates_of(conn, user_id, session_id, None):
        left.append("forge_candidate")
    if dbos_workflows(conn, [], [turn_workflow_prefix(session_id),
                                 f"{wrapup_workflow_prefix(user_id)}{session_id}:"]):
        left.append("dbos.workflow_status")
    t = taint_closure(conn, user_id, session_id)
    if fetch_one(conn, "select 1 from dream_source where user_id=%s and source_id=any(%s) limit 1",
                 (user_id, list(t.paths))) is not None:
        left.append("dream_source")
    calls = [n.removeprefix("call:") for n in t.paths if n.startswith("call:")]
    if calls and fetch_one(conn, "select 1 from llm_call where id=any(%s) limit 1", (calls,)):
        left.append("llm_call:derived")
    for turn in t._turns:
        if any(r["type"] == "assistant" and r["payload"].get("text") != PLACEHOLDER
               for r in turn.entries):
            left.append(f"answer:{turn.key}")
        if _turn_runs(conn, user_id, turn.session_id, turn.message_id):
            left.append(f"agent_run:{turn.key}")
        if dbos_workflows(conn, [turn_workflow_prefix(turn.session_id) + turn.message_id], []):
            left.append(f"dbos.workflow_status:{turn.key}")
    if t.episodes and fetch_one(
            conn, "select 1 from memory_episode where user_id = %s "
                  "and session_id || '/' || message_id = any(%s) limit 1",
            (user_id, sorted(t.episodes))) is not None:
        left.append("memory_episode:tainted")
    if t.items and fetch_one(conn, "select 1 from memory_item where item_id = any(%s) limit 1",
                             (sorted(t.items),)) is not None:
        left.append("memory_item:tainted")
    return left


class ForgetService:
    """The delete flow over `pool` (connected as evowiki_app). `harness` / `spec` None: deleting a
    session is refused (ForgetUnavailable, no request made)."""

    def __init__(self, pool: ConnectionPool, ledger: LedgerPort, *,
                 harness: HarnessForget | None, spec: SpecForget | None,
                 clock: Callable[[], datetime], new_id: Callable[[str], str]):
        self._pool, self._ledger = pool, ledger
        self._harness, self._spec = harness, spec
        self._clock, self._new_id = clock, new_id

    @property
    def available(self) -> bool:
        return self._harness is not None and self._spec is not None

    def _require(self) -> None:
        if not self.available:
            raise ForgetUnavailable(UNAVAILABLE)

    def _owned(self, conn, user_id: str, session_id: str) -> None:
        r = fetch_one(conn, "select user_id from assistant_session where session_id = %s",
                      (session_id,))
        if r is None or r["user_id"] != user_id:
            raise LookupError(f"unknown assistant session {session_id}")

    # ---- preview ----

    def preview(self, user_id: str, session_id: str) -> ForgetPreview:
        self._require()
        assert self._spec is not None
        with self._pool.connection() as conn:
            self._owned(conn, user_id, session_id)
            t = taint_closure(conn, user_id, session_id)
            materials = tuple(session_materials(conn, user_id, session_id))
            counts = fetch_one(
                conn, "select (select count(*) from memory_episode where session_id = %(s)s) "
                      "as episodes, (select count(*) from memory_item where item_id = "
                      "any(%(i)s)) as items", {"s": session_id, "i": sorted(t.items)})
        prefs = self._spec.items_from_session(user_id, session_id)
        tainted = []
        for sid in sorted(t.sessions):
            rows = t._rows.get(sid, ())
            first = next((r for r in rows if r["type"] == "user"), None)
            tainted.append(TaintedSession(
                session_id=sid,
                title=conversation_title(first["payload"].get("text") or "") if first else "",
                turns=sum(1 for s, _ in t.turns if s == sid)))
        return ForgetPreview(
            rules=prefs.rules, facts=prefs.facts, tainted_sessions=tuple(tainted),
            materials=materials,
            memory_counts={"episodes": int(counts["episodes"]), "items": int(counts["items"]),
                           "tainted_turns": len(t.turns)})

    # ---- a session ----

    def open_request(self, user_id: str, session_id: str) -> str | None:
        with self._pool.connection() as conn:
            r = fetch_one(conn, "select request_id from forget_request where user_id = %s "
                                "and session_id = %s and state <> 'committed' "
                                "order by requested_at limit 1", (user_id, session_id))
        return None if r is None else r["request_id"]

    def request_session(self, user_id: str, session_id: str, *,
                        revoke_rule_ids: Sequence[str] = (), revoke_fact_keys: Sequence[str] = (),
                        delete_source_ids: Sequence[str] = (), requested_by: str) -> str:
        """The request (state requested) and its ledger row, in one transaction: a ledger that
        cannot be written leaves no request. An open request of the session is reused.
        `delete_source_ids` must be materials of the session (`session_materials`; else
        LookupError and no request)."""
        self._require()
        if (rid := self.open_request(user_id, session_id)) is not None:
            return rid
        with self._pool.connection() as conn, conn.transaction():
            lock_consolidation(conn, session_id)      # after a landing in flight, before the next
            self._owned(conn, user_id, session_id)
            mine = {m.source_id for m in session_materials(conn, user_id, session_id)}
            if not set(delete_source_ids) <= mine:
                raise LookupError(f"not materials of {session_id}: "
                                  f"{sorted(set(delete_source_ids) - mine)}")
            rid = conn.execute("select evowiki_forget_request(%s, %s, null, %s) as rid",
                               (user_id, session_id, requested_by)).fetchone()["rid"]
            self._ledger.append(rid, user_id, session_id, None, "requested",
                                revoke_rule_ids=tuple(revoke_rule_ids),
                                revoke_fact_keys=tuple(revoke_fact_keys),
                                delete_source_ids=tuple(dict.fromkeys(delete_source_ids)))
        return rid

    def forget_session(self, user_id: str, session_id: str, *,
                       revoke_rule_ids: Sequence[str] = (), revoke_fact_keys: Sequence[str] = (),
                       delete_source_ids: Sequence[str] = (),
                       requested_by: str) -> ForgetOutcome:
        rid = self.request_session(user_id, session_id, revoke_rule_ids=revoke_rule_ids,
                                   revoke_fact_keys=revoke_fact_keys,
                                   delete_source_ids=delete_source_ids,
                                   requested_by=requested_by)
        return self.run(rid)

    def _request(self, request_id: str) -> dict[str, Any]:
        with self._pool.connection() as conn:
            r = fetch_one(conn, "select * from forget_request where request_id = %s",
                          (request_id,))
        if r is None:
            raise LookupError(f"unknown forget request {request_id}")
        return r

    def run(self, request_id: str) -> ForgetOutcome:
        """One attempt of the deleting transaction; committed, or failed with the error (the
        request is left `failed` for a retry; nothing was deleted)."""
        req = self._request(request_id)
        if req["state"] == "committed":
            return ForgetOutcome(request_id, "committed")
        try:
            while True:
                with self._pool.connection() as conn, conn.transaction():
                    complete = closure_batch(conn, request_id)
                if complete:
                    break
            if req["session_id"] is not None:
                self._run_session(req)
            else:
                self._run_item(req)
        except Exception as exc:   # recorded on the request, retried later
            log.warning("forget request %s failed", request_id, exc_info=True)
            err = f"{type(exc).__name__}: {exc}"[:2000]
            try:
                with self._pool.connection() as conn:
                    conn.execute("select evowiki_forget_fail(%s, %s)", (request_id, err))
            except Exception:
                if self._request(request_id)["state"] == "committed":   # a concurrent run won
                    return ForgetOutcome(request_id, "committed")
                raise
            return ForgetOutcome(request_id, "failed", err)
        self._committed(req)
        return ForgetOutcome(request_id, "committed")

    def _committed(self, req: Mapping[str, Any]) -> None:
        try:
            self._ledger.append(req["request_id"], req["user_id"], req["session_id"],
                                req["item_id"], "committed")
        except Exception:
            # the database committed; the ledger stays `requested` and reconcile runs it again
            # (a no-op on a clean database)
            log.exception("forget ledger: committed row for %s not written", req["request_id"])

    def _await_wrapups(self, user_id: str) -> None:
        """The user's background wrap-ups see the open request and stop at their next step: a
        moment, unless one is in a slow model call (then the request fails as ForgetBusy, and that
        run starts it again when it ends)."""
        deadline = time.monotonic() + WRAPUP_WAIT_S
        while time.monotonic() < deadline:
            with self._pool.connection() as conn:
                rows = dbos_workflows(conn, [], [wrapup_workflow_prefix(user_id)])
            if not any(r["status"] in DBOS_ACTIVE for r in rows):
                return
            time.sleep(WRAPUP_POLL_S)

    def retry_user(self, user_id: str) -> None:
        """Run this user's requests that have not committed."""
        with self._pool.connection() as conn:
            rows = fetch_all(conn, "select request_id from forget_request where user_id = %s "
                                   "and state <> 'committed' order by requested_at", (user_id,))
        for r in rows:
            self.run(r["request_id"])

    def _run_session(self, req: Mapping[str, Any]) -> None:
        self._require()
        assert self._spec is not None and self._harness is not None
        rid, uid, sid = req["request_id"], req["user_id"], req["session_id"]
        self._await_wrapups(uid)
        rules, facts = self._ledger.revoke_of(rid)
        sources = self._ledger.sources_of(rid)
        now = self._clock()
        spec, harness = self._spec, self._harness

        def body(conn) -> None:
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            # the exclusive side of store.trace's prune lock: a trace sink write that reuses an
            # llm_text row holds the shared side until it commits, so the texts this delete
            # (and harness forget_session's) finds unreferenced really are
            conn.execute("select pg_advisory_xact_lock(hashtext('trace:prune'))")
            t = _persisted_taint(conn, uid, sid, rid)
            _invalidate_derived(conn, uid, list(t.paths), purge=True)
            _note(conn, rid, "closure checkpoint nodes", len(t.paths))
            exact = [turn_workflow_prefix(s) + m for s, m in sorted(t.turns)]
            prefixes = [turn_workflow_prefix(sid), wrapup_workflow_prefix(uid)]
            for src in sources:
                e, p = source_workflow_ids(uid, src)
                exact += e
                prefixes += p
            for k, v in delete_dbos_workflows(conn, exact, prefixes).items():
                _note(conn, rid, k, v)
            before = {s: {r["entry_id"]: _hash(r["payload"]) for r in rows}
                      for s, rows in t._rows.items()}
            changed = self._treat_tainted(conn, rid, uid, t, now)
            own = fetch_all(conn, "select type, entry_id, payload ->> 'message_id' as mid "
                                  "from assistant_entry where session_id = %s "
                                  "and type in ('assistant', 'compaction')", (sid,))
            answers = [r["mid"] for r in own if r["type"] == "assistant"]
            answers += [a for turn in t._turns for a in turn.answer_ids]
            comps = [r["entry_id"] for r in own if r["type"] == "compaction"]
            comps += [r["entry_id"] for rows in t._rows.values() for r in rows
                      if r["type"] == "compaction" and r["entry_id"] in changed]
            for k, v in forget_artifacts(conn, uid, answers, comps).items():
                _note(conn, rid, k, v)
            self._delete_memory(conn, rid, uid, sid, t, [*rules, *facts])
            runs = delete_runs(conn, uid, _session_runs(conn, uid, sid), [sid])
            for k, v in runs.items():
                _note(conn, rid, f"session.{k}", v)
            n = conn.execute(
                "delete from agent_action where user_id = %s and scope <> 'preference' and ("
                "starts_with(run_id, %s) or content ->> 'session_id' = %s)",
                (uid, f"assistant:{sid}:", sid)).rowcount
            _note(conn, rid, "agent_action", n)
            for k, v in forget_candidates(conn, uid, _candidates_of(conn, uid, sid, None),
                                          cites=f"trace:assistant:{sid}:",
                                          session_id=sid, quotes=t.quotes).items():
                _note(conn, rid, f"session.{k}", v)
            for k, v in forget_materials(conn, uid, sid, sources, now).items():
                _note(conn, rid, f"materials.{k}", v)
            spec.redact_session_evidence(conn, uid, sid, request_id=rid)
            harness.forget(conn, uid, sid, quotes=t.quotes)
            n = conn.execute("delete from assistant_turn_stop where session_id = %s",
                             (sid,)).rowcount
            _note(conn, rid, "assistant_turn_stop", n)
            conn.execute("delete from assistant_entry where session_id = %s", (sid,))
            conn.execute("delete from assistant_session where session_id = %s", (sid,))
            conn.execute("insert into forgotten_session (user_id, session_id, at) "
                         "values (%s, %s, %s) on conflict (session_id) do nothing",
                         (uid, sid, now))
            _note(conn, rid, "forgotten_session", 1)
            self._check(conn, uid, sid, t, before, changed)
            conn.execute("select evowiki_forget_close(%s)", (rid,))

        spec.revoking(uid, rules, facts, body, now=now)

    def _treat_tainted(self, conn, rid: str, uid: str, t: Taint, now: datetime) -> set[str]:
        """Placeholder answers, redacted tool results, deleted episodes / traces of the tainted
        turns; void the compactions that folded one. Returns the entry ids changed."""
        from evowiki.assistant.pg_store import insert_entry, lock_session

        gone = frozenset({f"ep:{e}" for e in t.episodes} | {f"item:{i}" for i in t.items})
        changed: set[str] = set()
        for turn in t._turns:
            for r in turn.entries:
                if r["type"] not in ("assistant", "tool"):
                    continue
                new = _redacted(r["type"], r["payload"], gone)
                if new != r["payload"]:
                    conn.execute("update assistant_entry set payload = %s "
                                 "where session_id = %s and seq = %s",
                                 (Jsonb(new), r["session_id"], r["seq"]))
                changed.add(r["entry_id"])
            n = conn.execute("delete from memory_episode where session_id = %s "
                             "and message_id = %s", (turn.session_id, turn.message_id)).rowcount
            _note(conn, rid, f"memory_episode {turn.key}", n)
            for k, v in delete_runs(conn, uid, _turn_runs(conn, uid, turn.session_id,
                                                          turn.message_id)).items():
                _note(conn, rid, f"turn {turn.key}.{k}", v)
            _note(conn, rid, f"turn {turn.key}.agent_action redacted",
                  redact_turn_actions(conn, uid, turn.session_id, turn.message_id))
            for k, v in forget_candidates(
                    conn, uid, _candidates_of(conn, uid, turn.session_id, turn.message_id),
                    cites=f"trace:assistant:{turn.session_id}:{turn.message_id}",
                    quotes=t.quotes).items():
                _note(conn, rid, f"turn {turn.key}.{k}", v)
        for sid in sorted(t.sessions):
            rows = t._rows[sid]
            info = SessionInfo.model_validate(fetch_one(
                conn, "select * from assistant_session where session_id = %s", (sid,)))
            entries = [Entry.model_validate({**r}) for r in rows]
            state = fold(info, entries)
            idx = {tt.message_id: i for i, tt in enumerate(state.turns)}
            first = min(idx[m] for s, m in t.turns if s == sid and m in idx)
            if first >= state.first_kept:
                continue                      # no compaction folded a tainted turn
            # a compaction whose kept turn comes after `first` summarised it: void it
            seen = 0
            for r in rows:
                if r["type"] == "user":
                    seen += 1
                if r["type"] != "compaction":
                    continue
                kept = r["payload"].get("first_kept")
                if (seen if kept is None else idx.get(kept, seen)) <= first:
                    continue
                if any(r["payload"].get(k) for k in VOIDED_KEYS):
                    # every summary part goes (a compact@v7 hand-over, its learning items and
                    # edits, or an older text); where the cut fell stays
                    p = {**{k: v for k, v in r["payload"].items() if k not in VOIDED_KEYS},
                         "summary": ""}
                    conn.execute("update assistant_entry set payload = %s "
                                 "where session_id = %s and seq = %s",
                                 (Jsonb(p), sid, r["seq"]))
                changed.add(r["entry_id"])
            lock_session(conn, sid)
            insert_entry(conn, sid, f"forget_{rid}_{sid}", "compaction",
                         {"summary": "", "first_kept": state.turns[first].message_id}, now)
            _note(conn, rid, f"compaction {sid} voided", 1)
        return changed

    def _delete_memory(self, conn, rid: str, uid: str, sid: str, t: Taint,
                       revoked: Sequence[str] = ()) -> None:
        from evowiki.memory.conflicts import drop_with

        mids = [r["payload"]["message_id"] for r in fetch_all(
            conn, "select payload from assistant_entry where session_id = %s "
                  "and type in ('user', 'assistant')", (sid,))]
        rows = fetch_all(
            conn, "delete from memory_item where user_id = %s and (item_id = any(%s) "
                  "or session_id = %s) returning item_id, pending_action_id",
            (uid, sorted(t.items), sid))
        gone = [r["item_id"] for r in rows]
        _note(conn, rid, "memory_item", len(gone))
        # a fact from these words still waiting for his yes is in no profile yet: its card would
        # go on telling the chat what he deleted (a pending rule is listed in the dialog instead)
        n = conn.execute("delete from agent_action where user_id = %s and action_id = any(%s) "
                         "and kind = 'profile_fact' and status = 'proposed'",
                         (uid, [r["pending_action_id"] for r in rows
                                if r["pending_action_id"]])).rowcount
        _note(conn, rid, "agent_action waiting fact", n)
        # the conflict groups naming what goes (its items, the rules / lines he ticked)
        _note(conn, rid, "memory_conflict", drop_with(conn, uid, [*gone, *revoked]))
        n = conn.execute("delete from memory_episode where session_id = %s", (sid,)).rowcount
        _note(conn, rid, "memory_episode", n)
        n = conn.execute("delete from memory_suppression where user_id = %s "
                         "and evidence_message_ids && %s::text[]", (uid, mids)).rowcount
        _note(conn, rid, "memory_suppression", n)

    def _check(self, conn, uid: str, sid: str, t: Taint, before: Mapping[str, Mapping[str, str]],
               changed: set[str]) -> None:
        """Every node handled; every other entry of the touched sessions unchanged."""
        for s, hashes in before.items():
            now_rows = {r["entry_id"]: _hash(r["payload"]) for r in fetch_all(
                conn, "select entry_id, payload from assistant_entry where session_id = %s",
                (s,))}
            for eid, h in hashes.items():
                if eid not in changed and now_rows.get(eid) != h:
                    raise ForgetIntegrityError(f"untainted entry {eid} of {s} changed")
        if left := residue(conn, uid, sid):
            raise ForgetIntegrityError(f"left after forgetting {sid}: {left}")

    # ---- one item ----

    def forget_item(self, user_id: str, item_id: str, *, requested_by: str) -> ItemOutcome:
        """Delete one decision / open question and record its suppression (ids, kind, scope; no
        text), through a forget request like a session."""
        with self._pool.connection() as conn:
            it = fetch_one(conn, "select kind from memory_item where user_id = %s "
                                 "and item_id = %s", (user_id, item_id))
        if it is None:
            return "not_found"
        if it["kind"] in ("preference", "about_me"):
            return "elsewhere"
        with self._pool.connection() as conn, conn.transaction():
            rid = conn.execute("select evowiki_forget_request(%s, null, %s, %s) as rid",
                               (user_id, item_id, requested_by)).fetchone()["rid"]
            self._ledger.append(rid, user_id, None, item_id, "requested")
        return "committed" if self.run(rid).state == "committed" else "failed"

    def _run_item(self, req: Mapping[str, Any]) -> None:
        from evowiki.memory.store import _ITEM_COLS, _item, suppression_of

        rid, uid, item_id = req["request_id"], req["user_id"], req["item_id"]
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            t = _persisted_taint(conn, uid, "", rid)
            suppress_sources(conn, uid, rid, list(t.paths))
            _invalidate_derived(conn, uid, list(t.paths), purge=True)
            self._treat_tainted(conn, rid, uid, t, self._clock())
            r = fetch_one(conn, f"delete from memory_item where user_id = %s and item_id = %s "
                                f"returning {_ITEM_COLS}", (uid, item_id))
            _note(conn, rid, "memory_item", 0 if r is None else 1)
            from evowiki.memory.conflicts import drop_with
            _note(conn, rid, "memory_conflict", drop_with(conn, uid, [item_id]))
            if r is not None:
                it = _item(r)
                roots = [f"item:{item_id}"] + [
                    f"message:{e.session_id}/{e.message_id}" for e in it.evidence
                    if e.session_id and e.message_id]
                suppress_sources(conn, uid, rid, roots)
                n = conn.execute(
                    "insert into memory_suppression (user_id, item_id, kind, "
                    "evidence_message_ids, scope_type, scope_id, at) values "
                    "(%s, %s, %s, %s, %s, %s, %s) on conflict (item_id) do nothing",
                    (uid, it.item_id, it.kind, sorted(suppression_of(it).evidence_message_ids),
                     it.scope_type, it.scope_id, self._clock())).rowcount
                _note(conn, rid, "memory_suppression", n)
            self._delete_memory(conn, rid, uid, "", t)
            conn.execute("select evowiki_forget_close(%s)", (rid,))

    # ---- retries ----

    def pending(self, user_id: str) -> list[dict[str, Any]]:
        """The user's session requests not committed yet: (session_id, state)."""
        with self._pool.connection() as conn:
            return fetch_all(conn, "select session_id, state, requested_at, "
                                   "(select count(*) from forget_frontier f where "
                                   "f.request_id=forget_request.request_id) as closure_nodes, "
                                   "(select count(*) from forget_frontier f where "
                                   "f.request_id=forget_request.request_id and expanded) as expanded_nodes "
                                   "from forget_request "
                                   "where user_id = %s and state <> 'committed' "
                                   "and session_id is not null order by requested_at",
                             (user_id,))

    def open_requests(self) -> list[str]:
        with self._pool.connection() as conn:
            return [r["request_id"] for r in fetch_all(
                conn, "select request_id from forget_request where state <> 'committed' "
                      "order by requested_at")]

    def retry_open(self) -> list[ForgetOutcome]:
        """Run every request that has not committed (the API's 202s, a crash mid-way)."""
        return [self.run(rid) for rid in self.open_requests()]
