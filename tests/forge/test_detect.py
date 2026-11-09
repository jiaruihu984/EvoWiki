from datetime import UTC, datetime, timedelta

from evowiki.agent.tools import ToolResult
from evowiki.content.models import Lang
from evowiki.forge import detect
from evowiki.forge.detect import RunTrace, ToolStep
from evowiki.store.trace import PgTraceSink
from evowiki.store.users import UserRow, create_user

T0 = datetime(2026, 10, 22, tzinfo=UTC)


def reads(*pages, error_on=()):
    return tuple(ToolStep(name="read_page", args={"page_id": p}, content=f"<{p}>",
                          error="坏了" if p in error_on else None) for p in pages)


def trace(i, steps, stop="finished", agent="deep", user="u1"):
    return RunTrace(agent=agent, user_id=user, stop=stop, started_at=T0 + timedelta(minutes=i),
                    steps=steps)


def three_runs(**kw):
    return {f"r{i}": trace(i, reads(f"a{i}", f"b{i}", f"c{i}", f"d{i}"), **kw) for i in range(3)}


def test_three_runs_of_fanout_are_one_finding():
    [w] = detect.find(three_runs())
    assert (w.agent, w.tool, w.arg) == ("deep", "read_page", "page_id")
    assert w.runs == ("r2", "r1", "r0")                               # newest first
    assert [h.args["page_id"] for h in w.samples[0]] == ["a2", "b2", "c2", "d2"]
    assert w.samples[0][0].content == "<a2>" and w.samples[0][0].ok


def test_two_runs_are_not_enough():
    runs = three_runs()
    runs.pop("r0")
    assert detect.find(runs) == []


def test_error_results_do_not_count():
    runs = three_runs()
    runs["r0"] = trace(0, reads("a", "b", "c", error_on=("c",)))
    assert detect.find(runs) == []


def test_unfinished_runs_other_tools_and_users_do_not_count():
    base = three_runs()
    assert detect.find({**base, "r0": trace(0, base["r0"].steps, stop="tool_cap")}) == []
    assert detect.find({**base, "r0": trace(0, base["r0"].steps, user="u2")}) == []
    other = tuple(ToolStep(name="write_section", args={"page_id": p}, content="ok")
                  for p in "abc")
    assert detect.find({**base, "r0": trace(0, other)}) == []


def test_repeated_same_value_is_not_fanout():
    runs = three_runs()
    runs["r0"] = trace(0, reads("a", "a", "a", "b"))
    assert detect.find(runs) == []


def test_answered_assistant_runs_count():
    runs = {f"s{i}": trace(i, reads("x", "y", "z"), stop="answered", agent="assistant")
            for i in range(3)}
    [w] = detect.find(runs)
    assert w.agent == "assistant"


def test_to_request_uses_the_recorded_calls():
    [w] = detect.find(three_runs())
    req = detect.to_request(w)
    assert req.name == "batch_read_page" and req.title == "一次读多页" and len(req.title) <= 16
    assert req.host_tools == ("read_page",) and req.agent == "deep" and req.scope == "user"
    assert len(req.examples) == 3
    for ex, sample in zip(req.examples, w.samples, strict=True):
        assert ex.args == {"page_ids": [h.args["page_id"] for h in sample]}
        assert ex.expect == {"results": [h.content for h in sample]}
        assert [(h.tool, h.args, h.ok, h.content) for h in ex.host] == \
               [(h.tool, h.args, h.ok, h.content) for h in sample]


def test_load_reads_the_window(pool):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))
    now = [T0]
    sink = PgTraceSink(pool, lambda: now[0])

    def run(run_id, at, agent="deep.write", stop="finished", pages=("a", "b", "c")):
        now[0] = at
        sink.start(run_id, user_id="u1", agent=agent, subject="p", pack_version=0, input={})
        for i, p in enumerate(pages):
            sink.tool(run_id, i, f"t{i}", "read_page", {"page_id": p},
                      ToolResult(content=f"<{p}>"))
        sink.finish(run_id, stop=stop, error=None)

    run("old", T0 - timedelta(days=20))
    run("r1", T0)
    run("cap", T0, stop="tool_cap")
    run("bot", T0, agent="bot.plan")
    with pool.connection() as conn:
        got = detect.load(conn, "u1", now=T0 + timedelta(days=1))
    assert set(got) == {"r1"}
    t = got["r1"]
    assert t.agent == "deep" and t.stop == "finished"
    assert [(s.name, dict(s.args), s.content, s.error) for s in t.steps] == [
        ("read_page", {"page_id": p}, f"<{p}>", None) for p in "abc"]
