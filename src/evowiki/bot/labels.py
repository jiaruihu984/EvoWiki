"""Plain-Chinese words the Bot shows (gate details, preference rows). No
database, no clock."""

from datetime import datetime
from zoneinfo import ZoneInfo

KIND_ZH: dict[str, str] = {
    "review_due": "复习提醒", "wrong_answers": "补讲", "goal_idle": "继续目标",
    "strategy_negative": "讲解调整", "source_added": "资料更新", "card_stale": "复习卡更新", "open_question": "没懂的问题",
    "chat": "页面小建议", "idle_write": "空闲时写页面", "goal_review": "每周回顾",
}
PAUSED_DETAIL = "连续失败，已暂停，明天再试"


def kind_zh(kind: str) -> str:
    return KIND_ZH.get(kind, kind)


def md(t: datetime, tz: str) -> str:
    """`10月14日` in the user's time zone."""
    t = t.astimezone(ZoneInfo(tz))
    return f"{t.month}月{t.day}日"


def md_hm(t: datetime, tz: str) -> str:
    """`10月14日 09:05` in the user's time zone."""
    return f"{md(t, tz)} {t.astimezone(ZoneInfo(tz)):%H:%M}"
