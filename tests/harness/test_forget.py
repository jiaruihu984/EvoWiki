"""`harness.forget.forget_session` inside the 0082 forget gate: what one session leaves in the
harness-RSI records is deleted or redacted, nothing of another session or round moves, and a second
run (the memory side's reconcile) changes nothing. `pool` connects as evowiki_app, as the service."""

import json
from datetime import UTC, datetime

import psycopg
import pytest

from evowiki.harness.forget import DELETED, REDACTED, forget_session

T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
TURN = "turn:assistant:s1:m1"
OTHER = "turn:assistant:s2:m9"


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _round(conn, rid, turn, words):
    cases = {"problem": [{"case_id": turn, "run_id": turn[5:], "kind": "assistant",
                          "signal_ids": [f"arej:{rid}"]},
                         {"case_id": "case:run-1:s2", "run_id": "run-1", "kind": "deep",
                          "signal_ids": ["sig-deep"]}],
             "anchors": [], "unusable": [], "finding": "f", "agent": "mixed"}
    conn.execute(
        "insert into harness_round (round_id, user_id, started_at, finished_at, status, reason, "
        "finding, signal_ids, cases, candidates, comparisons, decision, diff, cost) values "
        "(%s, 'u1', %s, %s, 'applied', %s, 'f', %s, %s, %s, %s, %s, %s, 0.5)",
        (rid, T0, T0, f"已改：{words}", _j([f"arej:{rid}", "sig-deep"]), _j(cases),
         _j([{"kind": "general", "rationale": f"他说「{words}」"}]),
         _j([{"cmp_id": f"cmp-{rid}", "case_id": turn, "quote": words, "reason": words,
              "winner": "a", "final": False}]),
         _j({"improver": "a", "rationale": f"因为「{words}」", "gate": {"verdict": "pass"}}),
         _j({"ops": [{"name": "H1.assistant", "value": "x", "before": None}]})))
    conn.execute("insert into harness_change (change_id, user_id, round_id, diff, status, reason, "
                 "created_at) values (%s, 'u1', %s, %s, 'applied', %s, %s)",
                 (f"c-{rid}", rid, _j({"ops": []}), f"因为「{words}」", T0))
    conn.execute("insert into harness_experience (exp_id, user_id, situation, change, evidence, "
                 "verdict, scope, created_at, weight) values (%s, 'u1', %s, '{}', %s, "
                 "'harmful', 'user', %s, 1)",
                 (f"x-{rid}", _j({"agent": "assistant"}), _j({"round_id": rid, "by": "user"}), T0))


def _call(conn, cid, subject, text_hash):
    conn.execute("insert into llm_text (hash, text) values (%s, %s) on conflict do nothing",
                 (text_hash, f"原文 {cid}"))
    conn.execute("insert into llm_call (id, input_hash, purpose, prompt_version, subject, provider, "
                 "model, params, raw_output, latency, ts) values (%s, %s, 'p', 'v', %s, 'x', 'm', "
                 "'{}', 'out', 0, %s)", (cid, f"h-{cid}", subject, T0))
    conn.execute("insert into llm_attempt (id, input_hash, purpose, prompt_version, subject, "
                 "provider, model, params, status, latency, ts) values (%s, %s, 'p', 'v', %s, 'x', "
                 "'m', '{}', 'accepted', 0, %s)", (f"a-{cid}", f"h-{cid}", subject, T0))
    conn.execute("insert into llm_request (call_id, system_hash, prompt_hash, schema_name) "
                 "values (%s, %s, %s, 's')", (cid, text_hash, text_hash))


def _run(conn, run_id, call_id, mention=""):
    conn.execute("insert into agent_run (run_id, user_id, agent, subject, pack_version, input, "
                 "started_at) values (%s, 'u1', 'a', 's', 0, '{}', %s)", (run_id, T0))
    conn.execute("insert into agent_step (run_id, seq, kind, payload, call_id, ts) values "
                 "(%s, 1, 'model', %s, %s, %s)",
                 (run_id, _j({"system_hash": f"t-{call_id}", "note": mention}), call_id, T0))


@pytest.fixture
def world(pool):
    with pool.connection() as conn:
        for uid in ("u1", "u2"):
            conn.execute("insert into app_user (user_id, background, condition, lang, created_at) "
                         "values (%s, 'none', 'S2', 'en', %s)", (uid, T0))
        for sid in ("s1", "s2"):
            conn.execute("insert into assistant_session (session_id, user_id, created_at) "
                         "values (%s, 'u1', %s)", (sid, T0))
        _round(conn, "r1", TURN, "别再用表格了")          # names a turn of s1
        _round(conn, "r2", OTHER, "s2 的话")               # another session only
        _call(conn, "k-replay", "u1/x", "t-k-replay")
        _run(conn, f"harness-replay:hr1:{TURN[5:]}", "k-replay")
        _call(conn, "k-imp", "r1", "t-k-imp")
        _run(conn, "harness:r1:improver", "k-imp", mention=TURN)
        _call(conn, "k-judge", f"u1/{TURN}/ab", "t-k-judge")   # a judge call on the turn
        _call(conn, "k-keep", "u1/" + OTHER + "/ab", "t-k-keep")
        _call(conn, "k-imp2", "r2", "t-k-imp2")
        _run(conn, "harness:r2:improver", "k-imp2", mention=OTHER)
    return pool


def _forget(pool, session="s1"):
    with pool.connection() as conn:
        rid = conn.execute("select evowiki_forget_request('u1', %s, null, 'api:test') as r",
                           (session,)).fetchone()["r"]
    with pool.connection() as conn, conn.transaction():
        conn.execute("select evowiki_forget_open(%s)", (rid,))
        out = forget_session(conn, "u1", session)
        conn.execute("select evowiki_forget_close(%s)", (rid,))
    return out


def _one(pool, sql, params=()):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchone()


def test_the_session_is_redacted_and_its_runs_and_calls_deleted(world):
    out = _forget(world)
    r1 = _one(world, "select * from harness_round where round_id = 'r1'")
    assert r1["reason"] == REDACTED
    assert r1["candidates"][0]["rationale"] == REDACTED
    assert r1["decision"]["rationale"] == REDACTED and r1["decision"]["gate"] == {"verdict": "pass"}
    turn_case, deep_case = r1["cases"]["problem"]
    assert turn_case == {"case_id": DELETED, "run_id": DELETED, "kind": "assistant",
                         "signal_ids": []}
    assert deep_case["case_id"] == "case:run-1:s2"                     # another case stays
    assert r1["signal_ids"] == ["sig-deep"]
    assert r1["comparisons"][0]["quote"] == REDACTED == r1["comparisons"][0]["reason"]
    assert r1["diff"]["ops"][0]["name"] == "H1.assistant"               # undo still has it
    assert r1["status"] == "applied" and r1["cost"] == 0.5
    ch = _one(world, "select * from harness_change where change_id = 'c-r1'")
    assert ch["reason"] == REDACTED and ch["status"] == "applied"
    ex = _one(world, "select * from harness_experience where exp_id = 'x-r1'")
    assert ex["verdict"] == "harmful" and ex["evidence"]["source_deleted"] is True
    for run in (f"harness-replay:hr1:{TURN[5:]}", "harness:r1:improver"):
        assert _one(world, "select count(*) as n from agent_run where run_id = %s", (run,))["n"] == 0
    for cid in ("k-replay", "k-imp", "k-judge"):
        assert _one(world, "select count(*) as n from llm_call where id = %s", (cid,))["n"] == 0
        assert _one(world, "select count(*) as n from llm_attempt where id = %s",
                    (f"a-{cid}",))["n"] == 0
        assert _one(world, "select count(*) as n from llm_text where hash = %s",
                    (f"t-{cid}",))["n"] == 0
    assert out["agent_run"] == 2 and out["llm_call"] == 3 and out["harness_round"] == 1


def test_another_session_and_its_round_are_untouched(world):
    _forget(world)
    r2 = _one(world, "select * from harness_round where round_id = 'r2'")
    assert r2["reason"] == "已改：s2 的话" and r2["cases"]["problem"][0]["case_id"] == OTHER
    assert _one(world, "select reason from harness_change where change_id = 'c-r2'")["reason"] \
        == "因为「s2 的话」"
    assert "source_deleted" not in _one(world, "select evidence from harness_experience "
                                               "where exp_id = 'x-r2'")["evidence"]
    for cid in ("k-keep", "k-imp2"):
        assert _one(world, "select count(*) as n from llm_call where id = %s", (cid,))["n"] == 1
    assert _one(world, "select count(*) as n from agent_run where run_id = 'harness:r2:improver'"
                )["n"] == 1


def test_a_second_run_changes_nothing(world):
    first = _forget(world)
    assert first["harness_round"] == 1
    again = _forget(world)
    assert all(v == 0 for v in again.values()), again
    with world.connection() as conn:
        rid = conn.execute("select request_id from forget_request order by requested_at desc "
                           "limit 1").fetchone()["request_id"]
        moved = conn.execute("select coalesce(sum(rows), 0) as n from forget_audit where "
                             "request_id = %s and (step like 'delete:%%' or step like "
                             "'update:%%')", (rid,)).fetchone()["n"]
    assert moved == 0                      # the reconcile run moved no row of any table


def test_without_an_open_gate_it_is_refused(world):
    with world.connection() as conn, conn.transaction(), \
            pytest.raises(psycopg.errors.RaiseException):
        forget_session(conn, "u1", "s1")


# ---- the session's own words copied into changes -----------------------------------------------

QUOTE = "以后讲公式的时候请先把每个符号是什么意思讲清楚"
H1_TEXT = f"学习者说过「{QUOTE}」，所以每个新符号先解释再用。"


@pytest.fixture
def quoted(world):
    """The improver copied the session's words into a live H1 value and its change."""
    from evowiki.store import harness as pack_store
    with world.connection() as conn:
        conn.execute("insert into assistant_entry (session_id, seq, entry_id, type, payload, "
                     "occurred_at) values ('s1', 1, 'e1', 'user', %s, %s)",
                     (_j({"text": f"嗯。{QUOTE}，谢谢"}), T0))
        pack_store.set_value(conn, "u1", "H1.write", H1_TEXT, source="improver", why=H1_TEXT,
                             now=T0)
        conn.execute("insert into harness_change (change_id, user_id, round_id, diff, status, "
                     "reason, created_at) values ('c-q', 'u1', 'r2', %s, 'applied', 'x', %s)",
                     (_j({"ops": [{"name": "H1.write", "value": H1_TEXT, "before": None}]}), T0))
        conn.execute("insert into harness_experience (exp_id, user_id, situation, change, "
                     "evidence, verdict, scope, created_at, weight) values ('x-q', 'u1', '{}', "
                     "%s, '{}', 'effective', 'user', %s, 1)",
                     (_j({"ops": [{"name": "H1.write", "value": H1_TEXT}]}), T0))
    return world


def _texts(pool) -> str:
    with pool.connection() as conn:
        parts = [conn.execute(f"select coalesce(string_agg({col}::text, ' '), '') as t "
                              f"from {table} where user_id = 'u1'").fetchone()["t"]
                 for table, col in (("harness_value", "value"), ("harness_change", "diff"),
                                    ("harness_experience", "change"),
                                    ("harness_version", "concat(before, after, why)"))]
    return " ".join(parts)


def test_copied_words_are_removed_everywhere_and_that_change_can_no_longer_be_undone(quoted):
    from evowiki.harness.change_source import SOURCE_DELETED, undo_change
    from evowiki.harness.forget import LIVE_WHY, PLACEHOLDER
    assert QUOTE in _texts(quoted)
    out = _forget(quoted)
    left = _texts(quoted)
    assert QUOTE not in left and PLACEHOLDER in left
    assert out["harness_value"] == 1 and out["harness_change_diff"] == 1
    with quoted.connection() as conn:
        live = conn.execute("select value, source from harness_value where user_id = 'u1' "
                            "and key = 'H1.write'").fetchone()
        assert live["value"] == H1_TEXT.replace(QUOTE, PLACEHOLDER) and live["source"] == "rollback"
        last = conn.execute("select why from harness_version where user_id = 'u1' "
                            "order by version desc limit 1").fetchone()
        assert last["why"] == LIVE_WHY
        ch = conn.execute("select diff from harness_change where change_id = 'c-q'").fetchone()
        assert ch["diff"]["source_deleted"] is True
        untouched = conn.execute("select diff from harness_change where change_id = 'c-r2'"
                                 ).fetchone()
        assert "source_deleted" not in untouched["diff"]          # quoted nothing: undo as before
    with quoted.connection() as conn, conn.transaction(), \
            pytest.raises(ValueError, match=SOURCE_DELETED):
        undo_change(conn, "u1", "c-q", registry=None, now=T0)
    again = _forget(quoted)
    assert all(v == 0 for v in again.values()), again


def test_explicit_quotes_win_over_the_session_entries(quoted):
    out = _forget_with(quoted, quotes=["完全不相干的另一句很长的话在这里"])
    assert out["harness_value"] == 0 and QUOTE in _texts(quoted)


def test_short_pieces_are_not_matched():
    from evowiki.harness.forget import fragments
    assert fragments(["好的，谢谢。先讲符号，再讲公式的推导过程吧"]) == ("再讲公式的推导过程吧",)


def _forget_with(pool, *, quotes):
    with pool.connection() as conn:
        rid = conn.execute("select evowiki_forget_request('u1', 's1', null, 'api:test') as r"
                           ).fetchone()["r"]
    with pool.connection() as conn, conn.transaction():
        conn.execute("select evowiki_forget_open(%s)", (rid,))
        out = forget_session(conn, "u1", "s1", quotes=quotes)
        conn.execute("select evowiki_forget_close(%s)", (rid,))
    return out
