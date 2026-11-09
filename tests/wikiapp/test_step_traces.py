"""The main lines as traced steps (observability plan 2026-10-29 §5 1a): knowledge split (each 细分
entrance its own run, source in its input) and the learning path record their refused replies on their run."""
from types import SimpleNamespace

import pytest

from evowiki.goals.intake import IntakeError
from evowiki.goals.path import LearningPath, PathViolation
from evowiki.llm import run_context
from evowiki.wikiapp.llm import require
from evowiki.wikiapp.node_split import split_node
from evowiki.wikiapp.path_trace import traced_plan
from tests.llm.test_run_context import Store
from tests.wikiapp.graph_seed import DET, MM, NOW, seed
from tests.wikiapp.script import Script
from tests.wikiapp.split_answers import PURPOSE as SPLIT
from tests.wikiapp.split_answers import keep, parts


@pytest.fixture
def store():
    s = Store()
    run_context.configure(s)
    yield s
    run_context.configure(None)


class Gw:
    """A CountingGateway stand-in: returns `out` with a call whose id is `cid`."""

    def __init__(self, user_id="u1"):
        self.user_id = user_id

    def call(self, role, req, schema):
        call = SimpleNamespace(id="llm_x", raw_output="{}")
        run_context.record_call(req, schema.__name__, call, None)
        return SimpleNamespace(), call


def test_a_split_unusable_twice_is_two_rejections_and_the_run_error(store):
    repo, ids = seed()
    s = Script().push(SPLIT, parts("一行乘一列"), parts("一行乘一列"))
    with pytest.raises(IntakeError):
        split_node(repo, require(s.gateway(), "u1"), "u1", MM, reason="confused",
                   source="dont_get", now=NOW, new_id=ids)
    assert store.kinds() == ["start", "call", "reject", "call", "reject", "finish"]
    start = store.rows[0]
    assert start[5] == "decompose" and start[6] == {"source": "dont_get", "reason": "confused"}
    assert "至少要 2 个" in store.rows[2][3]
    assert store.rows[-1][3].startswith("IntakeError: ")


def test_each_entrance_is_its_own_run_with_its_source_and_a_kept_split_says_so(store):
    repo, ids = seed()
    s = Script().push(SPLIT, keep(), parts("点积", "维度"))
    gw = require(s.gateway(), "u1")
    split_node(repo, gw, "u1", MM, reason="confused", source="dont_get", now=NOW, new_id=ids)
    split_node(repo, gw, "u1", DET, reason="requested", source="chat", now=NOW, new_id=ids)
    starts = [r for r in store.rows if r[0] == "start"]
    finishes = [r for r in store.rows if r[0] == "finish"]
    assert [r[6]["source"] for r in starts] == ["dont_get", "chat"]
    assert len({r[1] for r in starts}) == 2                    # two runs, one per split
    assert [r[2] for r in finishes] == ["kept", "split"]


class Llm:
    def __init__(self, calls):
        self.calls = list(calls)


def _path(source, rejected=()):
    return LearningPath.model_construct(source=source, rejected=tuple(rejected))


def test_each_sent_back_path_draft_is_a_rejection_naming_its_call(store):
    a, b = SimpleNamespace(id="c1", raw_output="x"), SimpleNamespace(id="c2", raw_output="y")
    llm = Llm([])
    bad = (PathViolation(rule="shape", message="第 1 页没有目标"),)

    def plan(llm):
        llm.calls += [a, b]
        return _path("llm", [bad])
    traced_plan("u1", "path-k", llm, plan)
    rows = store.rows
    assert [r[0] for r in rows] == ["start", "reject", "finish"]
    assert rows[1][3:5] == ("第 1 页没有目标", "c1") and rows[1][5]["attempt"] == 1
    assert rows[2][2] == "llm"


def test_a_draft_that_did_not_parse_is_not_a_rejection(store):
    llm = Llm([])

    def plan(llm):
        llm.calls += [None]
        return _path("fallback", [(PathViolation(rule="shape", message="不是 JSON"),)])
    traced_plan("u1", "path-k", llm, plan)
    assert store.kinds() == ["start", "finish"] and store.rows[1][2] == "fallback"
