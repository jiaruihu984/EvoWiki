from dataclasses import replace
from datetime import UTC, datetime, timedelta

from evowiki.agent import durable
from evowiki.assistant.session import MemorySessionStore, SessionInfo, fold
from evowiki.goals.context import estimate_tokens
from evowiki.memory.ports import MemoryPorts
from evowiki.memory.recall import (
    NO_RECALL,
    RECALL_BUDGET,
    RECALL_MAX_ITEMS,
    RECALLED_NOTE,
    content_hash,
    recall_block,
)
from evowiki.memory.store import Episode, InMemoryMemoryStore
from evowiki.retrieval.embed import HashingEmbedder

T = datetime(2026, 10, 27, 20, tzinfo=UTC)          # 2026-10-28 04:00 in Asia/Shanghai
E = HashingEmbedder()
INFO = SessionInfo(session_id="s2", user_id="u1", page_id="p2", note_concept_id=None,
                   created_at=T)


def _ep(mem, sid, mid, text, *, turn=0, at=T, answer="答"):
    mem.upsert_episode(Episode(user_id="u1", session_id=sid, message_id=mid,
                               answer_message_id=f"a{mid}", trace_run_id=f"assistant:{sid}:{mid}",
                               turn_index=turn, page_id="p1", at=at, user_text=text,
                               answer_text=answer))


def _ports(mem):
    return MemoryPorts(store=mem, embedder=E, sessions=MemorySessionStore())


def _state(first_kept=0):
    return fold(INFO, []).model_copy(update={"first_kept": first_kept})


def test_lines_are_dated_in_his_zone_labelled_and_carry_ids():
    mem = InMemoryMemoryStore()
    _ep(mem, "s1", "m1", "atomic 怎么定")
    r = recall_block(_ports(mem), info=INFO, query="atomic", page_id="p2", concept_ids=(),
                     state=_state(), known_concepts=frozenset(), now=T,
                     page_titles={"p1": "知识结构"})
    assert r.message.origin == "memory" and r.message.role == "user"
    assert r.message.text == f"{RECALLED_NOTE}\n- [2026-10-28 以前的对话 知识结构] 用户：atomic 怎么定 / 助手：答 (ep:s1/m1)"
    assert r.recalled == ({"id": "ep:s1/m1", "kind": "episode",
                           "content_hash": content_hash("用户：atomic 怎么定\n助手：答")},)


def test_an_answer_that_cites_pages_comes_back_as_its_question_and_those_pages():
    mem = InMemoryMemoryStore()
    _ep(mem, "s1", "m1", "density 公式", answer="讲义第 13 页给出了公式 rho = c/(n-1)（讲义 p.13）")
    r = recall_block(_ports(mem), info=INFO, query="density 公式", page_id="p2", concept_ids=(),
                     state=_state(), known_concepts=frozenset(), now=T)
    assert "c/(n-1)" not in r.message.text and "第 13 页" in r.message.text
    assert "density 公式" in r.message.text and "不是这一轮读到的" in r.message.text


def test_nothing_found_is_no_recall():
    r = recall_block(_ports(InMemoryMemoryStore()), info=INFO, query="atomic", page_id=None,
                     concept_ids=(), state=_state(), known_concepts=frozenset(), now=T)
    assert r == NO_RECALL


def test_at_most_five_lines_within_the_budget():
    mem = InMemoryMemoryStore()
    for i in range(12):
        _ep(mem, "s1", f"m{i}", f"atomic 第{i}次 " + "很长的话" * 200, turn=i,
            at=T - timedelta(hours=i))
    r = recall_block(_ports(mem), info=INFO, query="atomic", page_id=None, concept_ids=(),
                     state=_state(), known_concepts=frozenset(), now=T)
    assert 1 <= len(r.recalled) <= RECALL_MAX_ITEMS
    assert len(r.message.text.splitlines()) == 1 + len(r.recalled)
    assert estimate_tokens(r.message.text) <= RECALL_BUDGET


def test_live_turns_of_this_session_are_not_recalled_folded_ones_are():
    mem = InMemoryMemoryStore()
    _ep(mem, "s2", "m0", "atomic 折叠了", turn=0)
    _ep(mem, "s2", "m1", "atomic 还在上下文里", turn=1)
    r = recall_block(_ports(mem), info=INFO, query="atomic", page_id=None, concept_ids=(),
                     state=_state(first_kept=1), known_concepts=frozenset(), now=T)
    assert [x["id"] for x in r.recalled] == ["ep:s2/m0"]


def test_durable_recall_is_one_step_and_failure_is_no_recall(monkeypatch):
    mem = InMemoryMemoryStore()
    _ep(mem, "s1", "m1", "atomic 怎么定")
    names = []
    monkeypatch.setattr(durable, "step", lambda name, fn: (names.append(name), fn())[1])
    r = _ports(mem).durable().recall(info=INFO, query="atomic", concept_ids=(), state=_state(),
                                     now=T)
    assert names == ["assistant.memory.recall"] and r.recalled[0]["id"] == "ep:s1/m1"

    def broken(name, fn):
        raise RuntimeError("down")
    monkeypatch.setattr(durable, "step", broken)
    assert _ports(mem).durable().recall(info=INFO, query="atomic", concept_ids=(),
                                        state=_state(), now=T) == NO_RECALL


def test_a_turn_whose_action_he_undid_says_so_first():
    """Run 11041904a6d7 undone-limit-not-recalled: the next session read 「已设成每天最多 30 分钟」 and repeated it."""
    mem = InMemoryMemoryStore()
    _ep(mem, "s1", "m1", "把 atomic 目标设成每天最多 30 分钟", answer="已设好。" + "很长的说明" * 80)
    ports = replace(_ports(mem), undone=lambda s, m: ["把条件改成每天最多 30 分钟"] if s == "s1" else [])
    r = recall_block(ports, info=INFO, query="atomic 目标", page_id="p2", concept_ids=(), state=_state(),
                     known_concepts=frozenset(), now=T)
    line = next(x for x in r.message.text.splitlines() if "ep:s1/m1" in x)
    assert "他后来撤销了，现在不生效：把条件改成每天最多 30 分钟" in line      # before the clip of a long answer
    none = recall_block(replace(ports, undone=lambda s, m: []), info=INFO, query="atomic 目标", page_id="p2",
                        concept_ids=(), state=_state(), known_concepts=frozenset(), now=T)
    assert "撤销" not in none.message.text
