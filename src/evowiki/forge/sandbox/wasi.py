"""WasiSandbox: run forged tool code in CPython-on-WASI under wasmtime.

Policy ``read-only`` (Codex naming): the guest sees only the read-only stdlib at
``/lib``, the read-only workspace at ``/work`` and the runner at ``/runner``. No
other preopens, no inherited env, no sockets (WASI preview 1 has none). Limits:
fuel, an epoch deadline for wall time, a linear-memory cap, and an output cap.
Missing or unusable runtime -> ``SandboxUnavailable``; there is no fallback.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Any, Self

from evowiki.forge.limits import HOST_TOTAL_WALL_S, L1, WS_ALLOWED, SandboxLimits
from evowiki.forge.models import RunOutcome, SandboxStats, TestCase, TestReport
from evowiki.forge.sandbox import HostSpec, SandboxUnavailable, runtime
from evowiki.forge.sandbox.hostcalls import drive

try:
    import wasmtime
except ImportError:  # pragma: no cover - dependency is declared; fail closed anyway
    wasmtime = None  # type: ignore[assignment]

RUNNER = Path(__file__).with_name("guest_runner.py")
GUEST_HOST = Path(__file__).with_name("guest_host.py")
ARGV = ["python", "-I", "-B", "-X", "utf8", "/runner/guest_runner.py"]
TICK_S = 0.05
ERROR_MAX = 2000
STDERR_MAX = 16 << 10
_ALLOWED = tuple(re.compile(p) for p in WS_ALLOWED)


class _Ticker:
    """Advance one engine's epoch every ``TICK_S`` while any call is running.

    A shared ticker (not a per-call timer) so concurrent calls on the same
    engine never cut each other short; each store's deadline is in ticks.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._active = 0
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while True:
            with self._cv:
                while self._active == 0:
                    self._cv.wait()
            time.sleep(TICK_S)
            self._engine.increment_epoch()

    def __enter__(self) -> Self:
        with self._cv:
            self._active += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="forge-wasi-epoch",
                                                daemon=True)
                self._thread.start()
            self._cv.notify_all()
        return self

    def __exit__(self, *exc: object) -> None:
        with self._cv:
            self._active -= 1


class _Capped:
    """stdout/stderr sink keeping at most ``cap`` bytes; counts everything."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.buf = bytearray()
        self.total = 0

    def __call__(self, data: bytes) -> int:
        self.total += len(data)
        room = self.cap - len(self.buf)
        if room > 0:
            self.buf += data[:room]
        return len(data)

    def text(self) -> str:
        return bytes(self.buf).decode("utf-8", errors="replace")


_COMPILED: dict[str, tuple[Any, Any, _Ticker]] = {}
_COMPILED_LOCK = threading.Lock()


def _check_api() -> None:
    if wasmtime is None:
        raise SandboxUnavailable("wasmtime is not installed")
    params = inspect.signature(wasmtime.WasiConfig.preopen_dir).parameters
    if "dir_perms" not in params and "fs_mutable" not in params:
        raise SandboxUnavailable("this wasmtime cannot make read-only preopens")
    if not hasattr(wasmtime.WasiConfig, "stdout_custom"):
        raise SandboxUnavailable("this wasmtime cannot cap guest output (no stdout_custom)")


def _preopen_ro(cfg: Any, host: Path, guest: str) -> None:
    params = inspect.signature(cfg.preopen_dir).parameters
    if "dir_perms" in params:          # older wasmtime-py
        cfg.preopen_dir(str(host), guest, wasmtime.DirPerms.READ_ONLY, wasmtime.FilePerms.READ_ONLY)
    else:
        cfg.preopen_dir(str(host), guest, fs_mutable=False)


def _load(wasm: Path) -> tuple[Any, Any, _Ticker]:
    """Engine + module for ``wasm``, compiled once per process and cached on disk."""
    key = str(wasm.resolve())
    with _COMPILED_LOCK:
        if key in _COMPILED:
            return _COMPILED[key]
        cfg = wasmtime.Config()
        cfg.consume_fuel = True
        cfg.epoch_interruption = True
        engine = wasmtime.Engine(cfg)
        st = wasm.stat()
        tag = hashlib.sha256(
            f"{metadata.version('wasmtime')}|fuel|epoch|{st.st_size}|{st.st_mtime_ns}".encode()
        ).hexdigest()[:16]
        cwasm = wasm.with_name(f"python-{tag}.cwasm")
        module = None
        if cwasm.is_file():
            try:
                module = wasmtime.Module.deserialize_file(engine, str(cwasm))
            except Exception:  # noqa: BLE001 - stale/corrupt cache: recompile below
                module = None
        if module is None:
            try:
                module = wasmtime.Module.from_file(engine, str(wasm))
            except Exception as exc:
                raise SandboxUnavailable(f"cannot compile {wasm}: {exc}") from exc
            try:
                tmp = cwasm.with_suffix(".tmp")
                tmp.write_bytes(bytes(module.serialize()))
                tmp.replace(cwasm)
            except OSError:
                pass                      # read-only runtime dir: just recompile next process
        entry = (engine, module, _Ticker(engine))
        _COMPILED[key] = entry
        return entry


def _tail(s: str, n: int = ERROR_MAX) -> str:
    return s if len(s) <= n else s[-n:]


class WasiSandbox:
    policy = "read-only"

    def __init__(self, wasm: Path, lib: Path, limits: SandboxLimits = L1) -> None:
        self._wasm = wasm
        self._lib = lib
        self.limits = limits
        self._engine, self._module, self._ticker = _load(wasm)

    @classmethod
    def from_env(cls, limits: SandboxLimits = L1) -> WasiSandbox:
        if not runtime.available():
            raise SandboxUnavailable(runtime.why_unavailable())
        _check_api()
        return cls(runtime.wasm_path(), runtime.lib_path(), limits)

    # -- public protocol -------------------------------------------------
    def run_tool(self, files: Mapping[str, str], args: dict[str, Any],
                 host: HostSpec | None = None) -> RunOutcome:
        """One tool call. With host calls the pure guest is re-executed with the results recorded
        so far (`hostcalls.drive`); `HOST_TOTAL_WALL_S` bounds all executions together."""
        start = time.monotonic()
        tools = list(host.allowed) if host else []
        last: dict[str, Any] = {"stats": SandboxStats(wall_s=0.0, exit="ok"), "err": "",
                                "trap": False}

        def once(recorded: list[dict[str, Any]]) -> dict[str, Any]:
            left = HOST_TOTAL_WALL_S - (time.monotonic() - start)
            if left <= 0:
                last["stats"] = _stats(time.monotonic() - start, None, "timeout")
                last["err"] = "wall-clock limit hit: host calls took too long"
                last["trap"] = True
                return {"ok": False, "error": last["err"]}
            res, stats, err = self._call(
                files, {"mode": "run", "args": args, "host": recorded, "host_tools": tools},
                wall_s=min(self.limits.wall_s, left))
            last["stats"], last["err"], last["trap"] = stats, err, res is None
            return res if res is not None else {"ok": False, "error": err}

        res, calls = drive(once, host)
        stats = last["stats"].model_copy(update={"wall_s": round(time.monotonic() - start, 4)})
        if last["trap"]:
            return RunOutcome(ok=False, error=last["err"], stats=stats, host_calls=calls)
        if not res.get("ok"):
            return RunOutcome(ok=False, error=_tail(str(res.get("error", ""))),
                              stats=_with_exit(stats, _guest_exit(res)), host_calls=calls)
        out = res.get("output")
        if not isinstance(out, dict):
            return RunOutcome(ok=False, error="tool output is not a JSON object",
                              stats=_with_exit(stats, "error"), host_calls=calls)
        return RunOutcome(ok=True, output=out, stats=stats, host_calls=calls)

    def run_tests(self, files: Mapping[str, str]) -> TestReport:
        res, stats, err = self._call(files, {"mode": "test"})
        if res is None:
            return TestReport(cases=(), stats=stats, error=err)
        if not res.get("ok"):
            return TestReport(cases=(), error=_tail(str(res.get("error", ""))),
                              stats=_with_exit(stats, _guest_exit(res)))
        cases = tuple(TestCase(name=str(c.get("name", "")), passed=bool(c.get("passed")),
                               detail=_tail(str(c.get("detail", ""))))
                      for c in res.get("cases") or ())
        return TestReport(cases=cases, stats=stats)

    # -- one guest process -------------------------------------------------
    def _call(self, files: Mapping[str, str], request: dict[str, Any], *,
              wall_s: float | None = None) -> tuple[dict[str, Any] | None, SandboxStats, str]:
        lim = self.limits
        wall_limit = lim.wall_s if wall_s is None else wall_s
        work = Path(tempfile.mkdtemp(prefix="forge-work-"))
        runner = Path(tempfile.mkdtemp(prefix="forge-runner-"))
        io_dir = Path(tempfile.mkdtemp(prefix="forge-io-"))
        start = time.monotonic()
        store = None
        try:
            for name, text in files.items():
                if any(p.fullmatch(name) for p in _ALLOWED):   # match + `$` lets "x.py\n" in
                    (work / name).write_text(text, encoding="utf-8")
            shutil.copyfile(RUNNER, runner / "guest_runner.py")
            shutil.copyfile(GUEST_HOST, runner / "guest_host.py")
            stdin = io_dir / "stdin.json"
            stdin.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")

            out = _Capped(lim.output_bytes + 1)
            errs = _Capped(STDERR_MAX)
            cfg = wasmtime.WasiConfig()
            cfg.argv = ARGV
            cfg.env = []
            cfg.stdin_file = str(stdin)
            cfg.stdout_custom = out
            cfg.stderr_custom = errs
            _preopen_ro(cfg, self._lib, "/lib")
            _preopen_ro(cfg, work, "/work")
            _preopen_ro(cfg, runner, "/runner")

            store = wasmtime.Store(self._engine)
            store.set_wasi(cfg)
            store.set_fuel(lim.fuel)
            store.set_limits(memory_size=lim.memory_bytes)
            store.set_epoch_deadline(max(1, math.ceil(wall_limit / TICK_S)))
            linker = wasmtime.Linker(self._engine)
            linker.define_wasi()

            exit_kind, err = "ok", ""
            with self._ticker:
                try:
                    inst = linker.instantiate(store, self._module)
                    inst.exports(store)["_start"](store)
                except wasmtime.ExitTrap as exc:
                    if exc.code != 0:
                        exit_kind, err = "error", f"guest exited with code {exc.code}"
                except wasmtime.Trap as exc:
                    exit_kind, err = _trap_kind(exc), exc.message
                except wasmtime.WasmtimeError as exc:
                    exit_kind, err = "error", str(exc)
            wall = time.monotonic() - start
            fuel_used = _fuel_used(store, lim.fuel)
            stderr = errs.text()

            if out.total > lim.output_bytes:
                return None, _stats(wall, fuel_used, "output_cap"), (
                    f"output over {lim.output_bytes} bytes")
            if exit_kind != "ok":
                if exit_kind == "error" and "MemoryError" in stderr:
                    exit_kind = "memory"
                return None, _stats(wall, fuel_used, exit_kind), _tail(
                    f"{_REASON.get(exit_kind, '')}{err}\n{stderr}".strip())
            res = _last_json_line(out.text())
            if res is None:
                return None, _stats(wall, fuel_used, "error"), _tail(
                    ("guest wrote no result\n" + stderr).strip())
            return res, _stats(wall, fuel_used, "ok"), ""
        finally:
            if store is not None:
                store.close()
            for d in (work, runner, io_dir):
                shutil.rmtree(d, ignore_errors=True)


_REASON = {"fuel": "fuel exhausted: ", "timeout": "wall-clock limit hit: ",
           "memory": "memory limit hit: "}


def _trap_kind(exc: Any) -> str:
    code = exc.trap_code
    if code == wasmtime.TrapCode.OUT_OF_FUEL:
        return "fuel"
    if code == wasmtime.TrapCode.INTERRUPT:
        return "timeout"
    msg = (exc.message or "").lower()
    if "fuel" in msg:
        return "fuel"
    if "interrupt" in msg or "epoch" in msg:
        return "timeout"
    if "memory" in msg and ("grow" in msg or "limit" in msg):
        return "memory"
    return "error"


def _fuel_used(store: Any, budget: int) -> int | None:
    try:
        return budget - store.get_fuel()
    except Exception:  # noqa: BLE001 - stats only
        return None


def _stats(wall: float, fuel_used: int | None, exit_kind: str) -> SandboxStats:
    return SandboxStats(wall_s=round(wall, 4), fuel_used=fuel_used, exit=exit_kind)  # type: ignore[arg-type]


def _with_exit(stats: SandboxStats, exit_kind: str) -> SandboxStats:
    return stats.model_copy(update={"exit": exit_kind})


def _guest_exit(res: dict[str, Any]) -> str:
    return "memory" if "MemoryError" in str(res.get("error", "")) else "error"


def _last_json_line(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        if line.strip():
            try:
                obj = json.loads(line)
            except ValueError:
                return None
            return obj if isinstance(obj, dict) else None
    return None
