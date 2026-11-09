import pytest

from evowiki.agent.compact import (
    CompactPolicy,
    choose_cut,
    overflow_drop,
    tool_pairs_intact,
)
from evowiki.llm.chat import ChatMessage, ToolCall

P = CompactPolicy(history_budget=100, keep_turns=3, keep_budget=60, hard_cap=150,
                  summary_max_tokens=40)


def test_policy_defaults_match_the_spec_and_bad_policies_are_refused():
    d = CompactPolicy()
    assert (d.history_budget, d.keep_turns, d.hard_cap) == (20_000, 8, 30_000)
    assert d.keep_budget == d.history_budget - d.summary_max_tokens == 16_000
    for bad in ({"keep_turns": 0}, {"keep_budget": 30_000}, {"hard_cap": 21_000},
                {"summary_max_tokens": 0}):
        with pytest.raises(ValueError):
            CompactPolicy(**bad)


def test_choose_cut_none_when_within_budget():
    assert choose_cut([10, 10, 10], 50, P) is None


def test_choose_cut_keeps_the_last_keep_turns():
    assert choose_cut([20] * 8, 0, P) == 5             # 160 > 100; keep 3 (60 <= 60)


def test_default_policy_keeps_eight_turns_of_up_to_2000_tokens_verbatim():
    n = 12
    assert choose_cut([2000] * n, 0, CompactPolicy()) == n - 8


def test_choose_cut_keeps_fewer_turns_when_they_exceed_keep_budget():
    assert choose_cut([10, 10, 10, 30, 30, 30], 0, P) == 4   # last 3 = 90 > 60 -> keep 2
    assert choose_cut([10, 90, 95], 0, P) == 3                 # last turn alone > 60 -> fold all


def test_choose_cut_none_when_nothing_would_be_folded():
    assert choose_cut([20, 20], 70, P) is None          # over budget but both turns are kept


def test_overflow_drop_only_over_hard_cap_and_down_to_budget():
    assert overflow_drop([40, 40, 40], 20, P) == 0      # 140 <= 150
    assert overflow_drop([40, 40, 40, 40], 20, P) == 2  # 180 > 150 -> 20 + 80 <= 100
    assert overflow_drop([200], 0, P) == 1


def _a(*ids):
    return ChatMessage(role="assistant", tool_calls=tuple(ToolCall(id=i, name="t", args={})
                                                          for i in ids))


def _t(i):
    return ChatMessage(role="tool", tool_call_id=i, text="r")


def test_tool_pairs_intact():
    u = ChatMessage(role="user", text="q")
    assert tool_pairs_intact([u, _a("1", "2"), _t("1"), _t("2"), ChatMessage(role="assistant",
                                                                            text="a")])
    assert not tool_pairs_intact([u, _a("1", "2"), _t("1")])                 # a call unanswered
    assert not tool_pairs_intact([u, _t("1")])                               # result w/o call
    assert not tool_pairs_intact([u, _a("1"), u, _t("1")])                   # split by a user msg
