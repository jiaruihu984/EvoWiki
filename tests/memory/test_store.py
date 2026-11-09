from datetime import timedelta

import pytest

from evowiki.memory.contracts import EvidenceRef
from evowiki.memory.store import Episode, InMemoryMemoryStore, Item, PgMemoryStore, tag
from evowiki.retrieval.embed import HashingEmbedder
from tests.memory.conftest import T, seed_user_and_session


@pytest.fixture(params=["mem", "pg"])
def store(request):
    if request.param == "mem":
        return InMemoryMemoryStore()
    pool = request.getfixturevalue("pool")
    seed_user_and_session(pool, "u1", "s1")
    seed_user_and_session(pool, "u2", "s2")
    return PgMemoryStore(pool)


def ep(uid="u1", sid="s1", mid="m1", at=T, text="atomic 怎么定", turn=0):
    return Episode(user_id=uid, session_id=sid, message_id=mid, answer_message_id=f"a-{mid}",
                   trace_run_id=f"assistant:{sid}:{mid}", turn_index=turn, page_id="p1",
                   concept_ids=("c1",), at=at, user_text=text, answer_text="由知识本身决定")


def item(iid, *, uid="u1", kind="decision", at=T, status="active", content="atomic 由知识决定"):
    return Item(item_id=iid, user_id=uid, kind=kind, content=content,
                provenance="explicit_current",
                evidence=(EvidenceRef(message_id="m1", session_id="s1", role="user", quote="就这样"),),
                scope_type="global", session_id="s1", valid_from=at, created_at=at,
                status=status if kind in ("decision", "open_question") else None)


def test_upsert_is_idempotent(store):
    store.upsert_episode(ep())
    store.upsert_episode(ep())
    assert len(store.episodes("u1")) == 1


def test_episodes_are_per_user_and_cut_by_as_of(store):
    store.upsert_episode(ep())
    store.upsert_episode(ep(mid="m2", at=T + timedelta(days=1), turn=1))
    store.upsert_episode(ep(uid="u2", sid="s2", mid="m9"))
    assert {e.message_id for e in store.episodes("u1")} == {"m1", "m2"}
    assert {e.message_id for e in store.episodes("u1", as_of=T + timedelta(hours=1))} == {"m1"}
    assert {e.message_id for e in store.episodes("u1", since=T + timedelta(hours=1))} == {"m2"}
    assert {e.message_id for e in store.episodes("u1", until=T + timedelta(hours=1))} == {"m1"}


def test_exclude_only_the_live_turns_of_the_current_session(store):
    store.upsert_episode(ep(mid="m1", at=T, turn=0))
    store.upsert_episode(ep(mid="m2", at=T + timedelta(1), turn=1))
    # turns at index >= first_kept (m2) are in the context verbatim; folded m1 stays searchable
    got = store.episodes("u1", exclude_sessions_from={"s1": 1})
    assert {e.message_id for e in got} == {"m1"}


def test_vectors_are_tagged_and_per_user(store):
    e = HashingEmbedder()
    store.upsert_episode(ep())
    store.upsert_episode(ep(uid="u2", sid="s2", mid="m9"))
    assert [x.message_id for x in store.episodes_without_vector("u1", tag(e), 10)] == ["m1"]
    store.set_episode_vector("s1", "m1", e.embed_query("x"), e)
    store.set_episode_vector("s2", "m9", e.embed_query("y"), e)
    assert list(store.episode_vectors("u1", tag(e))) == ["s1/m1"]
    assert store.episode_vectors("u1", tag(HashingEmbedder(dim=64))) == {}
    assert store.episodes_without_vector("u1", tag(e), 10) == []
    store.upsert_episode(ep())                              # a re-index keeps the vector
    assert list(store.episode_vectors("u1", tag(e))) == ["s1/m1"]
    store.put_item(item("i1"))
    store.set_item_vector("i1", e.embed_query("z"), e)
    assert list(store.item_vectors("u1", tag(e))) == ["i1"]
    assert store.item_vectors("u2", tag(e)) == {}


def test_creation_supersede_and_resolve_rebuild_with_as_of(store):
    store.put_item(item("a", at=T))
    assert store.items("u1", as_of=T - timedelta(hours=1)) == []           # before creation
    assert store.items("u1", as_of=T)[0].status == "active"
    store.put_item(item("b", at=T + timedelta(days=2)))
    store.set_item_status("a", "superseded", at=T + timedelta(days=2), by="consolidation",
                          superseded_by="b")
    before = store.items("u1", as_of=T + timedelta(days=1))
    assert [(i.item_id, i.status, i.superseded_by) for i in before] == [("a", "active", None)]
    now = {i.item_id: i for i in store.items("u1")}
    assert now["a"].status == "superseded" and now["a"].superseded_by == "b"
    assert [h["status"] for h in now["a"].status_history] == ["active", "superseded"]

    store.put_item(item("q", kind="open_question", at=T))
    store.set_item_status("q", "resolved", at=T + timedelta(days=3), by="consolidation")
    assert store.get_item("u1", "q").status == "resolved"
    mid = {i.item_id: i for i in store.items("u1", as_of=T + timedelta(days=1))}
    assert mid["q"].status == "active"
    assert [i.item_id for i in store.items("u1", statuses=["active"])] == ["b"]
    assert [i.item_id for i in store.items("u1", kinds=["open_question"])] == ["q"]


def test_preference_items_have_no_status_and_delete_is_per_user(store):
    store.put_item(item("p", kind="preference"))
    assert store.get_item("u1", "p").status is None
    with pytest.raises((ValueError, LookupError)):
        store.set_item_status("p", "resolved", at=T, by="x")
    assert store.delete_item("u2", "p") is False
    assert store.delete_item("u1", "p") is True
    assert store.get_item("u1", "p") is None


def test_forget_item_deletes_and_suppresses_without_text(store):
    store.put_item(item("d"))
    assert store.forget_item("u2", "d", at=T) is False
    assert store.forget_item("u1", "d", at=T) is True
    assert store.get_item("u1", "d") is None
    [s] = store.suppressions("u1")
    assert (s.item_id, s.kind, s.evidence_message_ids) == ("d", "decision", frozenset({"m1"}))
    assert store.suppressions("u2") == []
