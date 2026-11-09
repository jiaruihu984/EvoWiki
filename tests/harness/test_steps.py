import pytest

from evowiki.harness.keys import HOOK_MAX_TOOL_CALLS, HOOKS
from evowiki.harness.pack import HarnessInvalid
from evowiki.harness.steps import parse_examples, parse_presets, parse_steps

KEY = {"deep.before_write": "H9.deep.before_write", "deep.after_write": "H11.deep.after_write",
       "assistant.before_answer": "H9.assistant.before_answer"}


def _step(at="deep.before_write", **kw):
    h = HOOKS[at]
    d = {"at": at, "kind": h.kinds[0], "tier": h.tiers[0], "tools": list(h.tools[:2]),
         "instruction": "  先读一下相关页面  ", "output_to": h.output_to, "max_tool_calls": 3}
    d.update(kw)
    return d


@pytest.mark.parametrize("at", list(HOOKS))
def test_valid_round_trip(at):
    out = parse_steps(KEY[at], [_step(at), _step(at, tools=[], max_tool_calls=0)])
    assert len(out) == 2 and out[0].at == at and out[0].instruction == "先读一下相关页面"
    again = parse_steps(KEY[at], [s.model_dump(mode="json") for s in out])
    assert again == out


@pytest.mark.parametrize("key,value,reason", [
    ("H9.deep.before_write", [_step(at="deep.after_write")], "位置要是 deep.before_write"),
    ("H9.deep.before_write", [_step(kind="self_check")], "这里只能是 delegate"),
    ("H11.deep.after_write", [_step("deep.after_write", kind="delegate")], "这里只能是 self_check"),
    ("H9.assistant.before_answer", [_step("assistant.before_answer", tier="deep")],
     "这里不能用 deep 这一档"),
    ("H9.deep.before_write", [_step(tools=["write_section"])], "这里不能用工具 write_section"),
    ("H9.deep.before_write", [_step(tools=["read_k", "read_k"])], "工具重复了"),
    ("H9.deep.before_write", [_step(max_tool_calls=HOOK_MAX_TOOL_CALLS + 1)],
     "「最多调用几次工具」最多是 12"),
    ("H9.deep.before_write", [_step(max_tool_calls=-1)], "「最多调用几次工具」最少是 0"),
    ("H9.deep.before_write", [_step(instruction="   ")], "说明不能是空的"),
    ("H9.deep.before_write", [_step(instruction="读⟦一下")], "说明里不能有 ⟦ 或 ⟧"),
    ("H9.deep.before_write", [], "要是一个不为空的列表"),
    ("H9.deep.before_write", [_step(extra=1)], "多了不认识的字段「extra」"),
    ("H9.deep.before_write", [_step(output_to="context.must_fix")], "结果要放到 context.notes"),
    ("H9.deep.before_write", [_step(max_tool_calls=True)], "「最多调用几次工具」要是整数"),
    ("H9.deep.before_write", [_step(tier="best")], "「档位」只能是 fast、good、deep 之一"),
    ("H9.deep.before_write", [{"at": "deep.before_write"}], "缺少「种类」"),
    ("H9.deep.before_write", ["x"], "每一项都要是键值对象"),
    ("H9.deep.before_write", {"at": "deep.before_write"}, "要是一个不为空的列表"),
    ("H11.assistant.before_answer", [_step("assistant.before_answer", kind="self_check")],
     "没有这个设置"),
])
def test_refusals(key, value, reason):
    with pytest.raises(HarnessInvalid, match=reason):
        parse_steps(key, value)


def test_presets_and_examples():
    p = parse_presets([{"name": "正态", "use": "讲分布时", "config": {"mu": 0}}])
    assert p[0].config == {"mu": 0}
    e = parse_examples([{"name": "选择题", "use": "检查理解", "block": {"type": "x"}}])
    assert e[0].block == {"type": "x"}
    for bad, reason in (
            ([], "要是一个不为空的列表"),
            ([{"name": "a", "use": "b", "config": {}}] * 2, "里有重名的项"),
            ([{"name": "  ", "use": "b", "config": {}}], "名字不能是空的"),
            ([{"name": "a", "use": " ", "config": {}}], "用途不能是空的"),
            ([{"name": "a" * 41, "use": "b", "config": {}}], "名字最多 40 个字"),
            ([{"name": "a", "use": "b", "config": {}, "x": 1}], "多了不认识的字段「x」"),
            ([{"name": 3, "use": "b", "config": {}}], "「名字」要是文字"),
            ([{"name": "a", "use": "b", "config": 3}], "「配置」要是键值对象"),
            ("x", "要是一个不为空的列表")):
        with pytest.raises(HarnessInvalid, match=reason):
            parse_presets(bad)
    with pytest.raises(HarnessInvalid, match="缺少「交互块」"):
        parse_examples([{"name": "a", "use": "b"}])
    assert parse_presets([{"name": " 正态 ", "use": " 讲分布 ", "config": {}}])[0].name == "正态"


def test_reason_of_a_foreign_validator_is_plain_chinese():
    """A model whose validators are not ours (English messages): the reason names the field."""
    from pydantic import BaseModel, ValidationError, field_validator

    from evowiki.harness.steps import _reason

    class Foreign(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def _n(cls, v: str) -> str:
            raise ValueError("must be kebab-case")

    with pytest.raises(ValidationError) as exc:
        Foreign(name="x")
    err = exc.value.errors(include_url=False)[0]
    assert _reason(Foreign, err, own_text=False) == "「名字」不合法"
    assert _reason(Foreign, err) == "must be kebab-case"
