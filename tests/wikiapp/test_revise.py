"""Page revisions from materials (`wikiapp.revise`): the whole-page swap. Pure."""

from datetime import UTC, datetime

import pytest

from evowiki.deep.contracts import RefineDraft, RefinedSection, RefinePlan
from evowiki.goals.contracts import (
    CheckItem,
    OutlineSection,
    PageOutline,
    SectionRole,
    SectionText,
)
from evowiki.goals.fold import Page
from evowiki.wikiapp.events import PageRecord
from evowiki.wikiapp.revise import revised_record

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
PLAN = RefinePlan(known="k", observable="o", one_at_a_time=("a",), contrast="c", spoken_answer="s")


def page_record(page_id="pg1", revisions=0) -> PageRecord:
    """s1 开头 / s2 均值怎么算 / s3 旧的例子 (explain) and q1 (check)."""
    outline = PageOutline(
        page_id=page_id, goal="g", concepts=("c1",), targets=("c1",),
        sections=(OutlineSection(id="s1", heading="开头", role=SectionRole.EXPLAIN),
                  OutlineSection(id="s2", heading="均值怎么算", role=SectionRole.EXPLAIN),
                  OutlineSection(id="s3", heading="旧的例子", role=SectionRole.EXPLAIN),
                  OutlineSection(id="q1", heading="检查一下", role=SectionRole.CHECK, check=CheckItem(
                      cell="c1.procedure", stem="?", options=("a", "b", "c"), answer=0))))
    page = Page(outline=outline, states={}, texts={
        "s1": SectionText(section_id="s1", body="第一行\n第二行\n", cache_key="a"),
        "s2": SectionText(section_id="s2", body="旧的一句。\n旧的两句。", cache_key="b"),
        "s3": SectionText(section_id="s3", body="旧例子。", cache_key="c")})
    return PageRecord(page_id=page_id, title="均值", goal="g", concepts=("c1",),
                      targets=("c1",), page=page, created_at=NOW, revisions=revisions)


def sec(sid, body, heading=None, after=None) -> RefinedSection:
    return RefinedSection(section_id=sid, after=after, heading=heading or f"标题{sid}",
                          role="explain", depth="quick", cue="提示", body=body)


def refine(page_id="pg1", *sections) -> RefineDraft:
    return RefineDraft(page_id=page_id, plan=PLAN, sections=sections or (sec("s1", "正文"),))


def test_revised_record_replaces_the_whole_page_and_counts_the_revision():
    rec = page_record(revisions=2)
    draft = refine("pg1", sec("s1", "新开头", heading="开头"),
                   sec("s2", "新的均值", heading="均值怎么算"),
                   sec("s9", "新加的一段", heading="新段", after="s3"))
    out = revised_record(rec, draft, [], now=NOW, job_id="run_1", model="m")
    assert out.revisions == 3
    ids = [s.id for s in out.page.outline.sections]
    assert "s3" not in ids and "s3" not in out.page.texts      # whole page, not a merge
    assert [i for i in ids if i != "q1"] == ["s1", "s2", "s9"]
    assert "q1" in ids                                        # the writer never writes checks
    assert {sid: out.page.texts[sid].body for sid in ("s1", "s2", "s9")} == {
        "s1": "新开头", "s2": "新的均值", "s9": "新加的一段"}
    assert out.written is not None and out.written.kind == "rewrite"
    assert out.written.job_id == "run_1"


def test_revised_record_refuses_a_user_note():
    note = page_record().model_copy(update={"page": None})
    with pytest.raises(ValueError):
        revised_record(note, refine(), [], now=NOW, job_id="r", model="m")


def test_a_rebuild_starts_the_revision_count_again():
    rec = page_record(revisions=5)
    draft = refine("pg1", sec("s1", "重写的开头", heading="开头"))
    assert revised_record(rec, draft, [], now=NOW, job_id="r", model="m").revisions == 6
    assert revised_record(rec, draft, [], now=NOW, job_id="r", model="m",
                          rebuild=True).revisions == 0
