"""Judge routing by writer family and the blind both-order comparison (harness RSI phase 2, Task 4).
Every judge reply is scripted; nothing reaches a real provider."""

import json
import re
from datetime import UTC, datetime

import pytest

from evowiki.deep.agent import SnapshotSeqs
from evowiki.harness.anchors import LENGTH_RATIO
from evowiki.harness.judge import (
    Comparison,
    JudgeConfigs,
    JudgeUnavailable,
    compare,
    judge_configs,
    judge_for,
    judge_gateway,
    judge_providers,
)
from evowiki.harness.judge_prompt import JUDGE_PURPOSE, JUDGE_VERSION
from evowiki.harness.replay import Case, ReplayResult
from evowiki.llm.gateway import Gateway, LLMParseError, ProviderOutputError
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import PriceTable
from evowiki.llm.scripted import ScriptedProvider
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl
from evowiki.runtime.llm import DEEPSEEK_NOTHINK
from evowiki.runtime.usage import runtime_tier_configs
from tests.runtime.llm_helpers import MemoryCallLog

DS = ModelRef(provider="deepseek", model="deepseek-v4-pro")
HAIKU = ModelRef(provider="anthropic", model="claude-haiku-4-5")
ACC = Account(user_id="u1", group="other")
CASE = Case(case_id="c1", run_id="run-1", page_id="p1", section_id="s2", signal_ids=("sig1",),
            anchor=False, snapshot=SnapshotSeqs(wiki_seq=3, aspect_seq=1, infer_version=1),
            mode="rewrite", instruction="", user_id="u1")
A_TEXT = "回忆线索：导数是变化率\n\n导数描述函数在一点附近的变化快慢。比如速度就是位置的导数。"
B_TEXT = "回忆线索：导数的定义\n\n导数是一种极限。它的定义用到了极限，也就是差商在步长趋于零时的值。"


class _Meter:
    def __init__(self):
        self.records = []

    def check(self, account):
        pass

    def record(self, account, purpose, usage, *, replayed):
        self.records.append((account, purpose))


def _shown(prompt: str) -> tuple[str, str]:
    m = re.search(r"【甲】\n(.*?)\n\n【乙】\n(.*?)\n\n(?:这只是页面里的一段|只比较内容)", prompt, re.DOTALL)
    assert m, prompt
    return m.group(1), m.group(2)


def judge_gw(decide, ref=DS):
    """A metered gateway whose verifier is `ref`, answering each judge call with
    `decide(first_text, second_text) -> dict` (or a raw string)."""
    def respond(model, req):
        out = decide(*_shown(req.prompt))
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)

    prov = ScriptedProvider(ref.provider, respond)
    meter = _Meter()
    gw = Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
                 MemoryCallLog(), mode="record", providers={ref.provider: prov},
                 usage=UsageControl(meter=meter, prices=PriceTable({})),
                 clock=lambda: datetime(2026, 10, 21, tzinfo=UTC))
    gw.provider, gw.meter = prov, meter
    return gw


def res(text, run_id, provider="anthropic"):
    return ReplayResult(case_id="c1", text=text, writer_provider=provider, run_id=run_id, cost=0.1)


A = res(A_TEXT, "hr-a")
B = res(B_TEXT, "hr-b")


def prefer(target: str, quote: str | None = None):
    """A judge that always picks the text `target` (whichever side it is on)."""
    def decide(first, second):
        if target not in (first, second):
            return {"better": "same", "quote": "", "reason": "差不多"}
        q = quote if quote is not None else target.splitlines()[-1]
        return {"better": "甲" if first == target else "乙", "quote": q, "reason": "有例子"}
    return decide


def run(decide, a=A, b=B, *, more=False, signals="例子不够", ref=DS):
    gw = judge_gw(decide, ref)
    out = compare(CASE, a, b, signals_text=signals, asked_for_more=more, gw=gw, account=ACC)
    return out, gw


# --- comparison -------------------------------------------------------------------------------


def test_agreement_in_both_orders_wins():
    out, gw = run(prefer(A_TEXT))
    assert isinstance(out, Comparison)
    assert (out.winner, out.flipped) == ("a", False)
    assert (out.case_id, out.a_id, out.b_id) == ("c1", "hr-a", "hr-b")
    assert out.quote == A_TEXT.splitlines()[-1]
    assert len(out.call_ids) == 2 and out.call_ids[0] != out.call_ids[1]
    assert out.cmp_id.startswith("cmp:")
    # both orders were shown, a first then b first
    shown = [_shown(r.prompt) for r in gw.provider.calls]
    assert shown == [(A_TEXT, B_TEXT), (B_TEXT, A_TEXT)]
    req = gw.provider.calls[0]
    assert (req.purpose, req.prompt_version) == (JUDGE_PURPOSE, JUDGE_VERSION)
    assert "例子不够" in req.prompt
    # metered to the account the caller passed
    assert [acc for acc, _ in gw.meter.records] == [ACC, ACC]


def test_reference_winning_is_b():
    quote = "它的定义用到了极限，也就是差商在步长趋于零时的值。"
    out, _ = run(prefer(B_TEXT, quote))
    assert out.winner == "b" and out.quote == quote


def test_flip_between_orders_is_a_tie():
    # a judge that always picks the first text it sees
    out, _ = run(lambda first, second: {"better": "甲", "quote": first.splitlines()[-1],
                                        "reason": "前面的好"})
    assert (out.winner, out.flipped) == ("tie", True)


def test_same_in_one_order_is_a_tie_without_a_flip():
    calls = []

    def decide(first, second):
        calls.append(first)
        if len(calls) == 1:
            return {"better": "甲", "quote": "比如速度就是位置的导数。", "reason": "有例子"}
        return {"better": "same", "quote": "", "reason": "差不多"}

    out, _ = run(decide)
    assert (out.winner, out.flipped) == ("tie", False)


def test_same_in_both_orders_needs_no_quote():
    out, _ = run(lambda f, s: {"better": "same", "quote": "", "reason": "差不多"})
    assert (out.winner, out.flipped, out.quote) == ("tie", False, "")


def test_quote_not_in_the_named_text_is_a_tie():
    out, _ = run(prefer(A_TEXT, "这句话哪里都没有。"))
    assert out.winner == "tie"
    assert out.reason.startswith("引用的句子")


def test_quote_from_the_other_text_is_a_tie():
    # names a as better but copies a sentence of b
    out, _ = run(prefer(A_TEXT, "它的定义用到了极限，也就是差商在步长趋于零时的值。"))
    assert out.winner == "tie"


def test_empty_quote_with_a_winner_is_a_tie():
    out, _ = run(prefer(A_TEXT, "  "))
    assert out.winner == "tie"


def test_quote_matches_after_whitespace_normalisation():
    out, _ = run(prefer(A_TEXT, "  比如速度就是\n  位置的导数。 "))
    assert out.winner == "tie"      # inner whitespace the text does not have: not a copy
    spaced = res("回忆线索：x\n\n导数描述变化快慢。\n比如  速度\n就是位置的导数。", "hr-a")
    out, _ = run(prefer(spaced.text, "比如 速度 就是位置的导数。"), a=spaced)
    assert out.winner == "a"


def _long(n: int) -> str:
    return "回忆线索：长\n\n" + "长文里的一句。" + "补" * n


def test_longer_winner_is_a_tie_unless_asked_for_more():
    short = len(B_TEXT.strip())
    long_a = res(_long(int(short * LENGTH_RATIO)), "hr-a")
    assert len(long_a.text.strip()) > LENGTH_RATIO * short
    out, _ = run(prefer(long_a.text), a=long_a)
    assert out.winner == "tie" and "更长" in out.reason
    out, _ = run(prefer(long_a.text), a=long_a, more=True)
    assert out.winner == "a"


def test_a_longer_reference_winning_stays_a_reference_win():
    long_b = res(_long(int(len(A_TEXT) * LENGTH_RATIO)), "hr-b")
    out, _ = run(prefer(long_b.text), b=long_b)
    assert out.winner == "b"


def test_a_shared_sentence_is_no_proof():
    shared = "比如速度就是位置的导数。"
    b = res("回忆线索：导数\n\n导数是一种极限，也就是差商的极限。" + shared, "hr-b")
    out, _ = run(prefer(A_TEXT, shared), b=b)
    assert out.winner == "tie" and "另一段也有" in out.reason
    out, _ = run(prefer(A_TEXT, "导数描述函数在一点附近的变化快慢。"), b=b)
    assert out.winner == "a"


def test_longer_text_losing_keeps_the_win():
    long_b = res(_long(int(len(A_TEXT) * LENGTH_RATIO)), "hr-b")
    out, _ = run(prefer(A_TEXT), b=long_b)
    assert out.winner == "a"


def test_within_the_ratio_the_longer_may_win():
    a = res(B_TEXT + "比如", "hr-a")     # only a little longer
    out, _ = run(prefer(a.text, "比如"), a=a)
    assert out.winner == "a"


def test_mixed_writer_providers_are_refused():
    gw = judge_gw(prefer(A_TEXT))
    with pytest.raises(ValueError, match="writer"):
        compare(CASE, A, res(B_TEXT, "hr-b", provider="deepseek"), signals_text="",
                asked_for_more=False, gw=gw, account=ACC)
    assert gw.provider.calls == []


def test_texts_of_another_case_are_refused():
    gw = judge_gw(prefer(A_TEXT))
    other = ReplayResult(case_id="c9", text=B_TEXT, writer_provider="anthropic", run_id="x",
                         cost=0.0)
    with pytest.raises(ValueError, match="case"):
        compare(CASE, A, other, signals_text="", asked_for_more=False, gw=gw, account=ACC)


def test_same_family_judge_gateway_is_refused_before_any_call():
    gw = judge_gw(prefer(A_TEXT), ref=HAIKU)        # writer is anthropic too
    with pytest.raises(JudgeUnavailable):
        compare(CASE, A, B, signals_text="", asked_for_more=False, gw=gw, account=ACC)
    assert gw.provider.calls == []


def test_deepseek_writer_is_judged_by_anthropic():
    a = res(A_TEXT, "hr-a", provider="deepseek-nothink")
    b = res(B_TEXT, "hr-b", provider="deepseek-nothink")
    out, _ = run(prefer(A_TEXT), a=a, b=b, ref=HAIKU)
    assert out.winner == "a"
    gw2 = judge_gw(prefer(A_TEXT), ref=DS)          # deepseek judging deepseek
    with pytest.raises(JudgeUnavailable):
        compare(CASE, a, b, signals_text="", asked_for_more=False, gw=gw2, account=ACC)


def test_unparseable_judge_output_raises():
    with pytest.raises((LLMParseError, ProviderOutputError)):
        run(lambda f, s: "不是 JSON")
    with pytest.raises((LLMParseError, ProviderOutputError)):
        run(lambda f, s: {"better": "丙", "quote": "", "reason": ""})


def test_prompt_is_blind():
    _, gw = run(prefer(A_TEXT))
    text = gw.provider.calls[0].prompt + gw.provider.calls[0].system
    for word in ("harness", "Harness", "模型", "新", "旧", "手册", "候选", "anthropic", "deepseek",
                 "hr-a", "hr-b"):
        assert word not in text, word


def test_cmp_id_is_stable_for_the_same_comparison():
    one, _ = run(prefer(A_TEXT))
    two, _ = run(prefer(A_TEXT))
    assert one.cmp_id == two.cmp_id
    three, _ = run(prefer(A_TEXT), b=res(B_TEXT, "hr-c"))
    assert three.cmp_id != one.cmp_id


# --- routing -----------------------------------------------------------------------------------


BOTH = JudgeConfigs(deepseek=DS, anthropic=HAIKU)


def test_anthropic_writer_gets_deepseek_judge():
    assert judge_for("anthropic", BOTH) == DS
    assert judge_for("anthropic-think", BOTH) == DS     # the assistant's 思考 level: same vendor


def test_deepseek_writer_gets_anthropic_cheap_judge():
    assert judge_for("deepseek", BOTH) == HAIKU
    assert judge_for("deepseek-nothink", BOTH) == HAIKU


def test_missing_judge_config_is_unavailable():
    with pytest.raises(JudgeUnavailable) as e:
        judge_for("anthropic", JudgeConfigs(deepseek=None, anthropic=HAIKU))
    assert "DeepSeek" in e.value.reason
    with pytest.raises(JudgeUnavailable):
        judge_for("deepseek", JudgeConfigs(deepseek=DS, anthropic=None))


def test_unknown_writer_is_unavailable():
    with pytest.raises(JudgeUnavailable) as e:
        judge_for("scripted", BOTH)
    assert e.value.reason


def test_same_family_config_is_never_used():
    bad = JudgeConfigs(deepseek=ModelRef(provider="anthropic", model="x"), anthropic=HAIKU)
    with pytest.raises(JudgeUnavailable):
        judge_for("anthropic", bad)


def test_judge_configs_from_env():
    env = {"ANTHROPIC_API_KEY": "k", "DEEPSEEK_API_KEY": "k"}
    fast = runtime_tier_configs({**env, "EVOWIKI_RUNTIME_PROVIDER": "anthropic"})["fast"]
    cheap_ds = ModelRef(provider=DEEPSEEK_NOTHINK, model=fast.verifier.model)
    c = judge_configs(env)
    assert c == JudgeConfigs(deepseek=cheap_ds, anthropic=fast.generator)
    assert c.anthropic == HAIKU and c.deepseek.model == "deepseek-v4-flash"
    # the app's own mode does not change the judges
    assert judge_configs({**env, "EVOWIKI_RUNTIME_PROVIDER": "deepseek"}) == c
    c = judge_configs({**env, "EVOWIKI_DEEPSEEK_FAST_MODEL": "ds-x",
                       "EVOWIKI_RUNTIME_FAST_MODEL": "h-x"})
    assert c.deepseek == ModelRef(provider=DEEPSEEK_NOTHINK, model="ds-x")
    assert c.anthropic == ModelRef(provider="anthropic", model="h-x")
    only_ds = judge_configs({"DEEPSEEK_API_KEY": "k"})
    assert only_ds.anthropic is None
    with pytest.raises(JudgeUnavailable) as e:
        judge_for("deepseek", only_ds)
    assert "Anthropic" in e.value.reason
    assert judge_configs({}) == JudgeConfigs(deepseek=None, anthropic=None)


def test_a_tier_routing_to_the_writers_family_is_caught_after_the_call():
    from evowiki.llm.usage_control import TIERS
    same = GatewayConfig(generator=HAIKU, verifier=HAIKU, allow_single_provider=True)
    prov = ScriptedProvider("anthropic", lambda m, r: json.dumps(
        {"better": "same", "quote": "", "reason": ""}))
    gw = Gateway(GatewayConfig(generator=DS, verifier=DS, allow_single_provider=True),
                 MemoryCallLog(), mode="record", providers={"anthropic": prov},
                 usage=UsageControl(meter=_Meter(), prices=PriceTable({}),
                                    tier_configs={t: same for t in TIERS}),
                 clock=lambda: datetime(2026, 10, 21, tzinfo=UTC))
    with pytest.raises(JudgeUnavailable):
        compare(CASE, A, B, signals_text="", asked_for_more=False, gw=gw, account=ACC)
    assert len(prov.calls) == 1          # caught before the second call


def test_judge_providers_build_the_thinking_off_deepseek():
    assert judge_providers({}) == {}
    # runtime_providers skips (logs) a provider whose SDK is missing; it never raises ImportError
    pytest.importorskip("anthropic")
    pytest.importorskip("openai")
    out = judge_providers({"ANTHROPIC_API_KEY": "k", "DEEPSEEK_API_KEY": "k"})
    assert {"anthropic", DEEPSEEK_NOTHINK} <= set(out)
    assert out[DEEPSEEK_NOTHINK]._thinking is False


def _deep_llm(providers):
    from evowiki.deep.llm import DeepLLM
    return DeepLLM(config=GatewayConfig(generator=HAIKU, verifier=DS), providers=providers,
                   log=MemoryCallLog(), prices=PriceTable({}), meter=_Meter(),
                   clock=lambda: datetime(2026, 10, 21, tzinfo=UTC))


def test_judge_gateway_routes_every_call_to_the_judge():
    prov = ScriptedProvider("deepseek", lambda m, r: json.dumps(
        {"better": "甲", "quote": "比如速度就是位置的导数。", "reason": "有例子"}
        if _shown(r.prompt)[0] == A_TEXT else
        {"better": "乙", "quote": "比如速度就是位置的导数。", "reason": "有例子"}))
    other = ScriptedProvider("anthropic", lambda m, r: "{}")
    llm = _deep_llm({"anthropic": other})
    gw = judge_gateway(llm, judge_for("anthropic", BOTH),
                       providers={"deepseek": prov, "anthropic": other})
    assert gw.config.verifier == DS and gw.metered
    out = compare(CASE, A, B, signals_text="", asked_for_more=False, gw=gw, account=ACC)
    assert out.winner == "a"
    assert len(prov.calls) == 2 and other.calls == []
    assert [acc for acc, _ in llm.meter.records] == [ACC, ACC]


def test_judge_gateway_without_the_judges_provider_is_unavailable():
    only = {"anthropic": ScriptedProvider("anthropic", lambda m, r: "{}")}
    with pytest.raises(JudgeUnavailable):
        judge_gateway(_deep_llm(only), DS, providers=only)


def test_two_replays_that_act_differently_are_a_tie_and_the_judge_reads_no_simulated_line():
    from evowiki.harness.judge import ACTIONS_NOTE, content_of

    text = "正文\n\n【模拟】调用 open_review：重放不执行，没有写入\n\n【模拟动作】开考"
    assert content_of(text) == "正文"
    assert ACTIONS_NOTE.format(a="open_review", b="没有动作")
    assert "【模拟修改建议】改一段" in content_of("【模拟修改建议】改一段\n新文字")
