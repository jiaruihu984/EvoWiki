"""Persistent usage meter (plan rev 3.2 §9.4): per-user spend by purpose group, spending
limits and fast/good tiers. PgUsageMeter is the UsageMeter and TierSource Gateway uses."""

import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from evowiki.llm.usage import Account, BudgetExceededError, PurposeGroup, Usage
from evowiki.llm.usage_control import DEFAULT_TIERS, GROUPS, PICK_KEY, TIERS, Tier
from evowiki.store.db import fetch_all, fetch_one
from evowiki.store.users import UnknownUser

TZ_ENV = "EVOWIKI_USAGE_TZ"
UNSET: Any = object()


def usage_tz(env: Mapping[str, str] = os.environ) -> tzinfo:
    """Where "today" and "this month" start: EVOWIKI_USAGE_TZ (IANA name), else the server's
    local zone."""
    name = env.get(TZ_ENV)
    if name:
        return ZoneInfo(name)
    local = datetime.now().astimezone().tzinfo
    assert local is not None
    return local


def day_start(now: datetime, tz: tzinfo) -> datetime:
    return now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: datetime, tz: tzinfo) -> datetime:
    return day_start(now, tz).replace(day=1)


def reset_text(period: str, now: datetime, tz: tzinfo) -> str:
    """When a day / month limit lifts, in the local time of this machine (the learner's own, also
    when EVOWIKI_USAGE_TZ draws the day somewhere else): 「明天 0 点恢复」."""
    today = day_start(now, tz)
    start = today + timedelta(days=1) if period == "day" else (
        today.replace(day=1) + timedelta(days=32)).replace(day=1)
    at, here = start.astimezone(), now.astimezone()
    clock = f"{at.hour} 点" if at.minute == 0 else f"{at.hour}:{at.minute:02d}"
    day = {0: "今天", 1: "明天"}.get((at.date() - here.date()).days, f"{at.month} 月 {at.day} 日")
    return f"{day} {clock}恢复"


@dataclass(frozen=True)
class UsageSettings:
    daily_limit_usd: float | None
    monthly_limit_usd: float | None
    tiers: Mapping[PurposeGroup, Tier]  # effective: defaults overlaid by the user's choices
    budget_confirmed: bool = True


class BudgetNotConfirmed(PermissionError):
    error_code = "budget_confirmation_required"

    def __init__(self):
        super().__init__("请先确认模型费用和使用预算，再开始学习")


@dataclass(frozen=True)
class GroupSpend:
    cost_usd: float
    input_tokens: int
    output_tokens: int
    calls: int           # paid provider responses (priced or not)
    replayed_calls: int  # cache replays: recorded, never billed
    unpriced_calls: int  # paid responses whose model has no price (cost unknown)
    cache_read_tokens: int = 0    # prompt-cache tokens read (not in input_tokens)
    cache_write_tokens: int = 0   # prompt-cache tokens written (not in input_tokens)


_ZERO = GroupSpend(0.0, 0, 0, 0, 0, 0)


def insert_usage(conn: psycopg.Connection, account: Account, purpose: str, usage: Usage, *,
                 replayed: bool, ts: datetime) -> None:
    conn.execute(
        "insert into usage_record (user_id, grp, purpose, provider, model, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, replayed, ts, call_id) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (account.user_id, account.group, purpose, usage.provider, usage.model,
         usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
         usage.cache_write_tokens, usage.cost_usd, replayed, ts, usage.call_id))


def spend_by_group(conn: psycopg.Connection, user_id: str,
                   since: datetime) -> dict[PurposeGroup, GroupSpend]:
    rows = fetch_all(conn, """
        select grp,
               coalesce(sum(cost_usd) filter (where not replayed), 0) as cost_usd,
               coalesce(sum(input_tokens) filter (where not replayed), 0) as input_tokens,
               coalesce(sum(output_tokens) filter (where not replayed), 0) as output_tokens,
               coalesce(sum(cache_read_tokens) filter (where not replayed), 0)
                 as cache_read_tokens,
               coalesce(sum(cache_write_tokens) filter (where not replayed), 0)
                 as cache_write_tokens,
               count(*) filter (where not replayed) as calls,
               count(*) filter (where replayed) as replayed_calls,
               count(*) filter (where not replayed and cost_usd is null) as unpriced_calls
          from usage_record where user_id = %s and ts >= %s group by grp""", (user_id, since))
    out: dict[PurposeGroup, GroupSpend] = dict.fromkeys(GROUPS, _ZERO)
    for r in rows:
        out[r["grp"]] = GroupSpend(float(r["cost_usd"]), int(r["input_tokens"]),
                                   int(r["output_tokens"]), r["calls"], r["replayed_calls"],
                                   r["unpriced_calls"], int(r["cache_read_tokens"]),
                                   int(r["cache_write_tokens"]))
    return out


def total_cost(conn: psycopg.Connection, user_id: str, since: datetime) -> float:
    r = fetch_one(conn, "select coalesce(sum(cost_usd), 0) as c from usage_record "
                        "where user_id = %s and ts >= %s and not replayed", (user_id, since))
    assert r is not None
    return float(r["c"])


def _settings(row: dict | None) -> UsageSettings:
    if row is None:
        return UsageSettings(None, None, DEFAULT_TIERS)
    return UsageSettings(row["daily_limit_usd"], row["monthly_limit_usd"],
                         MappingProxyType({**DEFAULT_TIERS, **row["tiers"]}), row["budget_confirmed"])


def get_settings(conn: psycopg.Connection, user_id: str) -> UsageSettings:
    return _settings(fetch_one(conn, "select * from usage_settings where user_id = %s",
                               (user_id,)))


def _limit(name: str, v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v < 0:
        raise ValueError(f"{name} limit must be a finite non-negative number or None, got {v!r}")
    return float(v)


def update_settings(conn: psycopg.Connection, user_id: str, *, now: datetime,
                    daily_limit_usd: Any = UNSET, monthly_limit_usd: Any = UNSET,
                    tiers: Mapping[str, str] | None = None) -> UsageSettings:
    """Partial update: UNSET keeps a limit, None clears it; tiers are merged per group."""
    for group, tier in (tiers or {}).items():
        if group not in GROUPS:
            raise ValueError(f"unknown purpose group {group!r}; expected one of {GROUPS}")
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r} for {group}; expected one of {TIERS}")
    with conn.transaction():
        conn.execute("insert into usage_settings (user_id, updated_at) values (%s, %s) "
                     "on conflict (user_id) do nothing", (user_id, now))
        row = fetch_one(conn, "select * from usage_settings where user_id = %s for update",
                        (user_id,))
        assert row is not None
        daily = row["daily_limit_usd"] if daily_limit_usd is UNSET else _limit(
            "daily", daily_limit_usd)
        monthly = row["monthly_limit_usd"] if monthly_limit_usd is UNSET else _limit(
            "monthly", monthly_limit_usd)
        merged = {**row["tiers"], **(tiers or {})}
        conn.execute("update usage_settings set daily_limit_usd = %s, monthly_limit_usd = %s, "
                     "tiers = %s, updated_at = %s where user_id = %s",
                     (daily, monthly, Jsonb(merged), now, user_id))
    return get_settings(conn, user_id)


class PgUsageMeter:
    """UsageMeter + TierSource on Postgres. Limits apply per user across all groups and are
    checked before a provider call; a call already started always finishes and is recorded,
    so concurrent calls can overshoot a limit by at most the calls in flight."""

    def __init__(self, pool: ConnectionPool, *, clock: Callable[[], datetime], tz: tzinfo):
        self._pool = pool
        self._clock = clock
        self._tz = tz

    def check(self, account: Account) -> None:
        now = self._clock()
        with self._pool.connection() as conn:
            if fetch_one(conn, "select 1 from app_user where user_id=%s", (account.user_id,)) is None:
                raise UnknownUser(account.user_id)
            s = get_settings(conn, account.user_id)
            if not s.budget_confirmed:
                raise BudgetNotConfirmed()
            for period, limit, since in (("day", s.daily_limit_usd, day_start(now, self._tz)),
                                         ("month", s.monthly_limit_usd,
                                          month_start(now, self._tz))):
                if limit is not None and total_cost(conn, account.user_id, since) >= limit:
                    raise BudgetExceededError(account, period, limit,
                                              reset_text(period, now, self._tz))

    def record(self, account: Account, purpose: str, usage: Usage, *, replayed: bool) -> None:
        with self._pool.connection() as conn:
            insert_usage(conn, account, purpose, usage, replayed=replayed, ts=self._clock())

    def tier_for(self, account: Account) -> Tier:
        with self._pool.connection() as conn:
            return get_settings(conn, account.user_id).tiers[account.group]

    def chosen_tier(self, account: Account) -> Tier | None:
        """Only the user's explicit choice for the group (defaults are not a choice), so the
        per-purpose defaults can sit between it and the group default."""
        with self._pool.connection() as conn:
            return chosen_tiers(conn, account.user_id).get(account.group)

    def chosen_model(self, account: Account) -> str | None:
        """The user's model (the composer picker; one for all their calls), None when never chosen."""
        with self._pool.connection() as conn:
            return chosen_models(conn, account.user_id).get(PICK_KEY)

    def chosen_reasoning(self, account: Account) -> str | None:
        """The explicit reasoning selection stored atomically with the concrete model."""
        with self._pool.connection() as conn:
            return chosen_models(conn, account.user_id).get("reasoning")


def chosen_tiers(conn: psycopg.Connection, user_id: str) -> dict[str, Tier]:
    """The user's explicit tier choices by purpose group (absent group = never chosen)."""
    row = fetch_one(conn, "select tiers from usage_settings where user_id = %s", (user_id,))
    return {} if row is None else dict(row["tiers"])


def chosen_models(conn: psycopg.Connection, user_id: str) -> dict[str, str]:
    """The user's model choices by purpose group (absent group = the server's default model)."""
    row = fetch_one(conn, "select models from usage_settings where user_id = %s", (user_id,))
    return {} if row is None else dict(row["models"])


def set_model_choice(conn: psycopg.Connection, user_id: str, model: str,
                     reasoning: str, *, now: datetime) -> None:
    """Persist the validated concrete model and reasoning in one JSON update."""
    if not model or not reasoning:
        raise ValueError("model and reasoning must be non-empty")
    with conn.transaction():
        conn.execute("insert into usage_settings (user_id, updated_at) values (%s, %s) "
                     "on conflict (user_id) do nothing", (user_id, now))
        conn.execute("update usage_settings set models = models || %s, updated_at = %s "
                     "where user_id = %s", (Jsonb({PICK_KEY: model, "reasoning": reasoning}), now, user_id))


def latest_attempt(conn: psycopg.Connection, providers: Sequence[str],
                   model: str | None = None, *, since: datetime | None = None) -> dict | None:
    """Latest attempt for exact providers/model within the active configuration epoch."""
    return fetch_one(conn, "select status, error_type, error_message from llm_attempt "
                           "where provider = any(%s) and (%s::text is null or model = %s) "
                           "and (%s::timestamptz is null or ts >= %s) "
                           "order by ts desc, id desc limit 1", (list(providers), model, model, since, since))
