"""A goal with attached materials as the chat makes it (S1, then the map of its blocks; materials
spec 2026-10-20 §7, plan task 5; S1 2026-10-28): S1 gets the material's table of contents; the
draft and the map record which materials the goal was made from. An empty goal becomes
「按《标题》学」; what he wrote then stays his words (`GoalMap.words`). Learning from a material:
blocks have no links (stage 2 brings them back on entry). Scripted LLM only."""

import pytest

from evowiki.goals.intake import SourceToc, TocSection
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import MemoryRepo
from tests.wikiapp.goal_script import auto_probe, plan_and_make, push_no_question
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids

TOC = SourceToc(source_id="s1", title="线性代数讲义", sections=(
    TocSection(id="sec1", label="§1", title="1 向量", pages="p.2–5"),
    TocSection(id="sec11", label="§1.1", title="1.1 向量加法", depth=1, pages="p.2–3"),
    TocSection(id="sec12", label="§1.2", title="1.2 数乘", depth=1, pages="p.4–5")))
PAPER = SourceToc(source_id="p1", title="论文名", sections=(
    TocSection(id="x0", label="§摘要", title="摘要", lead="一句话"),
    TocSection(id="x1", label="§1", title="1 引言", lead="问题")))
TWO_BLOCKS = {"title": "向量", "blocks": [
    {"name": "向量加法", "summary": "两个向量怎么加", "covers": ["加法"], "target": True},
    {"name": "数乘", "summary": "数乘向量", "covers": ["数乘"]}]}


@pytest.fixture
def env():                                   # as tests/wikiapp/test_goal.py
    s = Script()
    auto_probe(s)
    return MemoryRepo(["u1"]), s, Ids()


def _create(env, goal, *, sources=(TOC,), use="learn", words=()):
    repo, s, ids = env
    return plan_and_make(repo, CountingGateway(s.gateway()), "u1", goal, words, now=NOW,
                         new_id=ids, sources=sources, use=use)


def _state(repo):
    return repo.load("u1").state


def test_learn_with_a_material_is_one_s1_call_and_records_the_source(env):
    repo, s, _ = env
    s.push("goals.intake", TWO_BLOCKS)
    push_no_question(s)
    res = _create(env, "我要学向量")
    p = s.requests("goals.intake")[0].prompt
    assert "《线性代数讲义》" in p and "§1.1 1.1 向量加法" in p
    assert s.calls("goals.intake") == 1 and res.map_id is not None and res.links == ()
    assert res.draft.sources == ("s1",) and res.draft.source_use == "learn"
    assert res.draft.goal == "我要学向量"
    m = _state(repo).maps[res.map_id]
    assert m.sources == ("s1",) and [n.name for n in m.nodes] == ["向量加法", "数乘"]


def test_learn_with_an_empty_goal_follows_the_material(env):
    _, s, _ = env
    s.push("goals.intake", TWO_BLOCKS)
    push_no_question(s)
    res = _create(env, "  ")
    assert "「按《线性代数讲义》学」" in s.requests("goals.intake")[0].prompt
    assert res.draft.goal == "按《线性代数讲义》学"


def test_without_a_material_nothing_is_recorded(env):
    repo, s, _ = env
    s.push("goals.intake", TWO_BLOCKS)
    push_no_question(s)
    res = _create(env, "我要学向量", sources=())
    assert res.links == () and res.draft.sources == () and res.draft.source_use is None
    assert _state(repo).maps[res.map_id].sources == ()



# ---- materials F, plan decision 2: an exam paper under a learning goal is recorded as exam ----

def test_an_exam_paper_is_recorded_as_an_exam_use_with_the_goal(pool):
    from types import SimpleNamespace

    from evowiki.retrieval.embed import HashingEmbedder
    from evowiki.sources import store as source_store
    from evowiki.sources.importer import ImportDeps
    from evowiki.sources.vision import NoVision
    from evowiki.wikiapp.goal_sources import _record, load_materials, recorded_use
    from tests.runtime.helpers import make_user
    from tests.sources.test_importer import _ids, add_source, run
    make_user(pool, "u1")
    deps = ImportDeps(pool=pool, embedder=HashingEmbedder(), llm_for=lambda _u: None,
                      local=NoVision(), claude=NoVision(), assistant_sees_images=lambda _u: False,
                      clock=lambda: NOW, new_id=_ids())
    add_source(pool, "paper", "# 2023 期末考试\n\n1. 均值怎么算？\n".encode(), kind="md")
    add_source(pool, "notes", "# 向量\n\n向量有方向。\n".encode(), kind="md")
    assert run(deps, "paper")[-1][0] == "done"
    assert run(deps, "notes")[-1][0] == "done"
    paper, notes = load_materials(pool, "u1", ["paper", "notes"])
    assert recorded_use("learn", paper) == "exam"
    assert recorded_use("learn", notes) == "learn"
    assert recorded_use("understand", paper) == "understand"
    res = SimpleNamespace(links=(), map_id="m1", draft=None, path_id=None, page_id=None)
    with pool.connection() as conn:
        _record("u1", [paper, notes], "learn", NOW, [])(conn, res)
        uses = {sid: [(u["use"], u["goal_id"]) for u in source_store.uses_of(conn, "u1", sid)]
                for sid in ("paper", "notes")}
    assert uses == {"paper": [("exam", "m1")], "notes": [("learn", "m1")]}
