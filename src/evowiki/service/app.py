"""FastAPI application factory and domain-error → HTTP status mapping."""

import functools
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from evowiki.agent.effects import EffectConflict
from evowiki.agent.lifecycle import DRAINED
from evowiki.assistant.service import SessionBusy
from evowiki.assistant.turn_runs import AssistantUnavailable
from evowiki.bot.actions import NotDecidable
from evowiki.bot.prompts import PromptInvalid
from evowiki.bot.suggestions import BotConflict
from evowiki.i18n import language_context, tr
from evowiki.llm import run_context
from evowiki.llm.providers import ProviderBusyError, ProviderTimeoutError, notice_error
from evowiki.llm.usage import BudgetExceededError
from evowiki.memory.forget import ForgetUnavailable
from evowiki.memory.tombstone import LedgerError
from evowiki.rules.authority import RuleAuthorityError
from evowiki.service import (
    routes_assistant,
    routes_bot,
    routes_components,
    routes_exam,
    routes_harness,
    routes_health,
    routes_prompts,
    routes_reading,
    routes_refine,
    routes_revise,
    routes_rsi,
    routes_setup,
    routes_sources,
    routes_teach,
    routes_usage,
    routes_users,
    routes_wiki,
)
from evowiki.service.deps import Services
from evowiki.service.drain import DrainMiddleware, RequestGate
from evowiki.service.rsi_runs import CycleBusy, NotUndoable
from evowiki.service.sse import Answered
from evowiki.store.trace import PgTraceSink
from evowiki.store.users import UserExists
from evowiki.wikiapp.llm import LLMFailed, LLMUnavailable
from evowiki.wikiapp.repo import WikiConflict, WikiInvalid
from evowiki.wikiapp.teachspec import SpecInvalid

# Starlette picks a handler by walking the exception's MRO, so the most specific registered
# class wins regardless of order (UserExists → 409; a bare KeyError → LookupError → 404).
log = logging.getLogger(__name__)

_STATUS: tuple[tuple[type[BaseException], int], ...] = (
    (WikiConflict, 409), (LLMUnavailable, 503), (LLMFailed, 502), (WikiInvalid, 422),
    (SpecInvalid, 422), (PromptInvalid, 422), (RuleAuthorityError, 409),
    (UserExists, 409),
    (SessionBusy, 409), (AssistantUnavailable, 503), (NotUndoable, 409), (CycleBusy, 409),
    (BotConflict, 409), (NotDecidable, 409), (EffectConflict, 409),
    (PermissionError, 403),
    (NotImplementedError, 501),
    (BudgetExceededError, 429),
    (ProviderBusyError, 503),
    (ForgetUnavailable, 503), (LedgerError, 503),
    (ProviderTimeoutError, 504),
    (LookupError, 404),
)
# The user-facing `detail` of a mapped error (copy guide 2026-10-21 §7: what failed + what to do; never the raw
# exception text, which can carry ids, English or internals; that goes to the log). Status codes are unchanged.
_DETAIL: dict[type[BaseException], str] = {
    AssistantUnavailable: 'Switch models and try again.',
    LLMUnavailable: 'Switch models and try again.',
    LLMFailed: 'Switch models and try again.',
    WikiConflict: 'This content changed elsewhere. Refresh and try again.',
    EffectConflict: 'This content changed elsewhere. Refresh and try again.',
    WikiInvalid: 'Could not save. Check the content and try again.',
    SpecInvalid: 'Could not save. Check the content and try again.',
    RuleAuthorityError: 'This rule cannot be changed now. Refresh to check its status.',
    PromptInvalid: 'Could not save. Check the content and try again.',
    UserExists: 'This user already exists.',
    SessionBusy: 'The previous turn is still running.',
    NotUndoable: 'This change cannot be undone now.',
    CycleBusy: 'An improvement run is already in progress.',
    BotConflict: 'This action was already handled. Refresh to check its status.',
    NotDecidable: 'This change cannot be accepted or rejected now.',
    PermissionError: 'You do not have permission for this action.',
    NotImplementedError: 'This feature is not available yet.',
    LookupError: 'This item was not found. It may have been deleted.',
    ForgetUnavailable: 'The deletion could not be completed. Try again.',
    LedgerError: 'Memory is temporarily unavailable. Try again later.',
}

def _public_error(exc: BaseException) -> BaseException:
    """Preserve an explicitly declared public error across domain wrapper exceptions."""
    current = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(getattr(current, "public_message", None), str):
            return current
        seen.add(id(current))
        current = current.__cause__
    return exc


def _detail(exc: BaseException, *, lang: str = "zh") -> str:
    """Only declared public messages cross the error boundary; exception text stays in logs."""
    public = _public_error(exc)
    message = getattr(public, "public_message", None)
    params = getattr(public, "params", {})
    if isinstance(message, str):
        return tr(message, lang=lang, **params)
    for cls in type(exc).__mro__:
        if cls in _DETAIL:
            return tr(_DETAIL[cls], lang=lang)
    return tr("The action failed. Try again.", lang=lang)


def _request_language(request: Request) -> str:
    if locale := getattr(request.state, "language", None):
        return locale
    uid = request.headers.get("x-evowiki-user")
    pool = getattr(request.app.state.services, "pool", None)
    if uid and pool is not None:  # router-only applications have no account store
        from evowiki.wikiapp.repo import PgRepo
        try:
            return PgRepo(pool).load(uid).state.language
        except LookupError:
            pass  # No account language exists for an unknown identity.
    return "en" if request.headers.get("accept-language", "").lower().startswith("en") else "zh"


def create_app(services: Services) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # every model call of a user is a step of a traced run while the app runs
        # (llm/run_context.py, observability plan 2026-10-29); the previous store is back on exit,
        # a failed startup included
        prev = run_context.configured()
        run_context.configure(PgTraceSink(services.pool, services.clock))
        try:
            async with _running(_app):
                yield
        finally:
            run_context.configure(prev)

    @asynccontextmanager
    async def _running(_app: FastAPI):
        # DBOS is launched with the app (recovery of pending page runs happens at launch) and shut
        # down on exit; without `services.durable` (tests with inline runs) nothing is launched.
        # start / stop run in a worker thread: the sweeps and after-launch hooks use DBOS's sync
        # API, which DBOS refuses on a thread with a running event loop (this one). A raising start
        # propagates unchanged and aborts startup (fail closed).
        rt = services.durable
        if rt is not None:
            from evowiki.service.deps import page_deps
            a = services.assistant
            deps = page_deps(services)
            await run_in_threadpool(functools.partial(
                rt.start, deps, sweep=getattr(services.runs, "sweep", None),
                assistant=a if a is not None and a.durable else None))
        # vectors of an older embedding model are re-embedded in the background (only with the
        # durable runtime, i.e. the real service; tests call `reembed_stale` directly)
        reembed = None
        if rt is not None:
            from evowiki.sources.reembed import start_background
            reembed = start_background(services.pool, services.embedder)
        # the forget ledger is reconciled before the assistant answers (memory plan Task 14);
        # until it passes /assistant/* is 503, and a background thread keeps trying (and then
        # retries the deletes that have not committed)
        forgetting = None
        forget = getattr(services, "forget", None)
        if forget is not None and getattr(services, "forget_ledger", None) is not None:
            from evowiki.memory import tombstone
            try:
                await run_in_threadpool(tombstone.reconcile, services.pool,
                                        services.forget_ledger, forget)
                app.state.memory_ready = True
            except Exception:
                log.exception("forget reconcile failed at startup; the assistant stays closed")
            forgetting = tombstone.start_background(app.state, services.pool,
                                                    services.forget_ledger, forget)
        # the OTel export to a local Phoenix (obs/exporter.py): only the real service, and only
        # with EVOWIKI_OTEL_ENDPOINT set
        exporting = None
        if rt is not None:
            from evowiki.obs import exporter as otel
            if (cfg := otel.ExportConfig.from_env()) is not None:
                exporting = otel.start_background(otel.build(services.pool, cfg, services.clock),
                                                  cfg.every)
        # the daily database backup (service/backup.py): only the real service; a failure is logged, never fatal
        backing = None
        if rt is not None:
            from evowiki.service import backup
            if (bcfg := backup.BackupConfig.from_env(os.environ)) is not None:
                backing = backup.start_background(bcfg, services.clock)
        try:
            yield
        finally:
            if backing is not None:
                backing[1].set()
            if exporting is not None:
                exporting[1].set()
            if forgetting is not None:
                forgetting[1].set()
            if reembed is not None:
                reembed[1].set()
            if rt is not None:
                await run_in_threadpool(rt.stop)

    app = FastAPI(title="EvoWiki", version="0.1.0", lifespan=lifespan)
    app.state.services = services
    # False until the forget reconcile passed (set in lifespan); no delete flow wired: open
    app.state.memory_ready = getattr(services, "forget", None) is None

    @app.middleware("http")
    async def memory_gate(request: Request, call_next):
        request.state.language = await run_in_threadpool(_request_language, request)
        with language_context(request.state.language):
            if not request.app.state.memory_ready and request.url.path.startswith("/assistant"):
                return JSONResponse(status_code=503, content={"detail": tr("Checking deletion records. Try again shortly.",
                                                                      lang=request.state.language),
                                                           "code": "memory_unavailable", "params": {}})
            return await call_next(request)
    gate = RequestGate(DRAINED)
    app.state.request_gate = gate
    app.add_middleware(DrainMiddleware, gate=gate)
    for module in (routes_users, routes_wiki, routes_teach, routes_assistant, routes_usage, routes_rsi, routes_refine,
                   routes_components, routes_bot, routes_reading, routes_prompts, routes_harness,
                   routes_sources, routes_revise, routes_health, routes_exam, routes_setup):
        app.include_router(module.router)
    # in-flight DBOS workflows for /health (tests replace it; without a durable runtime there are none)
    app.state.in_flight = routes_health.dbos_in_flight if getattr(services, "durable", None) is not None else (lambda: 0)

    for exc_type, status in _STATUS:
        async def handler(request: Request, exc: BaseException, status: int = status):
            notice = notice_error(exc)   # e.g. an LLMFailed raised from a busy provider
            if notice is not None:
                return JSONResponse(status_code=503 if notice.code == "llm_busy" else 504,
                                    content={"detail": tr("The model service is busy. Try again." if notice.code == "llm_busy"
                                                          else "The model service timed out. Try again.",
                                                          lang=_request_language(request)),
                                             "code": notice.code, "params": {}})
            detail = _detail(exc, lang=_request_language(request))
            if detail != str(exc).strip():
                log.warning("%s %s → %d: %s", request.method, request.url.path, status, f"{type(exc).__name__}: {exc}")
            public = _public_error(exc)
            content = {"detail": detail, "code": getattr(public, "error_code", None) or type(exc).__name__,
                       "params": getattr(public, "params", {})}
            code = getattr(public, "error_code", None)   # a machine code clients branch on (e.g. patches_off)
            if isinstance(code, str) and code:
                content["code"] = code
            return JSONResponse(status_code=status, content=content)
        app.add_exception_handler(exc_type, handler)

    async def public_http_error(request: Request, exc: StarletteHTTPException):
        lang = _request_language(request)
        public = _public_error(exc)
        if public is not exc:
            code = getattr(public, "error_code", None) or f"http_{exc.status_code}"
            params = getattr(public, "params", {})
            detail = _detail(public, lang=lang)
        elif isinstance(exc.detail, dict):
            code = str(exc.detail.get("code") or f"http_{exc.status_code}")
            params = exc.detail.get("params", {})
            messages = {"rsi_setup_incomplete": "Complete the missing RSI configuration in settings.",
                        "forge_globally_disabled": "Tool and code generation is disabled for this instance."}
            detail = tr(str(exc.detail.get("message") or messages.get(code, "The action failed. Check your settings and try again.")), lang=lang, **params)
        else:
            code, params = f"http_{exc.status_code}", {}
            detail = tr(str(exc.detail), lang=lang)
        return JSONResponse(status_code=exc.status_code,
                            content={"code": code, "params": params, "detail": detail},
                            headers=exc.headers)
    app.add_exception_handler(StarletteHTTPException, public_http_error)

    async def invalid_request(request: Request, exc: RequestValidationError):
        # Only schema-declared field names cross this boundary; never input values, custom
        # validator text, dictionary keys, credentials or Pydantic's raw error context.
        declared: set[str] = set()
        route = request.scope.get("route")
        for field in getattr(getattr(route, "dependant", None), "body_params", ()):
            model = getattr(getattr(field, "field_info", None), "annotation", None)
            declared.update(getattr(model, "model_fields", {}))
        fields = sorted({str(error["loc"][1]) for error in exc.errors()
                         if len(error["loc"]) > 1 and error["loc"][0] == "body"
                         and error["loc"][1] in declared})
        public = {
            "source_title_empty": ("A material name cannot be empty.", {}),
            "source_title_too_long": ("A material name can contain at most {limit} characters.", {"limit": 200}),
        }
        for error in exc.errors():
            loc = error["loc"]
            if error["type"] in public and len(loc) == 2 and loc[0] == "body" and loc[1] == "title" and "title" in declared:
                message, limits = public[error["type"]]
                params = {"field": "title", **limits}
                return JSONResponse(status_code=422, content={
                    "code": error["type"], "params": params,
                    "detail": tr(message, lang=_request_language(request), **params)})
        return JSONResponse(status_code=422, content={
            "code": "invalid_request", "params": {"fields": fields},
            "detail": tr("Check the highlighted fields and try again.", lang=_request_language(request))})
    app.add_exception_handler(RequestValidationError, invalid_request)

    async def answered(_request: Request, exc: BaseException) -> Response:
        assert isinstance(exc, Answered)
        return exc.response
    app.add_exception_handler(Answered, answered)   # a stream route that answers without a stream
    return app
