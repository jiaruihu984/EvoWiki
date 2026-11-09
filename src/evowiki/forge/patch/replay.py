"""The parent side of patched-code replay (plan P3 T10, spec §5.2.3). The gate's replay must run
the **patched** code while the judge and the gate stay in the unmodified parent. So the child
(`python -m evowiki.forge.replay_child`) runs in a Job Object from a fresh worktree of
`forge/<cid>` with the secret-free test env, and every model call it needs comes back here as one
JSON line, which the parent makes through its own accounted `ForgeConverse`: keys never cross,
`llm_call` is written by the parent's gateway, and cost is trustworthy (the P2 host-RPC idea).

Trust boundary: the RPC pipe is the child's only channel out, and nothing on it is trusted. Every
line is size-capped, parsed strictly (exact keys, sequential ids, `ChatRequest.model_validate`)
and refused by killing the job. The child may ask for model calls of its entry's allowed purposes
only, at most `max_calls` of them, within `wall_s`; each call is relabelled `purpose=forge.replay`
(the child's purpose and subject kept in `subject`), so it cannot spoof accounting. Its outputs are
data for the judge: never executed, never read as verdicts. Which cases to replay and how to judge
them belongs to harness 3b."""

import json
import re
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError

from evowiki.forge import limits, replay_child, replay_entries
from evowiki.forge.binding import accounted
from evowiki.forge.models import ForgeMisconfigured, SandboxStats
from evowiki.forge.patch.commands import child_env
from evowiki.forge.patch.git import Git, GitError
from evowiki.forge.sandbox import SandboxUnavailable
from evowiki.forge.sandbox.jobs import LineTooLong, PipeProc
from evowiki.llm.chat import ChatRequest

LINE_MAX_BYTES = 2 << 20               # one protocol line, either direction
REPLAY_PURPOSE = "forge.replay"
SUBJECT_MAX = 200
EXIT_WAIT_S = 10.0                     # after "done", the child must exit 0 within this
_CID = re.compile(r"^fc_[A-Za-z0-9]{1,40}$")
# The child's protocol code, compared with the parent's own copy before the child starts: both are
# denied paths, so a difference means the candidate branch is not what the gate expects.
CHILD_FILES = {
    "src/evowiki/forge/replay_child.py": Path(replay_child.__file__),
    "src/evowiki/forge/replay_entries.py": Path(replay_entries.__file__),
}

Converse = Callable[[ChatRequest], tuple[Any, Any]]


@dataclass(frozen=True)
class ReplayResult:
    ok: bool
    outputs: tuple[dict, ...]          # one per case, in order; data for the judge only
    calls: int                         # model calls made by the parent for the child
    cost_usd: float | None             # None if any call's cost is unknown
    stats: SandboxStats
    reason: str                        # "" when ok


class PipeRunner(Protocol):
    def start(self, argv: Sequence[str], *, cwd: Path, env: dict[str, str],
              wall_s: float) -> PipeProc: ...


def _refused(reason: str) -> ReplayResult:
    return ReplayResult(ok=False, outputs=(), calls=0, cost_usd=None,
                        stats=SandboxStats(wall_s=0.0, exit="error"), reason=reason)


def _parse(line: str) -> dict[str, Any] | None:
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def serve(proc: PipeProc, *, entry: str, cases: Sequence[dict], converse: Converse,
          candidate_id: str, max_calls: int = limits.REPLAY_MAX_CALLS,
          wall_s: float = limits.REPLAY_WALL_S,
          clock: Callable[[], float] = time.monotonic) -> ReplayResult:
    """Drive one started child to its "done" line, answering its model calls. Every protocol
    violation kills the job and returns ok=False with the reason. The caller still calls
    `proc.kill()` (idempotent) to free the job."""
    start = clock()
    deadline = start + wall_s
    allowed = replay_entries.PURPOSES.get(entry, frozenset())
    calls = 0
    cost: float | None = 0.0

    def result(ok: bool, reason: str, exit_: str, outputs: tuple[dict, ...] = ()) -> ReplayResult:
        if not ok:
            proc.kill()
        return ReplayResult(ok=ok, outputs=outputs, calls=calls, cost_usd=cost,
                            stats=SandboxStats(wall_s=round(clock() - start, 3), exit=exit_),
                            reason=reason)

    def send(msg: dict[str, Any]) -> ReplayResult | None:
        try:
            proc.write_line(json.dumps(msg, ensure_ascii=True, separators=(",", ":")),
                            timeout=max(0.0, deadline - clock()))
        except TimeoutError:
            return result(False, "回放超时", "timeout")
        except OSError:
            return result(False, "回放进程提前退出", "error")
        return None

    if (bad := send({"entry": entry, "cases": list(cases)})) is not None:
        return bad
    while True:
        left = deadline - clock()
        if left <= 0:
            return result(False, "回放超时", "timeout")
        try:
            line = proc.read_line(left, max_bytes=LINE_MAX_BYTES)
        except TimeoutError:
            return result(False, "回放超时", "timeout")
        except LineTooLong:
            return result(False, "回放进程的输出行过长", "output_cap")
        except OSError:
            return result(False, "回放进程提前退出", "error")
        if line is None:
            return result(False, "回放进程提前退出", "error")
        msg = _parse(line)
        op = msg.get("op") if msg is not None else None
        if op == "converse" and set(msg) == {"op", "id", "request"}:
            if type(msg["id"]) is not int or msg["id"] != calls + 1:
                return result(False, "回放进程发来无法识别的消息", "error")
            try:
                req = ChatRequest.model_validate(msg["request"])
            except ValidationError:
                return result(False, "回放进程发来无法识别的消息", "error")
            if req.purpose not in allowed:
                return result(False, "回放进程请求了不允许的用途", "error")
            if calls >= max_calls:
                return result(False, "调用次数超过上限", "error")
            calls += 1
            req = req.model_copy(update={
                "purpose": REPLAY_PURPOSE,
                "subject": f"{candidate_id}:{req.purpose}:{req.subject}"[:SUBJECT_MAX]})
            try:
                turn, call = converse(req)
            except Exception as e:  # noqa: BLE001 - the child sees only the error type
                answer: dict[str, Any] = {"id": calls, "error": type(e).__name__}
            else:
                cost = None if cost is None or call.cost is None else cost + call.cost
                answer = {"id": calls, "turn": turn.model_dump(mode="json"),
                          "call": call.model_dump(mode="json")}
            if (bad := send(answer)) is not None:
                return bad
        elif op == "done" and set(msg) == {"op", "outputs"}:
            outputs = msg["outputs"]
            if (not isinstance(outputs, list) or len(outputs) != len(cases)
                    or not all(isinstance(o, dict) for o in outputs)):
                return result(False, "回放结果格式不对", "error")
            code = proc.wait(min(EXIT_WAIT_S, max(0.0, deadline - clock())))
            if code != 0:
                return result(False, "回放进程没有正常退出", "error")
            return result(True, "", "ok", tuple(outputs))
        else:
            return result(False, "回放进程发来无法识别的消息", "error")


def _text(p: Path) -> bytes:
    return p.read_bytes().replace(b"\r\n", b"\n")      # core.autocrlf may differ per checkout


def _child_files_match(worktree: Path) -> bool:
    for rel, mine in CHILD_FILES.items():
        theirs = worktree / rel
        if not theirs.is_file() or _text(theirs) != _text(mine):
            return False
    return True


def run_replay(candidate_id: str, *, entry: str, cases: Sequence[dict], main_root: Path,
               converse: Converse, runner: PipeRunner, python: str, test_db_url: str,
               tmp: Path, max_calls: int = limits.REPLAY_MAX_CALLS,
               wall_s: float = limits.REPLAY_WALL_S) -> ReplayResult:
    """Replay `cases` through `entry` on the code of branch `forge/<candidate_id>`. Refuses (ok
    False, visible reason; never an unsandboxed fallback) an unknown entry, a bad candidate id, a
    missing branch, a child protocol file that differs from the parent's, or an unavailable
    sandbox. Raises ForgeMisconfigured for a converse without an Account. The worktree is always
    removed."""
    if not accounted(converse):
        raise ForgeMisconfigured("replay model calls must carry an Account (cost accounting)")
    if entry not in replay_entries.ENTRIES:
        return _refused(f"未知的回放入口：{entry!r}")
    if not isinstance(candidate_id, str) or not _CID.match(candidate_id):
        return _refused(f"不是合法的候选编号：{candidate_id!r}")
    if not all(isinstance(c, dict) for c in cases):
        return _refused("回放用例必须是对象")
    try:
        job_bytes = len(json.dumps({"entry": entry, "cases": list(cases)}, ensure_ascii=True))
    except (TypeError, ValueError):
        return _refused("回放用例不能转成 JSON")
    if job_bytes > LINE_MAX_BYTES:
        return _refused("回放用例太大")
    git = Git(Path(main_root))
    try:
        sha = git.rev_parse(f"forge/{candidate_id}")
    except GitError:
        return _refused(f"候选分支不存在：forge/{candidate_id}")
    tag = f"replay-{candidate_id}-{uuid4().hex[:8]}"
    wt, child_tmp = Path(tmp) / tag, Path(tmp) / f"{tag}-tmp"
    try:
        Path(tmp).mkdir(parents=True, exist_ok=True)
        git.run("worktree", "add", "-q", "--detach", "--", str(wt), sha)
    except (GitError, OSError) as e:
        return _refused(f"无法建立回放工作区：{type(e).__name__}")
    try:
        if not _child_files_match(wt):
            return _refused("候选分支里的回放程序和当前版本不一致")
        child_tmp.mkdir(parents=True, exist_ok=True)
        env = child_env(worktree=wt, tmp=child_tmp, test_db_url=test_db_url, python=python)
        try:
            proc = runner.start([python, "-m", "evowiki.forge.replay_child"], cwd=wt, env=env,
                                wall_s=wall_s)
        except SandboxUnavailable as e:
            return _refused(f"沙箱不可用：{e}")
        try:
            return serve(proc, entry=entry, cases=cases, converse=converse,
                         candidate_id=candidate_id, max_calls=max_calls, wall_s=wall_s)
        finally:
            proc.kill()
    finally:
        try:
            git.worktree_remove(wt)
        except GitError:
            shutil.rmtree(wt, ignore_errors=True)
            git.run("worktree", "prune", check=False)
        shutil.rmtree(child_tmp, ignore_errors=True)
