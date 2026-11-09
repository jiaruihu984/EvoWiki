"""POST /users (register: cold-start survey → background) and GET /me."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from evowiki.schema import Lang
from evowiki.service.deps import Services, current_user, get_services
from evowiki.store.bot import mark_seen
from evowiki.store.locks import user_writer
from evowiki.store.users import UnknownUser, UserRow, create_user, get_user
from evowiki.store.wiki import append_wiki_events
from evowiki.wikiapp.events import LanguageSet
from evowiki.wikiapp.repo import PgRepo

router = APIRouter()
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]

class ColdStartSurvey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    math: Literal["none", "university"]
    ml: Literal["none", "beginner", "experienced"]
    dl: Literal["none", "experienced"]


def background_for(s: ColdStartSurvey) -> str:
    """Map survey answers to a θ_infer.prior_by_background key (most advanced answer wins)."""
    if s.dl == "experienced":
        return "dl_experienced"
    if s.ml == "experienced":
        return "ml_experienced"
    if s.ml == "beginner":
        return "ml_beginner"
    if s.math == "university":
        return "university_math"
    return "none"


class CreateUserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    survey: ColdStartSurvey
    condition: Literal["S0", "S1", "S2", "S3"] = "S2"
    lang: Lang = Lang.EN


class UserOut(BaseModel):
    user_id: str
    background: str
    condition: str
    lang: Lang


@router.post("/users", status_code=201, response_model=UserOut)
def create(body: CreateUserIn, svc: Svc) -> UserOut:
    background = background_for(body.survey)
    if background not in svc.ctx.theta_infer.prior_by_background:
        raise LookupError(f"no prior for background {background}")
    row = UserRow(user_id=body.user_id, background=background, condition=body.condition,
                  lang=body.lang, created_at=svc.clock())
    with user_writer(svc.pool, body.user_id) as tx:
        conn = tx.conn
        create_user(conn, row)
        from evowiki.forge.switch import set_enabled
        set_enabled(conn, body.user_id, "tool", False, svc.clock())
        set_enabled(conn, body.user_id, "patch", False, svc.clock())
        # Initialize the authoritative language in the same account transaction.
        event = LanguageSet(language=body.lang)
        append_wiki_events(tx, [
            (svc.new_id("wev"), svc.clock(), event.type, event.model_dump(mode="json")),
        ])
        conn.execute("insert into usage_settings(user_id,daily_limit_usd,monthly_limit_usd,"
                     "budget_confirmed,updated_at) values (%s,5,30,false,%s) "
                     "on conflict(user_id) do update set daily_limit_usd=5,monthly_limit_usd=30,"
                     "budget_confirmed=false,updated_at=excluded.updated_at",
                     (body.user_id, svc.clock()))
    return UserOut(user_id=row.user_id, background=background, condition=row.condition,
                   lang=row.lang)


@router.post("/me/seen", status_code=204)
def seen(svc: Svc, uid: User) -> None:
    """His Obsidian window is in front of him (`bot.settings.bot_serves`)."""
    with svc.pool.connection() as conn:
        if not mark_seen(conn, uid, svc.clock()):
            raise UnknownUser(f"unknown user {uid}")


@router.get("/me", response_model=UserOut)
def me(svc: Svc, uid: User) -> UserOut:
    with svc.pool.connection() as conn:
        row = get_user(conn, uid)
    if row is None:
        raise UnknownUser(uid)
    language = PgRepo(svc.pool).load(uid).state.language
    return UserOut(user_id=row.user_id, background=row.background, condition=row.condition,
                   lang=language)
