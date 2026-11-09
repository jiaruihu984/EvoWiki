"""Faster, faithful replays (2026-10-28): the improver's probe replays and the round's smoke replays
run at once and are recorded in order (`replay.run_all`); a page replay writes with its traced
run's model, never reaches the network for images, and its trace names the candidate it ran.
Fakes and scripted providers only."""

import contextvars
import threading
from dataclasses import replace

import pytest

from evowiki.harness import improver as improver_mod
from evowiki.harness import replay as replay_mod
from evowiki.harness.diff import PackDiff
from evowiki.harness.improver import run_improver
from evowiki.harness.pack import EMPTY
from evowiki.harness.replay import (
    MODEL_MIXED,
    MODEL_UNKNOWN,
    ReplayDeps,
    ReplayResult,
    candidate_ref,
    replay,
    run_all,
)
from evowiki.llm.models import ModelRef
from evowiki.store import trace
from evowiki.wikiapp.refine_service import MODEL_GONE, NoImages, pinned_writer
from tests.deep.fakes import DeepScript, simple_round
from tests.harness.test_improver import CASES, Improver, deps, mturn, propose_both, tc
from tests.harness.test_replay import World, candidate, candidate_round, log_writer_models
from tests.runtime.helpers import make_user

TIMEOUT = 10


# --- run_all ----------------------------------------------------------------------------------

def test_run_all_runs_at_once_and_returns_in_order():
    gate = threading.Barrier(3, timeout=TIMEOUT)     # passes only when all three run together
    var = contextvars.ContextVar("v", default="unset")
    var.set("caller")

    def job(i: int):
        def run():
            gate.wait()
            if i == 1:
                raise ValueError("boom")
            return (i, var.get())
        return run
    out = run_all([job(0), job(1), job(2)])
    assert out[0] == (0, "caller") and out[2] == (2, "caller")    # the caller's context
    assert isinstance(out[1], ValueError)                         # handed back, in place


def test_run_all_is_bounded():
    running, peak, lock = [0], [0], threading.Lock()
    release = threading.Event()

    def job():
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(0.2)
        with lock:
            running[0] -= 1
        return True
    assert run_all([job] * 7) == [True] * 7
    assert peak[0] <= replay_mod.REPLAY_WORKERS


# --- the improver's probes --------------------------------------------------------------------

class TogetherWriter:
    """Stands in for `harness.replay.replay`: the replays of one wave must run at once (a barrier
    of `wave` parties), and each text names its case and pack."""

    def __init__(self, wave: int):
        self.gate = threading.Barrier(wave, timeout=TIMEOUT)
        self.calls: list[tuple[str, bool]] = []
        self.lock = threading.Lock()

    def __call__(self, case, candidate, *, base_pack, base_spec, deps):
        self.gate.wait()
        cand = bool(candidate.ops or candidate.spec_ops)
        with self.lock:
            self.calls.append((case.case_id, cand))
        mark = "候选" if cand else "当前"
        return ReplayResult(case_id=case.case_id, text=f"回忆线索：{mark}\n\n{case.case_id} {mark}",
                            writer_provider="anthropic",
                            run_id=f"harness-replay:{case.case_id}:{int(cand)}", cost=0.01)


def test_a_probe_runs_its_references_then_its_candidates_at_once(pool, monkeypatch):
    make_user(pool, "u1")
    monkeypatch.setattr(improver_mod, "MAX_PROBES", 9)
    w = TogetherWriter(wave=3)
    monkeypatch.setattr(improver_mod, "replay", w)
    ids = ["case:run-1:s2", "case:run-2:s2", "case:run-3:s2"]
    imp = Improver(propose_both(), mturn(tc("probe", 1, candidate="general", case_ids=ids)),
                   mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[],
                            citations=["signal:sig1"], rationale="看看")))
    out = run_improver("u1", CASES, deps=deps(pool, imp))
    assert "已重放 6 次" in imp.results[2][0]
    # every reference before any candidate (two waves), recorded in the probe's order
    assert {c for c, cand in w.calls[:3]} == set(ids) and not any(c for _, c in w.calls[:3])
    assert [p.run_id for p in out.probes] == [
        f"harness-replay:{cid}:{k}" for cid in ids for k in (0, 1)]
    assert out.cost == pytest.approx(sum(c.cost or 0 for c in imp.log.calls()) + 6 * 0.01)


def test_a_candidate_is_not_replayed_when_its_reference_failed(pool, monkeypatch):
    from evowiki.wikiapp.refine_service import ReplayFailed
    make_user(pool, "u1")
    monkeypatch.setattr(improver_mod, "MAX_PROBES", 9)
    calls: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def flaky(case, candidate, **kw):
        cand = bool(candidate.ops or candidate.spec_ops)
        with lock:
            calls.append((case.case_id, cand))
        if case.case_id == "case:run-2:s2" and not cand:
            raise ReplayFailed("no_draft", "写不出来", cost=0.02, run_ids=("harness-replay:F:x",))
        return ReplayResult(case_id=case.case_id, text=f"{case.case_id} {cand}",
                            writer_provider="anthropic",
                            run_id=f"harness-replay:{case.case_id}:{int(cand)}", cost=0.01)
    monkeypatch.setattr(improver_mod, "replay", flaky)
    ids = ["case:run-1:s2", "case:run-2:s2"]
    imp = Improver(propose_both(), mturn(tc("probe", 1, candidate="general", case_ids=ids)),
                   mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[],
                            citations=["signal:sig1"], rationale="看看")))
    out = run_improver("u1", CASES, deps=deps(pool, imp))
    first = imp.results[2][0]
    assert ("case:run-2:s2", True) not in calls and len(calls) == 3
    assert "已重放 3 次" in first and "没写出这一段" in first
    assert out.cost == pytest.approx(sum(c.cost or 0 for c in imp.log.calls()) + 2 * 0.01 + 0.02)


# --- the round's smoke replays ----------------------------------------------------------------

def test_the_smoke_replays_run_at_once_and_are_recorded_in_case_order(pool):
    from tests.harness.test_round import Env, FakeReplay
    inner = FakeReplay(broken={"case:run-2:s2"})
    gate = threading.Barrier(2, timeout=TIMEOUT)      # the two smoke cases' candidates together
    seen: list[str] = []

    def together(case, candidate, **kw):
        if candidate.ops or candidate.spec_ops:
            seen.append(case.case_id)
            gate.wait()
        return inner(case, candidate, **kw)
    env = Env(pool, replay=together)
    r = env.run()
    assert r.status == "failed" and "新设置写不出 1 段" in r.reason
    (row,) = env.rounds()
    assert sorted(seen) == ["case:run-1:s2", "case:run-2:s2"]
    assert [b["id"] for b in row.decision["gate"]["broken"]] == ["case:run-2:s2"]


# --- the writer's model -----------------------------------------------------------------------

def _models(*refs: str):
    return lambda run_id: tuple(ModelRef(provider=r.split(":")[0], model=r.split(":")[1])
                                for r in refs)


def test_a_page_replay_writes_with_the_traced_runs_model_and_names_its_candidate(pool):
    world = World(pool)
    ds = DeepScript().plan().write(*simple_round()).plan().write(*candidate_round(2))
    case = world.recorded(ds)
    svc = world.service(ds)
    writer_models = log_writer_models(pool, svc.llm.log)
    (want,) = writer_models(case.run_id)
    assert f"{want.provider}:{want.model}" == svc.llm.model_for("u1")
    res = replay(case, candidate(), base_pack=EMPTY,
                 base_spec=world.spec(case),
                 deps=ReplayDeps(deep=svc, repo=world.repo, writer_models=writer_models))
    with pool.connection() as conn:
        job = trace.run(conn, res.run_id)["input"]["job"]
    assert job["candidate"] == candidate_ref(candidate()) and job["candidate"].startswith("cand-")
    assert candidate_ref(PackDiff()) is None
    assert candidate_ref(candidate()) != candidate_ref(PackDiff(ops=candidate().ops))


@pytest.mark.parametrize("models, why", [((), MODEL_UNKNOWN),
                                         (("anthropic:a", "deepseek:b"), MODEL_MIXED),
                                         (("openai:gone",), MODEL_GONE)])
def test_a_case_whose_writer_cannot_be_pinned_is_a_plain_lookup_error(pool, models, why):
    world = World(pool)
    ds = DeepScript().plan().write(*simple_round())
    case = world.recorded(ds)
    with pytest.raises(LookupError, match=why):
        replay(case, PackDiff(), base_pack=EMPTY,
               base_spec=world.spec(case),
               deps=ReplayDeps(deep=world.service(ds), repo=world.repo,
                               writer_models=_models(*models)))


def test_pinned_writer_picks_the_config_that_writes_with_the_model():
    from evowiki.llm.models import GatewayConfig
    ds = DeepScript()
    llm = ds.llm()
    g = llm.config.generator
    other = GatewayConfig(generator=ModelRef(provider="openai", model="o-x"),
                          verifier=llm.config.verifier)
    llm = replace(llm, writers={"o": other}, fallback=other)
    same = pinned_writer(llm, "u1", g)
    assert same.fallback is None and same.model_for("u1") == f"{g.provider}:{g.model}"
    ds_pin = pinned_writer(llm, "u1", other.generator)
    assert ds_pin.model_for("u1") == "openai:o-x" and ds_pin.fallback is None
    with pytest.raises(LookupError, match=MODEL_GONE):
        pinned_writer(llm, "u1", ModelRef(provider="openai", model="gone"))


def test_replays_search_no_images():
    from evowiki.deep.images import ImageRejected
    imgs = NoImages()
    assert imgs.search("DNA 双螺旋", 3) == []
    with pytest.raises(ImageRejected):
        imgs.fetch(None)  # type: ignore[arg-type]


def test_a_page_replays_writer_gets_no_network_images(pool, monkeypatch):
    import evowiki.wikiapp.refine_service as rs
    from evowiki.deep.agent import RefineFailed
    world = World(pool)
    ds = DeepScript().plan().write(*simple_round())
    case = world.recorded(ds)
    seen: list = []

    def capture(*args, images=None, **kw):
        seen.append(images)
        raise RefineFailed("no_draft", "stop here")
    monkeypatch.setattr(rs, "refine", capture)
    svc = replace(world.service(ds), images=object())      # a live search: must not be handed on
    with pytest.raises(rs.ReplayFailed):
        replay(case, PackDiff(), base_pack=EMPTY, base_spec=world.spec(case),
               deps=ReplayDeps(deep=svc, repo=world.repo))
    assert len(seen) == 1 and isinstance(seen[0], NoImages)
