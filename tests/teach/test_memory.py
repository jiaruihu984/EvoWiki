from datetime import UTC, datetime, timedelta

import pytest

from evowiki.teach.contracts import GateResult, PatchMemoryEntry
from evowiki.teach.memory import (
    HALF_LIFE_DAYS,
    bump,
    decayed,
    recursion_metrics,
    signature,
    similar,
)
from evowiki.teach.rsi_store import MemoryRecord, MemoryRsiStore

T0 = datetime(2026, 10, 1, tzinfo=UTC)
PATH = "evidence_weight.self"


def _rec(pid: str, *, status="rejected", path=PATH, direction=-1, frm=0.6, to=0.4, at=T0, outcomes=40,
         track="A", failure="overconfident", domain="d1", du=None, repeat=False, cx=1) -> MemoryRecord:
    entry = PatchMemoryEntry(patch_id=pid, track=track, signature=signature(failure, path, domain), path=path,
                             gate=GateResult(patch_id=pid, channel=track, passed=status != "gate_failed"),
                             status=status, delta_utility=du, at=at)
    return MemoryRecord(user_id="u", entry=entry, direction=direction, from_value=frm, to_value=to,
                        counts_at=at, repeat_of_rejected=repeat, outcomes_at_close=outcomes,
                        complexity=cx)


def test_constants():
    assert HALF_LIFE_DAYS == 14.0


def test_decay_halves_at_half_life():
    assert decayed(1.0, T0, T0 + timedelta(days=14)) == pytest.approx(0.5)
    assert decayed(2.0, T0, T0 + timedelta(days=28)) == pytest.approx(0.5)
    assert decayed(1.0, T0, T0) == 1.0
    assert decayed(1.0, T0, T0 - timedelta(days=3)) == 1.0     # clock skew never grows a count


def test_bump_decays_then_adds():
    r = bump(_rec("p"), harmful=1.0, now=T0)
    r = bump(r, helpful=1.0, now=T0 + timedelta(days=14))
    assert r.entry.online_harmful == pytest.approx(0.5) and r.entry.online_helpful == 1.0
    assert r.counts_at == T0 + timedelta(days=14)
    with pytest.raises(ValueError):
        bump(r, harmful=-1.0, now=T0)
    skewed = bump(r, harmful=1.0, now=T0)                      # earlier than counts_at: no rewind
    assert skewed.counts_at == T0 + timedelta(days=14)
    assert skewed.entry.online_harmful == pytest.approx(1.5)


def test_signature():
    assert signature("overconfident", PATH, "d1") == "overconfident|evidence_weight.self|d1"
    assert signature("overconfident", PATH, None) == "overconfident|evidence_weight.self|*"


def test_similar_orders_by_tier_then_newest():
    a = _rec("sig_old", at=T0)
    b = _rec("sig_new", at=T0 + timedelta(days=1))
    c = _rec("path", failure="underconfident", at=T0 + timedelta(days=2))
    d = _rec("fail", path="slip", at=T0 + timedelta(days=3))
    e = _rec("none", path="slip", failure="stale", at=T0 + timedelta(days=4))
    f = _rec("trackB", track="B", direction=0, frm=None, to=None)
    g = _rec("dom", domain="d2", at=T0 + timedelta(days=5))
    got = similar([a, b, c, d, e, f, g], track="A", failure="overconfident", path=PATH,
                  now=T0 + timedelta(days=9))
    ids = [r.entry.patch_id for r in got]
    # every same-signature (failure|path, any domain) record first, newest first; then same path; then failure
    assert ids == ["dom", "sig_new", "sig_old", "path", "fail"]
    assert [r.entry.patch_id for r in similar([a, b, c, d, e], track="A", failure="overconfident", path=PATH,
                                              now=T0 + timedelta(days=9), k=2)] == ["sig_new", "sig_old"]
    assert [r.entry.patch_id for r in similar([a, d], track="A", failure="overconfident", path=None,
                                              now=T0 + timedelta(days=9))] == ["fail", "sig_old"]
    assert similar([f], track="A", failure="overconfident", path=PATH, now=T0) == []


def test_recursion_metrics_exact():
    recs = [
        _rec("a1", status="personal_accepted", du=0.03),
        _rec("a2", status="personal_accepted", du=0.01, cx=3),
        _rec("rb", status="rolled_back", du=0.5),                # counted as accepted, ΔU not counted
        _rec("rj", status="rejected", du=None, repeat=True),
        _rec("gf", status="gate_failed", du=-0.02),
        _rec("b1", status="personal_accepted", du=9.0, track="B", direction=0, frm=None, to=None),
    ]
    m = recursion_metrics(recs, "A")
    assert (m.proposals, m.accepted) == (5, 3)
    assert m.proposal_efficiency == pytest.approx(0.04 / 5)
    assert m.complexity_efficiency == pytest.approx(0.04 / 4)
    assert m.repeat_rejected_rate == pytest.approx(1 / 5)
    assert recursion_metrics(recs).proposals == 6
    empty = recursion_metrics([], "A")
    assert (empty.proposals, empty.accepted) == (0, 0)
    assert empty.proposal_efficiency is None and empty.complexity_efficiency is None
    assert empty.repeat_rejected_rate is None


def test_memory_record_direction_checked_and_store_upserts():
    with pytest.raises(ValueError):
        _rec("p", direction=2)
    with pytest.raises(ValueError):
        _rec("p", cx=-1)
    assert _rec("p", cx=0).complexity == 0
    s = MemoryRsiStore()
    s.put_memory(_rec("p1"))
    s.put_memory(_rec("p2"))
    s.put_memory(bump(_rec("p1"), harmful=1.0, now=T0))
    m = s.memory("u")
    assert [r.entry.patch_id for r in m] == ["p1", "p2"] and m[0].entry.online_harmful == 1.0
    assert s.memory("x") == []
