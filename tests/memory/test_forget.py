"""Deleting a session / a memory item (plan Task 13): the state machine, the taint closure, what is
hidden while a request is open, the integrity check, single-item suppression. Postgres; RSI's and
S3's steps are fakes (tests/memory/forget_world.py); no model call."""

import json
from datetime import timedelta

import pytest

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.memory import forget as forget_mod
from evowiki.memory.forget import (
    PLACEHOLDER,
    TOOL_PLACEHOLDER,
    ForgetIntegrityError,
    ForgetService,
    ForgetUnavailable,
    taint_closure,
)
from evowiki.memory.search import search
from evowiki.memory.tombstone import Ledger
from evowiki.memory.turns import read_turn, verify_quote
from tests.memory.forget_world import CLEAN, T0, TAINTED, WORDS, FakeSpec, _item, build


@pytest.fixture
def w(pool, tmp_path):
    return build(pool, tmp_path)


def _requests(w, **where):
    sql = "select * from forget_request" + (
        " where " + " and ".join(f"{k} = %s" for k in where) if where else "")
    with w.pool.connection() as conn:
        return conn.execute(sql, tuple(where.values())).fetchall()


# ---------- the closure ----------

def test_closure_follows_branches_and_a_cycle_once_each(w):
    with w.pool.connection() as conn:
        t = taint_closure(conn, "u1", "A")
    assert t.turns == frozenset(TAINTED)
    assert t.sessions == {"B", "C"}
    assert t.items == {"item_a", "item_b2"}
    assert {"A/a1", "A/a2", "B/b2", "B/b4", "C/c1", "C/c2"} == set(t.episodes)
    # every node once, each with the node that tainted it (the cycle b4 <-> c2 ends)
    assert len(t.paths) == len(set(t.paths))
    assert t.paths["turn:B/b2"] == "ep:A/a1"
    assert t.paths["turn:C/c2"] in ("ep:B/b2", "ep:B/b4")
    assert t.paths["turn:B/b4"] == "ep:C/c2"
    assert t.paths["item:item_b2"] == "turn:B/b2"
    assert set(t.quotes) == {WORDS[m] for m in ("a1", "a2", "b2", "b4", "c1", "c2")}


def test_closure_checkpoint_survives_restart_and_delete_resumes(w):
    from evowiki.memory.forget import closure_batch

    rid = w.service.request_session("u1", "A", requested_by="test")
    with w.pool.connection() as conn, conn.transaction():
        assert not closure_batch(conn, rid, batch_size=1)
    with w.pool.connection() as conn:
        done = conn.execute("select count(*) as n from forget_frontier "
                            "where request_id=%s and expanded", (rid,)).fetchone()["n"]
    assert done == 1
    assert w.service.run(rid).state == "committed"
    assert w.count("select 1 from assistant_session where session_id='A'") == 0


# ---------- the whole delete ----------

def test_forget_session_deletes_it_and_propagates_twice(w):
    before = {k: w.turn_rows(*k) for k in CLEAN}
    out = w.service.forget_session("u1", "A", revoke_rule_ids=["si1"], requested_by="test")
    assert out.state == "committed"

    # A is gone from every table
    for sql in ("select 1 from assistant_session where session_id = 'A'",
                "select 1 from assistant_entry where session_id = 'A'",
                "select 1 from memory_episode where session_id = 'A'",
                "select 1 from memory_item where session_id = 'A'",
                "select 1 from agent_run where starts_with(run_id, 'assistant:A:')",
                "select 1 from llm_call where subject = 'A' or starts_with(id, 'call-A-')",
                "select 1 from llm_attempt where subject = 'A'",
                ("select 1 from llm_request where starts_with(call_id, 'call-A-') "
                 "or call_id = 'call-cons-A'")):
        assert w.count(sql) == 0, sql
    assert w.count("select 1 from forgotten_session where session_id = 'A'") == 1

    # B and C: tainted answers are placeholders, their words stay; episodes / items / traces gone
    for sid, mid in TAINTED:
        user, ans = w.turn_rows(sid, mid)
        assert user["payload"]["text"] == WORDS[mid]
        assert ans["payload"]["text"] == PLACEHOLDER
        assert all(r.get("deleted") for r in ans["payload"]["recalled"]
                   if r["id"].startswith(("ep:A/", "ep:B/b2", "ep:B/b4", "ep:C/c")))
        assert w.count("select 1 from memory_episode where session_id = %s and message_id = %s",
                       sid, mid) == 0
        assert w.count("select 1 from agent_run where run_id = %s", f"assistant:{sid}:{mid}") == 0
        assert w.count("select 1 from llm_call where id = %s", f"call-{sid}-{mid}") == 0
    tool = next(r for r in w.entries("C") if r["type"] == "tool"
                and r["payload"]["message_id"] == "c2")
    assert tool["payload"]["content"] == TOOL_PLACEHOLDER and tool["payload"]["args"] == {}
    assert w.count("select 1 from memory_item where item_id = 'item_b2'") == 0

    # untainted turns: not a byte changed, their memory and traces kept
    for k in CLEAN:
        assert w.turn_rows(*k) == before[k]
        assert w.count("select 1 from memory_episode where session_id = %s and message_id = %s",
                       *k) == 1
        assert w.count("select 1 from agent_run where run_id = %s", f"assistant:{k[0]}:{k[1]}") == 1
    assert w.count("select 1 from memory_item where item_id = 'item_b1'") == 1

    # B's compaction that folded b2 is void, and a new one keeps b2 on
    comps = [r["payload"] for r in w.entries("B") if r["type"] == "compaction"]
    assert comps[0] == {"summary": "", "first_kept": "b3"}    # every summary part is gone
    assert comps[-1] == {"summary": "", "first_kept": "b2"}
    state = PgSessionStore(w.pool)
    from evowiki.assistant.session import fold
    folded = fold(state.info("B"), state.entries("B"))
    assert folded.summary == "" and folded.turns[folded.first_kept].message_id == "b2"

    # the request, its audit (with the taint paths), the ledger, the ports
    [req] = _requests(w, session_id="A")
    assert req["state"] == "committed"
    with w.pool.connection() as conn:
        steps = [r["step"] for r in conn.execute(
            "select step from forget_audit where request_id = %s order by id",
            (req["request_id"],)).fetchall()]
    assert steps[0] == "request" and steps[1] == "open" and steps[-1] == "close"
    assert "closure checkpoint nodes" in steps
    with w.pool.connection() as conn:
        assert conn.execute("select cause from forget_frontier where request_id=%s "
                            "and node='turn:B/b2'", (req['request_id'],)).fetchone()['cause'] == 'ep:A/a1'
    assert any(s.startswith("delete:assistant_entry") for s in steps)
    assert w.ledger.requests()[req["request_id"]].state == "committed"
    assert w.ledger.requests()[req["request_id"]].revoke_rule_ids == ("si1",)
    assert w.spec.revoked == [(("si1",), ())] and w.spec.redacted == ["A"]
    [(sid, quotes)] = w.harness.calls
    assert sid == "A" and WORDS["a1"] in quotes and WORDS["c2"] in quotes


def test_a_session_with_a_stopped_turn_can_be_deleted(w):
    PgSessionStore(w.pool).request_stop("A", "m-stopped", T0)
    out = w.service.forget_session("u1", "A", revoke_rule_ids=["si1"], requested_by="test")
    assert out.state == "committed"
    assert w.count("select 1 from assistant_session where session_id = 'A'") == 0
    assert w.count("select 1 from assistant_turn_stop where session_id = 'A'") == 0


def test_untainted_entry_changed_inside_the_transaction_fails_closed(w):
    def touch(conn):          # a step that (wrongly) rewrites an untainted turn of B
        conn.execute("update assistant_entry set payload = payload || '{\"x\": 1}' "
                     "where session_id = 'B' and type = 'user' "
                     "and payload ->> 'message_id' = 'b1'")
    w.harness.also = touch
    out = w.service.forget_session("u1", "A", requested_by="test")
    assert out.state == "failed" and "ForgetIntegrityError" in out.error
    assert w.count("select 1 from assistant_entry where session_id = 'A'") == 4
    assert "x" not in w.turn_rows("B", "b1")[0]["payload"]
    with pytest.raises(ForgetIntegrityError):
        raise ForgetIntegrityError("type is importable")


# ---------- hidden from the moment of the request ----------

def test_hidden_as_soon_as_requested_and_after_a_failure(w):
    w.harness.fail = RuntimeError("rsi down")
    rid = w.service.request_session("u1", "A", requested_by="test")
    assert _requests(w, request_id=rid)[0]["state"] == "requested"

    def visible():
        sessions = PgSessionStore(w.pool)
        eps = {(e.session_id, e.message_id) for e in w.mem.episodes("u1")}
        items = {i.item_id for i in w.mem.items("u1")}
        listed = {i.session_id for i in sessions.user_sessions("u1")}
        hits = {h.session_id + "/" + h.message_ids[0] for h in search(
            w.mem, None, "u1", "pandas 处理数据", mode="history", k=10) if h.message_ids}
        return eps, items, listed, hits, sessions

    for _ in range(2):            # right after the request, and again after a failed run
        eps, items, listed, hits, sessions = visible()
        assert not eps & {("A", "a1"), ("A", "a2"), *TAINTED}
        assert eps == set()  # uncertain derived reads pause until the durable closure finishes
        assert items == set()
        assert "A" not in listed and {"B", "C", "D"} <= listed
        assert not any(h.startswith("A/") for h in hits)
        with pytest.raises(LookupError):
            sessions.info("A")
        assert sessions.entries("A") == []
        b2 = read_turn(sessions, "u1", "B", "b2")
        assert b2.user_text == WORDS["b2"] and b2.answer_text == PLACEHOLDER
        assert read_turn(sessions, "u1", "A", "a1") is None
        assert not verify_quote(sessions, "u1", "A", "a1", WORDS["a1"])
        assert sessions.recent_sessions("u1", limit=10) and all(
            s.session_id != "A" for s in sessions.recent_sessions("u1", limit=10))
        assert w.service.run(rid).state == "failed"
    assert _requests(w, request_id=rid)[0]["state"] == "failed"
    assert w.count("select 1 from assistant_entry where session_id = 'A'") == 4


def test_indexing_skips_a_hidden_turn(w):
    from evowiki.memory.store import Episode
    w.service.request_session("u1", "A", requested_by="test")
    with w.pool.connection() as conn:
        conn.execute("delete from memory_episode where session_id = 'B' and message_id = 'b2'")
    w.mem.upsert_episode(Episode(user_id="u1", session_id="B", message_id="b2",
                                 answer_message_id="x", trace_run_id="t", turn_index=1,
                                 page_id=None, at=T0, user_text="u", answer_text="a"))
    assert w.count("select 1 from memory_episode where session_id = 'B' "
                   "and message_id = 'b2'") == 0


# ---------- refusals ----------

def test_another_users_session_is_404_and_no_request(w):
    with pytest.raises(LookupError):
        w.service.forget_session("u2", "A", requested_by="test")
    with pytest.raises(LookupError):
        w.service.preview("u2", "A")
    assert _requests(w) == []


def test_missing_dependency_is_refused_before_any_request(pool, tmp_path):
    w = build(pool, tmp_path)
    for harness, spec in ((None, FakeSpec(pool)), (w.harness, None)):
        svc = ForgetService(pool, Ledger(tmp_path / "l2.jsonl"), harness=harness, spec=spec,
                            clock=lambda: T0, new_id=lambda p: p)
        assert not svc.available
        with pytest.raises(ForgetUnavailable):
            svc.forget_session("u1", "A", requested_by="test")
        with pytest.raises(ForgetUnavailable):
            svc.preview("u1", "A")
    assert _requests(w) == []


def test_preview_lists_rules_tainted_sessions_and_counts(w):
    p = w.service.preview("u1", "A")
    assert [r.item_id for r in p.rules] == ["si1"]
    assert {(t.session_id, t.turns) for t in p.tainted_sessions} == {("B", 2), ("C", 2)}
    assert p.tainted_sessions[0].title == WORDS["b1"]
    assert p.memory_counts == {"episodes": 2, "items": 2, "tainted_turns": 4}
    assert "继续生效" in p.note


# ---------- one item ----------

def test_forget_item_suppresses_without_text(w):
    assert w.service.forget_item("u1", "item_b1", requested_by="test") == "committed"
    assert w.mem.get_item("u1", "item_b1") is None
    with w.pool.connection() as conn:
        row = conn.execute("select * from memory_suppression where item_id = 'item_b1'"
                           ).fetchone()
        [req] = conn.execute("select * from forget_request where item_id = 'item_b1'"
                             ).fetchall()
        steps = [r["step"] for r in conn.execute(
            "select step from forget_audit where request_id = %s order by id",
            (req["request_id"],)).fetchall()]
    assert row["evidence_message_ids"] == ["b1"] and row["kind"] == "decision"
    assert "决定" not in json.dumps(row, default=str)            # no text stored
    assert req["state"] == "committed" and req["session_id"] is None
    assert steps[:5] == ["request", "open", "memory_item", "memory_conflict",
                         "memory_suppression"]
    assert steps[-1] == "close"
    assert [s.item_id for s in w.mem.suppressions("u1")] == ["item_b1"]
    assert w.ledger.requests()[req["request_id"]].state == "committed"
    assert w.service.forget_item("u1", "item_b1", requested_by="test") == "not_found"
    assert w.service.forget_item("u2", "item_b2", requested_by="test") == "not_found"


def test_forgotten_item_does_not_come_back_from_consolidation(w, tmp_path):
    from evowiki.assistant.session import fold
    from evowiki.memory.consolidate import consolidate
    from evowiki.memory.lander import NullPreferenceLander, NullRuleGuard
    from tests.memory.test_consolidate import _gateway, cand, ev

    assert w.service.forget_item("u1", "item_b1", requested_by="test") == "committed"
    sessions = PgSessionStore(w.pool)
    state = fold(sessions.info("B"), sessions.entries("B"))
    r = consolidate(state, sessions=sessions, store=w.mem,
                    gateway=_gateway(tmp_path, [cand("decision", "决定先复习均值",
                                                     ev("b1", quote=WORDS["b1"]))]),
                    account=None, lander=NullPreferenceLander(), guard=NullRuleGuard(),
                    first=0, through=len(state.turns), now=T0 + timedelta(days=2),
                    new_id=lambda p: f"{p}_new")
    assert not r.items_written
    assert w.turn_rows("B", "b1")[0]["payload"]["text"] == WORDS["b1"]


# ---------- the ledger rows and forged tools a session reached ----------

def _action(conn, aid: str, run_id: str, text: str) -> None:
    content = {"page_id": "p1", "section_id": "sec1", "text": text,
               "session_id": run_id.split(":")[1]}
    conn.execute("insert into agent_action (action_id, user_id, kind, scope, level, status, "
                 "source, summary, why, content, content_hash, idempotency_key, run_id, "
                 "created_at) values (%s, 'u1', 'assistant_edit', 'page_body', 'ask', "
                 "'executed', 'assistant', %s, %s, %s, 'h', %s, %s, %s)",
                 (aid, f"改写：{text}", f"因为{text}", json.dumps(content, ensure_ascii=False),
                  f"key-{aid}", run_id, T0))


def _candidate(conn, cid: str, *, origin: str = "round", cites: str = "signal:x") -> None:
    conn.execute("insert into forge_candidate (id, user_id, kind, name, spec, evidence, status, "
                 "run_id, created_at, updated_at, origin) values (%s, 'u1', 'tool', %s, %s, %s, "
                 "'built', %s, %s, %s, %s)",
                 (cid, f"tool_{cid}", json.dumps({"name": f"tool_{cid}", "purpose": "按 pandas 分组"},
                                                ensure_ascii=False),
                  json.dumps({"citations": [cites], "note": "他要分组"}), f"forge:{cid}", T0,
                  T0, origin))
    conn.execute("insert into forge_tool_vec (candidate_id, embedder, model_version, dim, vec) "
                 "values (%s, 'e', 'v', 2, '{0.1,0.2}')", (cid,))
    conn.execute("insert into agent_run (run_id, user_id, agent, subject, pack_version, input, "
                 "started_at) values (%s, 'u1', 'forge', %s, 0, '{}', %s)",
                 (f"forge:{cid}", cid, T0))


def test_tainted_turns_ledger_rows_are_redacted_and_kept(w):
    with w.pool.connection() as conn:
        _action(conn, "act_b2", "assistant:B:b2", "用 pandas 处理")
        _action(conn, "act_b1", "assistant:B:b1", "均值的段落")
        _action(conn, "act_a1", "assistant:A:a1", "A 里的改写")
    assert w.service.forget_session("u1", "A", requested_by="test").state == "committed"
    with w.pool.connection() as conn:
        rows = {r["action_id"]: r for r in conn.execute(
            "select * from agent_action where user_id = 'u1'").fetchall()}
    assert "act_a1" not in rows                         # the deleted session's own row is gone
    b2 = rows["act_b2"]
    assert b2["status"] == "executed" and b2["run_id"] == "assistant:B:b2"
    assert b2["summary"] == b2["why"] == b2["content"]["text"] == forget_mod.ACTION_REDACTED
    assert (b2["content"]["page_id"], b2["content"]["section_id"]) == ("p1", "sec1")
    assert b2["content_hash"] != "h"
    b1 = rows["act_b1"]
    assert b1["content"]["text"] == "均值的段落" and b1["why"] == "因为均值的段落"


def test_forged_tools_of_the_session_and_its_tainted_turns_are_deleted(w):
    with w.pool.connection() as conn:
        _candidate(conn, "fc_a", origin="session:A")
        _candidate(conn, "fc_a2", cites="trace:assistant:A:a2")
        _candidate(conn, "fc_c1", origin="session:C", cites="trace:assistant:C:c1")
        _candidate(conn, "fc_d1", origin="session:D", cites="trace:assistant:D:d1")
    assert w.service.forget_session("u1", "A", requested_by="test").state == "committed"
    with w.pool.connection() as conn:
        left = {r["id"] for r in conn.execute("select id from forge_candidate").fetchall()}
    assert left == {"fc_d1"}
    assert w.count("select 1 from forge_tool_vec") == 1
    assert w.count("select 1 from agent_run where starts_with(run_id, 'forge:')") == 1


def test_a_promoted_forged_tool_stays_with_its_sources_redacted(w):
    with w.pool.connection() as conn:
        _candidate(conn, "fc_a", origin="session:A", cites="trace:assistant:A:a1")
        _candidate(conn, "fc_a_new", origin="session:A")              # not promoted
        conn.execute("insert into forge_registry (scope, version, entries, change, created_at) "
                     "values ('u1', 1, '{\"tool_fc_a\": \"fc_a\"}', '{\"op\": \"promote\"}', "
                     "%s)", (T0,))
    assert w.service.forget_session("u1", "A", requested_by="test").state == "committed"
    with w.pool.connection() as conn:
        rows = {r["id"]: r for r in conn.execute("select * from forge_candidate").fetchall()}
    assert set(rows) == {"fc_a"}                                      # the unpromoted one went
    kept = rows["fc_a"]
    assert kept["origin"] == forget_mod.SOURCE_DELETED and kept["status"] == "built"
    assert kept["evidence"] == {"citations": [forget_mod.CITE_DELETED],
                                "note": forget_mod.ACTION_REDACTED}
    assert kept["spec"] == {"name": "tool_fc_a", "purpose": forget_mod.ACTION_REDACTED}
    text = json.dumps([kept["origin"], kept["spec"], kept["evidence"]], ensure_ascii=False)
    assert "assistant:A" not in text and "session:A" not in text and "他要分组" not in text and "pandas" not in text
    assert w.count("select 1 from forge_tool_vec where candidate_id = 'fc_a'") == 1
    assert w.count("select 1 from agent_run where starts_with(run_id, 'forge:')") == 0
    assert w.count("select 1 from forge_registry") == 1


def test_a_kept_tools_examples_lose_only_his_words(w):
    with w.pool.connection() as conn:
        _candidate(conn, "fc_a", origin="session:A")
        conn.execute(
            "update forge_candidate set spec = spec || %s where id = 'fc_a'",
            (json.dumps({"examples": [
                {"input": {"text": f"他说：{WORDS['a1']}。请分组"}, "output": "按列分组的结果"},
                {"input": {"text": "完全无关的例子"}, "output": "原样"}],
                "holdout": [{"input": {"text": WORDS["c1"]}, "output": "x"}]},
                ensure_ascii=False),))
        conn.execute("insert into forge_registry (scope, version, entries, change, created_at) "
                     "values ('u1', 1, '{\"tool_fc_a\": \"fc_a\"}', '{}', %s)", (T0,))
    assert w.service.forget_session("u1", "A", requested_by="test").state == "committed"
    with w.pool.connection() as conn:
        spec = conn.execute("select spec, files, status from forge_candidate where id = 'fc_a'"
                            ).fetchone()
    ex = spec["spec"]["examples"]
    assert ex[0] == {"input": {"text": "他说：（已删除的原话）。请分组"}, "output": "按列分组的结果"}
    assert ex[1] == {"input": {"text": "完全无关的例子"}, "output": "原样"}
    assert spec["spec"]["holdout"][0]["input"]["text"] == "（已删除的原话）"
    assert spec["spec"]["examples_redacted"] is True and spec["status"] == "built"
    assert WORDS["a1"] not in json.dumps(spec["spec"], ensure_ascii=False)
    assert spec["spec"]["name"] == "tool_fc_a"


# ---------- the materials the session used ----------

def _source(conn, sid: str, *uses: tuple[str | None, str | None]) -> None:
    """A live material with its file; `uses`: (session_id, page_id) of each source_use row."""
    conn.execute("insert into source (source_id, user_id, kind, title, sha256, bytes, status, "
                 "created_at, updated_at) values (%s, 'u1', 'md', %s, 'sha', 3, 'ready', %s, %s)",
                 (sid, f"资料 {sid}", T0, T0))
    conn.execute("insert into source_file (source_id, mime, data) values (%s, 'text/markdown', "
                 "'abc')", (sid,))
    for i, (session, page) in enumerate(uses):
        conn.execute("insert into source_use (use_id, source_id, user_id, use, page_id, "
                     "session_id, created_at) values (%s, %s, 'u1', 'ask', %s, %s, %s)",
                     (f"use-{sid}-{i}", sid, page, session, T0))


@pytest.fixture
def materials(w):
    with w.pool.connection() as conn:
        _source(conn, "src_only", ("A", None))
        _source(conn, "src_shared", ("A", None), ("C", None), (None, "p1"))
        _source(conn, "src_linked", ("A", None))
        conn.execute("insert into source_section (section_id, source_id, ord, title, text, "
                     "tokens, read_by) values ('sec_l', 'src_linked', 0, 't', 'x', 1, 'text')")
        conn.execute("insert into source_link (user_id, node_id, section_id, role, by, "
                     "created_at) values ('u1', 'n1', 'sec_l', 'teaches', 'user', %s)", (T0,))
        _source(conn, "src_other", ("C", None))
    return w


def test_preview_lists_the_sessions_materials_and_where_else_they_are_used(materials):
    got = {m.source_id: m for m in materials.service.preview("u1", "A").materials}
    assert set(got) == {"src_only", "src_shared", "src_linked"}
    assert not got["src_only"].in_use_elsewhere and got["src_only"].title == "资料 src_only"
    assert got["src_shared"].in_use_elsewhere
    assert got["src_shared"].used_by == ("session:C", "page:p1")
    assert got["src_linked"].used_by == ("node:n1",)


def test_ticked_materials_are_deleted_the_rest_only_lose_the_session(materials):
    from evowiki.sources.store import session_attachments
    w = materials
    out = w.service.forget_session("u1", "A", delete_source_ids=["src_only"],
                                   requested_by="test")
    assert out.state == "committed"
    with w.pool.connection() as conn:
        live = {r["source_id"]: r["deleted_at"] for r in conn.execute(
            "select source_id, deleted_at from source").fetchall()}
        assert live["src_only"] is not None                       # the library's soft delete
        assert {k for k, v in live.items() if v is None} == {"src_shared", "src_linked",
                                                             "src_other"}
        assert set(session_attachments(conn, "u1", "C")) == {"src_shared", "src_other"}
    assert w.count("select 1 from source_file where source_id = 'src_only'") == 0
    assert w.count("select 1 from source_file where source_id = 'src_shared'") == 1
    assert w.count("select 1 from source_use where session_id = 'A'") == 0
    assert w.count("select 1 from source_use where source_id = 'src_shared'") == 2
    assert w.count("select 1 from source_link where node_id = 'n1'") == 1
    assert w.ledger.requests()[out.request_id].delete_source_ids == ("src_only",)


def test_a_material_of_another_session_cannot_be_ticked(materials):
    with pytest.raises(LookupError):
        materials.service.forget_session("u1", "A", delete_source_ids=["src_other"],
                                         requested_by="test")
    assert _requests(materials) == []


def test_a_failed_delete_keeps_the_materials(materials):
    w = materials
    w.harness.fail = RuntimeError("rsi down")
    out = w.service.forget_session("u1", "A", delete_source_ids=["src_only"],
                                   requested_by="test")
    assert out.state == "failed"
    assert w.count("select 1 from source where source_id = 'src_only' "
                   "and deleted_at is null") == 1
    assert w.count("select 1 from source_use where session_id = 'A'") == 3
    w.harness.fail = None
    assert w.service.run(out.request_id).state == "committed"     # the retry keeps the choice
    assert w.count("select 1 from source where source_id = 'src_only' "
                   "and deleted_at is null") == 0


def test_runs_nested_in_a_turn_go_with_it(w):
    """A step run opened inside a turn's run (agent.trace traced_step: `input.parent_run`), and a
    step nested in that one, go with the session / tainted turn; another turn's stays."""
    with w.pool.connection() as conn:
        for run, parent in (("step:recall:1", "assistant:A:a1"), ("step:judge:1", "step:recall:1"),
                            ("step:recall:2", "assistant:B:b2"), ("step:recall:3", "assistant:B:b1"),
                            ("step:consolidate:9", None)):
            # the last: a call of A outside any run, its own auto run (subject = the session)
            conn.execute("insert into agent_run (run_id, user_id, agent, subject, pack_version, "
                         "input, started_at) values (%s, 'u1', 'step', %s, 0, %s, %s)",
                         (run, "A" if parent is None else "x",
                          json.dumps({"auto": True} if parent is None else {"parent_run": parent}),
                          T0))
            conn.execute("insert into llm_call (id, input_hash, purpose, prompt_version, subject, "
                         "provider, model, params, raw_output, latency, ts) values (%s, %s, 'p', "
                         "'v', 'not-the-session', 'x', 'm', '{}', 'out', 0, %s)",
                         (f"call-{run}", f"in-{run}", T0))
            conn.execute("insert into agent_step (run_id, seq, kind, payload, call_id, ts) values "
                         "(%s, 1, 'model', '{}', %s, %s)", (run, f"call-{run}", T0))
        assert set(forget_mod._session_runs(conn, "u1", "A")) == {
            "assistant:A:a1", "assistant:A:a2", "step:recall:1", "step:judge:1",
            "step:consolidate:9"}
    assert w.service.forget_session("u1", "A", requested_by="test").state == "committed"
    with w.pool.connection() as conn:
        left = {r["run_id"] for r in conn.execute(
            "select run_id from agent_run where starts_with(run_id, 'step:')")}
        calls = {r["id"] for r in conn.execute(
            "select id from llm_call where starts_with(id, 'call-step:')")}
        assert forget_mod.residue(conn, "u1", "A") == []
    assert left == {"step:recall:3"} and calls == {"call-step:recall:3"}


def test_a_fact_card_from_the_session_goes_with_it(w):
    _item(w, "mem_w", "A", "a1")
    with w.pool.connection() as conn:
        conn.execute("insert into agent_action (action_id, user_id, kind, scope, level, status, "
                     "source, summary, why, content, content_hash, idempotency_key, created_at) "
                     "values ('act_w', 'u1', 'profile_fact', 'suggestion', 'ask', 'proposed', "
                     "'assistant', '记进档案：课题代号', '', '{}', 'h', 'key-w', %s)", (T0,))
        conn.execute("update memory_item set pending_action_id = 'act_w' where item_id = 'mem_w'")
    assert w.service.forget_session("u1", "A", requested_by="test").state == "committed"
    assert w.count("select 1 from agent_action where action_id = 'act_w'") == 0
