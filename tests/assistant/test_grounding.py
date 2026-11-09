"""Tiers and grounding (stage 3 T1, design B3): every assistant tool has a code tier; a direct tool
runs only on the learner's explicit words of THIS message (or a reply of this turn), at least four
characters, or it checks its own grounds; read-tier results are fenced and stubbed, add_source's
report is not."""

from unittest.mock import MagicMock

from evowiki.agent.loop import _framed
from evowiki.agent.tools import ToolContext, ToolResult
from evowiki.assistant.loop import turn_elide
from evowiki.assistant.tools import build_tools
from evowiki.assistant.tools.actions import ASKED_NOT_QUOTED, Grounded, UndoArgs, build_action_tools
from evowiki.assistant.tools.ask_user import AskUserTool
from evowiki.assistant.tools.grounding import SELF_CHECKING
from evowiki.assistant.tools.read_more import ReadDeps, build_read_tools
from evowiki.assistant.turn_skills import inert_tools
from evowiki.harness.pack import HarnessPack
from tests.assistant.test_grounded_actions import Undo


def every_tool(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    more = [*build_read_tools(ReadDeps(MagicMock(), MagicMock())),
            *build_action_tools(MagicMock())]
    built = build_tools(pages=MagicMock(), kview=MagicMock(), more=more, sources=MagicMock(),
                        sandbox=MagicMock()).values()
    return [*built, AskUserTool(), *inert_tools()]


def test_every_tool_has_a_tier_and_every_direct_one_is_grounded(monkeypatch):
    tools = every_tool(monkeypatch)
    by = {t.spec.name: t for t in tools}
    assert all(t.spec.tier in ("read", "draft", "direct", "observe") for t in tools)
    direct = {n for n, t in by.items() if t.spec.tier == "direct"}
    for name in direct:
        t = by[name]
        assert isinstance(t, Grounded) or name in SELF_CHECKING, name
    # the ones the design names (B3.2)
    assert {"make_cards", "fix_card", "drop_card", "postpone_reviews", "bot_settings",
            "mark_source_use", "set_exemplar", "undo", "add_source",
            "remember_preference", "remember_about_me", "report_problem", "confirm"} <= direct
    assert by["split_page"].spec.tier == "draft" and by["new_goal"].spec.tier == "draft"
    assert by["read_page"].spec.tier == "read" and by["load_skill"].spec.tier == "read"
    assert by["offer"].spec.tier == "draft"


def ctx(words=("帮我撤销刚才那个",), earlier=("先撤销上周那个",)):
    return ToolContext(user_id="u1", session_id="s1", current_words=words,
                       user_words=(*words, *earlier))


def test_a_quote_from_an_earlier_message_or_shorter_than_four_is_refused():
    inner = Undo()
    tool = Grounded(inner)
    for asked in ("撤销上周那个", "撤销", "好"):
        res = tool.run({"action_id": "last", "asked": asked}, ctx(words=("好，撤销",)))
        assert res.error is not None and ASKED_NOT_QUOTED in res.content, asked
    assert inner.calls == []
    assert tool.run({"action_id": "last", "asked": "撤销刚才那个"}, ctx()).error is None
    assert inner.calls == [{"action_id": "last"}]


def test_in_a_resumed_turn_the_message_the_question_was_asked_in_counts_as_this_message():
    """A turn that resumes a question (ask_user) has his answer and the message it was asked in
    as this message (assistant.loop: current_words)."""
    inner = Undo()
    res = Grounded(inner).run({"action_id": "last", "asked": "对，撤销那个改动"},
                              ctx(words=("嗯", "对，撤销那个改动")))
    assert res.error is None and inner.calls == [{"action_id": "last"}]


def test_add_source_report_is_not_fenced_or_stubbed(monkeypatch):
    by = {t.spec.name: t for t in every_tool(monkeypatch)}
    report = ToolResult(content="已加入资料（src1），正在读。")
    assert _framed(by["add_source"], report) == report.content          # tier direct
    assert _framed(by["read_page"], report) != report.content            # tier read: fenced
    policy = turn_elide(HarnessPack(1, {}), list(by.values()))
    assert "add_source" not in policy.read_tools and "web_search" in policy.read_tools
    assert "load_skill" not in policy.read_tools and "read_ref" not in policy.read_tools


def test_undo_args_model_is_unchanged():
    assert set(UndoArgs.model_fields) == {"action_id", "other_sessions"}


def test_a_whole_short_message_counts_but_a_short_piece_of_a_longer_one_does_not():
    """H3: 「考考我」「撤销」「整理」 typed alone ground their tool; 「撤销」 out of 「帮我撤销」 does not."""
    for message in ("考考我", "撤销", "整理", "撤销！", " 整理 "):
        inner = Undo()
        res = Grounded(inner).run({"action_id": "last", "asked": message.strip(" ！")},
                                  ctx(words=(message,), earlier=()))
        assert res.error is None, message
    inner = Undo()
    res = Grounded(inner).run({"action_id": "last", "asked": "撤销"},
                              ctx(words=("帮我撤销",), earlier=()))
    assert res.error is not None and inner.calls == []


def test_remember_quotes_come_from_this_message(monkeypatch):
    """LOW: remember_* write at once only from this message's words; an earlier message's go
    pending through land_preference (tests/assistant/test_stated_preferences.py), never straight in."""
    by = {t.spec.name: t for t in every_tool(monkeypatch)}
    res = by["remember_about_me"].run(
        {"applies": "lasting", "section": "我是谁", "quote": "我是护士", "text": "护士"},
        ctx(words=("今天讲什么",), earlier=("我是护士",)))
    assert res.error is not None and res.action is None


def _pending(words, pending: bool) -> ToolContext:
    return ctx(words=words, earlier=()).model_copy(update={"has_pending": lambda: pending})


def test_a_bare_ok_with_an_op_pending_grounds_only_confirm(monkeypatch):
    """While an op waits for his yes, 「好」 is that yes: it grounds no other tool."""
    from tests.assistant.memory_ledger import MemoryLedger
    by = {t.spec.name: t for t in every_tool(monkeypatch)}
    inner = Undo()
    res = Grounded(inner).run({"action_id": "last", "asked": "好"}, _pending(("好",), True))
    assert res.error is not None and inner.calls == []
    cards = by["make_cards"].run({"page": "p", "cards": [{"section_id": "s1"}], "asked": "好"},
                                _pending(("好",), True))
    assert cards.error is not None and "asked" in cards.content
    from types import SimpleNamespace

    from evowiki.assistant.tools.actions import ActionDeps, build_action_tools
    deps = ActionDeps(repo=SimpleNamespace(load=lambda uid: SimpleNamespace(state=None)),  # type: ignore[arg-type]
                      ledger=MemoryLedger(), clock=lambda: None, new_id=str)  # type: ignore[arg-type]
    [pref] = [t for t in build_action_tools(deps) if t.spec.name == "remember_preference"]
    res = pref.run({"applies": "lasting", "quote": "好", "text": "好"}, _pending(("好",), True))
    assert res.error is not None and res.action is None
    # confirm itself takes it (assistant.pending: confirming=True)
    from evowiki.assistant.tools.grounding import quoted_now
    assert quoted_now("好", _pending(("好",), True), confirming=True)


def test_short_commands_typed_alone_still_work_when_nothing_is_pending():
    for word in ("撤销", "考考我"):
        inner = Undo()
        res = Grounded(inner).run({"action_id": "last", "asked": word}, _pending((word,), False))
        assert res.error is None and inner.calls == [{"action_id": "last"}], word
