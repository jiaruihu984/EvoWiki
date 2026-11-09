import pytest

from evowiki.harness.keys import HOOKS
from evowiki.harness.pack import EMPTY, HarnessInvalid, HarnessPack, validate


def _step(at="deep.before_write"):
    h = HOOKS[at]
    return {"at": at, "kind": h.kinds[0], "tier": h.tiers[0], "tools": ["read_k"],
            "instruction": "先读", "output_to": h.output_to, "max_tool_calls": 2}


PRESET = {"name": "正态", "use": "讲分布时", "config": {"mu": 0}}
EXAMPLE = {"name": "选择题", "use": "检查理解", "block": {"type": "x"}}


@pytest.mark.parametrize("name,value,out", [
    ("H3.deep.read_k", False, False),
    ("H3.assistant.web_search", True, True),
    ("H10.deep.write.tier", "good", "good"),
    ("H10.assistant.turn.thinking", False, False),
    ("H8.deep.spec.plan", ["exemplar", "items"], ["exemplar", "items"]),
    ("H8.deep.spec.write", [], []),
    ("H8.component.number-line.note", "  讲分布时用  ", "讲分布时用"),
])
def test_valid_values(name, value, out):
    assert validate(name, value) == out


def test_structured_values_are_normalised():
    assert validate("H9.deep.before_write", [_step()]) == [_step()]
    assert validate("H8.deep.evoui_examples", [EXAMPLE]) == [EXAMPLE]
    assert validate("H8.component.number-line.presets", [PRESET]) == [PRESET]


@pytest.mark.parametrize("name,value,reason", [
    ("H3.deep.read_k", 1, "要是 true 或 false"), ("H3.deep.read_k", "false", "要是 true 或 false"),
    ("H3.deep.read_k", None, "要是 true 或 false"),
    ("H10.deep.write.tier", "fast", "只能是 deep、good 之一"),
    ("H10.deep.write.tier", 1, "只能是 deep、good 之一"),
    ("H10.bot.plan.thinking", 0, "要是 true 或 false"),
    ("H8.deep.spec.plan", ["items", "items"], "里有重复的项"),
    ("H8.deep.spec.plan", ["nope"], "只能含 items、exemplar"),
    ("H8.deep.spec.plan", "items", "要是文字列表"), ("H8.deep.spec.plan", [1], "要是文字列表"),
    ("H8.component.number-line.note", "", "不能是空的"),
    ("H8.component.number-line.note", "x" * 401, "最多 400 个字"),
    ("H8.component.number-line.note", "⟦", "里不能有 ⟦ 或 ⟧"),
    ("H8.deep.evoui_examples", [], "要是一个不为空的列表"),
    ("H8.deep.evoui_examples", [PRESET], "缺少「交互块」"),
    ("H8.component.number-line.presets", [EXAMPLE], "缺少「配置」"),
    ("H8.component.number-line.presets", {}, "要是一个不为空的列表"),
    ("H9.deep.before_write", [_step("assistant.before_answer")], "位置要是 deep.before_write"),
    ("H11.deep.after_write", [_step()], "位置要是 deep.after_write"),
    ("H4.deep.max_tool_calls", True, "要是整数"),
    ("H3.deep.write_section", False, "没有这个设置"),
])
def test_bad_values_are_refused(name, value, reason):
    with pytest.raises(HarnessInvalid, match=reason):
        validate(name, value)


def test_accessors_on_an_empty_pack():
    assert EMPTY.flag("H3.deep.read_k", True) is True
    assert EMPTY.choice("H10.deep.write.tier", None) is None
    assert EMPTY.choice("H10.deep.write.tier", "deep") == "deep"
    assert EMPTY.items("H9.deep.before_write") is None
    assert EMPTY.items("H8.component.abc.presets") is None
    with pytest.raises(KeyError):
        EMPTY.flag("H3.deep.finish", True)


@pytest.mark.parametrize("call", [
    lambda: EMPTY.flag("H10.deep.write.tier", True),
    lambda: EMPTY.flag("H4.deep.max_tool_calls", True),
    lambda: EMPTY.choice("H3.deep.read_k", None),
    lambda: EMPTY.choice("H8.deep.spec.write", None),
    lambda: EMPTY.items("H10.deep.write.tier"),
    lambda: EMPTY.items("H8.component.abc.note"),
])
def test_accessor_of_the_wrong_kind_is_a_type_error(call):
    with pytest.raises(TypeError):
        call()


def test_accessors_read_values():
    p = HarnessPack(2, {"H3.deep.read_k": False, "H10.deep.plan.tier": "good",
                        "H8.deep.spec.write": ["items"], "H9.deep.before_write": [_step()]})
    assert p.flag("H3.deep.read_k", True) is False
    assert p.choice("H10.deep.plan.tier", None) == "good"
    assert p.items("H8.deep.spec.write") == ["items"]
    assert p.items("H9.deep.before_write")[0]["at"] == "deep.before_write"


def test_stored_tier_the_step_may_not_use_fails_on_read():
    p = HarnessPack(1, {"H10.deep.write.tier": "fast"})
    with pytest.raises(HarnessInvalid, match="只能是 deep、good 之一"):
        p.choice("H10.deep.write.tier", None)
