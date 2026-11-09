from datetime import UTC, datetime

from evowiki.llm.budget import PageBudgetExceeded
from evowiki.llm.usage import Account, BudgetExceededError
from evowiki.store.usage import reset_text

ACC = Account(user_id="u", group="assistant")
EVENING = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)


def test_reset_is_the_next_day_or_month_in_this_machines_time():
    assert reset_text("day", EVENING, EVENING.astimezone().tzinfo) == "明天 0 点恢复"
    assert reset_text("month", EVENING, EVENING.astimezone().tzinfo) == "12 月 1 日 0 点恢复"


def test_each_limit_says_which_it_is_how_much_and_the_way_out():
    day = BudgetExceededError(ACC, "day", 1.0, "明天 0 点恢复")
    assert day.line("这次没有回答") == "今天的花费到了上限 $1.00，这次没有回答。明天 0 点恢复，也可以在设置里调高。"
    month = BudgetExceededError(ACC, "month", 20.0, "12 月 1 日 0 点恢复")
    assert month.line("这次没有回答") == (
        "这个月的花费到了上限 $20.00，这次没有回答。12 月 1 日 0 点恢复，也可以在设置里调高。")
    page = PageBudgetExceeded(ACC, 0.5)
    assert page.line("这次没有写完") == "这一页的花费到了上限 $0.50，这次没有写完。可以在设置里调高。"
