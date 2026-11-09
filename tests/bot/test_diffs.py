"""ActionOut.diff: assistant edits, page revisions, RSI changes, nothing for the rest."""
import dataclasses
from datetime import UTC, datetime

from evowiki.bot.bot_schemas import ActionOut
from evowiki.bot.diffs import Names, diff_for
from evowiki.goals.contracts import OutlineSection, PageOutline, SectionRole, SectionText
from evowiki.goals.fold import Page
from evowiki.store.bot import ActionRow
from evowiki.wikiapp.events import PageRecord
from evowiki.wikiapp.state import WikiState

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def row(kind="assistant_edit", content=None, **kw):
    base = {"action_id": "act_1", "user_id": "u1", "kind": kind, "scope": "structure",
            "level": "auto_undo", "status": "executed", "source": "assistant", "summary": "收起这页",
            "content": content or {}, "content_hash": "h", "idempotency_key": "k",
            "created_at": NOW}
    return ActionRow(**{**base, **kw})


def state_with_page():
    outline = PageOutline(
        page_id="pg1", goal="g", concepts=("c1",), targets=("c1",),
        sections=(OutlineSection(id="s1", heading="开头", role=SectionRole.EXPLAIN),
                  OutlineSection(id="s2", heading="均值怎么算", role=SectionRole.EXPLAIN)))
    page = Page(outline=outline, states={}, texts={
        "s1": SectionText(section_id="s1", body="第一行\n第二行\n", cache_key="a"),
        "s2": SectionText(section_id="s2", body="旧的一句。\n旧的两句。", cache_key="b")})
    rec = PageRecord(page_id="pg1", title="均值", goal="g", concepts=("c1",),
                     targets=("c1",), page=page, created_at=NOW)
    return WikiState(pages={"pg1": rec}, page_order=("pg1",))


def kinds(lines):
    return [(ln["kind"], ln["text"]) for ln in lines]


def edit_row(**kw):
    c = {"kind": "rewrite_section", "page_id": "pg1", "section_id": "s2", "heading": "均值怎么算",
         "cells": [], "text": "新的一句。\n新的两句。", "why": "更清楚"}
    return row(kind="assistant_edit", scope="page_body", source="assistant", content=c,
               status="proposed", **kw)


def test_assistant_rewrite_shows_replaced_text_as_del_and_new_as_add():
    lines = diff_for(edit_row(), Names(state_with_page()))
    assert kinds(lines) == [("ctx", "均值怎么算"), ("del", "旧的一句。"), ("del", "旧的两句。"),
                            ("add", "新的一句。"), ("add", "新的两句。")]


def test_an_executed_rewrite_takes_the_old_text_from_its_undo_data():
    r = edit_row(undo={"prev_text": {"body": "更早的话"}})
    r = ActionRow(**{**r.__dict__, "status": "executed"})
    assert kinds(diff_for(r, Names(state_with_page())))[:2] == [("ctx", "均值怎么算"),
                                                                 ("del", "更早的话")]


def test_assistant_insert_after_starts_with_the_previous_sections_last_line():
    c = {"kind": "add_section", "page_id": "pg1", "section_id": "s1", "heading": "补一段",
         "cells": [], "text": "新增内容", "why": "缺这个"}
    r = row(kind="assistant_edit", scope="page_body", content=c)
    lines = diff_for(r, Names(state_with_page()))
    assert kinds(lines) == [("ctx", "第二行"), ("ctx", "补一段"), ("add", "新增内容")]


def test_preference_and_rsi_rows_have_no_diff_and_action_out_carries_it():
    assert diff_for(row(kind="preference", scope="preference"), Names(None)) == []
    assert diff_for(row(kind="rsi_patch", scope="rsi"), Names(None)) == []
    out = ActionOut.of(edit_row(), diff_for(edit_row(), Names(state_with_page())))
    assert [d.kind for d in out.diff] == ["ctx", "del", "del", "add", "add"]


def test_an_rsi_change_waiting_for_the_user_shows_the_diff_it_was_proposed_with():
    diff = [{"kind": "ctx", "text": "学习状态评估"}, {"kind": "del", "text": "自我评估权重 0.60"},
            {"kind": "add", "text": "自我评估权重 0.42"}, {"kind": "bogus", "text": "x"}, "junk"]
    r = row(kind="rsi_patch", scope="teaching_params", status="proposed", level="ask",
            content={"patch_id": "p1", "track": "A", "diff": diff})
    assert diff_for(r, Names(None)) == diff[:3]
    m = row(kind="rsi_method", scope="rsi", content={"change_id": "c1", "diff": "not a list"})
    assert diff_for(m, Names(None)) == []


def revise_row(status="proposed", undo=None, state="ready"):
    from evowiki.deep.contracts import RefineDraft, RefinedSection, RefinePlan
    from evowiki.wikiapp.revise import RevisionDraft

    def sec(sid, heading, body):
        return RefinedSection(section_id=sid, heading=heading, role="explain", depth="quick",
                              cue="提示", body=body)
    plan = RefinePlan(known="k", observable="o", one_at_a_time=("a",), contrast="c",
                      spoken_answer="s")
    draft = RevisionDraft(
        page_id="pg1", base_version="v1",
        draft=RefineDraft(page_id="pg1", plan=plan, sections=(
            sec("s2", "均值怎么算", "新的一句。"), sec("s9", "新段", "新加的话"))))
    content = {"state": state, "confirm": True, "page_id": "pg1", "source_ids": [],
               "run_id": "r1", "detail": "",
               "draft": draft.model_dump(mode="json") if state == "ready" else None}
    return row(kind="page_revise", scope="page_body", source="user", level="ask", status=status,
               content=content, undo=undo)


def test_page_revise_lists_the_changed_added_and_dropped_sections():
    lines = diff_for(revise_row(), Names(state_with_page()))
    assert kinds(lines) == [
        ("ctx", "均值怎么算"), ("del", "旧的一句。"), ("del", "旧的两句。"), ("add", "新的一句。"),
        ("ctx", "新段"), ("add", "新加的话"),
        ("ctx", "开头"), ("del", "第一行"), ("del", "第二行")]


def test_an_executed_page_revise_diffs_against_the_page_it_replaced():
    prev = state_with_page().pages["pg1"].model_dump(mode="json")
    prev["page"]["texts"]["s1"]["body"] = "更早的开头"
    lines = diff_for(revise_row(status="executed", undo={"prev_record": prev}), Names(None))
    assert kinds(lines)[-2:] == [("ctx", "开头"), ("del", "更早的开头")]


def test_a_page_revise_still_writing_or_failed_has_no_diff():
    assert diff_for(revise_row(state="writing"), Names(state_with_page())) == []
    assert diff_for(revise_row(state="failed"), Names(state_with_page())) == []


def _embed_state(body: str):
    st = state_with_page()
    rec = st.pages["pg1"]
    texts = {**rec.page.texts, "s2": rec.page.texts["s2"].model_copy(update={"body": body})}
    rec = rec.model_copy(update={"page": rec.page.model_copy(update={"texts": texts})})
    return WikiState(pages={"pg1": rec}, page_order=("pg1",))


def _interactive_row(body: str, *, placed: bool):
    from evowiki.deep.contracts import (
        InteractiveDraft,
        Probe,
        RefineDraft,
        RefinedSection,
        RefinePlan,
    )
    from evowiki.wikiapp.revise import RevisionDraft
    plan = RefinePlan(known="k", observable="o", one_at_a_time=("a",), contrast="c",
                      spoken_answer="s")
    secs = (RefinedSection(section_id="s1", heading="开头", role="explain", depth="quick",
                           cue="提示", body="第一行\n第二行"),
            RefinedSection(section_id="s2", heading="均值怎么算", role="explain", depth="quick",
                           cue="提示", body=body))
    inter = (InteractiveDraft(interactive_id="z-demo", section_id="s2", title="均值演示",
                              learning_question="q", html="<p>x</p>", height=400,
                              probes=(Probe(target="#b", action="click", expect_change="#o"),)),
             ) if placed else ()
    draft = RevisionDraft(page_id="pg1", base_version="v1", draft=RefineDraft(
        page_id="pg1", plan=plan, sections=secs, interactives=inter))
    return row(kind="page_revise", scope="page_body", source="user", level="ask",
               status="proposed", content={"state": "ready", "page_id": "pg1",
                                           "draft": draft.model_dump(mode="json")})


EMBED = ("讲一句。\n```embedded-html\ntype: embedded-html\nid: z-demo\nsrc: assets/ia_1.html\n"
         "title: 均值演示\nheight: 400\n```")


def test_an_interactive_the_draft_keeps_is_not_a_change():
    from evowiki.bot.diffs import revision_view
    names = Names(_embed_state(EMBED))
    for r in (_interactive_row("讲一句。\n{{interactive:z-demo}}", placed=True),   # placeholder
              _interactive_row(EMBED, placed=False)):                            # copied block
        assert diff_for(r, names) == []
        view = revision_view(r, names)
        assert view.sections == () and view.unchanged == 2
    evoui = "讲一句。\n```evoui\n{\"type\": \"evoui\", \"title\": \"均值演示\"}\n```"
    assert diff_for(_interactive_row(evoui, placed=False), names) == []


def test_revision_view_gives_each_changed_section_its_old_and_new_text():
    from evowiki.bot.diffs import revision_view
    view = revision_view(revise_row(), Names(state_with_page()))
    got = [(c.section_id, c.status, c.old_heading, c.old_body, c.new_body) for c in view.sections]
    assert got == [("s2", "changed", "均值怎么算", "旧的一句。\n旧的两句。", "新的一句。"),
                   ("s9", "added", None, "", "新加的话"),
                   ("s1", "removed", "开头", "第一行\n第二行\n", "")]
    assert view.unchanged == 0
    assert revision_view(revise_row(state="writing"), Names(state_with_page())) is None


def _revise_row_with(sections, *, sources=(), rebuild=False, **kw):
    from evowiki.deep.contracts import RefineDraft, RefinedSection, RefinePlan
    from evowiki.wikiapp.revise import RevisionDraft, RevisionSource

    plan = RefinePlan(known="k", observable="o", one_at_a_time=("a",), contrast="c",
                      spoken_answer="s")
    draft = RevisionDraft(
        page_id="pg1", base_version="v1", rebuild=rebuild,
        sources=tuple(RevisionSource(source_id=f"src{i}", title=t) for i, t in enumerate(sources)),
        draft=RefineDraft(page_id="pg1", plan=plan, sections=tuple(
            RefinedSection(section_id=sid, heading=h, role="explain", depth="quick", cue="提示",
                           body=b) for sid, h, b in sections)))
    content = {"state": "ready", "confirm": True, "page_id": "pg1", "source_ids": [],
               "run_id": "r1", "detail": "", "draft": draft.model_dump(mode="json")}
    base = {"kind": "page_revise", "scope": "page_body", "source": "user", "level": "ask",
            "status": "proposed", "summary": "按资料改「均值」", "why": "", "content": content}
    return row(**{**base, **kw})


def test_a_section_that_differs_only_in_whitespace_is_neither_counted_nor_diffed():
    from evowiki.bot.diffs import revision_view
    # s1 only re-indented / re-wrapped (a code block's spaces), s2 really changed
    r = _revise_row_with([("s1", "开头", "第一行\n\n  第二行  \n"),
                          ("s2", "均值怎么算", "新的一句。")])
    view = revision_view(r, Names(state_with_page()))
    assert [c.section_id for c in view.sections] == ["s2"] and view.unchanged == 1
    assert [t for k, t in kinds(diff_for(r, Names(state_with_page()))) if k == "ctx"] == ["均值怎么算"]


def test_a_page_revise_change_row_reads_like_the_demo():
    from evowiki.bot.change_source import change_out
    from evowiki.bot.diffs import revise_label
    n = Names(state_with_page())
    one = _revise_row_with([("s1", "开头", "第一行\n第二行"), ("s2", "均值怎么算", "新的一句。")],
                           sources=["线性代数讲义.pdf"], source="bot")
    assert revise_label(one, n) == ("「均值」按《线性代数讲义》更新了 1 节",
                                    "《线性代数讲义》讲到了这一页已有的内容")
    # a rejected draft is listed too (faint 已拒绝): the same words
    out = change_out(dataclasses.replace(one, status="rejected"), names=n)
    assert (out.summary, out.why) == revise_label(one, n)
    two = _revise_row_with([("s2", "均值怎么算", "新的一句。"), ("s9", "新段", "新加的")],
                           sources=["讲义", "笔记"], source="assistant")
    assert revise_label(two, n) == ("「均值」按《讲义》等 2 份资料更新了 3 节",
                                    "你让我按《讲义》《笔记》改这一页")
    mine = _revise_row_with([("s2", "均值怎么算", "新的一句。")], why="我想要更多例子",
                            rebuild=False)
    assert revise_label(mine, n)[1] == "我想要更多例子"
    rebuilt = _revise_row_with([("s2", "均值怎么算", "新的一句。")], rebuild=True)
    assert revise_label(rebuilt, n)[1].startswith("这一页按资料改过好几次了")
    assert revise_label(rebuilt, n)[0] == "「均值」按资料更新了 2 节"


def test_an_executed_page_revise_row_counts_against_the_page_it_replaced():
    from evowiki.bot.change_source import change_out
    prev = state_with_page().pages["pg1"].model_dump(mode="json")
    prev["title"] = "均值（旧名）"
    r = _revise_row_with([("s1", "开头", "第一行\n第二行"), ("s2", "均值怎么算", "新的一句。")],
                         sources=["讲义"], status="executed", undo={"prev_record": prev})
    # no wiki state: the title and the old text come from the replaced page
    assert change_out(r).summary == "「均值（旧名）」按《讲义》更新了 1 节"


def test_a_page_revise_without_a_draft_keeps_its_stored_words():
    from evowiki.bot.change_source import change_out
    out = change_out(revise_row(status="failed", state="failed"), names=Names(state_with_page()))
    assert out.summary == "收起这页"


def test_shown_body_reads_a_stored_blocks_title_from_its_asset():
    from evowiki.bot.diffs import shown_body
    from evowiki.deep.evoui import asset_fence
    body = f"前文\n{asset_fence('a1')}\n后文"
    names = Names(None, {"a1": "练熟：梯度下降"}.get)
    assert shown_body(body, {}, names.asset) == "前文\n〔互动：练熟：梯度下降〕\n后文"
    # an unknown asset, or no lookup at all: the plain 互动
    assert shown_body(asset_fence("zz"), {}, names.asset) == "〔互动：互动〕"
    assert shown_body(asset_fence("a1"), {}) == "〔互动：互动〕"
