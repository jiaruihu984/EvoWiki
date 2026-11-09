"""K is built with the user's θ_infer,u (RSI track A): default = exactly as before, a committed
track-A version shows up in `load` / `commit`, a rollback restores the default."""

from datetime import UTC, datetime, timedelta

from evowiki.belief.aspects import DefineConcept, Observe
from evowiki.goals.contracts import Aspect, Observation, cell_id
from evowiki.store.rsi import PgRsiStore
from evowiki.teach import infer_paths as ip
from evowiki.teach import registry
from evowiki.teach.contracts import InferParams
from evowiki.teach.rsi_store import MemoryRsiStore
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from evowiki.wikiapp.repo import Commit, MemoryRepo, PgRepo, k_config
from tests.runtime.helpers import make_user

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
SELF = "evidence_weight.self"
CELL = cell_id("c1", Aspect.TERM)


def _events(tag: str) -> list:
    return [DefineConcept(event_id=f"{tag}-d", ts=T0, concept_id="c1",
                          aspects=(Aspect.TERM, Aspect.PURPOSE)),
            Observe(event_id=f"{tag}-o", ts=T0 + timedelta(minutes=1), observation=Observation(
                cell=CELL, polarity="+", strength=1.0, kind="self", evidence_ref=f"{tag}-r",
                quote="我会"))]


def _belief(snap):
    return snap.k.belief(CELL, T0 + timedelta(hours=1))


def test_k_config_default_is_the_v1_config_itself():
    assert k_config(InferParams()) is ASPECT_CONFIG_V1
    low = k_config(ip.set_(InferParams(), SELF, 0.3))
    assert ip.get(low.infer, SELF) == 0.3 and low != ASPECT_CONFIG_V1


def test_memory_repo_uses_the_users_infer():
    plain = MemoryRepo(["u1"])
    store = MemoryRsiStore()
    repo = MemoryRepo(["u1", "u2"], infer_for=lambda u: registry.infer_for(store, u))
    for r, users in ((plain, ["u1"]), (repo, ["u1", "u2"])):
        for u in users:
            r.commit(r.load(u), Commit(aspect=_events(u)))
    assert repo.load("u1").k.config is ASPECT_CONFIG_V1
    assert repo.load("u1").k.to_json() == plain.load("u1").k.to_json()
    before = _belief(repo.load("u1"))

    registry.commit(store, "u1", "A", infer=ip.set_(InferParams(), SELF, 0.3), patch_id="p1", now=T0)
    snap = repo.load("u1")
    assert ip.get(snap.k.config.infer, SELF) == 0.3
    assert _belief(snap) != before
    assert repo.load("u2").k.config is ASPECT_CONFIG_V1           # per user
    after = repo.commit(snap, Commit(aspect=[Observe(
        event_id="u1-more", ts=T0 + timedelta(minutes=2), observation=Observation(
            cell=CELL, polarity="+", strength=1.0, kind="quiz", evidence_ref="u1-more-r"))]))
    assert ip.get(after.k.config.infer, SELF) == 0.3

    registry.rollback(store, "u1", "A", 1, now=T0)
    assert repo.load("u1").k.config is ASPECT_CONFIG_V1


def test_pg_repo_reads_the_version_in_load_and_commit(pool):
    for u in ("u1", "u2"):
        make_user(pool, u)
    repo = PgRepo(pool)
    for u in ("u1", "u2"):
        repo.commit(repo.load(u), Commit(aspect=_events(u)))
    snap = repo.load("u1")
    assert snap.k.config is ASPECT_CONFIG_V1
    before = _belief(snap)

    store = PgRsiStore(pool)
    with store.transaction():
        registry.commit(store, "u1", "A", infer=ip.set_(InferParams(), SELF, 0.3), patch_id="p1",
                        now=T0)
    snap = repo.load("u1")
    assert ip.get(snap.k.config.infer, SELF) == 0.3 and _belief(snap) != before
    assert repo.load("u2").k.config is ASPECT_CONFIG_V1
    after = repo.commit(snap, Commit(aspect=[Observe(
        event_id="u1-more", ts=T0 + timedelta(minutes=2), observation=Observation(
            cell=CELL, polarity="+", strength=1.0, kind="quiz", evidence_ref="u1-more-r"))]))
    assert ip.get(after.k.config.infer, SELF) == 0.3
    assert after.k.to_json() == repo.load("u1").k.to_json()

    with store.transaction():
        registry.rollback(store, "u1", "A", 1, now=T0)
    assert repo.load("u1").k.config is ASPECT_CONFIG_V1
