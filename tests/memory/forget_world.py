"""A user with four sessions whose content flowed into each other, for the forget tests (plan
Tasks 13–15). Postgres (`pool` as evowiki_app); RSI's and S3's steps are fakes.

  A  a1「我决定用 pandas 处理数据」  a2  (the session deleted)
  B  b1 clean · b2 recalled ep:A/a1 · b3 clean · [compaction keeping b3: folded b1, b2]
     · b4 recalled ep:C/c2
  C  c1 recalled ep:A/a2 · c2 search_memory showed ep:B/b2 and recalled ep:B/b4 (a cycle with
     b4) · c3 read_past_turn of B/b1 (clean)
  D  d1 clean

Tainted outside A: b2, b4, c1, c2 (A→B, A→C, B→C, C→B). Clean: b1, b3, c3, d1. Items: one
from A, one citing b2 (tainted), one citing b1 (kept)."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.memory.contracts import EvidenceRef
from evowiki.memory.forget import ForgetService, RuleRow, SessionPrefs
from evowiki.memory.store import Episode, Item, PgMemoryStore
from evowiki.memory.tombstone import Ledger
from tests.memory.conftest import seed_user_and_session

T0 = datetime(2026, 10, 28, 9, tzinfo=UTC)
CLEAN = (("B", "b1"), ("B", "b3"), ("C", "c3"), ("D", "d1"))
TAINTED = (("B", "b2"), ("B", "b4"), ("C", "c1"), ("C", "c2"))
WORDS = {"a1": "我决定用 pandas 处理数据", "a2": "pandas 的 groupby 怎么写",
         "b1": "先复习一下均值", "b2": "接着上次的数据处理说", "b3": "方差是什么",
         "b4": "再讲讲那个分组", "c1": "groupby 再举个例子", "c2": "上次说的处理方式是什么",
         "c3": "均值那轮怎么说的", "d1": "今天学概率"}


class FakeHarness:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail: BaseException | None = None
        self.also: Any = None          # (conn) -> None: an extra write inside the transaction

    def forget(self, conn, user_id, session_id, *, quotes):
        self.calls.append((session_id, tuple(quotes)))
        if self.also is not None:
            self.also(conn)
        if self.fail is not None:
            raise self.fail
        return {}


class FakeSpec:
    def __init__(self, pool) -> None:
        self.pool = pool
        self.redacted: list[str] = []
        self.revoked: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def items_from_session(self, user_id, session_id):
        return SessionPrefs(rules=(RuleRow("si1", "先给例子再讲定义", "user"),))

    def redact_session_evidence(self, conn, user_id, session_id, *, request_id):
        conn.execute("select evowiki_forget_note(%s, 'preferences.fake', 0)", (request_id,))
        self.redacted.append(session_id)
        return {}

    def revoking(self, user_id, rule_ids, fact_keys, body, *, now):
        with self.pool.connection() as conn, conn.transaction():
            body(conn)
        self.revoked.append((tuple(rule_ids), tuple(fact_keys)))


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


@dataclass
class World:
    pool: Any
    sessions: PgSessionStore
    mem: PgMemoryStore
    ledger: Ledger
    harness: FakeHarness
    spec: FakeSpec
    service: ForgetService
    seq: Any = field(default_factory=lambda: count(1))

    def entries(self, sid: str) -> list[dict]:
        with self.pool.connection() as conn:
            return conn.execute("select entry_id, type, payload from assistant_entry "
                                "where session_id = %s order by seq", (sid,)).fetchall()

    def turn_rows(self, sid: str, mid: str) -> list[dict]:
        """The user entry and answer of turn `mid` (the answer right after it)."""
        rows = self.entries(sid)
        i = next(i for i, r in enumerate(rows)
                 if r["type"] == "user" and r["payload"]["message_id"] == mid)
        ans = next(r for r in rows[i + 1:] if r["type"] == "assistant")
        return [rows[i], ans]

    def count(self, sql: str, *args) -> int:
        with self.pool.connection() as conn:
            return conn.execute(f"select count(*) as n from ({sql}) x", args).fetchone()["n"]


def _turn(w: World, sid: str, mid: str, i: int, *, recalled=(), tools=()) -> None:
    at = T0 + timedelta(minutes=10 * ord(sid) + i)
    s = w.sessions
    s.append(sid, f"{sid}-e{next(w.seq)}", "user",
             {"message_id": mid, "text": WORDS[mid], "selection": None}, at)
    for name, args, data in tools:
        s.append(sid, f"{sid}-e{next(w.seq)}", "tool",
                 {"message_id": mid, "call_id": f"call-{mid}-{name}", "name": name,
                  "effect": "read", "args": args, "ok": True, "summary": "查记忆",
                  "content": f"工具读到的内容 {mid}", "sources": [], **data}, at)
    answer = f"{mid}-ans"
    s.append(sid, f"{sid}-e{next(w.seq)}", "assistant",
             {"message_id": answer, "text": f"回答 {mid}：{WORDS[mid]}", "sources": [],
              "recalled": [{"id": r, "kind": "episode", "content_hash": "h"} for r in recalled],
              "retrieval_version": "memory-recall@v1"}, at)
    w.mem.upsert_episode(Episode(
        user_id="u1", session_id=sid, message_id=mid, answer_message_id=answer,
        trace_run_id=f"assistant:{sid}:{mid}", turn_index=i, page_id="p1", at=at,
        user_text=WORDS[mid], answer_text=f"回答 {mid}"))
    with w.pool.connection() as conn:
        _trace(conn, f"assistant:{sid}:{mid}", f"call-{sid}-{mid}", sid, at)


def _trace(conn, run_id: str | None, call_id: str, subject: str, at: datetime) -> None:
    h = f"text-{call_id}"
    conn.execute("insert into llm_text (hash, text) values (%s, %s)", (h, f"提示 {call_id}"))
    conn.execute("insert into llm_call (id, input_hash, purpose, prompt_version, subject, "
                 "provider, model, params, raw_output, latency, ts) values (%s, %s, 'assistant', "
                 "'v', %s, 'x', 'm', '{}', 'out', 0, %s)", (call_id, f"in-{call_id}", subject, at))
    conn.execute("insert into llm_attempt (id, input_hash, purpose, prompt_version, subject, "
                 "provider, model, params, status, latency, ts) values (%s, %s, 'assistant', "
                 "'v', %s, 'x', 'm', '{}', 'accepted', 0, %s)",
                 (f"att-{call_id}", f"in-{call_id}", subject, at))
    conn.execute("insert into llm_request (call_id, system_hash, prompt_hash, schema_name) "
                 "values (%s, %s, %s, 's')", (call_id, h, h))
    if run_id is None:
        return
    conn.execute("insert into agent_run (run_id, user_id, agent, subject, pack_version, input, "
                 "started_at) values (%s, 'u1', 'assistant', %s, 0, '{}', %s)",
                 (run_id, subject, at))
    conn.execute("insert into agent_step (run_id, seq, kind, payload, call_id, ts) values "
                 "(%s, 1, 'model', %s, %s, %s)", (run_id, _j({"system_hash": h}), call_id, at))


def _item(w: World, iid: str, sid: str, mid: str) -> None:
    w.mem.put_item(Item(
        item_id=iid, user_id="u1", kind="decision", content=f"决定 {iid}",
        provenance="explicit_history",
        evidence=(EvidenceRef(message_id=mid, session_id=sid, role="user", quote=WORDS[mid]),),
        scope_type="global", session_id=sid, valid_from=T0, created_at=T0, status="active"))


def build(pool, tmp_path, *, harness: FakeHarness | None = None,
          spec: FakeSpec | None = None) -> World:
    for sid in "ABCD":
        seed_user_and_session(pool, "u1", sid)
    seed_user_and_session(pool, "u2", "E")
    ledger = Ledger(tmp_path / "forget-ledger.jsonl")
    harness = harness or FakeHarness()
    spec = spec or FakeSpec(pool)
    ids = count(1)
    service = ForgetService(pool, ledger, harness=harness, spec=spec,
                            clock=lambda: T0 + timedelta(days=1),
                            new_id=lambda p: f"{p}_{next(ids)}")
    w = World(pool, PgSessionStore(pool), PgMemoryStore(pool), ledger, harness, spec, service)
    _turn(w, "A", "a1", 0)
    _turn(w, "A", "a2", 1)
    _turn(w, "B", "b1", 0)
    _turn(w, "B", "b2", 1, recalled=["ep:A/a1"])
    _turn(w, "B", "b3", 2)
    w.sessions.append("B", "B-compaction", "compaction",
                      {"version": "compact@v7", "handoff": {"summary": "他决定用 pandas"},
                       "learning": {"understood": [{"concept": "pandas 读表",
                                                    "evidence_id": "b2"}]},
                       "edits": ["加了小节「pandas」"], "first_kept": "b3"},
                      T0 + timedelta(hours=3))
    _turn(w, "B", "b4", 3, recalled=["ep:C/c2"])
    _turn(w, "C", "c1", 0, recalled=["ep:A/a2"])
    _turn(w, "C", "c2", 1, recalled=["ep:B/b4"],
          tools=[("search_memory", {"query": "数据处理"},
                  {"memory_ids": ["ep:B/b2"], "sources_sessions": ["B"]})])
    _turn(w, "C", "c3", 2, tools=[("read_past_turn", {"session_id": "B", "message_id": "b1"},
                                   {"sources_sessions": ["B"]})])
    _turn(w, "D", "d1", 0)
    _item(w, "item_a", "A", "a1")
    _item(w, "item_b2", "B", "b2")
    _item(w, "item_b1", "B", "b1")
    with pool.connection() as conn:            # a session-level call of A (its consolidation)
        _trace(conn, None, "call-cons-A", "A", T0)
    return w
