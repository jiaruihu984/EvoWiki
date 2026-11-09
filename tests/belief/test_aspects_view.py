from datetime import UTC, datetime

import pytest

from evowiki.belief.aspects import (
    DONT_EVIDENCE,
    AddNote,
    AspectConfig,
    AspectK,
    AspectKView,
    AspectRejected,
    Declare,
    DefineConcept,
    Observe,
    RetireNote,
    note_id,
    render_note,
)
from evowiki.belief.theta import ThetaInfer
from evowiki.goals.contracts import Aspect, Band, KView, Observation

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def base() -> AspectK:
    k = AspectK(AspectConfig.from_theta(ThetaInfer.default()))
    k.apply(DefineConcept(event_id="d", ts=T0, concept_id="z",
                          aspects=(Aspect.TERM, Aspect.PROCEDURE, Aspect.MECHANISM)))
    return k


def test_declaration_is_stored_apart_from_evidence():
    k = base()
    k.apply(Declare(event_id="x", ts=T0, cell="z.mechanism", value="know"))
    b = k.belief("z.mechanism", T0)
    assert b.declared == "know" and b.band is Band.KNOWN
    assert b.direct == 0 and b.m == pytest.approx(0.3)
    k.apply(Observe(event_id="o", ts=T0, observation=Observation(
        cell="z.mechanism", polarity="-", strength=1.0, kind="quiz", evidence_ref="e")))
    b = k.belief("z.mechanism", T0)
    assert b.declared == "know" and b.band is Band.KNOWN and b.direct == pytest.approx(1.0)
    k.apply(Declare(event_id="y", ts=T0, cell="z.mechanism", value=None))
    assert k.belief("z.mechanism", T0).band is Band.GAP
    k.apply(Declare(event_id="w", ts=T0, cell="z.term", value="dont"))
    b = k.belief("z.term", T0)
    assert b.band is Band.GAP and b.declared == "dont" and b.direct == DONT_EVIDENCE
    with pytest.raises(AspectRejected):
        k.apply(Declare(event_id="q", ts=T0, cell="z.notation", value="know"))


def test_notes_cite_evidence_have_stable_ids_and_never_touch_numbers():
    k = base()
    before = k.belief("z.term", T0)
    k.apply(AddNote(event_id="n1", ts=T0, cell="z.term", text="把 σ 当成方差", evidence_ref="ev7"))
    k.apply(AddNote(event_id="n2", ts=T0, cell="z.procedure", text="喜欢先看例子",
                    evidence_ref="ev8"))
    assert k.belief("z.term", T0) == before
    notes = k.notes("z")
    assert [n.entry_id for n in notes] == [note_id("n1"), note_id("n2")]
    assert notes[0].evidence_ref == "ev7" and note_id("n1").startswith("m_")
    k.apply(RetireNote(event_id="r", ts=T0, entry_id=note_id("n1")))
    assert [n.entry_id for n in k.notes("z")] == [note_id("n2")]
    with pytest.raises(AspectRejected):
        k.apply(RetireNote(event_id="r2", ts=T0, entry_id=note_id("n1")))
    with pytest.raises(AspectRejected):
        k.apply(AddNote(event_id="n3", ts=T0, cell="z.notation", text="x", evidence_ref="e"))


def test_render_note_format():
    k = base()
    k.apply(AddNote(event_id="n1", ts=T0, cell="z.term", text="把 σ 当成方差", evidence_ref="ev7"))
    out = render_note(k.notes("z"))
    nid = note_id("n1")
    assert out == f"- 把 σ 当成方差 <!--{nid}-->[^1]\n\n[^1]: z.term · ev7\n"
    assert render_note(()) == ""


def test_concept_summary_counts_bands():
    k = base()
    k.apply(Observe(event_id="o", ts=T0, observation=Observation(
        cell="z.term", polarity="+", strength=1.0, kind="quiz", evidence_ref="e")))
    s = k.concept_summary("z", T0)
    assert s.bands[Band.LIKELY] == 1 and s.bands[Band.NONE] == 2 and s.bands[Band.GAP] == 0
    assert s.mean_m == pytest.approx((1.6 / 3 + 0.3 + 0.3) / 3)


def test_view_implements_kview():
    k = base()
    v: KView = AspectKView(k, T0)
    assert v.aspects("z") == (Aspect.TERM, Aspect.PROCEDURE, Aspect.MECHANISM)
    assert set(v.cells(("z",))) == {"z.term", "z.procedure", "z.mechanism"}
    assert v.cell("z.term").band is Band.NONE
    assert v.note("z") == ()
    with pytest.raises(KeyError):
        v.cell("z.notation")
    with pytest.raises(KeyError):
        v.cell("nope.term")


def test_view_unknown_concept_has_no_aspects_and_no_cells():
    v = AspectKView(base(), T0)
    assert v.aspects("nope") == ()
    assert v.cells(("nope",)) == {}
    assert set(v.cells(("nope", "z"))) == {"z.term", "z.procedure", "z.mechanism"}
    with pytest.raises(KeyError):
        v.cell("nope.term")


def test_naive_as_of_is_rejected():
    k = base()
    naive = T0.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        AspectKView(k, naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        k.belief("z.term", naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        k.concept_summary("z", naive)


def test_note_id_collision_is_rejected_fail_closed(monkeypatch):
    import evowiki.belief.aspects as mod
    monkeypatch.setattr(mod, "note_id", lambda event_id: "m_const")
    k = base()
    k.apply(AddNote(event_id="n1", ts=T0, cell="z.term", text="a", evidence_ref="e1"))
    snap = k.to_json()
    with pytest.raises(AspectRejected):
        k.apply(AddNote(event_id="n2", ts=T0, cell="z.procedure", text="b", evidence_ref="e2"))
    assert k.to_json() == snap
    k.apply(RetireNote(event_id="r", ts=T0, entry_id="m_const"))
    snap = k.to_json()
    with pytest.raises(AspectRejected):
        k.apply(AddNote(event_id="n3", ts=T0, cell="z.term", text="c", evidence_ref="e3"))
    assert k.to_json() == snap and k.notes("z") == ()
    c = k.copy()
    assert c.to_json() == snap
    with pytest.raises(AspectRejected):
        c.apply(AddNote(event_id="n4", ts=T0, cell="z.term", text="d", evidence_ref="e4"))
