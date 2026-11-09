"""WASI sandbox (plan 2026-10-22 coding-agent RSI phase 1, Task 1).

Real-sandbox tests need the pinned CPython WASI runtime
(``uv run python scripts/fetch_wasi_python.py``) and skip cleanly without it.
"""
import pytest

from evowiki.forge.limits import L1, SandboxLimits
from evowiki.forge.models import HostExchange
from evowiki.forge.sandbox import HostSpec, SandboxUnavailable, runtime
from evowiki.forge.sandbox.wasi import WasiSandbox

needs_runtime = pytest.mark.skipif(not runtime.available(), reason=runtime.why_unavailable())


def test_missing_runtime_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOWIKI_WASI_PYTHON", str(tmp_path / "nope"))
    assert not runtime.available()
    assert "nope" in runtime.why_unavailable()
    with pytest.raises(SandboxUnavailable):
        WasiSandbox.from_env()


@pytest.fixture(scope="module")
def sb():
    return WasiSandbox.from_env()


TOOL = "def run(args):\n    return {'n': len(args['s'])}\n"


@needs_runtime
def test_runs_a_pure_tool(sb):
    out = sb.run_tool({"tool.py": TOOL}, {"s": "abc"})
    assert out.ok and out.output == {"n": 3} and out.stats.exit == "ok"
    assert sb.policy == "read-only"
    # interpreter start-up must leave most of the budget to the tool (stdlib .pyc precompiled)
    assert out.stats.fuel_used and 0 < out.stats.fuel_used < L1.fuel // 4


@needs_runtime
def test_unicode_round_trips(sb):
    out = sb.run_tool({"tool.py": "def run(a):\n    return {'r': a['s'][::-1]}\n"}, {"s": "配平"})
    assert out.ok and out.output == {"r": "平配"}


@needs_runtime
def test_no_network(sb):
    code = "import socket\ndef run(a):\n    socket.create_connection(('1.1.1.1', 80), 1)\n    return {}\n"
    out = sb.run_tool({"tool.py": code}, {})
    assert not out.ok and out.stats.exit == "error"


@needs_runtime
def test_no_host_files(sb):
    code = "def run(a):\n    return {'t': open('D:/Example/.env').read()}\n"
    assert not sb.run_tool({"tool.py": code}, {}).ok


@needs_runtime
def test_cannot_escape_preopens(sb):
    code = "def run(a):\n    return {'t': open('/work/../../../../etc/hosts').read()}\n"
    assert not sb.run_tool({"tool.py": code}, {}).ok


@needs_runtime
def test_stdlib_is_read_only(sb):
    code = "import os\ndef run(a):\n    open(os.__file__, 'a').write('x')\n    return {}\n"
    assert not sb.run_tool({"tool.py": code}, {}).ok


@needs_runtime
def test_workspace_is_read_only(sb):
    code = "def run(a):\n    open('/work/new.py', 'w').write('x')\n    return {}\n"
    assert not sb.run_tool({"tool.py": code}, {}).ok


@needs_runtime
def test_infinite_loop_is_stopped(sb):
    out = sb.run_tool({"tool.py": "def run(a):\n    while True: pass\n"}, {})
    assert out.stats.exit in ("fuel", "timeout") and not out.ok


@needs_runtime
def test_wall_clock_is_enforced():
    fast = WasiSandbox.from_env(SandboxLimits(wall_s=1.0, fuel=10**15,
                                              memory_bytes=L1.memory_bytes,
                                              output_bytes=L1.output_bytes))
    out = fast.run_tool({"tool.py": "def run(a):\n    while True: pass\n"}, {})
    assert out.stats.exit == "timeout" and not out.ok


@needs_runtime
def test_memory_cap(sb):
    out = sb.run_tool({"tool.py": "def run(a):\n    x = bytearray(1 << 30)\n    return {}\n"}, {})
    assert out.stats.exit in ("memory", "error") and not out.ok


@needs_runtime
def test_output_cap(sb):
    out = sb.run_tool({"tool.py": "def run(a):\n    return {'x': 'a' * (1 << 20)}\n"}, {})
    assert out.stats.exit == "output_cap" and not out.ok


@needs_runtime
def test_non_json_output_is_an_error(sb):
    out = sb.run_tool({"tool.py": "def run(a):\n    return {1, 2}\n"}, {})
    assert not out.ok and out.stats.exit == "error"


@needs_runtime
def test_tool_exception_reports_traceback_tail(sb):
    out = sb.run_tool({"tool.py": "def run(a):\n    raise ValueError('boom')\n"}, {})
    assert not out.ok and "ValueError" in out.error and "boom" in out.error


@needs_runtime
def test_disallowed_file_names_are_not_copied(sb):
    code = "import os\ndef run(a):\n    return {'f': sorted(os.listdir('/work'))}\n"
    out = sb.run_tool({"tool.py": code, "evil.sh": "rm -rf /", "Bad.py": "x"}, {})
    assert out.ok and out.output == {"f": ["tool.py"]}


@needs_runtime
def test_run_tests_reports_each_case(sb):
    files = {"tool.py": TOOL, "test_tool.py":
             "from tool import run\ndef test_a():\n    assert run({'s': 'ab'}) == {'n': 2}\n"
             "def test_b():\n    assert run({'s': ''}) == {'n': 1}\n"}
    rep = sb.run_tests(files)
    assert [(c.name, c.passed) for c in rep.cases] == [("test_a", True), ("test_b", False)]
    assert rep.stats.exit == "ok" and "AssertionError" in rep.cases[1].detail


@needs_runtime
def test_run_tests_import_failure_is_reported(sb):
    rep = sb.run_tests({"tool.py": TOOL, "test_tool.py": "import nope_missing\n"})
    assert rep.cases == () and "nope_missing" in rep.error and rep.stats.exit == "error"


# --- host read-only RPC in the real guest (plan P2 T5) --------------------------------------------

TWO_PAGES = ("import host\n"
             "def run(args):\n"
             "    return {'texts': [host.call('read_page', {'page_id': p}) for p in args['ids']]}\n")


def _port(log):
    def p(tool, args):
        log.append((tool, args))
        return HostExchange(tool=tool, args=args, ok=True, content=f"<{args['page_id']}>")
    return p


@needs_runtime
def test_host_calls_by_reexecution(sb):
    log = []
    out = sb.run_tool({"tool.py": TWO_PAGES}, {"ids": ["a", "页"]},
                      HostSpec(("read_page",), _port(log)))
    assert out.ok and out.output == {"texts": ["<a>", "<页>"]}, out.error
    assert log == [("read_page", {"page_id": "a"}), ("read_page", {"page_id": "页"})]
    assert len(out.host_calls) == 2


@needs_runtime
def test_host_calls_fail_closed_in_the_guest(sb):
    log = []
    assert not sb.run_tool({"tool.py": TWO_PAGES}, {"ids": ["a"]}, None).ok
    out = sb.run_tool({"tool.py": TWO_PAGES}, {"ids": ["a"]}, HostSpec(("read_k",), _port(log)))
    assert not out.ok and "read_page" in out.error and log == []


@needs_runtime
def test_workspace_host_py_cannot_shadow_the_runner_host(sb):
    fake = "def call(tool, args):\n    return 'forged'\n"
    log = []
    out = sb.run_tool({"tool.py": TWO_PAGES, "host.py": fake}, {"ids": ["a"]},
                      HostSpec(("read_page",), _port(log)))
    assert out.ok and out.output == {"texts": ["<a>"]} and len(log) == 1


@needs_runtime
def test_tests_use_installed_fake_host(sb):
    test = ("import host, tool\n"
            "def test_a():\n"
            "    host.install(lambda t, a: 'x' + a['page_id'])\n"
            "    assert tool.run({'ids': ['1']}) == {'texts': ['x1']}\n"
            "def test_no_real_host():\n"
            "    host.install(None)\n"
            "    try:\n        tool.run({'ids': ['1']})\n    except PermissionError:\n        return\n"
            "    raise AssertionError('reached a host')\n")
    rep = sb.run_tests({"tool.py": TWO_PAGES, "test_tool.py": test})
    assert [(c.name, c.passed) for c in rep.cases] == [("test_a", True),
                                                      ("test_no_real_host", True)], rep


@needs_runtime
def test_only_allowed_file_names_reach_work(sb):
    """A name the allowlist matches only with a trailing newline never reaches /work."""
    code = "import os\ndef run(a):\n    return {'f': sorted(os.listdir('/work'))}\n"
    out = sb.run_tool({"tool.py": code, "x.py\n": "pass\n"}, {})
    assert out.ok and out.output == {"f": ["tool.py"]}
