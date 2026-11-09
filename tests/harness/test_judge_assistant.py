"""The assistant-answer judge (follow-up 3, J1): scripted judge replies only, no real provider."""

import hashlib
import json
import re
from datetime import UTC, datetime

from evowiki.harness.judge import compare
from evowiki.harness.judge_prompt import (
    ASSISTANT_SYSTEM,
    JUDGE_PURPOSE,
    JUDGE_VERSION,
    PROMPT_VERSION_ASSISTANT,
    SYSTEM,
    assistant_judge_prompt,
    judge_prompt,
)
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig
from evowiki.llm.prices import PriceTable
from evowiki.llm.scripted import ScriptedProvider
from evowiki.llm.usage_control import UsageControl
from tests.harness.test_judge import ACC, CASE, DS, _Meter, res
from tests.runtime.llm_helpers import MemoryCallLog

A_TEXT = "先别急着改页面。你可以先告诉我哪一步卡住了。\n\n我建议先把例子换成速度和位置的关系。"
B_TEXT = "我已经重写了整页，把所有章节都改了一遍。\n\n导数是一种极限，它的定义用到了差商，下面逐条展开讲解。"
A = res(A_TEXT, "t-a")
B = res(B_TEXT, "t-b")
SIGNALS = "拒绝了：整页重写"


def _shown(prompt: str) -> tuple[str, str]:
    m = re.search(r"【甲】\n(.*?)\n\n【乙】\n(.*?)\n\n只比较", prompt, re.DOTALL)
    assert m, prompt
    return m.group(1), m.group(2)


def gateway(decide):
    """A gateway whose judge answers each call with `decide(first_text, second_text)` (assistant
    template) or, for a page prompt, with `same`."""
    def respond(model, req):
        if "这只是页面里的一段" in req.prompt:
            out = {"better": "same", "quote": "", "reason": ""}
        else:
            out = decide(*_shown(req.prompt))
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)

    prov = ScriptedProvider(DS.provider, respond)
    gw = Gateway(GatewayConfig(generator=DS, verifier=DS, allow_single_provider=True),
                 MemoryCallLog(), mode="record", providers={DS.provider: prov},
                 usage=UsageControl(meter=_Meter(), prices=PriceTable({})),
                 clock=lambda: datetime(2026, 10, 23, tzinfo=UTC))
    gw.provider = prov
    return gw


def run(decide, a=A, b=B, *, more=False, kind="assistant"):
    gw = gateway(decide)
    out = compare(CASE, a, b, signals_text=SIGNALS, asked_for_more=more, gw=gw, account=ACC,
                  kind=kind)
    return out, gw


def prefer(target: str, quote: str | None = None):
    def decide(first, second):
        q = quote if quote is not None else target.splitlines()[-1]
        return {"better": "甲" if first == target else "乙", "quote": q, "reason": "更贴合"}
    return decide


def test_a_win_needs_the_same_unique_quote_in_both_orders():
    out, gw = run(prefer(A_TEXT))
    assert (out.winner, out.flipped) == ("a", False)
    assert out.quote == A_TEXT.splitlines()[-1]
    assert [_shown(r.prompt) for r in gw.provider.calls] == [(A_TEXT, B_TEXT), (B_TEXT, A_TEXT)]


def test_a_quote_from_the_other_answer_is_a_tie():
    out, _ = run(prefer(A_TEXT, B_TEXT.splitlines()[-1]))
    assert out.winner == "tie" and not out.flipped


def test_a_flipped_order_is_a_tie():
    out, _ = run(lambda f, s: {"better": "甲", "quote": f.splitlines()[-1], "reason": "x"})
    assert (out.winner, out.flipped) == ("tie", True)


def test_longer_candidate_is_a_tie_unless_asked_for_more():
    tail = "再补充一点。" * 80
    long_a = res(A_TEXT + "\n\n" + tail, "t-a2")
    assert run(prefer(long_a.text, tail), a=long_a)[0].winner == "tie"
    assert run(prefer(long_a.text, tail), a=long_a, more=True)[0].winner == "a"


def test_request_carries_the_assistant_version_and_prompt():
    _, gw = run(prefer(A_TEXT))
    req = gw.provider.calls[0]
    assert (req.purpose, req.prompt_version) == (JUDGE_PURPOSE, PROMPT_VERSION_ASSISTANT)
    assert PROMPT_VERSION_ASSISTANT == "harness.judge.assistant@v2"
    assert req.system == ASSISTANT_SYSTEM and SIGNALS in req.prompt
    assert req.prompt == assistant_judge_prompt(A_TEXT, B_TEXT, signals=SIGNALS)


def test_assistant_prompt_is_blind_and_not_page_worded():
    text = assistant_judge_prompt("x", "y", signals="s") + ASSISTANT_SYSTEM
    for word in ("harness", "Harness", "模型", "新", "旧", "手册", "候选", "anthropic", "deepseek",
                 "页面", "回忆线索", "讲解正文", "这一段", "段"):
        assert word not in text, word


def test_page_default_request_is_unchanged():
    gw = gateway(lambda f, s: {"better": "same", "quote": "", "reason": ""})
    compare(CASE, res("甲文", "r1"), res("乙文", "r2"), signals_text="例子不够",
            asked_for_more=False, gw=gw, account=ACC)
    req = gw.provider.calls[0]
    assert (req.purpose, req.prompt_version, req.system) == (JUDGE_PURPOSE, JUDGE_VERSION, SYSTEM)
    assert req.prompt == judge_prompt("甲文", "乙文", signals="例子不够", anchor=CASE.anchor)


def test_page_prompt_text_is_byte_identical():
    """The page judge is a protected anchor: only the user changes it (v2, 2026-11-05)."""
    p = "|".join([judge_prompt("甲文", "乙文", signals="例子不够", anchor=True),
                  judge_prompt("x", "y", signals="", anchor=False),
                  SYSTEM, JUDGE_VERSION, JUDGE_PURPOSE])
    assert hashlib.sha256(p.encode()).hexdigest() == (
        "eb579fc78f9b437e48edd99d69aff595e76b46492b07a5708a03301f326564da")
