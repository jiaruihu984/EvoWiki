"""service/engine.py (GET /wiki/engine's pure parts): model labels over env x tier, the step
table, prompt texts and θ_infer rows. No LLM, no database."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from evowiki.deep import agent as deep_agent
from evowiki.llm.models import ModelRef
from evowiki.llm.prompt_steps import strip_marks
from evowiki.service import engine
from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import InferParams, Localization, ParamPatchOp, Patch
from evowiki.teach.rsi_store import PatchRecord
from evowiki.wikiapp import followup, review_cards, understand
from evowiki.wikiapp.llm import JSON_PROMPT_VERSION, PURPOSE_GROUPS

ANTHROPIC = {"ANTHROPIC_API_KEY": "x", "DEEPSEEK_API_KEY": "y"}   # presence only
DEEPSEEK = {"EVOWIKI_RUNTIME_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "y"}
STEPS = ["拆目标和摸底", "列大纲", "写每一段", "出确认题", "理解你的反馈", "复习卡"]


@pytest.mark.parametrize(("ref", "label"), [
    (ModelRef(provider="deepseek", model="deepseek-v4-pro"), "DeepSeek · 好"),
    (ModelRef(provider="deepseek-nothink", model="deepseek-v4-pro"), "DeepSeek · 快"),
    (ModelRef(provider="anthropic", model="claude-sonnet-5-5"), "Claude Sonnet"),
    (ModelRef(provider="anthropic", model="claude-haiku-4-5"), "Claude Haiku"),
    (ModelRef(provider="anthropic", model="claude-opus-4-1"), "Claude Opus"),
    (ModelRef(provider="anthropic", model="claude-x"), "Claude · claude-x"),
    (ModelRef(provider="other", model="m"), "other · m"),
])
def test_ref_label(ref, label):
    assert engine.ref_label(ref) == label


@pytest.mark.parametrize(("env", "tier", "role", "label"), [
    # anthropic mode (the default): no tier routing, every call is the runtime config
    ({}, "good", "generator", "Claude Sonnet"),
    ({}, "fast", "generator", "Claude Sonnet"),
    ({}, "fast", "verifier", "DeepSeek · 好"),
    ({"EVOWIKI_RUNTIME_MODEL": "claude-haiku-4-5"}, "good", "generator", "Claude Haiku"),
    # DeepSeek mode: good = thinking, fast = non-thinking, for both roles
    (DEEPSEEK, "good", "generator", "DeepSeek · 好"),
    (DEEPSEEK, "fast", "generator", "DeepSeek · 快"),
    (DEEPSEEK, "fast", "verifier", "DeepSeek · 快"),
    ({**DEEPSEEK, "EVOWIKI_DEEPSEEK_FAST_MODEL": "deepseek-v4-flash"}, "fast", "generator",
     "DeepSeek · 快"),
    # deep tier: the same provider as the runtime gateway (86c527a2), never thinking (2026-10-28)
    ({}, None, "generator", "Claude Sonnet"),
    (ANTHROPIC, None, "generator", "Claude Sonnet"),
    ({**DEEPSEEK, "ANTHROPIC_API_KEY": "x"}, None, "generator", "DeepSeek · 快"),
    ({"ANTHROPIC_API_KEY": "x", "EVOWIKI_DEEP_MODEL": "claude-opus-4-1"}, None, "generator",
     "Claude Opus"),
])
def test_model_label(env, tier, role, label):
    assert engine.model_label(env, tier, role) == label


def test_bad_provider_fails_closed():
    with pytest.raises(ValueError):
        engine.model_label({"EVOWIKI_RUNTIME_PROVIDER": "nope"}, "good")


def test_call_tier_follows_tier_for():
    assert engine.call_tier("u1", "sources.toc", {}) == "fast"             # purpose default
    assert engine.call_tier("u1", "wiki.feedback_understand", {}) == "good"  # group default
    assert engine.call_tier("u1", "sources.toc", {"other": "good"}) == "good"  # user choice
    assert engine.call_tier("u1", "deep.write", {"page": "fast"}) is None


DEEP = deep_agent.PROMPT_VERSION


def by_step(env, chosen=None):
    return {p.step: p for p in engine.prompts_out(env, "u1", chosen or {})}


def test_steps_order_versions_and_models():
    rows = engine.prompts_out(DEEPSEEK, "u1", {})
    assert [r.step for r in rows] == STEPS
    assert [r.ver for r in rows] == [
        "intake@v38 · enter@v5 · page-fit@v3", DEEP, DEEP,
        f"{DEEP} · followup-check@v5", "feedback-understand@v14", "card-rewrite@v9"]
    # 拆目标和摸底: the blocks / splits always think (the diagnosis questions are the assistant's)
    assert [r.model for r in rows] == [
        "DeepSeek · 好 / DeepSeek · 快", "DeepSeek · 快", "DeepSeek · 快", "DeepSeek · 快",
        "DeepSeek · 好",
        "DeepSeek · 快"]
    chosen = by_step(DEEPSEEK, {"review": "good", "diagnostic": "good"})
    assert chosen["复习卡"].model == "DeepSeek · 好"
    assert chosen["拆目标和摸底"].model == "DeepSeek · 好"
    # the writer's outline (never thinking), the followup check (the review tier)
    assert chosen["出确认题"].model == "DeepSeek · 快 / DeepSeek · 好"
    anth = by_step(ANTHROPIC)
    assert anth["拆目标和摸底"].model == "Claude Sonnet"   # no verifier call any more
    assert anth["写每一段"].model == "Claude Sonnet"
    assert anth["复习卡"].model == "Claude Sonnet"


def test_versions_are_the_ones_the_calls_use():
    assert understand.PROMPT_VERSION == "feedback-understand@v14"
    assert "goals.probe" not in JSON_PROMPT_VERSION      # the assistant asks (ask_check, T11)
    for purpose in ("goals.intake", "goals.enter",
                    review_cards.PURPOSE, followup.PURPOSE):
        assert purpose in JSON_PROMPT_VERSION and purpose in PURPOSE_GROUPS


def test_texts_are_the_real_prompts():
    rows = by_step({})
    org = rows["列大纲"].text            # the outline part of the writer's first message
    assert org.startswith("# 列大纲\n\n这一页还没有大纲。") and "〈概念〉" in org
    deep = rows["写每一段"].text
    assert deep.startswith(strip_marks(deep_agent.WRITE_SYSTEM)) and "规划】" not in deep
    diag = rows["拆目标和摸底"].text
    v1, v2 = JSON_PROMPT_VERSION["goals.intake"], JSON_PROMPT_VERSION["goals.enter"]
    heads = (f"【{v1} · 分块】", f"【{v2} · 进入一块】")
    assert [h for h in heads if h in diag] == list(heads)
    assert [diag.index(h) for h in heads] == sorted(diag.index(h) for h in heads)
    assert "probe@" not in diag                     # no diagnosis item writer any more
    assert "diag-gen" not in diag   # the goal flow no longer generates diagnostic items
    assert "diag-verify" not in diag   # quiz verification is removed: structural checks only
    check = rows["出确认题"].text
    v = deep_agent.PROMPT_VERSION
    assert check.startswith(f"【{v}】\n") and "【followup-check@v5】\n他刚才在" in check
    assert rows["理解你的反馈"].text.startswith(understand.SYSTEM + "\n\n你是一个个人学习 Wiki 的编辑")
    cards = rows["复习卡"].text
    assert "【card-rewrite@v9 · 回想提示】\n你在改一张复习卡" in cards
    assert "【card-rewrite@v9 · 确认题】\n你在改一道复习用的单选确认题" in cards
    # the card box sends the user's own words: no fixed label, the model may hide the card
    assert cards.count("他对这张卡说的话：〈他的话〉") == 2 and "他的反馈：" not in cards
    assert cards.count('{"action": "hide"}') == 2


T = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)


def rec(path: str, value: float, why: str, at: datetime, *, status="personal_accepted",
        track="A", pid="pa1") -> PatchRecord:
    loc = Localization(track=track, failure="overconfident", target="infer_param", ref=path,
                       signals=("s1",), reason="r")
    body = ParamPatchOp(track=track, path=path, value=value)
    return PatchRecord(user_id="u1", cutoff=None, patch=Patch(
        patch_id=pid, track=track, localization=loc, body=body, why=why, status=status,
        from_version=1, to_version=2, created_at=at))


def test_default_params():
    rows = engine.params_out(InferParams(), [], UTC)
    assert [r.path for r in rows] == list(ip.PATHS)
    assert [r.label for r in rows] == [ip.PATH_LABEL[p] for p in ip.PATHS]
    assert all(r.default and r.tuned_at is None and r.why is None for r in rows)
    v = {r.path: r.value for r in rows}
    assert v["evidence_weight.self"] == "0.6" and v["evidence_weight.quiz"] == "1.0"
    assert v["forgetting_scale"] == "× 1.0" and v["guess_scale"] == "× 1.0"
    assert v["slip"] == "0.1" and v["inference_strength"] == "1.0"
    assert v["start_known"] == "0.3"            # a probability, not a 「×」 multiplier
    assert {r.path: r.label for r in rows}["start_known"] == "初始掌握概率"


def test_tuned_param_note():
    p = ip.set_(InferParams(), "evidence_weight.self", 0.45)
    p = ip.set_(p, "forgetting_scale", 1.25)
    patches = [
        rec("evidence_weight.self", 0.5, "旧的理由", T - timedelta(days=3), pid="pa0"),
        rec("evidence_weight.self", 0.45, "你的自述偏乐观", T, pid="pa1"),
        rec("evidence_weight.self", 0.3, "被撤销的", T + timedelta(days=1), pid="pa2",
            status="rolled_back"),
        rec("evidence_weight.self", 0.3, "B", T, pid="pb1", track="B"),
    ]
    rows = {r.path: r for r in engine.params_out(p, patches, UTC)}
    assert {key: row.description for key, row in rows.items()} == ip.PATH_DESCRIPTION
    s = rows["evidence_weight.self"]
    assert (s.value, s.default, s.tuned_at, s.why) == ("0.45", False, "2026-10-09", "你的自述偏乐观")
    # the date is the user's local date
    tokyo = engine.params_out(p, patches, timezone(timedelta(hours=9)))
    assert {r.path: r.tuned_at for r in tokyo}["evidence_weight.self"] == "2026-10-10"
    f = rows["forgetting_scale"]          # tuned, but no live patch found
    assert (f.value, f.default, f.tuned_at, f.why) == ("× 1.25", False, None, None)
    assert rows["slip"].default and rows["slip"].tuned_at is None
