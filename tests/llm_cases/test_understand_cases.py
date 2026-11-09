"""wiki.feedback_understand (feedback-understand@v9) offline cases: one call reads a page-level
feedback and returns a reading + aspects (each: issue 问题 kept apart from action 改法) + page plan +
missing concepts + style proposal.

Information flow (the product specification), rule OWN_WORDS: the
call works only from the user's own words (verbatim), the chips they picked (their labels) and the
page — its sections (id, heading, opening), its and its path's concepts, K's bands for them, the
teaching rules in force on it and the component library it may pick from. No template: the prompt
says so, carries no canned reading, and nothing from other pages of the wiki. The reading has to
restate the words (`restates`: a shared two-character piece), else NotUnderstood (fail closed).
Harness: valid answer → Understood; unknown section / component / concept ids dropped and named
in `dropped`; a concept named by its name is mapped to its id; a block together with a component
loses the block; an invalid block is dropped; missing required fields → NotUnderstood. Aspects: one
feedback may carry several; an aspect without an issue or an action, or whose quote is not the
user's words verbatim (OWN_WORDS), is dropped and named; at most MAX_ASPECTS; with no page summary
the summary is the actions in order (the one-summary consumers keep working)."""

import json

import pytest

from evowiki.teach.contracts import PageFeedbackIn
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.understand import (
    MAX_ADDS,
    MAX_ASPECTS,
    PROMPT_VERSION,
    PURPOSE,
    REPAIR_TURNS,
    Context,
    NotUnderstood,
    UnderstandOut,
    context,
    prompt,
    reply_schema,
    restates,
    understand,
    validate,
)
from tests.deep.test_evoui_typed_templates import EXAMPLES
from tests.wikiapp.script import Script

TEXT = "标准差那段我完全看不懂，能不能画个图让我动手看看"
CTX = Context(page_id="pg1", title="z 分数",
              sections=(("s2", "z 分数怎么读", "z 告诉你离均值几个标准差"),
                        ("s3", "标准差提醒", "标准差是典型的偏离")),
              concepts={"c1": "z 分数", "c2": "标准差"},
              bands=("z 分数：procedure 模糊，interpret 不会", "标准差：term 确定会"),
              rules=("先给公式再讲直觉",),
              components={"variance-dots": "方差点图 — 拖动分散程度，看偏差的平方怎样变"})


ASPECTS = [{"aspect": "讲法", "quote": "标准差那段我完全看不懂",
            "issue": "标准差那段他完全看不懂", "action": "先用一句人话讲标准差是典型的偏离"},
           {"aspect": "交互", "quote": "画个图让我动手看看",
            "issue": "光看文字他没法想象分散程度", "action": "放一个能拖的方差点图"}]


def answer(**kw) -> dict:
    base = {"reading": "你看不懂标准差那段，想要一个能动手看的图", "aspects": ASPECTS, "missing": [],
            "page": {"summary": "给标准差那段补一个直观说法，再放一个能拖的图",
                     "sections": [{"section_id": "s3", "add": "先别管公式：标准差就是典型的偏离有多大。"}],
                     "component": "variance-dots", "component_after": "s3"},
            "style": {"kind": "缺少可视化", "before": "交互只在确实需要时才做",
                      "after": "有分布的概念都配一个能动手看的图"}}
    return {**base, **kw}


def body(text=TEXT, chips=("unclear",), section_id=None) -> PageFeedbackIn:
    return PageFeedbackIn(text=text, chips=chips, section_id=section_id)


def check(out: dict, ctx=CTX, b=None):
    return validate(UnderstandOut.model_validate(out), ctx, b or body())


# --- (a) what the prompt must know / must not know ----------------------------------------------

def test_prompt_has_the_words_chips_and_page():
    p = prompt(CTX, body(section_id="s3"))
    assert f"「{TEXT}」" in p and "他点的感觉：不像人话" in p and "（他是对小节 [s3] 说的）" in p
    for s in ("- [s2] z 分数怎么读｜z 告诉你离均值几个标准差", "z 分数（c1）、标准差（c2）",
              "z 分数：procedure 模糊，interpret 不会", "- 先给公式再讲直觉",
              "- variance-dots：方差点图"):
        assert s in p, s


def test_prompt_separates_issue_from_action_and_allows_several_aspects():
    p = prompt(CTX, body())
    assert "一条反馈可以同时说几件事" in p and "「问题」和「改法」分开写" in p
    assert "quote 原样抄他的原话片段" in p and "不要把改法写进问题里" in p
    assert '"aspects": [{"aspect"' in p and f"aspects 最多 {MAX_ASPECTS} 条" in p
    assert PROMPT_VERSION == "feedback-understand@v14"


def test_prompt_says_no_template_and_carries_no_canned_reading():
    p = prompt(CTX, body())
    assert "只依据他的原话，不要套模板" in p
    assert "你看不懂" not in p and "缺少可视化" not in p       # nothing pre-written to copy
    assert "方差点图 — 拖动" in p and "放一个能拖的方差点图" not in p


def test_nothing_written_or_no_chips_are_said_plainly():
    assert "「（没写）」" in prompt(CTX, body(text=""))
    assert "他点的感觉：（没点）" in prompt(CTX, body(chips=()))


def test_empty_library_says_no_component():
    p = prompt(Context(**{**CTX.__dict__, "components": {}}), body())
    assert "（组件库是空的，不要插组件）" in p


def test_context_reads_only_this_page():
    """Through a real snapshot: a second page of the wiki (other title, other text) never
    reaches the prompt."""
    from evowiki.wikiapp.repo import MemoryRepo
    from tests.wikiapp.test_goal import NOW, Ids
    from tests.wikiapp.test_organize import add_page
    from tests.wikiapp.test_page import organized

    env = (MemoryRepo(["u1"]), Script(), Ids())
    pid = organized(env)
    add_page(env[0], env[2], title="另一页：完全无关的标题")
    snap = env[0].load("u1")
    ctx = context(snap, pid, NOW, CTX.components)
    p = prompt(ctx, body())
    assert "另一页：完全无关的标题" not in p
    rec = snap.state.pages[pid]
    assert rec.title in p and all(f"[{sid}]" in p for sid in rec.page.texts)


# --- (a) harness: outputs -----------------------------------------------------------------------

def test_valid_answer_through_one_call():
    s = Script().push(PURPOSE, answer())
    out = understand(CountingGateway(s.gateway(), "u1"), CTX, body(), subject="pg1/fb1")
    assert out.reading.reading.startswith("你看不懂标准差") and out.dropped == ()
    assert out.reading.page.component == "variance-dots" and s.calls(PURPOSE) == 1
    assert TEXT in s.requests(PURPOSE)[0].prompt


def test_reading_that_does_not_restate_the_words_is_not_understood():
    with pytest.raises(NotUnderstood, match="对上原话"):
        check(answer(reading="用户希望页面更好"))


def test_empty_reading_is_not_understood():
    with pytest.raises(NotUnderstood, match="没有给出理解"):
        check(answer(reading="   "))


def test_chips_only_feedback_needs_no_restatement():
    out = check(answer(reading="页面需要更直观"), b=body(text=""))
    assert out.reading.reading == "页面需要更直观"


def test_unknown_ids_are_dropped_and_named():
    out = check(answer(missing=["量子力学", "标准差"],
                       page={"summary": "改", "component": "made-up", "component_after": "s9",
                             "sections": [{"section_id": "s9", "add": "x"},
                                          {"section_id": "s2", "add": "补一句"}]}))
    assert set(out.dropped) == {"概念 量子力学", "小节 s9", "组件 made-up"}
    assert out.reading.missing == ("c2",)                      # mapped from its name
    assert [a.section_id for a in out.reading.page.sections] == ["s2"]
    assert out.reading.page.component is None and out.reading.page.component_after is None


def test_additions_are_capped():
    adds = [{"section_id": "s2", "add": f"第 {i} 段"} for i in range(MAX_ADDS + 3)]
    out = check(answer(page={"summary": "改", "sections": adds}))
    assert len(out.reading.page.sections) == MAX_ADDS


LAB = {**EXAMPLES["lab"]}
BAD_LAB = {**EXAMPLES["lab"], "derived": [{"name": "r", "expr": "window.alert(1)"}]}


def test_block_with_a_component_is_dropped_and_an_invalid_block_too():
    out = check(answer(page={**answer()["page"], "block": LAB}))
    assert out.reading.page.block is None and "EvoUI 块（同时给了组件，只用组件）" in out.dropped
    out2 = check(answer(page={"summary": "改", "sections": [], "block": BAD_LAB}))
    assert out2.reading.page.block is None and any("不合法" in d for d in out2.dropped)
    out3 = check(answer(page={"summary": "改", "sections": [], "block": LAB}))
    assert out3.reading.page.block["type"] == "evoui" and out3.dropped == ()   # compiled from the template
    out4 = check(answer(page={"summary": "改", "sections": [], "block": None}))
    assert out4.reading.page.block is None and not any("EvoUI" in d for d in out4.dropped)


def test_v11_the_block_is_typed_a_string_or_an_open_object_does_not_parse():
    from pydantic import ValidationError
    for bad in ('{"type": "evoui"}', {"nonsense": 1}, {"type": "quiz"}):
        with pytest.raises(ValidationError):
            UnderstandOut.model_validate(answer(page={"summary": "改", "sections": [], "block": bad}))


CONFIG = {"type": "object", "additionalProperties": False, "required": ["title", "question", "n"],
          "properties": {"title": {"type": "string"}, "question": {"type": "string"}, "n": {"type": "integer"}}}
CTX_CFG = Context(page_id="pg1", title="z 分数", sections=CTX.sections, concepts=CTX.concepts, bands=CTX.bands,
                  rules=CTX.rules, components={**CTX.components, "bars": "柱状图 [可配置]"},
                  schemas={"variance-dots": None, "bars": CONFIG})


def test_the_reading_names_the_components_and_the_config_call_has_the_components_shape():
    text = json.dumps(reply_schema(CTX_CFG).model_json_schema(), ensure_ascii=False)
    assert '"enum": ["bars", "variance-dots"]' in text and '"n"' not in text      # no config in the reading
    from evowiki.wikiapp.component_fill import reply_schema as config_schema
    assert '"n": {"type": "integer"}' in json.dumps(config_schema("bars", CONFIG).model_json_schema())
    assert "JSON 字符串" not in prompt(CTX_CFG, body())


def test_a_refused_block_goes_back_once_and_the_repaired_one_is_kept():
    s = Script().push(PURPOSE, answer(page={"summary": "改", "sections": [], "block": BAD_LAB}),
                      answer(page={"summary": "改", "sections": [], "block": LAB}))
    out = understand(CountingGateway(s.gateway(), "u1"), CTX, body(), subject="pg1/fb1")
    assert out.reading.page.block is not None and out.dropped == () and s.calls(PURPOSE) == 2
    second = s.requests(PURPOSE)[1].prompt
    assert "你上一次的回答" in second and "EvoUI 块" in second


def test_a_refused_config_goes_back_and_after_the_turns_it_is_dropped():
    reading = answer(page={"summary": "改", "sections": [], "component": "bars", "component_after": "s3",
                           "component_config": None})
    bad = {"config": {"title": "t", "question": "q", "n": "many"}}
    s = Script().push(PURPOSE, reading, *[bad] * (1 + REPAIR_TURNS))
    out = understand(CountingGateway(s.gateway(), "u1"), CTX_CFG, body(), subject="pg1/fb1")
    assert s.calls(PURPOSE) == 2 + REPAIR_TURNS and "组件 bars" in out.dropped[0]
    s2 = Script().push(PURPOSE, reading, bad, {"config": {"title": "t", "question": "q", "n": 3}})
    out2 = understand(CountingGateway(s2.gateway(), "u1"), CTX_CFG, body(), subject="pg1/fb2")
    assert out2.reading.page.component_config == {"title": "t", "question": "q", "n": 3} and s2.calls(PURPOSE) == 3
    assert "n" in s2.requests(PURPOSE)[2].prompt and "你上一次的回答" in s2.requests(PURPOSE)[2].prompt


def test_incomplete_style_is_not_understood():
    with pytest.raises(NotUnderstood, match="不完整"):
        check(answer(style={"kind": "", "before": "x", "after": "y"}))


def test_an_unparseable_reply_is_asked_again_and_raises_after_the_turns():
    from evowiki.llm.gateway import LLMParseError
    s = Script().push(PURPOSE, "not json", answer())
    out = understand(CountingGateway(s.gateway(), "u1"), CTX, body(), subject="pg1/fb1")
    assert out.reading.page.component == "variance-dots" and s.calls(PURPOSE) == 2
    s2 = Script().push(PURPOSE, *["not json"] * (1 + REPAIR_TURNS))
    with pytest.raises(LLMParseError):
        understand(CountingGateway(s2.gateway(), "u1"), CTX, body(), subject="pg1/fb2")


def test_restates_is_a_two_character_overlap():
    assert restates("你想要一个能动手看的图", TEXT)
    assert not restates("页面更好", TEXT)
    assert restates("任何话", "好")                            # under 2 chars: nothing to match


# --- aspects: issue apart from action --------------------------------------------------------

def test_several_aspects_are_kept_in_order_with_issue_and_action_apart():
    out = check(answer())
    assert [(a.aspect, a.quote, a.issue, a.action) for a in out.aspects] == [
        (x["aspect"], x["quote"], x["issue"], x["action"]) for x in ASPECTS]
    assert out.dropped == ()


def test_aspect_without_issue_or_action_is_dropped_and_named():
    bad = [{"aspect": "讲法", "issue": "看不懂", "action": ""},
           {"aspect": "", "issue": "", "action": "补一句"}, ASPECTS[1]]
    out = check(answer(aspects=bad))
    assert [a.aspect for a in out.aspects] == ["交互"]
    assert out.dropped == ("方面 讲法（缺问题或改法）", "方面 （没写）（缺问题或改法）")


def test_a_quote_that_is_not_verbatim_drops_the_aspect():
    para = {**ASPECTS[0], "quote": "标准差那一段我看不太懂"}         # paraphrased, not his words
    out = check(answer(aspects=[para, ASPECTS[1]]))
    assert [a.aspect for a in out.aspects] == ["交互"] and out.dropped == ("方面 讲法（引用不是原话）",)


def test_empty_quote_is_allowed_for_chip_only_feedback():
    a = {"aspect": "节奏", "quote": "", "issue": "他觉得太难", "action": "把卡住的那步拆细"}
    out = check(answer(reading="页面需要慢一点", aspects=[a]), b=body(text="", chips=("too_hard",)))
    assert out.aspects[0].quote == "" and out.dropped == ()


def test_aspects_are_capped():
    many = [{**ASPECTS[1], "aspect": f"a{i}"} for i in range(MAX_ASPECTS + 2)]
    out = check(answer(aspects=many))
    assert len(out.aspects) == MAX_ASPECTS and len(out.dropped) == 2


def test_missing_summary_takes_the_actions_in_order():
    out = check(answer(page={**answer()["page"], "summary": ""}))
    assert out.reading.page.summary == "先用一句人话讲标准差是典型的偏离；放一个能拖的方差点图"
    no_aspects = check(answer(aspects=[], page={**answer()["page"], "summary": ""}))
    assert no_aspects.reading.page.summary == "按你的反馈补了几处" and no_aspects.aspects == ()


def test_an_answer_without_aspects_still_works():
    a = answer()
    del a["aspects"]
    out = check(a)
    assert out.aspects == () and out.reading.page.component == "variance-dots"
