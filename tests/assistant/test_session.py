from datetime import timedelta

import pytest

from evowiki.assistant.session import (
    EntryConflict,
    MemorySessionStore,
    SessionInfo,
    fold,
    session_out,
)
from evowiki.assistant.summary import render_handoff
from tests.assistant.fakes import INFO, T0, run


def test_fold_builds_turns_messages_and_decisions():
    st = fold(INFO, run(MemorySessionStore()).entries("s1"))
    assert [(t.user_text, t.answer) for t in st.turns] == [("问一", "答一"), ("问二", None)]
    assert st.turns[0].selection == {"text": "选中", "heading": "h"}
    assert [m.role for m in st.messages] == ["user", "assistant", "user"]
    assert st.messages[1].proposals == ("pr1",) and st.decisions == {"pr1": "rejected"}
    # the stored quote is replayed on the user message; assistant messages carry none
    assert st.messages[0].selection.model_dump() == {"kind": "text", "text": "选中", "heading": "h", "artifact_id": None}
    assert st.messages[1].selection is None
    out = session_out(st)
    assert out.session_id == "s1" and out.page_id == "p1" and out.recap is None
    # the raised proposal comes back with its draft, as the live event carried it
    assert [(p.proposal_id, p.draft.kind, p.draft.heading, p.draft.text) for p in out.proposals] == [
        ("pr1", "add_section", "补充", "正文")]


def test_seq_is_dense_and_entries_are_ordered():
    store = run(MemorySessionStore())
    assert [e.seq for e in store.entries("s1")] == list(range(1, 9))


def test_compaction_sets_summary_and_first_kept():
    store = run(MemorySessionStore())
    store.append("s1", "e9", "compaction", {"summary": "前面聊了 z", "first_kept": "m2"}, T0)
    st = fold(INFO, store.entries("s1"))
    assert st.summary.endswith("\n前面聊了 z") and "不是他的授权" in st.summary
    assert st.first_kept == 1 and st.recap.legacy


def test_find_proposal_and_unknown_session():
    store = run(MemorySessionStore())
    info, entry = store.find_proposal("pr1")
    assert info.session_id == "s1" and entry.payload["draft"]["heading"] == "补充"
    with pytest.raises(LookupError):
        store.info("nope")
    with pytest.raises(LookupError):
        store.find_proposal("nope")


def test_failed_decision_is_not_a_decision_and_latest_wins():
    store = run(MemorySessionStore())
    store.append("s1", "e9", "decision",
                 {"proposal_id": "pr1", "status": "failed", "applied": [], "detail": "x"}, T0)
    assert fold(INFO, store.entries("s1")).decisions == {"pr1": "rejected"}
    store.append("s1", "e10", "decision",
                 {"proposal_id": "pr1", "status": "accepted", "applied": ["sec"], "detail": ""}, T0)
    assert fold(INFO, store.entries("s1")).decisions == {"pr1": "accepted"}


def test_latest_decision_per_proposal_includes_failed_and_comes_back_on_session_out():
    store = run(MemorySessionStore())                  # pr1 rejected
    assert [d.model_dump() for d in session_out(fold(INFO, store.entries("s1"))).decisions] == [
        {"proposal_id": "pr1", "status": "rejected", "applied": (), "detail": "",
         "action_id": "", "can_undo": False}]
    store.append("s1", "e9", "decision",
                 {"proposal_id": "pr1", "status": "failed", "applied": [],
                  "detail": "这一段已钉住，不能改写"}, T0)
    st = fold(INFO, store.entries("s1"))
    assert st.latest["pr1"].status == "failed"
    assert st.latest["pr1"].detail == "这一段已钉住，不能改写"
    store.append("s1", "e10", "decision",
                 {"proposal_id": "pr1", "status": "accepted", "applied": ["sec"], "detail": ""},
                 T0)
    out = session_out(fold(INFO, store.entries("s1")))
    assert [(d.status, d.applied) for d in out.decisions] == [("accepted", ("sec",))]


def test_legacy_failed_detail_without_chinese_folds_to_the_internal_code():
    store = run(MemorySessionStore())
    store.append("s1", "e9", "decision",
                 {"proposal_id": "pr1", "status": "failed", "applied": [],
                  "detail": "Traceback (most recent call last): KeyError 'sec_42'"}, T0)
    assert fold(INFO, store.entries("s1")).latest["pr1"].detail == "internal"


def test_duplicate_session_and_entry_id_are_refused():
    store = run(MemorySessionStore())
    with pytest.raises(ValueError):
        store.create(INFO)
    with pytest.raises(EntryConflict):
        store.append("s1", "e1", "user", {"message_id": "m3", "text": "x", "selection": None}, T0)
    with pytest.raises(LookupError):
        store.append("nope", "e99", "user", {"message_id": "m3", "text": "x"}, T0)


def test_assistant_message_replays_the_turns_stored_tool_calls():
    """GET session carries each answered turn's tool calls (read from the log) so a restored
    turn shows its tool line; the summary is the query / url, else the one source's title."""
    store = run(MemorySessionStore())
    a = lambda i, t, p: store.append("s1", f"e{i}", t, p, T0 + timedelta(seconds=i))
    a(9, "user", {"message_id": "m3", "text": "问三", "selection": None})
    a(10, "tool", {"message_id": "m3", "call_id": "d1", "name": "search_wiki", "effect": "read",
                   "args": {"query": "特征值"}, "ok": True, "content": "…", "sources": []})
    a(11, "tool", {"message_id": "m3", "call_id": "d2", "name": "read_page", "effect": "read",
                   "args": {"page_id": "p2"}, "ok": True, "content": "…",
                   "sources": [{"title": "pca（2/4）", "page_id": "p2"}]})
    a(12, "tool", {"message_id": "m3", "call_id": "d3", "name": "nope", "effect": None,
                   "args": {}, "ok": False, "content": "没有这个工具：nope", "sources": []})
    a(13, "tool", {"message_id": "m3", "call_id": "d4", "name": "fetch_url", "effect": "network",
                   "args": {"url": "https://w/z"}, "ok": False, "content": "x", "sources": []})
    a(13.5, "tool", {"message_id": "m3", "call_id": "d5", "name": "read_path", "effect": "read",
                     "args": {"path_id": "p_pca"}, "ok": True, "content": "…", "sources": [],
                     "summary": "学会 PCA"})
    a(14, "assistant", {"message_id": "a3", "text": "答三", "sources": [], "proposals": [],
                        "tool_calls": 4, "llm_calls": 2})
    msgs = session_out(fold(INFO, store.entries("s1"))).messages
    # the first turn's read_k call; the failed turn (m2) has no assistant message
    assert [(t.name, t.summary, t.ok) for t in msgs[1].tools] == [("read_k", "", True)]
    assert msgs[0].tools == () and msgs[2].tools == ()
    last = msgs[-1]
    assert last.message_id == "a3"
    assert [(t.call_id, t.name, t.effect, t.summary, t.ok) for t in last.tools] == [
        ("d1", "search_wiki", "read", "特征值", True),
        ("d2", "read_page", "read", "pca（2/4）", True),
        ("d4", "fetch_url", "network", "https://w/z", False),
        ("d5", "read_path", "read", "学会 PCA", True)]    # a logged summary is the live line's
    assert last.tools[1].sources[0].page_id == "p2"


def test_a_restored_remember_about_me_row_says_what_happened_to_the_fact():
    """The quiet row's label depends on its ledger card (logged as `quiet_action`): a restored
    session labelled a kept fact 「这次照着用，没有记进档案」 (stage3 walk check 13)."""
    store = run(MemorySessionStore())
    a = lambda i, t, p: store.append("s1", f"e{i}", t, p, T0 + timedelta(seconds=i))
    card = {"action_id": "act_1", "kind": "profile_fact", "title": "记下了：护士",
            "status": "executed", "can_undo": True}
    a(9, "user", {"message_id": "m3", "text": "我是护士", "selection": None})
    a(10, "tool", {"message_id": "m3", "call_id": "f1", "name": "remember_about_me",
                   "effect": "propose", "args": {"applies": "lasting"}, "ok": True,
                   "content": "已记进档案", "sources": [], "quiet_action": card})
    a(11, "tool", {"message_id": "m3", "call_id": "f2", "name": "remember_about_me",
                   "effect": "propose", "args": {"applies": "lasting"}, "ok": True,
                   "content": "等他确认", "sources": [],
                   "quiet_action": {**card, "action_id": "act_2", "status": "proposed"}})
    a(12, "tool", {"message_id": "m3", "call_id": "f3", "name": "remember_about_me",
                   "effect": "propose", "args": {"applies": "this_task"}, "ok": True,
                   "content": "这次照着用", "sources": []})
    a(13, "assistant", {"message_id": "a3", "text": "记下了。", "sources": [], "proposals": [],
                        "tool_calls": 3, "llm_calls": 2})
    last = session_out(fold(INFO, store.entries("s1"))).messages[-1]
    assert [t.label for t in last.tools] == ["记进了你的档案「我」", "等你确认要不要记进档案",
                                             "这次照着用，没有记进档案"]


def test_conversation_title_rule():
    from evowiki.assistant.session import conversation_title
    assert conversation_title("  什么是 z 分数？ ") == "什么是 z 分数"
    assert conversation_title("what is z?") == "what is z"
    assert conversation_title("讲讲这个。") == "讲讲这个"
    assert conversation_title("为什么？？") == "为什么？"           # only ONE trailing char
    assert conversation_title("一二三四五六七八九十" * 3) == "一二三四五六七八九十" * 2 + "一二三四"
    assert conversation_title("   ") == ""
    # 学点新的's goal message: the goal, then a 背景 line — the title is the goal
    assert conversation_title("我要学黑壳虾\n背景：学过高中生物。") == "我要学黑壳虾"


def test_user_sessions_only_owner_and_non_empty():
    from evowiki.assistant.session import SessionInfo
    store = run(MemorySessionStore())
    store.create(SessionInfo(session_id="s2", user_id="u1", page_id=None, note_concept_id=None,
                             created_at=T0))                      # no entries -> not listed
    store.create(SessionInfo(session_id="s3", user_id="u2", page_id=None, note_concept_id=None,
                             created_at=T0))
    store.append("s3", "x1", "user", {"message_id": "m", "text": "q", "selection": None}, T0)
    assert [i.session_id for i in store.user_sessions("u1")] == ["s1"]
    assert [i.session_id for i in store.user_sessions("u2")] == ["s3"]
    assert store.user_sessions("nobody") == []


def _log(*payloads):
    store = MemorySessionStore()
    store.create(INFO)
    for n, (type_, p) in enumerate(payloads, start=1):
        store.append("s1", f"e{n}", type_, p, T0 + timedelta(seconds=n))
    return fold(INFO, store.entries("s1"))


def _turn(i):
    return [("user", {"message_id": f"m{i}", "text": f"问{i}", "selection": None}),
            ("assistant", {"message_id": f"a{i}", "text": f"答{i}", "sources": [],
                           "proposals": [], "tool_calls": 0, "llm_calls": 1})]


V7 = {"version": "compact@v7",
      "handoff": {"summary": "聊了 z 分数", "decisions": ["先不改「均值」一节"],
                  "active_requests": ["公式先解释符号"],
                  "pending": [{"task": "存量摘要的旧字段", "next_step": "读时不管"}]},
      "learning": {"unclear": [{"concept": "负的 z", "evidence_id": "m1"}],
                   "understood": [{"concept": "均值", "evidence_id": "m2"}]},
      "edits": ["改写了小节「均值」"]}


def test_v7_compaction_folds_to_a_recap_and_the_rendered_hand_over():
    st = _log(*_turn(1), *_turn(2), ("compaction", {**V7, "first_kept": None}))
    assert st.first_kept == 2 and not st.recap.legacy
    assert [e.concept for e in st.recap.learning.unclear] == ["负的 z"]
    assert st.summary == render_handoff(st.recap)
    for part in ("聊了 z 分数", "定下的事：\n- 先不改「均值」一节",
                 "他在这次对话里提的要求（只在这次对话有效）：\n- 公式先解释符号",
                 "他说不懂或答错过：\n- 负的 z", "他说懂了或答对过（自述不等于掌握）：\n- 均值",
                 "改过的页面：\n- 改写了小节「均值」", "不是他的授权"):
        assert part in st.summary
    # the learner sees plain items, never the hand-over text
    assert session_out(st).recap.model_dump() == {
        "understood": ("均值",), "unclear": ("负的 z",),
        "edits": ("改写了小节「均值」",)}


def test_empty_v7_compaction_has_no_summary_and_nothing_to_show():
    st = _log(*_turn(1), ("compaction", {"version": "compact@v7", "handoff": {},
                                         "learning": {}, "edits": [], "first_kept": None}))
    assert st.summary == "" and st.first_kept == 1 and session_out(st).recap is None


def test_old_compaction_is_read_as_text_only():
    v6 = "问了什么：z\n讲过什么：无\n还没懂的：负的 z\n改过哪里：无"
    st = _log(*_turn(1), *_turn(2),
              ("compaction", {"summary": v6, "fields": {"还没懂的": ["负的 z"]},
                              "first_kept": "m2"}))
    assert st.first_kept == 1 and st.recap.legacy and st.summary.endswith(v6)
    assert st.recap.learning.unclear == [] and st.recap.edits == ()
    assert session_out(st).recap is None          # never shown as his 还没懂的


def test_omitted_compaction_is_a_visible_notice_and_keeps_the_summary():
    st = _log(*_turn(1), *_turn(2), *_turn(3),
              ("compaction", {**V7, "first_kept": "m3", "omitted": 2,
                              "reason": "summary_failed"}))
    assert st.first_kept == 2 and "聊了 z 分数" in st.summary and len(st.turns) == 3
    notice = st.messages[-1]
    assert notice.role == "assistant" and notice.text == "更早的 2 轮对话已省略（摘要生成失败）"
    assert notice.message_id.startswith("omit_")
    assert session_out(st).messages[-1] == notice


def test_recent_sessions_newest_first_by_page_and_exclude():
    store = MemorySessionStore()
    for i, page in enumerate(["p1", "p2", "p1"]):
        store.create(SessionInfo(session_id=f"s{i}", user_id="u1", page_id=page,
                                 note_concept_id=None, created_at=T0 + timedelta(minutes=i)))
    store.create(SessionInfo(session_id="x", user_id="u2", page_id="p1", note_concept_id=None,
                             created_at=T0))
    ids = lambda xs: [x.session_id for x in xs]
    assert ids(store.recent_sessions("u1", limit=5)) == ["s2", "s1", "s0"]
    assert ids(store.recent_sessions("u1", limit=5, page_id="p1", exclude="s2")) == ["s0"]
    assert ids(store.recent_sessions("u1", limit=1)) == ["s2"]
