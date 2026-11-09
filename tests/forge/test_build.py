import json
from types import SimpleNamespace

import pytest

from evowiki.forge.build import build
from evowiki.forge.models import ToolExample, ToolRequest
from evowiki.forge.sandbox import SandboxUnavailable
from tests.forge.fakes import InProcessSandbox

TOOL = "def run(args):\n    return {'ok': args['n'] % 2 == 0}\n"
TEST = "from tool import run\n\ndef test_even():\n    assert run({'n': 2}) == {'ok': True}\n"
SCHEMA = json.dumps({"type": "object", "description": "even?",
                     "properties": {"n": {"type": "integer"}}, "required": ["n"]})


def ws(**over):
    files = {"tool.py": TOOL, "test_tool.py": TEST, "schema.json": SCHEMA}
    files.update(over)
    return SimpleNamespace(files={k: v for k, v in files.items() if v is not None})


def req(*examples):
    ex = examples or (ToolExample(args={"n": 2}, expect={"ok": True}),)
    # an explicit (passing) held-out example keeps every given example visible
    hold = (ToolExample(args={"n": 8}, expect={"ok": True}),) if len(ex) > 1 else ()
    return ToolRequest(name="is_even", title="判断偶数", purpose="p", agent="deep",
                       examples=tuple(ex), holdout=hold)


def test_good_workspace_passes():
    r = build(ws(), req(ToolExample(args={"n": 3}, expect={"ok": False})), InProcessSandbox())
    assert r.ok and r.tests_passed == 1 and r.tests_failed == 0
    assert r.examples_passed == 1 and r.examples_failed == 0 and r.lint_ok and r.schema_ok
    assert r.reason == "测试 1 个、样例 1 个全部通过（没有留出样例）" and r.holdout_total == 0


def test_missing_test_file_fails_and_is_named():
    r = build(ws(**{"test_tool.py": None}), req(), InProcessSandbox())
    assert not r.ok and "test_tool.py" in r.reason


def test_zero_tests_fails():
    r = build(ws(**{"test_tool.py": "x = 1\n"}), req(), InProcessSandbox())
    assert not r.ok and "test_" in r.reason


@pytest.mark.parametrize("schema", [
    "{", json.dumps({"type": "array", "description": "d"}),
    json.dumps({"type": "object"}), json.dumps({"type": "object", "description": "d",
                                                "properties": {"n": {"type": "nonsense"}}}),
    None])
def test_bad_schema(schema):
    r = build(ws(**{"schema.json": schema}), req(), InProcessSandbox())
    assert not r.ok and not r.schema_ok


def test_example_args_failing_schema_counted_failed():
    r = build(ws(), req(ToolExample(args={"n": 2}, expect={"ok": True}),
                        ToolExample(args={"n": "x"}, expect={"ok": True})), InProcessSandbox())
    assert not r.ok and r.examples_passed == 1 and r.examples_failed == 1
    assert "第 2 个样例" in r.reason


def test_example_output_mismatch():
    r = build(ws(), req(ToolExample(args={"n": 2}, expect={"ok": False})), InProcessSandbox())
    assert not r.ok and r.examples_failed == 1


def test_syntax_error_is_lint_failure():
    r = build(ws(**{"tool.py": "def run(:\n"}), req(), InProcessSandbox())
    assert not r.ok and not r.lint_ok


def test_failing_tests_stop_the_cascade():
    bad = "from tool import run\n\ndef test_x():\n    assert False\n"
    r = build(ws(**{"test_tool.py": bad}), req(), InProcessSandbox())
    assert not r.ok and r.tests_failed == 1 and r.examples_passed == 0 and r.examples_failed == 0


def test_sandbox_unavailable_propagates():
    class Down(InProcessSandbox):
        def run_tests(self, files):
            raise SandboxUnavailable("no runtime")

    with pytest.raises(SandboxUnavailable):
        build(ws(), req(), Down())
