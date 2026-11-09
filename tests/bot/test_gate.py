from datetime import UTC, datetime, timedelta

from evowiki.bot.gate import SKIP_REASONS, GateContext, GateDecision, allow_check, gate
from evowiki.bot.rules import Intent
from evowiki.bot.settings import DEFAULT_SETTINGS
from evowiki.bot.suppression import Counters

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)


def intent(route="direct", placement="today", kind="review_due"):
    return Intent(kind=kind, rule_id=kind, rule_version=1, source_key="s", dedup_key="d",
                  route=route, evidence=(), payload={}, priority=1, reason="r",
                  placement=placement, page_id="p" if placement == "page" else None)


def ctx(**kw):
    base = {"settings": DEFAULT_SETTINGS, "now": NOW, "placed_today": 0, "placed_on_page": 0,
            "last_suggested_at": None, "counters": Counters()}
    return GateContext(**{**base, **kw})


def sett(**kw):
    return DEFAULT_SETTINGS.model_copy(update=kw)


def test_skip_reasons_exact():
    assert SKIP_REASONS == ("paused", "kind_off", "cap_reached", "cooldown", "suppressed",
                            "empty", "busy")


def test_pass():
    assert gate(intent(), ctx()) == GateDecision("pass", None)


def test_paused():
    assert gate(intent(), ctx(settings=sett(paused="always"))) == GateDecision("skip", "paused")


def test_paused_until_expires():
    s = sett(paused="until", paused_until=NOW + timedelta(hours=1))
    assert gate(intent(), ctx(settings=s)).reason == "paused"
    assert gate(intent(), ctx(settings=s, now=NOW + timedelta(hours=2))).outcome == "pass"


def test_kind_off():
    s = sett(kinds={"review_due": False})
    assert gate(intent(), ctx(settings=s)) == GateDecision("skip", "kind_off")


def test_cap_today_and_page():
    assert gate(intent(), ctx(placed_today=5)).reason == "cap_reached"
    assert gate(intent(), ctx(placed_today=4)).outcome == "pass"
    assert gate(intent(placement="page"), ctx(placed_on_page=2)).reason == "cap_reached"
    assert gate(intent(placement="page"), ctx(placed_today=9, placed_on_page=1)).outcome == "pass"
    assert gate(intent(), ctx(placed_on_page=9)).outcome == "pass"


def test_cooldown():
    assert gate(intent(), ctx(last_suggested_at=NOW - timedelta(hours=19))).reason == "cooldown"
    assert gate(intent(), ctx(last_suggested_at=NOW - timedelta(hours=20))).outcome == "pass"


def test_suppressed():
    c = Counters(not_interested=1, last_not_interested_at=NOW - timedelta(hours=30))
    assert gate(intent(), ctx(counters=c)).reason == "suppressed"      # until +40h
    c = Counters(not_interested=1, last_not_interested_at=NOW - timedelta(hours=41))
    assert gate(intent(), ctx(counters=c)).outcome == "pass"


def test_order():
    full = {"settings": sett(paused="always", kinds={"review_due": False}), "placed_today": 9,
            "last_suggested_at": NOW,
            "counters": Counters(not_interested=1, last_not_interested_at=NOW)}
    assert gate(intent(), ctx(**full)).reason == "paused"
    full["settings"] = sett(kinds={"review_due": False})
    assert gate(intent(), ctx(**full)).reason == "kind_off"
    full["settings"] = DEFAULT_SETTINGS
    assert gate(intent(), ctx(**full)).reason == "cap_reached"
    full["placed_today"] = 0
    assert gate(intent(), ctx(**full)).reason == "cooldown"
    full["last_suggested_at"] = None
    assert gate(intent(), ctx(**full)).reason == "suppressed"
    full["counters"] = Counters()
    assert gate(intent(), ctx(**full)).outcome == "pass"


def test_rsi_route_ignores_caps_cooldown_suppression():
    c = ctx(placed_today=99, placed_on_page=99, last_suggested_at=NOW,
            counters=Counters(not_interested=3, last_not_interested_at=NOW))
    assert gate(intent(route="rsi", kind="strategy_negative"), c).outcome == "pass"


def test_rsi_route_still_paused_and_kind_off():
    assert gate(intent(route="rsi"), ctx(settings=sett(paused="always"))).reason == "paused"
    s = sett(kinds={"review_due": False})
    assert gate(intent(route="rsi"), ctx(settings=s)).reason == "kind_off"


def test_allow_check():
    four = [NOW - timedelta(seconds=s) for s in (1, 10, 20, 30)]
    assert allow_check(four, NOW) is True
    assert allow_check([*four, NOW - timedelta(seconds=59)], NOW) is False
    assert allow_check([*four, NOW - timedelta(seconds=60)], NOW) is True   # window edge: outside


# --- details (plain Chinese, the user's time zone) --------------------------------------------

CHAT_ON = sett(kinds={**DEFAULT_SETTINGS.kinds, "chat": True})     # the kind is off by default


def test_pass_detail_names_the_count_and_cooldown_subject():
    d = gate(intent(), ctx(placed_today=2))
    assert d.detail == "今日第 3 / 5 项"
    d = gate(intent(placement="page", kind="chat"), ctx(settings=CHAT_ON, placed_on_page=1))
    assert d.detail == "这一页今天第 2 / 2 条"


def test_skip_details():
    assert gate(intent(), ctx(settings=sett(paused="always"))).detail == "Bot 已暂停"
    off = sett(kinds={**DEFAULT_SETTINGS.kinds, "review_due": False})
    assert gate(intent(), ctx(settings=off)).detail == "「复习提醒」已关闭"
    assert gate(intent(), ctx(placed_today=5)).detail == "今天的待办已达 5 项上限"
    assert (gate(intent(placement="page", kind="chat"), ctx(settings=CHAT_ON, placed_on_page=2)).detail
            == "这一页今天的提示已达 2 条上限")


def test_cooldown_and_suppressed_details_use_the_user_time_zone():
    last = NOW - timedelta(hours=1)                    # 08:00 UTC = 16:00 Shanghai
    d = gate(intent(), ctx(last_suggested_at=last))
    assert (d.outcome, d.reason) == ("skip", "cooldown")
    assert d.detail == "10月15日 11:00 之后再提醒"       # last + 20 h = 03:00 UTC next day
    ny = sett(timezone="America/New_York")
    assert gate(intent(), ctx(settings=ny, last_suggested_at=last)).detail == (
        "10月14日 23:00 之后再提醒")
    c = Counters(not_interested=1, last_not_interested_at=NOW - timedelta(hours=2))
    d = gate(intent(), ctx(counters=c))
    assert (d.reason, d.detail) == ("suppressed", "你点过「不感兴趣」，10月16日 前不再安排")


def test_one_shot_source_added_defers_while_paused_or_capped():
    src = intent(kind="source_added")
    assert gate(src, ctx(settings=sett(paused="always"))) == GateDecision("defer", "paused")
    assert gate(src, ctx(placed_today=5)) == GateDecision("defer", "cap_reached")
    s = sett(kinds={**DEFAULT_SETTINGS.kinds, "source_added": False})
    assert gate(src, ctx(settings=s)) == GateDecision("skip", "kind_off")   # turned off: gone
