"""Task 11 of assistant-actions: an executed row's card and ledger view carry the pages it made, found
by draft key (`made_of`, `ActionOut.made`, `card_of`), so the chat card shows each line's writing
status and path number."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from evowiki.assistant.contracts import ActionEvent
from evowiki.assistant.tools.actions import card_of
from evowiki.bot.actions import ActionRow, made_of
from evowiki.bot.bot_schemas import ActionOut

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
SPLIT = {"path_id": "pa", "page_key": "k", "pages": [
    {"key": "q1", "title": "甲"}, {"key": "q2", "title": "乙", "covers": "讲乙"}, {"key": "q3", "title": "丙"}]}


def made(*ids_keys_n):
    return [{"key": k, "page_id": p, "n": n, "folder": ""} for p, k, n in ids_keys_n]


def row(kind="page_split", status="executed", result=None, content=None) -> ActionRow:
    return ActionRow(action_id="a1", user_id="u1", kind=kind, scope="new_page", level="ask", status=status,
                     source="assistant", summary="拆成 3 页", content=content or SPLIT, content_hash="h",
                     idempotency_key="k", created_at=NOW, result=result)


def test_made_of_reads_the_result_of_an_executed_row_only():
    r = {"made": made(("p1", "q1", 2))}
    assert made_of(row(result=r)) == r["made"]
    assert made_of(row(status="proposed", result=r)) == []
    assert made_of(row(status="undone", result=r)) == []
    assert made_of(row(result=None)) == []
    assert made_of(row(result={"made": [{"key": "q1", "page_id": 3}]})) == []


def test_ledger_out_lists_the_pages_made():
    out = ActionOut.of(row(result={"made": made(("p1", "q1", 2))}))
    assert [(m.key, m.page_id, m.n) for m in out.made] == [("q1", "p1", 2)]
    assert ActionOut.of(row(status="proposed")).made == []


def test_card_items_are_matched_by_key_not_by_order():
    # q2 made no page (its node was already on the path): q1 and q3 keep their own ids and numbers
    card = card_of(row(result={"made": made(("p1", "q1", 2), ("p3", "q3", 3))}))
    assert [(i.key, i.page_id, i.n, i.covers) for i in card.items] == [("q1", "p1", 2, None), ("q3", "p3", 3, None)]
    assert [i.page_id for i in card_of(row(status="proposed")).items] == [None, None, None]


def test_a_merge_card_lists_the_one_page_it_made_with_its_folder():
    content = {"title": "方差和协方差", "pages": [{"key": "pv"}, {"key": "pc"}]}
    m = [{"key": None, "page_id": "pm", "n": None, "folder": "统计"}]
    card = card_of(row(kind="page_merge", content=content, result={"made": m}))
    assert [(i.key, i.title, i.page_id, i.covers) for i in card.items] == [("pm", "方差和协方差", "pm", "在「统计」文件夹里")]


def test_an_executed_goal_card_carries_the_ids_it_opened_as_the_ledger_view_does():
    # a reloaded chat rebuilds the diagnosis under the card from them; the executor's other data stays out
    content = {"goal": "黑壳虾", "pages": [{"key": "b1", "title": "饲养"}]}
    res = {"draft_id": "dr1", "map_id": "m1", "undo": {"secret": 1}, "cost_usd": 0.1}
    card = card_of(row(kind="goal_new", content=content, result=res))
    assert card.result == {"draft_id": "dr1", "map_id": "m1"} == ActionOut.of(row(kind="goal_new", content=content, result=res)).result
    assert card_of(row(kind="goal_new", status="proposed", content=content)).result == {}


@pytest.mark.parametrize("status", ["proposed", "executed"])
def test_goal_card_and_live_event_preserve_the_topic_separately_from_the_action_heading(status):
    content = {"goal": "会梯度下降，能手算一次参数更新", "title": "梯度下降",
               "blocks": []}
    record = replace(row(kind="goal_new", status=status, content=content),
                     summary="「梯度下降」分成5块")
    card = card_of(record)
    event = ActionEvent(**card.model_dump(exclude={"detail"}))
    assert card.title == event.title == "「梯度下降」分成5块"
    assert card.goal_title == event.goal_title == "梯度下降"
    assert event.model_dump()["goal_title"] == "梯度下降"


def test_goal_topic_is_absent_for_other_actions_or_a_goal_without_a_topic():
    assert card_of(row(kind="page_merge", content={"title": "方差"})).goal_title is None
    assert card_of(row(kind="goal_new", content={"goal": "我要学梯度下降"})).goal_title is None
