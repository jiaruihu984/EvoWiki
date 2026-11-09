import json
from pathlib import Path

import pytest

from evowiki.agent.tools import ToolResult, ToolSpec
from evowiki.harness import apply
from evowiki.harness.pack import EMPTY, HarnessInvalid, HarnessPack
from evowiki.llm.models import THINKING_PARAM
from evowiki.llm.usage import Account
from tests.deep.fakes import component, library
from tests.deep.test_components import CFG, configurable

EXAMPLES = Path(__file__).parents[2] / "src/evowiki/deep/evoui/examples.json"


class _T:
    def __init__(self, name):
        self.spec = ToolSpec(name=name, description=name, effect="read",
                             parameters={"type": "object", "properties": {}})

    def run(self, args, ctx):
        return ToolResult(content="ok")


def P(values):
    return HarnessPack(1, values)


def test_empty_pack_returns_every_input_object():
    tools = [_T("read_k"), _T("finish")]
    roster = {"deep": [object()], "good": [object()]}
    acct = Account(user_id="u", group="assistant")
    params = {"max_tokens": 10}
    assert apply.toolset(EMPTY, "deep", tools) is tools
    assert apply.toolset(EMPTY, "bot", tools) is tools
    assert apply.chain(EMPTY, "deep.write", roster) is roster["deep"]
    assert apply.account_for(EMPTY, "assistant.turn", acct) is acct
    assert apply.account_for(EMPTY, "bot.plan", None) is None
    assert apply.thinking_params(EMPTY, "deep.write", params) is params
    assert apply.hook_steps(EMPTY, "deep.before_write") == ()
    default = ("items",)
    assert apply.spec_parts(EMPTY, "deep.plan", default) is default
    assert apply.components(EMPTY, [component()]) == ({}, {})
    assert apply.evoui_examples(EMPTY) == ()


def test_toolset_drops_an_off_tool_and_keeps_order():
    tools = [_T("read_k"), _T("read_page"), _T("finish"), _T("read_spec")]
    out = apply.toolset(P({"H3.deep.read_page": False}), "deep", tools)
    assert [t.spec.name for t in out] == ["read_k", "finish", "read_spec"]
    assert apply.toolset(P({"H3.deep.read_page": True}), "deep", tools) is tools
    assert apply.toolset(P({"H3.deep.place_component": False}), "deep", tools) is tools


def test_chain_picks_tier_and_refuses_a_missing_one():
    roster = {"deep": ["d"], "good": ["g"]}
    assert apply.chain(P({"H10.deep.write.tier": "good"}), "deep.write", roster) == ["g"]
    assert apply.chain(P({"H10.deep.plan.tier": "deep"}), "deep.plan", roster) is roster["deep"]
    with pytest.raises(HarnessInvalid, match="deep.plan"):
        apply.chain(P({"H10.deep.plan.tier": "good"}), "deep.plan", {"deep": ["d"]})


def test_account_for_sets_the_pin():
    base = Account(user_id="u", group="assistant")
    out = apply.account_for(P({"H10.assistant.turn.tier": "good"}), "assistant.turn", base)
    assert out.tier_pin == "good" and base.tier_pin is None
    assert apply.account_for(P({"H10.bot.plan.tier": "fast"}), "bot.plan", None) is None


def test_thinking_params_sets_the_key_on_a_copy():
    params = {"max_tokens": 10}
    out = apply.thinking_params(P({"H10.deep.write.thinking": False}), "deep.write", params)
    assert out == {"max_tokens": 10, THINKING_PARAM: False} and params == {"max_tokens": 10}
    assert apply.thinking_params(P({"H10.bot.plan.thinking": True}), "bot.plan", {})[
        THINKING_PARAM] is True


def test_hook_steps_and_spec_parts():
    step = {"at": "deep.before_write", "kind": "delegate", "tier": "fast", "tools": ["read_k"],
            "instruction": "先查一下", "output_to": "context.notes", "max_tool_calls": 3}
    got = apply.hook_steps(P({"H9.deep.before_write": [step]}), "deep.before_write")
    assert len(got) == 1 and got[0].instruction == "先查一下"
    assert apply.hook_steps(P({"H9.deep.before_write": [step]}), "deep.after_write") == ()
    assert apply.spec_parts(P({"H8.deep.spec.write": ["exemplar"]}), "deep.write",
                            ("items",)) == ("exemplar",)


def test_components_notes_and_presets():
    comps = [component("fixed-one"), configurable()]
    preset = {"name": "复制", "use": "讲复制", "config": CFG}
    notes, presets = apply.components(P({
        "H8.component.fixed-one.note": "点按钮看计数",
        "H8.component.steps-demo.presets": [preset]}), comps)
    assert notes == {"fixed-one": "点按钮看计数"}
    assert presets["steps-demo"][0].name == "复制"


def test_components_refuse_the_three_bad_cases():
    comps = [component("fixed-one"), configurable()]
    preset = {"name": "复制", "use": "讲复制", "config": CFG}
    with pytest.raises(HarnessInvalid, match="ghost"):
        apply.components(P({"H8.component.ghost.note": "x"}), comps)
    with pytest.raises(HarnessInvalid, match="ghost"):
        apply.components(P({"H8.component.ghost.presets": [preset]}), comps)
    with pytest.raises(HarnessInvalid, match="固定内容"):
        apply.components(P({"H8.component.fixed-one.presets": [preset]}), comps)
    bad = {**preset, "config": {**CFG, "steps": 1}}
    with pytest.raises(HarnessInvalid, match="^组件 steps-demo 的预设「复制」配置不合法$"):
        apply.components(P({"H8.component.steps-demo.presets": [bad]}), comps)
    assert library("pair-demo").get("pair-demo")


def test_evoui_examples_returns_valid_and_refuses_invalid():
    ex = json.loads(EXAMPLES.read_text(encoding="utf-8"))[0]
    got = apply.evoui_examples(P({"H8.deep.evoui_examples": [ex]}))
    assert got == ({"name": ex["name"], "use": ex["use"], "block": ex["block"], "group": ex["group"]},)
    bad = {**ex, "block": {"nonsense": True}}
    with pytest.raises(HarnessInvalid, match="块不合法"):
        apply.evoui_examples(P({"H8.deep.evoui_examples": [bad]}))
