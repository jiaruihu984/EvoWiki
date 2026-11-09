"""Runs INSIDE the WASI guest (CPython wasm32-wasip1). Stdlib only.

Reads one JSON object from stdin:
  {"mode": "run"|"test", "args": {...}, "host": [<recorded exchanges>], "host_tools": [...]}.
Writes exactly one JSON line to stdout:
  run:  {"ok": true, "output": {...}}      test: {"ok": true, "cases": [...]}
  a host call not recorded yet: {"ok": true, "need": {"tool": ..., "args": {...}}}
  any failure: {"ok": false, "error": "<traceback tail>"}
The host never imports this file; it is copied read-only to /runner with `guest_host.py`, which
the tool imports as `host` (test mode leaves its allowlist empty: tests must `host.install(...)`).
"""
import json
import sys
import traceback

TAIL = 1500


def _tail() -> str:
    return traceback.format_exc()[-TAIL:]


def _run(args):
    import tool
    out = tool.run(args)
    if not isinstance(out, dict):
        raise TypeError(f"run() must return a dict, got {type(out).__name__}")
    json.dumps(out, ensure_ascii=False)          # TypeError if not JSON-serialisable
    return {"ok": True, "output": out}


def _test():
    import test_tool
    cases = []
    for name in sorted(n for n in dir(test_tool) if n.startswith("test_")):
        fn = getattr(test_tool, name)
        if not callable(fn):
            continue
        try:
            fn()
            cases.append({"name": name, "passed": True, "detail": ""})
        except BaseException:                    # noqa: BLE001 - every failure is a case result
            cases.append({"name": name, "passed": False, "detail": _tail()})
    return {"ok": True, "cases": cases}


def main():
    real_stdout = sys.stdout
    sys.stdout = sys.stderr                      # the guest's own prints never mix into the result
    host = None
    run_mode = False
    try:
        req = json.loads(sys.stdin.read())
        sys.path.insert(0, "/runner")
        import guest_host  # the runner's copy, before /work can shadow it
        sys.path.remove("/runner")
        sys.modules["host"] = guest_host
        host = guest_host
        sys.path.insert(0, "/work")
        if req.get("mode") == "test":
            res = _test()
        else:
            run_mode = True
            host._recorded = list(req.get("host") or ())
            host._allowed = tuple(req.get("host_tools") or ())
            res = _run(req.get("args") or {})
    except BaseException:                        # noqa: BLE001 - report, never crash silently
        res = {"ok": False, "error": _tail()}
    if run_mode and host._need is not None:      # even if the tool swallowed the stop
        res = {"ok": True, "need": host._need}
    line = json.dumps(res, ensure_ascii=False)
    real_stdout.write(line + "\n")
    real_stdout.flush()


main()
