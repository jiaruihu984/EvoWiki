"""Materials, stage 3 (spec 2026-10-28-on-demand-decomposition-design §2.5, §6 第 3 阶段): the
first-level blocks keep their section links (`source_link` by "blocks", many-to-many); entering a
block gets a TOC trimmed to its linked sections and writes its parts' finer links (by "enter")
in the same commit; a point with no links of its own uses its parent block's for the page
writer; a goal's material use (reference / follow / understand) is decided by the §2.5 table and
stored on the map apart from the material's own use. Scripted LLM only."""

from datetime import UTC, datetime

import pytest

from evowiki.goals.blocks import Block, block_links, blocks_prompt
from evowiki.goals.intake import NodeSectionLink, SourceToc, TocSection
from evowiki.sources.excerpts import MemorySourceExcerpts, with_parents
from evowiki.sources.models import LinkRow, SectionRow, SourceRow
from evowiki.wikiapp.goal import create_goal_from_blocks, enter_region, stop_draft
from evowiki.wikiapp.goal_sources import Material, material_use, trimmed_toc
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.refine_apply import concept_ancestors
from evowiki.wikiapp.repo import MemoryRepo
from tests.wikiapp.goal_script import auto_probe, plan_and_make, push_no_question
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids

INTAKE = "goals.intake"
ENTER = "goals.enter"          # S2: entering a block (its replies are pushed in call order)
BOOK = SourceToc(source_id="s1", title="黑壳虾饲养讲义", sections=(
    TocSection(id="x1", label="§1", title="1 认识黑壳虾", chars=0),
    TocSection(id="x11", label="§1.1", title="1.1 外形", depth=1),
    TocSection(id="x12", label="§1.2", title="1.2 习性", depth=1),
    TocSection(id="x2", label="§2", title="2 水质", chars=0),
    TocSection(id="x21", label="§2.1", title="2.1 换水", depth=1),
    TocSection(id="x22", label="§2.2", title="2.2 pH", depth=1),
    TocSection(id="x3", label="§3", title="3 习题")))
GOAL = "我想养黑壳虾"


@pytest.fixture
def env():
    s = Script()
    auto_probe(s)
    return MemoryRepo(["u1"]), s, Ids()


def gw(env):
    return CountingGateway(env[1].gateway())


def made(env, blocks, **kw):
    repo, _, ids = env
    res = create_goal_from_blocks(repo, gw(env), "u1", GOAL, blocks, now=NOW, new_id=ids,
                                  sources=(BOOK,), **kw)
    stop_draft(repo, gw(env), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    return res


BLOCKS = (Block(name="认识黑壳虾", covers=("外形",), sections=("§1.1", "§1.2")),
          Block(name="把水养好", covers=("换水", "pH"), target=True,
                sections=("§2", "§1.2"), exercises=("§3",)))


# --- the first level keeps its section links ------------------------------------------------

def test_blocks_with_sections_store_block_links_many_to_many(env):
    res = made(env, BLOCKS)
    st = env[0].load("u1").state
    ids = [n.id for n in st.maps[res.draft.map_id].nodes]
    assert set(res.links) == {
        NodeSectionLink(node_id=ids[0], section_id="x11", source_id="s1", role="teaches", by="blocks"),
        NodeSectionLink(node_id=ids[0], section_id="x12", source_id="s1", role="teaches", by="blocks"),
        NodeSectionLink(node_id=ids[1], section_id="x2", source_id="s1", role="teaches", by="blocks"),
        NodeSectionLink(node_id=ids[1], section_id="x12", source_id="s1", role="teaches", by="blocks"),
        NodeSectionLink(node_id=ids[1], section_id="x3", source_id="s1", role="exercises",
                        by="blocks")}
    # the goal's own use is on the map (follow by default with materials), not the material's
    assert st.maps[res.draft.map_id].material_use == "follow"
    assert res.draft.source_use == "learn"


def test_a_label_is_matched_loosely_and_an_unknown_one_is_dropped():
    blocks = [Block(name="甲", sections=("1.1", "§ 2.2", "2.1 换水", "§9"))]
    links, dropped = block_links(blocks, [BOOK])
    assert [x[1] for x in links] == ["x11", "x22", "x21"]
    assert [(d.reason, d.item["section"], d.item["name"]) for d in dropped] == [
        ("unknown_section", "§9", "甲")]


def test_the_model_s_sections_reach_the_links_through_s1(env):
    repo, s, ids = env
    s.push(INTAKE, {"title": "黑壳虾", "blocks": [
        {"name": "认识黑壳虾", "summary": "它是什么虾", "sections": ["§1.1"]},
        {"name": "把水养好", "summary": "水", "sections": ["§2.1", "§2.2"], "exercises": ["§3"],
         "target": True}]})
    push_no_question(s)
    res = plan_and_make(repo, gw(env), "u1", GOAL, now=NOW, new_id=ids, sources=(BOOK,),
                        mode="reference")
    assert {(x.section_id, x.role) for x in res.links} == {
        ("x11", "teaches"), ("x21", "teaches"), ("x22", "teaches"), ("x3", "exercises")}
    assert repo.load("u1").state.maps[res.map_id].material_use == "reference"
    p = s.requests(INTAKE)[0].prompt
    assert "只作参考" in p and "不按资料的目录排" in p and "先后顺序跟资料走" not in p


def test_reference_blocks_keep_the_model_s_order_not_the_table_of_contents(env):
    """Only a reference: the blocks are the goal's, in the model's order, even when it runs
    against the TOC; the sections are only linked."""
    res = made(env, (Block(name="把水养好", sections=("§2",), target=True),
                     Block(name="认识黑壳虾", sections=("§1",))), material_use="reference")
    m = env[0].load("u1").state.maps[res.draft.map_id]
    assert [n.name for n in m.nodes] == ["把水养好", "认识黑壳虾"]
    assert m.material_use == "reference"
    assert [x.section_id for x in res.links] == ["x2", "x1"]


def test_the_follow_prompt_keeps_the_material_order_rules():
    p = blocks_prompt(GOAL, (), (), sources=(BOOK,))
    assert "先后顺序跟资料走" in p and "只作参考" not in p


# --- entering refines the links ---------------------------------------------------------------

class FakeEnterSources:
    def __init__(self, links: dict[str, list[str]]):
        self.links, self.asked, self.written = links, [], []

    def block_tocs(self, user_id, source_ids, node_ids):
        self.asked.append((tuple(source_ids), tuple(node_ids)))
        first = next((n for n in node_ids if self.links.get(n)), None)
        t = trimmed_toc(BOOK, set(self.links[first])) if first else None
        return [t] if t else []

    def write_links(self, conn, user_id, links, now):
        self.written.append(tuple(links))


def test_trimmed_toc_keeps_the_linked_sections_and_their_subsections():
    t = trimmed_toc(BOOK, {"x2", "x11"})
    assert [s.id for s in t.sections] == ["x11", "x2", "x21", "x22"]
    assert trimmed_toc(BOOK, {"nope"}) is None


@pytest.mark.parametrize("use,said,unsaid", [
    ("follow", "按资料来拆", "只作参考"),
    ("reference", "只作参考", "按资料来拆")])
def test_entering_a_block_passes_its_sections_and_writes_the_parts_links(env, use, said, unsaid):
    repo, s, ids = env
    res = made(env, BLOCKS, material_use=use)
    m = repo.load("u1").state.maps[res.draft.map_id]
    water = m.nodes[1].id
    src = FakeEnterSources({water: [x.section_id for x in res.links if x.node_id == water
                                    and x.role == "teaches"]})
    s.push(INTAKE, {"parts": [
        {"name": "换水", "summary": "多久换", "atomic": True, "sections": ["§2.1"]},
        {"name": "pH", "summary": "酸碱", "atomic": True, "sections": ["§2.2", "§9"],
         "exercises": ["§3"]}]})
    out = enter_region(repo, gw(env), "u1", m.map_id, water, now=NOW, new_id=ids, sources=src)
    p = s.requests(ENTER)[-1].prompt
    assert said in p and unsaid not in p
    assert "§2.1 2.1 换水" in p and "§1.1 1.1 外形" not in p          # trimmed to this block
    assert src.asked == [(("s1",), (water,))]
    kids = {n.name: n.id for n in repo.load("u1").state.maps[m.map_id].children(water)}
    assert set(out.links) == {
        NodeSectionLink(node_id=kids["换水"], section_id="x21", source_id="s1", role="teaches",
                        by="enter"),
        NodeSectionLink(node_id=kids["pH"], section_id="x22", source_id="s1", role="teaches",
                        by="enter")}                     # §9 unknown, §3 not in the trimmed TOC
    assert src.written == [out.links]


def test_entering_without_linked_sections_passes_no_material(env):
    repo, s, ids = env
    res = made(env, (Block(name="认识黑壳虾"), Block(name="把水养好", target=True)))
    m = repo.load("u1").state.maps[res.draft.map_id]
    src = FakeEnterSources({})
    s.push(INTAKE, {"parts": [{"name": "换水", "atomic": True}, {"name": "pH", "atomic": True}]})
    out = enter_region(repo, gw(env), "u1", m.map_id, m.nodes[1].id, now=NOW, new_id=ids,
                       sources=src)
    assert "资料" not in s.requests(ENTER)[-1].prompt.split("怎样拆最好")[0]
    assert out.links == () and src.written == []


# --- the page writer falls back to the parent block's links -----------------------------------

T = datetime(2026, 10, 28, tzinfo=UTC)
ROW = SourceRow(source_id="s1", user_id="u1", kind="pdf", title="黑壳虾饲养讲义", sha256="x",
                bytes=1, pages=10, status="ready", error=None, unreadable_pages=(),
                toc_mode="outline", progress={}, origin=None, created_at=T, updated_at=T,
                deleted_at=None)
SECS = [SectionRow(section_id=x, source_id="s1", parent_id=None, ord=i, depth=0, title=x,
                   page_from=None, page_to=None, text=f"{x} 的原文", tokens=3, summary=None,
                   read_by="text") for i, x in enumerate(("x2", "x21", "x22"))]


def test_a_point_without_links_gets_its_parent_block_s_sections(env):
    repo, s, ids = env
    res = made(env, BLOCKS)
    m = repo.load("u1").state.maps[res.draft.map_id]
    water = m.nodes[1].id
    s.push(INTAKE, {"parts": [{"name": "换水", "atomic": True}, {"name": "pH", "atomic": True}]})
    enter_region(repo, gw(env), "u1", m.map_id, water, now=NOW, new_id=ids)
    st = repo.load("u1").state
    change, ph = (n.id for n in st.maps[m.map_id].children(water))
    links = [LinkRow(node_id=water, section_id="x2", source_id="s1", role="teaches", by="blocks"),
             LinkRow(node_id=ph, section_id="x22", source_id="s1", role="teaches", by="enter")]
    ex = MemorySourceExcerpts({"u1": [(ROW, SECS, links)]})
    parents = concept_ancestors(st, [change, ph])
    assert parents[change][0] == water
    got = ex.for_nodes("u1", [change], parents=parents)
    assert [(e.section_id, e.text) for e in got] == [("x2", "x2 的原文")]
    assert [e.section_id for e in ex.for_nodes("u1", [ph], parents=parents)] == ["x22"]
    assert ex.for_nodes("u1", [change]) == []                        # no fallback asked: none


def test_with_parents_takes_the_nearest_ancestor_that_has_links():
    up = LinkRow(node_id="a", section_id="s", source_id="s1", role="teaches", by="blocks")
    mid = LinkRow(node_id="b", section_id="t", source_id="s1", role="teaches", by="enter")
    assert with_parents([up, mid], ["c"], {"c": ("b", "a")}) == [mid]
    assert with_parents([up], ["c"], {"c": ("b", "a")}) == [up]


# --- the use table (§2.5): six inputs ---------------------------------------------------------

def _material(*titles: str, pages: int = 10) -> Material:
    row = ROW.__class__(**{**ROW.__dict__, "pages": pages})
    secs = [SectionRow(section_id=f"y{i}", source_id="s1", parent_id=None, ord=i, depth=0,
                       title=t, page_from=None, page_to=None, text="正文。", tokens=1,
                       summary=None, read_by="text") for i, t in enumerate(titles)]
    return Material(row, secs, BOOK)


TEXTBOOK = _material("前言", "第1章 向量", "第2章 矩阵", "习题", "索引", pages=300)
PAPER = _material("摘要", "1 引言", "2 方法", "3 实验", "参考文献")


@pytest.mark.parametrize("goal,materials,use,default", [
    ("学机器学习，这本书作为参考", [TEXTBOOK], "reference", False),
    ("按这本教材学机器学习", [TEXTBOOK], "follow", False),
    ("帮我读懂这篇论文", [PAPER], "understand", False),
    ("学机器学习", [TEXTBOOK], "reference", True),
    ("", [TEXTBOOK], "follow", True),
    ("", [PAPER], None, False)])
def test_the_use_table(goal, materials, use, default):
    got = material_use(goal, None, materials)
    assert (got.use, got.default) == (use, default)


def test_a_stated_use_from_the_assistant_wins_and_no_material_has_no_use():
    assert material_use("", "understand", [TEXTBOOK]).use == "understand"
    assert material_use("学机器学习", "follow", [PAPER]).use == "follow"
    assert material_use("学机器学习", None, []) is None
