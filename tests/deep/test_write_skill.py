"""deep@v32: the page writer runs on the write-page skill (SKILL.md + references), the outline part is
the user's target text, and a page's checks are on what it teaches (no LLM)."""

from evowiki.agent.tools import ToolContext
from evowiki.deep import agent, skill
from evowiki.deep.evoui import GROUPS, spec_text, templates
from evowiki.goals.guardrails import DraftOutline, page_bands
from evowiki.goals.itemflaws import ITEM_RULES_ZH
from evowiki.goals.outline_context import accept_draft
from tests.goals.fakes_k import ZSCORE_OUTLINE, zscore_k, zscore_spec

CTX = ToolContext(user_id="u", session_id="s", page_id="p")


def test_every_ref_reads_and_the_system_prompt_is_the_skill_entry():
    s = skill.skill()
    for name in s.refs:
        assert skill.ref(name).strip(), name
    assert agent.WRITE_SYSTEM == s.body and "read_ref" in agent.WRITE_SYSTEM
    assert "⟦g:write⟧" in agent.WRITE_SYSTEM and "evoui/chart" in agent.WRITE_SYSTEM
    # what changes per run is not in the entry, and no rule there names the old seven steps
    assert "普通做法" not in agent.WRITE_SYSTEM and "面试表达" not in agent.WRITE_SYSTEM


def test_the_evoui_format_has_one_source_and_each_group_carries_its_templates():
    whole = spec_text()
    for g in GROUPS:
        assert skill.ref(f"evoui/{g}").splitlines()[0] in whole
    for t in templates():
        assert t["group"] in GROUPS
        assert f"### {t['name']}（" in skill.ref(f"evoui/{t['group']}")
    extra = ({"name": "我的模板", "use": "试一下", "block": {"type": "evoui"}},)
    assert "我的模板" in skill.ref("evoui/core", extra) and "我的模板" not in skill.ref("evoui/chart", extra)


def test_the_stable_prefix_preloads_teaching_and_the_index_but_no_group():
    from evowiki.deep.tools import Workspace
    stable = agent.write_prompt_stable(Workspace(None), ())
    assert skill.ref("teaching") in stable and skill.ref("evoui/index") in stable
    assert skill.ref("evoui/core").splitlines()[0] not in stable
    assert skill.ref("interaction").splitlines()[0] not in stable


def test_the_outline_part_is_the_target_text_with_k_as_bands_and_evidence():
    p = agent.outline_part(zscore_spec(), zscore_k())
    assert skill.ref("outline") in p and skill.ref("assessment") in p
    assert ITEM_RULES_ZH in p and "m=" not in p and "u=" not in p
    assert "organizer" not in p and "compare" not in p and "【标准·scope】" not in p
    assert "这一页负责讲（学习目标）：z_score z-score" in p
    assert "只是用到的前置（不出确认题）：mean 均值、variance 方差" in p


def _with_check(cell: str) -> DraftOutline:
    d = {**ZSCORE_OUTLINE, "sections": [*ZSCORE_OUTLINE["sections"], {
        "id": "s9", "heading": "确认一下", "role": "check", "cells": [cell],
        "check": {"cell": cell, "stem": "z = 1 表示 x 比均值高多少？",
                  "options": ["一个标准差", "一个方差", "百分之一"], "answer": 0}}]}
    return DraftOutline(**d)


def test_a_check_on_a_mentioned_prerequisite_is_sent_back():
    spec, k = zscore_spec(), zscore_k()
    ok = accept_draft(_with_check("z_score.interpret"), spec, k)
    assert ok.outline is not None and not ok.problems
    bad = accept_draft(_with_check("mean.procedure"), spec, k)
    assert bad.outline is None and any("不是这一页负责讲的知识点" in v.message for v in bad.problems)
    assert page_bands(spec, k)


def test_the_model_is_offered_roles_by_room_not_page_shape():
    from evowiki.goals.guardrails import DraftSection
    roles = DraftSection.model_json_schema()["properties"]["role"]["enum"]
    assert roles == ["explain", "recap", "line", "check"]
    assert DraftSection(heading="x").role == "explain"
