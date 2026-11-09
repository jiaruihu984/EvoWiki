"""Live orchestration (plan P3 T8): throwaway repos in tmp_path, a fake backend, the test Postgres.
Never the real application repository or its rsi/live branch."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evowiki.forge import limits
from evowiki.forge.patch import live as live_mod
from evowiki.forge.patch.git import Git
from evowiki.store import forge as fstore
from evowiki.store import forge_patch as ps
from tests.forge.patch.fakes import FakeBackend

T = datetime(2026, 10, 23, 9, tzinfo=UTC)


@dataclass
class Setup:
    main: Git
    live: Git
    backend: FakeBackend
    cids: list[str]
    tmp: Path

    def step(self):
        return live_mod.step(self.pool, live=self.live, main=self.main, backend=self.backend,
                             now=lambda: T, sleep=self.backend.sleep, clock=self.backend.clock)

    def row(self, cid):
        with self.pool.connection() as conn:
            return ps.get_patch(conn, cid)

    def set_state(self, cid, state):
        with self.pool.connection() as conn:
            ps.set_state(conn, cid, state, now=T)

    def events(self):
        with self.pool.connection() as conn:
            return [e["kind"] for e in ps.events(conn)]


def _candidate(main: Git, tmp: Path, cid: str, files: dict[str, str]) -> str:
    wt = main.worktree_add(tmp / f"wt_{cid}", branch=f"forge/{cid}", base="master")
    for p, text in files.items():
        f = wt.root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    wt.add_all()
    sha = wt.commit(f"修好 {cid}\n\n{live_mod.TRAILER}: {cid}", author=limits.FORGE_AUTHOR)
    main.worktree_remove(wt.root)
    return sha


@pytest.fixture
def live_setup(pool, git_repo, tmp_path):
    def make(queued=1, *, in_flight=0, boots=(), smoke_ok=True, touch=None, files=None):
        main = Git(git_repo)
        live = main.worktree_add(tmp_path / "live", branch="rsi/live", base="master")
        cids = []
        for i in range(queued):
            cid = f"fc_{i}"
            if files is not None:
                fs = files[i]
            elif touch is not None:
                fs = {touch: f"# changed {i}\n"}
            else:
                fs = {f"src/evowiki/deep/new{i}.py": f"N = {i}\n"}
            sha = _candidate(main, tmp_path, cid, fs)
            with pool.connection() as conn:
                fstore.insert_candidate(conn, fstore.CandidateRow(
                    id=cid, user_id=None, kind="patch", name=None, parent_id=None,
                    spec={"goal": "g"}, evidence={}, files={}, build=None, status="promoted",
                    run_id=None, cost_usd=0.1, created_at=T, updated_at=T, origin="round"))
                ps.insert_patch(conn, ps.PatchRow(
                    candidate_id=cid, user_id=None, title=f"改动 {i}", base_commit=main.head(),
                    commit=sha, branch=f"forge/{cid}", state="queued", live_commit=None,
                    revert_commit=None, reason="", gate_ref=None, cost_usd=0.1,
                    created_at=T.replace(second=i), updated_at=T))
            cids.append(cid)
        backend = FakeBackend(live, in_flight=in_flight, boots=list(boots), smoke_ok=smoke_ok)
        s = Setup(main, live, backend, cids, tmp_path)
        s.pool = pool
        return s
    return make


def test_nothing_to_do(live_setup):
    s = live_setup(queued=0)
    r = s.step()
    assert r.did == "nothing" and s.backend.drains == [] and s.backend.restarts == 0


def test_lands_queued_patch_and_canary_ok(live_setup):
    s = live_setup(queued=1)
    cid = s.cids[0]
    r = s.step()
    assert r.did == "landed" and r.candidate_ids == (cid,)
    p = s.row(cid)
    assert p.state == "landed" and p.live_commit == s.live.head()
    assert (s.live.root / "src/evowiki/deep/new0.py").exists()
    assert s.backend.restarts == 1 and s.backend.drains == [True, False]
    assert "land" in s.events() and "canary_ok" in s.events()


def test_lands_fifo(live_setup):
    s = live_setup(queued=2)
    r = s.step()
    assert r.candidate_ids == tuple(s.cids)
    msgs = [m for _, m in s.live.commits_since("master")]
    assert [live_mod.trailers(m, live_mod.TRAILER) for m in msgs] == [("fc_0",), ("fc_1",)]


def test_busy_never_restarts(live_setup):
    s = live_setup(queued=1, in_flight=3)
    r = s.step()
    assert r.did == "busy" and s.backend.restarts == 0 and s.backend.drains == [True, False]
    assert s.row(s.cids[0]).state == "queued"
    assert "idle_timeout" in s.events()
    assert s.backend.t >= limits.IDLE_WAIT_S


def test_idle_tick_restores_admission_after_a_missed_undrain(live_setup):
    s = live_setup(queued=0)
    s.backend.drains.append(True)
    assert s.step().did == "nothing"
    assert s.backend.drains == [True, False] and s.backend.restarts == 0


def test_failed_drain_keeps_operator_requests(live_setup, monkeypatch):
    s = live_setup(queued=0)
    with s.pool.connection() as conn:
        ps.set_live_request(conn, "revert_all", by="u1", now=T)

    def fail(on):
        if on:
            raise RuntimeError("drain unavailable")

    monkeypatch.setattr(s.backend, "drain", fail)
    with pytest.raises(RuntimeError, match="drain unavailable"):
        s.step()
    with s.pool.connection() as conn:
        assert ps.take_live_requests(conn)[0]["op"] == "revert_all"
    assert s.backend.restarts == 0


def test_busy_keeps_operator_requests(live_setup):
    s = live_setup(queued=0, in_flight=1)
    with s.pool.connection() as conn:
        ps.set_live_request(conn, "revert_all", by="u1", now=T)
    assert s.step().did == "busy"
    with s.pool.connection() as conn:
        assert [r["op"] for r in ps.take_live_requests(conn)] == ["revert_all"]


def test_wait_idle_unknown_health_is_not_idle():
    class Down:
        def health(self):
            return None
    t = [0.0]
    assert live_mod.wait_idle(Down(), timeout_s=10, poll_s=1, sleep=lambda s: t.__setitem__(0, t[0] + s),
                              clock=lambda: t[0]) is False


def test_canary_failure_auto_reverts(live_setup):
    s = live_setup(queued=1, boots=["down", "ok"])
    cid = s.cids[0]
    before = s.live.head()
    r = s.step()
    assert r.did == "canary_failed" and r.candidate_ids == (cid,)
    assert s.live.run("diff", before, "HEAD", "--stat").strip() == ""   # tree back to before
    p = s.row(cid)
    assert p.state == "canary_failed" and "已撤回" in p.reason and p.revert_commit == s.live.head()
    assert s.backend.restarts == 2 and s.backend.drains == [True, False]
    assert "canary_failed" in s.events()


def test_canary_failure_twice_falls_back_to_pre_tick(live_setup):
    s = live_setup(queued=1, boots=["down", "down", "ok"])
    before = s.live.head()
    r = s.step()
    assert r.did == "canary_failed" and r.detail.startswith("fallback")
    assert s.live.head() == before
    assert s.backend.restarts == 3
    assert s.row(s.cids[0]).state == "canary_failed"
    with s.pool.connection() as conn:
        fails = [e for e in ps.events(conn) if e["kind"] == "canary_failed"]
    assert any(e["detail"].get("fallback") == "pre-tick" for e in fails)


def test_canary_checks_commit(live_setup):
    s = live_setup(queued=1, boots=["stale", "ok"])
    r = s.step()
    assert r.did == "canary_failed"      # /health answered, but by the old process


def test_canary_needs_smoke(live_setup):
    s = live_setup(queued=1)
    s.backend.smoke_ok = False
    ok, why = live_mod.canary(s.backend, expect_commit=s.live.head(), within_s=10,
                              sleep=s.backend.sleep, clock=s.backend.clock)
    assert ok is False and why


def test_landing_rechecks_paths(live_setup):
    s = live_setup(queued=1, touch="src/evowiki/store/db.py")
    before = s.live.head()
    r = s.step()
    p = s.row(s.cids[0])
    assert p.state == "withdrawn" and p.reason
    assert r.did == "nothing" and r.candidate_ids == (s.cids[0],)
    assert s.live.head() == before and s.backend.restarts == 0
    with s.pool.connection() as conn:
        assert fstore.get_candidate(conn, s.cids[0]).status == "withdrawn"


def test_landing_refuses_simulator_file(live_setup):
    s = live_setup(queued=1, touch="src/evowiki/content/shared.py")
    s.step()
    assert s.row(s.cids[0]).state == "withdrawn"


def test_landing_conflict_withdraws(live_setup):
    s = live_setup(queued=2, files=[{"src/evowiki/deep/mathx.py": "def add(a, b):\n    return a + b\n"},
                                    {"src/evowiki/deep/mathx.py": "def add(a, b):\n    return b + a\n"}])
    r = s.step()
    assert r.did == "landed" and r.candidate_ids == ("fc_0",)
    p = s.row("fc_1")
    assert p.state == "withdrawn" and p.reason == "和当前代码冲突，已撤下"
    assert s.live.is_clean()


def test_revert_single(live_setup):
    s = live_setup(queued=2)
    pre_land = s.live.head()
    s.step()
    s.set_state("fc_0", "revert_queued")
    r = s.step()
    assert r.did == "reverted" and r.candidate_ids == ("fc_0",)
    p = s.row("fc_0")
    assert p.state == "reverted" and p.revert_commit == s.live.head()
    assert not (s.live.root / "src/evowiki/deep/new0.py").exists()
    assert (s.live.root / "src/evowiki/deep/new1.py").exists()
    assert s.row("fc_1").state == "landed"
    s.set_state("fc_1", "revert_queued")
    s.step()
    assert s.live.run("diff", pre_land, "HEAD", "--stat").strip() == ""   # tree == pre-land
    assert s.backend.restarts == 3


def test_revert_all(live_setup):
    s = live_setup(queued=2)
    s.step()
    n = len(s.live.commits_since("master"))
    with s.pool.connection() as conn:
        ps.set_live_request(conn, "revert_all", by="u1", now=T)
    r = s.step()
    assert r.did == "reverted" and set(r.candidate_ids) == set(s.cids)
    assert len(s.live.commits_since("master")) == n + 1                 # one combined commit
    assert s.live.run("diff", "master", "HEAD", "--stat").strip() == ""  # tree == master tree
    for cid in s.cids:
        p = s.row(cid)
        assert p.state == "reverted" and p.revert_commit == s.live.head()
    assert "撤回全部代码改动" in s.live.run("log", "-1", "--format=%B")
    # nothing left to revert: a second request changes nothing
    with s.pool.connection() as conn:
        ps.set_live_request(conn, "revert_all", by="u1", now=T)
    head = s.live.head()
    s.step()
    assert s.live.head() == head


def test_rebase_withdraws_conflicting(live_setup):
    s = live_setup(queued=2, files=[{"src/evowiki/deep/mathx.py": "def add(a, b):\n    return a + b\n"},
                                    {"src/evowiki/deep/other.py": "Z = 3\n"}])
    s.step()
    old_live = {cid: s.row(cid).live_commit for cid in s.cids}
    # master moves and edits the same line as fc_0
    (s.main.root / "src/evowiki/deep/mathx.py").write_text("def add(a, b):\n    return b + a\n",
                                                           encoding="utf-8")
    s.main.add_all()
    s.main.commit("master fix", author="Dev <dev@x>")
    r = s.step()
    assert r.did == "rebased"
    assert s.live.merge_base("HEAD", "master") == s.main.rev_parse("master")
    p0, p1 = s.row("fc_0"), s.row("fc_1")
    assert p0.state == "withdrawn" and p0.reason == "主线更新后和这次改动冲突，已撤下"
    assert p1.state == "landed" and p1.live_commit != old_live["fc_1"]
    assert p1.live_commit == s.live.head()
    assert "withdrawn" in s.events() and "rebase" in s.events()
    assert s.backend.restarts == 2


def test_rebase_drops_reverted_pairs(live_setup):
    s = live_setup(queued=2)
    s.step()
    s.set_state("fc_0", "revert_queued")
    s.step()
    (s.main.root / "README.md").write_text("hi\n", encoding="utf-8")
    s.main.add_all()
    s.main.commit("docs", author="Dev <dev@x>")
    s.step()
    msgs = [m for _, m in s.live.commits_since("master")]
    assert len(msgs) == 1 and live_mod.trailers(msgs[0], live_mod.TRAILER) == ("fc_1",)
    assert s.row("fc_0").state == "reverted"


def test_revert_that_breaks_start_is_restored(live_setup):
    s = live_setup(queued=1, boots=["ok", "down", "ok"])
    cid = s.cids[0]
    s.step()
    landed_tree = s.live.head()
    s.set_state(cid, "revert_queued")
    r = s.step()
    assert r.did == "canary_failed"
    assert s.live.run("diff", landed_tree, "HEAD", "--stat").strip() == ""
    p = s.row(cid)
    assert p.state == "landed" and p.live_commit == s.live.head() and "已恢复" in p.reason
    # the re-applied change is a forge commit again: a later revert-all still finds it
    eff, _ = live_mod._effective(s.live, "master")
    assert [c for c, _ in eff] == [cid]


def test_unexpected_git_error_resets_and_restores(live_setup, monkeypatch):
    s = live_setup(queued=1)
    before = s.live.head()

    def land_then_fail(t, live, queued):
        live.cherry_pick(queued[0].commit)
        t.write(queued[0].candidate_id, "landed", live_commit=live.head())
        raise live_mod.GitError("boom")

    monkeypatch.setattr(live_mod, "_land", land_then_fail)
    with pytest.raises(live_mod.GitError):
        s.step()
    assert s.live.head() == before
    assert s.row(s.cids[0]).state == "queued"
    assert s.backend.drains == [True, False] and s.backend.restarts == 0


def test_trailers_parse():
    msg = "x\n\nForge-Candidate: fc_a\nForge-Revert: fc_b\nForge-Revert: fc_c\n(cherry picked from commit 1)"
    assert live_mod.trailers(msg, live_mod.TRAILER) == ("fc_a",)
    assert live_mod.trailers(msg, live_mod.REVERT_TRAILER) == ("fc_b", "fc_c")


def test_landings_off_keeps_queue_but_still_reverts(live_setup):
    """Kill switch / paused supervisor (plan P3 T11): queued landings stay queued, reverts still run."""
    s = live_setup(queued=2)
    first, second = s.cids
    s.set_state(second, "built")
    assert s.step().did == "landed"
    s.set_state(second, "queued")

    def step_off():
        return live_mod.step(s.pool, live=s.live, main=s.main, backend=s.backend, now=lambda: T,
                             sleep=s.backend.sleep, clock=s.backend.clock, landings=False)

    assert step_off().did == "nothing" and s.row(second).state == "queued"
    s.set_state(first, "revert_queued")
    r = step_off()
    assert r.did == "reverted" and r.candidate_ids == (first,)
    assert s.row(second).state == "queued"
    assert not (s.live.root / "src/evowiki/deep/new0.py").exists()
    assert not (s.live.root / "src/evowiki/deep/new1.py").exists()
