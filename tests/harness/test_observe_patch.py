"""Watching landed code patches a harness round evaluated (harness 3b, Task 10): the after side
starts at the forge's `land` event, points go to `harness_patch_observation`, two harmful points
in a row ask the forge to revert the patch (`revert_queued`) with a harmful lesson, a refused
revert is a ROLLBACK_REFUSED point. Real Postgres with real forge rows; fake signals."""

from datetime import timedelta

import pytest

from evowiki.forge.patch import land as forge_land
from evowiki.harness.change_source import ROLLBACK_REFUSED
from evowiki.harness.observe import ObserveDeps, observe_changes
from evowiki.harness.round import observation_row
from evowiki.store import forge as fstore
from evowiki.store import forge_patch as ps
from evowiki.store import harness_rsi as hr
from evowiki.teach.layer2.outcomes import NOTE_AUTO_ROLLBACK
from evowiki.teach.spec_registry import MemoryRegistry
from tests.forge.patch.test_land import seed
from tests.harness.test_observe import APPLIED, T0, Signals, U
from tests.runtime.helpers import make_user

CID = "fc_obs1"
LANDED = APPLIED


def _setup(pool, *, subject=f"patch:{CID}", gate_user=U, land_event=True):
    make_user(pool, U, now=T0)
    with pool.connection() as conn:
        seed(conn, CID, state="landed", status="promoted", title="补读前置概念")
        rid = hr.start_round(conn, gate_user, finding="讲得太浅：页一", now=APPLIED - timedelta(days=1))
        hr.finish_round(conn, rid, "no_change", reason="不改",
                        cases={"problem": [{"case_id": "c1", "signal_ids": ["trig"]}],
                               "anchors": [], "unusable": [], "finding": "讲得太浅：页一"},
                        decision={"improver": "none"}, now=APPLIED - timedelta(days=1))
        ref = hr.record_gate(conn, gate_user, rid, subject, "pass", [], {},
                             now=APPLIED - timedelta(days=1))
        ps.set_state(conn, CID, "landed", now=LANDED, gate_ref=ref, live_commit="l1")
        if land_event:
            ps.log_event(conn, "land", candidate_id=CID, detail={"commit": "l1"}, now=LANDED)
    sig = Signals()
    sig.items.append(_trigger())
    return sig


def _trigger():
    from evowiki.teach.contracts import Signal
    return Signal(signal_id="trig", kind="page_feedback", at=LANDED - timedelta(hours=1),
                  page_id="p1", chips=("too_shallow",))


def deps(pool, sig):
    return ObserveDeps(pool=pool, registry=MemoryRegistry(), signals=sig)


def _points(pool):
    with pool.connection() as conn:
        return hr.patch_observations(conn, U, CID)


def _lessons(pool):
    with pool.connection() as conn:
        return hr.experience(conn, U, query=None, limit=50, now=APPLIED + timedelta(days=30))


def _state(pool):
    with pool.connection() as conn:
        return ps.get_patch(conn, CID).state


def _harmful(sig):
    sig.add("check_answer", 20, T0, outcome=1.0)                        # no 看不懂 before
    sig.add("check_unclear", 10, LANDED + timedelta(hours=1))           # all 看不懂 after


def test_no_land_event_is_not_watched(pool):
    sig = _setup(pool, land_event=False)
    _harmful(sig)
    assert observe_changes(U, deps(pool, sig), now=APPLIED + timedelta(days=2)) == []
    assert _points(pool) == []


@pytest.mark.parametrize("subject,gate_user", [("patch:fc_other", U), (f"patch:{CID}", "u2")])
def test_a_patch_without_this_users_harness_gate_is_not_watched(pool, subject, gate_user):
    sig = _setup(pool, subject=subject, gate_user=gate_user)
    _harmful(sig)
    assert observe_changes(U, deps(pool, sig), now=APPLIED + timedelta(days=2)) == []


def test_harmful_twice_queues_the_revert_with_a_harmful_lesson(pool):
    sig = _setup(pool)
    _harmful(sig)
    now = APPLIED + timedelta(days=2)
    (res,) = observe_changes(U, deps(pool, sig), now=now)
    assert (res.kind, res.change_id, res.verdict, res.rolled_back) == ("patch", CID, "harmful",
                                                                      False)
    assert res.deltas["unclear_rate"] == pytest.approx(-1.0)      # the trigger stays before
    assert _state(pool) == "landed" and _lessons(pool) == []
    assert observation_row(res)["change_id"] == f"forgep:{CID}"
    sig.add("check_unclear", 10, LANDED + timedelta(days=1))
    (res,) = observe_changes(U, deps(pool, sig), now=now + timedelta(days=1))
    assert res.verdict == "harmful" and res.rolled_back
    assert _state(pool) == "revert_queued"
    with pool.connection() as conn:
        assert fstore.get_candidate(conn, CID).status == "promoted"
        events = [e for e in ps.events(conn) if e["candidate_id"] == CID]
    assert events[0]["kind"] == "revert_requested" and events[0]["detail"]["by"] == "observe"
    (lesson,) = _lessons(pool)
    assert lesson.verdict == "harmful" and lesson.evidence["candidate_id"] == CID
    assert lesson.change == {"patch": {"candidate_id": CID, "title": "补读前置概念",
                                       "commit": "c"}}
    assert lesson.situation["signal_kinds"] == ["page_feedback"]
    pts = _points(pool)
    assert [p.note for p in pts] == ["", "", NOTE_AUTO_ROLLBACK]
    assert pts[-1].lesson_id is not None
    # a reverted (revert_queued) patch is no longer watched
    assert observe_changes(U, deps(pool, sig), now=now + timedelta(days=3)) == []


def test_a_refused_revert_is_a_rollback_refused_point(pool, monkeypatch):
    sig = _setup(pool)
    _harmful(sig)

    def refuse(conn, user_id, cid, *, by, now):
        raise ValueError("这次改动已经撤销过了")
    monkeypatch.setattr(forge_land, "revert_patch", refuse)
    now = APPLIED + timedelta(days=2)
    observe_changes(U, deps(pool, sig), now=now)
    sig.add("check_unclear", 10, LANDED + timedelta(days=1))
    (res,) = observe_changes(U, deps(pool, sig), now=now + timedelta(days=1))
    assert res.verdict == "harmful" and not res.rolled_back and res.lesson == "harmful"
    assert _state(pool) == "landed"
    pts = _points(pool)
    assert pts[-1].note == ROLLBACK_REFUSED and pts[-1].reason == "这次改动已经撤销过了"
    assert len(_lessons(pool)) == 1
    # not retried every cycle: no new evidence, no new attempt
    (res,) = observe_changes(U, deps(pool, sig), now=now + timedelta(days=2))
    assert not res.rolled_back and len(_points(pool)) == len(pts)


def test_harness_patch_observation_is_append_only(pool):
    sig = _setup(pool)
    _harmful(sig)
    observe_changes(U, deps(pool, sig), now=APPLIED + timedelta(days=2))
    import psycopg
    with pool.connection() as conn, pytest.raises(psycopg.Error):
        conn.execute("update harness_patch_observation set note = 'x'")


# ---- one timeline: windows end at the next watched event of either kind ----------------------

E1 = APPLIED
E2 = APPLIED + timedelta(days=3)
H4 = "H4.deep.max_tool_calls"


def _gated_patch(conn, cid, at):
    seed(conn, cid, state="landed", status="promoted", title="补读前置概念")
    rid = hr.start_round(conn, U, finding="讲得太浅：页一", now=at - timedelta(days=1))
    hr.finish_round(conn, rid, "no_change", reason="不改",
                    cases={"problem": [{"case_id": "c1", "signal_ids": []}], "anchors": [],
                           "unusable": [], "finding": "讲得太浅：页一"},
                    decision={"improver": "none"}, now=at - timedelta(days=1))
    ref = hr.record_gate(conn, U, rid, f"patch:{cid}", "pass", [], {}, now=at - timedelta(days=1))
    ps.set_state(conn, cid, "landed", now=at, gate_ref=ref, live_commit="l1")
    ps.log_event(conn, "land", candidate_id=cid, detail={"commit": "l1"}, now=at)


def _change(conn, at):
    from evowiki.harness.diff import KeyOp, PackDiff, diff_to_json
    from evowiki.store import harness as pack_store
    if pack_store.load_pack(conn, U).values.get(H4) is None:
        pack_store.set_value(conn, U, H4, 80, source="user", why="起点", now=T0)
    rid = hr.start_round(conn, U, finding="讲得太浅：页一", now=at)
    hr.finish_round(conn, rid, "applied", reason="关卡通过",
                    cases={"problem": [{"case_id": "c1", "signal_ids": []}], "anchors": [],
                           "unusable": [], "finding": "讲得太浅：页一"},
                    decision={"improver": "a"}, now=at)
    v = pack_store.set_value(conn, U, H4, 100, source="improver", why="r", now=at)
    stored = diff_to_json(PackDiff((KeyOp(H4, 100),)), {H4: 80}, None)
    return hr.record_change(conn, U, rid, stored, "applied", version=v, now=at)


def _timeline(pool, first, second):
    """Two watched events at E1 and E2 (`first` / `second`: "patch" or "change"); signals: 看不懂
    before E1, understood between E1 and E2, 看不懂 again after E2. Returns the two ids."""
    make_user(pool, U, now=T0)
    ids = []
    with pool.connection() as conn:
        for n, (kind, at) in enumerate(((first, E1), (second, E2))):
            if kind == "patch":
                cid = f"fc_t{n}"
                _gated_patch(conn, cid, at)
                ids.append(cid)
            else:
                ids.append(_change(conn, at))
    sig = Signals()
    sig.add("check_unclear", 20, T0)
    sig.add("check_answer", 20, E1 + timedelta(hours=1), outcome=1.0)
    sig.add("check_unclear", 10, E2 + timedelta(hours=1))
    return ids, sig


def _by_id(results):
    return {r.change_id: r for r in results}


@pytest.mark.parametrize("first,second", [("patch", "patch"), ("change", "patch"),
                                          ("patch", "change")])
def test_harm_after_the_second_event_is_never_charged_to_the_first(pool, first, second):
    (a, b), sig = _timeline(pool, first, second)
    d = deps(pool, sig)
    now = E2 + timedelta(days=1)
    res = _by_id(observe_changes(U, d, now=now))
    assert res[a].verdict == "helpful" and res[a].kind == first
    assert res[b].verdict == "harmful" and res[b].kind == second
    sig.add("check_unclear", 10, E2 + timedelta(hours=12))
    res = _by_id(observe_changes(U, d, now=now + timedelta(days=1)))
    assert res[b].rolled_back and not res[a].rolled_back
    with pool.connection() as conn:
        if first == "patch":
            assert ps.get_patch(conn, a).state == "landed"
        else:
            assert hr.get_change(conn, U, a).status == "applied"
        if second == "patch":
            assert ps.get_patch(conn, b).state == "revert_queued"
        else:
            assert hr.get_change(conn, U, b).status == "rolled_back"
    lessons = {(les.verdict, les.evidence.get("candidate_id") or les.evidence.get("change_id"))
               for les in _lessons(pool)}
    assert lessons == {("effective", a), ("harmful", b)}


def test_a_helpful_patch_writes_one_effective_lesson(pool):
    sig = _setup(pool)
    sig.add("check_unclear", 20, T0)
    sig.add("check_answer", 20, LANDED + timedelta(hours=1), outcome=1.0)
    (res,) = observe_changes(U, deps(pool, sig), now=LANDED + timedelta(days=2))
    assert res.verdict == "helpful" and res.lesson == "effective"
    (lesson,) = _lessons(pool)
    assert lesson.verdict == "effective" and lesson.evidence["candidate_id"] == CID
    assert lesson.change["patch"]["candidate_id"] == CID and _points(pool)[0].lesson_id
    assert _state(pool) == "landed"


def test_a_vanished_patch_refusal_note_is_plain_chinese(pool, monkeypatch):
    from evowiki.forge.models import ForgeNotFound
    from evowiki.harness.observe import PATCH_GONE
    sig = _setup(pool)
    _harmful(sig)

    def gone(conn, user_id, cid, *, by, now):
        raise ForgeNotFound(f"no patch candidate {cid!r}")
    monkeypatch.setattr(forge_land, "revert_patch", gone)
    now = APPLIED + timedelta(days=2)
    observe_changes(U, deps(pool, sig), now=now)
    sig.add("check_unclear", 10, LANDED + timedelta(days=1))
    (res,) = observe_changes(U, deps(pool, sig), now=now + timedelta(days=1))
    assert PATCH_GONE in res.note and "candidate" not in res.note
    assert _points(pool)[-1].reason == PATCH_GONE
