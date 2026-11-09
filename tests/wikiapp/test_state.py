from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from evowiki.goals.contracts import (
    Band,
    CheckItem,
    OutlineSection,
    PageOutline,
    SectionRole,
    SectionState,
    SectionText,
)
from evowiki.goals.fold import Page, states_for
from evowiki.goals.intake import IntakeConcept, Subitem
from evowiki.teach.contracts import Exemplar, SectionLog, Signal
from evowiki.wikiapp.events import (
    CellChange,
    ConceptName,
    ConceptsNamed,
    DiffNote,
    Draft,
    DraftClosed,
    DraftCreated,
    Guideline,
    GuidelineAdded,
    GuidelineProposal,
    GuidelineProposalClosed,
    GuidelineProposalRejected,
    GuidelineProposed,
    GuidelineRemoved,
    PageOpened,
    PageRecord,
    PageSaved,
    SectionLogsSaved,
    SignalRecorded,
    TeachingSpecSaved,
    UsageAdded,
    UsageRecord,
    WikiEvent,
    WikiEventBody,
    WikiStarted,
)
from evowiki.wikiapp.state import (
    ASPECT_CONFIG_VERSION,
    ReplayMismatch,
    WikiState,
    apply_event,
    replay,
)
from evowiki.wikiapp.teachspec import add_exemplar, add_item, migrate, migration_evidence

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
_n = iter(range(100_000))


def ev(body, ts=T0) -> WikiEvent:
    return WikiEvent(event_id=f"e{next(_n)}", ts=ts, body=body)


def outline(pid="p1") -> PageOutline:
    item = CheckItem(cell="zscore.procedure", stem="z=?", options=("a", "b", "c"), answer=1,
                     verified=True)
    return PageOutline(
        page_id=pid, goal="学会 z-score", concepts=("mean", "zscore"), targets=("zscore",),
        sections=(
            OutlineSection(id="s1", heading="开篇", role=SectionRole.ORGANIZER),
            OutlineSection(id="s2", heading="均值", role=SectionRole.RECAP, cells=("mean.term",)),
            OutlineSection(id="s3", heading="确认", role=SectionRole.CHECK,
                           cells=("zscore.procedure",), check=item),
        ))


def page(pid="p1") -> Page:
    o = outline(pid)
    st = states_for(o)
    st["s2"] = SectionState(section_id="s2", folded=True, fold_locked=True)
    return Page(outline=o, states=st,
                texts={"s2": SectionText(section_id="s2", cue="均值=平均", body="正文",
                                         cache_key="k")},
                snapshot={"mean.term": Band.KNOWN, "zscore.procedure": Band.GAP},
                unresolved_conflicts=("「均值」还不会，但只一行带过",))


def record(pid="p1", **kw) -> PageRecord:
    base = {"page_id": pid, "title": "z-score", "goal": "学会 z-score",
            "concepts": ("mean", "zscore"), "targets": ("zscore",), "created_at": T0}
    return PageRecord(**{**base, **kw})


def draft() -> Draft:
    return Draft(
        draft_id="d1", goal="学会 z-score", title="z-score",
        concepts=(IntakeConcept(id="zscore", name="z-score", target=True,
                                subitems=(Subitem(aspect="term", name="定义"),)),),
        targets=("zscore",),
        items=(CheckItem(cell="zscore.term", stem="?", options=("a", "b", "c"), answer=0,
                         verified=True),),
        from_background=1, reused=0, dropped_questions=0)


def script() -> list[WikiEvent]:
    rec = record(page=page(), changed={"s2": "folded", "s3": "inserted"}, checks={"s3": -1},
                 diff=DiffNote(cell_changes=(CellChange(cell="mean.term", before=Band.SHAKY,
                                                        after=Band.KNOWN),),
                               folded=("s2",), patched=("s3",)))
    return [
        ev(WikiStarted(aspect_config=ASPECT_CONFIG_VERSION)),
        ev(ConceptsNamed(names=(ConceptName(concept_id="zscore", name="z-score"),))),
        ev(DraftCreated(draft=draft())),
        ev(DraftClosed(draft_id="d1", outcome="answered", page_id="p1")),
        ev(PageSaved(record=record(), action="created")),
        ev(PageSaved(record=record("p2"), action="created")),
        ev(PageOpened(page_id="p1"), ts=T0 + timedelta(minutes=5)),
        ev(PageSaved(record=rec, action="organized")),
        ev(UsageAdded(record=UsageRecord(action="open", page="p1"))),
        ev(GuidelineAdded(guideline=Guideline(guideline_id="g1", text="多举例", source="user"))),
        ev(GuidelineProposed(proposal=GuidelineProposal(proposal_id="gp1", text="先给公式",
                                                         evidence="你两次要公式"))),
        ev(GuidelineProposalClosed(proposal_id="gp1")),
        ev(GuidelineAdded(guideline=Guideline(guideline_id="g2", text="先给公式",
                                              source="feedback", evidence="你两次要公式"))),
        ev(GuidelineProposed(proposal=GuidelineProposal(proposal_id="gp2", text="少用比喻",
                                                         evidence="?"))),
        ev(GuidelineProposalRejected(proposal_id="gp2")),
        ev(GuidelineRemoved(guideline_id="g1")),
    ]


def test_language_defaults_zh_and_replays():
    from evowiki.wikiapp.events import LanguageSet
    assert replay([]).language == "zh"
    assert replay(script()).language == "zh"          # an old log has no LanguageSet
    s = replay([*script(), ev(LanguageSet(language="en"))])
    assert s.language == "en"
    assert s.to_json() == replay([*script(), ev(LanguageSet(language="en"))]).to_json()


def test_stepwise_apply_equals_replay_and_rules():
    events = script()
    s = WikiState()
    for e in events:
        s = apply_event(s, e)
    r = replay(events)
    assert s.to_json() == r.to_json()
    assert r.started
    assert r.names["zscore"].name == "z-score"
    assert "d1" in r.drafts and r.closed_drafts == frozenset({"d1"})
    assert r.page_order == ("p1", "p2")          # re-save does not re-append
    assert r.pages["p1"].page is not None and r.pages["p1"].changed["s3"] == "inserted"
    assert r.current_page == "p1"                # opened after p2 was created
    assert r.opened_at["p1"] == T0 + timedelta(minutes=5)
    assert r.principles() == ("先给公式",)
    assert r.guidelines[0].source == "feedback"
    assert r.rejected_guidelines == ("少用比喻",)
    assert r.guideline_proposals == {}


def test_page_saved_created_sets_current_page():
    s = replay([ev(WikiStarted(aspect_config=ASPECT_CONFIG_VERSION)),
                ev(PageSaved(record=record("p1"), action="created")),
                ev(PageSaved(record=record("p2"), action="created")),
                ev(PageSaved(record=record("p1", done=True), action="done"))])
    assert s.current_page == "p2" and s.pages["p1"].done


def test_apply_is_pure():
    s0 = WikiState()
    before = s0.to_json()
    s1 = apply_event(s0, ev(PageSaved(record=record(), action="created")))
    assert s0.to_json() == before and s0.pages == {} and s1.page_order == ("p1",)


def test_to_json_stable_and_set_order_free():
    assert replay(script()).to_json() == replay(script()).to_json()
    s = WikiState(closed_drafts=frozenset({"b", "a", "c"}),
                  hidden_cards=frozenset({"z", "y", "x"}))
    t = WikiState(closed_drafts=frozenset({"c", "a", "b"}),
                  hidden_cards=frozenset({"x", "z", "y"}))
    assert s.to_json() == t.to_json()
    j = s.to_json()
    assert '"closed_drafts":["a","b","c"]' in j and '"hidden_cards":["x","y","z"]' in j


def test_active_pages_excludes_shelved():
    s = replay([ev(PageSaved(record=record("p1"), action="created")),
                ev(PageSaved(record=record("p2", shelved=True), action="created")),
                ev(PageSaved(record=record("p3", done=True), action="created"))])
    assert s.active_pages() == ("p1", "p3")       # done pages are still active (U2)


def test_wiki_event_json_round_trip_with_page():
    ta = TypeAdapter(list[WikiEvent])
    events = script()
    back = ta.validate_json(ta.dump_json(events))
    assert back == events
    saved = next(e for e in back if isinstance(e.body, PageSaved) and e.body.record.page)
    assert saved.body.record.page == page()
    assert saved.body.record.page.unresolved_conflicts == ("「均值」还不会，但只一行带过",)
    assert saved.body.record.diff.cell_changes[0].before is Band.SHAKY
    assert replay(back).to_json() == replay(events).to_json()
    one = WikiEvent.model_validate_json(events[7].model_dump_json())
    assert one == events[7] and isinstance(one.body, PageSaved) and one.body.record.page


def test_body_discriminated_by_type_and_forbids_extra():
    ta = TypeAdapter(WikiEventBody)
    assert isinstance(ta.validate_python({"type": "page_opened", "page_id": "p1"}), PageOpened)
    with pytest.raises(ValidationError):
        ta.validate_python({"type": "page_opened", "page_id": "p1", "x": 1})
    with pytest.raises(ValidationError):
        ta.validate_python({"type": "nope"})
    with pytest.raises(ValidationError):
        PageRecord(**{**record().model_dump(), "changed": {"s1": "deleted"}})
    with pytest.raises(ValidationError):
        Guideline(guideline_id="g", text="t", source="user", src="x")
    with pytest.raises(ValidationError):
        Guideline(guideline_id="g", text="t", source="llm")
    with pytest.raises(ValidationError):
        PageRecord(**{**record().model_dump(), "diff_note": "x"})


def test_events_are_frozen():
    e = ev(PageOpened(page_id="p1"))
    with pytest.raises(ValidationError):
        e.body.page_id = "p2"
    with pytest.raises(ValidationError):
        CellChange(cell="a.term", before=Band.GAP, after=Band.KNOWN).cell = "b"
    with pytest.raises(ValidationError):
        WikiState().started = True


def test_wiki_started_version_mismatch_fails_closed():
    with pytest.raises(ReplayMismatch):
        replay([ev(WikiStarted(aspect_config="aspect-config@v0"))])
    assert issubclass(ReplayMismatch, ValueError)


def test_usage_keeps_last_50():
    s = replay([ev(UsageAdded(record=UsageRecord(action="open", text=str(i)))) for i in range(60)])
    assert len(s.usage) == 50 and s.usage[0].text == "10" and s.usage[-1].text == "59"


def test_guideline_proposals_open_close_and_reject():
    s = replay([
        ev(GuidelineProposed(proposal=GuidelineProposal(proposal_id="a", text="A", evidence=""))),
        ev(GuidelineProposed(proposal=GuidelineProposal(proposal_id="b", text="B", evidence=""))),
    ])
    assert list(s.guideline_proposals) == ["a", "b"]
    s = apply_event(s, ev(GuidelineProposalClosed(proposal_id="b")))
    assert list(s.guideline_proposals) == ["a"] and s.rejected_guidelines == ()
    s = apply_event(s, ev(GuidelineProposalRejected(proposal_id="a")))
    assert s.guideline_proposals == {} and s.rejected_guidelines == ("A",)


# ---- teaching spec / section logs / signals (R1) ----

def _teach_script() -> list[WikiEvent]:
    v1 = migrate(script_guidelines := (Guideline(guideline_id="g2", text="先给公式",
                                                 source="feedback", evidence="你两次要公式"),))
    v2 = add_item(v1, item_id="u1", text="多画图", scope="concept", domain="生物")
    v3 = add_exemplar(v2, Exemplar(exemplar_id="x1", kind="vault_note", ref="a.md", title="A",
                                   text="正文"))
    t = T0 + timedelta(hours=1)
    log = lambda sid, at, producer="organizer": SectionLog(
        page_id="p1", section_id=sid, at=at, producer=producer, cells=("mean.term",),
        bands={"mean.term": "known"}, role="recap", why="已会", spec_items=("d01", "g2"),
        spec_version=1, infer_version=1)
    sig = Signal(signal_id="sg1", kind="section_know", at=t, page_id="p1", section_id="s2",
                 spec_version=1, infer_version=1)
    return [
        *script(),
        ev(PageSaved(record=record(page=page(), domain="生物"), action="organized")),
        ev(TeachingSpecSaved(spec=v1, action="migrated",
                             evidence=migration_evidence(script_guidelines)), ts=t),
        ev(TeachingSpecSaved(spec=v2, action="edit"), ts=t),
        ev(TeachingSpecSaved(spec=v3, action="exemplar"), ts=t),
        ev(SectionLogsSaved(logs=(log("s1", t), log("s2", t))), ts=t),
        ev(SectionLogsSaved(logs=(log("s2", t + timedelta(minutes=1), "writer"),)),
           ts=t + timedelta(minutes=1)),
        ev(SignalRecorded(signal=sig), ts=t),
        ev(SignalRecorded(signal=sig.model_copy(update={"signal_id": "sg2"})), ts=t),
    ]


def test_teach_events_stepwise_equals_replay():
    events = _teach_script()
    s = WikiState()
    for e in events:
        s = apply_event(s, e)
    r = replay(events)
    assert s.to_json() == r.to_json()
    assert r.spec is not None and r.spec.version == 3 and len(r.spec.exemplars) == 1
    assert [(m.version, m.action, m.n_exemplars) for m in r.spec_versions] == \
        [(1, "migrated", 0), (2, "edit", 0), (3, "exemplar", 1)]
    assert r.spec_versions[-1].n_items == len(r.spec.items)
    assert r.item_evidence == {"g2": "你两次要公式"}
    assert r.pages["p1"].domain == "生物"
    assert set(r.section_logs["p1"]) == {"s1", "s2"}
    assert r.section_logs["p1"]["s2"].producer == "writer"         # latest per section
    assert r.section_log("p1", "s1").section_id == "s1"
    assert r.section_log("p1", "s3").section_id == "s2"             # else the page's latest
    assert r.section_log("p1").section_id == "s2"
    assert r.section_log("p2") is None
    assert r.signals_recorded == 2
    # guideline events from the old log still replay
    assert r.principles() == ("先给公式",)


def test_teach_events_json_round_trip():
    ta = TypeAdapter(list[WikiEvent])
    events = _teach_script()
    back = ta.validate_json(ta.dump_json(events))
    assert back == events
    assert replay(back).to_json() == replay(events).to_json()


def test_old_page_record_payload_without_domain_parses():
    raw = record().model_dump(mode="json")
    del raw["domain"]
    assert PageRecord.model_validate(raw).domain is None
    assert WikiState().spec is None and WikiState().section_logs == {}
