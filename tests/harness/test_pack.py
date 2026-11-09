import pytest

from evowiki.harness.pack import EMPTY, HarnessInvalid, HarnessPack, validate


def test_empty_pack_returns_defaults():
    assert EMPTY.empty and EMPTY.version == 0
    assert EMPTY.int("H4.deep.max_tool_calls", 80) == 80
    assert EMPTY.text("H1.write", "default") == "default"
    assert EMPTY.guidance() == {}


def test_values_win_over_defaults():
    p = HarnessPack(3, {"H4.deep.max_tool_calls": 100, "H1.write": "先举例"})
    assert p.int("H4.deep.max_tool_calls", 80) == 100
    assert p.guidance() == {"write": "先举例"}


def test_unknown_key_is_refused_on_read_and_write():
    with pytest.raises(KeyError):
        EMPTY.int("H4.deep.max_tool_call", 80)       # typo: fail closed
    with pytest.raises(HarnessInvalid):
        validate("H9.nope", 1)


@pytest.mark.parametrize("name,value", [
    ("H4.deep.max_tool_calls", 9), ("H4.deep.max_tool_calls", 201),
    ("H4.deep.max_tool_calls", "80"), ("H4.deep.max_tool_calls", True),
    ("H1.write", ""), ("H1.write", "x" * 4001), ("H1.write", 3),
    ("H2.deep.write_block", "x" * 1001),
    ("H1.write", "先举例⟦g:write⟧"), ("H1.write", "先举例⟦/g⟧"),
    ("H2.deep.write_block", "写⟦块"), ("H2.deep.write_block.why", "写⟧块"),
])
def test_bad_values_are_refused(name, value):
    with pytest.raises(HarnessInvalid):
        validate(name, value)


@pytest.mark.parametrize("name", [
    # phase 3b: an unknown deep / assistant tool name is a forged tool's by pattern, so the
    # refused names are a bad argument of a built-in, a bot tool, a reserved forge tool, a bad name
    "H2.deep.write_block.nope", "H2.bot.read_page", "H2.deep.call_tool", "H2.assistant.Finish"])
def test_h2_name_outside_the_tool_registry_is_refused(name):
    with pytest.raises(HarnessInvalid, match="没有这个设置"):
        validate(name, "说明")


def test_h2_names_of_the_registry_are_accepted():
    for name in ("H2.deep.place_component", "H2.assistant.search_wiki.query", "H2.bot.no_action"):
        assert validate(name, "说明") == "说明"


def test_stored_name_with_no_key_is_invalid_not_a_key_error():
    """A stored row whose key is gone (a removed step, a renamed tool) fails closed as
    HarnessInvalid; a code lookup of an unknown name stays a KeyError."""
    p = HarnessPack(1, {"H1.no_such_step": "x", "H2.bot.no_such_tool": "y"})
    with pytest.raises(HarnessInvalid, match="没有这个设置"):
        p.guidance()
    with pytest.raises(HarnessInvalid, match="没有这个设置"):
        p.with_prefix("H2.bot.")
    with pytest.raises(KeyError):
        p.get("H1.no_such_step")


def test_text_is_stripped_and_kept():
    assert validate("H1.write", "  先举例  ") == "先举例"
    assert validate("H2.deep.write_block", "写一个块") == "写一个块"


def test_bad_row_in_pack_fails_on_read():
    p = HarnessPack(1, {"H4.deep.max_tool_calls": "eighty"})
    with pytest.raises(HarnessInvalid):
        p.int("H4.deep.max_tool_calls", 80)
