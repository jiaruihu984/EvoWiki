"""harness/hooks.run_hook: an H9 delegate / H11 self-check step run as a small agent on
agent.loop (spec §3.3, rulings R7 / R8)."""

from typing import Any

import pytest

from evowiki.agent.tools import ToolContext, ToolResult, ToolSpec
from evowiki.harness.hooks import CLIP_MARK, HOOK_SYSTEM, PROMPT_VERSION, report_tool, run_hook
from evowiki.harness.keys import HOOK_OUTPUT_MAX
from evowiki.harness.pack import HarnessInvalid
from evowiki.harness.steps import HookStep
from evowiki.llm.chat import ChatTurn
from evowiki.llm.gateway import ProviderUnavailableError
from tests.agent.test_loop import Script
from tests.agent.test_loop_trace import Recorder
from tests.deep.fakes import call, turn

CTX = ToolContext(user_id="u1", session_id="refine:p1", page_id="p1")


class ReadPage:
    spec = ToolSpec(name="read_page", description="读页面", parameters={"type": "object"},
                    effect="read")

    def __init__(self) -> None:
        self.seen: list[tuple[dict[str, Any], ToolContext]] = []

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.seen.append((args, ctx))
        return ToolResult(content="页面正文")


class ReadK(ReadPage):
    spec = ToolSpec(name="read_k", description="读 K", parameters={"type": "object"},
                    effect="read")


def _turns(*items: dict | Exception) -> Script:
    return Script(*(i if isinstance(i, Exception) else ChatTurn.model_validate(i) for i in items))


def _step(at: str = "deep.before_write", **kw: Any) -> HookStep:
    kind = "self_check" if at == "deep.after_write" else "delegate"
    output_to = "context.must_fix" if kind == "self_check" else "context.notes"
    d = {"at": at, "kind": kind, "tier": "fast", "tools": ["read_page"],
         "instruction": "先读相关页面", "output_to": output_to, "max_tool_calls": 3, **kw}
    return HookStep.model_validate(d)


def test_delegate_reads_then_answers():
    page = ReadPage()
    script = _turns(turn(call("read_page", page_id="p2")), turn(text="  要点：先讲印章。  "))
    out = run_hook(_step(), brief="页面：配对", tools=[page, ReadK()], converse=script, ctx=CTX)
    assert out.at == "deep.before_write"
    assert out.notes == ("要点：先讲印章。",) and out.ok is True
    assert len(out.calls) == 2
    assert page.seen == [({"page_id": "p2"}, CTX)]          # the main agent's context
    req = script.requests[0]
    assert req.purpose == "hook.deep.before_write" and req.prompt_version == PROMPT_VERSION
    assert req.system == HOOK_SYSTEM and req.params == {}
    [msg] = req.messages
    assert msg.role == "user" and msg.text == "先读相关页面\n\n页面：配对"
    assert [t.name for t in req.tools] == ["read_page"]      # only the step's tools


def test_sent_is_applied():
    script = _turns(turn(text="好"))
    run_hook(_step(), brief="b", tools=[ReadPage()], converse=script, ctx=CTX,
             sent=lambda r: r.model_copy(update={"system": r.system + "!"}))
    assert script.requests[0].system == HOOK_SYSTEM + "!"


def test_self_check_report():
    script = _turns(turn(call("report", ok=False, notes=["第二节缺例子", "术语前后不一"])))
    out = run_hook(_step("deep.after_write"), brief="草稿", tools=[ReadPage()], converse=script,
                   ctx=CTX)
    assert (out.ok, out.notes) == (False, ("第二节缺例子", "术语前后不一"))
    assert len(out.calls) == 1                                # report ends the step
    assert [t.name for t in script.requests[0].tools] == ["read_page", "report"]


def test_self_check_bad_report_goes_back_to_the_model():
    script = _turns(turn(call("report", ok="no", notes=[])),
                    turn(call("report", 2, ok=True, notes=[])))
    out = run_hook(_step("deep.after_write"), brief="草稿", tools=[ReadPage()], converse=script,
                   ctx=CTX)
    assert (out.ok, out.notes) == (True, ())
    tool_msg = script.requests[1].messages[-1]
    assert tool_msg.role == "tool" and tool_msg.is_error


def test_self_check_text_only_is_notes():
    script = _turns(turn(text="第三节太长"))
    out = run_hook(_step("deep.after_write"), brief="草稿", tools=[ReadPage()], converse=script,
                   ctx=CTX)
    assert (out.ok, out.notes) == (False, ("第三节太长",))


@pytest.mark.parametrize("at", ["deep.before_write", "deep.after_write"])
def test_empty_text_is_no_notes(at):
    out = run_hook(_step(at), brief="b", tools=[ReadPage()], converse=_turns(turn(text="  ")),
                   ctx=CTX)
    assert (out.ok, out.notes) == (True, ())


def test_missing_tool_is_invalid():
    with pytest.raises(HarnessInvalid, match="read_page"):
        run_hook(_step(), brief="b", tools=[ReadK()], converse=_turns(), ctx=CTX)


def test_clip():
    long = "字" * (HOOK_OUTPUT_MAX + 10)
    out = run_hook(_step(), brief="b", tools=[ReadPage()], converse=_turns(turn(text=long)),
                   ctx=CTX)
    [note] = out.notes
    assert note == "字" * HOOK_OUTPUT_MAX + "…（已截断）"
    exact = "字" * HOOK_OUTPUT_MAX
    out = run_hook(_step(), brief="b", tools=[ReadPage()], converse=_turns(turn(text=exact)),
                   ctx=CTX)
    assert out.notes == (exact,)


def test_traced_run():
    sink = Recorder()
    run_hook(_step(), brief="b", tools=[ReadPage()],
             converse=_turns(turn(call("read_page")), turn(text="好")), ctx=CTX,
             trace=sink, run_id="h1", user_id="u1", pack_version=7)
    kinds = [c[0] for c in sink.calls]
    assert kinds == ["start", "model", "tool", "model", "finish"]
    start = sink.calls[0]
    assert start[1:6] == ("h1", "u1", "hook.deep.before_write", "p1", 7)
    assert start[6]["instruction"] == "先读相关页面" and start[6]["brief"] == "b"
    assert sink.calls[-1] == ("finish", "h1", "answered", None)


def test_untraced_without_run_id():
    sink = Recorder()
    run_hook(_step(), brief="b", tools=[ReadPage()], converse=_turns(turn(text="好")), ctx=CTX,
             trace=sink)
    assert sink.calls == []


def test_provider_error_propagates_and_finishes_failed():
    sink = Recorder()
    with pytest.raises(ProviderUnavailableError):
        run_hook(_step(), brief="b", tools=[ReadPage()],
                 converse=_turns(ProviderUnavailableError("down")), ctx=CTX,
                 trace=sink, run_id="h1")
    assert sink.calls[-1] == ("finish", "h1", None, "ProviderUnavailableError")


def test_zero_cap_offers_only_report():
    script = _turns(turn(call("report", ok=True, notes=[])))
    out = run_hook(_step("deep.after_write", max_tool_calls=0), brief="b", tools=[ReadPage()],
                   converse=script, ctx=CTX)
    assert out.ok is True
    req = script.requests[0]
    assert [t.name for t in req.tools] == ["report"] and req.tool_choice == "auto"


def test_zero_cap_delegate_offers_nothing():
    script = _turns(turn(text="直接回答"))
    out = run_hook(_step(max_tool_calls=0), brief="b", tools=[ReadPage()], converse=script,
                   ctx=CTX)
    assert out.notes == ("直接回答",)
    req = script.requests[0]
    assert req.tools == () and req.tool_choice == "none"


def test_report_tool_is_replayed_not_recorded():
    # a pure function of its args (it only fills the output being built): it runs in the workflow
    # body on a durable replay, so the recorded report is rebuilt
    spec = report_tool([]).spec
    assert (spec.effect, spec.category) == ("read", "draft")


def test_self_check_reports_after_spending_the_cap_on_reads():
    page = ReadPage()
    script = _turns(turn(call("read_page", 1)), turn(call("read_page", 2)),
                    turn(call("report", ok=False, notes=["缺例子"])))
    out = run_hook(_step("deep.after_write", max_tool_calls=2), brief="草稿", tools=[page],
                   converse=script, ctx=CTX)
    assert (out.ok, out.notes) == (False, ("缺例子",))
    assert len(page.seen) == 2
    assert [r.tool_choice for r in script.requests] == ["auto", "auto", "auto"]


def test_self_check_notes_clipped_as_a_whole():
    notes = ["字" * 300] * 20                                   # 20 * 301 - 1 > HOOK_OUTPUT_MAX
    script = _turns(turn(call("report", ok=False, notes=notes)))
    out = run_hook(_step("deep.after_write"), brief="草稿", tools=[ReadPage()], converse=script,
                   ctx=CTX)
    joined = "\n".join(out.notes)
    assert joined.endswith(CLIP_MARK)
    assert len(joined) == HOOK_OUTPUT_MAX + len(CLIP_MARK)
    assert out.notes[:13] == tuple(notes[:13])
