import pytest

from evowiki.agent.read_page import PINNED, ReadPageTool, paragraph_spans
from evowiki.assistant.tools.propose import (
    ProposePageEditTool,
    change_evidence,
    formulas_gone,
    without_heading,
)
from evowiki.goals.contracts import SectionState
from evowiki.wikiapp.page_reader import InMemoryPageStore

ADD = {"kind": "add_section", "section_id": "s2", "heading": "一个例子",
       "text": "身高 180，均值 170，标准差 5，z=2。", "why": "用户刚问过怎么算",
       "cue": "身高 180，均值 170，标准差 5，z 是多少？", "answer": "z=2。",
       "cells": ["zscore.procedure"], "paragraphs": []}


class RecordingStore:
    """Wraps a store and fails the test on anything but get/all."""

    def __init__(self, inner):
        self._inner, self.calls = inner, []

    def get(self, user_id, page_id):
        self.calls.append("get")
        return self._inner.get(user_id, page_id)

    def all(self, user_id):
        self.calls.append("all")
        return self._inner.all(user_id)

    def __getattr__(self, name):
        raise AssertionError(f"propose touched {name}")


def test_add_section_draft(store, ctx):
    before = store.get("u1", "p1").model_dump_json()
    rec = RecordingStore(store)
    r = ProposePageEditTool(rec).run(ADD, ctx)
    assert r.error is None and r.proposal is not None
    d = r.proposal
    assert (d.kind, d.page_id, d.section_id, d.cells) == ("add_section", "p1", "s2",
                                                          ("zscore.procedure",))
    assert "确认" in r.content
    assert set(rec.calls) <= {"get", "all"}
    assert store.get("u1", "p1").model_dump_json() == before     # nothing written


def test_rewrite_needs_existing_section(store, ctx):
    t = ProposePageEditTool(store)
    ok = t.run({**ADD, "kind": "rewrite_section", "section_id": "s1", "paragraphs": []}, ctx)
    assert ok.error is None and ok.proposal.kind == "rewrite_section"
    assert t.run({**ADD, "kind": "rewrite_section", "section_id": None}, ctx).error
    assert t.run({**ADD, "kind": "rewrite_section", "section_id": "zz"}, ctx).error


def test_errors(store, ctx):
    t = ProposePageEditTool(store)
    assert t.run({**ADD, "page_id": "nope"}, ctx).error
    assert t.run({**ADD, "page_id": "p9"}, ctx).error                       # other user's page
    assert t.run({**ADD, "section_id": "zz"}, ctx).error
    assert t.run({**ADD, "cells": ["nope.term"]}, ctx).error                # not on the page
    assert t.run({**ADD, "cells": ["zscore.bogus"]}, ctx).error
    assert t.run({**ADD, "kind": "delete_section"}, ctx).error
    assert t.run({**ADD, "text": "x" * 4001}, ctx).error                    # too long: no clip
    assert t.run(ADD, ctx.model_copy(update={"page_id": None})).error


def test_a_character_box_redirects_to_the_section_illustration_tool(store, ctx):
    result = ProposePageEditTool(store).run({**ADD, "text": "┌────┐\n│ 均值 │\n└────┘"}, ctx)
    assert result.proposal is None and result.error
    assert "illustrate_section" in result.content


def test_spec_is_propose():
    s = ProposePageEditTool(InMemoryPageStore({})).spec
    assert s.name == "propose_page_edit" and s.effect == "propose"
    assert len(s.description) <= 300 and "提议卡片" in s.description
    props = s.parameters["properties"]
    assert "概念.层面" in props["cells"]["description"]
    assert "zscore.procedure" in props["cells"]["description"]
    assert "rewrite_section" in props["section_id"]["description"]
    assert "add_section" in props["section_id"]["description"]
    assert props["page_id"].get("description")


def test_the_result_counts_what_a_rewrite_drops():
    old = "我手改的一句。想知道谁的朋友最多：数边。\n\n回到上一页的网络。\n\n第三段。"
    said = change_evidence(old, "想知道谁的朋友最多：数边。\n\n第三段。")
    assert "原有 3 个自然段，草稿 2 个" in said
    assert "「我手改的一句。」" in said and "「回到上一页的网络。」" in said
    assert "「第三段。」" not in said and "「想知道" not in said
    assert "都还在" in change_evidence("一句。\n\n两句。", "一句。\n两句。 ")


def test_a_formula_lost_is_named_in_the_evidence():
    old = "写成式子：\n\n$$k_i = \\#\\{ e : i\\in e \\}$$\n\n这里 $k_i$ 是度。"
    assert formulas_gone(old, "这里 $k_i$ 是度。") == ["$$k_i = \\#\\{ e : i\\in e \\}$$"]
    assert formulas_gone(old, old.replace(" ", "")) == []
    said = change_evidence(old, "这里 $k_i$ 是度。")
    assert "没有原样留下" in said and "不能说公式没动" in said



def _with_body(page, body, *, pinned=False):
    text = page.page.texts["s1"].model_copy(update={"body": body})
    states = {**page.page.states, "s1": SectionState(section_id="s1", pinned=pinned)}
    return InMemoryPageStore({"u1": [page.model_copy(update={"page": page.page.model_copy(
        update={"texts": {**page.page.texts, "s1": text}, "states": states})})]})


REWRITE = {**ADD, "kind": "rewrite_section", "section_id": "s1", "cells": []}
# run 110500597858 first-para-shorter: the section as it stood, and the model's two drafts of 「把第一段改短一点」
# Synthetic paragraph-scope regression fixture.
NETWORK = '这是为段落编辑回归编写的虚构设备样例。\n\n测试清单包含设备甲、乙、丙、丁、戊五项。\n\n连接表只有甲乙、甲丙、乙丙、丙丁四条连接。\n\n设备戊没有相连设备，仍必须保留在节点清单中。\n\n因此必须同时保存五台设备的完整清单和四条连接。这节后续始终使用同一张虚构网络，不能因为设备孤立就把它从清单中删掉。'
# Synthetic paragraph-scope regression fixture.
SHORT = '这是五台虚构测试设备的网络示例。'


def test_a_paragraph_draft_keeps_the_others_from_the_page(page, ctx):
    t = ProposePageEditTool(_with_body(page, NETWORK))
    r = t.run({**REWRITE, "paragraphs": [1], "text": SHORT}, ctx)
    assert r.error is None
    assert r.proposal.text == NETWORK.replace(NETWORK.split("\n\n")[0], SHORT)


def test_full_draft_losing_part_of_unselected_last_paragraph_is_refused(page, ctx):
    t = ProposePageEditTool(_with_body(page, NETWORK))
    parts = NETWORK.split("\n\n")
    bad = "\n\n".join((SHORT, *parts[1:4], "因此必须同时保存五台设备的完整清单和四条连接。"))
    r = t.run({**REWRITE, "paragraphs": [1], "text": bad}, ctx)
    assert r.proposal is None and "因此必须同时保存五台设备" in r.error
    # the first draft: the whole section's text gone, with the same scope
    assert t.run({**REWRITE, "paragraphs": [1], "text": SHORT + "\n\n" + parts[1]}, ctx).proposal is None



def test_the_whole_section_handed_back_with_every_other_paragraph_is_fine(page, ctx):
    t = ProposePageEditTool(_with_body(page, NETWORK))
    whole = "\n\n".join((SHORT, *NETWORK.split("\n\n")[1:]))
    assert t.run({**REWRITE, "paragraphs": [1], "text": whole}, ctx).proposal.text == whole


def test_what_he_asked_to_delete_goes(page, ctx):
    """「只留前两句」, 「删掉第二段」: the range says it, no words of his are read."""
    t = ProposePageEditTool(_with_body(page, "一句。两句。三句。\n\n第二段讲别的事情，要长一点才算。\n\n第三段。"))
    cut = t.run({**REWRITE, "paragraphs": [1], "text": "一句。两句。"}, ctx)
    assert cut.proposal.text == "一句。两句。\n\n第二段讲别的事情，要长一点才算。\n\n第三段。"
    gone = t.run({**REWRITE, "paragraphs": [2], "text": ""}, ctx)
    assert gone.proposal.text == "一句。两句。三句。\n\n第三段。"
    assert t.run({**REWRITE, "paragraphs": [1, 2, 3], "text": ""}, ctx).proposal is None      # no section left
    whole = t.run({**REWRITE, "paragraphs": [], "text": "全新的一节。"}, ctx)                  # 重讲: sentences may go
    assert whole.proposal.text == "全新的一节。"


def test_a_range_with_a_gap_keeps_the_gap(page, ctx):
    t = ProposePageEditTool(_with_body(page, "第一段。\n\n第二段不动。\n\n第三段。\n\n第四段。"))
    assert "第二段不动" in t.run({**REWRITE, "paragraphs": [1, 3], "text": "新一。\n\n新三。"}, ctx).error
    ok = t.run({**REWRITE, "paragraphs": [1, 3], "text": "新一。\n\n第二段不动。\n\n新三。"}, ctx)
    assert ok.proposal.text == "新一。\n\n第二段不动。\n\n新三。\n\n第四段。"


def test_the_paragraph_scope_is_required_and_checked(page, ctx):
    t = ProposePageEditTool(_with_body(page, "第一段。\n\n第二段。"))
    assert "paragraphs" in t.run({k: v for k, v in REWRITE.items() if k != "paragraphs"}, ctx).error
    assert "只有 2 个" in t.run({**REWRITE, "paragraphs": [5]}, ctx).error


def test_a_blank_line_in_code_or_a_formula_divides_nothing():
    body = "讲。\n\n```py\na = 1\n\nb = 2\n```\n\n$$\nx\n\ny\n$$\n\n完。\n"
    assert [body[a:b] for a, b in paragraph_spans(body)] == [
        "讲。", "```py\na = 1\n\nb = 2\n```", "$$\nx\n\ny\n$$", "完。"]


@pytest.mark.parametrize("mark", ["$$k_i = 1$$", "$k=1$", "[上一页](#p:pg_1)", "![图](a.png)", "{{block:b1}}"])
def test_a_mark_goes_only_when_named(page, ctx, mark):
    t = ProposePageEditTool(_with_body(page, f"讲一下 {mark} 这个东西。\n\n下一段不动。"))
    args = {**REWRITE, "paragraphs": [1], "text": "讲一下这个东西。"}
    assert mark in t.run(args, ctx).error
    assert t.run({**args, "removed": [mark]}, ctx).proposal is not None
    assert t.run({**args, "text": f"换个讲法 {mark}。"}, ctx).proposal is not None
    assert t.run({**args, "text": ""}, ctx).proposal is not None       # the whole paragraph, said by the call
    whole = {**REWRITE, "paragraphs": [], "text": "全新。"}
    assert mark in t.run(whole, ctx).error


def test_a_formula_written_without_its_marks_is_the_same_formula(page, ctx):
    t = ProposePageEditTool(_with_body(page, "写成 $P(k)$ 这样。"))
    assert t.run({**REWRITE, "paragraphs": [], "text": "写成 P(k) 这样，更清楚。"}, ctx).proposal is not None


def test_a_section_he_edited_keeps_its_sentences_in_a_whole_rewrite(page, ctx):
    t = ProposePageEditTool(_with_body(page, "原来一句。\n\n我手改的一句：孤立节点的度是 0。", pinned=True))
    whole = {**REWRITE, "paragraphs": [], "text": "重讲的一节。"}
    assert "我手改的一句" in t.run(whole, ctx).error
    assert t.run({**whole, "text": "重讲的一节。我手改的一句：孤立节点的度是 0。原来一句。"}, ctx).proposal is not None
    assert t.run({**whole, "removed": ["原来一句。", "我手改的一句：孤立节点的度是 0。"]}, ctx).proposal is not None
    assert t.run({**whole, "paragraphs": [1], "text": "新的一句。"}, ctx).proposal is not None     # he named that paragraph


def test_a_section_he_edited_by_hand_says_so_in_the_outline_and_the_draft(page, ctx):
    """Run 110419518252 hand-edit-kept-on-reorganize: 「按现在的规则重写」 redrafted the section he had edited, his
    sentence gone, nothing told. Asked for by name it can still be drafted."""
    hand = page.model_copy(update={"page": page.page.model_copy(update={
        "states": {**page.page.states, "s1": SectionState(section_id="s1", pinned=True)}})})
    store = InMemoryPageStore({"u1": [hand]})
    outline = ReadPageTool(store).run({}, ctx).content
    assert PINNED in next(ln for ln in outline.splitlines() if ln.startswith("- s1")) and outline.count(PINNED) == 1
    args = {**ADD, "kind": "rewrite_section", "section_id": "s1", "cells": ["zscore.purpose"], "paragraphs": [1]}
    r = ProposePageEditTool(store).run(args, ctx)
    assert r.proposal is not None and PINNED in r.content
    assert PINNED not in ProposePageEditTool(store).run({**args, "section_id": "s2", "cells": ["zscore.procedure"]}, ctx).content


def test_a_body_handed_back_with_the_section_heading_loses_only_that_line():
    assert without_heading("## 新标题\n\n正文\n\n## 小节", "新标题") == "正文\n\n## 小节"
    assert without_heading("## 别的\n\n正文", "新标题") == "## 别的\n\n正文"
    assert without_heading("新标题\n\n正文", "新标题") == "新标题\n\n正文"
