"""The learner's 👍 / 👎 joins the rated turn's episode (memory/index.py `reindex_feedback`):
recalled in another session as 「以前的反馈」, gone again when the rating is cleared, never an
item. In-memory stores and the hashing embedder; no database, no model call."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from evowiki.assistant.contracts import RatingIn
from evowiki.assistant.session import MemorySessionStore, SessionInfo, fold, rating_payload
from evowiki.llm.chat import ChatTurn
from evowiki.memory.index import FEEDBACK_HEAD, backfill_vectors, feedback_line, index_turn
from evowiki.memory.ports import MemoryPorts
from evowiki.memory.recall import FEEDBACK_LABEL, recall_block
from evowiki.memory.store import InMemoryMemoryStore, tag
from evowiki.retrieval.embed import HashingEmbedder
from tests.service.test_assistant_api import U1, events, make

T = datetime(2026, 10, 28, 9, tzinfo=UTC)
E = HashingEmbedder()
QUESTION = "你学过样本方差吗？"
ASKED = "标准差为什么要除以 n-1 而不是 n"


def world(embedder=E):
    sessions, mem = MemorySessionStore(), InMemoryMemoryStore()
    ports = MemoryPorts(store=mem, embedder=embedder, sessions=sessions, clock=lambda: T)
    info = SessionInfo(session_id="s1", user_id="u1", page_id="p1", note_concept_id=None,
                       created_at=T)
    sessions.create(info)
    sessions.append("s1", "e1", "user", {"message_id": "m1", "text": ASKED, "selection": None}, T)
    sessions.append("s1", "e2", "tool", {"message_id": "m1", "call_id": "c1", "name": "ask_user",
                                         "effect": "read", "args": {}, "ok": True, "content": "",
                                         "sources": [], "ask": {"payload": {"question": QUESTION}}},
                    T)
    sessions.append("s1", "e3", "assistant", {"message_id": "a1", "text": f"先问一句：{QUESTION}",
                                              "sources": []}, T)
    index_turn(mem, E, info=info, turn_index=0, user_message_id="m1", answer_message_id="a1",
               user_text=ASKED, answer_text=f"先问一句：{QUESTION}", concept_ids=(), at=T)
    return sessions, mem, ports, info


def rate(sessions, ports, info, n, **body):
    payload = rating_payload("a1", "m1", RatingIn(**body),
                             [{"call_id": "c1", "question": QUESTION}])
    entry = sessions.append("s1", f"r{n}", "rating", payload, T + timedelta(minutes=n))
    ports.rated(info, sessions.entries("s1"), "m1")
    return entry


def recalled(sessions, ports):
    info = SessionInfo(session_id="s2", user_id="u1", page_id="p1", note_concept_id=None,
                       created_at=T)
    sessions.create(info)
    r = recall_block(ports, info=info, query=ASKED, page_id="p1", concept_ids=(),
                     state=fold(info, []), known_concepts=frozenset(),
                     now=T + timedelta(days=1))
    return r.message.text if r.message is not None else ""


def test_feedback_line_names_the_question_chips_and_note():
    p = {"value": "down", "chips": ["我说过不会"], "note": "上周就说了",
         "asks": [{"call_id": "c1", "question": QUESTION}]}
    assert feedback_line(p) == f"{FEEDBACK_HEAD}👎（问题「{QUESTION}」） 我说过不会；备注：上周就说了"
    assert feedback_line({"value": "up", "chips": [], "note": ""}) == f"{FEEDBACK_HEAD}👍"
    assert feedback_line({"value": None}) is None


def test_a_down_on_a_question_is_recalled_in_another_session_and_never_an_item():
    sessions, mem, ports, info = world()
    rate(sessions, ports, info, 1, value="down", chips=["我说过不会"], note="上周说过")
    [ep] = mem.episodes("u1")
    assert ep.answer_text.endswith(f"{FEEDBACK_HEAD}👎（问题「{QUESTION}」） 我说过不会；备注：上周说过")
    assert mem.episode_vectors("u1", tag(E))                    # embedded again
    text = recalled(sessions, ports)
    assert FEEDBACK_LABEL in text and "我说过不会" in text and QUESTION in text
    assert mem.items("u1") == []
    rate(sessions, ports, info, 2, value="down", chips=["我说过不会"], note="上周说过")
    assert mem.episodes("u1") == [ep]                           # idempotent


def test_clearing_the_rating_removes_the_line():
    sessions, mem, ports, info = world()
    rate(sessions, ports, info, 1, value="down", chips=["问过了"])
    rate(sessions, ports, info, 2, value=None)
    [ep] = mem.episodes("u1")
    assert FEEDBACK_HEAD not in ep.answer_text and ep.answer_text == f"先问一句：{QUESTION}"
    text = recalled(sessions, ports)
    assert FEEDBACK_LABEL not in text and "问过了" not in text


def test_a_failed_embedding_leaves_the_episode_to_the_backfill():
    class Broken(HashingEmbedder):
        def embed_documents(self, texts):
            raise RuntimeError("model down")
    sessions, mem, ports, info = world()
    rate(sessions, replace(ports, embedder=Broken()), info, 1, value="down", chips=["问过了"])
    assert mem.episode_vectors("u1", tag(E)) == {}
    assert FEEDBACK_HEAD in mem.episodes("u1")[0].answer_text
    assert backfill_vectors(mem, E, "u1") == 1


def test_the_rating_route_feeds_the_episode(tmp_path):
    c = make(tmp_path, [ChatTurn(text="样本方差要除以 n-1")])
    svc = c.app.state.services.assistant
    mem = InMemoryMemoryStore()
    svc._deps = replace(svc._deps, memory=MemoryPorts(store=mem, embedder=E,
                                                      sessions=svc._deps.store))
    sid = c.post("/assistant/sessions", json={"page_id": "p1"}, headers=U1).json()["session_id"]
    evs = events(c.post(f"/assistant/sessions/{sid}/messages", json={"text": ASKED},
                        headers=U1))
    answer = next(d for e, d in evs if e == "done")["message_id"]
    user_mid = c.get(f"/assistant/sessions/{sid}", headers=U1).json()["messages"][0]["message_id"]
    index_turn(mem, E, info=svc._deps.store.info(sid), turn_index=0, user_message_id=user_mid,
               answer_message_id=answer, user_text=ASKED, answer_text="样本方差要除以 n-1",
               concept_ids=(), at=T)
    r = c.post(f"/assistant/sessions/{sid}/messages/{answer}/rating", headers=U1,
               json={"value": "down", "chips": ["不对"], "note": "公式写错了"})
    assert r.status_code == 200
    [ep] = mem.episodes("u1")
    assert ep.answer_text.endswith(f"{FEEDBACK_HEAD}👎 不对；备注：公式写错了")
    assert mem.items("u1") == []
