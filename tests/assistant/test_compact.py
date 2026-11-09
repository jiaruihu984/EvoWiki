import json
import logging
from datetime import UTC, datetime

import pytest

from evowiki.agent.compact import CompactPolicy
from evowiki.assistant.compact import (
    CLOSE_MIN_TOKENS,
    COMPACT_SYSTEM,
    SUMMARY_MAX_CHARS,
    compact,
    history_tokens,
    maintain,
    plan_cut,
    turn_tokens,
    worth_summarising,
)
from evowiki.assistant.session import MemorySessionStore, SessionInfo, SessionState, Turn, fold
from evowiki.assistant.summary import (
    CompactReply,
    Evidence,
    Handoff,
    Learning,
    Recap,
    check_reply,
    render_handoff,
)
from evowiki.llm.gateway import Gateway, LLMParseError, ProviderUnavailableError
from evowiki.llm.log import CallLog
from evowiki.llm.models import TEMPERATURE_PARAM, THINKING_PARAM, GatewayConfig, ModelRef
from evowiki.llm.prompt_steps import strip_marks
from evowiki.llm.providers import (
    ProviderRefusalError,
    ProviderRejectedError,
    ProviderTransientError,
)
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import Account

INFO = SessionInfo(session_id="s1", user_id="u1", page_id=None, note_concept_id=None,
                   created_at=datetime(2026, 10, 8, tzinfo=UTC))
REF = ModelRef(provider="scripted", model="m")
ACC = Account(user_id="u1", group="assistant")

SMALL = CompactPolicy(history_budget=1000, keep_turns=4, keep_budget=600, hard_cap=1500,
                      summary_max_tokens=300)


def _reply(summary="讲了 z 分数", unclear=(), understood=(), pending=(), requests=()):
    """A compact@v7 reply as the model would write it."""
    return json.dumps({
        "handoff": {"summary": summary, "decisions": [], "active_requests": list(requests)},
        "learning": {"unclear": [{"concept": c, "evidence_id": i} for c, i in unclear],
                     "understood": [{"concept": c, "evidence_id": i} for c, i in understood]}},
        ensure_ascii=False)


GOOD = _reply(unclear=[("负的 z", "m2")])


def _no_chat(model, req):
    raise AssertionError(f"compaction is a JSON call, not a chat: {req.purpose}")


def state(n, size=50, first_kept=0, summary=""):
    turns = tuple(Turn(message_id=f"m{i}", user_text=f"问{i}" + "字" * size, selection=None,
                       answer=f"答{i}" + "字" * size) for i in range(n))
    return SessionState(info=INFO, turns=turns, summary=summary, first_kept=first_kept,
                        messages=(), decisions={})


def _errored(st, *indexes):
    turns = tuple(Turn(message_id=t.message_id, user_text=t.user_text, selection=None,
                       answer=None) if i in indexes else t for i, t in enumerate(st.turns))
    return st.model_copy(update={"turns": turns})


def _gw(tmp_path, text=GOOD):
    prov = ScriptedChatProvider("scripted", _no_chat, lambda m, r: text)
    gw = Gateway(GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True),
                 CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov})
    return prov, gw


# ---- tokens and the cut ----

def test_turn_tokens_counts_question_selection_and_answer():
    t = Turn(message_id="m0", user_text="问" * 10, selection={"text": "选" * 6, "heading": "h"},
             answer="答" * 4)
    assert turn_tokens(t) == 10 + 6 + 4
    assert turn_tokens(Turn(message_id="m1", user_text="问" * 10, selection=None,
                            answer="答" * 4)) == 14
    assert turn_tokens(state(1, size=50).turns[0]) == 104      # "问0" + 50 字, "答0" + 50 字


def test_history_tokens_counts_summary_and_answered_turns_after_first_kept():
    st = state(3, size=10, first_kept=1, summary="摘要")
    errored = Turn(message_id="m3", user_text="错" * 100, selection=None, answer=None)
    st = st.model_copy(update={"turns": (*st.turns, errored)})
    # summary 2 + m1, m2 at 12 + 12 each ("问1" + 10 CJK = 11 + ceil(1/4)); m0 is before the
    # cut and errored m3 is not counted
    assert history_tokens(st) == 2 + 2 * 24


def test_plan_cut_keeps_the_last_turns_whole():
    st = state(10, size=50)                     # 104 tokens a turn, 1040 > 1000
    assert plan_cut(st, SMALL) == 6
    assert plan_cut(state(5, size=50), SMALL) is None
    assert plan_cut(state(10, size=1), SMALL) is None     # many turns but under budget


def test_plan_cut_keep_budget_shrinks_the_kept_turns():
    st = state(10, size=100)                    # 204 a turn: 3 turns = 612 > 600, so keep 2
    assert plan_cut(st, SMALL) == 8


def test_plan_cut_folds_everything_when_one_huge_turn_cannot_be_kept():
    st = state(3, size=700)                     # 1404 a turn: nothing fits keep_budget
    assert plan_cut(st, SMALL) == 3


def test_plan_cut_skips_errored_turns_and_respects_first_kept():
    # first_kept=2, summary 300 tokens: with m3 errored the answered turns after it are
    # m2 m4 m5 m6 m7 m8 m9 (7 * 104 + 300 = 1028 > 1000); the last four answered stay -> m6
    st = _errored(state(10, size=50, first_kept=2, summary="旧" * 300), 3)
    assert plan_cut(st, SMALL) == 6
    # m7 errored instead: answered m2 m3 m4 m5 m6 m8 m9, the last four are m5 m6 m8 m9 -> cut at
    # m5, the errored m7 stays inside the kept range
    st = _errored(state(10, size=50, first_kept=2, summary="旧" * 300), 7)
    assert plan_cut(st, SMALL) == 5
    # m0 and m1 are before first_kept and do not count: 8 * 104 + 100 = 932 <= 1000
    assert plan_cut(state(10, size=50, first_kept=2, summary="旧" * 100), SMALL) is None


# ---- compact ----

def test_compact_stores_handoff_checked_learning_and_code_edits(tmp_path):
    prov, gw = _gw(tmp_path)
    st = state(10, size=50)
    turns = list(st.turns)
    turns[1] = turns[1].model_copy(update={"edits": ("改写了小节「均值」",)})
    payload, call = compact(st.model_copy(update={"turns": tuple(turns)}), gw, ACC, cut=6,
                            policy=SMALL)
    assert payload == {
        "version": "compact@v7",
        "handoff": {"summary": "讲了 z 分数", "decisions": [], "active_requests": [],
                    },
        "learning": {"unclear": [{"concept": "负的 z", "evidence_id": "m2"}], "understood": []},
        "edits": ["改写了小节「均值」"],
        "first_kept": "m6"}
    req = prov.calls[0]
    assert "[m0] 用户：问0" in req.prompt and "问6" not in req.prompt
    assert "（这一轮改了页面：改写了小节「均值」；系统已记下）" in req.prompt
    assert req.purpose == "assistant.compact" and req.prompt_version == "compact@v16"
    assert req.params[THINKING_PARAM] is False       # thinking off (user ruling 2026-11-03)
    assert call.purpose == "assistant.compact"


def test_compact_fold_all_has_no_first_kept(tmp_path):
    _, gw = _gw(tmp_path)
    payload, _ = compact(state(3), gw, ACC, cut=3, policy=SMALL)
    assert payload["first_kept"] is None


def test_compact_respects_first_kept_and_skips_errored_turns(tmp_path):
    prov, gw = _gw(tmp_path)
    st = state(8, size=5, first_kept=2)
    turns = list(st.turns)
    turns[3] = Turn(message_id="m3", user_text="出错的问题", selection=None, answer=None)
    st = st.model_copy(update={"turns": tuple(turns)})
    payload, _ = compact(st, gw, ACC, cut=5, policy=SMALL)
    assert payload["first_kept"] == "m5"
    req = prov.calls[0]
    assert "问2" in req.prompt and "问4" in req.prompt
    assert "问1" not in req.prompt and "出错的问题" not in req.prompt and "问5" not in req.prompt
    assert req.subject == "s1"
    # m3 errored: only the answered folded turns may be cited
    assert req.params == {"upto": "m2", "through": "m4", "evidence_ids": ["m2", "m4"],
                          THINKING_PARAM: False, TEMPERATURE_PARAM: 0}
    # m2 is folded here, so the unclear item citing it is kept
    assert payload["learning"]["unclear"] == [{"concept": "负的 z", "evidence_id": "m2"}]
    compact(state(6, size=5), gw, ACC, cut=3, policy=SMALL)
    assert prov.calls[1].params == {"upto": None, "through": "m2",
                                    "evidence_ids": ["m0", "m1", "m2"], THINKING_PARAM: False,
                                    TEMPERATURE_PARAM: 0}


def test_compact_same_cut_replays_the_same_summary(tmp_path):
    prov, gw = _gw(tmp_path)
    st = state(6, size=5)
    first, _ = compact(st, gw, ACC, cut=3, policy=SMALL)
    second, _ = compact(st, gw, ACC, cut=3, policy=SMALL)
    assert first == second and len(prov.calls) == 1


def test_compact_refuses_a_bad_cut_and_nothing_to_fold_before_any_call(tmp_path):
    prov, gw = _gw(tmp_path)
    st = state(6, first_kept=2)
    for cut in (0, 1, 2, 7):             # <= first_kept, or beyond the turns
        with pytest.raises(ValueError, match="bad cut"):
            compact(st, gw, ACC, cut=cut, policy=SMALL)
    with pytest.raises(ValueError, match="nothing to compact"):
        compact(_errored(state(6, first_kept=2), 2, 3), gw, ACC, cut=4, policy=SMALL)
    assert prov.calls == []


def test_compact_drops_learning_items_whose_evidence_does_not_check(tmp_path, caplog):
    reply = _reply(unclear=[("负的 z", "m2"), ("等他给两道重复的题", "a_m1"), ("方差", "m9")],
                   understood=[("均值", "m0"), ("均值", "m1"), ("中位数", "")])
    _, gw = _gw(tmp_path, reply)
    with caplog.at_level(logging.INFO, logger="evowiki.assistant.compact"):
        payload, _ = compact(state(10, size=50), gw, ACC, cut=6, policy=SMALL)
    # m9 is a kept turn, a_m1 an assistant message, "" nothing: dropped; the summary stays
    assert payload["learning"] == {"unclear": [{"concept": "负的 z", "evidence_id": "m2"}],
                                   "understood": [{"concept": "均值", "evidence_id": "m0"}]}
    assert payload["dropped"] == [
        {"field": "unclear", "concept": "等他给两道重复的题", "evidence_id": "a_m1"},
        {"field": "unclear", "concept": "方差", "evidence_id": "m9"},
        {"field": "understood", "concept": "中位数", "evidence_id": ""}]
    assert payload["handoff"]["summary"] == "讲了 z 分数"
    assert "dropped a unclear item without checked evidence" in caplog.text


def test_compact_merge_keeps_checked_items_of_the_previous_summary(tmp_path):
    """An item the previous v7 summary checked may be carried on with its old id; a new id
    must be a folded message. Earlier edits are kept, the new ones appended."""
    prev = Recap(learning=Learning(unclear=[Evidence(concept="负的 z", evidence_id="m0")]),
                 handoff=Handoff(summary="旧"), edits=("加了小节「例子」",))
    reply = _reply(unclear=[("负的 z", "m0"), ("编的", "m1")])
    prov, gw = _gw(tmp_path, reply)
    st = state(8, size=5, first_kept=2).model_copy(
        update={"recap": prev, "summary": render_handoff(prev)})
    turns = list(st.turns)
    turns[2] = turns[2].model_copy(update={"edits": ("改写了小节「均值」", "加了小节「例子」")})
    payload, _ = compact(st.model_copy(update={"turns": tuple(turns)}), gw, ACC, cut=4,
                         policy=SMALL)
    assert payload["learning"]["unclear"] == [{"concept": "负的 z", "evidence_id": "m0"}]
    assert payload["dropped"] == [{"field": "unclear", "concept": "编的", "evidence_id": "m1"}]
    assert payload["edits"] == ["加了小节「例子」", "改写了小节「均值」"]
    assert '"summary": "旧"' in prov.calls[0].prompt and "之前已确认的交接信息" in prov.calls[0].prompt


def test_a_stored_request_alone_rechecks_a_reply(tmp_path):
    """A counterfactual rerun (same stored request, another prompt) is held to the same code
    check: `check_reply` needs only the request's params, never the session."""
    prov, gw = _gw(tmp_path, GOOD)
    payload, _ = compact(state(10, size=50), gw, ACC, cut=6, policy=SMALL)
    params = prov.calls[0].params
    assert params["evidence_ids"] == [f"m{i}" for i in range(6)]
    other = CompactReply.model_validate_json(
        _reply(unclear=[("负的 z", "m2"), ("编的", "m8")]))
    learning, dropped = check_reply(other, params)
    assert learning.model_dump(mode="json") == payload["learning"]
    assert [d.evidence_id for d in dropped] == ["m8"]
    # a request without the param allows nothing (fail closed)
    assert check_reply(other, {})[0].unclear == []


def test_compact_merges_a_legacy_summary_as_text(tmp_path):
    prov, gw = _gw(tmp_path)
    st = state(6, size=5).model_copy(update={"recap": Recap(version="legacy",
                                                            legacy_text="问了什么：\n- 旧"),
                                             "summary": "问了什么：\n- 旧"})
    compact(st, gw, ACC, cut=3, policy=SMALL)
    assert "以下是之前已确认的交接信息。新对话未明确改变的内容仍然有效。请根据新增信息更新交接，保留尚未解决的学习记录。\n问了什么：\n- 旧" in prov.calls[0].prompt


@pytest.mark.parametrize("reply", ["只是一段话", "", '{"handoff": {}}',
                                   _reply(summary="字" * 400)])
def test_compact_refuses_unparseable_or_over_long_summaries(tmp_path, reply):
    _, gw = _gw(tmp_path, reply)
    with pytest.raises(ValueError, match="unusable"):
        compact(state(10, size=50), gw, ACC, cut=6, policy=SMALL)


def _raising_gw(tmp_path, exc):
    def reply(model, req):
        raise exc
    prov = ScriptedChatProvider("scripted", _no_chat, reply)
    return Gateway(GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True),
                   CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov})


@pytest.mark.parametrize("exc", [ProviderRefusalError("refused"),
                                 ProviderTransientError("timeout"),
                                 ProviderUnavailableError("no provider")])
def test_compact_treats_a_refusal_or_transient_error_as_an_unusable_summary(tmp_path, exc):
    gw = _raising_gw(tmp_path, exc)
    with pytest.raises(ValueError, match="unusable") as info:
        compact(state(10, size=50), gw, ACC, cut=6, policy=SMALL)
    assert info.value.__cause__ is not None


def test_compact_still_propagates_a_rejected_provider_error(tmp_path):
    gw = _raising_gw(tmp_path, ProviderRejectedError("HTTP 402", status_code=402))
    with pytest.raises(ProviderRejectedError):
        compact(state(10, size=50), gw, ACC, cut=6, policy=SMALL)


def test_maintain_over_the_hard_cap_survives_a_refused_summary(tmp_path, caplog):
    gw = _raising_gw(tmp_path, ProviderRefusalError("refused"))
    _, put, refold = _real(state(16, size=50, summary="旧"))
    with caplog.at_level(logging.WARNING, logger="evowiki.assistant.compact"):
        st, calls = maintain(refold(), gw, ACC, policy=SMALL, language="zh",
                             append=lambda p: put("compaction", p), refold=refold)
    assert calls == [] and st.first_kept == 7 and st.recap.legacy_text == "旧"
    assert "compaction failed for s1, retried next turn" in caplog.text


def test_compact_refuses_when_every_folded_turn_errored_and_no_summary(tmp_path):
    prov, gw = _gw(tmp_path)
    st = _errored(state(6, size=5), 0, 1)
    with pytest.raises(ValueError, match="nothing to compact"):
        compact(st, gw, ACC, cut=2, policy=SMALL)
    assert prov.calls == []
    # with an earlier summary there is still something to carry forward
    payload, _ = compact(st.model_copy(update={"summary": "旧"}), gw, ACC, cut=2, policy=SMALL)
    assert payload["first_kept"] == "m2"


def test_compact_passes_the_account_only_to_a_metered_gateway(tmp_path):
    from evowiki.llm.prices import PriceTable
    from evowiki.llm.usage_control import UsageControl

    class Spy:
        def __init__(self):
            self.checks = []

        def check(self, a):
            self.checks.append(a)

        def record(self, *a, **k):
            pass

    meter = Spy()
    prov = ScriptedChatProvider("scripted", _no_chat, lambda m, r: GOOD)
    gw = Gateway(GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True),
                 CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov},
                 usage=UsageControl(meter=meter, prices=PriceTable({})))
    compact(state(6, size=5), gw, ACC, cut=3, policy=SMALL)
    assert meter.checks == [ACC]
    _, plain = _gw(tmp_path / "plain")       # unmetered: the account is dropped, no ValueError
    payload, _ = compact(state(6, size=5), plain, ACC, cut=3, policy=SMALL)
    assert payload["handoff"]["summary"] == "讲了 z 分数"


def test_compact_prompt_states_the_schema_and_the_evidence_rule():
    for key in ('"handoff"', '"summary"', '"decisions"', '"active_requests"',
                '"learning"', '"unclear"', '"understood"',
                '"concept"', '"evidence_id"'):
        assert key in COMPACT_SYSTEM
    body = strip_marks(COMPACT_SYSTEM)
    editable = COMPACT_SYSTEM[COMPACT_SYSTEM.index("⟦g:compact⟧"):COMPACT_SYSTEM.index("⟦/g⟧")]
    # field meaning, evidence and boundaries are outside the editable span
    for rule in ("消息开头的真实编号", "还没答的题", "要求仅在本次会话有效",
                 "由代码记录", "不能推广到同一句话中的其他知识点", "本轮没提到旧知识点：原样保留",
                 "普通提问不代表不懂", "摘要不构成删除、修改或执行页面操作的授权",
                 "summary 使用空字符串"):
        assert rule in body and rule not in editable
    assert SUMMARY_MAX_CHARS == 2000 and "不超过 2000 字" in COMPACT_SYSTEM
    assert "不按对话顺序复述" in body


def test_worth_summarising_skips_a_lone_greeting():
    hi = Turn(message_id="m0", user_text="你好", selection=None, answer="你好！想聊哪一页？")
    assert not worth_summarising((hi,))
    assert not worth_summarising(())
    assert worth_summarising((hi.model_copy(update={"edits": ("加了小节「例子」",)}),))
    assert worth_summarising((hi.model_copy(update={"asks": ("你想先看哪个？",)}),))
    long = Turn(message_id="m1", user_text="z 分数是什么", selection=None,
                answer="字" * CLOSE_MIN_TOKENS)
    assert worth_summarising((hi, long))
    errored = long.model_copy(update={"answer": None})
    assert not worth_summarising((hi, errored))


# ---- maintain (real MemorySessionStore + fold) ----

def _real(st):
    """A real store + fold behind `st`: each turn becomes user (+ assistant) entries, an earlier
    summary a compaction entry (pre-v7 text) at the end. Returns (store, put, refold)."""
    store = MemorySessionStore()
    store.create(INFO)
    at = datetime(2026, 10, 8, tzinfo=UTC)
    n = [0]

    def put(type_, payload):
        n[0] += 1
        store.append("s1", f"e{n[0]}", type_, payload, at)

    for t in st.turns:
        put("user", {"message_id": t.message_id, "text": t.user_text, "selection": t.selection})
        if t.answer is not None:
            put("assistant", {"message_id": f"a_{t.message_id}", "text": t.answer})
    if st.summary:
        put("compaction", {"summary": st.summary, "first_kept": st.turns[st.first_kept].message_id})
    return store, put, lambda: fold(INFO, store.entries("s1"))


def _maintain(tmp_path, reply, st, policy=SMALL):
    _, gw = _gw(tmp_path, reply)
    _, put, refold = _real(st)
    log = []

    def append(p):
        log.append(p)
        put("compaction", p)

    folded = refold()
    assert folded.first_kept == st.first_kept
    return maintain(folded, gw, ACC, policy=policy, language="zh", append=append,
                    refold=refold), log


def test_maintain_compacts_when_over_budget(tmp_path):
    (st, calls), log = _maintain(tmp_path, GOOD, state(10, size=50))
    assert len(calls) == 1 and len(log) == 1
    assert log[0]["first_kept"] == "m6" and "omitted" not in log[0]
    assert log[0]["learning"]["unclear"] == [{"concept": "负的 z", "evidence_id": "m2"}]
    # the state was re-folded from the real log: the new summary + the four kept turns
    assert st.first_kept == 6 and st.summary == render_handoff(st.recap)
    assert "讲了 z 分数" in st.summary and "他说不懂或答错过：\n- 负的 z" in st.summary
    assert [e.concept for e in st.recap.learning.unclear] == ["负的 z"]
    assert history_tokens(st) <= SMALL.history_budget


def test_maintain_does_nothing_within_budget(tmp_path):
    (st, calls), log = _maintain(tmp_path, GOOD, state(5, size=50))
    assert log == [] and calls == [] and st.first_kept == 0


def test_maintain_failed_summary_under_hard_cap_stores_nothing(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="evowiki.assistant.compact"):
        (st, calls), log = _maintain(tmp_path, "坏", state(12, size=50))   # 1248 <= 1500
    assert log == [] and calls == [] and st.first_kept == 0
    assert "compaction failed for s1, retried next turn" in caplog.text


def test_maintain_failed_summary_over_hard_cap_drops_oldest_turns_visibly(tmp_path, caplog):
    # 16 turns * 104 + the old summary (its text, with the hand-over head) > hard cap 1500
    with caplog.at_level(logging.WARNING, logger="evowiki.assistant.compact"):
        (st, calls), log = _maintain(tmp_path, "坏", state(16, size=50, summary="旧"))
    assert calls == [] and len(log) == 1
    assert log[0]["omitted"] == 7 and log[0]["reason"] == "summary_failed"
    assert log[0] == {"summary": "旧", "first_kept": "m7", "omitted": 7,
                      "reason": "summary_failed"}
    assert st.first_kept == 7 and st.recap.legacy_text == "旧"
    assert history_tokens(st) <= SMALL.history_budget
    assert "更早的 7 轮对话已省略（摘要生成失败）" in [m.text for m in st.messages]
    assert "dropped the oldest 7 turn(s)" in caplog.text


def test_maintain_keeps_a_v7_summary_when_it_drops_turns(tmp_path):
    gw = _raising_gw(tmp_path, ProviderRefusalError("refused"))
    _, put, refold = _real(state(16, size=50))
    v7 = {"version": "compact@v7", "handoff": {"summary": "旧"},
          "learning": {"unclear": [{"concept": "负的 z", "evidence_id": "m0"}]},
          "edits": ["加了小节「例子」"]}
    put("compaction", {**v7, "first_kept": "m0"})
    st, _ = maintain(refold(), gw, ACC, policy=SMALL, language="zh",
                     append=lambda p: put("compaction", p), refold=refold)
    assert st.first_kept > 0 and st.recap.edits == ("加了小节「例子」",)
    assert st.recap.handoff.summary == "旧"
    assert [e.concept for e in st.recap.learning.unclear] == ["负的 z"]


def test_maintain_drops_everything_when_no_turn_fits(tmp_path):
    (st, _calls), log = _maintain(tmp_path, "坏", state(3, size=700))       # 1404 a turn
    assert log[0]["omitted"] == 3 and log[0]["first_kept"] is None
    assert st.first_kept == 3 and history_tokens(st) == 0


def test_maintain_skips_a_reply_that_does_not_fit_the_schema(tmp_path, caplog):
    """compact@v7 is a JSON call: a reply the gateway cannot parse (LLMParseError, after its own
    retry) is a bad summary like any other — this turn is not compacted, the turn goes on."""
    class Boom:
        metered = False

        def call(self, *a, **k):
            raise LLMParseError("bad json")

    store, put, refold = _real(state(10, size=50))
    with caplog.at_level(logging.WARNING, logger="evowiki.assistant.compact"):
        st, calls = maintain(refold(), Boom(), ACC, policy=SMALL, language="zh",
                             append=lambda p: put("compaction", p), refold=refold)
    assert calls == [] and st.first_kept == 0
    assert [e.type for e in store.entries("s1")].count("compaction") == 0
    assert "compaction failed for s1, retried next turn" in caplog.text


def test_a_summary_has_no_field_for_what_waits_and_keeps_his_words():
    """User ruling 2026-11-04: structure, no literal rule: 「草稿」 in what he studies stays."""
    from evowiki.assistant.summary import Handoff, tidy_handoff

    assert "pending" not in Handoff.model_fields
    h = tidy_handoff(Handoff.model_validate(
        {"summary": "他在学写作：先写草稿再改。", "pending": [{"task": "拆页草稿等确认"}]}))
    assert h.summary == "他在学写作：先写草稿再改。" and "pending" not in h.model_dump()
