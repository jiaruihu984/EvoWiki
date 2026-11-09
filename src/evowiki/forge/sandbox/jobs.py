"""L2 sandbox backend: a Windows Job Object (pywin32 win32job / win32process; plan P3 T4).
Codex CLI 'workspace-write' semantics as far as Windows-without-admin allows: memory cap, process
cap, CPU-time cap, wall-clock kill, and the whole tree dies with the job handle
(KILL_ON_JOB_CLOSE). Network is off via pytest-socket (patch/commands.py); files outside the
worktree are protected by the command allowlist, the secret-free env and the cwd, not by the OS
(spec §8: this is not hard isolation, said plainly). OpenHands-style: a replaceable Runner.

Fail closed: no Windows or no pywin32 -> SandboxUnavailable; a job that cannot be created or set
up raises SandboxUnavailable too. There is never a fallback to an unsandboxed run."""

import contextlib
import os
import subprocess  # only subprocess.list2cmdline: the process is created by win32process
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from evowiki.forge import limits
from evowiki.forge.limits import SandboxLimits
from evowiki.forge.models import SandboxStats
from evowiki.forge.sandbox import SandboxUnavailable
from evowiki.paths import ROOT

POLL_S = 0.1                       # wall-clock / output-spill check interval
SPILL_FACTOR = 256                 # output beyond output_bytes * this (16 MiB) kills the job
CPU_FACTOR = 2                     # job CPU time may use at most wall_s * this
TMP_ROOT = ROOT / ".forge" / "tmp"
_ERROR_ACCESS_DENIED = 5
_MEMORY_NEAR = 0.98                # peak at or above this share of the limit = the limit was hit


@dataclass(frozen=True)
class ProcOutcome:
    exit_code: int | None              # None: killed (timeout / spill)
    output: str                        # stdout+stderr tail, <= limits.L2.output_bytes
    stats: SandboxStats                # wall_s, exit: ok|error|timeout|memory|output_cap
    peak_memory: int | None


class Runner(Protocol):
    policy: str                        # "workspace-write"

    def run(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
            wall_s: float = limits.L2_CMD_WALL_S) -> ProcOutcome: ...


def _tail(data: bytes, cap: int) -> str:
    text = data[-cap:].decode("utf-8", errors="replace")
    while len(text) > cap:                              # replacement chars can't grow it, but be exact
        text = text[1:]
    return text


# --- a pipe-connected child (plan P3 T10: the replay child talks JSON lines to the parent) -------

PIPE_POLL_S = 0.01
PIPE_CHUNK = 64 << 10
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232


class LineTooLong(ValueError):
    """The child sent more than `max_bytes` without a newline."""


class PipeProc(Protocol):
    """A child in a Job Object with stdin/stdout as pipes (stderr goes to a file)."""

    def write_line(self, line: str, timeout: float) -> None: ...      # TimeoutError, BrokenPipeError

    def read_line(self, timeout: float, max_bytes: int) -> str | None: ...  # None: EOF

    def wait(self, timeout: float) -> int | None: ...                 # exit code; None: running

    def kill(self) -> None: ...                                       # idempotent; frees everything


def _close(*handles) -> None:
    import win32api

    for h in handles:
        with contextlib.suppress(Exception):        # already closed
            win32api.CloseHandle(h)


class _JobPipeProc:
    def __init__(self, job, hp, w_in, r_out, err_file):
        self._job, self._hp, self._w, self._r, self._err = job, hp, w_in, r_out, err_file
        self._buf = b""
        self._eof = False
        self._closed = False

    def write_line(self, line: str, timeout: float) -> None:
        import threading

        import pywintypes
        import win32file

        if self._closed:
            raise BrokenPipeError("the child is gone")
        data = (line + "\n").encode("utf-8")
        failed: list[BaseException] = []

        def write() -> None:
            try:
                win32file.WriteFile(self._w, data)
            except pywintypes.error as e:
                failed.append(BrokenPipeError(f"write failed: {e.winerror}"))

        # a child that never reads must not hold the parent past its deadline: the write runs in a
        # thread; on timeout the job is killed, which closes the read end and ends the blocked write
        t = threading.Thread(target=write, daemon=True)
        t.start()
        t.join(max(0.0, timeout))
        if t.is_alive():
            self._terminate()
            t.join(5.0)
            raise TimeoutError("the child did not read its input in time")
        if failed:
            raise failed[0]

    def read_line(self, timeout: float, max_bytes: int) -> str | None:
        import pywintypes
        import win32file
        import win32pipe

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            i = self._buf.find(b"\n")
            if i >= 0:
                line, self._buf = self._buf[:i], self._buf[i + 1:]
                if len(line) > max_bytes:
                    raise LineTooLong(f"line over {max_bytes} bytes")
                return line.decode("utf-8", errors="replace").rstrip("\r")
            if len(self._buf) > max_bytes:
                raise LineTooLong(f"line over {max_bytes} bytes")
            if self._eof or self._closed:
                return None                        # a partial last line is not a message
            try:
                _, avail, _ = win32pipe.PeekNamedPipe(self._r, 0)
                if avail:
                    _, data = win32file.ReadFile(self._r, min(avail, PIPE_CHUNK))
                    self._buf += data
                    continue
            except pywintypes.error as e:
                if e.winerror not in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA):
                    raise OSError(f"pipe read failed: {e.winerror}") from None
                self._eof = True
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("no line from the child in time")
            time.sleep(min(PIPE_POLL_S, left))

    def wait(self, timeout: float) -> int | None:
        import win32event
        import win32process

        if self._closed:
            return None
        rc = win32event.WaitForSingleObject(self._hp, int(max(0.0, timeout) * 1000))
        if rc != win32event.WAIT_OBJECT_0:
            return None
        code = win32process.GetExitCodeProcess(self._hp)
        return code & 0xFFFFFFFF if code < 0 else code

    def stderr_tail(self, cap: int = 2000) -> str:
        if self._closed:
            return ""
        self._err.flush()
        self._err.seek(0, os.SEEK_END)
        size = self._err.tell()
        self._err.seek(max(0, size - cap))
        return _tail(self._err.read(), cap)

    def _terminate(self) -> None:
        import pywintypes
        import win32job

        with contextlib.suppress(pywintypes.error):  # the job already ended
            win32job.TerminateJobObject(self._job, 1)

    def kill(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._terminate()
        _close(self._w, self._r, self._hp, self._job)     # closing the job kills the tree too
        self._err.close()


class JobRunner:
    """Runs one argv (never through a shell) as the root of a fresh Job Object."""

    policy = "workspace-write"

    def __init__(self, lim: SandboxLimits = limits.L2, max_procs: int = limits.L2_MAX_PROCESSES,
                 tmp_root: Path = TMP_ROOT):
        if sys.platform != "win32":
            raise SandboxUnavailable("L2 sandbox needs Windows Job Objects")
        try:
            import win32api  # noqa: F401
            import win32con  # noqa: F401
            import win32event  # noqa: F401
            import win32job  # noqa: F401
            import win32process  # noqa: F401
        except ImportError as e:
            raise SandboxUnavailable(f"pywin32 missing: {e}") from None
        self.lim, self.max_procs, self.tmp_root = lim, max_procs, Path(tmp_root)

    def _job(self, wall_s: float):
        import win32job

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        basic = info["BasicLimitInformation"]
        basic["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY | win32job.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            | win32job.JOB_OBJECT_LIMIT_JOB_TIME)
        basic["ActiveProcessLimit"] = self.max_procs
        basic["PerJobUserTimeLimit"] = int(wall_s * CPU_FACTOR * 10_000_000)    # 100 ns units
        info["JobMemoryLimit"] = self.lim.memory_bytes
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        return job

    def _create(self, cmdline: str, env: Mapping[str, str], cwd: Path, si, flags: int):
        import pywintypes
        import win32process

        try:
            return win32process.CreateProcess(None, cmdline, None, None, True, flags, dict(env),
                                              str(cwd), si)
        except pywintypes.error as e:
            if e.winerror != _ERROR_ACCESS_DENIED or not flags & win32process.CREATE_BREAKAWAY_FROM_JOB:
                raise
        # the parent's own job forbids breakaway: nest instead (Windows 8+)
        return win32process.CreateProcess(None, cmdline, None, None, True,
                                          flags & ~win32process.CREATE_BREAKAWAY_FROM_JOB,
                                          dict(env), str(cwd), si)

    def run(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
            wall_s: float = limits.L2_CMD_WALL_S) -> ProcOutcome:
        import msvcrt

        import pywintypes
        import win32api
        import win32con
        import win32event
        import win32job
        import win32process

        argv = list(argv)
        if not argv:
            raise ValueError("empty argv")
        cap = self.lim.output_bytes
        spill = cap * SPILL_FACTOR
        try:
            self.tmp_root.mkdir(parents=True, exist_ok=True)
            job = self._job(wall_s)
        except (OSError, pywintypes.error) as e:
            raise SandboxUnavailable(f"cannot set up the Job Object: {e}") from None
        killed = timed_out = spilled = False
        code: int | None = None
        peak: int | None = None
        try:
            # a dedicated output file outside the worktree, so it never shows up in `git diff`
            with tempfile.TemporaryFile(dir=self.tmp_root) as out, open(os.devnull, "rb") as nul:
                h_out = msvcrt.get_osfhandle(out.fileno())
                h_in = msvcrt.get_osfhandle(nul.fileno())
                os.set_handle_inheritable(h_out, True)
                os.set_handle_inheritable(h_in, True)
                si = win32process.STARTUPINFO()
                si.dwFlags |= win32con.STARTF_USESTDHANDLES
                si.hStdOutput = si.hStdError = h_out
                si.hStdInput = h_in
                flags = (win32process.CREATE_SUSPENDED | win32process.CREATE_NO_WINDOW
                         | win32process.CREATE_UNICODE_ENVIRONMENT
                         | win32process.CREATE_BREAKAWAY_FROM_JOB)
                start = time.monotonic()
                try:
                    hp, ht, _pid, _tid = self._create(subprocess.list2cmdline(argv), env, cwd, si, flags)
                except pywintypes.error as e:
                    raise SandboxUnavailable(f"cannot start the sandboxed process: {e}") from None
                finally:
                    os.set_handle_inheritable(h_out, False)
                    os.set_handle_inheritable(h_in, False)
                try:
                    try:
                        win32job.AssignProcessToJobObject(job, hp)
                    except pywintypes.error as e:
                        win32process.TerminateProcess(hp, 1)       # never run it outside the job
                        raise SandboxUnavailable(f"cannot put the process in the Job Object: {e}") from None
                    win32process.ResumeThread(ht)
                    deadline = start + wall_s
                    while True:
                        left = deadline - time.monotonic()
                        rc = win32event.WaitForSingleObject(hp, int(max(0.0, min(POLL_S, left)) * 1000))
                        if rc == win32event.WAIT_OBJECT_0:
                            break
                        if time.monotonic() >= deadline:
                            timed_out = True
                        elif os.fstat(out.fileno()).st_size > spill:
                            spilled = True
                        if timed_out or spilled:
                            win32job.TerminateJobObject(job, 1)
                            win32event.WaitForSingleObject(hp, 5000)
                            killed = True
                            break
                    if not killed:
                        code = win32process.GetExitCodeProcess(hp)
                        if code < 0:                               # NTSTATUS codes come back signed
                            code &= 0xFFFFFFFF
                    acct = win32job.QueryInformationJobObject(
                        job, win32job.JobObjectExtendedLimitInformation)
                    peak = int(acct.get("PeakJobMemoryUsed") or 0) or None
                    basic = win32job.QueryInformationJobObject(
                        job, win32job.JobObjectBasicAccountingInformation)
                    cpu_s = (basic.get("TotalUserTime") or 0) / 10_000_000
                finally:
                    win32api.CloseHandle(ht)
                    win32api.CloseHandle(hp)
                    win32api.CloseHandle(job)                      # KILL_ON_JOB_CLOSE: the tree dies
                    job = None
                wall = time.monotonic() - start
                out.seek(0, os.SEEK_END)
                size = out.tell()
                out.seek(max(0, size - cap))
                data = out.read()
        finally:
            if job is not None:
                win32api.CloseHandle(job)
        if timed_out or (not killed and cpu_s >= wall_s * CPU_FACTOR * 0.99):
            exit_, code = "timeout", None if timed_out else code
        elif peak is not None and peak >= self.lim.memory_bytes * _MEMORY_NEAR and code != 0:
            exit_ = "memory"
        elif spilled or size > cap:
            exit_ = "output_cap"
        elif code == 0:
            exit_ = "ok"
        else:
            exit_ = "error"
        return ProcOutcome(exit_code=code, output=_tail(data, cap),
                           stats=SandboxStats(wall_s=round(wall, 3), exit=exit_), peak_memory=peak)

    def start(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
              wall_s: float = limits.REPLAY_WALL_S) -> PipeProc:
        """Like `run`, but the child's stdin/stdout are pipes kept open for a JSON-lines RPC
        (plan P3 T10). Same Job Object limits (memory, processes, CPU time from `wall_s`, tree
        kill on close). The caller owns the wall clock and must call `kill()` at the end."""
        import msvcrt

        import pywintypes
        import win32api
        import win32con
        import win32job
        import win32pipe
        import win32process

        argv = list(argv)
        if not argv:
            raise ValueError("empty argv")
        try:
            self.tmp_root.mkdir(parents=True, exist_ok=True)
            job = self._job(wall_s)
        except (OSError, pywintypes.error) as e:
            raise SandboxUnavailable(f"cannot set up the Job Object: {e}") from None
        owned: list = [job]                    # closed on any failure (job close = tree kill)
        err = None
        try:
            err = tempfile.TemporaryFile(dir=self.tmp_root)  # noqa: SIM115 - owned by the proc
            r_in, w_in = win32pipe.CreatePipe(None, 0)       # not inheritable; the child ends are
            r_out, w_out = win32pipe.CreatePipe(None, 0)     # made so only around CreateProcess
            owned += [r_in, w_in, r_out, w_out]
            h_err = msvcrt.get_osfhandle(err.fileno())
            flag = win32con.HANDLE_FLAG_INHERIT
            for h in (r_in, w_out):
                win32api.SetHandleInformation(h, flag, flag)
            os.set_handle_inheritable(h_err, True)
            si = win32process.STARTUPINFO()
            si.dwFlags |= win32con.STARTF_USESTDHANDLES
            si.hStdInput, si.hStdOutput, si.hStdError = r_in, w_out, h_err
            flags = (win32process.CREATE_SUSPENDED | win32process.CREATE_NO_WINDOW
                     | win32process.CREATE_UNICODE_ENVIRONMENT
                     | win32process.CREATE_BREAKAWAY_FROM_JOB)
            try:
                hp, ht, _pid, _tid = self._create(subprocess.list2cmdline(argv), env, cwd, si, flags)
            except pywintypes.error as e:
                raise SandboxUnavailable(f"cannot start the sandboxed process: {e}") from None
            finally:
                for h in (r_in, w_out):
                    win32api.SetHandleInformation(h, flag, 0)
                os.set_handle_inheritable(h_err, False)
            owned += [hp, ht]
            try:
                win32job.AssignProcessToJobObject(job, hp)
            except pywintypes.error as e:
                win32process.TerminateProcess(hp, 1)       # never run it outside the job
                raise SandboxUnavailable(f"cannot put the process in the Job Object: {e}") from None
            win32process.ResumeThread(ht)
            _close(ht, r_in, w_out)                        # the parent keeps only its own ends
            proc = _JobPipeProc(job, hp, w_in, r_out, err)
            owned, err = [], None
            return proc
        except (OSError, pywintypes.error) as e:
            raise SandboxUnavailable(f"cannot set up the child's pipes: {e}") from None
        finally:
            _close(*owned)
            if err is not None:
                err.close()
