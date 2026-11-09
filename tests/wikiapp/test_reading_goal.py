"""Confirming a 读懂 goal (reading-mode spec §3.4, plan stage 4 Task 3): `create_reading_goal`
writes the prerequisite blocks (as a learning goal's blocks: `block_goal`), a map with only them
on top and the one material as its source, and the reading's units as planned — one commit, no
model call (the opening diagnosis over the prerequisites only picks: stage 3 T11, the assistant
writes its question in the chat). The invariants of §2.3 / §8.1
(不变量, 诊断和 K): a unit is never a node (not in ConceptName, GraphSaved, a map or K); no
「读懂《T》」 block, no 「《T》§…」 node. Scripted LLM only; the use row against the test DB."""

import pytest

from evowiki.goals.blocks import Block
from evowiki.goals.intake import SourceToc, TocSection
from evowiki.goals.reading import ReadingPlan, ReadingUnit, UnitSpan
from evowiki.sources.labels import paragraphs
from evowiki.wikiapp import goal as goal_mod
from evowiki.wikiapp import goal_sources
from evowiki.wikiapp.events import (
    ConceptsNamed,
    GoalMapSaved,
    GraphSaved,
    ReadingSaved,
)
from evowiki.wikiapp.goal import GOAL_IN_PROGRESS, DraftResult
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.reading import (
    MSG_PLAN_NOT_FRESH,
    MSG_PLAN_OTHER_SOURCE,
    MSG_PLAN_STALE,
    create_reading_goal,
    reading_map_id,
    understand_use,
)
from evowiki.wikiapp.repo import MemoryRepo, WikiConflict, WikiInvalid
from tests.wikiapp.goal_script import auto_probe
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids

TITLE = "注意力就是一切"
GOAL = f"读懂《{TITLE}》"
SOURCE = SourceToc(source_id="p1", title=TITLE, sections=(
    TocSection(id="x0", label="§1", title="1 引言", chars=300),
    TocSection(id="x1", label="§2", title="2 方法", chars=900),
    TocSection(id="x2", label="§3", title="3 实验", chars=600)))
UNITS = (ReadingUnit(unit_id="ru_a", order=1, title="要解决什么问题", about="序列模型太慢",
                     spans=(UnitSpan(section_id="x0"),)),
         ReadingUnit(unit_id="ru_b", order=2, title="自注意力怎么算", spans=(UnitSpan(section_id="x1"),)),
         ReadingUnit(unit_id="ru_c", order=3, title="实验结果", spans=(UnitSpan(section_id="x2"),)))
PLAN = ReadingPlan(units=UNITS, source_id="p1", title=TITLE, left_out=())
WORDS = ("我线性代数还行，softmax 完全没学过",)
LIMITS = ("先别讲 RNN",)
PREREQS = (Block(name="矩阵乘法", covers=("矩阵", "乘法"), summary="两个矩阵怎么乘", atomic=True),
           Block(name="softmax", covers=("指数", "归一化"), summary="把分数变成概率",
                 said=("softmax 完全没学过",), said_level="unlearned"),
           Block(name="编程基础", covers=("变量",), diagnose=False))
UNIT_IDS = tuple(u.unit_id for u in UNITS)


@pytest.fixture
def env(monkeypatch):
    """create_goal_from_blocks must not be the way a 读懂 goal is made (§8.1 入口)."""
    def refuse(*_a, **_k):
        raise AssertionError("a reading goal went through the learning goal flow")
    for mod in (goal_mod, goal_sources):
        monkeypatch.setattr(mod, "create_goal_from_blocks", refuse)
    s = Script()
    auto_probe(s)
    return MemoryRepo(["u1"]), s, Ids()


def make(env, blocks=PREREQS, plan=PLAN, *, source=SOURCE, gw=True, **kw):
    repo, s, ids = env
    return create_reading_goal(repo, CountingGateway(s.gateway()) if gw else None, "u1", GOAL,
                               blocks, plan, now=NOW, new_id=ids, source=source, words=WORDS,
                               limits=LIMITS, title=TITLE, **kw)


def state(env):
    return env[0].load("u1").state


def test_confirm_writes_prerequisites_map_and_reading_in_one_commit(env):
    repo, s, _ = env
    commits = []
    inner = repo.commit
    repo.commit = lambda snap, c, **kw: commits.append(c) or inner(snap, c, **kw)
    res = make(env)
    assert len(commits) == 1
    assert s.calls() == 0                       # no S1, and the opening pick calls no model
    st = state(env)
    map_id = reading_map_id(GOAL, "p1")
    m = st.maps[map_id]
    assert res.draft.map_id == map_id and res.reading_id is not None
    # the map: only the prerequisites, all top-level, the one material as its source
    assert [(n.name, n.kind, n.parent) for n in m.nodes] == [
        ("矩阵乘法", "region", None), ("softmax", "region", None), ("编程基础", "region", None)]
    assert m.sources == ("p1",) and m.title == TITLE
    assert m.words == WORDS and m.limits == LIMITS            # the goal record, as S1's learn goal
    assert [n.diagnose for n in m.nodes] == [True, True, False]
    assert m.nodes[1].said == ("softmax 完全没学过",)
    # ConceptName with the blocks call's atomic; every prerequisite a graph node and a K concept
    ids = [n.id for n in m.nodes]
    assert [st.names[i].atomic for i in ids] == [True, False, False]
    assert set(ids) <= {n.id for n in st.graph.nodes}
    assert set(ids) <= set(repo.load("u1").k.concept_ids())
    # the reading: the units as planned, structure only, on this map
    reading = st.reading_of(map_id)
    assert reading is not None and reading.reading_id == res.reading_id
    assert reading.units == UNITS and not any(u.analyzed for u in reading.units)
    assert (reading.source_id, reading.title, reading.created_at) == ("p1", TITLE, NOW)
    saved = [e.body for e in repo.wiki_log("u1") if isinstance(e.body, ReadingSaved)]
    assert [x.action for x in saved] == ["planned"]
    assert [e.body.action for e in repo.wiki_log("u1")
            if isinstance(e.body, GoalMapSaved)] == ["planned"]


def test_units_are_never_nodes(env):
    """§2.3 不变量: unit ids are in no ConceptName, GraphSaved, map node or K cell; the goal has
    no 「读懂《T》」 block and no 「《T》§…」 node."""
    repo, _, _ = env
    res = make(env)
    st = state(env)
    k = repo.load("u1").k
    for ev in repo.wiki_log("u1"):
        if isinstance(ev.body, ConceptsNamed | GraphSaved | GoalMapSaved):
            dumped = ev.body.model_dump_json()
            assert not any(u in dumped for u in UNIT_IDS), type(ev.body).__name__
    assert not set(UNIT_IDS) & set(st.names)
    assert not set(UNIT_IDS) & {n.id for n in st.graph.nodes}
    assert not set(UNIT_IDS) & {x for r in st.graph.relations for x in (r.src, r.dst)}
    assert not set(UNIT_IDS) & {n.id for m in st.maps.values() for n in m.nodes}
    assert not set(UNIT_IDS) & set(k.concept_ids())
    for e in repo.aspect_log("u1"):
        assert not any(u in e.model_dump_json() for u in UNIT_IDS)
    names = [n.name for n in st.graph.nodes] + [n.name for n in st.maps[res.draft.map_id].nodes]
    assert not any(n.startswith("读懂《") or f"《{TITLE}》§" in n for n in names)


def test_opening_asks_about_the_prerequisites_only(env):
    """§8.1 诊断和 K: the opening diagnosis picks among the prerequisite blocks, never a unit; a
    block not to diagnose (a stated beginner's) is never picked."""
    res = make(env)
    m = state(env).maps[res.draft.map_id]
    c_mat, c_soft, c_prog = (n.id for n in m.nodes)
    assert res.open
    picks = [r.pick for r in res.draft.rounds if r.pick is not None]
    asked = {p.node for p in picks}
    assert asked and asked <= {c_mat, c_soft} and c_prog not in asked
    assert not any(u.unit_id in p.model_dump_json() for p in picks for u in UNITS)
    assert res.draft.sources == ("p1",) and res.draft.source_use == "understand"


def test_his_words_about_a_prerequisite_are_a_self_report(env):
    """The blocks part is the learning goal's (`block_goal` → `_said_gaps`): his verbatim words
    about softmax are evidence on its cells in the same commit."""
    from evowiki.belief.aspects import Observe
    repo, _, _ = env
    res = make(env)
    c_soft = state(env).maps[res.draft.map_id].nodes[1].id
    observed = [e.observation for e in repo.aspect_log("u1") if isinstance(e, Observe)]
    assert observed and {o.cell.rpartition(".")[0] for o in observed} == {c_soft}
    assert all(o.polarity == "-" for o in observed)


def test_no_prerequisites_is_a_map_with_no_nodes_and_no_probe(env):
    repo, s, _ = env
    res = make(env, (), gw=False)                 # no gateway: no call can be made
    assert s.calls() == 0 and res.calls == 0 and not res.open
    map_id = reading_map_id(GOAL, "p1")
    assert res.map_id == map_id and res.reading_id is not None
    st = state(env)
    assert st.maps[map_id].nodes == () and st.maps[map_id].sources == ("p1",)
    assert st.reading_of(map_id).units == UNITS
    assert not any(isinstance(e.body, ConceptsNamed) for e in repo.wiki_log("u1"))
    assert not st.drafts


def test_same_goal_again_replaces_an_unstarted_reading_and_refuses_a_started_one(env):
    repo, _, ids = env
    first = make(env)
    map_id = first.draft.map_id
    again = make(env, PREREQS[:1])
    assert again.draft.map_id == map_id and again.reading_id == first.reading_id
    st = state(env)
    assert first.draft.draft_id in st.closed_drafts            # the replaced opening: skipped
    assert [n.name for n in st.maps[map_id].nodes] == ["矩阵乘法"]
    assert len(st.readings) == 1
    # a unit started (its page exists) is progress: the same goal is refused
    reading = st.reading_of(map_id)
    started = reading.model_copy(update={"units": (
        reading.units[0].model_copy(update={"page_id": "pg_x"}), *reading.units[1:])})
    snap = repo.load("u1")
    w = goal_mod._Work(snap, None, "u1", now=NOW, new_id=ids)
    w.add(ReadingSaved(record=started, action="page_started"))
    w.commit(repo)
    with pytest.raises(WikiConflict) as exc:
        make(env)
    assert str(exc.value) == GOAL_IN_PROGRESS


def test_an_analyzed_unit_is_progress_too(env):
    """M3: 「看这一段」 paid for and wrote the unit's points: confirming the same goal again
    would throw that away, so it is refused as a started reading is."""
    repo, _, ids = env
    make(env)
    reading = next(iter(state(env).readings.values()))
    seen = reading.model_copy(update={"units": (
        reading.units[0].model_copy(update={"analyzed": True}), *reading.units[1:])})
    w = goal_mod._Work(repo.load("u1"), None, "u1", now=NOW, new_id=ids)
    w.add(ReadingSaved(record=seen, action="unit_analyzed"))
    w.commit(repo)
    with pytest.raises(WikiConflict) as exc:
        make(env)
    assert str(exc.value) == GOAL_IN_PROGRESS


@pytest.mark.parametrize(("plan", "source", "msg"), [
    (PLAN.model_copy(update={"source_id": "p2"}), SOURCE, MSG_PLAN_OTHER_SOURCE),
    (PLAN.model_copy(update={"units": (UNITS[0].model_copy(update={"analyzed": True}),
                                       *UNITS[1:])}), SOURCE, MSG_PLAN_NOT_FRESH),
    (PLAN, SOURCE.model_copy(update={"sections": SOURCE.sections[:2]}), MSG_PLAN_STALE),
    # a split span whose first words are no longer its paragraph's (the text changed: M6)
    (PLAN.model_copy(update={"units": (
        UNITS[0], UNITS[1].model_copy(update={"spans": (UnitSpan(
            section_id="x1", para_from=0, para_to=0, anchor="旧的开头"),)}),
        UNITS[2].model_copy(update={"spans": (UnitSpan(
            section_id="x1", para_from=1, para_to=1, anchor="缩放点积的公式。"),
            UnitSpan(section_id="x2"))}))}),
     SOURCE.model_copy(update={"sections": (
         SOURCE.sections[0],
         SOURCE.sections[1].model_copy(update={"paras": tuple(paragraphs(
             "自注意力的定义。\n\n缩放点积的公式。"))}),
         SOURCE.sections[2])}), MSG_PLAN_STALE),
])
def test_a_plan_that_does_not_fit_writes_nothing(env, plan, source, msg):
    repo, s, _ = env
    with pytest.raises(WikiInvalid) as exc:
        make(env, plan=plan, source=source)
    assert str(exc.value) == msg
    assert repo.wiki_log("u1") == [] and s.calls() == 0


def test_on_commit_gets_the_map_and_reading(env):
    seen: list[DraftResult] = []
    res = make(env, (), gw=False, on_commit=lambda _conn, r: seen.append(r))
    assert [(r.map_id, r.reading_id) for r in seen] == [(res.map_id, res.reading_id)]


def test_understand_use_writes_the_source_use_row(pool):
    """The commit's material write (§3.4 item 4): one `understand` use with the map as its goal,
    through the store function goal_sources._record uses; no link."""
    from evowiki.retrieval.embed import HashingEmbedder
    from evowiki.sources import store as source_store
    from evowiki.sources.importer import ImportDeps
    from evowiki.sources.vision import NoVision
    from tests.runtime.helpers import make_user
    from tests.sources.test_importer import _ids, add_source, run
    make_user(pool, "u1")
    deps = ImportDeps(pool=pool, embedder=HashingEmbedder(), llm_for=lambda _u: None,
                      local=NoVision(), claude=NoVision(), assistant_sees_images=lambda _u: False,
                      clock=lambda: NOW, new_id=_ids())
    add_source(pool, "paper", "# 引言\n\n序列模型太慢。\n\n# 方法\n\n自注意力。\n".encode(),
               kind="md")
    assert run(deps, "paper")[-1][0] == "done"
    res = DraftResult(draft=None, page_id=None, calls=0, map_id="map-r", reading_id="rdg_1")
    reruns: list = []
    with pool.connection() as conn:
        understand_use("u1", "paper", NOW, reruns)(conn, res)
        uses = [(u["use"], u["goal_id"]) for u in source_store.uses_of(conn, "u1", "paper")]
        links = source_store.links_for_sources(conn, "u1", ["paper"])
    assert uses == [("understand", "map-r")] and links == []
