"""`propose`: the one entry point the improver uses to get a code-level candidate (design
2026-10-22 §6.1, plan T7). It only builds and tests; it never promotes.

Order: kill switch → parent load → `building` row (short transaction) → coder inside a virtual
durable workflow (no connection held) → `build` re-runs lint, schema, tests and the examples
itself (the host-checked examples are authoritative, never the coder's claims) → the row becomes
`built` / `build_failed`. Every candidate is archived, failures too (DGM). A model / sandbox
exception marks the row `build_failed` with the reason and is re-raised (fail closed, visible).

`kind="patch"` (L2, plan P3 T9) is `_propose_patch`: a git worktree per attempt, the L2 coder,
an attributed commit, then the build cascade on a fresh checkout of that commit."""

import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
from psycopg_pool import ConnectionPool

from evowiki import paths
from evowiki.agent import durable
from evowiki.agent.loop import Converse
from evowiki.agent.trace import TraceSink
from evowiki.forge import ids, limits, switch
from evowiki.forge.binding import accounted
from evowiki.forge.build import ExampleSplit, build, split_examples
from evowiki.forge.coder import run_coder, start_workspace
from evowiki.forge.models import (
    BuildReport,
    Evidence,
    ForgeDisabled,
    ForgeKind,
    ForgeMisconfigured,
    ForgeNotFound,
    ForgeResult,
    PatchRequest,
    SandboxStats,
    ToolRequest,
)
from evowiki.forge.patch import testdb
from evowiki.forge.patch.build import build_patch
from evowiki.forge.patch.coder import run_patch_coder
from evowiki.forge.patch.git import Git, GitError
from evowiki.forge.patch.paths import declared_distributions, env_secret_values, simulator_files
from evowiki.forge.patch.tools import PatchHolder
from evowiki.forge.patch.workspace import PatchWorkspace
from evowiki.forge.sandbox import Sandbox
from evowiki.forge.sandbox.jobs import Runner
from evowiki.llm.chat import ChatRequest, ChatTurn
from evowiki.llm.models import LLMCall
from evowiki.store import forge as store
from evowiki.store import forge_patch as patch_store

if TYPE_CHECKING:
    from evowiki.harness.pack import HarnessPack

__all__ = ["ExampleSplit", "ForgeDeps", "PatchDeps", "propose", "split_examples"]


def _new_id() -> str:
    return f"fc_{uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(UTC)


def _same(r: ChatRequest) -> ChatRequest:
    return r


@dataclass(frozen=True)
class ForgeDeps:
    pool: ConnectionPool
    converse: Converse                 # agent.loop.Converse, already bound to the forge.code tier
    sandbox: Sandbox
    trace: TraceSink
    sent: Callable[[ChatRequest], ChatRequest] = field(default=_same)
    now: Callable[[], datetime] = field(default=_now)
    new_id: Callable[[], str] = field(default=_new_id)
    # where the candidate came from (stored on it): "round", "run:<run_id>", "session:<session_id>"
    origin: str = "round"
    # descriptions of the request's host tools for the coder's prompt (T5 host_section)
    host_docs: Mapping[str, str] | None = None
    # the proposing user's harness pack (a harness round passes its base pack): its
    # H4.forge.max_tool_calls may lower the coder's tool-call cap; None = the forge's cap
    pack: "HarnessPack | None" = None


class _Metered:
    """Wraps a converse and keeps every call's `LLMCall` for the cost sum."""

    def __init__(self, converse: Converse):
        self._converse = converse
        self.calls: list[LLMCall] = []

    def __call__(self, req: ChatRequest) -> tuple[ChatTurn, LLMCall]:
        turn, call = self._converse(req)
        self.calls.append(call)
        return turn, call

    def cost(self) -> float | None:
        """Same semantics as `ToolContext.spent_usd`: None when any call has no price."""
        costs = [c.cost for c in self.calls]
        return None if any(x is None for x in costs) else float(sum(costs))


def _failed(reason: str, run_id: str, cost: float | None) -> BuildReport:
    return BuildReport(ok=False, tests_passed=0, tests_failed=0, examples_passed=0,
                       examples_failed=0, lint_ok=False, schema_ok=False,
                       sandbox=SandboxStats(wall_s=0.0, exit="error"), run_id=run_id,
                       cost_usd=cost, reason=reason)


def _parent_files(conn: psycopg.Connection, parent: str, user_id: str) -> dict[str, str]:
    row = store.get_candidate(conn, parent)
    # another user's candidate is "not found" for this user: no cross-user reads
    if row is None or row.kind != "tool" or row.user_id not in (user_id, None):
        raise ForgeNotFound(f"没有找到要改进的工具候选 {parent}")
    return dict(row.files)


def propose(kind: ForgeKind, spec: ToolRequest | PatchRequest, evidence: Evidence,
            *, user_id: str, deps: ForgeDeps | None = None,
            patch_deps: "PatchDeps | None" = None) -> ForgeResult:
    """Build and test one candidate; archive it whatever the outcome. Never promotes.

    Raises `ForgeMisconfigured` (before any row or model call) unless the converse is an
    accounted `ForgeConverse`, `ForgeDisabled` when the kill switch is off, `ForgeNotFound` for an unknown parent,
    `TypeError` when the spec or the deps do not fit `kind` (`kind="patch"` needs a `PatchRequest`
    and `patch_deps`); a model or sandbox exception is re-raised after the candidate is marked
    `build_failed`."""
    ids.check_user(user_id)
    if kind == "patch":
        if not isinstance(spec, PatchRequest) or patch_deps is None:
            raise TypeError("kind='patch' needs a PatchRequest and patch_deps")
        return _propose_patch(spec, evidence, user_id=user_id, deps=patch_deps)
    if kind != "tool" or not isinstance(spec, ToolRequest) or deps is None:
        raise TypeError("kind='tool' needs a ToolRequest and deps")
    if not accounted(deps.converse):
        raise ForgeMisconfigured("forge model calls must carry an Account (cost accounting)")
    cid = deps.new_id()
    run_id = f"forge:{cid}"
    started = deps.now()
    with deps.pool.connection() as conn:
        if not switch.enabled(conn, user_id, "tool"):
            raise ForgeDisabled("工具自动生成已关闭")
        seed: Mapping[str, str] | None = (
            _parent_files(conn, spec.parent, user_id) if spec.parent else None)
        # LATM: the coder sees only the visible examples; `build` runs the held-out ones
        coder_spec = split_examples(spec).request_for_coder
        start = start_workspace(coder_spec, seed)
        store.insert_candidate(conn, store.CandidateRow(
            id=cid, user_id=None if spec.scope == "global" else user_id, kind="tool",
            name=spec.name, parent_id=spec.parent, spec=spec.model_dump(mode="json"),
            evidence=evidence.model_dump(mode="json"), files=dict(start.files), build=None,
            status="building", run_id=run_id, cost_usd=None, created_at=started,
            updated_at=started, origin=deps.origin))

    metered = _Metered(deps.converse)
    files: dict[str, str] = dict(start.files)
    try:
        with durable.virtual_workflow(cid):
            ws, _ = run_coder(coder_spec, metered, sandbox=deps.sandbox, trace=deps.trace,
                              run_id=run_id, user_id=user_id, seed=seed, sent=deps.sent,
                              host_docs=deps.host_docs, pack=deps.pack)
        files = dict(ws.files)
        report = build(ws, spec, deps.sandbox)
    except BaseException as exc:
        cost = metered.cost()
        bad = _failed(f"生成过程出错：{type(exc).__name__}: {str(exc)[:300]}", run_id, cost)
        with deps.pool.connection() as conn:
            store.set_build(conn, cid, build=bad.model_dump(mode="json"), files=files,
                            status="build_failed", cost_usd=cost, now=deps.now())
        raise
    cost = metered.cost()
    report = report.model_copy(update={"run_id": run_id, "cost_usd": cost})
    with deps.pool.connection() as conn:
        store.set_build(conn, cid, build=report.model_dump(mode="json"), files=files,
                        status="built" if report.ok else "build_failed", cost_usd=cost,
                        now=deps.now())
    return ForgeResult(candidate_id=cid, kind="tool", build=report)


# --- L2 code patches (plan P3 T9) ------------------------------------------------------------

DIFF_MAX_BYTES = 200 << 10


@dataclass(frozen=True)
class PatchDeps:
    pool: ConnectionPool
    converse: Converse                 # ForgeConverse over the DEEP tier gateway (R7)
    runner: Runner
    trace: TraceSink
    main_root: Path                    # the main Git checkout; worktrees are added from it
    live_branch: str = limits.LIVE_BRANCH
    forge_root: Path = Path(limits.FORGE_ROOT)
    python: str = sys.executable
    test_db_url: Callable[[], str] = field(default=testdb.admin_url)
    env_file: Path | None = None
    now: Callable[[], datetime] = field(default=_now)
    new_id: Callable[[], str] = field(default=_new_id)
    origin: str = "round"
    sent: Callable[[ChatRequest], ChatRequest] = field(default=_same)
    # the proposing user's harness pack (a harness round passes its base pack): its
    # H4.forge.patch.max_tool_calls may lower the coder's tool-call cap; None = the forge's cap
    pack: "HarnessPack | None" = None

    def __post_init__(self) -> None:
        if self.env_file is None:
            object.__setattr__(self, "env_file", paths.ROOT / ".env")


def _clip_bytes(text: str, cap: int) -> str:
    raw = text.encode("utf-8")
    return text if len(raw) <= cap else raw[:cap].decode("utf-8", errors="ignore")


def _commit_message(spec: PatchRequest, evidence: Evidence, cid: str, base: str) -> str:
    return (f"forge: {spec.title}\n\n{spec.goal}\n\n"
            f"Forge-Candidate: {cid}\n"
            f"Forge-Evidence: {', '.join(evidence.citations)}\n"
            f"Forge-Base: {base}\n")


def _patch_parent(conn: psycopg.Connection, parent: str, user_id: str) -> None:
    row = store.get_candidate(conn, parent)
    if row is None or row.kind != "patch" or row.user_id not in (user_id, None):
        raise ForgeNotFound(f"没有找到要改进的代码改动候选 {parent}")


def _remove_worktree(git: Git, path: Path) -> None:
    if not path.exists():
        return
    try:
        git.worktree_remove(path)
    except GitError:
        shutil.rmtree(path, ignore_errors=True)
        git.run("worktree", "prune", check=False)


def _intended_paths(ws: PatchWorkspace) -> list[str]:
    """The coder's change: tracked files that differ from the base, plus the new files the
    workspace itself wrote. Untracked files a test run left behind are not part of it (they are
    never committed, so the fresh checkout the build runs on does not have them)."""
    created = ws._created
    return sorted({c.path for c in ws.changes() if c.path in ws.base_files or c.path in created})


def _propose_patch(spec: PatchRequest, evidence: Evidence, *, user_id: str,
                   deps: PatchDeps) -> ForgeResult:
    """`propose("patch", ...)`: kill switch -> `rsi/live` base -> `building` row -> worktree
    `forge/<cid>` -> L2 coder (virtual durable workflow) -> one attributed commit of the coder's
    change -> `build_patch` on a FRESH detached checkout of that commit (the verdict never comes
    from the editing worktree) -> `forge_patch` row (`built`) on ok -> candidate `built` /
    `build_failed` with the report and the diff. Both worktrees are removed in every case; the
    branch keeps the commit (DGM archive; replay re-adds a worktree from it)."""
    if not accounted(deps.converse):
        raise ForgeMisconfigured("forge model calls must carry an Account (cost accounting)")
    cid = deps.new_id()
    run_id = f"forge:{cid}"
    started = deps.now()
    main = Git(Path(deps.main_root))
    with deps.pool.connection() as conn:
        if not switch.enabled(conn, user_id, "patch"):
            raise ForgeDisabled("代码自动修改已关闭")
        try:
            base = main.rev_parse(deps.live_branch)
        except GitError:
            base = main.rev_parse("HEAD")   # never served from live: the checkout it runs from
        if spec.parent:
            _patch_parent(conn, spec.parent, user_id)
        store.insert_candidate(conn, store.CandidateRow(
            id=cid, user_id=user_id, kind="patch", name=None, parent_id=spec.parent,
            spec=spec.model_dump(mode="json"), evidence=evidence.model_dump(mode="json"),
            files={}, build=None, status="building", run_id=run_id, cost_usd=None,
            created_at=started, updated_at=started, origin=deps.origin))

    forge_root = Path(deps.forge_root)
    wt = forge_root / cid
    fresh = forge_root / "build" / cid
    tmp = forge_root / "tmp" / cid
    branch = f"forge/{cid}"
    metered = _Metered(deps.converse)
    files: dict[str, str] = {}
    try:
        try:
            tmp.mkdir(parents=True, exist_ok=True)
            git = main.worktree_add(wt, branch=branch, base=base)
            base_files = git.ls_files(base)
            sim_files = simulator_files(wt)                 # of the base: before any edit
            ws = PatchWorkspace(root=wt, git=git, base_commit=base, base_files=base_files,
                                sim_files=sim_files)
            holder = PatchHolder(ws=ws, runner=deps.runner, python=deps.python,
                                 test_db_url=deps.test_db_url(), tmp=tmp / "coder")
            holder.tmp.mkdir(exist_ok=True)
            with durable.virtual_workflow(cid):
                run_patch_coder(spec, metered, holder=holder, trace=deps.trace, run_id=run_id,
                                user_id=user_id, sent=deps.sent, evidence=evidence,
                                pack=deps.pack)
            paths = _intended_paths(ws)
            # Deliberately independent of the coder's stop / submit status: tool_cap or a
            # rejected summary must not discard completed edits. Only build_patch on the
            # fresh checkout decides success (including fail-to-pass and old-test regression).
            commit = ""
            if paths:
                git.run("add", "-A", "--", *paths)
                commit = git.commit(_commit_message(spec, evidence, cid, base),
                                    author=limits.FORGE_AUTHOR)
                files = {"diff": _clip_bytes(git.diff(base, commit), DIFF_MAX_BYTES)}
            fresh.parent.mkdir(parents=True, exist_ok=True)
            git.run("worktree", "add", "-q", "--detach", "--", str(fresh), commit or base)
            fresh_ws = PatchWorkspace(root=fresh, git=Git(fresh), base_commit=base,
                                      base_files=base_files, sim_files=sim_files)
            pyproject = tmp / "pyproject.base.toml"
            pyproject.write_text(git.show(base, "pyproject.toml") or "", encoding="utf-8")
            report = build_patch(fresh_ws, runner=deps.runner, python=deps.python,
                                 test_db_url=holder.test_db_url, tmp=tmp / "build",
                                 env_values=env_secret_values(Path(deps.env_file)),
                                 declared=declared_distributions(pyproject), cid=cid,
                                 request=spec, evidence=evidence)
        except BaseException as exc:
            cost = metered.cost()
            bad = _failed(f"生成过程出错：{type(exc).__name__}: {str(exc)[:300]}", run_id, cost)
            with deps.pool.connection() as conn:
                store.set_build(conn, cid, build=bad.model_dump(mode="json"), files=files,
                                status="build_failed", cost_usd=cost, now=deps.now())
            raise
        cost = metered.cost()
        report = report.model_copy(update={"run_id": run_id, "cost_usd": cost, "commit": commit})
        now = deps.now()
        with deps.pool.connection() as conn:
            if report.ok:
                patch_store.insert_patch(conn, patch_store.PatchRow(
                    candidate_id=cid, user_id=user_id, title=spec.title, base_commit=base,
                    commit=commit, branch=branch, state="built", live_commit=None,
                    revert_commit=None, reason=report.reason, gate_ref=None, cost_usd=cost,
                    created_at=now, updated_at=now))
            store.set_build(conn, cid, build=report.model_dump(mode="json"), files=files,
                            status="built" if report.ok else "build_failed", cost_usd=cost,
                            now=now)
        return ForgeResult(candidate_id=cid, kind="patch", build=report)
    finally:
        _remove_worktree(main, fresh)
        _remove_worktree(main, wt)
        shutil.rmtree(tmp, ignore_errors=True)
