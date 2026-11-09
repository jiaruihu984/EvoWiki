from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from evowiki.forge import switch
from evowiki.forge.models import (
    BuildReport,
    Evidence,
    ForgeResult,
    PatchRequest,
    SandboxStats,
)
from evowiki.forge.patch.round import NoPatchGate, run_patch
from evowiki.store import forge as fstore
from evowiki.store import forge_patch as ps
from tests.forge.patch.test_land import seed

T = datetime(2026, 10, 23, 9, tzinfo=UTC)
REQ = PatchRequest(title="修好加法", goal="修好加法", failing_case="add(1,2)==3")
EV = Evidence(citations=("trace:r1",), note="答案总被判错")


def _report(ok):
    return BuildReport(ok=ok, tests_passed=1, tests_failed=0, examples_passed=0, examples_failed=0,
                       lint_ok=True, schema_ok=True, sandbox=SandboxStats(wall_s=0.1, exit="ok"),
                       reason="ok")


class GoodGate:
    def evaluate(self, conn, candidate_id, user_id):
        return "g1"

    def ok(self, ref):
        return ref == "g1"


class FailingGate(GoodGate):
    def ok(self, ref):
        return False


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE", raising=False)


def _run(pool, gate, ok=True):
    with pool.connection() as conn:
        seed(conn, "p1")
        switch.set_enabled(conn, None, "patch", True, T)
    calls = []

    def fake_propose(kind, spec, evidence, *, user_id, patch_deps):
        calls.append((kind, spec, user_id, patch_deps))
        return ForgeResult(candidate_id="p1", kind="patch", build=_report(ok))

    deps = SimpleNamespace(pool=pool)
    res = run_patch(REQ, EV, user_id="u1", deps=deps, gate=gate, now=T, propose_fn=fake_propose)
    assert calls[0][0] == "patch" and calls[0][3] is deps
    return res


def _state(pool):
    with pool.connection() as conn:
        return ps.get_patch(conn, "p1").state, fstore.get_candidate(conn, "p1").status


def test_no_patch_gate_leaves_built(pool):
    res = _run(pool, NoPatchGate())
    assert res.build.ok and _state(pool) == ("built", "built")


def test_failing_gate_leaves_built(pool):
    _run(pool, FailingGate())
    assert _state(pool) == ("built", "built")


def test_passing_gate_waits_for_the_users_approval(pool):
    _run(pool, GoodGate())                      # review mode: a gate never queues a landing
    assert _state(pool) == ("awaiting_approval", "promoted")


def test_failed_build_skips_gate(pool):
    _run(pool, GoodGate(), ok=False)
    assert _state(pool) == ("built", "built")
