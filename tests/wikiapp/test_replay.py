"""The z-score → PCA scenario run on MemoryRepo and on PgRepo (fresh script / gateway each, same
ids and clock) ends in the same wiki state and K; replaying Pg's stored wiki events equals the
runtime state the last commit returned, and every stored prefix replays (pure fold)."""

from evowiki.store.wiki import last_wiki_seq, wiki_events
from evowiki.wikiapp.events import WikiEvent
from evowiki.wikiapp.repo import MemoryRepo, PgRepo
from evowiki.wikiapp.state import replay
from tests.runtime.helpers import T0, FixedClock, make_user
from tests.wikiapp.e2e_driver import USER, Ids, RecordingRepo, load_script, run_scenario
from tests.wikiapp.fake_writer import writing


def _run(repo) -> RecordingRepo:
    rec = RecordingRepo(repo)
    script = load_script()
    run_scenario(rec, script.gateway(), FixedClock(T0), Ids(), writing(rec, script))
    return rec


def _events(conn, upto_seq=None) -> list[WikiEvent]:
    return [WikiEvent.model_validate({"event_id": s.event_id, "ts": s.occurred_at,
                                      "body": s.payload})
            for s in wiki_events(conn, USER, upto_seq=upto_seq)]


def test_pg_run_equals_memory_run_and_replays(pool):
    memory = _run(MemoryRepo([USER]))
    make_user(pool, USER)
    pg = _run(PgRepo(pool))

    mem_snap = memory.inner.load(USER)
    pg_snap = PgRepo(pool).load(USER)
    assert pg_snap.state.to_json() == mem_snap.state.to_json()
    assert pg_snap.k.to_json() == mem_snap.k.to_json()
    assert (pg_snap.wiki_seq, pg_snap.aspect_seq) == (pg.last.wiki_seq, pg.last.aspect_seq)

    assert pg.last is not None and memory.last is not None
    assert pg.last.state.to_json() == memory.last.state.to_json()
    with pool.connection() as conn:
        assert replay(_events(conn)).to_json() == pg.last.state.to_json()
        last = last_wiki_seq(conn, USER)
        seqs = [s.seq for s in wiki_events(conn, USER)]
        assert seqs and seqs[-1] == last
        mem_log = memory.inner.wiki_log(USER)
        assert len(mem_log) == len(seqs)
        for i, seq in enumerate(seqs, 1):             # pure fold: every prefix replays
            prefix = _events(conn, upto_seq=seq)
            assert len(prefix) == i
            assert replay(prefix).to_json() == replay(mem_log[:i]).to_json()
