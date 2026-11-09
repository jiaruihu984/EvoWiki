import pytest

from evowiki.harness import keys, tool_names
from evowiki.harness.pack import HarnessInvalid, validate


def test_forged_opt_in_is_auto_bool():
    k = keys.key_of("H3.deep.check_balanced")
    assert k is not None and k.dim == "H3" and k.kind == "bool" and k.approval == "auto"
    assert keys.is_forged_opt_in("H3.deep.check_balanced")
    assert validate("H3.deep.check_balanced", True) is True


def test_builtin_toggle_keeps_ask_and_locked_stays_unknown():
    assert keys.needs_approval("H3.deep.read_page")
    assert not keys.is_forged_opt_in("H3.deep.read_page")
    assert keys.key_of("H3.deep.finish") is None          # locked tools are never a key
    assert keys.key_of("H3.bot.anything") is None         # bot has no forged tools in 3b


def test_is_builtin_is_the_static_registry_only():
    assert tool_names.is_builtin("deep", "read_page")
    assert not tool_names.is_builtin("deep", "check_balanced")
    assert not tool_names.is_builtin("bot", "read_page")
    assert keys.h12_tool_key("deep", "x_y") == "H12.tool.deep.x_y"
    assert keys.h12_patch_key("fc_1") == "H12.patch.fc_1"


def test_reserved_forge_names_are_not_keys():
    for n in ("call_tool", "find_tool", "request_tool"):
        assert keys.key_of(f"H3.deep.{n}") is None


def test_h2_for_forged_tool_by_pattern():
    assert validate("H2.deep.check_balanced", "判断方程式是否配平") == "判断方程式是否配平"
    assert validate("H2.deep.check_balanced.eq", "方程式") == "方程式"


def test_h12_values():
    assert keys.key_of("H12.tool.deep.check_balanced").approval == "auto"
    assert keys.needs_approval("H12.patch.fc_abc123")
    v = validate("H12.tool.deep.check_balanced", {"candidate_id": "fc_abc123"})
    assert v == {"candidate_id": "fc_abc123"}
    validate("H12.tool.deep.check_balanced", {"candidate_id": "fc_abc123", "registry_version": 3})
    validate("H12.patch.fc_abc123", {"candidate_id": "fc_abc123", "commit": "a" * 40})
    with pytest.raises(HarnessInvalid):
        validate("H12.patch.fc_abc123", {"candidate_id": "fc_other", "commit": "a" * 40})
    with pytest.raises(HarnessInvalid):
        validate("H12.tool.deep.check_balanced", {"candidate_id": "x"})


def test_forge_limit_keys_only_lower():
    # The equality with forge/limits.py (CODER_MAX_TOOL_CALLS / L2_MAX_TOOL_CALLS) is asserted
    # once the forge package lands on master.
    k = keys.key_of("H4.forge.max_tool_calls")
    assert (k.lo, k.hi) == (1, keys.FORGE_CAP)
    k = keys.key_of("H4.forge.patch.max_tool_calls")
    assert (k.lo, k.hi) == (1, keys.FORGE_PATCH_CAP)


def test_h10_forge_code():
    assert keys.STEPS_H10["forge.code"] == ("forge.code", ("good", "deep"))
    assert keys.needs_approval("H10.forge.code.tier")


def test_a_trailing_newline_is_never_a_key_or_a_name():
    """`$` with `re.match` accepts a trailing newline; every 3b pattern uses fullmatch (T1)."""
    for name in ("H3.deep.check_balanced\n", "H12.tool.deep.check_balanced\n",
                 "H12.patch.fc_abc\n", "H2.deep.read_page\n", "H2.deep.check_balanced.x\n"):
        assert keys.key_of(name) is None, name
    assert not keys.is_h2("H2.deep.read_page\n")
    assert not tool_names.known("deep", "check_balanced\n", None)
    assert not tool_names.known("deep", "check_balanced", "arg\n")
    with pytest.raises(HarnessInvalid):
        validate("H12.tool.deep.check_balanced", {"candidate_id": "fc_abc\n"})
    with pytest.raises(HarnessInvalid):
        validate("H12.patch.fc_abc", {"candidate_id": "fc_abc", "commit": "abcdef1\n"})
