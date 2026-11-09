"""`propose("patch", ...)` end to end with fakes (plan P3 T9): scripted coder, FakeRunner, a temp
repo with an `rsi/live` branch. No model, no pytest child, no sandbox."""
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evowiki.agent.tool_helpers import fail
from evowiki.content.models import Lang
from evowiki.forge import limits, switch
from evowiki.forge.api import PatchDeps, propose
from evowiki.forge.models import (
    Evidence,
    ForgeDisabled,
    ForgeMisconfigured,
    PatchBuildReport,
    PatchRequest,
)
from evowiki.forge.patch.coder import PROMPT_VERSION, PURPOSE
from evowiki.forge.patch.git import Git
from evowiki.forge.patch.tools import SubmitTool
from evowiki.harness.pack import HarnessPack
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.store import forge as store
from evowiki.store import forge_patch as patch_store
from evowiki.store.users import UserRow, create_user
from tests.forge.fakes import ScriptedConverse, accounted_fake
from tests.forge.patch.fakes import FakeRunner, outcome

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
REQ = PatchRequest(goal="add 结果不对", title="修好加法", failing_case="add(1,2) 返回 -1")
EVI = Evidence(citations=("signal:1",))
DB = "postgresql://forge@127.0.0.1:9811/forge_test"
NEWF = "tests/forge_add/test_add.py"
NEW = f"{NEWF}::test_add"
OLD = "tests/test_old.py::test_old"
TEST = "from evowiki.deep.mathx import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def call(i, name, **args):
    return ToolCall(id=f"t{i}", name=name, args=args)


def turn(*calls):
    return ChatTurn(text="", tool_calls=tuple(calls))


def fixing_script():
    return ScriptedConverse(
        turn(call(1, "write_file", path=NEWF, content=TEST)),
        turn(call(2, "run_cmd", argv=["pytest", NEWF])),
        turn(call(3, "edit_file", path="src/evowiki/deep/mathx.py", old_string="return a - b",
                  new_string="return a + b")),
        turn(call(4, "submit", summary="修好了 add，新测试证明 add(1,2)==3")))


class Priced:
    def __init__(self, inner, costs):
        self.inner, self.costs = inner, list(costs)
        self.requests = inner.requests

    def __call__(self, req):
        t, c = self.inner(req)
        return t, c.model_copy(update={"cost": self.costs.pop(0)})


class Trace:
    def __init__(self):
        self.runs, self.finished = [], []

    def start(self, run_id, **kw):
        self.runs.append((run_id, kw))

    def model(self, *a, **kw): ...

    def tool(self, *a, **kw): ...

    def finish(self, run_id, *, stop, error):
        self.finished.append((run_id, stop, error))


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


@pytest.fixture
def repo(git_repo):
    _git(git_repo, "branch", limits.LIVE_BRANCH)
    return git_repo


@pytest.fixture
def user(pool):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2",
                                  lang=Lang.EN, created_at=T0))
        switch.set_enabled(conn, "u1", "patch", True, T0)
    return "u1"


def _runner(forge: Path, stray: bool = True) -> FakeRunner:
    def coder_run(argv, cwd):
        if cwd != forge / "fc_1":
            return False
        if stray:                     # a test run leaves a file behind in the editing worktree
            (cwd / "stray.txt").write_text("junk", encoding="utf-8")
        return True

    return FakeRunner([
        (lambda a, cwd: "ruff" in a, outcome(), None),
        (lambda a, cwd: "swe-base" in cwd.name, outcome(1, exit="error"),
         {NEW: "failed", OLD: "passed"}),
        (coder_run, outcome(1, exit="error"), {NEW: "failed"}),
        (lambda a, cwd: cwd == forge / "build" / "fc_1", outcome(),
         {NEW: "passed", OLD: "passed"}),
    ])


def deps(pool, repo, tmp_path, converse, *, runner=None, trace=None, accounted=True):
    forge = tmp_path / "forge"
    return PatchDeps(pool=pool, converse=accounted_fake(converse) if accounted else converse,
                     runner=runner or _runner(forge), trace=trace or Trace(), main_root=repo,
                     forge_root=forge, python="py", test_db_url=lambda: DB,
                     env_file=tmp_path / "none.env", now=lambda: T0, new_id=lambda: "fc_1")


def _cand(pool, cid="fc_1"):
    with pool.connection() as conn:
        return store.get_candidate(conn, cid)


def _patch(pool, cid="fc_1"):
    with pool.connection() as conn:
        return patch_store.get_patch(conn, cid)


def test_fixes_the_planted_bug(pool, user, repo, tmp_path):
    conv = Priced(fixing_script(), [0.01, 0.02, 0.03, 0.04])
    trace = Trace()
    d = deps(pool, repo, tmp_path, conv, trace=trace)
    res = propose("patch", REQ, EVI, user_id=user, patch_deps=d)
    b = res.build
    assert isinstance(b, PatchBuildReport) and b.ok, b.reason
    assert res.kind == "patch" and res.candidate_id == "fc_1"
    assert b.fail_to_pass == (NEW,) and b.cost_usd == pytest.approx(0.10)
    assert b.run_id == "forge:fc_1" and trace.runs[0][0] == "forge:fc_1"
    base = Git(repo).rev_parse(limits.LIVE_BRANCH)
    head = Git(repo).rev_parse("forge/fc_1")
    assert b.commit == head and b.base_commit == base and head != base
    # one attributed commit with the trailers; the stray file is not in it
    msg = _git(repo, "log", "-1", "--format=%an <%ae>%n%B", head)
    assert msg.startswith(limits.FORGE_AUTHOR) and "forge: 修好加法" in msg
    assert "Forge-Candidate: fc_1" in msg and "Forge-Evidence: signal:1" in msg
    assert f"Forge-Base: {base}" in msg
    names = set(_git(repo, "diff", "--name-only", base, head).split())
    assert names == {"src/evowiki/deep/mathx.py", NEWF}
    # the build ran on the fresh checkout of the commit, not on the editing worktree
    forge = tmp_path / "forge"
    assert any(cwd == forge / "build" / "fc_1" for _, cwd, _ in d.runner.calls)
    # rows
    c = _cand(pool)
    assert c.status == "built" and c.kind == "patch" and c.name is None and c.user_id == "u1"
    assert c.build["ok"] is True and c.build["commit"] == head
    assert "+    return a + b" in c.files["diff"] and c.cost_usd == pytest.approx(0.10)
    p = _patch(pool)
    assert p.state == "built" and p.commit == head and p.branch == "forge/fc_1"
    assert p.base_commit == base and p.title == "修好加法" and p.cost_usd == pytest.approx(0.10)
    # worktrees gone, branch kept
    assert not (forge / "fc_1").exists() and not (forge / "build" / "fc_1").exists()
    assert len(_git(repo, "worktree", "list").splitlines()) == 1
    # the coder's request: deep-tier purpose and prompt; the first message asks to reproduce first
    first = conv.requests[0]
    assert first.purpose == PURPOSE and first.prompt_version == PROMPT_VERSION
    assert "先写复现测试并看到它失败" in first.messages[0].text


def test_coder_env_has_no_secrets(pool, user, repo, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-zzzzzzzzzzzzzzzzzzzzzzzz")
    d = deps(pool, repo, tmp_path, fixing_script())
    propose("patch", REQ, EVI, user_id=user, patch_deps=d)
    for _, _, env in d.runner.calls:
        assert "DEEPSEEK_API_KEY" not in env
        assert env["EVOWIKI_TEST_DATABASE_URL"] == DB


def test_kill_switch_off_by_default(pool, repo, tmp_path):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2",
                                  lang=Lang.EN, created_at=T0))
    conv = fixing_script()
    with pytest.raises(ForgeDisabled):
        propose("patch", REQ, EVI, user_id="u1", patch_deps=deps(pool, repo, tmp_path, conv))
    assert conv.requests == [] and _cand(pool) is None


def test_no_live_branch_bases_on_the_running_checkout(pool, user, git_repo, tmp_path):
    runner = _runner(tmp_path / "forge")
    res = propose("patch", REQ, EVI, user_id=user,
                  patch_deps=deps(pool, git_repo, tmp_path, fixing_script(), runner=runner))
    assert res.build.base_commit == _git(git_repo, "rev-parse", "HEAD").strip()


def test_converse_not_accounted(pool, user, repo, tmp_path):
    conv = fixing_script()
    with pytest.raises(ForgeMisconfigured):
        propose("patch", REQ, EVI, user_id=user,
                patch_deps=deps(pool, repo, tmp_path, conv, accounted=False))
    assert conv.requests == [] and _cand(pool) is None


def test_patch_needs_patch_deps(pool, user):
    with pytest.raises(TypeError):
        propose("patch", REQ, EVI, user_id=user)


def test_denied_edit_is_an_error_and_nothing_changes(pool, user, repo, tmp_path):
    conv = ScriptedConverse(
        turn(call(1, "edit_file", path="src/evowiki/store/db.py", old_string="X = 1",
                  new_string="X = 2")),
        turn(call(2, "submit", summary="改了")))
    d = deps(pool, repo, tmp_path, conv)
    res = propose("patch", REQ, EVI, user_id=user, patch_deps=d)
    tool_msg = conv.requests[1].messages[-1]
    assert tool_msg.role == "tool" and tool_msg.is_error
    assert not res.build.ok and "没有改动" in res.build.reason and res.build.commit == ""
    assert _cand(pool).status == "build_failed" and _patch(pool) is None
    assert Git(repo).rev_parse("forge/fc_1") == Git(repo).rev_parse(limits.LIVE_BRANCH)
    assert (repo / "src/evowiki/store/db.py").read_text() == "X = 1\n"
    assert d.runner.calls == []


def test_stray_files_alone_are_no_change(pool, user, repo, tmp_path):
    forge = tmp_path / "forge"
    conv = ScriptedConverse(turn(call(1, "run_cmd", argv=["pytest", "tests/test_old.py"])),
                            turn(call(2, "submit", summary="没改")))
    runner = FakeRunner([(lambda a, cwd: (cwd / "stray.txt").write_text("x") > 0,
                          outcome(), {OLD: "passed"})])
    res = propose("patch", REQ, EVI, user_id=user,
                  patch_deps=deps(pool, repo, tmp_path, conv, runner=runner))
    assert not res.build.ok and "没有改动" in res.build.reason
    assert not (forge / "fc_1").exists()


def test_coder_exception_fails_and_cleans_up(pool, user, repo, tmp_path):
    conv = ScriptedConverse(turn(call(1, "write_file", path=NEWF, content=TEST)),
                            RuntimeError("provider down"))
    trace = Trace()
    with pytest.raises(RuntimeError, match="provider down"):
        propose("patch", REQ, EVI, user_id=user,
                patch_deps=deps(pool, repo, tmp_path, conv, trace=trace))
    c = _cand(pool)
    assert c.status == "build_failed" and "provider down" in c.build["reason"]
    assert _patch(pool) is None
    assert not (tmp_path / "forge" / "fc_1").exists()
    assert len(_git(repo, "worktree", "list").splitlines()) == 1
    assert trace.finished and trace.finished[0][2]


@pytest.mark.parametrize("ending", ["cap", "cap_batch", "rejected_submit", "long_submit"])
@pytest.mark.parametrize("after", ["passed", "failed", "error", "skipped"])
def test_saved_patch_is_built_regardless_of_coder_ending(
        pool, user, repo, tmp_path, monkeypatch, ending, after):
    """模型停止原因不代替构建判定；即使它声称通过，也只认全新检出上的结果。"""
    script = fixing_script().items[:3]
    cap = 3
    summary = "改动和测试说明。" * 2000
    if ending == "cap_batch":
        script[-1] = turn(*script[-1].tool_calls, call(4, "submit", summary=summary))
    elif ending in ("rejected_submit", "long_submit"):
        cap = 4
        script.append(turn(call(4, "submit", summary=summary)))
        if ending == "rejected_submit":
            monkeypatch.setattr(SubmitTool, "run", lambda *a: fail("提交说明超过字数上限"))
    conv = ScriptedConverse(*script)
    trace = Trace()
    d = deps(pool, repo, tmp_path, conv, trace=trace)
    d = replace(
        d, pack=HarnessPack(1, {"H4.forge.patch.max_tool_calls": cap}))
    # 构建结果刻意与模型自报相反，证明不会信模型说“已通过”。
    match, _, _ = d.runner.script[-1]
    d.runner.script[-1] = (match, outcome(0 if after == "passed" else 1),
                           {NEW: after, OLD: "passed"})
    res = propose("patch", REQ, EVI, user_id=user, patch_deps=d)
    expected = after == "passed"
    assert res.build.ok is expected, res.build.reason
    assert res.build.commit and "+    return a + b" in _cand(pool).files["diff"]
    assert _cand(pool).status == ("built" if expected else "build_failed")
    assert (_patch(pool) is not None) is expected
    if expected:
        assert _patch(pool).state == "built"   # 仍需原有门控和批准，不直接生效
        assert res.build.fail_to_pass == (NEW,)
    assert trace.finished[-1][1] == ("finished" if ending == "long_submit" else "tool_cap")
    assert any(cwd == tmp_path / "forge" / "build" / "fc_1" for _, cwd, _ in d.runner.calls)
    assert len(conv.requests) == len(script)


@pytest.mark.parametrize("change", ["none", "source_only", "test_only"])
def test_cap_does_not_approve_incomplete_work(pool, user, repo, tmp_path, change):
    if change == "none":
        step = call(1, "list_files")
    elif change == "source_only":
        step = call(1, "edit_file", path="src/evowiki/deep/mathx.py",
                    old_string="return a - b", new_string="return a + b")
    else:
        step = call(1, "write_file", path=NEWF, content=TEST)
    trace = Trace()
    d = deps(pool, repo, tmp_path, ScriptedConverse(turn(step)), trace=trace)
    d = replace(
        d, pack=HarnessPack(1, {"H4.forge.patch.max_tool_calls": 1}))
    match, _, _ = d.runner.script[-1]
    d.runner.script[-1] = (match, outcome(1), {NEW: "failed", OLD: "passed"})
    res = propose("patch", REQ, EVI, user_id=user, patch_deps=d)
    assert not res.build.ok and _patch(pool) is None
    assert _cand(pool).status == "build_failed"
    assert trace.finished[-1][1] == "tool_cap"
    reason = {"none": "没有改动", "source_only": "没有新增测试", "test_only": "新测试没全部通过"}
    assert reason[change] in res.build.reason
