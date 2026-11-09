import json
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.agent.tools import ToolResult
from evowiki.content.models import Lang
from evowiki.forge import optin, round, switch
from evowiki.forge.api import ForgeDeps
from evowiki.forge.models import ForgeDisabled
from evowiki.forge.round import NoGate, run_round
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.store import forge as store
from evowiki.store.trace import PgTraceSink
from evowiki.store.users import UserRow, create_user
from tests.forge.fakes import InProcessSandbox, ScriptedConverse, accounted_fake

T0 = datetime(2026, 10, 22, tzinfo=UTC)
NOW = T0 + timedelta(days=1)
TOOL = ("import host\n"
        "def run(args):\n"
        "    return {'results': [host.call('read_page', {'page_id': p}) "
        "for p in args['page_ids']]}\n")
TESTS = ("import host\nimport tool\n\ndef test_reads():\n"
         "    host.install(lambda t, a: '<' + a['page_id'] + '>')\n"
         "    assert tool.run({'page_ids': ['x']}) == {'results': ['<x>']}\n")
SCHEMA = json.dumps({"type": "object", "description": "一次读多页：按顺序读取每个页面",
                     "properties": {"page_ids": {"type": "array", "items": {"type": "string"},
                                                 "description": "页面 id 列表"}},
                     "required": ["page_ids"]})


def coder():
    def c(i, name, **args):
        return ChatTurn(text="", tool_calls=(ToolCall(id=f"t{i}", name=name, args=args),))
    return ScriptedConverse(c(1, "write_file", path="tool.py", content=TOOL),
                            c(2, "write_file", path="test_tool.py", content=TESTS),
                            c(3, "write_file", path="schema.json", content=SCHEMA),
                            c(4, "submit", summary="写好了"))


class Trace:
    def start(self, *a, **kw): ...
    def model(self, *a, **kw): ...
    def tool(self, *a, **kw): ...
    def finish(self, *a, **kw): ...


class FakeGate:
    def __init__(self):
        self.seen = []

    def evaluate(self, conn, candidate_id, user_id):
        self.seen.append((candidate_id, user_id))
        return f"gate:{candidate_id}"

    def ok(self, gate_ref):
        return gate_ref.startswith("gate:")


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"fc_r{self.n}"


@pytest.fixture
def traced(pool):
    """u1 with three finished page writes, each reading four pages one by one."""
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))
    now = [T0]
    sink = PgTraceSink(pool, lambda: now[0])
    for r in range(3):
        now[0] = T0 + timedelta(minutes=r)
        rid = f"run{r}"
        sink.start(rid, user_id="u1", agent="deep.write", subject="p", pack_version=0, input={})
        for i, p in enumerate(f"{x}{r}" for x in "abcd"):
            sink.tool(rid, i, f"t{i}", "read_page", {"page_id": p}, ToolResult(content=f"<{p}>"))
        sink.finish(rid, stop="finished", error=None)
    return "u1"


def deps(pool, converse, ids=None):
    return ForgeDeps(pool=pool, converse=accounted_fake(converse, cost=0.01),
                     sandbox=InProcessSandbox(), trace=Trace(), now=lambda: NOW,
                     new_id=ids or Ids())


def test_passing_gate_promotes_opts_in_and_indexes(pool, traced):
    emb, gate = HashingEmbedder(dim=64), FakeGate()
    res = run_round("u1", deps=deps(pool, coder()), gate=gate, embedder=emb, now=NOW)
    assert res.proposed == res.built == res.promoted == ("fc_r1",)
    assert res.cost_usd == pytest.approx(0.04) and gate.seen == [("fc_r1", "u1")]
    with pool.connection() as conn:
        assert store.registry_head(conn, "u1")[1] == {"batch_read_page": "fc_r1"}
        assert store.get_candidate(conn, "fc_r1").status == "promoted"
        assert store.get_candidate(conn, "fc_r1").origin == "round"
        assert optin.opted_in(conn, "u1", "deep", ["batch_read_page"], {}) == {"batch_read_page"}
        assert set(store.vecs(conn, ["fc_r1"], model_version=emb.model_version)) == {"fc_r1"}
        ev = store.get_candidate(conn, "fc_r1").evidence
    assert ev["citations"] == ["trace:run2", "trace:run1", "trace:run0"]


def test_no_gate_leaves_the_candidate_built(pool, traced):
    res = run_round("u1", deps=deps(pool, coder()), gate=NoGate(), embedder=HashingEmbedder(),
                    now=NOW)
    assert res.proposed == res.built == ("fc_r1",) and res.promoted == ()
    with pool.connection() as conn:
        assert store.get_candidate(conn, "fc_r1").status == "built"
        assert store.registry_head(conn, "u1")[1] == {}
        assert optin.opted_in(conn, "u1", "deep", ["batch_read_page"], {}) == frozenset()


def test_second_round_skips_the_duplicate_without_a_model_call(pool, traced):
    run_round("u1", deps=deps(pool, coder()), gate=NoGate(), embedder=HashingEmbedder(), now=NOW)
    idle = ScriptedConverse()
    res = run_round("u1", deps=deps(pool, idle, Ids()), gate=NoGate(),
                    embedder=HashingEmbedder(), now=NOW)
    assert res.proposed == () and idle.requests == []
    assert len(res.skipped) == 1 and "一次读多页" in res.skipped[0]
    with pool.connection() as conn:
        assert round.requests(conn, "u1", now=NOW, embedder=HashingEmbedder()) == []


def test_promoted_tool_is_a_duplicate_too(pool, traced):
    run_round("u1", deps=deps(pool, coder()), gate=FakeGate(), embedder=HashingEmbedder(),
              now=NOW)
    idle = ScriptedConverse()
    res = run_round("u1", deps=deps(pool, idle), gate=FakeGate(), embedder=HashingEmbedder(),
                    now=NOW)
    assert res.proposed == () and idle.requests == [] and "已经上线" in res.skipped[0]


def test_proposal_cap(pool, traced):
    idle = ScriptedConverse()
    res = run_round("u1", deps=deps(pool, idle), gate=NoGate(), embedder=HashingEmbedder(),
                    now=NOW, max_proposals=0)
    assert res.proposed == () and idle.requests == [] and "下一轮" in res.skipped[0]
    assert res.cost_usd == 0.0


def test_nothing_detected_is_an_empty_round(pool, traced):
    res = run_round("u1", deps=deps(pool, ScriptedConverse()), gate=NoGate(),
                    embedder=HashingEmbedder(), now=NOW + timedelta(days=30))
    assert res == round.RoundResult(proposed=(), built=(), promoted=(), skipped=(), cost_usd=0.0)


def test_kill_switch(pool, traced):
    with pool.connection() as conn:
        switch.set_enabled(conn, "u1", "tool", False, T0)
    idle = ScriptedConverse()
    with pytest.raises(ForgeDisabled):
        run_round("u1", deps=deps(pool, idle), gate=FakeGate(), embedder=HashingEmbedder(),
                  now=NOW)
    assert idle.requests == []
    with pool.connection() as conn:
        assert store.archive(conn, "u1") == []
