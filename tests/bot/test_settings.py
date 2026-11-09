from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from evowiki.bot.settings import DEFAULT_SETTINGS, OFF_BY_DEFAULT, BotSettings, effective_paused


def test_defaults_match_spec():
    s = DEFAULT_SETTINGS
    assert s.timezone == "Asia/Shanghai"
    assert (s.daily_hour, s.daily_minute) == (8, 0)
    assert (s.today_cap, s.page_cap) == (5, 2)
    assert (s.wrong_count, s.wrong_days, s.idle_days) == (3, 7, 3)
    assert s.kinds["source_added"] is True
    assert all(v for k, v in s.kinds.items() if k not in OFF_BY_DEFAULT)
    assert all(v for k, v in BotSettings(kinds={}).kinds.items() if k not in OFF_BY_DEFAULT)
    assert BotSettings(kinds={"source_added": False}).kinds["source_added"] is False
    assert s.levels == {
        "user_notes": "never", "page_body": "auto_undo", "new_page": "auto_undo",
        "delete_merge": "ask", "structure": "auto_undo", "teaching_params": "ask",
        "rsi": "ask", "suggestion": "auto", "preference": "auto_undo",
        "path": "ask"}
    assert set(s.kinds) >= {"chat", "idle_write"} and not s.kinds["idle_write"]
    assert s.kinds["chat"] is False       # removed from the UI 2026-10-21
    assert "prefetch" not in s.kinds
    # B5 (user 2026-10-15): weekly review on Monday at the daily check; path edits auto + undo
    assert s.kinds["goal_review"] and s.review_weekday == 0
    assert BotSettings(levels={"path": "ask"}).levels["path"] == "ask"
    with pytest.raises(ValidationError):
        BotSettings(levels={"path": "auto"})
    with pytest.raises(ValidationError):
        BotSettings(review_weekday=7)


def test_bad_timezone_rejected():
    with pytest.raises(ValidationError):
        BotSettings(timezone="Mars/Olympus")


def test_pause_until_and_forever():
    now = datetime(2026, 10, 14, 1, 0, tzinfo=UTC)
    assert not effective_paused(DEFAULT_SETTINGS, now)
    until = DEFAULT_SETTINGS.model_copy(update={"paused": "until",
                                                "paused_until": datetime(2026, 10, 14, 16, 0, tzinfo=UTC)})
    assert effective_paused(until, now)
    assert not effective_paused(until, datetime(2026, 10, 14, 16, 0, tzinfo=UTC))
    assert effective_paused(DEFAULT_SETTINGS.model_copy(update={"paused": "always"}), now)


def test_levels_are_configurable_where_the_spec_allows():
    s = BotSettings(levels={"page_body": "ask", "new_page": "auto_undo", "delete_merge": "auto_undo",
                            "structure": "ask"})
    assert s.levels["page_body"] == "ask" and s.levels["delete_merge"] == "auto_undo"
    assert s.levels["structure"] == "ask" and s.levels["rsi"] == "ask"    # filled in


@pytest.mark.parametrize("scope,level", [("user_notes", "auto_undo"), ("suggestion", "ask"),
                                         ("page_body", "auto"), ("page_body", "never")])
def test_fixed_scopes_and_unknown_levels_are_rejected(scope, level):
    with pytest.raises(ValidationError):
        BotSettings(levels={scope: level})


@pytest.mark.parametrize("scope", ["teaching_params", "rsi", "new_page"])
def test_ask_for_rsi_scopes_and_new_page_is_choosable(scope):
    # RSI changes that passed their Gate wait for the user (teach.approval); new_page: no
    # automated creator yet, so ask holds nothing
    assert BotSettings(levels={scope: "ask"}).levels[scope] == "ask"
    assert BotSettings(levels={scope: "auto_undo"}).levels[scope] == "auto_undo"
    with pytest.raises(ValidationError, match="的审批方式只能选「先问我」、「直接做」"):
        BotSettings(levels={scope: "auto"})
