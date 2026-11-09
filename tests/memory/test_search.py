from datetime import timedelta
from itertools import count

import pytest

from evowiki.memory.contracts import EvidenceRef
from evowiki.memory.search import search
from evowiki.memory.store import Episode, InMemoryMemoryStore, Item
from evowiki.retrieval.embed import HashingEmbedder
from tests.memory.conftest import T

E = HashingEmbedder()
_n = count(1)


@pytest.fixture
def mem():
    return InMemoryMemoryStore()


def put_item(mem, *, kind="decision", content="x", status="active", superseded_by=None,
             concept_ids=(), embed=True, created_days_ago=0, at=None, uid="u1"):
    iid = f"item-{next(_n)}"
    at = at or T - timedelta(days=created_days_ago)
    mem.put_item(Item(item_id=iid, user_id=uid, kind=kind, content=content,
                      provenance="explicit_current",
                      evidence=(EvidenceRef(message_id="m1", session_id="s1", role="user",
                                            quote="就这样"),),
                      scope_type="global", concept_ids=concept_ids, session_id="s1",
                      valid_from=at, created_at=at,
                      status=status if kind in ("decision", "open_question") else None,
                      superseded_by=superseded_by))
    if embed:
        mem.set_item_vector(iid, E.embed_documents([content])[0], E)
    return iid


def put_episode(mem, *, text, uid="u1", sid="s1", at=T, embed=True):
    mid = f"m{next(_n)}"
    mem.upsert_episode(Episode(user_id=uid, session_id=sid, message_id=mid,
                               answer_message_id=f"a{mid}", trace_run_id=f"assistant:{sid}:{mid}",
                               turn_index=0, page_id="p1", at=at, user_text=text,
                               answer_text="好的"))
    if embed:
        mem.set_episode_vector(sid, mid, E.embed_documents([f"用户：{text}\n助手：好的"])[0], E)
    return mid


def test_auto_never_returns_preferences_or_inactive(mem):
    put_item(mem, kind="preference", content="atomic 先解释符号", status=None)
    put_item(mem, kind="decision", content="atomic 由知识本身决定", status="superseded")
    put_item(mem, kind="decision", content="atomic 放在 ConceptName 上", status="active")
    hits = search(mem, E, "u1", "atomic 怎么定", mode="auto")
    assert [h.text for h in hits] == ["atomic 放在 ConceptName 上"]


def test_history_returns_superseded_with_label(mem):
    old = put_item(mem, kind="decision", content="旧决定 atomic", status="superseded",
                   superseded_by="item-new")
    hits = search(mem, E, "u1", "atomic", mode="history")
    assert any(h.id == f"item:{old}" and "后来被" in h.label for h in hits)


def test_auto_can_return_nothing(mem):
    put_episode(mem, text="矩阵乘法怎么算")
    assert search(mem, E, "u1", "今天天气怎么样", mode="auto") == []


def test_keyword_hit_alone_does_not_enter_auto_recall(mem):
    put_episode(mem, text="矩阵是什么，和向量有什么关系")
    put_episode(mem, text="矩阵的秩")
    # cosine can never reach 1.01, so only the strict lexical channel could admit these
    assert search(mem, E, "u1", "矩阵", mode="auto", threshold=1.01) == []
    assert len(search(mem, E, "u1", "矩阵", mode="history")) == 2


def test_shared_concept_with_a_keyword_hit_enters_auto(mem):
    put_item(mem, content="矩阵的秩按列算", concept_ids=("rank",), embed=False)
    assert search(mem, E, "u1", "矩阵", mode="auto", threshold=1.01) == []
    assert search(mem, E, "u1", "矩阵", mode="auto", threshold=1.01, concept_ids=("rank",))


def test_strict_lexical_channel_recalls_unembedded_rows(mem):
    put_item(mem, kind="decision", content="atomic 由知识本身决定", embed=False)   # no vector
    put_item(mem, kind="decision", content="ConceptName 上存 atomic 标记", embed=False)
    hits = search(mem, E, "u1", "ConceptName 那个决定", mode="auto", threshold=1.01)
    assert [h.text for h in hits] == ["ConceptName 上存 atomic 标记"]      # identifier match
    hits = search(mem, E, "u1", "知识本身决定的那条", mode="auto", threshold=1.01)
    assert [h.text for h in hits] == ["atomic 由知识本身决定"]             # contiguous phrase


def test_decisions_do_not_decay_but_episodes_do(mem):
    put_item(mem, kind="decision", content="atomic 由知识本身决定", created_days_ago=200)
    assert search(mem, E, "u1", "atomic 由知识本身决定", mode="auto", now=T)
    old = put_episode(mem, text="方差用具体数字讲", at=T - timedelta(days=60))
    new = put_episode(mem, text="方差用具体数字讲", at=T)
    hits = search(mem, E, "u1", "方差用具体数字讲", mode="history", kinds=["episode"], now=T)
    assert [h.message_ids[0] for h in hits] == [new, old]
    assert hits[1].score == pytest.approx(hits[0].score * 0.25, rel=0.05)   # rrf rank 2 vs 1


def test_as_of_hides_later_rows_and_restores_status(mem):
    a = put_item(mem, content="atomic 放在页面上", at=T)
    b = put_item(mem, content="atomic 放在 ConceptName 上", at=T + timedelta(days=2))
    mem.set_item_status(a, "superseded", at=T + timedelta(days=2), by="consolidation",
                        superseded_by=b)
    hits = search(mem, E, "u1", "atomic", mode="history", as_of=T + timedelta(days=1))
    assert [(h.id, h.status, h.label) for h in hits] == [(f"item:{a}", "active", "")]
    now = {h.id: h for h in search(mem, E, "u1", "atomic", mode="history")}
    assert now[f"item:{a}"].status == "superseded" and "后来被" in now[f"item:{a}"].label


def test_known_concept_open_question_is_skipped_in_auto(mem):
    put_item(mem, kind="open_question", content="为什么除以标准差", concept_ids=("z",))
    assert search(mem, E, "u1", "标准差", mode="auto", known_concepts=frozenset({"z"})) == []
    assert search(mem, E, "u1", "标准差", mode="history", known_concepts=frozenset({"z"}))


def test_user_isolation(mem):
    put_episode(mem, uid="u2", sid="s2", text="u2 的秘密 atomic")
    assert search(mem, E, "u1", "atomic", mode="history") == []


def test_page_and_concept_boosts_and_since(mem):
    put_episode(mem, text="atomic 一", at=T - timedelta(days=3))
    put_episode(mem, text="atomic 二", at=T)
    hits = search(mem, E, "u1", "atomic", mode="history", since=T - timedelta(days=1))
    assert [h.text.split("\n")[0] for h in hits] == ["用户：atomic 二"]
    other = put_item(mem, content="atomic 别页", embed=False)
    hits = search(mem, E, "u1", "atomic", mode="history", kinds=["decision"], page_id="p9")
    assert [h.id for h in hits] == [f"item:{other}"]
