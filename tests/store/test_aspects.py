from datetime import timedelta, timezone

import psycopg
import pytest

from evowiki.belief.aspects import (
    AddNote,
    AspectConfig,
    Declare,
    DefineConcept,
    Observe,
    RetireNote,
    RevokeInference,
    note_id,
)
from evowiki.belief.theta import ThetaInfer
from evowiki.goals.contracts import Aspect, Observation
from evowiki.runtime.aspect_beliefs import replay_aspects
from evowiki.store.aspects import (
    AspectEventConflict,
    append_aspect_event,
    aspect_events,
    last_aspect_seq,
)
from evowiki.store.locks import user_writer
from tests.runtime.helpers import T0, make_user

CST = timezone(timedelta(hours=8))


def _ev():
    return [DefineConcept(event_id="u1-d", ts=T0, concept_id="z", aspects=(Aspect.TERM,)),
            Observe(event_id="u1-o", ts=T0, observation=Observation(
                cell="z.term", polarity="~", strength=0.7, kind="self", evidence_ref="e1",
                quote="大概懂"))]


def _all_types():
    """One of each event type, forming a log that replays cleanly (the store must not be tested
    on a log the runtime could never have written)."""
    return [
        DefineConcept(event_id="a-d", ts=T0, concept_id="z",
                      aspects=(Aspect.TERM, Aspect.PROCEDURE), prereqs=("y",), difficulty=4),
        Observe(event_id="a-o", ts=(T0 + timedelta(minutes=1)).astimezone(CST),
                observation=Observation(cell="z.procedure", polarity="+", strength=0.9,
                                        kind="quiz", evidence_ref="e2")),
        RevokeInference(event_id="a-r", ts=T0 + timedelta(minutes=2), source_event_id="a-o"),
        Declare(event_id="a-k", ts=T0 + timedelta(minutes=3), cell="z.term", value="know"),
        Declare(event_id="a-n", ts=T0 + timedelta(minutes=4), cell="z.term", value=None),
        AddNote(event_id="a-a", ts=T0 + timedelta(minutes=5), cell="z.term",
                text="混淆了均值和中位数", evidence_ref="e3"),
        RetireNote(event_id="a-x", ts=T0 + timedelta(minutes=6), entry_id=note_id("a-a")),
    ]


def test_round_trip_and_order(pool):
    make_user(pool, "u1")
    with user_writer(pool, "u1") as tx:
        got = [append_aspect_event(tx, e) for e in _ev()]
    assert [(s.seq, c) for s, c in got] == [(1, True), (2, True)]
    assert [s.event for s, _ in got] == _ev()
    with pool.connection() as conn:
        stored = aspect_events(conn, "u1")
        assert [s.event for s in stored] == _ev()
        assert last_aspect_seq(conn, "u1") == 2 and last_aspect_seq(conn, "nobody") == 0
        assert [s.seq for s in aspect_events(conn, "u1", upto_seq=1)] == [1]
        assert aspect_events(conn, "nobody") == []


def test_all_event_types_round_trip(pool):
    make_user(pool, "u1")
    evs = _all_types()
    assert evs[1].ts.utcoffset() == timedelta(0)  # normalised on construction
    with user_writer(pool, "u1") as tx:
        got = [append_aspect_event(tx, e) for e in evs]
    assert all(c for _, c in got)
    with pool.connection() as conn:
        stored = aspect_events(conn, "u1")
    assert [s.seq for s in stored] == list(range(1, len(evs) + 1))
    assert [s.event for s in stored] == evs
    assert [type(s.event) for s in stored] == [type(e) for e in evs]
    assert stored[4].event.value is None
    cfg = AspectConfig.from_theta(ThetaInfer.default())
    replayed = replay_aspects(cfg, [s.event for s in stored])
    assert replayed.to_json() == replay_aspects(cfg, evs).to_json()
    assert all(s.event.ts.tzinfo is not None and s.event.ts.utcoffset() == timedelta(0)
               for s in stored)


def test_idempotent_retry_and_conflict(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    with user_writer(pool, "u1") as tx:
        append_aspect_event(tx, _ev()[0])
        again, created = append_aspect_event(tx, _ev()[0])
    assert not created and again.seq == 1 and again.event == _ev()[0]
    with pytest.raises(AspectEventConflict), user_writer(pool, "u1") as tx:
        append_aspect_event(tx, _ev()[0].model_copy(update={"concept_id": "y"}))
    with pytest.raises(AspectEventConflict), user_writer(pool, "u1") as tx:
        append_aspect_event(tx, _ev()[0].model_copy(update={"ts": T0 + timedelta(seconds=1)}))
    with pytest.raises(AspectEventConflict), user_writer(pool, "u2") as tx:
        append_aspect_event(tx, _ev()[0])
    with pool.connection() as conn:
        assert last_aspect_seq(conn, "u1") == 1 and last_aspect_seq(conn, "u2") == 0


def test_log_is_append_only(pool, admin_pool):
    make_user(pool, "u1")
    with user_writer(pool, "u1") as tx:
        append_aspect_event(tx, _ev()[0])
    for sql, op in (("update aspect_event set type = 'declare'", "UPDATE"),
                    ("delete from aspect_event", "DELETE"), ("truncate aspect_event", "TRUNCATE")):
        with (pytest.raises(psycopg.errors.RaiseException,
                            match=f"append-only table aspect_event: {op}"),
              admin_pool.connection() as conn):
            conn.execute(sql)
