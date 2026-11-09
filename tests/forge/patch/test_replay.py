"""Replay child + parent RPC (plan P3 T10). The child runs from the candidate's worktree with no keys;
every model call comes back to the parent as one JSON line, is validated, re-labelled
(purpose forge.replay) and made through the parent's accounted converse."""

import importlib.util
import io
import json
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from evowiki.forge import replay_child, replay_entries
from evowiki.forge.models import ForgeMisconfigured
from evowiki.forge.patch import replay
from evowiki.forge.patch.commands import _NO_READ_SITE
from evowiki.forge.sandbox.jobs import LineTooLong
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn
from tests.forge.fakes import ScriptedConverse, accounted_fake

DB = "postgresql://127.0.0.1:1/x"
SRC = Path(replay_child.__file__).resolve().parents[2]          # .../src
CHILD_FILES = ("evowiki/forge/replay_child.py", "evowiki/forge/replay_entries.py")


def text_turn(t: str) -> ChatTurn:
    return ChatTurn(text=t)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(git_repo: Path) -> Path:
    """The tiny repo with the parent's own child files committed (LF, whatever line endings this
    checkout has: the repo's own core.autocrlf=false keeps them as written) and a branch
    forge/fc_1."""
    for rel in CHILD_FILES:
        dst = git_repo / "src" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((SRC / rel).read_bytes().replace(b"\r\n", b"\n"))
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "child files")
    _git(git_repo, "branch", "forge/fc_1")
    return git_repo


# --- an in-memory pipe runner: replay_child.main in a thread -----------------------------------


class _Q(io.TextIOBase):
    """A line pipe: write() puts text, readline() blocks for the next line ("" = closed)."""

    def __init__(self) -> None:
        self.q: queue.Queue[str | None] = queue.Queue()
        self.buf = ""

    def write(self, s: str) -> int:
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.q.put(line + "\n")
        return len(s)

    def readline(self, *_a) -> str:
        item = self.q.get()
        if item is None:
            self.q.put(None)
            return ""
        return item

    def close_pipe(self) -> None:
        self.q.put(None)


@dataclass
class FakePipeProc:
    to_child: _Q
    from_child: _Q
    thread: threading.Thread
    result: list = field(default_factory=list)
    killed: bool = False
    lines_read: int = 0

    def write_line(self, line: str, timeout: float) -> None:
        if self.killed:
            raise BrokenPipeError("killed")
        self.to_child.write(line + "\n")

    def read_line(self, timeout: float, max_bytes: int) -> str | None:
        try:
            item = self.from_child.q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if item is None:
            return None
        self.lines_read += 1
        if len(item.encode()) > max_bytes:
            raise LineTooLong("long")
        return item.rstrip("\r\n")

    def wait(self, timeout: float) -> int | None:
        self.thread.join(timeout)
        return None if self.thread.is_alive() else (self.result or [None])[0]

    def kill(self) -> None:
        self.killed = True
        self.to_child.close_pipe()                 # the child's next read sees EOF and stops


@dataclass
class FakePipeRunner:
    child: object                                  # (inp, out) -> int
    starts: list[tuple[tuple[str, ...], Path, dict]] = field(default_factory=list)
    procs: list[FakePipeProc] = field(default_factory=list)

    def start(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
              wall_s: float) -> FakePipeProc:
        self.starts.append((tuple(argv), Path(cwd), dict(env)))
        assert (Path(cwd) / "src/evowiki/forge/replay_child.py").is_file()   # runs from the worktree
        to_child, from_child = _Q(), _Q()
        proc = FakePipeProc(to_child, from_child, None)  # type: ignore[arg-type]

        def body():
            try:
                proc.result.append(self.child(to_child, from_child))
            except Exception:                      # noqa: BLE001 - a dying child = EOF
                proc.result.append(1)
            finally:
                from_child.close_pipe()

        proc.thread = threading.Thread(target=body, daemon=True)
        proc.thread.start()
        self.procs.append(proc)
        return proc


def in_process_child(inp, out) -> int:
    return replay_child.main(inp, out)


def raw_child(*lines: str):
    """A hostile child: ignores the protocol and writes the given lines."""
    def child(inp, out):
        inp.readline()
        for line in lines:
            out.write(line + "\n")
        return 0
    return child


def _run(repo, tmp_path, conv, runner=None, entry="probe", cases=({"q": "hi"},), **kw):
    runner = runner or FakePipeRunner(child=in_process_child)
    r = replay.run_replay("fc_1", entry=entry, cases=list(cases), main_root=repo, converse=conv,
                          runner=runner, python=sys.executable, test_db_url=DB,
                          tmp=tmp_path / "rt", **kw)
    return r, runner


def _wts(repo: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                         check=True, capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if ln.startswith("worktree ")]


# --- tests -------------------------------------------------------------------------------------


def test_replay_probe_roundtrip(repo, tmp_path):
    inner = ScriptedConverse(text_turn("ok"))
    conv = accounted_fake(inner, cost=0.01)
    r, runner = _run(repo, tmp_path, conv)
    assert r.reason == "" and r.ok and r.outputs == ({"text": "ok"},)
    assert r.calls == 1 and r.cost_usd == 0.01 and r.stats.exit == "ok"
    argv, cwd, _ = runner.starts[0]
    assert argv == (sys.executable, "-m", "evowiki.forge.replay_child")
    assert cwd.name.startswith("replay-fc_1-")
    assert not cwd.exists() and len(_wts(repo)) == 1          # the worktree is always removed


def test_purpose_forced(repo, tmp_path):
    inner = ScriptedConverse(text_turn("ok"))
    _run(repo, tmp_path, accounted_fake(inner))
    req = inner.requests[0]
    assert req.purpose == "forge.replay"
    assert req.subject == f"fc_1:{replay_entries.PROBE_PURPOSE}:probe"


def test_unknown_entry_refused(repo, tmp_path):
    inner = ScriptedConverse()
    r, runner = _run(repo, tmp_path, accounted_fake(inner), entry="os:system")
    assert not r.ok and "回放入口" in r.reason and runner.starts == [] and r.calls == 0


def test_unaccounted_converse_refused(repo, tmp_path):
    with pytest.raises(ForgeMisconfigured):
        _run(repo, tmp_path, ScriptedConverse(text_turn("x")))


@pytest.mark.parametrize("cid", ["fc_1;x", "../fc_1", "-fc", "master"])
def test_bad_candidate_id_refused(repo, tmp_path, cid):
    r = replay.run_replay(cid, entry="probe", cases=[{"q": "hi"}], main_root=repo,
                          converse=accounted_fake(ScriptedConverse()),
                          runner=FakePipeRunner(child=in_process_child), python=sys.executable,
                          test_db_url=DB, tmp=tmp_path)
    assert not r.ok and "候选" in r.reason


def test_missing_branch_refused(repo, tmp_path):
    _git(repo, "branch", "-D", "forge/fc_1")
    r, runner = _run(repo, tmp_path, accounted_fake(ScriptedConverse()))
    assert not r.ok and runner.starts == []


def test_modified_child_files_refused(repo, tmp_path):
    _git(repo, "checkout", "-q", "forge/fc_1")
    p = repo / "src/evowiki/forge/replay_entries.py"
    p.write_text(p.read_text(encoding="utf-8") + "\nX = 1\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "tamper")
    _git(repo, "checkout", "-q", "master")
    r, runner = _run(repo, tmp_path, accounted_fake(ScriptedConverse()))
    assert not r.ok and "回放程序" in r.reason and runner.starts == []
    assert len(_wts(repo)) == 1


def _loop_entry(n: int):
    def entry(case, converse):
        for _ in range(n):
            converse(ChatRequest(purpose=replay_entries.PROBE_PURPOSE, prompt_version="v",
                                 subject="s", system="s",
                                 messages=(ChatMessage(role="user", text="q"),)))
        return {"n": n}
    return entry


@pytest.fixture
def loop_entry(monkeypatch):
    def install(n: int, purpose: str = replay_entries.PROBE_PURPOSE):
        mod = sys.modules[__name__]
        monkeypatch.setattr(mod, "LOOP", _loop_entry(n), raising=False)
        monkeypatch.setattr(replay_entries, "ENTRIES",
                            {**replay_entries.ENTRIES, "loop": f"{__name__}:LOOP"})
        monkeypatch.setattr(replay_entries, "PURPOSES",
                            {**replay_entries.PURPOSES, "loop": frozenset({purpose})})
    return install


def test_call_cap_kills(repo, tmp_path, loop_entry):
    loop_entry(41)
    inner = ScriptedConverse(*[text_turn("x")] * 41)
    r, runner = _run(repo, tmp_path, accounted_fake(inner, cost=0.001), entry="loop")
    assert not r.ok and "调用次数超过上限" in r.reason
    assert len(inner.requests) == 40 and r.calls == 40 and runner.procs[0].killed


def test_wall_clock_kills(repo, tmp_path, loop_entry):
    loop_entry(5)

    class Slow(ScriptedConverse):
        def __call__(self, req):
            time.sleep(0.3)
            return super().__call__(req)

    inner = Slow(*[text_turn("x")] * 5)
    r, runner = _run(repo, tmp_path, accounted_fake(inner), entry="loop", wall_s=0.5)
    assert not r.ok and r.stats.exit == "timeout" and runner.procs[0].killed
    assert len(inner.requests) < 5


def test_disallowed_purpose_kills(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(replay_entries, "PURPOSES", {"probe": frozenset({"deep.section"})})
    inner = ScriptedConverse(text_turn("x"))
    r, runner = _run(repo, tmp_path, accounted_fake(inner))
    assert not r.ok and "用途" in r.reason and inner.requests == [] and runner.procs[0].killed


def test_child_env_has_no_keys(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    monkeypatch.setenv("EVOWIKI_DATABASE_URL", "postgresql://dev")
    _, runner = _run(repo, tmp_path, accounted_fake(ScriptedConverse(text_turn("ok"))))
    _, cwd, env = runner.starts[0]
    bad = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|DATABASE_URL")
    assert [k for k in env if bad.search(k) and k != "EVOWIKI_TEST_DATABASE_URL"] == []
    assert "sk-secret" not in json.dumps(env) and env["EVOWIKI_TEST_DATABASE_URL"] == DB
    # the sealing sitecustomize first (it holds no evowiki), then the worktree's src
    assert env["PYTHONPATH"].split(";" if sys.platform == "win32" else ":")[:2] == [
        str(_NO_READ_SITE), str(cwd / "src")]


@pytest.mark.parametrize("lines,why", [
    (["not json"], "无法识别"),
    (["[1, 2]"], "无法识别"),
    (['{"op": "shell", "cmd": "dir"}'], "无法识别"),
    (['{"op": "converse", "id": 1, "request": {"purpose": "x"}}'], "无法识别"),
    (['{"op": "converse", "id": 7, "request": {}}'], "无法识别"),
    (['{"op": "converse", "id": 1, "request": {}, "role": "judge"}'], "无法识别"),
    (['{"op": "done", "outputs": [{"a": 1}, {"b": 2}]}'], "格式"),
    (['{"op": "done", "outputs": ["pass"]}'], "格式"),
    (['{"op": "done", "outputs": [{"a": 1}], "verdict": "pass"}'], "无法识别"),
    ([], "提前退出"),
])
def test_garbage_line_kills(repo, tmp_path, lines, why):
    inner = ScriptedConverse()
    r, runner = _run(repo, tmp_path, accounted_fake(inner), runner=FakePipeRunner(raw_child(*lines)))
    assert not r.ok and why in r.reason and inner.requests == [] and runner.procs[0].killed
    assert r.outputs == ()


def test_oversized_line_kills(repo, tmp_path):
    big = json.dumps({"op": "done", "outputs": [{"x": "a" * (replay.LINE_MAX_BYTES + 10)}]})
    r, _ = _run(repo, tmp_path, accounted_fake(ScriptedConverse()),
                runner=FakePipeRunner(raw_child(big)))
    assert not r.ok and r.stats.exit == "output_cap"


def test_converse_error_reaches_child_as_text_only(repo, tmp_path):
    inner = ScriptedConverse(RuntimeError("upstream said sk-secret"))
    r, _ = _run(repo, tmp_path, accounted_fake(inner))
    assert r.ok and r.calls == 1
    assert r.outputs[0]["error"].startswith("ReplayCallError") and "sk-secret" not in r.outputs[0]["error"]


def test_entry_exception_is_an_output_not_a_crash(repo, tmp_path):
    r, _ = _run(repo, tmp_path, accounted_fake(ScriptedConverse()), cases=({"q": 3},))
    assert r.ok and r.outputs[0]["error"].startswith("ValueError")


def test_unknown_cost_is_none(repo, tmp_path):
    r, _ = _run(repo, tmp_path, accounted_fake(ScriptedConverse(text_turn("ok"))))
    assert r.ok and r.cost_usd is None


def test_child_refuses_unknown_entry_itself():
    inp, out = io.StringIO(json.dumps({"entry": "nope", "cases": []}) + "\n"), io.StringIO()
    assert replay_child.main(inp, out) == 2 and out.getvalue() == ""


def test_entries_point_into_denied_packages_only():
    for target in replay_entries.ENTRIES.values():
        mod = target.split(":", 1)[0]
        assert mod.startswith(("evowiki.forge.", "evowiki.harness.")), target
    assert set(replay_entries.PURPOSES) == set(replay_entries.ENTRIES)


# --- one real process, in a real Job Object ----------------------------------------------------

real = pytest.mark.skipif(sys.platform != "win32" or importlib.util.find_spec("win32job") is None,
                          reason="needs Windows Job Objects (pywin32)")


@real
def test_real_job_child_probe(tmp_path):
    from evowiki.forge.patch.commands import child_env
    from evowiki.forge.sandbox.jobs import JobRunner

    root = SRC.parent
    (tmp_path / "t").mkdir()
    env = child_env(worktree=root, tmp=tmp_path / "t", test_db_url=DB, python=sys.executable)
    proc = JobRunner(tmp_root=tmp_path / "jobs").start(
        [sys.executable, "-m", "evowiki.forge.replay_child"], cwd=root, env=env, wall_s=120.0)
    inner = ScriptedConverse(text_turn("ok"))
    try:
        r = replay.serve(proc, entry="probe", cases=[{"q": "hi"}],
                         converse=accounted_fake(inner, cost=0.02), candidate_id="fc_1",
                         max_calls=3, wall_s=120.0)
    finally:
        proc.kill()
    assert r.reason == "" and r.ok and r.outputs == ({"text": "ok"},) and r.cost_usd == 0.02
    assert inner.requests[0].purpose == "forge.replay"


@real
def test_real_job_garbage_and_eof(tmp_path):
    from evowiki.forge.patch.commands import child_env
    from evowiki.forge.sandbox.jobs import JobRunner

    (tmp_path / "t").mkdir()
    env = child_env(worktree=tmp_path, tmp=tmp_path / "t", test_db_url=DB, python=sys.executable)
    proc = JobRunner(tmp_root=tmp_path / "jobs").start(
        [sys.executable, "-c", "import sys; sys.stdin.readline(); print('x' * 5000)"],
        cwd=tmp_path, env=env, wall_s=60.0)
    try:
        proc.write_line("{}", timeout=10)
        with pytest.raises(LineTooLong):
            proc.read_line(20.0, max_bytes=1000)
    finally:
        proc.kill()
    proc.kill()                                   # idempotent


@real
def test_real_job_child_that_never_reads_cannot_block_the_parent(tmp_path):
    from evowiki.forge.patch.commands import child_env
    from evowiki.forge.sandbox.jobs import JobRunner

    (tmp_path / "t").mkdir()
    env = child_env(worktree=tmp_path, tmp=tmp_path / "t", test_db_url=DB, python=sys.executable)
    proc = JobRunner(tmp_root=tmp_path / "jobs").start(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path, env=env, wall_s=120.0)
    t0 = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            proc.write_line("x" * (4 << 20), timeout=1.0)
        with pytest.raises((TimeoutError, OSError)):
            proc.write_line("again", timeout=1.0)        # the job was killed: nothing to talk to
        assert proc.read_line(1.0, max_bytes=100) is None
    finally:
        proc.kill()
    assert time.monotonic() - t0 < 15


def test_crlf_checkout_of_child_files_still_matches(repo, tmp_path):
    _git(repo, "checkout", "-q", "forge/fc_1")
    for rel in CHILD_FILES:
        p = repo / "src" / rel
        p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    _git(repo, "commit", "-qam", "crlf")
    _git(repo, "checkout", "-q", "master")
    r, _ = _run(repo, tmp_path, accounted_fake(ScriptedConverse(text_turn("ok"))))
    assert r.ok, r.reason
