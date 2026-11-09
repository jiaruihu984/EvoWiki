from datetime import UTC, datetime, timedelta, timezone

import pytest

from evowiki.belief.aspects import (
    AddNote,
    AspectKView,
    AspectRejected,
    DefineConcept,
    Observe,
)
from evowiki.goals.contracts import Aspect, Observation, cell_id
from evowiki.goals.path import LearningPath
from evowiki.nodes.model import NodeGraph
from evowiki.store.aspects import aspect_events
from evowiki.store.wiki import WikiEventConflict, wiki_events
from evowiki.teach.contracts import Signal
from evowiki.wikiapp.events import (
    DraftClosed,
    GraphSaved,
    GroupName,
    GroupsNamed,
    Guideline,
    GuidelineAdded,
    LanguageSet,
    PageMarksCleared,
    PageOpened,
    PageRecord,
    PageSaved,
    PagesTrashed,
    PathRecord,
    PathSaved,
    SignalRecorded,
    UsageAdded,
    UsageRecord,
    WikiEvent,
    WikiStarted,
)
from evowiki.wikiapp.repo import (
    ANY,
    K_SCOPE,
    PAGE_LIST,
    Commit,
    MemoryRepo,
    PgRepo,
    Snapshot,
    WikiConflict,
    scopes,
)
from evowiki.wikiapp.state import ASPECT_CONFIG_VERSION, WikiState
from tests.runtime.helpers import make_user

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
_n = iter(range(100_000))


def wev(body, ts=T0) -> WikiEvent:
    return WikiEvent(event_id=f"we{next(_n)}", ts=ts, body=body)


def usage(action: str, ts=T0) -> WikiEvent:
    return wev(UsageAdded(record=UsageRecord(action=action)), ts)


def define(cid: str, ts=T0) -> DefineConcept:
    return DefineConcept(event_id=f"ae{next(_n)}", ts=ts, concept_id=cid,
                         aspects=(Aspect.TERM, Aspect.PROCEDURE))


def observe(cell: str, ts=T0) -> Observe:
    return Observe(event_id=f"ae{next(_n)}", ts=ts, observation=Observation(
        cell=cell, polarity="+", strength=0.8, kind="quiz", evidence_ref=f"quiz:{next(_n)}"))


def guideline(gid: str) -> WikiEvent:
    return wev(GuidelineAdded(guideline=Guideline(guideline_id=gid, text="先例子",
                                                  source="user")))


def page_saved(pid: str, title: str = "t") -> WikiEvent:
    return wev(PageSaved(record=PageRecord(page_id=pid, title=title, goal="g",
                                           concepts=(), targets=(), created_at=T0),
                         action="created"))


def groups_named(*keys: str) -> WikiEvent:
    return wev(GroupsNamed(names=tuple(GroupName(key=k, name=k, summary="") for k in keys)))


def language_set(language="en") -> WikiEvent:
    return wev(LanguageSet(language=language))


def graph_saved() -> WikiEvent:
    return wev(GraphSaved(graph=NodeGraph(), action="user_edit"))


def same(a: Snapshot, b: Snapshot) -> bool:
    return (a.user_id, a.state.to_json(), a.k.to_json(), a.wiki_seq, a.aspect_seq) == (
        b.user_id, b.state.to_json(), b.k.to_json(), b.wiki_seq, b.aspect_seq)


@pytest.fixture(params=["memory", "pg"])
def repo(request):
    if request.param == "memory":
        return MemoryRepo(["u1", "u2"])
    pool = request.getfixturevalue("pool")
    make_user(pool, "u1")
    make_user(pool, "u2")
    return PgRepo(pool)


def test_fresh_user_loads_empty(repo):
    s = repo.load("u1")
    assert s.user_id == "u1" and s.wiki_seq == 0 and s.aspect_seq == 0
    assert s.state == WikiState() and not s.state.started and s.k.concept_ids() == ()
    assert isinstance(s.kview(T0), AspectKView)


def test_unknown_user_is_lookup_error(repo):
    with pytest.raises(LookupError):
        repo.load("nobody")


def test_commit_then_load_equals_returned_snapshot_and_starts_once(repo):
    s0 = repo.load("u1")
    c = define("c1")
    s1 = repo.commit(s0, Commit(aspect=[c, observe(cell_id("c1", "term"))],
                                wiki=[usage("a"), usage("b")]))
    assert s1.state.started and s1.wiki_seq == 3 and s1.aspect_seq == 2
    assert [u.action for u in s1.state.usage] == ["a", "b"]
    assert s1.k.concept_ids() == ("c1",)
    assert same(repo.load("u1"), s1)
    s2 = repo.commit(s1, Commit(wiki=[usage("c")]))
    assert s2.wiki_seq == 4 and s2.aspect_seq == 2           # no second WikiStarted
    assert same(repo.load("u1"), s2)
    assert same(repo.load("u2"), MemoryRepo(["u2"]).load("u2"))   # other user untouched
    # the input snapshot is never mutated
    assert s0.k.concept_ids() == () and s0.wiki_seq == 0


def test_first_commit_prepends_wiki_started(repo, request):
    s1 = repo.commit(repo.load("u1"), Commit(aspect=[define("c1")]))
    assert s1.state.started and s1.wiki_seq == 1 and s1.aspect_seq == 1
    if isinstance(repo, PgRepo):
        pool = request.getfixturevalue("pool")
        with pool.connection() as conn:
            got = wiki_events(conn, "u1")
        assert [e.type for e in got] == ["wiki_started"]
        assert got[0].payload["aspect_config"] == ASPECT_CONFIG_VERSION
    else:
        evs = repo.wiki_log("u1")
        assert isinstance(evs[0].body, WikiStarted)
        assert evs[0].body.aspect_config == ASPECT_CONFIG_VERSION


def test_empty_commit_writes_nothing(repo):
    s0 = repo.load("u1")
    s1 = repo.commit(s0, Commit())
    assert same(s1, s0) and not s1.state.started
    assert same(repo.load("u1"), s0)


def test_stale_snapshot_writing_the_same_record_conflicts_and_writes_nothing(repo):
    s0 = repo.commit(repo.load("u1"), Commit(aspect=[define("c1")], wiki=[usage("a")]))
    s1 = repo.commit(s0, Commit(wiki=[guideline("g_stale")]))
    with pytest.raises(WikiConflict):
        repo.commit(s0, Commit(aspect=[define("c2")], wiki=[guideline("g2")]))
    assert same(repo.load("u1"), s1)


def test_rejected_aspect_batch_writes_no_wiki_events(repo):
    s0 = repo.commit(repo.load("u1"), Commit(wiki=[usage("a")]))
    with pytest.raises(AspectRejected):
        repo.commit(s0, Commit(aspect=[define("c1"), observe(cell_id("nope", "term"))],
                               wiki=[usage("b")]))
    assert same(repo.load("u1"), s0)


def test_wiki_failure_rolls_back_the_aspect_batch(repo):
    dup = usage("a")
    s0 = repo.commit(repo.load("u1"), Commit(wiki=[dup]))
    with pytest.raises(WikiEventConflict):
        repo.commit(s0, Commit(aspect=[define("c1")], wiki=[dup]))
    assert same(repo.load("u1"), s0)


def test_pg_rejected_batch_leaves_both_logs_untouched(pool):
    make_user(pool, "u1")
    repo = PgRepo(pool)
    s0 = repo.commit(repo.load("u1"), Commit(aspect=[define("c1")], wiki=[usage("a")]))
    with pytest.raises(AspectRejected):
        repo.commit(s0, Commit(aspect=[observe(cell_id("c1", "term")),
                                       observe(cell_id("zz", "term"))],
                               wiki=[usage("b")]))
    with pool.connection() as conn:
        assert len(wiki_events(conn, "u1")) == 2
        assert len(aspect_events(conn, "u1")) == 1


def test_pg_repo_aspect_events(pool):
    make_user(pool, "u1")
    repo = PgRepo(pool)
    assert repo.aspect_events("u1") == []
    d = define("c1")
    repo.commit(repo.load("u1"), Commit(aspect=[d]))
    assert repo.aspect_events("u1") == [d]
    with pytest.raises(LookupError):
        repo.aspect_events("nobody")


def test_memory_and_pg_snapshots_agree(pool):
    make_user(pool, "u1")
    pg, mem = PgRepo(pool), MemoryRepo(["u1"])
    plus8 = timezone(timedelta(hours=8))
    batches = [
        Commit(aspect=[define("c1")], wiki=[usage("a")]),
        Commit(aspect=[observe(cell_id("c1", "term")),
                       AddNote(event_id="note1", ts=T0, cell=cell_id("c1", "term"),
                               text="原话", evidence_ref="quiz:1")]),
        Commit(wiki=[wev(PageOpened(page_id="p1"), T0.astimezone(plus8)),
                     wev(GuidelineAdded(guideline=Guideline(guideline_id="g1", text="先例子",
                                                            source="user")))]),
        Commit(),
    ]
    sp, sm = pg.load("u1"), mem.load("u1")
    assert same(sp, sm)
    for b in batches:
        sp, sm = pg.commit(sp, b), mem.commit(sm, b)
        assert same(sp, sm)
        assert same(pg.load("u1"), sp) and same(mem.load("u1"), sm)
    assert sp.state.opened_at["p1"] == T0


def test_aspect_retry_across_batches_is_idempotent(repo):
    d = define("c1")
    s1 = repo.commit(repo.load("u1"), Commit(aspect=[d]))
    s2 = repo.commit(s1, Commit(aspect=[d, observe(cell_id("c1", "term"))]))
    assert s2.aspect_seq == 2 and same(repo.load("u1"), s2)


# ---- M4: benign intervening wiki events ----

def test_benign_wiki_events_meanwhile_append_after_them(repo):
    s0 = repo.commit(repo.load("u1"), Commit(aspect=[define("c1")], wiki=[usage("a")]))
    other = repo.commit(repo.load("u1"), Commit(wiki=[wev(PageOpened(page_id="pg1"))]))
    other = repo.commit(other, Commit(wiki=[usage("b")]))
    s1 = repo.commit(s0, Commit(aspect=[observe(cell_id("c1", "term"))], wiki=[usage("x")]))
    assert s1.wiki_seq == s0.wiki_seq + 3 and s1.aspect_seq == s0.aspect_seq + 1
    assert [u.action for u in s1.state.usage] == ["a", "b", "x"]        # full replay
    assert s1.state.current_page == "pg1" and "pg1" in s1.state.opened_at
    assert same(repo.load("u1"), s1)


def test_benign_wiki_events_meanwhile_with_empty_commit(repo):
    s0 = repo.commit(repo.load("u1"), Commit(wiki=[usage("a")]))
    repo.commit(repo.load("u1"), Commit(wiki=[wev(PageOpened(page_id="pg1"))]))
    s1 = repo.commit(s0, Commit())
    assert same(repo.load("u1"), s1)


def test_signal_recorded_meanwhile_is_benign(repo):
    """R1: a signal (page feedback, a note edit, a failed rewrite's click) landing between a
    flow's load and its commit is appended under it — the flow's commit goes on top, no 409."""
    s0 = repo.commit(repo.load("u1"), Commit(aspect=[define("c1")], wiki=[usage("a")]))
    sig = Signal(signal_id="sig_mid", kind="page_feedback", at=T0, page_id="pg1", text="中途")
    repo.commit(repo.load("u1"), Commit(wiki=[wev(SignalRecorded(signal=sig)), usage("fb")]))
    s1 = repo.commit(s0, Commit(aspect=[observe(cell_id("c1", "term"))],
                                wiki=[guideline("g1"), usage("x")]))
    assert s1.wiki_seq == s0.wiki_seq + 4 and s1.state.signals_recorded == 1
    assert [u.action for u in s1.state.usage] == ["a", "fb", "x"]
    assert "g1" in {g.guideline_id for g in s1.state.guidelines}
    assert same(repo.load("u1"), s1)
    assert [e.body.signal for _, e in repo.wiki_events_since("u1", ("signal_recorded",))] == [sig]


def test_aspect_events_meanwhile_do_not_conflict_and_are_kept(repo):
    """K events are appended evidence: another request's Observe lands; a pure K append and a
    wiki write without K events go on top, and the returned K has everything."""
    s0 = repo.commit(repo.load("u1"), Commit(aspect=[define("c1")], wiki=[usage("a")]))
    repo.commit(repo.load("u1"), Commit(aspect=[observe(cell_id("c1", "term"))],
                                        wiki=[usage("b")]))
    s1 = repo.commit(s0, Commit(aspect=[observe(cell_id("c1", "procedure"))]))
    assert s1.aspect_seq == s0.aspect_seq + 2
    assert same(repo.load("u1"), s1)
    s2 = repo.commit(s0, Commit(wiki=[page_saved("p1")]))
    assert "p1" in s2.state.pages and s2.aspect_seq == s1.aspect_seq
    assert same(repo.load("u1"), s2)
    s3 = repo.commit(s0, Commit())          # an empty commit on the old snapshot catches up too
    assert same(repo.load("u1"), s3)


@pytest.mark.parametrize("how", ["own_k_events", "depends"])
def test_a_commit_computed_on_k_conflicts_when_k_moved(repo, how):
    """A wiki write made together with its own K events (a K-change pass), or one that declared
    K_SCOPE, was computed on K: K events landing meanwhile conflict."""
    s0 = repo.commit(repo.load("u1"), Commit(aspect=[define("c1")], wiki=[usage("a")]))
    s1 = repo.commit(repo.load("u1"), Commit(aspect=[observe(cell_id("c1", "term"))]))
    c = (Commit(aspect=[observe(cell_id("c1", "procedure"))], wiki=[page_saved("p1")])
         if how == "own_k_events" else
         Commit(wiki=[page_saved("p1")], depends=frozenset({K_SCOPE})))
    with pytest.raises(WikiConflict):
        repo.commit(s0, c)
    assert same(repo.load("u1"), s1)


def test_unrelated_wiki_events_meanwhile_append_after_them(repo):
    """Group naming, another page's save, a setting: none touches the record this commit
    writes, so it lands on top and the returned state replays them."""
    s0 = repo.commit(repo.load("u1"), Commit(wiki=[page_saved("p1"), page_saved("p2")]))
    repo.commit(repo.load("u1"), Commit(wiki=[groups_named("g:a"), page_saved("p2", "新标题"),
                                              language_set()]))
    s1 = repo.commit(s0, Commit(wiki=[page_saved("p1", "改过")]))
    assert s1.state.pages["p1"].title == "改过" and s1.state.pages["p2"].title == "新标题"
    assert "g:a" in s1.state.groups
    assert same(repo.load("u1"), s1)


@pytest.mark.parametrize("landed,own", [
    ("page", "page"), ("language", "language"), ("group", "group"), ("graph", "graph"),
    ("any", "page"), ("page", "any"), ("trash", "page")])
def test_same_record_meanwhile_conflicts(repo, landed, own):
    make = {"page": lambda: page_saved("p1", "x"), "language": language_set,
            "group": lambda: groups_named("g:a"), "graph": graph_saved,
            "any": lambda: wev(PageMarksCleared(job_id="h_1")),
            "trash": lambda: wev(PagesTrashed(page_ids=("p1",)))}
    s0 = repo.commit(repo.load("u1"), Commit(wiki=[page_saved("p1")]))
    s1 = repo.commit(repo.load("u1"), Commit(wiki=[make[landed]()]))
    with pytest.raises(WikiConflict):
        repo.commit(s0, Commit(wiki=[make[own]()]))
    assert same(repo.load("u1"), s1)


def test_depends_conflicts_with_a_write_to_what_the_flow_read(repo):
    s0 = repo.commit(repo.load("u1"), Commit(wiki=[page_saved("p1")]))
    repo.commit(repo.load("u1"), Commit(wiki=[graph_saved()]))
    with pytest.raises(WikiConflict):
        repo.commit(s0, Commit(wiki=[page_saved("p1", "x")], depends=frozenset({"graph"})))
    repo.commit(s0, Commit(wiki=[page_saved("p1", "x")], depends=frozenset({"path:q"})))


def test_page_list_dependency_conflicts_only_with_a_created_page(repo):
    """A flow read the set of pages: a page created since conflicts, a save of an existing
    page does not; two commits that each create a page do not conflict with each other."""
    s0 = repo.commit(repo.load("u1"), Commit(wiki=[page_saved("p1")]))
    repo.commit(repo.load("u1"), Commit(wiki=[page_saved("p1", "x")]))
    s1 = repo.commit(s0, Commit(wiki=[language_set()], depends=frozenset({PAGE_LIST})))
    repo.commit(repo.load("u1"), Commit(wiki=[page_saved("p2")]))
    with pytest.raises(WikiConflict):
        repo.commit(s1, Commit(wiki=[language_set("zh")], depends=frozenset({PAGE_LIST})))
    repo.commit(s1, Commit(wiki=[page_saved("p3")]))          # creates beside p2: fine


def test_benign_only_commit_never_conflicts(repo):
    s0 = repo.commit(repo.load("u1"), Commit(wiki=[usage("a")]))
    s1 = repo.commit(repo.load("u1"), Commit(wiki=[wev(PageOpened(page_id="pg1")),
                                                   guideline("g1"), usage("b")]))
    s2 = repo.commit(s0, Commit(wiki=[usage("x")]))
    assert s2.wiki_seq == s1.wiki_seq + 1
    assert [u.action for u in s2.state.usage] == ["a", "b", "x"]
    assert same(repo.load("u1"), s2)


def test_first_commit_on_a_stale_unstarted_snapshot_conflicts(repo):
    s0 = repo.load("u1")
    repo.commit(repo.load("u1"), Commit(wiki=[page_saved("p1")]))
    with pytest.raises(WikiConflict):
        repo.commit(s0, Commit(wiki=[page_saved("p2")]))


def _bare(body) -> WikiEvent:
    return WikiEvent.model_construct(event_id="e", ts=T0, body=body)


def test_scopes_table():
    path = PathSaved.model_construct(
        record=PathRecord.model_construct(path=LearningPath.model_construct(path_id="q1")),
        action="planned")
    assert scopes(_bare(path)) == {"path:q1"}
    assert scopes(wev(PageOpened(page_id="p"))) == frozenset()
    assert scopes(usage("a")) == frozenset()
    assert scopes(page_saved("p1")) == {"page:p1"}
    assert scopes(wev(PagesTrashed(page_ids=("a", "b")))) == {"page:a", "page:b"}
    assert scopes(language_set()) == {"language"}
    assert scopes(graph_saved()) == {"graph"}
    assert scopes(groups_named("k1", "k2")) == {"group:k1", "group:k2"}
    assert scopes(wev(DraftClosed(draft_id="d1", outcome="answered"))) == {"draft:d1"}
    assert scopes(wev(WikiStarted(aspect_config=ASPECT_CONFIG_VERSION))) == {ANY}
    assert scopes(wev(PageMarksCleared(job_id="h_1"))) == {ANY}
    assert scopes(guideline("g")) == {"guidelines"}
    # a rule decision lands in its spec save's commit and is what the spec checkpoint reads
    from evowiki.wikiapp.events import RuleDecided
    assert scopes(wev(RuleDecided(decision_id="rd1", rule_id="si1", text="t", text_key="t",
                                  action="revoked", via="ui"))) == {"spec"}


def test_every_event_type_has_a_scope_rule():
    """A new body type falls to ANY (fail closed) until someone gives it a scope; the table
    names every type it knows so this list stays honest."""
    from typing import get_args

    from evowiki.wikiapp.events import WikiEventBody
    union = get_args(get_args(WikiEventBody)[0])
    assert len(union) == 46, "a new wiki event type: give it a scope in repo.scopes"
