"""Usage API (plan rev 3.2 §9.4): spend today and this month by purpose group; spending
limits and explicit budget confirmation. GET /usage, GET /usage/settings, PATCH /usage/settings."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from evowiki.llm.usage import PurposeGroup
from evowiki.service.deps import Services, check_user, current_user, get_services
from evowiki.store.usage import (
    UNSET,
    UsageSettings,
    day_start,
    get_settings,
    month_start,
    spend_by_group,
    update_settings,
    usage_tz,
)

router = APIRouter()
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]
Limit = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]  # no bool/str coercion


class GroupSpendOut(BaseModel):
    cost_usd: float
    input_tokens: int
    output_tokens: int
    calls: int
    replayed_calls: int
    unpriced_calls: int
    cache_read_tokens: int     # prompt-cache tokens read; input_tokens excludes them
    cache_write_tokens: int    # prompt-cache tokens written; input_tokens excludes them


class PeriodOut(BaseModel):
    since: AwareDatetime
    total_usd: float
    by_group: dict[PurposeGroup, GroupSpendOut]


class UsageOut(BaseModel):
    today: PeriodOut
    month: PeriodOut
    daily_limit_usd: float | None
    monthly_limit_usd: float | None


class SettingsOut(BaseModel):
    daily_limit_usd: float | None
    monthly_limit_usd: float | None
    budget_confirmed: bool


class SettingsIn(BaseModel):
    """Partial update: an absent field is kept; a null limit removes that limit."""

    model_config = ConfigDict(extra="forbid")

    daily_limit_usd: Limit | None = None
    monthly_limit_usd: Limit | None = None
    confirm_budget: bool = Field(default=False, strict=True)


def _settings_out(s: UsageSettings) -> SettingsOut:
    return SettingsOut(daily_limit_usd=s.daily_limit_usd, monthly_limit_usd=s.monthly_limit_usd,
                       budget_confirmed=s.budget_confirmed)


@router.get("/usage", response_model=UsageOut)
def usage(svc: Svc, uid: User) -> UsageOut:
    check_user(svc, uid)
    now, tz = svc.clock(), usage_tz()
    periods = {}
    with svc.pool.connection() as conn:
        for name, since in (("today", day_start(now, tz)), ("month", month_start(now, tz))):
            groups = spend_by_group(conn, uid, since)
            periods[name] = PeriodOut(
                since=since, total_usd=sum(g.cost_usd for g in groups.values()),
                by_group={k: GroupSpendOut(**vars(g)) for k, g in groups.items()})
        s = get_settings(conn, uid)
    return UsageOut(today=periods["today"], month=periods["month"],
                    daily_limit_usd=s.daily_limit_usd, monthly_limit_usd=s.monthly_limit_usd)


@router.get("/usage/settings", response_model=SettingsOut)
def read_settings(svc: Svc, uid: User) -> SettingsOut:
    check_user(svc, uid)
    with svc.pool.connection() as conn:
        return _settings_out(get_settings(conn, uid))


@router.patch("/usage/settings", response_model=SettingsOut)
def patch_settings(body: SettingsIn, svc: Svc, uid: User) -> SettingsOut:
    check_user(svc, uid)
    given = body.model_fields_set
    with svc.pool.connection() as conn, conn.transaction():
        s = update_settings(
            conn, uid, now=svc.clock(),
            daily_limit_usd=body.daily_limit_usd if "daily_limit_usd" in given else UNSET,
            monthly_limit_usd=body.monthly_limit_usd if "monthly_limit_usd" in given else UNSET)
        if body.confirm_budget:
            conn.execute("update usage_settings set budget_confirmed=true where user_id=%s", (uid,))
            s = get_settings(conn, uid)
    return _settings_out(s)
