"""Regression cases from the live DeepSeek checks of Memory (2026-10-29, branch
claude/memory-live-2092eb, `.live-results/run1|run2/memory-c-folded.json` /
`memory-d-consolidate.json`): the segments as they were and the replies as recorded
(consolidate@v1 in run 1, @v2 in run 2), run through today's code. No paid call. C_RUN1 was recorded
again on Haiku 5.5 at consolidate@v11 (2026-11-04, one call, $0.0004).

C: he stated the decision himself and the assistant answered 「可以」. Both runs cited both
messages; the old rule read the assistant line as a proposal his words came before and dropped it
(confirmation_before_proposal). Now his words back it and the assistant line is background. Its
「这一页」 scope came without a page id (the model is never shown one): the session's page.
D: 「σ 到底是什么」 was answered in the same turn and still kept as an open question: that is
the prompt's to fix (v3 gives the example); code keeps what the model says."""

from datetime import timedelta

from evowiki.assistant.session import MemorySessionStore, fold
from evowiki.memory.consolidate import CONSOLIDATE_PROMPT_VERSION, CONSOLIDATE_PURPOSE, consolidate
from evowiki.memory.lander import Landed, NullPreferenceLander, NullRuleGuard, Refused
from evowiki.memory.store import InMemoryMemoryStore
from evowiki.wikiapp.preferences import NOT_GLOBAL
from tests.memory.test_consolidate import FakeLander, T, _gateway, _session, cand, ev

C_SAID = "我们先定个做法：这一页讲到方差的时候，用具体数字的例子来讲，不用字母推导。可以吗？"
C_TURNS = [("msg_0028", C_SAID,
            "msg_0036", "可以，就这么定。讲方差就用具体数字的例子，不做字母推导。"),
           ("msg_0039", "z 分数是什么？",
            "msg_0040", "z 分数就是一句话：它告诉你，某个数离平均数有几个标准差远。")]
C_RUN1 = [cand("decision", "讲方差时用具体数字的例子来讲，不用字母推导。", topic="方差", scope_type="page",
               evidence=[ev("msg_0028", quote="这一页讲到方差的时候，用具体数字的例子来讲，不用字母推导。可以吗？"),
                         ev("msg_0036", "assistant", quote="可以，就这么定。讲方差就用具体数字的例子，不做字母推导。")])]
C_RUN2 = [cand("preference", "讲方差时用具体数字的例子，不用字母推导",
               ev("msg_0028", quote=C_SAID), scope_type="page"),
          cand("decision", "讲方差时用具体数字例子，不用字母推导",
               ev("msg_0028", quote=C_SAID), ev("msg_0036", "assistant"), scope_type="page"),
          cand("open_question", "z 分数是什么？", ev("msg_0039", quote="z 分数是什么？"),
               scope_type="concept", scope_id="z-score")]

D_SIGMA = "这题讲简单点：z 分数里的 σ 到底是什么？"
D_ASK_PLAN = "帮我想一个这周复习 z 分数的计划。"
D_TURNS = [("msg_0039", D_ASK_PLAN,
            "msg_0049", ("要不要我把这个计划存成一个复习目标、每天提醒你走哪一步？或者这页内容"
                         "还空着，我也可以先陪你把它补起来，复习时就有东西可看。")),
           ("msg_0052", D_SIGMA,
            "msg_0053", ("σ 就是「标准差」（standard deviation）的简写，是个希腊字母。它只回答一个"
                         "问题：这组数据有多「散」——也就是这些数离平均值平均有多远。"))]
D_RUN2 = [cand("decision", "这周复习 z 分数的计划已制定", ev("msg_0039", quote=D_ASK_PLAN),
               ev("msg_0049", "assistant")),
          cand("open_question", "z 分数里的 σ 到底是什么？", ev("msg_0052", quote=D_SIGMA))]


def _run(tmp_path, turns, reply, seen=None, lander=None):
    sessions, mem = MemorySessionStore(), InMemoryMemoryStore()
    _session(sessions, "s1", "u1", turns)
    state = fold(sessions.info("s1"), sessions.entries("s1"))
    ids = iter(range(1, 100))
    return consolidate(state, sessions=sessions, store=mem,
                       gateway=_gateway(tmp_path, reply, seen), account=None,
                       lander=lander or NullPreferenceLander(), guard=NullRuleGuard(), first=0,
                       through=len(turns), now=T + timedelta(hours=1),
                       new_id=lambda p: f"{p}{next(ids)}")


def _kinds(r):
    return [(i.kind, i.content) for i in r.items_written], [why for _, why in r.dropped]


def test_c_run1_his_own_decision_with_the_assistants_yes_is_kept_on_this_page(tmp_path):
    seen = []
    r = _run(tmp_path, C_TURNS, C_RUN1, seen)
    [it] = r.items_written
    assert (it.kind, it.scope_type, it.scope_id) == ("decision", "page", "p1")
    assert [e.message_id for e in it.evidence] == ["msg_0028", "msg_0036"]
    [req] = seen
    assert (req.purpose, req.prompt_version) == (CONSOLIDATE_PURPOSE, CONSOLIDATE_PROMPT_VERSION)


def test_c_run2_the_page_decision_takes_the_preference_from_the_same_message(tmp_path):
    """The page decision is kept; the preference from his same message is that decision
    (same_as_decision), never sent to S3. The answered 「z 分数是什么？」 is still kept: the
    prompt's to fix."""
    lander = FakeLander(Refused(NOT_GLOBAL))
    r = _run(tmp_path, C_TURNS, C_RUN2, lander=lander)
    items, dropped = _kinds(r)
    assert items == [("decision", "讲方差时用具体数字例子，不用字母推导"),
                     ("open_question", "z 分数是什么？")]
    assert dropped == ["same_as_decision"] and lander.keys == []
    assert (r.items_written[0].scope_type, r.items_written[0].scope_id) == ("page", "p1")


def test_c_global_decision_restating_a_landed_preference_is_left_to_it(tmp_path):
    pref, dec = C_RUN2[0].copy(), C_RUN2[1].copy()
    pref["scope_type"] = dec["scope_type"] = "global"
    dec["content"] = pref["content"]
    r = _run(tmp_path, C_TURNS, [dec, pref])                 # the preference waits for S3
    assert _kinds(r) == ([], ["same_as_preference"]) and len(r.pending) == 1


C_RUN3 = [cand("preference", "讲方差时用具体数字的例子，不用字母推导",
               ev("msg_0028", quote="这一页讲到方差的时候，用具体数字的例子来讲，不用字母推导")),
          cand("decision", "本页讲方差采用具体数字例子，不做字母推导",
               ev("msg_0028", quote=C_SAID), ev("msg_0036", "assistant")),
          cand("open_question", "z 分数和正态分布的关系", ev("msg_0040", "assistant"))]
C_V4 = [cand("decision", "这一页讲方差用具体数字的例子，不用字母推导",
             ev("msg_0028", quote=C_SAID), scope_type="page"),
        cand("preference", "讲方差时用具体数字的例子，不用字母推导",
             ev("msg_0028", quote=C_SAID))]


def test_c_run3_v3_reply_made_both_global(tmp_path):
    """The @v3 live reply (memory-consolidate-v3-c): no scope at all, so a global decision and a
    global rule card. Code cannot tell 「这一页」 from a scope it was not given: v4's prompt."""
    lander = FakeLander(Landed(spec_item_id="si1", pending_action_id="act1"))
    r = _run(tmp_path, C_TURNS, C_RUN3, lander=lander)
    items, dropped = _kinds(r)
    assert [k for k, _ in items] == ["preference", "decision"]
    assert all(i.scope_type == "global" for i in r.items_written)
    assert dropped == ["not_from_user"] and len(lander.keys) == 1


def test_c_v4_shaped_reply_is_one_page_decision_and_no_rule_card(tmp_path):
    seen = []
    lander = FakeLander(Landed(spec_item_id="si1", pending_action_id="act1"))
    r = _run(tmp_path, C_TURNS, C_V4, seen, lander=lander)
    [it] = r.items_written
    assert (it.kind, it.scope_type, it.scope_id) == ("decision", "page", "p1")
    assert [why for _, why in r.dropped] == ["same_as_decision"] and lander.keys == []
    assert "不记成偏好" in seen[0].system


def test_d_run2_his_request_backs_a_decision_and_the_answered_question_stays(tmp_path):
    """What today's code makes of the recorded @v2 reply. 「帮我想一个计划」 is his words with
    content, so the plan the model called a decision is kept (the assistant line is background):
    whether v3 still calls it one is what the live check shows."""
    items, dropped = _kinds(_run(tmp_path, D_TURNS, D_RUN2))
    assert items == [("decision", "这周复习 z 分数的计划已制定"),
                     ("open_question", "z 分数里的 σ 到底是什么？")]
    assert dropped == []


def test_d_v3_shaped_reply_keeps_no_answered_question(tmp_path):
    seen = []
    items, _ = _kinds(_run(tmp_path, D_TURNS, [], seen))
    assert items == []
    assert "只是问了一句" in seen[0].system
