"""/assistant/*: the reading assistant (plan rev 3.2 §9.1) - sessions, a turn streamed over SSE,
and accept / reject of the page edits it proposes. 503 while no assistant is wired.

Deleting (cross-session memory plan Task 13, memory/forget.py): GET
/sessions/{id}/forget-preview lists what the delete dialog asks about; DELETE /sessions/{id}
deletes the session (200), or records the request and answers 202 when the attempt failed (it
is retried until done); GET /forget-requests lists those still being deleted; DELETE
/memory/{id} forgets one decision / open question. 503 when the delete flow is not wired.

Possibly conflicting memories (memory/conflicts.py): GET /memory/conflicts lists the open groups;
POST /memory/conflicts/{id} settles one ({"keep": item id} or {"keep_both": true}). 503 when not
wired."""

import logging
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from evowiki.assistant.attachments import GONE_DETAIL, AttachmentGone, AttachmentUnusable
from evowiki.assistant.contracts import (
    ErrorEv,
    ForgetFactOut,
    ForgetIn,
    ForgetMaterialOut,
    ForgetOut,
    ForgetPendingListOut,
    ForgetPendingOut,
    ForgetPreviewOut,
    ForgetRuleOut,
    ForgetSessionOut,
    KnownAnswerOut,
    KnownItemOut,
    LevelOut,
    MemoryConflictListOut,
    MemoryConflictResolveIn,
    MemoryConflictResolveOut,
    MessageIn,
    ModelIn,
    ModelOut,
    ModelsOut,
    ProposalDecisionOut,
    RatingIn,
    RatingOut,
    SessionCreateIn,
    SessionCreateOut,
    SessionListOut,
    SessionOut,
    StatusLine,
)
from evowiki.assistant.service import AssistantService
from evowiki.bot.actions import ActionRefused
from evowiki.i18n import current_language, tr
from evowiki.llm.usage_control import PICK_KEY
from evowiki.memory.forget import ELSEWHERE, RETRY_DETAIL, UNAVAILABLE, ForgetService
from evowiki.service.deps import Services, current_user, get_services
from evowiki.service.rsi_runs import rsi_tick
from evowiki.service.sse import Answered, closing_stream, event

log = logging.getLogger(__name__)
router = APIRouter(prefix="/assistant")
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]
Events = Iterator[ServerSentEvent]


def _assistant(svc: Services) -> AssistantService:
    if svc.assistant is None:
        raise HTTPException(status_code=503, detail="请切换模型重试")
    return svc.assistant


@router.post("/sessions")
def create_session(body: SessionCreateIn, svc: Svc, uid: User) -> SessionCreateOut:
    return _assistant(svc).create_session(uid, body)


# Model and reasoning share one persisted choice across the user's calls.
MODEL_GROUP = PICK_KEY
MODEL_OFF = "这个模型现在不能用"


def model_error(code: str, message: str, *, missing: bool = False, invalid: bool = False):
    from evowiki.wikiapp.repo import WikiConflict, WikiInvalid
    exc = (LookupError if missing else WikiInvalid if invalid else WikiConflict)(message)
    exc.error_code = code
    exc.public_message = message
    exc.params = {}
    return exc


def _models(svc: Svc, uid: str) -> ModelsOut:
    from evowiki.llm.catalog import LEVEL_NAMES
    from evowiki.wikiapp.repo import PgRepo, UnknownUser
    try:
        lang = PgRepo(svc.pool).load(uid).state.language
    except UnknownUser:
        lang = current_language()  # Model catalog is also available before account creation.
    from evowiki.runtime.models import model_states
    from evowiki.store.usage import chosen_models, latest_attempt

    choice = svc.models
    with svc.pool.connection() as conn:
        picked = chosen_models(conn, uid)
        states = model_states(choice.configs if choice else {},
                              lambda providers, model: latest_attempt(
                                  conn, providers, model, since=choice.health_since if choice else None),
                              choice.catalog if choice else None,
                              choice.unavailable if choice else None)
    current = choice.current(picked.get(MODEL_GROUP)) if choice else picked.get(MODEL_GROUP)
    reasoning = choice.reasoning(current, picked.get("reasoning")) if choice and current else None
    return ModelsOut(models=tuple(ModelOut(
        id=m.id, name=m.name, provider=m.provider, provider_name=m.provider_name,
        available=m.available, reason=tr({"pricing_missing": "Model pricing is not configured. Open model settings.",
                                        "provider_unavailable": "The model connection is not ready. Check its API key and SDK."}.get(m.reason, m.reason), lang=lang),
        levels=tuple(LevelOut(reasoning=level, name=tr(LEVEL_NAMES[level], lang=lang)) for level in m.levels))
        for m in states), current=current, reasoning=reasoning)


@router.get("/models")
def list_models(svc: Svc, uid: User) -> ModelsOut:
    """Configured catalog and this user's concrete model/reasoning choice; no model probes."""
    return _models(svc, uid)


@router.post("/model")
def pick_model(body: ModelIn, svc: Svc, uid: User) -> ModelsOut:
    """Validate the complete choice before atomically persisting model and reasoning."""
    from evowiki.store.usage import set_model_choice

    out = _models(svc, uid)
    model = body.model or out.current
    state = next((m for m in out.models if m.id == model), None)
    if state is None:
        raise model_error("model_not_found", "This model was not found. Select another model.", missing=bool(body.model))
    if not state.available:
        raise model_error("model_unavailable", state.reason or "This model is unavailable.")
    spec = svc.models.spec(model) if svc.models else None
    if spec is None:
        raise model_error("model_unavailable", "This model is unavailable.")
    reasoning = body.reasoning if body.reasoning is not None else svc.models.reasoning(model, out.reasoning)
    if reasoning not in spec.levels:
        raise model_error("reasoning_unsupported", "This model does not support this reasoning level.", invalid=True)
    with svc.pool.connection() as conn:
        set_model_choice(conn, uid, model, reasoning, now=svc.clock())
    return _models(svc, uid)


@router.get("/sessions")
def list_sessions(svc: Svc, uid: User, q: str = "",
                  limit: Annotated[int, Query(ge=1, le=500)] = 100) -> SessionListOut:
    return _assistant(svc).list_sessions(uid, q, limit)


@router.get("/forget-requests")
def forget_requests(svc: Svc, uid: User) -> ForgetPendingListOut:
    """The sessions being deleted (「删除中」): asked, not committed yet."""
    forget: ForgetService | None = getattr(svc, "forget", None)
    rows = forget.pending(uid) if forget is not None else []
    return ForgetPendingListOut(sessions=tuple(ForgetPendingOut(
        session_id=r["session_id"], state=r["state"], requested_at=r["requested_at"])
        for r in rows))


def _forget(svc: Services, *, sessions: bool = True) -> ForgetService:
    """The delete flow; a session's delete also needs its RSI and S3 steps wired."""
    f: ForgetService | None = getattr(svc, "forget", None)
    if f is None or (sessions and not f.available):
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return f


@router.get("/sessions/{session_id}/forget-preview")
def forget_preview(session_id: str, svc: Svc, uid: User) -> ForgetPreviewOut:
    p = _forget(svc).preview(uid, session_id)          # someone else's / unknown -> 404
    return ForgetPreviewOut(
        rules=tuple(ForgetRuleOut(item_id=r.item_id, text=r.text, authority=r.authority)
                    for r in p.rules),
        facts=tuple(ForgetFactOut(key=f.key, section=f.section, text=f.text) for f in p.facts),
        tainted_sessions=tuple(ForgetSessionOut(session_id=t.session_id, title=t.title,
                                                turns=t.turns) for t in p.tainted_sessions),
        memory_counts=dict(p.memory_counts),
        materials=tuple(ForgetMaterialOut(source_id=m.source_id, title=m.title,
                                          in_use_elsewhere=m.in_use_elsewhere, used_by=m.used_by)
                        for m in p.materials),
        note=p.note)


def _forget_answer(committed: bool) -> ForgetOut | JSONResponse:
    if committed:
        return ForgetOut(state="committed")
    return JSONResponse(status_code=202, content=ForgetOut(
        state="failed", detail=RETRY_DETAIL).model_dump(mode="json"))


@router.delete("/sessions/{session_id}", response_model=ForgetOut,
               responses={202: {"model": ForgetOut}})
def delete_session(session_id: str, svc: Svc, uid: User,
                   body: ForgetIn | None = None) -> ForgetOut | JSONResponse:
    body = body or ForgetIn()
    out = _forget(svc).forget_session(uid, session_id, revoke_rule_ids=body.revoke_rule_ids,
                                      revoke_fact_keys=body.revoke_fact_keys,
                                      delete_source_ids=body.delete_source_ids,
                                      requested_by=f"api:{uid}")
    return _forget_answer(out.state == "committed")


CONFLICTS_OFF = "没能读到，重试"


def _conflicts(svc: Services) -> Any:
    c = getattr(svc, "conflicts", None)
    if c is None:
        raise HTTPException(status_code=503, detail=CONFLICTS_OFF)
    return c


@router.get("/memory/conflicts")
def memory_conflicts(svc: Svc, uid: User) -> MemoryConflictListOut:
    return MemoryConflictListOut(conflicts=_conflicts(svc).open(uid))


@router.post("/memory/conflicts/{conflict_id}")
def resolve_memory_conflict(conflict_id: str, body: MemoryConflictResolveIn, svc: Svc,
                            uid: User) -> MemoryConflictResolveOut:
    """404: no such open group of his; 422: `keep` is not one of its entries."""
    try:
        status, action_id = _conflicts(svc).resolve(uid, conflict_id, keep=body.keep,
                                                    keep_both=body.keep_both)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="这组记忆已经处理过或不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="要保留的这条不在这一组里") from exc
    return MemoryConflictResolveOut(conflict_id=conflict_id, status=status, action_id=action_id)


@router.delete("/memory/{item_id}", response_model=ForgetOut,
               responses={202: {"model": ForgetOut}})
def delete_memory(item_id: str, svc: Svc, uid: User) -> ForgetOut | JSONResponse:
    out = _forget(svc, sessions=False).forget_item(uid, item_id, requested_by=f"api:{uid}")
    if out == "not_found":
        raise HTTPException(status_code=404, detail="找不到这条记忆")
    if out == "elsewhere":
        raise HTTPException(status_code=409, detail=ELSEWHERE)
    return _forget_answer(out == "committed")


THINKING = "正在思考…"


def _event_id(turn: Any, message_id: str | None) -> str | None:
    """`<message_id>:<cursor>` for a durable turn (the resume point), else None."""
    cursor = getattr(turn, "cursor", None)
    return f"{message_id}:{cursor}" if message_id is not None and cursor is not None else None


def parse_event_id(value: str) -> tuple[str, int]:
    mid, sep, n = value.rpartition(":")
    if not sep or not mid or not n.isdigit():
        raise HTTPException(status_code=422, detail="last_event_id must be <message_id>:<n>")
    return mid, int(n)


def _send_events(session_id: str, body: MessageIn, svc: Svc, uid: User) -> Iterator[Events]:
    try:
        turn = _assistant(svc).send(uid, session_id, body)
    except AttachmentUnusable as exc:
        raise Answered(_attachment_refused(exc.detail)) from exc
    except AttachmentGone as exc:
        raise Answered(_attachment_refused(GONE_DETAIL)) from exc
    yield from _turn_events(turn, getattr(turn, "message_id", None), announce=True)


@router.post("/sessions/{session_id}/messages", response_class=EventSourceResponse)
def send_message(events: Annotated[Events, Depends(_send_events)]) -> Events:
    """403 / 404 (unknown session) / 409 before streaming: `code` session_busy (a turn is still
    running), or `code` attachment_refused for an attachment that is deleted, not the user's,
    still reading or failed (the detail is the text to show). A gone attachment is a 409, not a
    404 (a 404 makes the client forget the session); someone else's id gets the same text, so its
    existence is not revealed."""
    yield from events


@router.post("/sessions/{session_id}/stop", status_code=204)
def stop_turn(session_id: str, svc: Svc, uid: User) -> Response:
    """The chat's stop: the session's running turn ends as stopped on its own stream (`done` with
    `stopped`), which the client that sent it is still following. 404: no turn is running."""
    _assistant(svc).stop(uid, session_id)
    return Response(status_code=204)


ATTACHMENT_REFUSED = "attachment_refused"


def _attachment_refused(detail: str) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": detail, "code": ATTACHMENT_REFUSED})


def _resume_events(session_id: str, svc: Svc, uid: User,
                   last_event_id: Annotated[str | None, Query()] = None,
                   last_event_id_header: Annotated[str | None,
                                                   Header(alias="Last-Event-ID")] = None,
                   ) -> Iterator[Events]:
    value = last_event_id or last_event_id_header
    if not value:
        raise HTTPException(status_code=422, detail="last_event_id is required")
    mid, after = parse_event_id(value)
    turn = _assistant(svc).resume(uid, session_id, mid, after)
    yield from _turn_events(turn, mid)


@router.get("/sessions/{session_id}/events", response_class=EventSourceResponse)
def resume_events(events: Annotated[Events, Depends(_resume_events)]) -> Events:
    """A turn's stream again, from the SSE id the client last saw (spec 2026-10-15 §3): the
    `last_event_id` query or the standard `Last-Event-ID` header. 404 for an unknown turn or an
    assistant that runs turns inline."""
    yield from events


def _turn_events(turn: Any, message_id: str | None, *, announce: bool = False) -> Iterator[Events]:
    """A stream dependency's tail for a turn (`service.sse`): the turn is closed (which releases
    the session lock) when the response has ended, also when the client left before its first
    event."""
    def stream() -> Events:
        last = None
        try:
            # a resume point before the first model reply: a restart then finds the turn again
            # (live 2026-11-05: killed while thinking, the resend got 「上一轮还没结束」 for good)
            first = _event_id(turn, message_id)
            if announce and first is not None:
                st = StatusLine(text=tr(THINKING, lang=current_language()))
                yield event(st.type, st.model_dump(mode="json"), first)
            for ev in turn:
                last = ev.type
                yield event(ev.type, ev.model_dump(mode="json"), _event_id(turn, message_id))
        except Exception as exc:
            log.exception("assistant stream failed")
            err = ErrorEv(code="internal", retryable=True,
                          detail=f"{type(exc).__name__}: {exc}"[:500])
            yield event(err.type, err.model_dump(mode="json"))
        else:
            if last not in ("done", "error"):
                err = ErrorEv(code="internal", retryable=True, detail="stream ended without done")
                yield event(err.type, err.model_dump(mode="json"))
        finally:
            close = getattr(turn, "close", None)   # releases the session lock as soon as the turn
            if close is not None:                  # ends, before the response's last bytes go out
                close()

    # closing the turn again is a no-op; it covers a client gone before stream() started
    yield from closing_stream(stream(), turn)


@router.get("/sessions/{session_id}")
def get_session(session_id: str, svc: Svc, uid: User) -> SessionOut:
    return _assistant(svc).get_session(uid, session_id)


@router.post("/sessions/{session_id}/messages/{message_id}/rating")
def rate_answer(session_id: str, message_id: str, body: RatingIn, svc: Svc,
                uid: User, tasks: BackgroundTasks) -> RatingOut:
    """👍 / 👎 on one answer (`message_id` = the assistant answer's id, as `Done.message_id`),
    `value` null clears it; returns the stored rating (latest wins). 404: an unknown or someone
    else's session, or no answer with that id in it; 422: chips without "down". A 👎 counts
    towards the harness round (`rsi_tick`)."""
    out = _assistant(svc).rate(uid, session_id, message_id, body)
    tasks.add_task(rsi_tick, svc, uid)
    return out


@router.post("/proposals/{proposal_id}/accept")
def accept(proposal_id: str, svc: Svc, uid: User) -> ProposalDecisionOut:
    return _assistant(svc).decide(uid, proposal_id, accept=True)


@router.post("/proposals/{proposal_id}/reject")
def reject(proposal_id: str, svc: Svc, uid: User, tasks: BackgroundTasks) -> ProposalDecisionOut:
    out = _assistant(svc).decide(uid, proposal_id, accept=False)
    tasks.add_task(rsi_tick, svc, uid)          # a rejection counts towards the harness round
    return out


class ChoiceIn(BaseModel):
    """`choice`: the option index, -1 = 不知道."""

    choice: int = Field(ge=-1)


@router.get("/actions/{action_id}/confirm")
def known_item(action_id: str, svc: Svc, uid: User) -> KnownItemOut:
    """The one confirm question after 「这个我早会了」 (no answer in it)."""
    return _assistant(svc).known_item(uid, action_id)


@router.post("/actions/{action_id}/confirm")
def known_answer(action_id: str, body: ChoiceIn, svc: Svc, uid: User) -> KnownAnswerOut:
    """Answer it: right keeps the claim, wrong takes it back (409 when already answered / undone)."""
    try:
        return _assistant(svc).known_answer(uid, action_id, body.choice)
    except ActionRefused as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
