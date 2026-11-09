import json

import pytest

from evowiki.agent import durable
from evowiki.forge.coder import PROMPT_VERSION, START_FILES, run_coder
from evowiki.forge.limits import CODER_MAX_NUDGES, CODER_MAX_TOOL_CALLS
from evowiki.forge.models import ToolExample, ToolRequest
from evowiki.llm.chat import ChatTurn, ToolCall
from tests.forge.fakes import InProcessSandbox, ScriptedConverse

REQ = ToolRequest(name="double", title="翻倍", purpose="把整数 n 翻倍", agent="deep",
                  examples=(ToolExample(args={"n": 2}, expect={"n": 4}),))
TOOL = "def run(args):\n    return {'n': args['n'] * 2}\n"
TESTS = "import tool\n\ndef test_example():\n    assert tool.run({'n': 2}) == {'n': 4}\n"
SCHEMA = json.dumps({"type": "object", "description": "把 n 翻倍",
                     "properties": {"n": {"type": "integer"}}, "required": ["n"]})


def call(i, name, **args):
    return ToolCall(id=f"t{i}", name=name, args=args)


def turn(*calls, text=""):
    return ChatTurn(text=text, tool_calls=tuple(calls))


def happy_script():
    return ScriptedConverse(
        turn(call(1, "write_file", path="tool.py", content=TOOL)),
        turn(call(2, "write_file", path="test_tool.py", content=TESTS)),
        turn(call(3, "write_file", path="schema.json", content=SCHEMA)),
        turn(call(4, "run_tests")),
        turn(call(5, "submit", summary="写好了")),
    )


class RecordingTrace:
    def __init__(self):
        self.events = []

    def start(self, run_id, *, user_id, agent, subject, pack_version, input):
        self.events.append(("start", run_id, agent, user_id, subject))

    def model(self, run_id, seq, request, turn, call_id, error):
        self.events.append(("model", seq))

    def tool(self, run_id, seq, call_id, name, args, result):
        self.events.append(("tool", seq, name))

    def finish(self, run_id, *, stop, error):
        self.events.append(("finish", stop, error))


def go(conv, trace=None, run_id="", **kw):
    with durable.virtual_workflow("t"):
        return run_coder(REQ, conv, sandbox=InProcessSandbox(), trace=trace or RecordingTrace(),
                         run_id=run_id, user_id="u1", **kw)


def tool_msgs(conv, k):
    return [m for m in conv.requests[k].messages if m.role == "tool"]


def test_happy_path_finishes_with_the_files():
    conv = happy_script()
    ws, res = go(conv)
    assert res.stop == "finished"
    assert {"tool.py", "test_tool.py", "schema.json", "examples.json"} <= set(ws.files)
    assert ws.files["tool.py"] == TOOL
    assert "通过 1 / 失败 0" in tool_msgs(conv, 4)[-1].text


def test_first_message_and_system_prompt():
    conv = happy_script()
    go(conv)
    req = conv.requests[0]
    assert req.prompt_version == PROMPT_VERSION == "forge-tool@v2"
    assert req.purpose == "forge.tool" and req.subject == "double"
    first = req.messages[0].text
    assert "把整数 n 翻倍" in first
    assert json.dumps({"n": 2}, ensure_ascii=False) in first and '"expect"' in first
    for f in START_FILES:
        assert f in first
    for rule in ("run(args: dict) -> dict", "标准库", "网络", "先", "test_tool.py",
                 "schema.json", "description", "submit"):
        assert rule in req.system, rule
    assert {t.name for t in req.tools} >= {"read_file", "edit_file", "run_tests", "submit"}


def test_start_workspace_has_the_stub_and_examples():
    conv = ScriptedConverse(turn(call(1, "submit", summary="x")))
    ws, res = go(conv)
    assert res.stop == "finished"
    assert ws.files["tool.py"] == "def run(args: dict) -> dict:\n    raise NotImplementedError\n"
    assert json.loads(ws.files["examples.json"]) == [{"args": {"n": 2}, "expect": {"n": 4}}]


def test_seed_files_are_kept_and_examples_come_from_the_request():
    conv = ScriptedConverse(turn(call(1, "submit", summary="x")))
    ws, _ = go(conv, seed={"tool.py": TOOL, "examples.json": "[]"})
    assert ws.files["tool.py"] == TOOL
    assert json.loads(ws.files["examples.json"])[0]["expect"] == {"n": 4}


def test_examples_json_is_read_only():
    conv = ScriptedConverse(
        turn(call(1, "write_file", path="examples.json", content="[]"),
             call(2, "edit_file", path="examples.json", old_string="4", new_string="5")),
        turn(call(3, "submit", summary="x")))
    ws, res = go(conv)
    assert res.stop == "finished"
    msgs = tool_msgs(conv, 1)
    assert all(m.is_error and "examples.json" in m.text for m in msgs)
    assert json.loads(ws.files["examples.json"])[0]["expect"] == {"n": 4}


def test_no_tool_calls_stops_after_the_nudges():
    conv = ScriptedConverse(*[turn(text="嗯") for _ in range(CODER_MAX_NUDGES + 1)])
    _, res = go(conv)
    assert res.stop == "no_tool_calls"
    assert len(conv.requests) == CODER_MAX_NUDGES + 1
    assert conv.requests[1].messages[-1].text == "请继续用工具完成，做完调用 submit。"


def test_tool_cap_stops():
    conv = ScriptedConverse(
        turn(*[call(i, "list_files") for i in range(CODER_MAX_TOOL_CALLS)]))
    _, res = go(conv)
    assert res.stop == "tool_cap" and res.tool_calls == CODER_MAX_TOOL_CALLS


def test_trace_start_steps_finish():
    trace = RecordingTrace()
    go(happy_script(), trace=trace, run_id="r1")
    ev = trace.events
    assert ev[0] == ("start", "r1", "forge", "u1", "double")
    assert ev[-1] == ("finish", "finished", None)
    kinds = [e[0] for e in ev[1:-1]]
    assert kinds == ["model", "tool"] * 5
    assert [e[1] for e in ev[1:-1]] == list(range(1, 11))


def test_untraced_run_does_not_touch_the_trace():
    trace = RecordingTrace()
    go(happy_script(), trace=trace, run_id="")
    assert trace.events == []


def test_model_error_finishes_the_trace_failed_and_reraises():
    trace = RecordingTrace()
    conv = ScriptedConverse(RuntimeError("provider down"))
    with pytest.raises(RuntimeError, match="provider down"):
        go(conv, trace=trace, run_id="r2")
    assert trace.events[0][0] == "start"
    assert trace.events[-1] == ("finish", None, "RuntimeError")


def test_syntax_error_edit_goes_back_to_the_model():
    conv = ScriptedConverse(
        turn(call(1, "write_file", path="tool.py", content=TOOL)),
        turn(call(2, "edit_file", path="tool.py", old_string="* 2}", new_string="* 2")),
        turn(call(3, "submit", summary="x")))
    ws, res = go(conv)
    assert res.stop == "finished"
    err = tool_msgs(conv, 2)[-1]
    assert err.is_error and "语法" in err.text
    assert ws.files["tool.py"] == TOOL


def test_sent_hook_is_applied():
    seen = []

    def sent(r):
        seen.append(r.prompt_version)
        return r

    go(happy_script(), sent=sent)
    assert seen == ["forge-tool@v2"] * 5
