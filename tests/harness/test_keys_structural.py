import pytest

from evowiki.harness import tool_names
from evowiki.harness.keys import (
    HOOKS,
    KEY_BY_NAME,
    LOCKED_TOOLS,
    SPEC_PARTS,
    STEPS_H10,
    TOGGLE_TOOLS,
    key_of,
    needs_approval,
)

NEW = (
    *(f"H3.deep.{t}" for t in TOGGLE_TOOLS["deep"]),
    *(f"H3.assistant.{t}" for t in TOGGLE_TOOLS["assistant"]),
    "H8.deep.spec.plan", "H8.deep.spec.write", "H8.deep.evoui_examples",
    "H9.deep.before_write", "H9.assistant.before_answer", "H11.deep.after_write",
    *(f"H10.{s}.tier" for s in STEPS_H10), *(f"H10.{s}.thinking" for s in STEPS_H10),
)


def test_every_new_key_is_registered():
    for n in NEW:
        assert n in KEY_BY_NAME, n
    assert KEY_BY_NAME["H3.deep.read_k"].kind == "bool"
    assert KEY_BY_NAME["H3.assistant.search_wiki"].doc == "助手：能不能用 search_wiki"
    assert KEY_BY_NAME["H3.deep.read_k"].doc == "写页面：能不能用 read_k"
    spec = KEY_BY_NAME["H8.deep.spec.write"]
    assert (spec.kind, spec.options, spec.lo, spec.hi) == ("list", SPEC_PARTS, 0, len(SPEC_PARTS))
    assert KEY_BY_NAME["H9.deep.before_write"].kind == "steps"
    assert KEY_BY_NAME["H11.deep.after_write"].dim == "H11"
    assert KEY_BY_NAME["H10.deep.write.tier"].options == ("deep", "good")
    assert KEY_BY_NAME["H10.bot.plan.tier"].options == ("fast", "good")


def test_approval_marker():
    for n in NEW:
        if n.split(".")[0] in ("H3", "H9", "H10", "H11"):
            assert needs_approval(n), n
    assert needs_approval("H8.component.x1.presets")
    assert not needs_approval("H8.component.x1.note")
    assert not needs_approval("H8.deep.evoui_examples")
    assert not needs_approval("H8.deep.spec.write")
    assert not needs_approval("H4.deep.max_tool_calls")
    with pytest.raises(KeyError):
        needs_approval("H3.deep.write_section")


def test_locked_tools_have_no_h3_key():
    for agent, tools in LOCKED_TOOLS.items():
        for t in tools:
            assert f"H3.{agent}.{t}" not in KEY_BY_NAME
            assert key_of(f"H3.{agent}.{t}") is None


def test_toggle_and_locked_split_the_deep_tools():
    assert not set(TOGGLE_TOOLS["deep"]) & set(LOCKED_TOOLS["deep"])
    assert set(TOGGLE_TOOLS["deep"]) | set(LOCKED_TOOLS["deep"]) == set(tool_names.TOOLS["deep"])
    # stage 3's Skill / confirmation tools are locked (core contracts); every other one toggles
    assert not set(TOGGLE_TOOLS["assistant"]) & set(LOCKED_TOOLS["assistant"])
    assert (set(TOGGLE_TOOLS["assistant"]) | set(LOCKED_TOOLS["assistant"])
            == set(tool_names.TOOLS["assistant"]))


def test_hook_tools_belong_to_their_agent():
    for at, hook in HOOKS.items():
        agent = at.split(".")[0]
        for t in hook.tools:
            assert t in tool_names.TOOLS[agent], (at, t)


def test_component_pattern_keys():
    note = key_of("H8.component.number-line_1.note")
    assert note is not None and (note.dim, note.kind, note.lo, note.hi) == ("H8", "text", 1, 400)
    presets = key_of("H8.component.number-line_1.presets")
    assert presets is not None and (presets.kind, presets.approval) == ("json", "ask")
    assert key_of("H8.component.Bad Id.note") is None
    assert key_of("H8.component.x.note") is None           # id needs 2+ characters
    assert key_of("H8.component.abc.other") is None


def test_deep_tells_bad_values_from_missing_tiers_with_every_anchored_tier():
    from evowiki.deep.agent import _EVERY_TIER

    assert set(_EVERY_TIER) == {"deep", "fast", "good"}
