"""diff.refuse / needs_ask / cost_keys / h12_* for the phase-3b structural and forged-tool keys."""

import pytest

from evowiki.harness.diff import (
    NO_PATCH,
    UNMEASURABLE,
    KeyOp,
    PackDiff,
    complexity,
    cost_keys,
    h12_patch,
    h12_tool,
    needs_ask,
    refuse,
)
from evowiki.harness.pack import EMPTY, HarnessPack

D = {"H4.deep.max_tool_calls": 60}
STEP = {"at": "deep.before_write", "kind": "delegate", "tier": "fast", "tools": ["read_page"],
        "instruction": "先读相关页面写摘要", "output_to": "context.notes", "max_tool_calls": 3}
CLOSED = "所属的功能不能自动改"
OUT = "不在可以自动改的范围内"


def r(*ops, pack=None, sources=None):
    return refuse(PackDiff(tuple(ops)), pack or HarnessPack(0, {}), sources or {}, defaults=D)


def test_structural_deep_keys_open():
    assert r(KeyOp("H3.deep.read_page", False)) is None
    assert r(KeyOp("H10.deep.write.tier", "good")) is None
    assert r(KeyOp("H10.deep.plan.thinking", False)) is None
    assert r(KeyOp("H9.deep.before_write", [STEP])) is None
    assert r(KeyOp("H8.deep.spec.write", ["items"])) is None
    assert r(KeyOp("H3.deep.read_page", None)) is None          # back to the default
    assert r(KeyOp("H9.deep.before_write", None)) is None


def test_other_agents_and_forge_keys_closed():
    # assistant keys follow rule R1 (tests/harness/test_diff_assistant.py)
    assert r(KeyOp("H3.assistant.web_search", False)) is None
    assert UNMEASURABLE in r(KeyOp("H9.assistant.before_answer", []))
    assert UNMEASURABLE in r(KeyOp("H10.assistant.turn.tier", "good"))
    assert OUT in r(KeyOp("H4.forge.max_tool_calls", 10))
    assert OUT in r(KeyOp("H10.forge.code.tier", "deep"))


@pytest.mark.parametrize("op", [
    KeyOp("H3.assistant.a_tool", True),                           # forged opt-in, approval auto
    KeyOp("H3.assistant.a_tool", None),
    KeyOp("H10.bot.plan.tier", "good"),
    KeyOp("H10.bot.plan.thinking", False),
    KeyOp("H12.tool.assistant.a_tool", {"candidate_id": "fc_a"}),  # agent is the 3rd segment
    KeyOp("H12.tool.assistant.a_tool", None),
])
def test_every_non_deep_agent_key_is_refused(op):
    assert CLOSED in r(op)


def test_a_non_deep_forged_tool_is_refused_even_with_its_pair():
    got = r(KeyOp("H3.assistant.a_tool", True),
            KeyOp("H12.tool.assistant.a_tool", {"candidate_id": "fc_a"}))
    assert CLOSED in got


@pytest.mark.parametrize("op", [
    KeyOp("H4.forge.max_tool_calls", 10),
    KeyOp("H4.forge.patch.max_tool_calls", 10),
    KeyOp("H4.forge.max_tool_calls", None),
    KeyOp("H10.forge.code.tier", "good"),
    KeyOp("H10.forge.code.thinking", True),
])
def test_forge_keys_are_out_of_reach_not_an_agent(op):
    got = r(op)
    assert OUT in got and CLOSED not in got


def test_code_patches_are_never_harness_changes():
    got = r(KeyOp("H12.patch.fc_x", {"candidate_id": "fc_x", "commit": "abcdef1"}))
    assert NO_PATCH in got
    assert NO_PATCH in r(KeyOp("H12.patch.fc_x", None))


def test_keys_without_an_agent_pass_the_agent_rule():
    assert r(KeyOp("H1.write", "先举例")) is None
    assert r(KeyOp("H8.component.quiz-card.note", "做完一节后用")) is None


def test_one_forged_tool_per_diff():
    ops = [KeyOp("H3.deep.a_tool", True), KeyOp("H12.tool.deep.a_tool", {"candidate_id": "fc_a"}),
           KeyOp("H3.deep.b_tool", True), KeyOp("H12.tool.deep.b_tool", {"candidate_id": "fc_b"})]
    assert "一次只能加一个" in r(*ops)


def test_forged_opt_in_needs_its_h12():
    assert "要和" in r(KeyOp("H3.deep.a_tool", True))     # an opt-in without its candidate
    assert "要和" in r(KeyOp("H3.deep.a_tool", True),     # with another tool's candidate
                       KeyOp("H12.tool.deep.b_tool", {"candidate_id": "fc_b"}))
    assert r(KeyOp("H3.deep.a_tool", True),
             KeyOp("H12.tool.deep.a_tool", {"candidate_id": "fc_a"})) is None
    assert r(KeyOp("H3.deep.a_tool", False)) is None      # switching one off needs no candidate


def test_the_improver_never_gives_a_registry_version():
    got = r(KeyOp("H3.deep.a_tool", True),
            KeyOp("H12.tool.deep.a_tool", {"candidate_id": "fc_a", "registry_version": 2}))
    assert "注册表版本" in got


def test_user_set_structural_keys_are_not_touched():
    assert "你自己设的" in r(KeyOp("H3.deep.read_page", False),
                            sources={"H3.deep.read_page": "user"})


def test_needs_ask_and_cost_keys():
    assert needs_ask(PackDiff((KeyOp("H3.deep.read_page", False),)))
    assert not needs_ask(PackDiff((KeyOp("H3.deep.a_tool", True),
                                   KeyOp("H12.tool.deep.a_tool", {"candidate_id": "fc_a"}))))
    assert not needs_ask(PackDiff((KeyOp("H1.write", "x"),)))
    assert needs_ask(PackDiff((KeyOp("H1.write", "x"), KeyOp("H10.deep.write.tier", "good"))))
    assert cost_keys(PackDiff((KeyOp("H10.deep.write.tier", "good"),)))
    assert cost_keys(PackDiff((KeyOp("H4.deep.max_tool_calls", 70),)))
    assert not cost_keys(PackDiff((KeyOp("H3.deep.read_page", False),)))
    assert h12_tool(PackDiff((KeyOp("H12.tool.deep.a_tool", {"candidate_id": "fc_a"}),))) == (
        "deep", "a_tool", "fc_a")
    assert h12_tool(PackDiff((KeyOp("H1.write", "x"),))) is None
    assert h12_patch(PackDiff((KeyOp("H12.patch.fc_x", None),))) == "fc_x"
    assert h12_patch(PackDiff()) is None


def test_complexity_prices_structured_values_by_their_dump():
    one = PackDiff((KeyOp("H9.deep.before_write", [STEP]),))
    c = complexity(EMPTY, one, {})
    assert c > 1.0                                        # the op + its added characters
    assert complexity(HarnessPack(1, {"H9.deep.before_write": [STEP]}), one, {}) == pytest.approx(1.0)
    assert complexity(EMPTY, PackDiff((KeyOp("H3.deep.read_page", False),)), {}) == pytest.approx(1.0)
    assert complexity(EMPTY, PackDiff((KeyOp("H8.deep.spec.write", ["items"]),)), {}) == \
        pytest.approx(1.0)
