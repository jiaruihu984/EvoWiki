from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import clip, fail, run_guarded, spec
from evowiki.agent.tools import ToolResult


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(min_length=1, max_length=10)


def test_clip_keeps_short_text_and_marks_cut():
    assert clip("abc", 10) == "abc"
    out = clip("x" * 500, 100)
    assert len(out) <= 100 and out.startswith("x") and "截断" in out and "500" in out


def test_fail_carries_error():
    r = fail("页面不存在")
    assert r.error == "页面不存在" and "页面不存在" in r.content


def test_run_guarded_validates_and_never_raises():
    ok = run_guarded(_Args, {"q": "hi"}, lambda a: ToolResult(content=a.q))
    assert ok.content == "hi" and ok.error is None
    bad = run_guarded(_Args, {"q": ""}, lambda a: ToolResult(content="never"))
    assert bad.error is not None and "参数" in bad.content
    extra = run_guarded(_Args, {"q": "a", "z": 1}, lambda a: ToolResult(content="never"))
    assert extra.error is not None

    def boom(a):
        raise RuntimeError("db down")
    crashed = run_guarded(_Args, {"q": "a"}, boom)
    assert crashed.error is not None and "RuntimeError" in crashed.error


def test_run_guarded_clips_oversized_content():
    r = run_guarded(_Args, {"q": "a"}, lambda a: ToolResult(content="y" * 11500))
    assert len(r.content) <= 11000 and "截断" in r.content


def test_spec_takes_schema_from_model():
    s = spec("read_x", "读 x。", _Args, "read")
    assert s.parameters["properties"]["q"]["maxLength"] == 10
    assert s.parameters.get("additionalProperties") is False
