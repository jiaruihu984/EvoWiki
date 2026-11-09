"""K1 scenario (Task 8): a big goal → learning path, path pages organized on demand, the
review_per_day setting, a review 58 days later graded 「没想起来」, 「学完」 on a path page, a
second big goal's first page. The runtime state the last commit returned equals the replay of the
stored wiki log (every prefix replays), and the same run on PgRepo ends in the same wiki state and
K as MemoryRepo."""

from typing import Any

import pytest

from evowiki.belief.aspects import Observe
from evowiki.goals.contracts import Observation
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import organize_stream
from evowiki.wikiapp.paths import next_path_page, path_page_done
from evowiki.wikiapp.repo import Commit, MemoryRepo, PgRepo, Repo
from evowiki.wikiapp.review import grade_review, review_today
from evowiki.wikiapp.settings import put_settings
from evowiki.wikiapp.state import replay
from evowiki.wikiapp.wiki_schemas import ReviewGradeIn, SettingsIO
from tests.runtime.helpers import T0, FixedClock, make_user
from tests.wikiapp.e2e_driver import USER, Ids, RecordingRepo
from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
from tests.wikiapp.fake_writer import WRITE, writing
from tests.wikiapp.flat_goal import flat_goal
from tests.wikiapp.runs import runs_for
from tests.wikiapp.script import Script
from tests.wikiapp.test_organize import text
from tests.wikiapp.test_paths import BIG_INTAKE, BIG_PATH, GOAL

LEARNED = ("c1.term", "c1.interpret")   # three quiz `+` each → 确定会, recall < 0.6 after 58 d
LEARN_EVENTS = 3
LATER_DAYS = 63                          # tests/wikiapp/test_review_flow.py LATER
GOAL2 = GOAL + "（第二遍）"               # another goal text: another path (goal_key differs)


ASPECTS4 = ("term", "purpose", "notation", "interpret")   # tests/wikiapp/test_paths.py


def _outline(*sections: tuple[str, str, str]) -> dict:
    """One explain section per target concept, covering all its cells (organizer rule 1)."""
    return {"profile": "从零开始", "assumed": [],
            "sections": [{"id": sid, "heading": h, "role": "explain",
                          "cells": [f"{c}.{a}" for a in ASPECTS4], "why": "目标概念，没证据"}
                         for sid, h, c in sections]}


PAGE1 = _outline(("s1", "总体与样本", "c1"), ("s2", "抽样分布", "c2"), ("s3", "零假设", "c4"))
PAGE2 = _outline(("s1", "置信区间", "c3"), ("s2", "检验统计量", "c5"))
PAGE3 = _outline(("s1", "p 值", "c6"))
def k1_script() -> Script:
    s = Script()
    s.push("goals.intake", BIG_INTAKE).push("goals.path", BIG_PATH)
    for outline in (PAGE1, PAGE2, PAGE3):
        s.push(ORGANIZE, outline)
        for sec in outline["sections"]:
            s.push(WRITE, text(sec["id"]))
    s.push("goals.intake", BIG_INTAKE).push("goals.path", BIG_PATH)     # the second goal
    s.push(ORGANIZE, PAGE1)       # its first page: page 1's sections again, all from the cache
    return s


def run_k1(repo: Repo, script: Script, clock: FixedClock, ids: Ids) -> dict[str, Any]:
    out: dict[str, Any] = {}
    u = USER
    gateway = script.gateway()

    def gw() -> CountingGateway:                   # one counter per request, like the routes
        return CountingGateway(gateway)

    def organize(page_id: str) -> None:
        items = list(organize_stream(repo, gw(), u, page_id, now=clock(), new_id=ids,
                                     writing=writing(repo, script), runs=runs_for(repo)))
        assert items[-1][0] == "done", items[-1]
        assert items[0][1].source == "organizer" and items[0][1].attempts == 1

    # 1. big goal → a path (no questions, no page)
    res = flat_goal(repo, gw(), u, GOAL, "", now=clock(), new_id=ids)
    assert res.path_id is not None and res.page_id is None
    path_id = out["path_id"] = res.path_id

    # 2. pages 1 and 2 on demand, organized
    clock.advance(minutes=1)
    p1 = out["p1"] = next_path_page(repo, u, path_id, now=clock(), new_id=ids).page_id
    p2 = out["p2"] = next_path_page(repo, u, path_id, now=clock(), new_id=ids).page_id
    clock.advance(minutes=5)
    organize(p1)

    # 3. page 2
    clock.advance(minutes=5)
    organize(p2)

    # 4. review_per_day = 3; page 1's cells learned (quiz results), then 58 days later reviewed
    out["settings"] = put_settings(repo, u, SettingsIO(review_per_day=3), now=clock(),
                                   new_id=ids)
    for i in range(LEARN_EVENTS):
        snap = repo.load(u)
        repo.commit(snap, Commit(aspect=[
            Observe(event_id=ids("aev"), ts=clock(), observation=Observation(
                cell=c, polarity="+", strength=1.0, kind="quiz", evidence_ref=f"quiz:t{i}"))
            for c in LEARNED]))
    clock.advance(days=LATER_DAYS)
    today = out["today"] = review_today(repo, u, now=clock())
    assert today.limit == 3 and today.items, today
    first = today.items[0]
    assert first.cell in LEARNED and first.page_id == p1
    out["grade"] = grade_review(repo, gw(), u, ReviewGradeIn(
        cell=first.cell, grade="forgot", page_id=first.page_id, section_id=first.section_id),
        now=clock(), new_id=ids)
    obs = [e for e in repo.aspect_events(u) if isinstance(e, Observe)][-1].observation
    assert (obs.kind, obs.polarity, obs.cell) == ("recall", "-", first.cell)

    # 5. 「学完」 on path page 1
    clock.advance(minutes=10)
    out["done"] = path_page_done(repo, u, path_id, 1, now=clock(), new_id=ids)

    # 6. page 3 on demand
    clock.advance(minutes=1)
    p3 = out["p3"] = next_path_page(repo, u, path_id, now=clock(), new_id=ids).page_id
    organize(p3)

    # 7. a second big goal and its first page
    clock.advance(minutes=1)
    res2 = flat_goal(repo, gw(), u, GOAL2, "", now=clock(), new_id=ids)
    assert res2.path_id is not None and res2.path_id != path_id
    q1 = out["q1"] = next_path_page(repo, u, res2.path_id, now=clock(), new_id=ids).page_id
    organize(q1)
    assert script.pending() == {}, script.pending()
    return out


@pytest.mark.skip(reason="Task 3 rebuilds the goal flow")
def test_k1_memory_runtime_equals_replay():
    repo = RecordingRepo(MemoryRepo([USER]))
    run_k1(repo, k1_script(), FixedClock(T0), Ids())
    log = repo.inner.wiki_log(USER)
    assert repo.last is not None
    assert replay(log).to_json() == repo.last.state.to_json()
    for i in range(1, len(log) + 1):                      # pure fold: every prefix replays
        replay(log[:i])


@pytest.mark.skip(reason="Task 3 rebuilds the goal flow")
def test_k1_pg_run_equals_memory_run(pool):
    memory = RecordingRepo(MemoryRepo([USER]))
    run_k1(memory, k1_script(), FixedClock(T0), Ids())
    make_user(pool, USER)
    pg = RecordingRepo(PgRepo(pool))
    run_k1(pg, k1_script(), FixedClock(T0), Ids())
    a, b = PgRepo(pool).load(USER), memory.inner.load(USER)
    assert a.state.to_json() == b.state.to_json()
    assert a.k.to_json() == b.k.to_json()
    assert pg.last is not None and pg.last.state.to_json() == a.state.to_json()
