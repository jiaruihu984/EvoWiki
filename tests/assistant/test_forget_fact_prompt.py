"""「忘掉我家猫叫年糕」, confirmed: a summary or a turn that names it reaches no prompt, and undo brings it back.
The executor, ledger and suppression are the real ones; the model is scripted."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from evowiki.assistant.carryover import carry_over
from evowiki.assistant.compact import DEFAULT_POLICY, compact_request
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.forgotten import FORGOT_LINE, MASKED
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import SessionInfo, fold
from evowiki.assistant.wrapup import Job, _summarise
from evowiki.bot.actions import ActionLedger
from evowiki.bot.facts import _open_questions
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.memory.anchors import AnchorMemory
from evowiki.memory.forget_fact import KIND, MemoryForgetExecutor
from evowiki.memory.ports import MemoryPorts
from evowiki.memory.store import Episode, PgMemoryStore
from evowiki.retrieval.embed import HashingEmbedder
from tests.assistant.fakes import T0, Data, Ids, peek_then, tools
from tests.runtime.helpers import make_user

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
WORD = "年糕"
SAID = "我家猫叫年糕，今年三岁"
HANDOFF = {"summary": "他家猫叫年糕，爱吃鱼。讲了 z 分数的含义。",
           "decisions": ["以后叫它年糕", "用表格讲 z 分数"], "active_requests": ["多举年糕的例子", "讲慢一点"]}
LEARNING = {"unclear": [{"concept": "年糕为什么掉毛", "evidence_id": "old_m0"},
                        {"concept": "负的 z", "evidence_id": "old_m1"}], "understood": []}
TURNS = ((SAID, "记住了，年糕很可爱"), ("负的 z 是什么", "低于均值"),
         ("年糕掉毛怎么办", "多梳毛"), ("再讲讲 z 分数", "z 分数是标准化后的值"))


def _info(sid):
    return SessionInfo(session_id=sid, user_id="u1", page_id="p1", note_concept_id=None, created_at=T0)


def _add(store, sid, n, type_, payload):
    store.append(sid, f"{sid}_e{n}", type_, payload, T0 + timedelta(seconds=n))


def _turn(store, sid, n, mid, text, answer):
    _add(store, sid, n, "user", {"message_id": mid, "text": text, "selection": None})
    _add(store, sid, n + 1, "assistant", {"message_id": f"{mid}_a", "text": answer, "sources": [],
                                          "proposals": [], "tool_calls": 0, "llm_calls": 1})


def _long_session(store, sid="old"):
    """His long conversation: a summary naming it folds the first two turns, two stay verbatim."""
    store.create(_info(sid))
    n = 1
    for i, (text, answer) in enumerate(TURNS):
        if i == 3:
            _add(store, sid, n, "compaction", {
                "version": "compact@v7", "handoff": HANDOFF, "learning": LEARNING, "edits": [],
                "first_kept": f"{sid}_m2"})
            n += 1
        _turn(store, sid, n, f"{sid}_m{i}", text, answer)
        n += 2


class World:
    def __init__(self, pool, tmp_path):
        make_user(pool, "u1")
        self.tmp_path, self.pool = tmp_path, pool
        self.sessions, self.ids = PgSessionStore(pool), Ids()
        self.mem = PgMemoryStore(pool)
        self.ledger = ActionLedger(pool, lambda: T0, Ids(), {})
        self.ledger.register(KIND, MemoryForgetExecutor(pool, self.mem, self.ledger, lambda: T0))
        _long_session(self.sessions)
        for i, (text, answer) in enumerate(TURNS):
            self.mem.upsert_episode(Episode(
                user_id="u1", session_id="old", message_id=f"old_m{i}", answer_message_id=f"old_m{i}_a",
                trace_run_id=f"assistant:old:old_m{i}", turn_index=i, page_id="p1", at=T0,
                user_text=text, answer_text=answer))

    def forget(self):
        row = self.ledger.propose("u1", kind=KIND, scope="suggestion", source="assistant", summary=f"忘掉「{WORD}」",
                                  why="他说的", content={"words": [WORD], "quote": "忘掉"}, key="forget-1", level="ask")
        self.ledger.approve("u1", row.action_id)
        return row.action_id

    def undo(self, action_id):
        self.ledger.undo("u1", action_id)

    def turn(self, sid, text):
        """One turn; the text of everything the model was sent for it."""
        prov = ScriptedChatProvider("scripted", peek_then())
        gw = Gateway(CFG, CallLog(self.tmp_path / f"{sid}.jsonl"), mode="record", providers={"scripted": prov})
        deps = TurnDeps(gateway=gw, tools=tools(), store=self.sessions, data=Data(), new_id=self.ids, clock=lambda: T0,
                        memory=MemoryPorts(store=self.mem, embedder=HashingEmbedder(), sessions=self.sessions))
        list(run_turn(deps, self.sessions.info(sid), MessageIn(text=text)))
        req = next(r for r in reversed(prov.chat_calls) if r.purpose == "assistant.turn")
        return "\n".join([req.system, *(m.text for m in req.messages)])


@pytest.fixture
def world(pool, tmp_path):
    return World(pool, tmp_path)


def test_the_same_conversation_no_longer_sends_it_and_undo_brings_it_back(world):
    before = world.turn("old", "讲下一个")
    assert WORD in before and "爱吃鱼" in before
    act = world.forget()
    after = world.turn("old", "我家猫叫什么")
    assert WORD not in after and "爱吃鱼" not in after
    assert "z 分数是标准化后的值" in after and "负的 z" in after
    assert FORGOT_LINE in after and MASKED not in after
    world.undo(act)
    restored = world.turn("old", "再问一次")
    assert WORD in restored and FORGOT_LINE not in restored


def test_a_new_conversation_on_the_page_does_not_inherit_it_and_undo_brings_it_back(world):
    world.sessions.create(_info("new"))
    act = world.forget()
    after = world.turn("new", "我家猫叫什么")
    assert WORD not in after and "z 分数是标准化后的值" in after
    world.undo(act)
    world.sessions.create(_info("new2"))
    assert WORD in world.turn("new2", "我家猫叫什么")


def test_carry_over_drops_a_kept_turn_it_names(world):
    from evowiki.assistant.forgotten import NOTHING, forgotten_of
    world.forget()
    got = carry_over(world.sessions, _info("cur"), forgotten_of(world.mem, "u1"))
    assert WORD not in got.page_summary and not any(WORD in r for r in got.page_recent)
    assert any("再讲讲 z 分数" in r for r in got.page_recent)
    assert any(WORD in text for text in carry_over(world.sessions, _info("cur"), NOTHING).page_recent)


def test_a_later_summary_is_not_fed_it_at_a_merge_or_a_wrap_up(world):
    from evowiki.assistant.forgotten import forgotten_of
    world.forget()
    state = forgotten_of(world.mem, "u1").view(fold(_info("old"), world.sessions.entries("old")))
    req = compact_request(state, cut=len(state.turns))
    assert WORD not in req.prompt and "z 分数是标准化后的值" in req.prompt

    sent = []

    def reply(_m, r):
        sent.append(r.prompt)
        import json
        node = next(iter(json.loads(r.prompt)['raw']))
        return json.dumps({'statements': [{'text': '继续讨论 z 分数', 'sources': [node]}]}, ensure_ascii=False)

    prov = ScriptedChatProvider("scripted", lambda m, r: None, reply)
    gw = Gateway(CFG, CallLog(world.tmp_path / "w.jsonl"), mode="record", providers={"scripted": prov})
    deps = SimpleNamespace(store=world.sessions, gateway=gw, compaction=DEFAULT_POLICY, clock=lambda: T0, new_id=Ids(),
                           data=Data(), memory=MemoryPorts(store=world.mem, embedder=HashingEmbedder(),
                                                           sessions=world.sessions,
                                                           anchors=AnchorMemory(world.pool, world.sessions, world.mem)))
    assert _summarise(deps, Job(user_id="u1", session_id="old", version=0, attempt=0))
    assert sent and WORD not in sent[0]


def test_consolidation_is_not_shown_it(world):
    from evowiki.memory.consolidate import consolidate
    world.forget()
    _turn(world.sessions, "old", 20, "old_m4", "好的", "已忘掉年糕。还要讲 z 分数吗")
    sent = []

    def reply(_m, r):
        sent.append(r.prompt)
        return '{"items": []}'

    prov = ScriptedChatProvider("scripted", lambda m, r: None, reply)
    gw = Gateway(CFG, CallLog(world.tmp_path / "k.jsonl"), mode="record", providers={"scripted": prov})
    state = fold(_info("old"), world.sessions.entries("old"))
    consolidate(state, gateway=gw, account=None, store=world.mem, sessions=world.sessions, lander=None, guard=None,
                first=0, through=len(state.turns), now=T0, new_id=Ids())
    assert sent and WORD not in sent[0] and "再讲讲 z 分数" in sent[0]


def test_consolidation_is_told_a_turn_was_undone(world):
    from evowiki.memory.consolidate import consolidate
    sent = []

    def reply(_m, r):
        sent.append(r.prompt)
        return '{"items": []}'

    prov = ScriptedChatProvider("scripted", lambda m, r: None, reply)
    gw = Gateway(CFG, CallLog(world.tmp_path / "u.jsonl"), mode="record", providers={"scripted": prov})
    state = fold(_info("old"), world.sessions.entries("old"))
    consolidate(state, gateway=gw, account=None, store=world.mem, sessions=world.sessions, lander=None, guard=None,
                first=0, through=len(state.turns), now=T0, new_id=Ids(),
                undone=lambda s, m: ["设成每天 30 分钟"] if m == "old_m3" else [])
    assert "他后来撤销了" in sent[0] and "设成每天 30 分钟" in sent[0]


def test_what_he_said_earlier_in_this_conversation_is_found_only_the_asking_message_is_not(world):
    from types import SimpleNamespace as NS

    from evowiki.assistant.tools.actions import _forget_memory
    from evowiki.memory.forget_fact import episode_key, fact_hits
    assert any(h.key == episode_key("old", "old_m0") for h in fact_hits(world.mem, "u1", (WORD,)))
    ctx = NS(user_id="u1", session_id="old", message_id="now", current_words=("忘掉我家猫叫年糕",))
    deps = NS(memory_store=world.mem, ledger=world.ledger, profile_lines=None)
    plan = _forget_memory(NS(keywords=[WORD], quote="忘掉我家猫叫年糕"), None, ctx, deps)
    assert "2 处记忆" in plan.title


def test_what_he_says_again_after_forgetting_stays(world):
    from evowiki.assistant.forgotten import forgotten_of
    world.forget()
    _turn(world.sessions, "old", 20, "old_m4", "其实年糕今天生病了", "年糕要去看医生")
    _turn(world.sessions, "old", 22, "old_m5", "好的", "已忘掉年糕。还要讲 z 分数吗")
    state = forgotten_of(world.mem, "u1").view(fold(_info("old"), world.sessions.entries("old")))
    by_id = {t.message_id: t for t in state.turns}
    assert WORD in by_id["old_m4"].user_text and WORD in (by_id["old_m4"].answer or "")
    assert by_id["old_m5"].answer == "还要讲 z 分数吗"      # his 好 said nothing; the echo goes


def test_the_ops_line_and_undo_list_do_not_name_it(world):
    from evowiki.assistant.forgotten import prompt_title
    act = world.forget()
    assert WORD not in prompt_title(world.ledger.get("u1", act))
    world.undo(act)
    assert WORD in prompt_title(world.ledger.get("u1", act))


def test_the_open_question_card_does_not_name_it(world):
    world.forget()
    with world.pool.connection() as conn:
        assert [q.text for q in _open_questions(conn, "u1", {})] == ["负的 z"]


@pytest.mark.parametrize("second_word", ["年糕", "猫", "三岁"])
@pytest.mark.parametrize("undo_first", [True, False])
def test_overlapping_forgets_keep_old_turn_hidden_until_last_undo(pool, tmp_path, monkeypatch,
                                                                second_word, undo_first):
    def short_session(store, sid="old"):
        store.create(_info(sid))
        _turn(store, sid, 1, "old_m0", SAID, "记住了，年糕很可爱")
    monkeypatch.setattr(__import__(__name__, fromlist=["_long_session"]), "_long_session", short_session)
    w = World(pool, tmp_path)
    first = w.forget()
    row = w.ledger.propose("u1", kind=KIND, scope="suggestion", source="assistant",
        summary="忘掉另一处", why="requested", content={"words": [second_word], "quote": "忘掉"},
        key="forget-second", level="ask")
    w.ledger.approve("u1", row.action_id)
    removed, remaining = (first, row.action_id) if undo_first else (row.action_id, first)
    w.undo(removed)
    assert w.ledger.get("u1", remaining).status == "executed"
    assert SAID not in w.turn("old", "讲下一个")
    w.undo(remaining)
    assert SAID in w.turn("old", "再讲下一个")
