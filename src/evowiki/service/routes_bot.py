"""/wiki/bot/*: Bot suggestions, reactions, badge, settings and pause."""

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from evowiki.bot import suggestions as sugg
from evowiki.bot import why as why_mod
from evowiki.bot.actions import ActionLedger, ActionRefused
from evowiki.bot.bot_schemas import (
    ActionOut,
    ActivityOut,
    ApproveIn,
    BadgeOut,
    DraftIn,
    PauseIn,
    ReactIn,
    ReviewReactIn,
    ReviewReminderOut,
    SuggestionOut,
    WhyOut,
)
from evowiki.bot.diffs import Names, diff_for, needs_state
from evowiki.bot.facts import UnknownPage
from evowiki.bot.goal_drafts import settle
from evowiki.bot.goals import GoalOut, list_goals, set_parked
from evowiki.bot.pipeline import page_chips
from evowiki.bot.presentation import trigger_text
from evowiki.bot.revise_schemas import revise_out, revise_source_ids
from evowiki.bot.schedule import next_daily_slot
from evowiki.bot.settings import BotSettings
from evowiki.i18n import current_language, tr
from evowiki.service.deps import (
    Services,
    asset_title,
    check_user,
    current_user,
    get_services,
    restart_revise,
)
from evowiki.service.rsi_runs import rsi_tick
from evowiki.sources import store as source_store
from evowiki.store import bot as store
from evowiki.store.bot import ActionRow
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.revise import KIND as PAGE_REVISE
from evowiki.wikiapp.source_revise import RevisePending

router = APIRouter(prefix="/wiki/bot")
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]


@router.get("/suggestions", response_model=list[SuggestionOut])
def list_suggestions(svc: Svc, uid: User, placement: Literal["today", "page"] = "today",
                     page_id: Annotated[str | None, Query()] = None):
    check_user(svc, uid)
    if placement == "page" and not page_id:
        raise HTTPException(422, "placement=page needs page_id")
    now = svc.clock()
    if placement == "page":
        try:       # the page's chat chips, made before listing (deterministic, once per day)
            page_chips(svc.pool, uid, page_id, now=now, new_id=svc.new_id)
        except UnknownPage as e:
            raise HTTPException(404, str(e)) from e
    rows = sugg.list_suggestions(svc.pool, uid, placement=placement, page_id=page_id, now=now)
    return _suggestions(svc, uid, rows)


def _suggestions(svc: Services, uid: str, rows: list) -> list[SuggestionOut]:
    prefs = sugg.preference_ids(svc.pool, uid, [r.suggestion_id for r in rows])
    output = []
    with svc.pool.connection() as conn:
        for row in rows:
            shown = SuggestionOut.of(row, prefs.get(row.suggestion_id))
            if current_language() == "en" and row.kind != "chat":
                trigger = store.get_trigger(conn, row.trigger_id) if row.trigger_id else None
                payload = trigger.payload if trigger is not None else {}
                title, reason = trigger_text(row.kind, payload)
                action = dict(shown.action)
                if row.kind == "wrong_answers" and "ask" in action:
                    action["ask"] = tr("Help me revisit {name}. Use my recent mistakes to identify what I missed, explain it differently, then give me a practice question.",
                                       lang="en", name=payload.get("name", "this concept"))
                shown = shown.model_copy(update={"title": title, "why": reason, "action": action})
            output.append(shown)
    return output


@router.post("/suggestions/{suggestion_id}/react", response_model=SuggestionOut)
def react(suggestion_id: str, body: ReactIn, svc: Svc, uid: User):
    check_user(svc, uid)
    row = sugg.react(svc.pool, uid, suggestion_id, body.reaction, now=svc.clock())
    return _suggestions(svc, uid, [row])[0]


@router.get("/review-reminder", response_model=ReviewReminderOut)
def review_reminder(svc: Svc, uid: User):
    check_user(svc, uid)
    r = sugg.review_reminder(svc.pool, uid, now=svc.clock())
    return ReviewReminderOut(hidden=r.hidden, preference_action_id=r.preference_action_id)


@router.post("/review-reminder", response_model=ReviewReminderOut)
def review_react(body: ReviewReactIn, svc: Svc, uid: User):
    """开始复习 (resets the cooldown) or 减少提醒 (one more step of it, one ledger row to undo)."""
    check_user(svc, uid)
    r = sugg.review_react(svc.pool, uid, body.reaction, now=svc.clock())
    return ReviewReminderOut(hidden=r.hidden, preference_action_id=r.preference_action_id)


def _ledger(svc: Services) -> ActionLedger:
    if svc.actions is None:
        raise HTTPException(status_code=503, detail="actions unavailable")
    return svc.actions


def _out_of(svc: Services, uid: str, rows: list[ActionRow]) -> list[ActionOut]:
    """Action rows with their 改动对比 (and a page revision's draft as track changes); the wiki
    state is read once, and only when a row needs it."""
    names = Names(PgRepo(svc.pool).load(uid).state if any(needs_state(r) for r in rows) else None,
                  asset_title(svc.deep.assets if svc.deep is not None else None, uid))
    superseded = _ledger(svc).superseded(uid, rows)
    sources = revise_sources(svc, uid, rows)
    return [ActionOut.of(r, diff_for(r, names), superseded, revise_out(r, names, sources))
            for r in rows]


def revise_sources(svc: Services, uid: str, rows: list[ActionRow]) -> dict[str, tuple[str, bool]]:
    """source_id -> (title, deleted) of the materials the page revisions among `rows` name."""
    ids = {sid for r in rows if r.kind == PAGE_REVISE for sid in revise_source_ids(r)}
    if not ids:
        return {}
    out = {}
    with svc.pool.connection() as conn:
        for sid in ids:
            row = source_store.get_source(conn, uid, sid)
            if row is not None:
                out[sid] = (row.title, row.deleted_at is not None)
    return out


ActionStatus = Literal["proposed", "approved", "rejected", "executed", "failed", "undone"]


@router.get("/actions", response_model=list[ActionOut])
def list_actions(svc: Svc, uid: User, status: ActionStatus | None = None):
    """The user's actions, newest first (a pending proposal stays until it is decided)."""
    check_user(svc, uid)
    ledger = _ledger(svc)
    rows = ledger.list(uid, status=status)
    if settle(ledger, PgRepo(svc.pool), uid, rows):
        rows = ledger.list(uid, status=status)
    return _out_of(svc, uid, rows)


@router.post("/actions/{action_id}/approve", response_model=ActionOut)
def approve_action(action_id: str, svc: Svc, uid: User, body: ApproveIn | None = None):
    check_user(svc, uid)
    draft = body.draft if body is not None else None
    return _out_of(svc, uid, [_ledger(svc).approve(uid, action_id, draft=draft)])[0]


@router.post("/actions/{action_id}/draft", response_model=ActionOut)
def edit_draft(action_id: str, body: DraftIn, svc: Svc, uid: User):
    """The learner's edit of a waiting draft on its card (a goal's blocks: move / × / 加一块),
    kept as it happens without approving (`ActionLedger.edit_draft`). 409: the row is not
    waiting any more, or the edit is refused (its text); 404: not the user's row."""
    check_user(svc, uid)
    try:
        row = _ledger(svc).edit_draft(uid, action_id, body.draft)
    except ActionRefused as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    return _out_of(svc, uid, [row])[0]


@router.post("/actions/{action_id}/redraft", response_model=ActionOut)
def redraft_action(action_id: str, svc: Svc, uid: User):
    """重试 on an in-page draft that no longer fits its page (the page changed where the draft goes):
    composed again for the same request against the page as it is now. A practice (加交互): the
    same row with a new draft, waiting again (a model call, answered when it is done). A materials
    draft: a new row written from the same materials (answered at once, being written; the old one
    is dropped). 409: refused (its text), not redraftable, or the page has another open draft;
    404: not the user's row."""
    check_user(svc, uid)
    ledger = _ledger(svc)
    row = ledger.get(uid, action_id)
    if row is None:
        raise LookupError(f"没有这条改动：{action_id}")
    try:
        if row.kind == PAGE_REVISE:
            if not (row.status == "proposed" or row.status == "failed"):
                raise HTTPException(status_code=409, detail="这份草稿已经处理过了")
            out = restart_revise(svc, uid, row)
        else:
            out = ledger.redraft(uid, action_id)
    except ActionRefused as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except RevisePending as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _out_of(svc, uid, [out])[0]


@router.post("/actions/{action_id}/reject", response_model=ActionOut)
def reject_action(action_id: str, svc: Svc, uid: User, tasks: BackgroundTasks):
    check_user(svc, uid)
    row = _ledger(svc).reject(uid, action_id)
    _tick_if_assistant(svc, uid, row, tasks)
    return _out_of(svc, uid, [row])[0]


@router.post("/actions/{action_id}/undo", response_model=ActionOut)
def undo_action(action_id: str, svc: Svc, uid: User, tasks: BackgroundTasks):
    check_user(svc, uid)
    row = _ledger(svc).undo(uid, action_id)
    _tick_if_assistant(svc, uid, row, tasks)
    return _out_of(svc, uid, [row])[0]


def _tick_if_assistant(svc: Services, uid: str, row: ActionRow, tasks: BackgroundTasks) -> None:
    """A rejected or undone assistant action counts towards the harness round (`rsi_tick`)."""
    if row.source == "assistant":
        tasks.add_task(rsi_tick, svc, uid)


@router.get("/why", response_model=WhyOut)
def why(svc: Svc, uid: User, suggestion_id: Annotated[str | None, Query()] = None,
        trigger_id: Annotated[str | None, Query()] = None):
    check_user(svc, uid)
    try:
        return why_mod.why(svc.pool, uid, suggestion_id=suggestion_id, trigger_id=trigger_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/recent", response_model=list[ActivityOut])
def recent(svc: Svc, uid: User, days: Annotated[int, Query(ge=1, le=90)] = 14,
           skipped: bool = False):
    check_user(svc, uid)
    return why_mod.recent(svc.pool, uid, now=svc.clock(), days=days, include_skipped=skipped,
                          writes=svc.write_queue)


@router.get("/badge", response_model=BadgeOut)
def badge(svc: Svc, uid: User):
    check_user(svc, uid)
    today_new, pending = sugg.badge(svc.pool, uid, now=svc.clock())
    return BadgeOut(today_new=today_new, pending_actions=pending)


@router.get("/goals", response_model=list[GoalOut])
def goals(svc: Svc, uid: User):
    """The user's goals: parked or not, the latest weekly review, this week's reading time."""
    check_user(svc, uid)
    return list_goals(svc.pool, uid, now=svc.clock())


@router.post("/goals/{path_id}/park", response_model=GoalOut)
def park_goal(path_id: str, svc: Svc, uid: User):
    """Park a goal: no 继续 card and no weekly review for it until resumed (nothing else
    changes). 404 for an unknown path."""
    check_user(svc, uid)
    return set_parked(svc.pool, uid, path_id, True, now=svc.clock())


@router.post("/goals/{path_id}/resume", response_model=GoalOut)
def resume_goal(path_id: str, svc: Svc, uid: User):
    check_user(svc, uid)
    out = set_parked(svc.pool, uid, path_id, False, now=svc.clock())
    _kick(svc, uid)             # the goal's pages are written again
    return out


def _kick(svc: Services, uid: str) -> None:
    """The idle page writing may have something to do now (settings, pause, a goal resumed)."""
    if svc.write_queue is not None:
        svc.write_queue.kick_soon(uid)


@router.get("/settings", response_model=BotSettings)
def get_settings(svc: Svc, uid: User):
    check_user(svc, uid)
    with svc.pool.connection() as conn:
        return store.get_settings(conn, uid)


@router.put("/settings", response_model=BotSettings)
def put_settings(body: BotSettings, svc: Svc, uid: User):
    check_user(svc, uid)
    with svc.pool.connection() as conn:
        store.put_settings(conn, uid, body, svc.clock())
    _kick(svc, uid)             # 「空闲时写页面」 may have been turned on
    return body


@router.post("/pause", response_model=BotSettings)
def pause(body: PauseIn, svc: Svc, uid: User):
    check_user(svc, uid)
    now = svc.clock()
    with svc.pool.connection() as conn, conn.transaction():
        s = store.get_settings(conn, uid)
        if body.until == "tomorrow":
            update = {"paused": "until", "paused_until": next_daily_slot(now, s)}
        elif body.until == "forever":
            update = {"paused": "always", "paused_until": None}
        else:
            update = {"paused": "no", "paused_until": None}
        s = s.model_copy(update=update)
        store.put_settings(conn, uid, s, now)
    _kick(svc, uid)             # the Bot may be running again
    return s
