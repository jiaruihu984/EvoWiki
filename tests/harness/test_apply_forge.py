from evowiki.harness import apply
from evowiki.harness.pack import HarnessPack


def test_forge_limits_default_and_lowered():
    assert apply.forge_limits(HarnessPack(0, {}), "tool", 60) == 60
    assert apply.forge_limits(HarnessPack(1, {"H4.forge.max_tool_calls": 20}), "tool", 60) == 20
    assert apply.forge_limits(HarnessPack(1, {"H4.forge.patch.max_tool_calls": 30}), "patch", 80) == 30


def test_forge_tier():
    assert apply.forge_tier(HarnessPack(0, {}), "tool") is None
    assert apply.forge_tier(HarnessPack(1, {"H10.forge.code.tier": "deep"}), "tool") == "deep"
