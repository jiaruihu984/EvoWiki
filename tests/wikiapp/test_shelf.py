"""收为参考卡 / 恢复成完整页 (spec 2026-10-29 retire-curate §4): offered only when every target is
确定会, the learner's own reversible toggle; shelving folds the page (a section with a weak cell stays
open), taking it off unfolds what is not all 确定会; the card's line is the page's first recall cue;
no model call, the graph and K untouched; roles are worked out from the pages, never from K."""

from datetime import UTC, datetime

import pytest

from evowiki.belief.aspects import DefineConcept
from evowiki.goals.contracts import (
    Aspect,
    Band,
    OutlineSection,
    PageOutline,
    SectionRole,
    SectionText,
)
from evowiki.goals.fold import Page, states_for
from evowiki.wikiapp import shelf
from evowiki.wikiapp.events import PageRecord, PageSaved, WikiEvent
from evowiki.wikiapp.kwrite import concept_roles
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.views import page_out, shelf_line, shelvable
from tests.wiki.fakes import FakeK

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
K = Band.KNOWN
KNOWN = FakeK({"mean": {Aspect.TERM: K}, "z": {Aspect.TERM: K, Aspect.PROCEDURE: K}})
SHAKY_MEAN = FakeK({"mean": {Aspect.TERM: Band.SHAKY}, "z": {Aspect.TERM: K, Aspect.PROCEDURE: K}})
HALF = FakeK({"mean": {Aspect.TERM: K}, "z": {Aspect.TERM: K, Aspect.PROCEDURE: Band.LIKELY}})


def record(pid="p1", **kw) -> PageRecord:
    o = PageOutline(page_id=pid, goal="z 分数", concepts=("mean", "z"), targets=("z",), sections=(
        OutlineSection(id="s1", heading="开篇", role=SectionRole.ORGANIZER),
        OutlineSection(id="s2", heading="均值", role=SectionRole.RECAP, cells=("mean.term",)),
        OutlineSection(id="s3", heading="z 分数", role=SectionRole.EXPLAIN,
                       cells=("z.term", "z.procedure"))))
    page = Page(outline=o, states=states_for(o), texts={
        "s2": SectionText(section_id="s2", cue="", body="均值", cache_key="k2"),
        "s3": SectionText(section_id="s3", cue="减均值再除以标准差", body="z", cache_key="k3")})
    base = {"page_id": pid, "title": "z 分数", "goal": "z 分数", "concepts": ("mean", "z"),
            "targets": ("z",), "created_at": NOW, "page": page}
    return PageRecord(**{**base, **kw})


def folded(rec: PageRecord) -> set[str]:
    assert rec.page is not None
    return {s for s, st in rec.page.states.items() if st.folded}


def test_offered_only_when_every_target_is_known():
    assert shelvable(record(), KNOWN)
    assert not shelvable(record(), HALF)                        # a target cell not 确定会
    assert not shelvable(record(shelved=True), KNOWN)           # already a card
    assert not shelvable(record(page=None), KNOWN)              # not written


def test_shelving_folds_but_a_weak_section_stays_open():
    new, did = shelf.shelved_record(record(), SHAKY_MEAN)
    assert new.shelved and new.collapse_applied
    assert "s2" not in folded(new) and "s3" in folded(new) and "s3" in did


def test_taking_it_off_unfolds_what_is_not_all_known():
    on, _ = shelf.shelved_record(record(), KNOWN)
    off, opened = shelf.unshelved_record(on, HALF)
    assert not off.shelved and not off.collapse_applied
    assert "s3" in opened and "s3" not in folded(off)


def test_the_cards_line_is_the_first_recall_cue():
    assert shelf_line(record()) == "减均值再除以标准差"         # s2 has none: s3's
    assert shelf_line(record(page=None)) == ""
    p = record().page
    no_cue = p.model_copy(update={"texts": {"s2": p.texts["s2"]}})
    assert shelf_line(record(page=no_cue)) == "均值"             # no cue at all: the first heading


def test_page_out_says_what_the_page_offers():
    st = WikiState(pages={"p1": record()}, page_order=("p1",))
    out = page_out(st, KNOWN, record())
    assert out.can_shelve and not out.collapsed and out.shelf_line == ""
    on = record(shelved=True)
    out = page_out(st.model_copy(update={"pages": {"p1": on}}), KNOWN, on)
    assert out.collapsed and not out.can_shelve and out.shelf_line == "减均值再除以标准差"


def test_roles_come_from_the_pages_not_from_k():
    learn, used = record("p1"), record("p2", concepts=("z", "mean"), targets=("mean",))
    st = WikiState(pages={"p1": learn, "p2": used}, page_order=("p1", "p2"))
    assert concept_roles(st, "p1") == {"z": "learn", "mean": "support"}
    assert concept_roles(st) == {"z": "learn", "mean": "learn"}
    st = st.model_copy(update={"pages": {"p1": learn.model_copy(update={"shelved": True}),
                                         "p2": used}})
    assert concept_roles(st, "p2") == {"mean": "learn", "z": "reference"}
    assert concept_roles(st)["z"] == "reference"


@pytest.fixture
def repo():
    r, n = MemoryRepo(["u1"]), iter(range(1000))
    snap = r.load("u1")
    r.commit(snap, Commit(
        aspect=[DefineConcept(event_id="aev-m", ts=NOW, concept_id="mean", aspects=("term",)),
                DefineConcept(event_id="aev-z", ts=NOW, concept_id="z",
                              aspects=("term", "procedure"))],
        wiki=[WikiEvent(event_id="wev-0", ts=NOW, body=PageSaved(record=record(),
                                                                  action="created"))]))
    return r, (lambda prefix: f"{prefix}-{next(n) + 1}")


def test_shelving_is_refused_while_a_target_is_not_known(repo):
    r, ids = repo                                    # an empty K: nothing is 确定会
    with pytest.raises(WikiConflict):
        shelf.collapse(r, "u1", "p1", now=NOW, new_id=ids)
    with pytest.raises(WikiConflict):
        shelf.expand(r, "u1", "p1", now=NOW, new_id=ids)          # not shelved
    with pytest.raises(LookupError):
        shelf.collapse(r, "u1", "nope", now=NOW, new_id=ids)


def test_shelve_and_take_off_round_trip(repo, monkeypatch):
    r, ids = repo
    monkeypatch.setattr(shelf, "shelvable", lambda rec, k: True)
    before = r.load("u1")
    out = shelf.collapse(r, "u1", "p1", now=NOW, new_id=ids)
    st = r.load("u1").state
    assert st.pages["p1"].shelved and st.active_pages() == () and out.change.folded
    assert st.graph == before.state.graph and r.load("u1").aspect_seq == before.aspect_seq
    shelf.expand(r, "u1", "p1", now=NOW, new_id=ids)
    st = r.load("u1").state
    assert not st.pages["p1"].shelved and st.active_pages() == ("p1",)
    assert r.load("u1").aspect_seq == before.aspect_seq           # K untouched
