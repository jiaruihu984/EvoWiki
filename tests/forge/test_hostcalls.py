"""Host read-only RPC by deterministic re-execution (plan P2 T5): fakes only, no runtime needed."""
import json
import sys

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.forge.build import build
from evowiki.forge.coder import PROMPT_VERSION, examples_json, first_message, start_workspace
from evowiki.forge.limits import HOST_MAX_CALLS, HOST_RESULT_CHARS
from evowiki.forge.models import HostExchange, ToolExample, ToolRequest
from evowiki.forge.registry import ForgedTool, SandboxedTool
from evowiki.forge.sandbox import HostSpec
from evowiki.forge.sandbox.hostcalls import CassetteHost, CassetteMismatch, drive
from tests.forge.fakes import InProcessSandbox

TWO_PAGES = ("import host\n"
             "def run(args):\n"
             "    return {'texts': [host.call('read_page', {'page_id': p}) for p in args['ids']]}\n")


def port(log):
    def p(tool, args):
        log.append((tool, args))
        return HostExchange(tool=tool, args=args, ok=True, content=f"<{args['page_id']}>")
    return p


def test_reexecution_returns_all_results_in_order():
    log = []
    out = InProcessSandbox().run_tool({"tool.py": TWO_PAGES}, {"ids": ["a", "b"]},
                                      HostSpec(("read_page",), port(log)))
    assert out.ok and out.output == {"texts": ["<a>", "<b>"]}
    assert log == [("read_page", {"page_id": "a"}), ("read_page", {"page_id": "b"})]   # each once
    assert [h.args["page_id"] for h in out.host_calls] == ["a", "b"]


def test_disallowed_tool_fails_closed():
    out = InProcessSandbox().run_tool({"tool.py": TWO_PAGES}, {"ids": ["a"]},
                                      HostSpec(("read_k",), port([])))
    assert not out.ok and "read_page" in out.error


def test_no_host_port_fails_closed():
    out = InProcessSandbox().run_tool({"tool.py": TWO_PAGES}, {"ids": ["a"]}, None)
    assert not out.ok


def test_call_cap():
    out = InProcessSandbox().run_tool({"tool.py": TWO_PAGES},
                                      {"ids": [str(i) for i in range(HOST_MAX_CALLS + 1)]},
                                      HostSpec(("read_page",), port([])))
    assert not out.ok and "次数" in out.error


def test_swallowing_need_does_not_help():
    sneaky = ("import host\n"
              "def run(args):\n"
              "    try:\n        host.call('read_page', {'page_id': 'a'})\n"
              "    except BaseException:\n        pass\n"
              "    return {'faked': True}\n")
    log = []
    out = InProcessSandbox().run_tool({"tool.py": sneaky}, {}, HostSpec(("read_page",), port(log)))
    assert out.ok and out.output == {"faked": True} and log == [("read_page", {"page_id": "a"})]
    # the need was served; the second execution got the result and the tool chose to ignore it


def test_cassette_is_strict():
    cas = CassetteHost((HostExchange(tool="read_page", args={"page_id": "a"}, ok=True, content="A"),))
    out = InProcessSandbox().run_tool({"tool.py": TWO_PAGES}, {"ids": ["b"]},
                                      HostSpec(("read_page",), cas))
    assert not out.ok


def test_host_tools_must_be_allowed_for_agent():
    with pytest.raises(ValueError):
        ToolRequest(name="x_y", title="读取", purpose="p", agent="deep", host_tools=("web_search",),
                    examples=(ToolExample(args={}, expect={}),))


# --- the host side never trusts the guest's `need` -------------------------------------------------

def _guest(*results):
    """A scripted guest: returns the given result dicts in turn, recording what it was given."""
    seen = []

    def once(recorded):
        seen.append(list(recorded))
        return results[min(len(seen) - 1, len(results) - 1)]
    return once, seen


@pytest.mark.parametrize("need", [
    "read_page",                                         # not an object
    {"tool": 3, "args": {}},                             # tool not a string
    {"tool": "read_page", "args": ["x"]},                # args not an object
    {"tool": "read_page", "args": {"p": "x" * 5000}},    # args too large
    {"tool": "read_page"},                               # args missing
])
def test_malformed_need_fails_closed_without_calling_the_port(need):
    log = []
    once, _ = _guest({"ok": True, "need": need})
    res, calls = drive(once, HostSpec(("read_page",), port(log)))
    assert res["ok"] is False and log == [] and calls == ()


def test_need_for_tool_outside_allowlist_never_reaches_the_port():
    log = []
    once, _ = _guest({"ok": True, "need": {"tool": "write_page", "args": {"page_id": "a"}}})
    res, calls = drive(once, HostSpec(("read_page",), port(log)))
    assert res["ok"] is False and "write_page" in res["error"] and log == [] and calls == ()


def test_allowlist_outside_the_read_tools_is_refused():
    log = []
    once, _ = _guest({"ok": True, "need": {"tool": "write_page", "args": {"page_id": "a"}}})
    res, _ = drive(once, HostSpec(("write_page",), port(log)))
    assert res["ok"] is False and log == []


def test_need_without_host_fails_closed():
    once, _ = _guest({"ok": True, "need": {"tool": "read_page", "args": {"page_id": "a"}}})
    res, calls = drive(once, None)
    assert res["ok"] is False and calls == ()


def test_results_are_clipped_before_the_guest_sees_them():
    def big(tool, args):
        return HostExchange(tool=tool, args=args, ok=True, content="x" * (HOST_RESULT_CHARS * 3))
    once, seen = _guest({"ok": True, "need": {"tool": "read_page", "args": {"page_id": "a"}}},
                        {"ok": True, "output": {}})
    res, calls = drive(once, HostSpec(("read_page",), big))
    assert res == {"ok": True, "output": {}}
    assert len(seen[1][0]["content"]) == HOST_RESULT_CHARS == len(calls[0].content)


def test_port_answer_for_a_different_call_fails_closed():
    def liar(tool, args):
        return HostExchange(tool="read_k", args={}, ok=True, content="secret")
    once, _ = _guest({"ok": True, "need": {"tool": "read_page", "args": {"page_id": "a"}}},
                     {"ok": True, "output": {}})
    res, _ = drive(once, HostSpec(("read_page", "read_k"), liar))
    assert res["ok"] is False


def test_port_exception_fails_closed():
    def boom(tool, args):
        raise RuntimeError("db down")
    once, _ = _guest({"ok": True, "need": {"tool": "read_page", "args": {"page_id": "a"}}})
    res, calls = drive(once, HostSpec(("read_page",), boom))
    assert res["ok"] is False and calls == ()


def test_host_error_reaches_the_guest_as_host_error():
    code = ("import host\n"
            "def run(args):\n"
            "    try:\n        return {'t': host.call('read_page', {'page_id': 'a'})}\n"
            "    except host.HostError as e:\n        return {'err': str(e)}\n")

    def missing(tool, args):
        return HostExchange(tool=tool, args=args, ok=False, content="没有这一页")
    out = InProcessSandbox().run_tool({"tool.py": code}, {}, HostSpec(("read_page",), missing))
    assert out.ok and out.output == {"err": "没有这一页"}


def test_nondeterministic_guest_is_caught():
    flaky = ("import host, sys\n"
             "def run(args):\n"
             "    sys._forge_flaky_n = getattr(sys, '_forge_flaky_n', 0) + 1\n"
             "    return {'t': host.call('read_page', {'page_id': str(sys._forge_flaky_n)})}\n")
    try:
        out = InProcessSandbox().run_tool({"tool.py": flaky}, {},
                                          HostSpec(("read_page",), port([])))
    finally:
        sys.__dict__.pop("_forge_flaky_n", None)
    assert not out.ok and "顺序" in out.error


def test_cassette_unused_and_mismatch():
    a = HostExchange(tool="read_page", args={"page_id": "a"}, ok=True, content="A")
    cas = CassetteHost((a, a))
    assert cas("read_page", {"page_id": "a"}) == a
    assert cas.unused() == (a,)
    with pytest.raises(CassetteMismatch):
        cas("read_page", {"page_id": "b"})
    with pytest.raises(CassetteMismatch):
        CassetteHost(())("read_page", {"page_id": "a"})


def test_example_host_calls_must_use_declared_tools():
    ex = ToolExample(args={}, expect={}, host=(
        HostExchange(tool="read_k", args={}, ok=True, content="k"),))
    with pytest.raises(ValueError):
        ToolRequest(name="x_y", title="读取", purpose="p", agent="deep", host_tools=("read_page",),
                    examples=(ex,))


# --- build replays each example's cassette strictly ---------------------------------------------

SCHEMA = json.dumps({"type": "object", "description": "读两页",
                     "properties": {"ids": {"type": "array", "items": {"type": "string"}}}})
TEST = ("import host, tool\n"
        "def test_a():\n"
        "    host.install(lambda t, a: '<' + a['page_id'] + '>')\n"
        "    assert tool.run({'ids': ['q']}) == {'texts': ['<q>']}\n")


def _ex(ids, served=None):
    served = ids if served is None else served
    return ToolExample(args={"ids": ids}, expect={"texts": [f"<{i}>" for i in ids]},
                       host=tuple(HostExchange(tool="read_page", args={"page_id": i}, ok=True,
                                               content=f"<{i}>") for i in served))


def _req(*examples, host_tools=("read_page",)):
    return ToolRequest(name="two_pages", title="读两页", purpose="p", agent="deep",
                       host_tools=host_tools, examples=examples, holdout=examples[:1])


def _ws():
    return type("W", (), {"files": {"tool.py": TWO_PAGES, "test_tool.py": TEST,
                                    "schema.json": SCHEMA}})()


def test_build_replays_cassettes():
    rep = build(_ws(), _req(_ex(["a"]), _ex(["b", "c"]), _ex([])), InProcessSandbox())
    assert rep.ok and rep.examples_passed == 3 and rep.holdout_passed == 1, rep.reason


def test_build_fails_on_unused_cassette_entries():
    rep = build(_ws(), _req(_ex(["a"], served=["a", "z"])), InProcessSandbox())
    assert not rep.ok and "读取数据" in rep.reason


def test_build_fails_on_diverging_cassette():
    rep = build(_ws(), _req(_ex(["a"], served=["b"])), InProcessSandbox())
    assert not rep.ok and "读取数据" in rep.reason


def test_empty_cassette_forbids_host_calls():
    ex = ToolExample(args={"ids": ["a"]}, expect={"texts": ["<a>"]})
    rep = build(_ws(), _req(ex), InProcessSandbox())
    assert not rep.ok


def test_holdout_cassette_failure_names_no_example():
    good = _ex(["a"])
    bad = _ex(["b"], served=["b", "z"])
    req = ToolRequest(name="two_pages", title="读两页", purpose="p", agent="deep",
                      host_tools=("read_page",), examples=(good,), holdout=(bad,))
    rep = build(_ws(), req, InProcessSandbox())
    assert not rep.ok and rep.holdout_failed == 1 and "留出" in rep.reason and "z" not in rep.reason


# --- registry and coder ---------------------------------------------------------------------------

def _forged(host_tools):
    return ForgedTool(name="two_pages", candidate_id="c1", files={"tool.py": TWO_PAGES},
                      schema=json.loads(SCHEMA), host_tools=host_tools)


def test_sandboxed_tool_passes_the_port_only_for_host_tools():
    log = []
    ctx = ToolContext(user_id="u1", session_id="s")
    ok = SandboxedTool(_forged(("read_page",)), InProcessSandbox(), host=port(log)).run(
        {"ids": ["a"]}, ctx)
    assert ok.error is None and "<a>" in ok.content and log == [("read_page", {"page_id": "a"})]
    no_port = SandboxedTool(_forged(("read_page",)), InProcessSandbox()).run({"ids": ["a"]}, ctx)
    assert no_port.error
    undeclared = SandboxedTool(_forged(()), InProcessSandbox(), host=port(log)).run(
        {"ids": ["b"]}, ctx)
    assert undeclared.error and len(log) == 1


def test_coder_prompt_explains_host_calls_and_shows_cassettes():
    req = _req(_ex(["a"]))
    ws = start_workspace(req)
    text = first_message(req, ws, host_docs={"read_page": "读一页的正文"}).text
    assert PROMPT_VERSION == "forge-tool@v2"
    assert "host.call" in text and "read_page" in text and "读一页的正文" in text
    assert "host.install" in text and "HostError" in text
    assert '"page_id": "a"' in examples_json(req)


def test_coder_prompt_without_host_tools_has_no_host_section():
    req = ToolRequest(name="double", title="翻倍", purpose="p", agent="deep",
                      examples=(ToolExample(args={"n": 1}, expect={"n": 2}),))
    text = first_message(req, start_workspace(req)).text
    assert "host.call" not in text and '"host"' not in examples_json(req)
