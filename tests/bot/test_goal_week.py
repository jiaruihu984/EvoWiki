"""The weekly review's facts (bot.goal_week.load_goal_week, B5 spec §1.2) on a real store."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from evowiki.belief.aspects import Claim, Declare, DefineConcept, Observe
from evowiki.bot.facts import load_facts
from evowiki.bot.goal_week import load_goal_week, window
from evowiki.bot.settings import DEFAULT_SETTINGS
from evowiki.goals.contracts import Aspect, Observation
from evowiki.goals.path import LearningPath, PathPage
from evowiki.goals.path_edit import MoveEdit, RemoveEdit
from evowiki.nodes.model import NodeGraph, NodeRec, Relation
from evowiki.store import bot_goals
from evowiki.wikiapp.events import (
    GraphSaved,
    PageOpened,
    PageRecord,
    PageSaved,
    PathRecord,
    PathSaved,
    WikiEvent,
)
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 19, 0, 30, tzinfo=UTC)       # Monday 08:30 in Asia/Shanghai
_n = iter(range(100_000))


def eid(p="wev"):
    return f"{p}_{next(_n)}"


def page(i: int, node: str) -> PathPage:
    return PathPage(page_id=f"path-k-p{i}", targets=(node,), concepts=(node,),
                    objective=f"学 {node}", new_cells=1, title=f"第{i}页 {node}")


def path(nodes=("c1", "c2", "c3", "c0")) -> LearningPath:
    return LearningPath(path_id="path-k", goal="学统计", goal_key="k", split=True, source="llm",
                        capacity=12, new_cells=4, concepts=(),
                        pages=tuple(page(i, n) for i, n in enumerate(nodes, 1)))


def wiki_page(pid: str, node: str, *, done: bool, created: datetime) -> PageRecord:
    return PageRecord(page_id=pid, title=f"页 {node}", goal="g", concepts=(node,),
                      targets=(node,), done=done, created_at=created)


def wrong(cell, ref, at):
    return Observe(event_id=eid("ae"), ts=at, observation=Observation(
        cell=cell, polarity="-", strength=0.8, kind="check", evidence_ref=ref))


def make_repo(pool):
    make_user(pool, "u1")
    r = PgRepo(pool)
    graph = NodeGraph(nodes=tuple(NodeRec(id=f"c{i}", name=f"概念{i}") for i in range(5)),
                      relations=(Relation(kind="prerequisite", src="c0", dst="c2"),))
    r.commit(r.load("u1"), Commit(
        aspect=[DefineConcept(event_id=f"ae_d{i}", ts=NOW - timedelta(days=40),
                              concept_id=f"c{i}", aspects=(Aspect.INTERPRET,))
                for i in range(5)],
        wiki=[WikiEvent(event_id="wev_g", ts=NOW - timedelta(days=40),
                        body=GraphSaved(graph=graph, action="goal_nodes"))]))
    return r


@pytest.fixture
def repo(pool):
    return make_repo(pool)


def seed(repo, *, nodes=("c1", "c2", "c3", "c0"), edits=(), first_done_days=3):
    created = NOW - timedelta(days=20)
    rec = PathRecord(path=path(nodes), page_ids=("pg_1",) + (None,) * (len(nodes) - 1),
                     created_at=created, edits=tuple(edits))
    wiki = [
        WikiEvent(event_id=eid(), ts=created, body=PageSaved(
            record=wiki_page("pg_1", nodes[0], done=False, created=created), action="created")),
        WikiEvent(event_id=eid(), ts=created, body=PathSaved(record=rec, action="page_started")),
        WikiEvent(event_id=eid(), ts=NOW - timedelta(days=first_done_days), body=PageSaved(
            record=wiki_page("pg_1", nodes[0], done=True, created=created), action="done")),
        WikiEvent(event_id=eid(), ts=NOW - timedelta(days=2), body=PageOpened(page_id="pg_1")),
    ]
    repo.commit(repo.load("u1"), Commit(wiki=wiki))


def goal_week(pool, **kw):
    with pool.connection() as conn:
        return load_goal_week(conn, "u1", now=NOW, settings=DEFAULT_SETTINGS, **kw)


def test_window_is_seven_local_days_before_today():
    start, first, last = window(NOW, "Asia/Shanghai")
    assert (first, last) == (date(2026, 10, 12), date(2026, 10, 19))
    assert start == datetime(2026, 10, 11, 16, 0, tzinfo=UTC)


def test_progress_next_page_and_a_weak_prerequisite_taught_later(pool, repo):
    seed(repo)
    answers = [wrong("c0.interpret", f"check:pg_x:s{i}", NOW - timedelta(days=1, hours=i))
               for i in range(4)]
    # 粗心答错 on the latest: no 「建议先复习」 speaks for it, the weekly review does
    repo.commit(repo.load("u1"), Commit(aspect=[*answers, Claim(
        event_id=eid("ae"), ts=NOW - timedelta(hours=20), claim="slip", cell="c0.interpret",
        source_event_id=answers[-1].event_id)]))
    [g] = goal_week(pool)
    assert (g.path_id, g.title, g.pages_shown, g.pages_done, g.done_this_week) == (
        "path-k", "学统计", 4, 1, 1)
    assert (g.next_key, g.next_page_id, g.next_title, g.next_position) == (
        "path-k-p2", None, "第2页 c2", 2)
    assert g.new_pages_this_week == 0 and g.active_days == 2     # opened 10-17, answers 10-18
    assert g.wrong_cells == ("概念0（解读结果）",)
    [w] = g.weak_prereqs
    assert (w.node_id, w.name, w.taught, w.page_key, w.page_title, w.for_title) == (
        "c0", "概念0", "page", "path-k-p4", "第4页 c0", "第2页 c2")
    assert g.reading_seconds is None and g.stuck is None           # never reported: unknown
    assert g.days_since_open == 2 and g.idle_declines == 0 and g.last_week is None


def test_a_prerequisite_no_page_teaches_is_missing(pool, repo):
    seed(repo, nodes=("c1", "c2", "c3"))
    repo.commit(repo.load("u1"), Commit(aspect=[
        wrong("c0.interpret", "review:pg_x:s1", NOW - timedelta(days=1))]))
    [g] = goal_week(pool)
    [w] = g.weak_prereqs
    assert (w.taught, w.page_key) == ("missing", None)


def test_pages_the_user_moved_or_removed_are_left_alone(pool, repo):
    seed(repo, edits=(MoveEdit(page_key="path-k-p4", to_index=3),))
    repo.commit(repo.load("u1"), Commit(aspect=[
        wrong("c0.interpret", "review:pg_x:s1", NOW - timedelta(days=1))]))
    assert goal_week(pool)[0].weak_prereqs[0].taught == "fixed"
    rec = repo.load("u1").state.paths["path-k"]
    removed = rec.model_copy(update={"edits": (RemoveEdit(page_key="path-k-p4"),)})
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=eid(), ts=NOW - timedelta(hours=1), body=PathSaved(record=removed,
                                                                     action="user_edit"))]))
    # removed by the user: never re-added either
    assert goal_week(pool)[0].weak_prereqs[0].taught == "fixed"


def test_no_weak_prerequisite_without_evidence(pool, repo):
    seed(repo)
    assert goal_week(pool)[0].weak_prereqs == ()


def test_parked_and_finished_goals_are_not_reviewed(pool, repo):
    seed(repo)
    with pool.connection() as conn:
        bot_goals.park(conn, "u1", "path-k", NOW)
    assert goal_week(pool) == ()
    with pool.connection() as conn:                  # no goal_idle card for it either
        assert load_facts(conn, "u1", now=NOW, settings=DEFAULT_SETTINGS).goals == ()
        bot_goals.resume(conn, "u1", "path-k", NOW)
        assert [g.path_id for g in load_facts(conn, "u1", now=NOW,
                                              settings=DEFAULT_SETTINGS).goals] == ["path-k"]
    assert len(goal_week(pool)) == 1


def test_reading_time_known_only_when_reports_cover_the_window(pool, repo):
    seed(repo)
    with pool.connection() as conn:
        bot_goals.add_reading(conn, "u1", "b1", [("pg_1", date(2026, 10, 15), 1200)], NOW)
    g = goal_week(pool)[0]
    assert g.reading_seconds is None                               # first report after the start
    with pool.connection() as conn:
        bot_goals.add_reading(conn, "u1", "b0", [("pg_1", date(2026, 10, 1), 60)], NOW)
    g = goal_week(pool)[0]
    assert g.reading_seconds == 1200 and g.stuck is None           # pg_1 is done: not stuck


def test_stuck_on_an_unfinished_page(pool, repo):
    seed(repo)
    rec = repo.load("u1").state.paths["path-k"]
    started = rec.model_copy(update={"page_ids": ("pg_1", "pg_2", None, None)})
    repo.commit(repo.load("u1"), Commit(wiki=[
        WikiEvent(event_id=eid(), ts=NOW - timedelta(days=4), body=PageSaved(
            record=wiki_page("pg_2", "c2", done=False, created=NOW - timedelta(days=4)),
            action="created")),
        WikiEvent(event_id=eid(), ts=NOW - timedelta(days=4),
                  body=PathSaved(record=started, action="page_started"))]))
    with pool.connection() as conn:
        bot_goals.add_reading(conn, "u1", "b0", [("pg_1", date(2026, 10, 1), 60)], NOW)
        bot_goals.add_reading(conn, "u1", "b1", [("pg_2", date(2026, 10, 16), 2400),
                                                 ("pg_2", date(2026, 10, 17), 1500)], NOW)
    g = goal_week(pool)[0]
    assert g.reading_seconds == 3900 and g.new_pages_this_week == 1
    assert (g.stuck.page_id, g.stuck.seconds) == ("pg_2", 3900)
    assert g.next_page_id == "pg_2"


def test_last_weeks_snapshot_and_declined_idle_cards(pool, repo):
    seed(repo)
    with pool.connection() as conn:
        bot_goals.put_review(conn, "u1", week_start=date(2026, 10, 12), path_id="path-k",
                             situation="normal", facts={"pages_done": 0}, trigger_id=None, at=NOW)
        for i in range(2):
            conn.execute(
                "insert into bot_suggestion (suggestion_id, user_id, kind, title, why, action, "
                "placement, priority, status, dedup_key, expires_at, created_at) values "
                "(%s, 'u1', 'goal_idle', 't', 'w', '{}', 'today', 40, 'not_interested', "
                "'goal_idle:path-k', %s, %s)", (f"s{i}", NOW, NOW - timedelta(days=3 + i)))
    g = goal_week(pool, week_start=date(2026, 10, 19))[0]
    assert g.last_week == {"pages_done": 0} and g.idle_declines == 2
    assert replace(g, last_week=None).path_id == "path-k"


# --- the Bot acts only on a prerequisite the path does not already teach in order (review fix 1) --

def regraph(repo, *rels):
    graph = NodeGraph(nodes=tuple(NodeRec(id=f"c{i}", name=f"概念{i}") for i in range(5)),
                      relations=tuple(Relation(kind="prerequisite", src=s, dst=d) for s, d in rels))
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=eid(), ts=NOW - timedelta(days=30), body=GraphSaved(graph=graph,
                                                                     action="goal_nodes"))]))


def weak_c0(repo):
    repo.commit(repo.load("u1"), Commit(aspect=[
        wrong("c0.interpret", "review:pg_x:s1", NOW - timedelta(days=1))]))


def test_the_teacher_is_the_next_page_nothing_to_do(pool, repo):
    """p1 c1 done, p2 c0 next, p3 c2 needs c0, c0 answered wrong: already in order."""
    seed(repo, nodes=("c1", "c0", "c2"))
    weak_c0(repo)
    [g] = goal_week(pool)
    assert g.next_key == "path-k-p2"
    [w] = g.weak_prereqs
    assert (w.taught, w.before_key) == ("ordered", "path-k-p3")


def test_the_teacher_already_precedes_the_page_that_needs_it(pool, repo):
    seed(repo, nodes=("c1", "c3", "c0", "c2"))
    weak_c0(repo)
    [w] = goal_week(pool)[0].weak_prereqs
    assert w.taught == "ordered"


def test_the_edit_goes_before_the_page_that_needs_it_not_before_next(pool, repo):
    seed(repo, nodes=("c1", "c3", "c2", "c4", "c0"))
    weak_c0(repo)
    [g] = goal_week(pool)
    assert g.next_key == "path-k-p2"
    [w] = g.weak_prereqs
    assert (w.taught, w.page_key, w.before_key) == ("page", "path-k-p5", "path-k-p3")


def test_never_moved_ahead_of_a_page_it_depends_on(pool, repo):
    """c0 (p4) needs c3 (p3); moving p4 before p2 would jump over p3."""
    regraph(repo, ("c0", "c2"), ("c3", "c0"))
    seed(repo, nodes=("c1", "c2", "c3", "c0"))
    weak_c0(repo)
    [w] = goal_week(pool)[0].weak_prereqs
    assert w.taught == "fixed"


def test_a_bare_declared_gap_is_not_evidence(pool, repo):
    seed(repo)
    repo.commit(repo.load("u1"), Commit(aspect=[
        Declare(event_id=eid("ae"), ts=NOW - timedelta(days=1), cell="c0.interpret",
                value="dont")]))
    assert goal_week(pool)[0].weak_prereqs == ()


def test_a_wrong_answer_still_standing_is_left_to_the_next_steps_suggestion(pool, repo):
    seed(repo)
    repo.commit(repo.load("u1"), Commit(aspect=[
        wrong("c0.interpret", "check:pg_x:s1", NOW - timedelta(days=1))]))
    [g] = goal_week(pool)
    assert g.weak_prereqs == ()
