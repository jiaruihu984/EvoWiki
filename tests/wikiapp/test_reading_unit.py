"""Entering a unit of a 读懂 goal (reading-mode spec §3.5, §4, plan stage 4 Task 4): 「看这一段」
(`analyze_unit`: one call, teaches / uses / focus into the shared graph, the map's top level and
the unit, teaches links in the same commit; never again once analyzed; an unusable answer asked
once more, then nothing written), the reading page (`start_unit_page`: no call, one path per
reading), a prerequisite found while reading (`add_unit_use`), the stability ops, and what the
writer gets (the unit's own paragraphs, focus, prerequisite pages, checks only on targets). The
K bullets of §8.1 「诊断和 K」 2–3. Scripted LLM / memory materials only."""

from dataclasses import replace

import pytest

from evowiki.belief.aspects import Observe
from evowiki.deep import agent as deep_agent
from evowiki.deep.contracts import RefineDraft, RefinedSection
from evowiki.goals.blocks import Block
from evowiki.goals.contracts import CheckItem, OutlineSection, PageOutline, SectionRole
from evowiki.goals.fold import Page, states_for
from evowiki.goals.intake import IntakeError
from evowiki.goals.reading import ReadingPlan, ReadingRefused, ReadingUnit, skip_unit, span_of
from evowiki.goals.reading_unit import PURPOSE
from evowiki.sources.excerpts import MemorySourceExcerpts
from evowiki.sources.labels import paragraphs, source_toc
from evowiki.wikiapp import goal as goal_mod
from evowiki.wikiapp.events import (
    ConceptsNamed,
    GoalMapSaved,
    GraphSaved,
    PageSaved,
    PathSaved,
    ReadingSaved,
    WikiEvent,
)
from evowiki.wikiapp.kchange import answer_check
from evowiki.wikiapp.kwrite import page_spec
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.page import mark_done
from evowiki.wikiapp.reading import (
    MSG_USE_IS_TAUGHT,
    add_unit_use,
    analyze_unit,
    create_reading_goal,
    reading_path_id,
    start_unit_page,
)
from evowiki.wikiapp.reading_view import unit_out
from evowiki.wikiapp.refine_apply import checked_citations, refine_input, with_sources
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiInvalid
from tests.sources.test_store import _sec, _src
from tests.wikiapp.goal_script import answer_all, answer_round, auto_probe, idk
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids

TITLE = "注意力就是一切"
GOAL = f"读懂《{TITLE}》"
SRC = replace(_src("p1", status="ready"), title=TITLE)
SECS = [replace(_sec("x0", 0, source="p1", text="〔p.1〕序列模型太慢。\n\n我们想并行。"),
                title="1 引言", page_from=1, page_to=1),
        replace(_sec("x1", 1, source="p1",
                     text="〔p.2〕自注意力的定义。\n\n缩放点积的公式。\n\n〔p.3〕多头注意力把它拆成几份。"),
                title="2 方法", page_from=2, page_to=3),
        replace(_sec("x2", 2, source="p1", text="〔p.4〕翻译任务上更快。"), title="3 实验",
                page_from=4, page_to=4)]
TOC = source_toc(SRC, SECS)
PARAS = {s.section_id: paragraphs(s.text, page=s.page_from) for s in SECS}
# §2 is split: unit b its first paragraph, unit c the other two (b and c share page 2)
UNITS = (ReadingUnit(unit_id="ru_a", order=1, title="要解决什么问题", spans=(span_of(PARAS["x0"], "x0"),)),
         ReadingUnit(unit_id="ru_b", order=2, title="自注意力", about="定义",
                     spans=(span_of(PARAS["x1"], "x1", 0, 0),)),
         ReadingUnit(unit_id="ru_c", order=3, title="缩放和多头",
                     spans=(span_of(PARAS["x1"], "x1", 1, 2),)),
         ReadingUnit(unit_id="ru_d", order=4, title="实验结果", spans=(span_of(PARAS["x2"], "x2"),)))
PLAN = ReadingPlan(units=UNITS, source_id="p1", title=TITLE)
PREREQS = (Block(name="矩阵乘法", summary="两个矩阵怎么乘", atomic=True),
           Block(name="softmax", summary="把分数变成概率", atomic=True))
UNIT_IDS = tuple(u.unit_id for u in UNITS)

B_OUT = {"teaches": [{"name": "自注意力", "summary": "每个词按相关程度看其他词", "atomic": True}],
         "uses": [{"block": "矩阵乘法"}, {"name": "向量点积", "summary": "对应相乘再相加",
                                         "atomic": True}],
         "focus": ["「每个位置都看所有位置」这句怎么理解"]}


@pytest.fixture
def env():
    s = Script()
    auto_probe(s)
    return MemoryRepo(["u1"]), s, Ids()


def materials() -> MemorySourceExcerpts:
    return MemorySourceExcerpts({"u1": [(SRC, SECS, [])]})


def goal(env, goal_text=GOAL, plan=PLAN, toc=TOC):
    repo, s, ids = env
    return create_reading_goal(repo, CountingGateway(s.gateway()), "u1", goal_text, PREREQS,
                               plan, now=NOW, new_id=ids, source=toc, title=TITLE)


def st(env):
    return env[0].load("u1").state


def analyze(env, unit_id, *outs, **kw):
    repo, s, ids = env
    for o in outs:
        s.push(PURPOSE, o)
    rid = next(iter(st(env).readings))
    text = materials().for_spans("u1", "p1", next(u for u in st(env).readings[rid].units
                                                    if u.unit_id == unit_id).spans)
    return analyze_unit(repo, CountingGateway(s.gateway()), "u1", rid, unit_id, now=NOW,
                        new_id=ids, excerpts=text, **kw)


def rid(env) -> str:
    return next(iter(st(env).readings))


def skip(env, unit_id: str) -> None:
    """A unit set to 不读 before anything started on it (the draft's edit_reading, stored)."""
    repo, _, ids = env
    snap = repo.load("u1")
    out = skip_unit(snap.state.readings[rid(env)], unit_id)
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                             body=ReadingSaved(record=out, action="edited"))]))


def map_of(env):
    return st(env).maps[st(env).readings[rid(env)].map_id]


# --- 看这一段 ------------------------------------------------------------------------------------

def test_first_open_analyzes_once_and_writes_graph_map_and_links(env):
    repo, s, _ = env
    goal(env)
    blocks = {n.name: n.id for n in map_of(env).nodes}
    before = len(repo.wiki_log("u1"))
    commits, seen = [], []
    inner = repo.commit
    repo.commit = lambda snap, c, **kw: commits.append(c) or inner(snap, c, **kw)
    res = analyze(env, "ru_b", B_OUT, on_commit=lambda _conn, r: seen.append(r))
    assert len(commits) == 1 and s.calls(PURPOSE) == 1 and res.calls == 1
    state = st(env)
    unit = state.readings[rid(env)].units[1]
    assert unit.analyzed and unit.focus == tuple(B_OUT["focus"])
    (t,) = unit.teaches
    assert unit.uses[0] == blocks["矩阵乘法"]
    dot = unit.uses[1]
    # teaches and the new prerequisite: shared-graph nodes, named (with atomic) and K concepts
    assert state.names[t].name == "自注意力" and state.names[t].atomic is True
    assert state.names[dot].name == "向量点积"
    assert {t, dot} <= {n.id for n in state.graph.nodes}
    assert {t, dot} <= set(repo.load("u1").k.concept_ids())
    assert any(r.kind == "prerequisite" and r.src == dot and r.dst == t
               for r in state.graph.relations)
    # the new prerequisite on the map's top level, the teaches never
    m = map_of(env)
    assert [(n.name, n.parent) for n in m.nodes][-1] == ("向量点积", None)
    assert t not in {n.id for n in m.nodes}
    # one source_link per teaches node and section the unit reads, by reading
    assert [(k.node_id, k.section_id, k.role, k.by) for k in res.links] == [
        (t, "x1", "teaches", "reading")]
    assert seen and seen[0].links == res.links
    actions = [type(e.body).__name__ + ":" + getattr(e.body, "action", "")
               for e in repo.wiki_log("u1")[before:]]
    assert "ReadingSaved:unit_analyzed" in actions and "GoalMapSaved:edited" in actions
    # opened again: no call, nothing written
    n = len(repo.wiki_log("u1"))
    again = analyze(env, "ru_b")
    assert again.calls == 0 and s.calls(PURPOSE) == 1 and len(repo.wiki_log("u1")) == n


def test_the_prompt_reads_only_the_units_paragraphs(env):
    _, s, _ = env
    goal(env)
    analyze(env, "ru_b", B_OUT)
    p = s.requests(PURPOSE)[0].prompt
    assert "自注意力的定义" in p and "缩放点积的公式" not in p      # §2 split: only unit b's part
    assert "「矩阵乘法」" in p and "← 这一段" in p


def test_same_name_and_existing_id_reuse_nodes(env):
    goal(env)
    analyze(env, "ru_b", B_OUT)
    b = st(env).readings[rid(env)].units[1]
    out = {"teaches": [{"name": "缩放点积", "atomic": True}],
           "uses": [{"existing_id": b.teaches[0]}, {"name": "向量点积"}], "focus": []}
    analyze(env, "ru_c", out)
    c = st(env).readings[rid(env)].units[2]
    assert c.uses == (b.teaches[0], b.uses[1])       # by id; the same goal's block of that name
    assert [n.name for n in map_of(env).nodes].count("向量点积") == 1
    # a point another unit of this reading teaches: a use of c only, never on the map (§2.3)
    assert b.teaches[0] not in {n.id for n in map_of(env).nodes}


def test_a_use_found_while_reading_that_another_unit_teaches_stays_off_the_map(env):
    repo, _, ids = env
    goal(env)
    analyze(env, "ru_b", B_OUT)
    analyze(env, "ru_c", {"teaches": [{"name": "多头注意力"}], "uses": [], "focus": []})
    b = st(env).readings[rid(env)].units[1]
    n = len(map_of(env).nodes)
    res = add_unit_use(repo, "u1", rid(env), "ru_c", existing_id=b.teaches[0], now=NOW,
                       new_id=ids)
    assert res.unit.uses == (b.teaches[0],)
    assert len(map_of(env).nodes) == n and b.teaches[0] not in {x.id for x in map_of(env).nodes}


def test_a_teaches_that_is_a_part_of_an_entered_block_is_refused(env):
    """M1: teaches is checked against every node of the map, not only its top level."""
    repo, s, ids = env
    goal(env)
    m = map_of(env)
    top = m.nodes[0]
    part = goal_mod.MapNode(id="c-part", kind="concept", parent=top.id, name="分块矩阵")
    w = goal_mod._Work(repo.load("u1"), None, "u1", now=NOW, new_id=ids)
    w.add(ConceptsNamed(names=(goal_mod.ConceptName(concept_id="c-part", name="分块矩阵",
                                                    atomic=True),)))
    w.add(GoalMapSaved(record=m.model_copy(update={"nodes": (*m.nodes, part)}),
                       action="edited"))
    w.commit(repo)
    bad = {"teaches": [{"name": "分块矩阵"}], "uses": [], "focus": []}
    with pytest.raises(IntakeError):
        analyze(env, "ru_b", bad, bad)
    p = s.requests(PURPOSE)[0].prompt
    assert "「分块矩阵」" in p                        # listed with the goal's prerequisites
    assert not st(env).readings[rid(env)].units[1].analyzed


def test_an_unusable_answer_is_asked_again_then_nothing_is_written(env):
    repo, s, _ = env
    goal(env)
    analyze(env, "ru_b", B_OUT)
    n = len(repo.wiki_log("u1"))
    again_b = {"teaches": [{"name": "自注意力"}]}                 # unit b teaches it already
    with pytest.raises(IntakeError):
        analyze(env, "ru_c", again_b, again_b)
    assert s.calls(PURPOSE) == 3 and len(repo.wiki_log("u1")) == n
    assert not st(env).readings[rid(env)].units[2].analyzed
    # (another unit: the gateway's log would answer unit c's identical request again)
    crossed = {"teaches": [{"name": "翻译速度"}], "uses": [{"name": "翻译速度"}]}
    good = {"teaches": [{"name": "翻译速度"}], "uses": [], "focus": []}
    analyze(env, "ru_d", crossed, good)
    assert st(env).readings[rid(env)].units[3].analyzed


def test_units_are_never_nodes_after_entering_and_reading(env):
    repo, _, ids = env
    goal(env)
    analyze(env, "ru_b", B_OUT)
    start_unit_page(repo, "u1", rid(env), "ru_b", now=NOW, new_id=ids)
    state, k = st(env), repo.load("u1").k
    for ev in repo.wiki_log("u1"):
        if isinstance(ev.body, ConceptsNamed | GraphSaved | GoalMapSaved):
            assert not any(u in ev.body.model_dump_json() for u in UNIT_IDS)
    assert not set(UNIT_IDS) & (set(state.names) | {n.id for n in state.graph.nodes}
                                | {n.id for m in state.maps.values() for n in m.nodes}
                                | set(k.concept_ids()))


# --- 写页面 / the reading path ------------------------------------------------------------------

def test_start_unit_page_makes_a_reading_page_on_the_readings_path(env):
    repo, _, ids = env
    goal(env)
    with pytest.raises(ReadingRefused) as exc:
        start_unit_page(repo, "u1", rid(env), "ru_b", now=NOW, new_id=ids)
    assert exc.value.reason == "not_analyzed"
    analyze(env, "ru_c", {"teaches": [{"name": "多头注意力"}], "uses": [], "focus": []})
    analyze(env, "ru_b", B_OUT)
    c = start_unit_page(repo, "u1", rid(env), "ru_c", now=NOW, new_id=ids)
    b = start_unit_page(repo, "u1", rid(env), "ru_b", now=NOW, new_id=ids)
    state = st(env)
    unit = state.readings[rid(env)].units[1]
    rec = state.pages[b.page_id]
    assert (rec.kind, rec.unit_id, rec.reading_id) == ("reading", "ru_b", rid(env))
    assert rec.targets == unit.teaches and rec.concepts == (*unit.teaches, *unit.uses)
    assert unit.page_id == b.page_id and b.path_id == reading_path_id(rid(env))
    path = state.paths[b.path_id]
    assert [p.page_id for p in path.path.pages] == ["ru_b", "ru_c"]     # unit order
    assert path.page_ids == (b.page_id, c.page_id)
    assert all(p.kind == "reading" for p in path.path.pages)
    assert path.path.pages[0].targets == unit.teaches
    assert path.sources == ("p1",)
    assert [e.body.action for e in repo.wiki_log("u1") if isinstance(e.body, PathSaved)] == [
        "page_started", "page_started"]
    # started again: its page back, nothing written
    n = len(repo.wiki_log("u1"))
    assert start_unit_page(repo, "u1", rid(env), "ru_b", now=NOW,
                           new_id=ids).page_id == b.page_id
    assert len(repo.wiki_log("u1")) == n


def test_a_skipped_unit_is_neither_analyzed_nor_started(env):
    repo, _, ids = env
    goal(env)
    skip(env, "ru_d")
    with pytest.raises(ReadingRefused):
        analyze(env, "ru_d")
    with pytest.raises(ReadingRefused):
        start_unit_page(repo, "u1", rid(env), "ru_d", now=NOW, new_id=ids)


# --- 前置 -----------------------------------------------------------------------------------------

def test_a_prerequisite_found_while_reading_joins_the_unit_and_this_map_only(env):
    repo, s, ids = env
    goal(env)
    other = create_reading_goal(repo, CountingGateway(s.gateway()), "u1", "读懂《另一篇》",
                                (), PLAN, now=NOW, new_id=ids, source=TOC, title="另一篇")
    other_map = st(env).maps[other.map_id]
    first = next(r.reading_id for r in st(env).readings.values() if r.map_id != other.map_id)
    s.push(PURPOSE, B_OUT)
    analyze_unit(repo, CountingGateway(s.gateway()), "u1", first, "ru_b", now=NOW, new_id=ids,
                 excerpts=materials().for_spans("u1", "p1", UNITS[1].spans))
    m_before = st(env).maps[st(env).readings[first].map_id]
    res = add_unit_use(repo, "u1", first, "ru_b", name="词向量", summary="词变成一串数",
                       now=NOW, new_id=ids)
    state = st(env)
    new = res.unit.uses[-1]
    assert state.names[new].name == "词向量" and len(res.unit.uses) == 3
    m = state.maps[m_before.map_id]
    assert len(m.nodes) == len(m_before.nodes) + 1 and m.nodes[-1].id == new
    assert m.nodes[-1].parent is None
    assert state.maps[other.map_id] == other_map                       # other goals untouched
    assert [e.body.action for e in repo.wiki_log("u1")
            if isinstance(e.body, ReadingSaved)][-1] == "uses_added"
    with pytest.raises(WikiInvalid) as exc:
        add_unit_use(repo, "u1", first, "ru_b", existing_id=res.unit.teaches[0], now=NOW,
                     new_id=ids)
    assert str(exc.value) == MSG_USE_IS_TAUGHT
    with pytest.raises(ReadingRefused):
        add_unit_use(repo, "u1", first, "ru_c", name="x", now=NOW, new_id=ids)


def test_unit_out_shows_k_and_pages_of_points(env):
    repo, _, ids = env
    goal(env)
    analyze(env, "ru_b", B_OUT)
    started = start_unit_page(repo, "u1", rid(env), "ru_b", now=NOW, new_id=ids)
    snap = repo.load("u1")
    r = snap.state.readings[rid(env)]
    out = unit_out(snap.state, snap.kview(NOW), r, r.units[1])
    assert out.analyzed and out.page_id == started.page_id
    assert out.teaches[0].page_id == started.page_id and not out.teaches[0].known
    assert [u.on_map for u in out.uses] == [True, True]


# --- what the writer gets --------------------------------------------------------------------------

def _prereq_page(env, cid) -> str:
    """A learning page that teaches `cid` (the 先补 page of a prerequisite)."""
    repo, _, ids = env
    snap = repo.load("u1")
    rec, evs = goal_mod.page_events_for(snap.state, snap.k, title="矩阵乘法怎么算", goal="矩阵",
                                        concepts=(cid,), targets=(cid,), now=NOW, new_id=ids)
    repo.commit(snap, Commit(wiki=evs))
    return rec.page_id


def _reading_input(env, unit_id="ru_b", out=B_OUT):
    repo, _, ids = env
    goal(env)
    analyze(env, unit_id, out)
    unit = next(u for u in st(env).readings[rid(env)].units if u.unit_id == unit_id)
    pre = _prereq_page(env, unit.uses[0]) if unit.uses else None
    pid = start_unit_page(repo, "u1", rid(env), unit_id, now=NOW, new_id=ids).page_id
    snap = repo.load("u1")
    inp = refine_input(snap, pid, NOW, WikiSpecSource(repo), mode="page",
                       outline_spec=page_spec(snap.state, snap.k, pid))
    return with_sources(inp, snap.state.pages[pid].concepts, materials()), pre, pid


def test_a_reading_page_gets_its_units_paragraphs_focus_and_prerequisite_pages(env):
    inp, pre, _ = _reading_input(env)
    assert inp.page_kind == "reading" and inp.focus == tuple(B_OUT["focus"])
    assert [e.section_id for e in inp.sources] == ["x1"]
    assert "自注意力的定义" in inp.sources[0].text and "缩放点积" not in inp.sources[0].text
    assert inp.prereq_pages == ((pre, "矩阵乘法怎么算"),)
    brief = deep_agent.page_brief(inp)
    assert "这一页是带读页" in brief and "读懂上面这一段原文" in brief
    assert B_OUT["focus"][0] in brief and f"[矩阵乘法怎么算](#p:{pre})" in brief
    assert "确认题只出给这一页负责讲的知识点" in brief


def test_the_reading_prompt_follows_the_page_kind_and_old_reads_links(env):
    inp, _, _ = _reading_input(env)
    learn = replace(inp, page_kind="learn", focus=(), prereq_pages=())
    assert "这一页是带读页" not in deep_agent.page_brief(learn)       # materials, but a learn page
    assert "确认题只出给" not in deep_agent.page_brief(learn)
    old = replace(learn, sources=tuple(replace(e, role="reads") for e in inp.sources))
    assert "这一页是带读页" in deep_agent.page_brief(old)              # an older reading page


def test_citations_check_on_a_page_whose_pdf_page_holds_two_units(env):
    out = {"teaches": [{"name": "缩放点积"}], "uses": [], "focus": []}
    inp, _, pid = _reading_input(env, "ru_c", out)
    assert inp.sources[0].page_from == 2 and inp.sources[0].page_to == 3
    good = f"〔{TITLE} p.2：「缩放点积的公式」〕"
    bad = f"〔{TITLE} p.3：「缩放点积的公式」〕"
    draft = RefineDraft.model_construct(page_id=pid, sections=(
        RefinedSection.model_construct(section_id="s1", body=f"先看公式{good}。"),
        RefinedSection.model_construct(section_id="s2", body=f"再看一次{bad}。")))
    checked, issues = checked_citations(draft, inp, materials())
    assert f"〔{TITLE} p.2｜2〕" in checked.sections[0].body      # verified on page 2 (units b, c)
    assert [i.section_id for i in issues if i.kind != "verified"] == ["s2"]   # not on page 3


# --- 诊断和 K (bullets 2–3) -------------------------------------------------------------------------

def _organized_reading_page(env):
    """Unit b's page written: an explain on its teaches point, a recap of the use, one check on
    the teaches cell."""
    repo, _, ids = env
    goal(env)
    analyze(env, "ru_b", B_OUT)
    pid = start_unit_page(repo, "u1", rid(env), "ru_b", now=NOW, new_id=ids).page_id
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    t, use = rec.targets[0], rec.concepts[-1]
    o = PageOutline(page_id=pid, goal=rec.goal, concepts=rec.concepts, targets=rec.targets,
                    sections=(
                        OutlineSection(id="s1", heading="回顾", role=SectionRole.RECAP,
                                       cells=(f"{use}.term",)),
                        OutlineSection(id="s2", heading="原文", role=SectionRole.EXPLAIN,
                                       cells=(f"{t}.term",)),
                        OutlineSection(id="s3", heading="确认", role=SectionRole.CHECK,
                                       cells=(f"{t}.term",), check=CheckItem(
                                           cell=f"{t}.term", stem="?", options=("对", "错", "不知道"),
                                           answer=0, verified=True))))
    page = Page(outline=o, states=states_for(o), texts={}, snapshot={})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(
        record=rec.model_copy(update={"page": page}), action="organized"))]))
    return pid, t, rec.concepts[len(rec.targets):]


def test_a_reading_pages_check_is_evidence_on_its_teaches_cell_only(env):
    repo, _, ids = env
    pid, t, uses = _organized_reading_page(env)
    before = repo.load("u1").kview(NOW)
    seen = len(repo.aspect_log("u1"))
    assert answer_check(repo, None, "u1", pid, "s3", 0, now=NOW, new_id=ids).correct
    new = repo.aspect_log("u1")[seen:]
    assert {e.observation.cell if isinstance(e, Observe) else e.cell for e in new} == {f"{t}.term"}
    after = repo.load("u1").kview(NOW)
    for u in uses:
        for a in after.aspects(u):
            assert after.cell(f"{u}.{a}") == before.cell(f"{u}.{a}")


def test_reading_a_unit_to_the_end_writes_no_evidence(env):
    repo, _, ids = env
    pid, t, _ = _organized_reading_page(env)
    seen = len(repo.aspect_log("u1"))
    mark_done(repo, "u1", pid, now=NOW, new_id=ids)
    assert st(env).pages[pid].done
    new = [e for e in repo.aspect_log("u1")[seen:] if e.type != "page_read"]    # no evidence
    assert not any(t in e.model_dump_json() for e in new)


def test_a_reading_page_whose_unit_is_gone_fails_closed(env):
    """M7: never written from the points' whole linked sections instead of its own paragraphs."""
    from evowiki.wikiapp.events import PageRecord
    from evowiki.wikiapp.refine_apply import reading_fields
    rec = PageRecord(page_id="pg1", title="t", goal="g", concepts=("c1",), targets=("c1",),
                     created_at=NOW, kind="reading", reading_id="rdg-gone", unit_id="ru_x")
    with pytest.raises(LookupError):
        reading_fields(st(env), rec)


# --- the reading goal's map is stage 1's (§8.1 前置, 诊断和 K) ---------------------------------------

def test_prerequisites_open_and_enter_as_stage_1_blocks(env, monkeypatch):
    """An atomic prerequisite opened goes through stage 1's `_open_point`; a non-atomic one
    entered is split one level, its parts on the map under it (the teaches never)."""
    from evowiki.wikiapp.goal import enter_region
    repo, s, ids = env
    blocks = (PREREQS[0], Block(name="线性代数", summary="向量和矩阵", atomic=False))
    gw = CountingGateway(s.gateway())
    res = create_reading_goal(repo, gw, "u1", GOAL, blocks, PLAN, now=NOW,
                              new_id=ids, source=TOC, title=TITLE)
    answer_all(res, idk, lambda draft_id, answers:
               answer_round(repo, gw, "u1", draft_id, answers, now=NOW, new_id=ids))
    m = map_of(env)
    mat, la = (n.id for n in m.nodes)
    opened: list[str] = []
    inner = goal_mod._open_point
    monkeypatch.setattr(goal_mod, "_open_point",
                        lambda repo_, snap, gw, uid, m_, node, **kw:
                        opened.append(node.id) or inner(repo_, snap, gw, uid, m_, node, **kw))
    enter_region(repo, CountingGateway(s.gateway()), "u1", m.map_id, mat, now=NOW, new_id=ids)
    assert opened == [mat]
    s.push("goals.intake", {"parts": [{"name": "向量", "summary": "有方向的量", "atomic": True},
                                      {"name": "矩阵", "summary": "数排成方阵", "atomic": True}]})
    out = enter_region(repo, CountingGateway(s.gateway()), "u1", m.map_id, la, now=NOW,
                       new_id=ids)
    assert out.map_id == m.map_id and out.path_id is None and opened == [mat]
    m2 = map_of(env)
    assert [n.name for n in m2.children(la)] == ["向量", "矩阵"]
    assert m2.node(la).state == "expanded"


def test_answering_the_opening_probe_is_evidence_on_prerequisites_only(env):
    """The opening's answers land on prerequisite cells; a unit's teaches cells are untouched."""
    from tests.wikiapp.goal_script import answer_round  # the chat's ask_check (stage 3 T11)

    repo, s, ids = env
    res = goal(env)
    assert res.open and res.draft.rounds
    analyze(env, "ru_b", B_OUT)
    taught = set(st(env).readings[rid(env)].units[1].teaches)
    before = len(repo.aspect_log("u1"))
    assert res.draft.rounds[-1].pick is not None          # the open pick, asked in the chat
    answer_round(repo, CountingGateway(s.gateway()), "u1", res.draft.draft_id, [0],
                 now=NOW, new_id=ids)
    seen = {e.observation.cell.rpartition(".")[0] for e in repo.aspect_log("u1")[before:]
            if isinstance(e, Observe)}
    prereqs = {n.id for n in map_of(env).nodes}
    assert seen and seen <= prereqs and not seen & taught
