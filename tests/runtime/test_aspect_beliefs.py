from datetime import timedelta

import pytest

from evowiki.belief.aspects import (
    AddNote,
    AspectConfig,
    AspectK,
    AspectRejected,
    Declare,
    DefineConcept,
    Observe,
    RevokeInference,
)
from evowiki.belief.theta import ThetaInfer
from evowiki.goals.contracts import Aspect, Band, Observation
from evowiki.runtime.aspect_beliefs import (
    load_aspects,
    read_kview,
    record_aspect_events,
    replay_aspects,
)
from evowiki.store.aspects import AspectEventConflict, aspect_events
from evowiki.store.locks import user_writer
from tests.runtime.helpers import T0, make_user

CFG = AspectConfig.from_theta(ThetaInfer.default())


def o(eid, cell, pol, strength=1.0, kind="quiz", dt=0, quote=None):
    return Observe(event_id=eid, ts=T0 + timedelta(days=dt), observation=Observation(
        cell=cell, polarity=pol, strength=strength, kind=kind, evidence_ref=f"ev-{eid}",
        quote=quote))


SCRIPT = [
    DefineConcept(event_id="d1", ts=T0, concept_id="mean", aspects=tuple(Aspect)),
    DefineConcept(event_id="d2", ts=T0, concept_id="var", aspects=tuple(Aspect),
                  prereqs=("mean",)),
    DefineConcept(event_id="d3", ts=T0, concept_id="z",
                  aspects=(Aspect.TERM, Aspect.NOTATION, Aspect.PROCEDURE, Aspect.INTERPRET),
                  prereqs=("var",)),
    o("o1", "z.procedure", "+"),
    o("o2", "var.procedure", "~", dt=1),
    o("o3", "var.mechanism", "+", 0.9, "self", dt=2, quote="我会推导方差"),
    Declare(event_id="x1", ts=T0, cell="mean.mechanism", value="dont"),
    AddNote(event_id="n1", ts=T0, cell="var.procedure", text="忘了除以 n−1", evidence_ref="ev-o2"),
    o("o4", "z.interpret", "-", 0.4, "feedback", dt=3),
    RevokeInference(event_id="r1", ts=T0, source_event_id="o1"),
    o("o5", "z.procedure", "+", 0.6, "ask", dt=40),
]


def in_memory() -> AspectK:
    k = AspectK(CFG)
    for e in SCRIPT:
        k.apply(e)
    return k


def test_replay_equals_in_memory_run(pool):
    make_user(pool, "u1")
    for i in range(0, len(SCRIPT), 3):          # several transactions
        with user_writer(pool, "u1") as tx:
            record_aspect_events(tx, CFG, SCRIPT[i:i + 3])
    with pool.connection() as conn:
        assert load_aspects(conn, "u1", CFG).to_json() == in_memory().to_json()
        view = read_kview(conn, "u1", CFG, T0 + timedelta(days=41))
    mem = in_memory()
    for c in ("mean", "var", "z"):
        for a in mem.aspects(c):
            assert view.cell(f"{c}.{a.value}") == mem.belief(f"{c}.{a.value}",
                                                             T0 + timedelta(days=41))
    assert view.note("var")[0].evidence_ref == "ev-o2"
    assert view.cell("mean.mechanism").band is Band.GAP


def test_replay_aspects_is_pure_fold():
    assert replay_aspects(CFG, SCRIPT).to_json() == in_memory().to_json()


def test_load_upto_seq_replays_prefix(pool):
    make_user(pool, "u1")
    with user_writer(pool, "u1") as tx:
        record_aspect_events(tx, CFG, SCRIPT)
    with pool.connection() as conn:
        assert (load_aspects(conn, "u1", CFG, upto_seq=4).to_json()
                == replay_aspects(CFG, SCRIPT[:4]).to_json())


def _ids(pool):
    with pool.connection() as conn:
        return [s.event.event_id for s in aspect_events(conn, "u1")]


def test_rejected_batch_appends_nothing(pool):
    make_user(pool, "u1")
    with user_writer(pool, "u1") as tx:
        record_aspect_events(tx, CFG, SCRIPT[:3])
    bad = [o("ok", "z.term", "+"), o("bad", "z.mechanism", "+")]   # z has no mechanism aspect
    # pytest.raises is the INNER context: it swallows the error, so the transaction commits.
    with user_writer(pool, "u1") as tx, pytest.raises(AspectRejected):
        record_aspect_events(tx, CFG, bad)
    assert _ids(pool) == ["d1", "d2", "d3"]


def test_conflicting_stored_event_in_mixed_batch_appends_nothing(pool):
    make_user(pool, "u1")
    with user_writer(pool, "u1") as tx:
        record_aspect_events(tx, CFG, SCRIPT[:3])
    clash = SCRIPT[2].model_copy(update={"difficulty": 5})          # stored id, other content
    batch = [o("new1", "z.term", "+"), clash, o("new2", "z.procedure", "+")]
    # inner pytest.raises swallows the error, so the transaction commits
    with user_writer(pool, "u1") as tx, pytest.raises(AspectEventConflict):
        record_aspect_events(tx, CFG, batch)
    assert _ids(pool) == ["d1", "d2", "d3"]


def test_duplicate_inside_one_batch_is_rejected_and_appends_nothing(pool):
    make_user(pool, "u1")
    with user_writer(pool, "u1") as tx:
        record_aspect_events(tx, CFG, SCRIPT[:3])
    dup = o("o1", "z.procedure", "+")
    # inner pytest.raises swallows the error, so the transaction commits
    with user_writer(pool, "u1") as tx, pytest.raises(AspectRejected):
        record_aspect_events(tx, CFG, [dup, o("o9", "z.term", "+"), dup])
    assert _ids(pool) == ["d1", "d2", "d3"]


def test_retry_of_stored_events_is_idempotent(pool):
    make_user(pool, "u1")
    with user_writer(pool, "u1") as tx:
        first = record_aspect_events(tx, CFG, SCRIPT[:4])
    with user_writer(pool, "u1") as tx:
        again = record_aspect_events(tx, CFG, SCRIPT[:4])
    assert again.to_json() == first.to_json()
    with pool.connection() as conn:
        assert len(aspect_events(conn, "u1")) == 4


def test_retry_mixed_with_new_events(pool):
    make_user(pool, "u1")
    with user_writer(pool, "u1") as tx:
        record_aspect_events(tx, CFG, SCRIPT[:4])
    with user_writer(pool, "u1") as tx:
        k = record_aspect_events(tx, CFG, SCRIPT[2:6])
    assert k.to_json() == replay_aspects(CFG, SCRIPT[:6]).to_json()
    with pool.connection() as conn:
        assert [s.event.event_id for s in aspect_events(conn, "u1")] == [
            e.event_id for e in SCRIPT[:6]]
