"""Goal completion: real PostgreSQL and scripted questions; no network calls."""

import pytest

from evowiki.goals.path import LearningPath, PathPage
from evowiki.store.locks import session_lock
from evowiki.wikiapp.events import PathRecord, PathSaved, WikiEvent
from evowiki.wikiapp.followup import PURPOSE
from evowiki.wikiapp.goal_check import PREFIX, complete_page, goal_cells
from evowiki.wikiapp.kchange import answer_check
from evowiki.wikiapp.llm import LLMFailed, LLMUnavailable
from evowiki.wikiapp.page import mark_done
from evowiki.wikiapp.repo import Commit, PgRepo, WikiConflict
from tests.runtime.helpers import make_user
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized

QUESTION = {"stem": "哪个数值等于二加三的结果？", "options": ["五", "六", "七"], "answer": 0}


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    repo, script, ids = PgRepo(pool), Script(), Ids()
    pid = organized((repo, script, ids))
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    path = LearningPath(path_id="goal1", goal="完整目标", goal_key="goal1", split=False,
                        source="single", new_cells=3, concepts=(), pages=(PathPage(
                            page_id="p1", targets=rec.concepts, concepts=rec.concepts,
                            objective="学完全部知识点", new_cells=3),))
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=PathSaved(
        record=PathRecord(path=path, page_ids=(pid,), created_at=NOW), action="planned"))]))
    return repo, script, ids, pid


def finish(env, gateway=True):
    repo, script, ids, pid = env
    return complete_page(repo, script.gateway() if gateway else None, "u1", pid,
                         now=NOW, new_id=ids)


def checks(env):
    repo, _, _, pid = env
    return [s for s in repo.load("u1").state.pages[pid].page.outline.sections
            if s.id.startswith(PREFIX)]


def script_questions(env):
    repo, script, _, pid = env
    for _ in goal_cells(repo.load("u1"), pid, now=NOW):
        script.push(PURPOSE, QUESTION)


def test_verified_questions_are_distinct_and_completion_does_not_claim_mastery(env):
    repo, script, _, pid = env
    before = repo.load("u1")
    script_questions(env)
    finish(env)
    sections = checks(env)
    assert len(sections) == 3 and len({s.check.cell for s in sections}) == 3
    assert all(s.check.verified for s in sections)
    after = repo.load("u1")
    assert after.state.pages[pid].done
    for s in sections:
        assert after.kview(NOW).cell(s.check.cell).direct == before.kview(NOW).cell(s.check.cell).direct
    calls = script.calls(PURPOSE)
    finish(env, gateway=False)
    assert script.calls(PURPOSE) == calls and checks(env) == sections


def test_correct_wrong_and_duplicate_answers_use_existing_evidence(env):
    repo, _, ids, pid = env
    script_questions(env)
    finish(env)
    right, wrong = checks(env)[:2]
    before = repo.load("u1").kview(NOW).cell(right.check.cell).m
    out = answer_check(repo, None, "u1", pid, right.id, right.check.answer, now=NOW, new_id=ids)
    assert out.correct is True and repo.load("u1").kview(NOW).cell(right.check.cell).m > before
    seq = repo.load("u1").aspect_seq
    with pytest.raises(WikiConflict):
        answer_check(repo, None, "u1", pid, right.id, right.check.answer, now=NOW, new_id=ids)
    assert repo.load("u1").aspect_seq == seq
    before = repo.load("u1").kview(NOW).cell(wrong.check.cell).m
    out = answer_check(repo, None, "u1", pid, wrong.id,
                       (wrong.check.answer + 1) % len(wrong.check.options), now=NOW, new_id=ids)
    assert out.correct is False and repo.load("u1").kview(NOW).cell(wrong.check.cell).m < before


def test_failed_generation_is_atomic_and_retryable(env):
    repo, script, _, pid = env
    script.push(PURPOSE, {**QUESTION, "answer": 99})
    before = repo.load("u1")
    with pytest.raises(LLMFailed):
        finish(env)
    assert repo.load("u1").wiki_seq == before.wiki_seq
    assert not repo.load("u1").state.pages[pid].done and checks(env) == []
    script_questions(env)
    finish(env)
    assert checks(env)


def test_missing_model_fails_and_old_done_page_can_retry(env):
    repo, _, ids, pid = env
    with pytest.raises(LLMUnavailable):
        finish(env, gateway=False)
    mark_done(repo, "u1", pid, now=NOW, new_id=ids)
    before = repo.load("u1")
    script_questions(env)
    finish(env)
    assert checks(env)
    after = repo.load("u1")
    assert after.aspect_seq == before.aspect_seq
    assert after.wiki_seq == before.wiki_seq + 1  # only the page's questions; no second done usage


def test_goal_cells_exclude_prerequisites_outside_the_goal_targets(env):
    repo, _, ids, pid = env
    snap = repo.load("u1")
    path = snap.state.paths["goal1"]
    page = path.path.pages[0].model_copy(update={"targets": ("c1",)})
    path = path.model_copy(update={"path": path.path.model_copy(update={"pages": (page,)})})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                            body=PathSaved(record=path, action="user_edit"))]))
    assert set(goal_cells(repo.load("u1"), pid, now=NOW)) == {"c1.procedure", "c1.interpret"}
    script_questions(env)
    finish(env)
    assert len(checks(env)) == 2


def test_completion_session_lock_excludes_duplicates_without_open_transaction(pool):
    with session_lock(pool, "goal-check:test") as first:
        assert first
        with session_lock(pool, "goal-check:test") as second:
            assert not second
        with pool.connection() as conn:
            row = conn.execute("select count(*) as n from pg_stat_activity "
                                   "where datname = current_database() and state = 'idle in transaction' "
                                   "and pid != pg_backend_pid()").fetchone()
            assert row["n"] == 0
    with session_lock(pool, "goal-check:test") as third:
        assert third
