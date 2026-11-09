"""Writing a page hands the writer the material sections its concepts map to, and the record keeps
which materials it used (materials Task 6). ScriptWriter + MemorySourceExcerpts; no LLM."""

from dataclasses import replace

import pytest

from evowiki.sources.excerpts import MemorySourceExcerpts
from evowiki.sources.models import LinkRow
from evowiki.wikiapp.events import PageSource
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.wiki_schemas import ErrorEvent
from tests.sources.test_store import _sec, _src
from tests.wikiapp.fake_writer import writing
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_organize import page1, run, script_page

BOOK = "线性代数讲义"


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def materials(node: str = "c1") -> MemorySourceExcerpts:
    src = replace(_src("s1", status="ready"), title=BOOK)
    secs = [replace(_sec("sec11", 0, text="〔p.2〕向量相加。"), title="1.1 向量加法"),
            replace(_sec("sec12", 1, text="〔p.4〕数乘。"), title="1.2 数乘")]
    links = [LinkRow(node, "sec11", "s1", "teaches", "intake"),
             LinkRow(node, "sec12", "s1", "teaches", "intake")]
    return MemorySourceExcerpts({"u1": [(src, secs, links)]})


def write_with(env, sources):
    pid = page1(env)
    script_page(env[1])
    kit = replace(writing(env), sources=sources)
    items, _ = run(env, pid, writing=kit)
    return pid, items, kit.writer


def test_the_writer_gets_the_mapped_sections_and_the_record_keeps_the_material(env):
    pid, items, w = write_with(env, materials("c1"))
    assert items[-1][0] == "done"
    got = w.inputs[-1].sources
    assert [(e.source_id, e.section_id, e.label) for e in got] == [("s1", "sec11", "§1.1"),
                                                                    ("s1", "sec12", "§1.2")]
    rec = env[0].load("u1").state.pages[pid]
    assert rec.sources == (PageSource(source_id="s1", title=BOOK),)


def test_concepts_without_links_get_no_sources(env):
    pid, items, w = write_with(env, materials("other-node"))
    assert items[-1][0] == "done"
    assert w.inputs[-1].sources == ()
    assert env[0].load("u1").state.pages[pid].sources == ()


def test_a_rewrite_job_reads_the_materials_too(env):
    from evowiki.sources.reader import MemorySourceReader
    from tests.deep.fakes import DeepScript, simple_round
    from tests.deep.test_service import service
    from tests.wikiapp.test_page import organized
    pid = organized(env)
    ds = DeepScript().plan().write(*simple_round())
    svc = replace(service(env, ds), sources=materials("c1"),
                  reader=MemorySourceReader({"u1": []}))
    res = svc.run("u1", svc.start("u1", pid, "user"))
    assert res.job.stage == "done"
    first = next(r for r in ds.requests if r.purpose == "deep.write").messages[0].text
    assert f"《{BOOK}》§1.1 向量加法" in first
    tools = {t.name for t in next(r for r in ds.requests if r.purpose == "deep.write").tools}
    assert "read_source" in tools
    assert env[0].load("u1").state.pages[pid].sources == (PageSource(source_id="s1", title=BOOK),)


class Broken:
    def for_nodes(self, user_id, node_ids, parents=None):
        raise RuntimeError("db down")


def test_a_failing_material_read_fails_the_write(env):
    pid, items, w = write_with(env, Broken())
    assert items[-1][0] == "error"
    err = items[-1][1]
    assert isinstance(err, ErrorEvent) and err.code == "internal" and "db down" in err.detail
    assert w.inputs == []
    assert env[0].load("u1").state.pages[pid].page is None


# ---- Task 6b: citations checked before the page is stored ----

CITED = ("按分量相加〔线性代数讲义 p.2：「就是把对应的分量分别相加」〕；"
         "长度〔线性代数讲义 p.2：「资料里根本没有这一句话」〕。"
         "⟦和资料不同：通行的说法是 X；资料原话：「把对应的分量分别相加」⟧")


def cited_materials(node: str = "c1", offset: int | None = None) -> MemorySourceExcerpts:
    src = replace(_src("s1", status="ready"), title=BOOK, page_offset=offset)
    secs = [replace(_sec("sec11", 0, text="〔p.2〕两个向量相加，就是把对应的分量分别相加。\n〔p.3〕数乘。"),
                    title="1.1 向量加法", page_from=2, page_to=3)]
    return MemorySourceExcerpts({"u1": [(src, secs, [LinkRow(node, "sec11", "s1", "teaches",
                                                            "intake")])]})


def write_cited(env, sources):
    from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
    from tests.wikiapp.fake_writer import WRITE
    from tests.wikiapp.test_organize import OUTLINE, text
    pid = page1(env)
    s = env[1]
    s.push(ORGANIZE, OUTLINE)
    s.push(WRITE, text("s1", CITED), text("s2"), text("s3"))
    items, _ = run(env, pid, writing=replace(writing(env), sources=sources))
    return pid, items


def test_citations_are_checked_and_the_issues_kept_on_the_record(env):
    pid, items = write_cited(env, cited_materials())
    assert items[-1][0] == "done"
    rec = env[0].load("u1").state.pages[pid]
    body = rec.page.texts["s1"].body
    assert "按分量相加〔线性代数讲义 p.2｜2〕；长度。⟦和资料不同：通行的说法是 X⟧" in body
    assert "资料里根本没有" not in body and "资料原话" not in body
    assert [(i.kind, i.section_id) for i in rec.cite_issues] == [("verified", "s1"),
                                                                 ("quote_not_found", "s1"),
                                                                 ("differs_verified", "s1")]
    assert rec.cite_issues[0].mark == "〔线性代数讲义 p.2｜2〕" and rec.cite_issues[0].quote
    # what was streamed with the raw marks is sent again as committed
    last_s1 = [p for n, p in items if n == "section_delta" and p.section_id == "s1"][-1]
    assert last_s1.body == body


def test_the_printed_page_is_shown_when_the_material_has_an_offset(env):
    pid, _ = write_cited(env, cited_materials(offset=1))
    rec = env[0].load("u1").state.pages[pid]
    assert "〔线性代数讲义 p.1｜2〕" in rec.page.texts["s1"].body
    assert rec.sources == (PageSource(source_id="s1", title=BOOK, page_offset=1),)


class PagesBroken(MemorySourceExcerpts):
    def page_texts(self, user_id, source_id, pages):
        raise RuntimeError("pages down")


def test_a_failing_page_read_fails_the_write(env):
    m = cited_materials()
    pid, items = write_cited(env, PagesBroken(m._data))
    assert items[-1][0] == "error" and "pages down" in items[-1][1].detail
    assert not env[0].load("u1").state.pages[pid].has_body     # written so far, not finished


def test_a_one_section_rewrite_keeps_the_rest_and_refuses_a_copied_stored_mark(env):
    from evowiki.deep.contracts import RefineScope
    from tests.deep.fakes import DeepScript, finish, section, turn
    from tests.deep.test_service import service
    pid, _ = write_cited(env, cited_materials())
    before = env[0].load("u1").state.pages[pid]
    copied = "抄来的出处〔线性代数讲义 p.2｜2〕。"          # s1's verified mark, pasted into s2
    ds = DeepScript().plan().write(turn(section("s2", 1, body=copied)), turn(finish(1)))
    svc = service(env, ds)                                # no materials configured this time
    job = svc.start("u1", pid, "user")
    res = svc.run("u1", job, RefineScope(page_id=pid, sections=("s2",)))
    assert res.job.stage == "done"
    rec = env[0].load("u1").state.pages[pid]
    assert rec.page.texts["s2"].body.startswith("抄来的出处。")
    assert rec.page.texts["s1"] == before.page.texts["s1"]                 # untouched
    assert rec.sources == before.sources                                   # still cited by s1
    kinds = [(i.kind, i.section_id) for i in rec.cite_issues]
    assert kinds == [("verified", "s1"), ("quote_not_found", "s1"), ("differs_verified", "s1"),
                     ("unverified_stored", "s2")]


def test_a_rewrite_may_carry_over_the_sections_own_stored_mark(env):
    from evowiki.deep.contracts import RefineScope
    from tests.deep.fakes import DeepScript, finish, section, turn
    from tests.deep.test_service import service
    pid, _ = write_cited(env, cited_materials())
    old = env[0].load("u1").state.pages[pid].page.texts["s1"].body
    kept = "改写后仍按分量相加〔线性代数讲义 p.2｜2〕。"
    ds = DeepScript().plan().write(
        turn(section("s1", 1, body=kept, role="organizer", depth="quick", heading="从你会的出发")),
        turn(finish(1)))
    svc = replace(service(env, ds), sources=cited_materials())
    res = svc.run("u1", svc.start("u1", pid, "user"), RefineScope(page_id=pid, sections=("s1",)))
    assert res.job.stage == "done" and "〔线性代数讲义 p.2｜2〕" in old
    rec = env[0].load("u1").state.pages[pid]
    assert rec.page.texts["s1"].body.startswith(kept)
    # the carried mark keeps its verified record (the quote is only there); no new problem
    assert [i.kind for i in rec.cite_issues] == ["verified"]


# ---- final review #4: a rename or delete, then a rewrite, keeps the old marks resolvable ----

def renamed_materials(title: str = "新讲义") -> MemorySourceExcerpts:
    m = cited_materials()
    ((src, secs, links),) = m._data["u1"]
    return MemorySourceExcerpts({"u1": [(replace(src, title=title), secs, links)]})


def _rewrite(env, pid, sid, body, sources):
    from evowiki.deep.contracts import RefineScope
    from tests.deep.fakes import DeepScript, finish, section, turn
    from tests.deep.test_service import service
    ds = DeepScript().plan().write(turn(section(sid, 1, body=body)), turn(finish(1)))
    svc = replace(service(env, ds), sources=sources)
    res = svc.run("u1", svc.start("u1", pid, "user"), RefineScope(page_id=pid, sections=(sid,)))
    assert res.job.stage == "done"
    return env[0].load("u1").state.pages[pid]


def test_rename_then_rewrite_another_section_keeps_the_old_titles_source(env):
    pid, _ = write_cited(env, cited_materials())
    rec = _rewrite(env, pid, "s2", "另一节。", renamed_materials())
    assert "〔线性代数讲义 p.2｜2〕" in rec.page.texts["s1"].body               # untouched
    assert PageSource(source_id="s1", title=BOOK) in rec.sources              # old marks resolve
    assert PageSource(source_id="s1", title="新讲义") in rec.sources           # this write's


def test_rename_then_rewrite_the_cited_section_keeps_its_carried_mark_resolvable(env):
    pid, _ = write_cited(env, cited_materials())
    rec = _rewrite(env, pid, "s1", "改写后〔线性代数讲义 p.2｜2〕。", renamed_materials())
    assert rec.page.texts["s1"].body.startswith("改写后〔线性代数讲义 p.2｜2〕。")
    assert PageSource(source_id="s1", title=BOOK) in rec.sources


def test_delete_then_rewrite_the_cited_section_keeps_its_carried_mark_resolvable(env):
    pid, _ = write_cited(env, cited_materials())
    empty = MemorySourceExcerpts({"u1": []})                  # the material is gone
    rec = _rewrite(env, pid, "s1", "改写后〔线性代数讲义 p.2｜2〕。", empty)
    assert rec.page.texts["s1"].body.startswith("改写后〔线性代数讲义 p.2｜2〕。")
    assert rec.sources == (PageSource(source_id="s1", title=BOOK),)


def test_a_source_no_longer_cited_anywhere_is_dropped(env):
    pid, _ = write_cited(env, cited_materials())
    rec = _rewrite(env, pid, "s1", "改写后不再引用。", MemorySourceExcerpts({"u1": []}))
    assert rec.sources == ()


def test_a_verified_citation_gets_its_block_when_the_reader_gives_the_document(env):
    from evowiki.sources.document import Block, DocPage, SourceDocument
    doc = SourceDocument(kind="pdf", backend="mineru", version="4", pages=(DocPage(2),),
                         blocks=(Block("p2-b1", 2, "paragraph", "两个向量相加，就是把对应的分量分别相加。",
                                       bbox=(0.1, 0.2, 0.9, 0.3)),))

    class WithDocument(MemorySourceExcerpts):
        def document(self, user_id, source_id):
            return doc

    base = cited_materials()
    pid, _ = write_cited(env, WithDocument(base._data))
    rec = env[0].load("u1").state.pages[pid]
    (v,) = [i for i in rec.cite_issues if i.kind == "verified"]
    assert v.locator["block_id"] == "p2-b1" and v.locator["page"] == 2
    assert v.locator["char_end"] > v.locator["char_start"]
