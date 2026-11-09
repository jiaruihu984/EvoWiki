"""load_at, commit(once=) (exactly once, key before version check), DurableRepo."""
import pytest

from evowiki.agent import durable
from evowiki.agent.effects import EffectConflict
from evowiki.wikiapp.durable_repo import DurableRepo
from evowiki.wikiapp.repo import Commit, MemoryRepo, PgRepo, WikiConflict
from tests.runtime.helpers import make_user
from tests.wikiapp.test_repo import define, observe, same, usage


def _two_commits(repo):
    s0 = repo.load("u1")
    s1 = repo.commit(s0, Commit(aspect=[define("c1")], wiki=[usage("a")]))
    s2 = repo.commit(s1, Commit(aspect=[observe("c1.term")], wiki=[usage("b")]))
    return s1, s2


def test_memory_load_at_is_the_old_snapshot():
    repo = MemoryRepo(["u1"])
    s1, s2 = _two_commits(repo)
    old = repo.load_at("u1", s1.wiki_seq, s1.aspect_seq, s1.infer_version)
    assert same(old, s1) and not same(old, s2)


def test_pg_load_at_is_the_old_snapshot(pool):
    make_user(pool, "u1")
    repo = PgRepo(pool)
    s1, s2 = _two_commits(repo)
    old = repo.load_at("u1", s1.wiki_seq, s1.aspect_seq, s1.infer_version)
    assert same(old, s1) and not same(old, s2)
    assert old.k.to_json() == s1.k.to_json() != s2.k.to_json()
    assert same(repo.load_at("u1", s2.wiki_seq, s2.aspect_seq, s2.infer_version), repo.load("u1"))


def test_pg_load_at_unknown_version_is_lookup_error(pool):
    make_user(pool, "u1")
    with pytest.raises(LookupError):
        PgRepo(pool).load_at("u1", 0, 0, 7)


def _n_wiki(pool) -> int:
    with pool.connection() as conn:
        return conn.execute("select count(*) from wiki_event").fetchone()['count']


def test_commit_once_applies_once_and_skips_the_version_check(pool):
    make_user(pool, "u1")
    repo = PgRepo(pool)
    snap = repo.load("u1")
    c = Commit(aspect=[define("c1")], wiki=[usage("a")])
    first = repo.commit(snap, c, once="k1")
    n = _n_wiki(pool)
    again = repo.commit(snap, c, once="k1")      # snap is stale now: no WikiConflict
    assert same(again, first) and _n_wiki(pool) == n
    with pytest.raises(WikiConflict):             # without a key the stale snapshot does conflict
        repo.commit(snap, Commit(aspect=[define("c2")]))


def test_commit_once_same_key_other_payload_conflicts(pool):
    make_user(pool, "u1")
    repo = PgRepo(pool)
    snap = repo.load("u1")
    repo.commit(snap, Commit(wiki=[usage("a")]), once="k1")
    with pytest.raises(EffectConflict):
        repo.commit(snap, Commit(wiki=[usage("other")]), once="k1")


def test_commit_once_rolls_back_with_the_commit(pool):
    make_user(pool, "u1")
    repo = PgRepo(pool)

    def boom(conn):
        raise RuntimeError("extra failed")

    with pytest.raises(RuntimeError):
        repo.commit(repo.load("u1"), Commit(wiki=[usage("a")], extra=[boom]), once="k1")
    assert _n_wiki(pool) == 0
    with pool.connection() as conn:
        assert conn.execute("select count(*) from effect_applied where key = 'k1'"
                            ).fetchone()['count'] == 0


def test_memory_commit_once_mirrors_pg():
    repo = MemoryRepo(["u1"])
    snap = repo.load("u1")
    c = Commit(aspect=[define("c1")], wiki=[usage("a")])
    first = repo.commit(snap, c, once="k1")
    assert same(repo.commit(snap, c, once="k1"), first)
    assert len(repo.wiki_log("u1")) == first.wiki_seq
    with pytest.raises(EffectConflict):
        repo.commit(snap, Commit(wiki=[usage("x")]), once="k1")


def test_durable_repo_outside_a_workflow_delegates():
    inner = MemoryRepo(["u1"])
    repo = DurableRepo(inner)
    s = repo.load("u1")
    out = repo.commit(s, Commit(wiki=[usage("a")]))
    assert out.wiki_seq == 2 and same(inner.load("u1"), out)
    assert [e.body.type for e in repo.wiki_events_of("u1", {"usage_added"})] == ["usage_added"]


def test_durable_repo_load_and_commit_are_steps_in_a_workflow(monkeypatch):
    names: list[str] = []
    real = durable.step_ran
    monkeypatch.setattr("evowiki.wikiapp.durable_repo.step_ran",
                        lambda n, fn: (names.append(n), real(n, fn))[1])
    inner = MemoryRepo(["u1"])
    seen: list[str | None] = []
    real_commit = inner.commit
    inner.commit = lambda s, c, *, once=None: (seen.append(once), real_commit(s, c, once=once))[1]
    repo = DurableRepo(inner)
    with durable.virtual_workflow("wf"):
        s = repo.load("u1")
        out = repo.commit(s, Commit(aspect=[define("c1")], wiki=[usage("a")]))
    assert names == ["repo.load", "repo.commit"]
    assert seen == ["wf:1"]
    assert same(out, inner.load("u1"))


def test_durable_repo_replayed_load_uses_load_at(monkeypatch):
    inner = MemoryRepo(["u1"])
    s1, _ = _two_commits(inner)
    calls = []
    real = inner.load_at
    inner.load_at = lambda *a: (calls.append(a), real(*a))[1]
    monkeypatch.setattr("evowiki.wikiapp.durable_repo.step_ran",
                        lambda n, fn: ([s1.wiki_seq, s1.aspect_seq, 1], False))
    with durable.virtual_workflow("wf"):
        got = DurableRepo(inner).load("u1")
    assert calls == [("u1", s1.wiki_seq, s1.aspect_seq, 1)] and same(got, s1)


def test_durable_repo_events_roundtrip_and_wiki_conflict_keeps_its_class():
    inner = MemoryRepo(["u1"])
    repo = DurableRepo(inner)
    with durable.virtual_workflow("wf"):
        s = repo.load("u1")
        repo.commit(s, Commit(aspect=[define("c1")], wiki=[usage("a")]))
        assert [e.body.type for e in repo.wiki_events_of("u1", {"usage_added"})] == ["usage_added"]
        assert [(q, e.body.type) for q, e in repo.wiki_events_since("u1", {"usage_added"}, 0)
                ] == [(2, "usage_added")]
        assert [e.type for e in repo.aspect_events("u1")] == ["define_concept"]
        with pytest.raises(WikiConflict):          # s is stale now
            repo.commit(s, Commit(aspect=[define("c2")]))
