"""Live orchestration: what the supervisor (T11) does at an idle point on `rsi/live` (design
2026-10-22 §5.3, plan P3 T8). Git and the backend are injected, so this module is testable without
processes; it never touches the service's own working tree.

Landing is aider-style: `cherry-pick -x` of the candidate commit, undo = `git revert`. Forge commits
are found by their `Forge-Candidate: <cid>` trailer (rebase rewrites shas); every revert this module
writes carries `Forge-Revert: <cid>` so a later rebase can drop the undone pairs, and a revert that
re-applies an undone change carries `Forge-Candidate: <cid>` again.

One `step` = one supervisor tick:
1. pending work = operator requests, queued landings, queued reverts, a master move; none → nothing;
2. drain on; wait for `in_flight == 0` (never force: on timeout the requests are put back, `busy`);
3. revert-all (one combined commit) or single reverts; rebase if master moved; landings in FIFO
   order, each re-checked with `check_change` (check 3 of 3 of §5.1) before its cherry-pick;
4. if the tree changed: restart + canary (`/health` answers with the new commit and one cheap read
   works). On failure the tick's new commits are reverted newest first and the backend restarted;
   when that canary fails too, live is reset to the HEAD before the tick (`detail` starts with
   "fallback": the supervisor stops landing until an operator acts);
5. drain off (always).

Fail closed: an unknown /health is "not idle" and "canary failed"; a check that cannot run keeps the
landings queued; an unexpected git error resets live to the HEAD before the tick, restores the rows
and re-raises."""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from evowiki.forge import limits
from evowiki.forge.patch.git import Git, GitError
from evowiki.forge.patch.importgraph import GraphError
from evowiki.forge.patch.paths import check_change, simulator_files
from evowiki.store import forge as fstore
from evowiki.store import forge_patch as ps

TRAILER = "Forge-Candidate"
REVERT_TRAILER = "Forge-Revert"
CANARY_POLL_S = 2.0

REASON_CANARY = "代码改动导致启动失败，已撤回"
REASON_CONFLICT = "和当前代码冲突，已撤下"
REASON_REBASE_CONFLICT = "主线更新后和这次改动冲突，已撤下"
REASON_REVERT_CONFLICT = "撤回时和后来的改动冲突，没能撤回"
REASON_RESTORED = "撤回后启动失败，已恢复这项改动"
REASON_PRE_TICK = "启动失败，已回到这次之前的版本"
REVERT_ALL_MESSAGE = "撤回全部代码改动"


class Backend(Protocol):
    def health(self) -> dict | None: ...          # GET /health JSON, None when not answering
    def drain(self, on: bool) -> None: ...
    def restart(self) -> None: ...                # stop (only the owned process) + start from LIVE_ROOT
    def smoke(self) -> bool: ...                  # database readiness (GET /health/ready) → 200


@dataclass(frozen=True)
class StepResult:
    did: str            # "idle" | "busy" | "landed" | "reverted" | "canary_failed" | "rebased" | "nothing"
    candidate_ids: tuple[str, ...] = ()
    detail: str = ""


def trailers(message: str, key: str) -> tuple[str, ...]:
    """Values of every `key: value` line in a commit message, in order."""
    return tuple(re.findall(rf"^{re.escape(key)}:[ \t]*(\S+)[ \t]*$", message, re.MULTILINE))


def _health(backend: Backend) -> dict | None:
    try:
        h = backend.health()
    except Exception:  # noqa: BLE001 - an unknown answer is "not answering" (fail closed)
        return None
    return h if isinstance(h, dict) else None


def wait_idle(backend: Backend, *, timeout_s: float = limits.IDLE_WAIT_S, poll_s: float = 5.0,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> bool:
    """True once /health reports `in_flight == 0`; False after `timeout_s` (never forces)."""
    start = clock()
    while True:
        h = _health(backend)
        if (h is not None and h.get("drained") is True
                and type(h.get("in_flight")) is int and h["in_flight"] == 0):
            return True
        if clock() - start >= timeout_s:
            return False
        sleep(poll_s)


def canary(backend: Backend, *, expect_commit: str, within_s: float = limits.CANARY_S,
           sleep: Callable[[float], None] = time.sleep,
           clock: Callable[[], float] = time.monotonic,
           poll_s: float = CANARY_POLL_S) -> tuple[bool, str]:
    """(True, "") once /health answers with `expect_commit` and the smoke read works within
    `within_s`; else (False, the last plain reason). A process still serving another commit is a
    stale process, not a pass."""
    start = clock()
    while True:
        h = _health(backend)
        if h is None:
            why = "服务没有响应"
        elif h.get("commit") != expect_commit:
            why = f"服务还在运行别的版本（{str(h.get('commit') or '未知')[:12]}）"
        else:
            try:
                smoke = backend.smoke() is True
            except Exception:  # noqa: BLE001 - fail closed
                smoke = False
            if smoke:
                return True, ""
            why = "服务启动了，但读取请求失败"
        if clock() - start >= within_s:
            return False, why
        sleep(poll_s)


def _effective(live: Git, base: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Forge commits of `base..HEAD` still in effect, oldest first, as (cid, sha), minus pairs
    undone by a later revert; and the shas of commits that carry no forge trailer."""
    active: dict[str, str] = {}
    other: list[str] = []
    for sha, msg in live.commits_since(base):
        cands, revs = trailers(msg, TRAILER), trailers(msg, REVERT_TRAILER)
        for cid in revs:
            active.pop(cid, None)
        for cid in cands:
            active.pop(cid, None)
            active[cid] = sha
        if not cands and not revs:
            other.append(sha)
    return list(active.items()), other


def _cand_status(state: str) -> str:
    return {"withdrawn": "withdrawn", "reverted": "rolled_back",
            "canary_failed": "rolled_back"}.get(state, "promoted")


@dataclass
class _Tick:
    pool: Any
    live: Git
    now: Callable[[], datetime]
    pre: str
    snap: dict[str, ps.PatchRow] = field(default_factory=dict)
    landed: list[str] = field(default_factory=list)
    reverted: list[str] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)
    rebased: bool = False
    mark: str = ""

    def write(self, cid: str, state: ps.PatchState, *, reason: str = "",
              live_commit: str | None = None, revert_commit: str | None = None) -> None:
        with self.pool.connection() as conn:
            if cid not in self.snap:
                row = ps.get_patch(conn, cid)
                if row is None:
                    return
                self.snap[cid] = row
            ps.set_state(conn, cid, state, now=self.now(), reason=reason, live_commit=live_commit,
                         revert_commit=revert_commit)
            fstore.set_status(conn, cid, _cand_status(state), now=self.now())

    def event(self, kind: str, cid: str | None, detail: dict[str, Any]) -> None:
        with self.pool.connection() as conn:
            if cid is not None and ps.get_patch(conn, cid) is None:
                detail = {**detail, "candidate": cid}
                cid = None
            ps.log_event(conn, kind, candidate_id=cid, detail=detail, now=self.now())

    def withdraw(self, cid: str, reason: str) -> None:
        self.write(cid, "withdrawn", reason=reason)
        self.withdrawn.append(cid)
        self.event("withdrawn", cid, {"reason": reason})

    def restore(self, reason: str) -> None:
        """Rows back to their state before the tick; landings of this tick → canary_failed."""
        for cid, row in self.snap.items():
            if cid in self.landed:
                self.write(cid, "canary_failed", reason=REASON_CANARY)
            else:
                self.write(cid, row.state, reason=reason, live_commit=row.live_commit,
                           revert_commit=row.revert_commit)


def step(pool, *, live: Git, main: Git, backend: Backend, now: Callable[[], datetime],
         sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic, landings: bool = True) -> StepResult:
    """One supervisor tick (see the module doc). `landings=False` (kill switch, paused supervisor):
    queued landings stay queued; reverts, revert-all and rebase still run (undo always works)."""
    with pool.connection() as conn:
        requests = ps.take_live_requests(conn)
        queued = ps.patches(conn, states=["queued"]) if landings else []
        reverts = ps.patches(conn, states=["revert_queued"])
    master = main.rev_parse("master")
    moved = live.merge_base("HEAD", master) != master
    if not (requests or queued or reverts or moved):
        # Recover admission if the last undrain request failed or the supervisor
        # exited after a restart. Reopen only the current, healthy live version.
        h = _health(backend)
        if h and h.get("drained") is True and h.get("commit") == live.head() and backend.smoke():
            backend.drain(False)
        return StepResult("nothing")
    try:
        backend.drain(True)
    except Exception:
        with pool.connection() as conn:
            for r in requests:
                ps.set_live_request(conn, r["op"], by=r["by_user"], now=now())
        backend.drain(False)
        raise
    try:
        if not wait_idle(backend, sleep=sleep, clock=clock):
            with pool.connection() as conn:
                for r in requests:
                    ps.set_live_request(conn, r["op"], by=r["by_user"], now=now())
                ps.log_event(conn, "idle_timeout", candidate_id=None,
                             detail={"waited_s": limits.IDLE_WAIT_S}, now=now())
            return StepResult("busy")
        tick = _Tick(pool, live, now, pre=live.head())
        revert_all = any(r["op"] == "revert_all" for r in requests)
        try:
            _apply(tick, live=live, master=master, moved=moved, revert_all=revert_all,
                   queued=queued, reverts=reverts)
        except Exception as e:
            live.reset_hard(tick.pre)
            tick.restore(REASON_PRE_TICK)
            tick.event("error", None, {"error": f"{type(e).__name__}: {str(e)[:300]}"})
            raise
        if live.head() == tick.pre:
            return StepResult("nothing", tuple(tick.withdrawn),
                              "withdrawn" if tick.withdrawn else "")
        return _restart_and_check(tick, live=live, backend=backend, sleep=sleep, clock=clock)
    finally:
        backend.drain(False)


def _apply(t: _Tick, *, live: Git, master: str, moved: bool, revert_all: bool,
           queued: list[ps.PatchRow], reverts: list[ps.PatchRow]) -> None:
    if revert_all:
        _revert_all(t, live, reverts)
    else:
        for row in reverts:
            _revert_one(t, live, row)
    t.mark = t.pre                 # canary auto-revert undoes every new commit after this mark
    if moved:
        _rebase(t, live, master)
        t.mark = live.head()
    if queued:
        _land(t, live, queued)


def _revert_one(t: _Tick, live: Git, row: ps.PatchRow) -> None:
    cid = row.candidate_id
    if row.live_commit is None:
        t.write(cid, "reverted")
        t.reverted.append(cid)
        return
    sha = live.revert(row.live_commit, f"撤回代码改动：{row.title}\n\n{REVERT_TRAILER}: {cid}",
                      author=limits.FORGE_AUTHOR)
    if sha is None:
        t.write(cid, "landed", reason=REASON_REVERT_CONFLICT)
        t.event("revert", cid, {"ok": False, "reason": REASON_REVERT_CONFLICT})
        return
    t.write(cid, "reverted", revert_commit=sha)
    t.reverted.append(cid)
    t.event("revert", cid, {"ok": True, "commit": sha})


def _revert_all(t: _Tick, live: Git, reverts: list[ps.PatchRow]) -> None:
    before = live.head()
    eff, _ = _effective(live, live.merge_base("HEAD", "master"))
    sha: str | None = None
    if eff:
        try:
            for _cid, c in reversed(eff):
                live.run("revert", "--no-commit", "--end-of-options", c)
        except GitError as e:
            live.run("revert", "--abort", check=False)
            live.reset_hard(before)
            t.event("revert_all", None, {"ok": False, "error": str(e)[:300]})
            return
        message = REVERT_ALL_MESSAGE + "\n\n" + "\n".join(f"{REVERT_TRAILER}: {cid}"
                                                         for cid, _ in reversed(eff))
        sha = live.commit(message, author=limits.FORGE_AUTHOR)
    done = [cid for cid, _ in eff]
    for cid in done:
        t.write(cid, "reverted", revert_commit=sha)
        t.reverted.append(cid)
    for row in reverts:                      # queued reverts no longer on live
        if row.candidate_id not in done:
            t.write(row.candidate_id, "reverted")
            t.reverted.append(row.candidate_id)
    t.event("revert_all", None, {"ok": True, "commit": sha, "candidates": done})


def _check(live: Git, sha: str, sim: frozenset[str]) -> tuple[str, ...]:
    """Check 3 of 3 (§5.1): the commit's own change against the live tree it would land on."""
    return check_change(live.diff_raw(f"{sha}^", sha), base_files=live.ls_files("HEAD"),
                        sim_files=sim)


def _rebase(t: _Tick, live: Git, master: str) -> None:
    old_base = live.merge_base("HEAD", master)
    eff, dropped = _effective(live, old_base)
    live.reset_hard(master)
    sim = simulator_files(live.root)          # GraphError → the tick resets and re-raises
    carried: list[str] = []
    for cid, sha in eff:
        problems = _check(live, sha, sim)
        if problems:
            t.withdraw(cid, "；".join(problems))
        elif live.cherry_pick(sha):
            t.write(cid, "landed", live_commit=live.head())
            carried.append(cid)
        else:
            t.withdraw(cid, REASON_REBASE_CONFLICT)
    t.rebased = True
    t.event("rebase", None, {"from": old_base, "to": master, "carried": carried,
                             "withdrawn": list(t.withdrawn), "dropped": dropped})


def _land(t: _Tick, live: Git, queued: list[ps.PatchRow]) -> None:
    try:
        sim = simulator_files(live.root)
    except (GraphError, OSError) as e:
        t.event("check_failed", None, {"error": str(e)[:300],
                                       "kept_queued": [r.candidate_id for r in queued]})
        return
    for row in queued:
        cid = row.candidate_id
        try:
            log = live.commits_since(f"{row.commit}^", row.commit)
        except GitError:
            log = []
        if len(log) != 1 or cid not in trailers(log[0][1], TRAILER):
            t.withdraw(cid, "找不到这次改动对应的提交")
            continue
        problems = _check(live, row.commit, sim)
        if problems:
            t.withdraw(cid, "；".join(problems))
        elif live.cherry_pick(row.commit):
            head = live.head()
            t.write(cid, "landed", live_commit=head)
            t.landed.append(cid)
            t.event("land", cid, {"commit": head})
        else:
            t.withdraw(cid, REASON_CONFLICT)


def _restart(backend: Backend) -> str:
    try:
        backend.restart()
    except Exception as e:  # noqa: BLE001 - a failed restart is a failed canary
        return f"重启失败：{type(e).__name__}"
    return ""


def _restart_and_check(t: _Tick, *, live: Git, backend: Backend, sleep, clock) -> StepResult:
    def attempt() -> tuple[bool, str]:
        head = live.head()
        why = _restart(backend)
        t.event("restart", None, {"commit": head, "error": why})
        if why:
            return False, why
        return canary(backend, expect_commit=head, sleep=sleep, clock=clock)

    ok, why = attempt()
    if ok:
        t.event("canary_ok", None, {"commit": live.head()})
        if t.landed:
            return StepResult("landed", tuple(t.landed))
        if t.reverted:
            return StepResult("reverted", tuple(t.reverted))
        return StepResult("rebased" if t.rebased else "nothing", tuple(t.withdrawn))
    t.event("canary_failed", None, {"why": why, "attempt": 1, "candidates": list(t.landed)})

    # first: revert this tick's new commits (after the rebase, if any), newest first
    undone: dict[str, str] = {}      # cid of a landing → its auto-revert commit
    redone: dict[str, str] = {}      # cid of a revert → the commit re-applying it
    try:
        for sha, msg in reversed(live.commits_since(t.mark)):
            cands, revs = trailers(msg, TRAILER), trailers(msg, REVERT_TRAILER)
            if cands:
                text = "启动失败，撤回代码改动\n\n" + "\n".join(f"{REVERT_TRAILER}: {c}" for c in cands)
            else:
                text = "启动失败，恢复被撤回的代码改动\n\n" + "\n".join(f"{TRAILER}: {c}" for c in revs)
            r = live.revert(sha, text, author=limits.FORGE_AUTHOR)
            if r is None:
                raise GitError("自动撤回时冲突")
            undone.update(dict.fromkeys(cands, r))
            redone.update(dict.fromkeys(revs, r))
        ok2, why2 = attempt()
    except GitError as e:
        ok2, why2 = False, str(e)[:300]
    if ok2:
        for cid in t.landed:
            t.write(cid, "canary_failed", reason=REASON_CANARY, revert_commit=undone.get(cid))
        for cid, sha in redone.items():
            if cid in t.reverted:
                t.write(cid, "landed", reason=REASON_RESTORED, live_commit=sha)
        t.event("canary_ok", None, {"commit": live.head(), "after": "auto-revert"})
        return StepResult("canary_failed", tuple(t.landed), why)

    # fallback: back to the HEAD before this tick
    live.reset_hard(t.pre)
    ok3, why3 = attempt()
    t.restore(REASON_PRE_TICK)
    t.event("canary_failed", None, {"fallback": "pre-tick", "why": why2, "ok": ok3,
                                    "why_after": why3})
    return StepResult("canary_failed", tuple(t.landed), f"fallback:pre-tick；{why2}")
