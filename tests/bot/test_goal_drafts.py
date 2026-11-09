"""A goal draft whose question is settled stops waiting (the failure: an 11-03 draft for a goal that
already existed stayed on top of the 知识图谱 for good); anything else waits for his 取消."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from evowiki.bot.goal_drafts import GOAL_EXISTS, REPLACED, settled
from evowiki.store.bot import ActionRow

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


def row(i: str, status="proposed", **content) -> ActionRow:
    return ActionRow(action_id=i, user_id="u1", kind="goal_new", scope="structure", level="ask", status=status,
                     source="assistant", summary="s", content=content, content_hash="h", idempotency_key=i,
                     created_at=NOW)


def state(*goals: str):
    maps = {g: SimpleNamespace(goal=g, title=g, map_id=g) for g in goals}
    return SimpleNamespace(maps=maps, goal_settings={}, reading_of=lambda _m: None)


def test_a_draft_for_a_goal_that_exists_stops_waiting_by_its_goal_or_its_title():
    rows = [row("a", goal="python", title="复杂网络中的渗流"), row("b", goal="我要学渗流", title="渗流"),
            row("c", goal="吉他")]
    assert [(r.action_id, w) for r, w in settled(state("Python"), rows)] == [("a", GOAL_EXISTS)]
    assert [(r.action_id, w) for r, w in settled(state("渗流"), rows)] == [("b", GOAL_EXISTS)]


def test_a_newer_draft_for_the_same_goal_replaces_the_older_one_in_any_conversation():
    rows = [row("new", goal="吉他"), row("old", goal="我要学吉他"), row("other", goal="钢琴")]
    assert [(r.action_id, w) for r, w in settled(state(), rows)] == [("old", REPLACED)]


def test_what_nobody_settled_keeps_waiting_however_old():
    long_ago = replace(row("a", goal="钢琴"), created_at=datetime(2020, 1, 1, tzinfo=UTC))
    assert settled(state("Python"), [long_ago]) == []


def test_a_draft_that_asks_for_something_new_on_purpose_is_left_alone():
    rows = [row("again", goal="python", again=True), row("mat", goal="python", source_ids=["s1"]),
            row("done", status="executed", goal="python")]
    assert settled(state("python"), rows) == []
