import pytest

from evowiki.teach import standard
from evowiki.teach.standard import CHECKLIST, SECTION_KEYS, marker, section, sections


def test_sections_carry_markers_in_the_order_given_and_unknown_keys_fail():
    out = sections("self_check", "order")
    assert out.startswith(marker("self_check")) and out.index(marker("order")) > 0
    assert marker("visual_question") not in out
    for k in SECTION_KEYS:
        assert section(k).startswith(f"【标准·{k}】")
    with pytest.raises(KeyError):
        sections("order", "nope")


def test_required_sections_exist():
    assert set(SECTION_KEYS) >= {"order", "terms_first", "depth", "visual_question", "tables_after",
                                 "spoken_first", "planning_questions", "step_trace", "two_views",
                                 "overview_first", "self_check"}


def test_checklist_ids_are_unique_and_the_self_check_section_lists_them():
    ids = [c.id for c in CHECKLIST]
    # standard@v14: six dimensions instead of c01–c18
    assert ids == ["accuracy", "coverage", "fits_learner", "coherence", "useful", "structure"]
    sc = section("self_check")
    assert all(f"{c.id}. {c.text}" in sc for c in CHECKLIST)
    assert standard.checklist_text(("coherence",)).startswith("coherence.")


def test_the_standard_covers_the_users_rules_and_nothing_private():
    full = sections(*SECTION_KEYS)
    for must in (
                 # v14 (writer prompt rework): the user's rewritten sections, no fixed steps
                 "不强制采用固定步骤", "首次实质使用时解释", "当面回答学习者的问题", "明确输入、过程和结果",
                 "不能代替必要的解释", "服务于具体的学习问题", "优先使用已有组件", "不机械套用模板",
                 "人为增加视角", "深度取决于学习者的已有知识和本页目标", "不虚构学习者的经历",
                 "四个规划问题", "能不能自己说出来", "先学协方差", "按用途分组", "路线图",
                 "拆细，不是加长", "具体误区", "太简单了",
                 # v10: materials (sources) and guided reading; v14: a wrong material is not taught
                 "〔资料名 p.页码：「原文片段」〕", "⟦和资料不同：", "资料原话：「", "不编页码",
                 "不把有问题的说法当成事实来教",
                 "限定和分寸", "带读页", "逐句讲",
                 # v11: citations only in section text (final review #16)
                 "出处只标在小节正文里，不写进 EvoUI 块和互动组件",
                 # v12: live run 2026-10-21 (later extension, count claims, prerequisite links)
                 "资料后面才讲的扩展或变体", "不要当成这一页的主模型",
                 "说了数目（「四句话」「三个步骤」），后面逐条讲的就是这么多条",
                 "链接到讲它的那一页（路径里补过的前置页）"):
        assert must in full, must
    assert not any(p in full for p in ("Jerry", "Zotero", "Ingestion Log", "Ctrl+Enter",
                                       "embedded-html", "Obsidian"))
    assert standard.STANDARD_VERSION == "standard@v16"
    assert {"scope", "language", "personal_examples", "feedback_split", "sources",
            "reading"} <= set(SECTION_KEYS)


def test_excerpt_is_the_marker_plus_the_first_lines():
    ex = standard.excerpt("planning_questions", 3)
    assert ex.startswith(marker("planning_questions")) and len(ex.splitlines()) == 3
    assert "已经会什么" in ex and "怎样一次只增加一个新东西" not in ex
    with pytest.raises(KeyError):
        standard.excerpt("nope", 2)


def test_v8_does_not_compress_pages():
    """standard@v8: a page teaches one whole question at the length it needs; code is
    notebook-style with only the teaching lines (the old 'one or two concepts, rather one more page'
    and 'every block runs on its own' rules are gone)."""
    scope = standard.section("scope")
    assert "篇幅跟着内容走" in scope and "宁可多一页" not in scope
    assert "一页的量有限" not in standard.checklist_text()
    trace = standard.section("step_trace")
    assert "只展示与当前知识有关的细节" in trace and "独立运行" not in trace
