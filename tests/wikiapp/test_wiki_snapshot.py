"""The wiki state cache (spec 2026-10-28-current-state, option B; migration 0078): a commit stores
the replayed state with its seq in the same transaction; a load reads it and replays only the
events after it; a snapshot from other code (another reducer fingerprint) is ignored. The event log
stays the source: every load equals a full replay. Scripted LLM only."""

import pytest

from evowiki.store.wiki import last_wiki_seq, wiki_events, wiki_snapshot
from evowiki.wikiapp.events import PageOpened, WikiEvent
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.repo import Commit, PgRepo, _from_row, reducer_fingerprint
from evowiki.wikiapp.state import WikiState, replay
from tests.runtime.helpers import make_user
from tests.wikiapp.goal_script import auto_probe
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_progress import enter, goal6, write_body


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    s = Script()
    auto_probe(s)
    return PgRepo(pool), s, Ids()


def full_replay(pool) -> WikiState:
    with pool.connection() as conn:
        return replay(_from_row(e.event_id, e.occurred_at, e.payload)
                      for e in wiki_events(conn, "u1"))


def test_a_load_from_the_snapshot_equals_the_full_replay(env, pool):
    repo, _, ids = env
    map_id = goal6(env)
    out = enter(env, map_id, "c1", ["c7"], ["c8"])
    first = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
    write_body(env, first.page_id)
    with pool.connection() as conn:
        row = wiki_snapshot(conn, "u1")
        assert row is not None and row.wiki_seq == last_wiki_seq(conn, "u1")
        assert row.reducer == reducer_fingerprint()
    assert repo.load("u1").state.to_json() == full_replay(pool).to_json()


def test_events_after_the_snapshot_are_replayed_on_top(env, pool):
    repo = env[0]
    goal6(env)
    with pool.connection() as conn:
        before = wiki_snapshot(conn, "u1").wiki_seq
        # an event the cache never saw (written with the snapshot row rolled back)
        conn.execute("update wiki_snapshot set wiki_seq = wiki_seq - 1, state = %s "
                     "where user_id = 'u1'", (_state_before_last(pool),))
    loaded = repo.load("u1")
    assert loaded.state.to_json() == full_replay(pool).to_json() and loaded.wiki_seq == before


def _state_before_last(pool):
    from psycopg.types.json import Jsonb
    with pool.connection() as conn:
        evs = wiki_events(conn, "u1")
    return Jsonb(replay(_from_row(e.event_id, e.occurred_at, e.payload)
                        for e in evs[:-1]).model_dump(mode="json"))


def test_a_snapshot_of_other_code_is_not_used_and_is_rewritten(env, pool):
    repo, _, ids = env
    goal6(env)
    with pool.connection() as conn:
        conn.execute("update wiki_snapshot set reducer = 'other', state = '{\"started\": false}' "
                     "where user_id = 'u1'")
    snap = repo.load("u1")
    assert snap.state.to_json() == full_replay(pool).to_json()
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                             body=PageOpened(page_id="p1"))]))
    with pool.connection() as conn:
        assert wiki_snapshot(conn, "u1").reducer == reducer_fingerprint()
    assert repo.load("u1").state.to_json() == full_replay(pool).to_json()
