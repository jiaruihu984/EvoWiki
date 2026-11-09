"""A pasted note on how he likes to be taught (it replaced the retired 「从文字导入讲解规则」,
the product specification): the assistant splits it into rules,
one `remember_preference(pasted=true)` each; every rule is a pending card whose source sentence
(`quote`, checked by code against his message) is its evidence; a rule whose sentence is not in
the note, or that has none, is refused and never reaches the spec. Real Postgres, the real
ledger, a SCRIPTED chat model - nothing here calls a paid API."""

import pytest

from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import run_turn
from evowiki.assistant.session import fold
from evowiki.assistant.skills import library
from evowiki.assistant.tools.actions import NOT_QUOTED_ANY, PASTED_NO_QUOTE
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.wikiapp.teachspec import page_teach
from tests.assistant.test_action_tools import INFO, chat_turns, turn_deps
from tests.assistant.test_stated_preferences import ctx, own_items, prefs_env, remember

pytestmark = pytest.mark.usefixtures("every_skill")


@pytest.fixture
def env(pool):
    return prefs_env(pool)

NOTE = ("下面是我希望你给我讲东西的方式：\n"
        "讲新东西先举数值例子再给公式。\n"
        "新符号第一次出现要解释。\n"
        "我的笔记都放在 D 盘的 notes 文件夹。")
EXAMPLE_FIRST = ("讲新东西先举数值例子再给公式。", "先给一个数值例子，再给公式")
SYMBOLS = ("新符号第一次出现要解释。", "新符号第一次出现要解释")      # word for word: still a card
MADE_UP = ("每页最后都要有一道练习题。", "每页最后都放一道练习题")        # not in the note


def _call(n: int, quote: str, text: str) -> ToolCall:
    return ToolCall(id=f"c{n}", name="remember_preference",
                    args={"applies": "lasting", "pasted": True, "quote": quote, "text": text})


def test_a_pasted_note_becomes_one_pending_card_per_rule_with_its_source_sentence(tmp_path, env):
    calls = tuple(_call(n, q, t) for n, (q, t) in enumerate((EXAMPLE_FIRST, SYMBOLS, MADE_UP), 1))
    prov = chat_turns(ChatTurn(text="", tool_calls=calls),
                      ChatTurn(text="我把说明里的两条讲法记成了待确认的规则，你确认后就会用上。"))
    deps = turn_deps(tmp_path, env, prov, list(env.tools.values()))
    ev = list(run_turn(deps, INFO, MessageIn(text=NOTE)))
    mid = fold(INFO, deps.store.entries("s1")).turns[-1].message_id

    cards = [e for e in ev if e.type == "action"]
    assert [(c.kind, c.status, c.confirm) for c in cards] == [("rule", "proposed", True)] * 2
    items = own_items(env)
    assert [i.text for i in items] == [EXAMPLE_FIRST[1], SYMBOLS[1]]
    evidence = env.state().item_evidence
    for item, (quote, _) in zip(items, (EXAMPLE_FIRST, SYMBOLS), strict=True):
        # pending, owned by nobody yet; its source is the sentence of this message, verbatim
        assert (item.origin, item.authority, item.origin_ref) == (
            "assistant_drafted", "pending", f"s1/{mid}")
        assert evidence[item.item_id] == f"你说过「{quote}」"
        assert quote in NOTE
        row = next(r for r in env.rows() if r.content.get("item_id") == item.item_id)
        assert (row.kind, row.status, row.content["quote"]) == ("rule", "proposed", quote)
        assert row.content["provenance"] == "explicit_current"
    # a requirement the note does not have is refused: no card, not in the spec
    assert MADE_UP[1] not in [i.text for i in items] and len(env.rows()) == 2
    third = prov.chat_calls[1].messages[-1]
    assert NOT_QUOTED_ANY in third.text
    # nothing teaches before his 确认
    assert not {EXAMPLE_FIRST[1], SYMBOLS[1]} & set(page_teach(env.state(), env.pid).guidelines)
    env.ledger.approve("u1", cards[0].action_id)
    assert EXAMPLE_FIRST[1] in page_teach(env.state(), env.pid).guidelines


def test_a_pasted_rule_needs_its_source_sentence(env):
    res = remember(env, ctx(NOTE), pasted=True, quote="", text=EXAMPLE_FIRST[1])
    assert res.error is not None and PASTED_NO_QUOTE in res.content
    off = remember(env, ctx(NOTE), pasted=True, quote=MADE_UP[0], text=MADE_UP[1])
    assert off.error is not None and NOT_QUOTED_ANY in off.content
    assert env.rows() == [] and own_items(env) == []


def test_the_remember_skill_says_how_to_split_a_pasted_note():
    s = library().get("remember")
    assert "pasted=true" in s.rules and "不要自己补充" in s.rules
