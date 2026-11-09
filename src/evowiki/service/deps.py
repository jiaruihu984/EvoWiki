"""Service wiring: the process-wide Services bundle and FastAPI dependencies."""

import logging
import os
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Header, Request
from psycopg_pool import ConnectionPool

from evowiki.agent import dbos_app
from evowiki.clock import Clock, SystemClock
from evowiki.content.loaded import LoadedContent
from evowiki.harness.source import HarnessSource
from evowiki.llm.catalog import model_specs, supports_images
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import PriceTable, load_prices
from evowiki.llm.usage import UsageMeter
from evowiki.llm.usage_control import UsageControl, check_prices
from evowiki.retrieval.embed import Embedder, FastEmbedEmbedder, embedder_from_env
from evowiki.runtime.context import RuntimeContext, prepare_runtime
from evowiki.runtime.llm import (
    RUNTIME_RECORD_MAX_ATTEMPTS,
    provider_config_problem,
    runtime_deep_providers,
    runtime_gateway_config,
    runtime_providers,
)
from evowiki.runtime.policy import PolicyBundle
from evowiki.runtime.usage import (
    runtime_deep_config,
    runtime_deep_fallback_config,
    runtime_tier_configs,
    runtime_visual_config,
)
from evowiki.sources.runs import DbosSourceRuns, NoSourceRuns, SourceRuns
from evowiki.store.content import initialize_content
from evowiki.store.db import database_url, make_pool
from evowiki.store.llm_logs import PgAttemptLog, PgCallLog
from evowiki.store.trace import PgRequestLog, PgTraceSink
from evowiki.store.usage import PgUsageMeter, usage_tz
from evowiki.store.users import UnknownUser, get_user
from evowiki.wikiapp.page_runs import DbosPageRuns, NoPageRuns, PageRuns

if TYPE_CHECKING:
    from evowiki.assistant.service import AssistantService
    from evowiki.assistant.tools.actions import Imported, ImportRequest
    from evowiki.assistant.turn_runs import TurnRuns
    from evowiki.bot.actions import ActionLedger, ActionRow
    from evowiki.bot.diffs import Names
    from evowiki.bot.practice import DeepPracticeWriter, PracticeDraft
    from evowiki.deep.library import ComponentLibrary
    from evowiki.deep.store import AssetStore
    from evowiki.forge.run import ForgeRuntime
    from evowiki.memory.conflicts import ConflictService
    from evowiki.memory.forget import ForgetService
    from evowiki.memory.tombstone import Ledger
    from evowiki.profile.service import ProfileService
    from evowiki.runtime.models import ModelChoice
    from evowiki.sources.claude_pages import ClaudePages
    from evowiki.sources.exam_flow import ExamDeps
    from evowiki.sources.importer import ImportDeps, LayoutParser
    from evowiki.sources.match_flow import MatchDeps
    from evowiki.sources.vision import VisionReader
    from evowiki.teach.change_source import ChangeSource
    from evowiki.wikiapp.group_naming import NamingDeps
    from evowiki.wikiapp.organize import Writing
    from evowiki.wikiapp.page_flows import PageDeps
    from evowiki.wikiapp.refine_service import DeepService
    from evowiki.wikiapp.write_queue import WriteQueue

log = logging.getLogger("evowiki.service")


def default_new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


# the providers that serve Claude (the plain one and the composer's 思考 level): their turns see images

@dataclass
class Services:
    pool: ConnectionPool
    ctx: RuntimeContext
    clock: Clock
    embedder: Embedder
    gateway: Gateway | None = None  # None: LLM unavailable → degrade (spec §5.7)
    new_id: Callable[[str], str] = default_new_id
    assistant: "AssistantService | None" = None  # reading assistant; None -> /assistant/* 503
    deep: "DeepService | None" = None  # deep refine jobs; None -> /wiki/pages/{id}/refine 503
    library: "ComponentLibrary | None" = None  # interactive components; None -> /wiki/components 503
    # page runs (organize, feedback, refine, rsi rewrite): `build_services` sets
    # `DbosPageRuns`; tests set `InlinePageRuns(store=PgPageRunStore(pool))`. The default fails
    # closed on use (run ids must not repeat for one repo: effect keys are `<run_id>:<step>`).
    runs: PageRuns = field(default_factory=NoPageRuns)
    # source imports (资料): `build_services` sets `DbosSourceRuns`; tests set
    # `InlineSourceRuns(import_deps(svc))`. The default fails closed on use.
    sources: SourceRuns = field(default_factory=NoSourceRuns)
    # launches DBOS in the app lifespan (None: no durable runtime, e.g. tests with inline runs)
    durable: "DurableRuntime | None" = None
    # an explicit writer kit (tests); None = built from `deep` (`writing()`)
    page_writing: "Writing | None" = None
    # external RSI change sources (teach.change_source, e.g. the knowledge graph's split / merge):
    # their rows join GET /wiki/rsi/changes and their ids' undo goes through the same route
    change_sources: "tuple[ChangeSource, ...]" = ()
    # wake-up of the proactive Bot after a request that recorded events (set by the service launch;
    # None = the Bot is off). Never raises.
    bot_notify: Callable[[str], None] | None = None
    # action ledger (approve / reject / undo of agent changes); None -> /wiki/bot/actions 503
    actions: "ActionLedger | None" = None
    # write-all (spec 2026-10-15 §4): the user's requested pages and the Bot's idle writing; None
    # = pages are only written where they are opened (tests without the Bot)
    write_queue: "WriteQueue | None" = None
    # the models the assistant composer's picker offers (runtime.models); None -> GET /assistant/models
    # lists none and a pick is refused
    models: "ModelChoice | None" = None
    # the learner profile (「我」 note; every model call made for a user carries it via the gateway);
    # None -> /wiki/profile 503
    profile: "ProfileService | None" = None
    # deleting a session / a memory item (memory/forget.py) and its out-of-database ledger
    # (memory/tombstone.py, reconciled in the app lifespan); None -> the delete routes 503
    forget: "ForgetService | None" = None
    forget_ledger: "Ledger | None" = None
    # possibly conflicting memories (memory/conflicts.py); None -> /assistant/memory/conflicts 503
    conflicts: "ConflictService | None" = None

    def writing(self) -> "Writing | None":
        """The page writer kit: `page_writing` when set, else the deep tier's writer, spec source and
        asset store. None without a deep service (organize then fails with llm_unavailable)."""
        if self.page_writing is not None:
            return self.page_writing
        if self.deep is None:
            return None
        from evowiki.deep.writer import PageWriter
        from evowiki.wikiapp.organize import Writing
        return Writing(writer=PageWriter(llm=self.deep.llm, checker=self.deep.checker,
                                         library=self.deep.library, forge=self.deep.forge,
                                         images=self.deep.images),
                       specs=self.deep.specs, assets=self.deep.assets, embedder=self.embedder,
                       sources=self.deep.sources, reader=self.deep.reader)


def ledger_stale(pool: ConnectionPool) -> Callable[[str], tuple[Counter[str], Counter[str]]]:
    """The ledger rows' stale-page counts (a rule row marks the pages it outdated by its action id)."""
    from evowiki.wikiapp.repo import PgRepo
    from evowiki.wikiapp.stale import stale_counts
    repo = PgRepo(pool)
    return lambda user_id: stale_counts(repo.load(user_id).state)


def ledger_names(pool: ConnectionPool) -> "Callable[[str], Names]":
    """The names a ledger row's words need (a page revision's page title, its draft's sections
    against the current page)."""
    from evowiki.bot.diffs import Names
    from evowiki.deep.store import PgAssetStore
    from evowiki.wikiapp.repo import PgRepo
    repo = PgRepo(pool)
    assets = PgAssetStore(pool)
    return lambda user_id: Names(repo.load(user_id).state, asset_title(assets, user_id))


def asset_title(assets: "AssetStore | None", user_id: str) -> "Callable[[str], str | None] | None":
    """A stored EvoUI block's title by asset id (`Names.asset`), None without an asset store."""
    if assets is None:
        return None

    def title(asset_id: str) -> str | None:
        a = assets.get(user_id, asset_id)
        return None if a is None else a.meta.title
    return title


def graph_change_sources(pool: ConnectionPool,
                         new_id: Callable[[str], str]) -> "tuple[ChangeSource, ...]":
    """The RSI change sources of the wiki: the knowledge graph's splits / merges (track G,
    ids "g_…", `wikiapp.graph_changes`)."""
    from evowiki.wikiapp.graph_changes import GraphChanges
    from evowiki.wikiapp.repo import PgRepo
    return (GraphChanges(PgRepo(pool), new_id),)


def harness_change_sources(pool: ConnectionPool,
                           new_id: Callable[[str], str]) -> "tuple[ChangeSource, ...]":
    """The harness pack's changes (track H, ids "h_…", `harness.change_source`); their teaching-spec
    ops are undone through the teaching-spec registry (`wikiapp.spec_store`)."""
    from evowiki.harness.change_source import HarnessChanges
    from evowiki.wikiapp.repo import PgRepo
    from evowiki.wikiapp.spec_store import RepoSpecRegistry
    return (HarnessChanges(pool, RepoSpecRegistry(PgRepo(pool), new_id), new_id),)


class DurableRuntime:
    """The one DBOS launch point of the service. `start(deps)`: `page_flows.install(deps)`, then
    `dbos_app.launch` (install first: recovery runs at launch and needs the deps), then the sweep, then each
    `after_launch` hook in order (queues / schedules of other subsystems need a launched DBOS;
    workflows are registered by decorator at import). A hook that raises fails startup: every
    `on_failed_start` step runs (newest first), then DBOS is stopped, then the error re-raised. `stop()`
    shuts DBOS down. See FINDINGS.md (B1b) for the call order and the workflow-id prefixes."""

    def __init__(self, database_url: str, executor_id: str, app_version: str = dbos_app.APP_VERSION,
                 queues: Sequence[str] | None = None, schedules: bool = False):
        self.database_url = database_url
        self.executor_id = executor_id
        self.app_version = app_version
        self.queues = queues            # None: dequeue every stored queue (`dbos_app.launch`)
        # Helpers default to no stored schedules. The service enables recovery schedules for
        # memory and corrections even with the Bot off; its queue admission is separate.
        self.schedules = schedules
        self.after_launch: list[Callable[[], None]] = []
        # undo steps of a failed start, run newest first BEFORE DBOS is shut down (an install made
        # outside `start`, e.g. the Bot's runtime, is undone here); each one must not raise
        self.on_failed_start: list[Callable[[], None]] = []
        # the source-import deps (`sources.flows.install`, before launch: recovery needs them);
        # None: imports recovered at launch fail closed (`flows.installed` raises)
        self.sources: ImportDeps | None = None
        # the supplement-match deps (`sources.match_flow.install`, before launch, like `sources`);
        # None: matches recovered at launch fail closed (`match_flow.installed` raises)
        self.matches: MatchDeps | None = None
        # the exam-extraction deps (`sources.exam_flow.install`, before launch, like `sources`);
        # None: extractions recovered at launch fail closed (`exam_flow.installed` raises)
        self.exams: ExamDeps | None = None
        # the graph-group naming deps (`wikiapp.group_naming.install`, before launch, like
        # `sources`); None: no naming queue and no graph listener (graph changes name nothing)
        self.groups: NamingDeps | None = None
        # a delete a background wrap-up held up runs again when that wrap-up ends (user id)
        self.forget_retry: Callable[[str], None] | None = None
        self.rsi_services: Services | None = None

    def start(self, deps: "PageDeps", *, sweep: Callable[[], object] | None = None,
              assistant: "AssistantService | None" = None) -> None:
        """`sweep` (the runner's) runs right after launch, before the hooks: it closes page-run
        rows whose workflow can never finish. `assistant` (when its turns run on DBOS) is installed
        with the page deps, before launch: recovery of a pending turn runs over its deps."""
        from evowiki.assistant import turn_runs, wrapup  # register the turn / wrap-up workflows
        from evowiki.bot import (
            durable as bot_durable,  # noqa: F401 - retained Bot schedules may still fire
        )
        from evowiki.bot import practice, section_visual  # noqa: F401 - registers draft workflows
        from evowiki.service import (
            retention_job,  # noqa: F401 - registers the retention workflow
            rsi_feedback,
        )
        from evowiki.sources import exam_flow, match_flow  # register exam / supplement workflows
        from evowiki.sources import flows as source_flows  # registers the import workflow
        from evowiki.wikiapp import group_naming, page_flows  # group naming's workflow too
        try:
            if self.rsi_services is not None:
                rsi_feedback.install(self.rsi_services)
            page_flows.install(deps)
            if self.sources is not None:
                source_flows.install(self.sources)
            if self.matches is not None:
                match_flow.install(self.matches)
            if self.exams is not None:
                exam_flow.install(self.exams)
            if self.groups is not None:
                group_naming.install(self.groups)
            if assistant is not None:
                turn_runs.install(assistant)
                wrapup.install(lambda: assistant.turn_deps, self.forget_retry)
            dbos_app.launch(self.database_url, app_version=self.app_version,
                            executor_id=self.executor_id, queues=self.queues,
                            schedules=self.schedules)
            if sweep is not None:
                sweep()
            if assistant is not None:
                turn_runs.sweep_foreign_turns()
            if assistant is not None:
                wrapup.register()
            if self.groups is not None:
                group_naming.register()
            if self.rsi_services is not None:
                rsi_feedback.register()
            for hook in self.after_launch:
                hook()
        except BaseException:      # fail closed: a failed startup leaves nothing launched / installed
            for undo in reversed(self.on_failed_start):
                try:
                    undo()
                except Exception:  # keep the cause; the other undo steps still run
                    log.warning("an undo step after a failed durable start failed", exc_info=True)
            try:
                self.stop()
            except Exception:      # (launch may have failed before DBOS existed) keep the cause
                log.warning("cleanup after a failed durable start failed", exc_info=True)
            raise

    def stop(self) -> None:
        from evowiki.assistant import turn_runs, wrapup
        from evowiki.service import rsi_feedback
        from evowiki.sources import exam_flow, match_flow
        from evowiki.sources import flows as source_flows
        from evowiki.wikiapp import group_naming, page_flows
        try:
            rsi_feedback.uninstall()
            group_naming.uninstall()    # no graph change enqueues on a DBOS that is going away
            wrapup.uninstall()
            dbos_app.shutdown()
        finally:
            try:
                page_flows.uninstall()
            finally:
                try:
                    turn_runs.uninstall()
                finally:
                    try:
                        source_flows.uninstall()
                    finally:
                        try:
                            match_flow.uninstall()
                        finally:
                            try:
                                exam_flow.uninstall()
                            finally:
                                self._close_readers()

    def _close_readers(self) -> None:
        """Stops the import's page readers that hold a process (the LayoutWorker, which serves
        as both the local reader and the layout parser, is closed once)."""
        if self.sources is None:
            return
        readers: list[Any] = []
        for reader in (self.sources.local, self.sources.claude,
                       getattr(self.sources, "layout", None)):
            if reader is not None and not any(reader is r for r in readers):
                readers.append(reader)
        for reader in readers:
            close = getattr(reader, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:   # shutdown goes on; the worker dies with the process
                    log.warning("closing the %s page reader failed",
                                getattr(reader, "name", type(reader).__name__), exc_info=True)


def page_deps(svc: Services) -> "PageDeps":
    """What the page workflows need from the service (`page_flows.install`; recovery uses it too).
    The writing kit is fixed here, at launch; the gateway is read live."""
    from evowiki.sources.reader import PooledSourceReader
    from evowiki.wikiapp.llm import require
    from evowiki.wikiapp.page_flows import PageDeps
    from evowiki.wikiapp.repo import PgRepo

    return PageDeps(
        repo=PgRepo(svc.pool),
        gateway=lambda uid: require(svc.gateway, uid) if svc.gateway is not None else None,
        writing=svc.writing(), store=svc.runs.store, clock=svc.clock, new_id=svc.new_id,
        deep=svc.deep,
        on_write=svc.write_queue.after_write if svc.write_queue is not None else None,
        after=svc.write_queue.kick if svc.write_queue is not None else None,
        sources=PooledSourceReader(svc.pool, svc.embedder), ledger=svc.actions)


def import_deps(svc: Services, *, local: "VisionReader | None" = None,
                claude: "VisionReader | None" = None,
                layout: "LayoutParser | None" = None) -> "ImportDeps":
    """What a source import needs from the service; the gateway is read live (None: no LLM,
    the toc falls back to page groups and summaries are skipped with a note). `local` / `claude`
    read pages from their images (None: NoVision; `build_services` passes the LayoutWorker and
    ClaudePages when they are configured); `layout` parses whole PDFs (None: no layout step). The assistant sees images when its turns are routed
    to Anthropic for that user. With a durable runtime a finished import starts the supplement
    match (`match_flow.start_match`) and the exam extraction (`exam_flow.start_exam`), each on
    its own (one failing to start never keeps the other from starting)."""
    from evowiki.assistant.loop import PURPOSE as ASSISTANT_PURPOSE
    from evowiki.llm.usage import Account
    from evowiki.sources.exam_flow import start_exam
    from evowiki.sources.importer import ImportDeps
    from evowiki.sources.match_flow import start_match
    from evowiki.sources.vision import NoVision

    def assistant_sees_images(uid: str) -> bool:
        gw = svc.gateway
        if gw is None:
            return False
        account = Account(user_id=uid, group="assistant") if gw.metered else None
        ref = gw.route_ref("generator", account, ASSISTANT_PURPOSE)
        spec = svc.models.spec(f"{ref.provider}/{ref.model}") if svc.models else None
        return spec.vision if spec else supports_images(ref.provider, ref.model)

    def on_ready(uid: str, sid: str) -> None:
        for name, hook in (("match", start_match), ("exam", start_exam)):
            try:
                hook(uid, sid)
            except Exception:      # both never raise; kept apart all the same
                log.exception("source %s/%s: the %s start after the import failed", uid, sid,
                              name)

    def on_text_changed(uid: str, sid: str, changed: list[str]) -> None:
        from evowiki.wikiapp.reading import reanchor_readings
        from evowiki.wikiapp.repo import PgRepo
        reanchor_readings(PgRepo(svc.pool), svc.pool, uid, sid, changed, now=svc.clock(),
                          new_id=svc.new_id)

    return ImportDeps(pool=svc.pool, embedder=svc.embedder, llm_for=_sources_llm(svc),
                      local=local or NoVision(), claude=claude or NoVision(),
                      assistant_sees_images=assistant_sees_images, clock=svc.clock,
                      new_id=lambda: uuid.uuid4().hex,
                      on_ready=on_ready if svc.durable is not None else None,
                      on_text_changed=on_text_changed, layout=layout)


def _sources_llm(svc: Services) -> Callable[[str], Any]:
    """user_id -> the materials' JsonLLM over the live gateway (None: no LLM)."""
    from evowiki.wikiapp.llm import GatewayJsonLLM, require

    def llm_for(uid: str):
        gw = svc.gateway
        return None if gw is None else GatewayJsonLLM(require(gw, uid), subject=f"sources:{uid}")
    return llm_for


def match_deps(svc: Services) -> "MatchDeps":
    """What the supplement match needs (`match_flow.install`); the gateway and the Bot's wake-up
    (`svc.bot_notify`, on once the Bot is registered) are read live."""
    from evowiki.sources.match_flow import MatchDeps
    from evowiki.wikiapp.repo import PgRepo

    def notify(uid: str) -> None:
        if svc.bot_notify is not None:
            svc.bot_notify(uid)

    return MatchDeps(pool=svc.pool, repo=PgRepo(svc.pool), embedder=svc.embedder,
                     llm_for=_sources_llm(svc), clock=svc.clock, notify=notify)


def exam_deps(svc: Services, *, claude: "VisionReader | None" = None) -> "ExamDeps":
    """What the exam extraction needs (`exam_flow.install`); its own fast-tier JsonLLM field
    (an end-to-end backend scripts only it), the gateway read live. `claude`: the import's
    Claude page reader (None: NoVision), which re-reads pages whose items the model could not
    trust."""
    from evowiki.sources.exam_flow import ExamDeps
    from evowiki.sources.vision import NoVision
    from evowiki.wikiapp.repo import PgRepo

    return ExamDeps(pool=svc.pool, repo=PgRepo(svc.pool), embedder=svc.embedder,
                    llm_for=_sources_llm(svc), clock=svc.clock, new_id=svc.new_id,
                    claude=claude or NoVision())


def naming_deps(svc: Services) -> "NamingDeps":
    """What the background group naming needs (`group_naming.install`); the gateway is read live
    (None: no LLM, a naming run logs the failure and the groups keep their fallback names)."""
    from evowiki.wikiapp.group_naming import NamingDeps
    from evowiki.wikiapp.llm import require
    from evowiki.wikiapp.repo import PgRepo

    return NamingDeps(
        repo=PgRepo(svc.pool),
        gateway=lambda uid: require(svc.gateway, uid) if svc.gateway is not None else None,
        clock=svc.clock, new_id=svc.new_id)


def build_eyes_gateway(env: Mapping[str, str], pool: ConnectionPool, meter: UsageMeter,
                       prices: PriceTable, providers: Mapping[str, Any],
                       clock: Clock, profile: Callable[[str, str], str] | None = None
                       ) -> Gateway | None:
    """The gateway that reads page images with Claude (EVOWIKI_VISION_MODEL, Q5): the runtime's
    Anthropic provider (whether or not the main generator is Anthropic), metered to the user's
    account without tiers (as the deep tier is), every call logged. None without an Anthropic
    key. An unpriced vision model fails startup (`check_prices`)."""
    from evowiki.llm.models import ModelRef
    from evowiki.sources.claude_pages import vision_model

    anthropic = providers.get("anthropic")
    if anthropic is None:
        return None
    ref = ModelRef(provider="anthropic", model=vision_model(dict(env)))
    config = GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True)
    check_prices([config], prices)
    return Gateway(config, PgCallLog(pool), mode="record", providers={"anthropic": anthropic},
                   attempts=PgAttemptLog(pool), clock=clock, record_max_attempts=1,
                   usage=UsageControl(meter, prices), profile=profile)


def build_page_readers(env: Mapping[str, str], pool: ConnectionPool, meter: UsageMeter,
                       prices: PriceTable, providers: Mapping[str, Any],
                       clock: Clock
                       ) -> "tuple[VisionReader, VisionReader, LayoutParser | None]":
    """(local, claude, layout) of the import: one LayoutWorker (MinerU / Docling) serves as both
    the local page reader and the layout parser when EVOWIKI_LAYOUT_PYTHON is set (NoVision and
    None otherwise); ClaudePages over `build_eyes_gateway` when there is an Anthropic key,
    NoVision otherwise."""
    from evowiki.sources.claude_pages import ClaudePages
    from evowiki.sources.layout_client import ENV_BACKEND, ENV_PYTHON, LayoutWorker
    from evowiki.sources.vision import NoVision

    worker = (LayoutWorker(env[ENV_PYTHON], backend=env.get(ENV_BACKEND) or None)
              if env.get(ENV_PYTHON) else None)
    local: VisionReader = worker if worker is not None else NoVision()
    eyes = build_eyes_gateway(env, pool, meter, prices, providers, clock)
    # ClaudePages meters each import to its user (`for_user`); without a user nothing is metered
    claude: VisionReader = ClaudePages(eyes, lambda: None) if eyes is not None else NoVision()
    return local, claude, worker


class _NoForgeModel:
    """The forge's gateway when no LLM provider is configured: `request_tool` answers
    「现在不能生成新工具」 (ForgeMisconfigured), never a half-working call."""

    def converse(self, role: Any, request: Any, *, account: Any = None) -> Any:
        from evowiki.forge.models import ForgeMisconfigured
        raise ForgeMisconfigured("no LLM provider configured for the forge")


def build_forge(pool: ConnectionPool, gateway: Gateway | None, embedder: Embedder,
                clock: Clock) -> "ForgeRuntime":
    """The service-wide forged-tool runtime (coding-agent RSI phase 2, plan T11). `gateway`: the
    good DeepSeek tier, metered per account and never tier-routed (None: no provider -> request_tool
    is unavailable; promoted tools still run). The WASI sandbox is lazy: without the runtime the
    service starts, a forged tool answers 「这个工具现在不能运行」 and request_tool
    「现在不能生成新工具」. Built even when the kill switch is off: the switch is read per run
    (`forge.pin`), so the page / turn workflows have the same steps either way and the tool lists
    are exactly the built-ins while it is off."""
    from evowiki.forge.registry import _LazyWasi
    from evowiki.forge.run import ForgeRuntime
    return ForgeRuntime(pool=pool, gateway=gateway if gateway is not None else _NoForgeModel(),
                        sandbox=_LazyWasi(), embedder=embedder, trace=PgTraceSink(pool, clock),
                        clock=clock)


class ForgeToolFactory:
    """The assistant's tool factory with the turn's forged tools after the built-ins (plan T11):
    one `ForgeRuntime.for_run` per turn (the pin is a recorded step of the turn workflow), host
    calls of forged tools run the turn's own read tools with the session's context. The turn
    passes its harness pack (`with_pack`, read once per turn by the loop). A turn with nothing to
    offer (forge off, nothing opted in) gets exactly the base factory's tools."""

    def __init__(self, base: Callable[..., Sequence[Any]], runtime: "ForgeRuntime"):
        self.base, self.runtime = base, runtime

    def durable(self) -> "ForgeToolFactory":
        base = self.base.durable() if hasattr(self.base, "durable") else self.base
        return ForgeToolFactory(base, self.runtime)

    def __call__(self, info: Any, started: Any) -> Sequence[Any]:
        """Without a pack (callers outside a turn): the default pack's opt-in only."""
        from evowiki.harness.pack import HarnessPack
        return self.with_pack(info, started, HarnessPack(0, {}))

    def with_pack(self, info: Any, started: Any, pack: Any,
                  charge: Callable[[float | None], None] | None = None) -> Sequence[Any]:
        """`charge`: told what each request_tool build cost (the turn adds it to `spent_usd`)."""
        from evowiki.agent.tools import ToolContext
        base = tuple(self.base(info, started))
        run = self.runtime.for_run(
            user_id=info.user_id, agent="assistant",
            run_id=f"asst:{info.session_id}:{started.isoformat()}", group="assistant",
            pack_values=pack.values, session_id=info.session_id, on_cost=charge)
        if run is None:
            return base
        ctx = ToolContext(user_id=info.user_id, session_id=info.session_id, page_id=info.page_id,
                          note_concept_id=info.note_concept_id)
        return (*base, *run.tools(base, ctx))


def forge_factory(base: Callable[..., Sequence[Any]], runtime: "ForgeRuntime | None") -> Any:
    """`base` wrapped with the forge (`ForgeToolFactory`); `base` itself without a runtime."""
    return base if runtime is None else ForgeToolFactory(base, runtime)


def durable_turns(runs: "TurnRuns | None") -> bool:
    from evowiki.assistant.turn_runs import DbosTurnRuns
    return isinstance(runs, DbosTurnRuns)


def build_assistant(pool: ConnectionPool, gateway: Gateway, content: LoadedContent,
                    embedder: Embedder, clock: Clock,
                    new_id: Callable[[str], str] = default_new_id,
                    runs: "TurnRuns | None" = None,
                    ledger: "ActionLedger | None" = None,
                    forget: "ForgetService | None" = None,
                    eyes: Gateway | None = None,
                    import_source: "Callable[[str, ImportRequest], Imported] | None" = None,
                    await_materials: "Callable[..., None] | None" = None,
                    page_reader: "ClaudePages | None" = None,
                    revise: "Callable[[str, str, Sequence[str], str], tuple[ActionRow, str]] | None"
                    = None,
                    practice: "Callable[[str, str, str, str, str], PracticeDraft] | None" = None,
                    illustrate: "Callable[[str, str, str, str, str], Any] | None" = None,
                    forge: "ForgeRuntime | None" = None,
                    profile_sections: "Callable[[str], Mapping[str, Sequence[str]]] | None" = None,
                    vision: Callable[[ModelRef], bool] | None = None
                    ) -> "AssistantService":
    """The reading assistant over Postgres: E1's wiki (PgRepo) for pages, K and evidence, F2's
    tools built per turn, sessions in PgSessionStore. Accepted page edits go through
    `WikiApplier` (a minimal patch on the page, same clock and ids). Metered when the gateway
    has usage control (as in `build_services`): the turn loop and the question observer both
    charge Account(user_id, "assistant"); a spent budget ends the turn with `budget_exceeded`
    and makes the observer skip (logged only). With a `ledger`, proposals are mirrored into it and
    decided through it; the ledger is built first (no executors yet) and the assistant's executor
    is registered on it here, which avoids a construction cycle.
    Chat attachments are always wired (a turn never runs with them missing): the materials are
    read from Postgres (`PgAttachmentReader`), the turn's model sees images when the gateway
    routes this account's assistant turns to Anthropic (its tier), and `eyes` (a Claude gateway,
    `build_eyes_gateway`; None: no Anthropic key, or the main model already sees) takes the
    turns with pictures the main model cannot get. `import_source` / `revise` back the
    assistant's add_source / revise_page (`assistant_import`, `assistant_revise`; None: those
    tools refuse). `forge`: the turn's forged tools after the built-ins (`ForgeToolFactory`);
    None = the built-ins only. Cross-session memory is always wired: `MemoryPorts` over
    PgMemoryStore, the same `embedder` as the materials search and the session store; it is
    `TurnDeps.memory` (index / recall / consolidation, durable steps through
    `durable_turn_deps`; the K read of `known_concepts` runs inside the recall step) and the
    turn's memory tools. Consolidation checks a preference with the rule contract's guard
    (`AutoRuleGuard`: `check_auto_rule`, actor system, the judge metered to the user; the wiki
    read fresh, `PgRepo`, not the turn's snapshot), then lands it through S3's
    `wikiapp.preferences.land_preference` (`S3PreferenceLander`: pending until he confirms it). History search labels a preference / fact
    by its authority now (`authority_status` over the wiki state and `profile_sections`, the
    profile's `sections`; None: a ProfileService of its own)."""
    from evowiki.assistant.attachments import PgAttachmentReader
    from evowiki.assistant.citations import PgCitationReader
    from evowiki.assistant.loop import PURPOSE, TurnDeps
    from evowiki.assistant.pg_store import PgSessionStore
    from evowiki.assistant.service import AssistantService
    from evowiki.assistant.tools.actions import ActionDeps
    from evowiki.assistant.wiki import (
        PooledContentSearch,
        WikiApplier,
        WikiData,
        WikiTools,
    )
    from evowiki.forge.registry import _LazyWasi
    from evowiki.memory.lander import AutoRuleGuard, S3PreferenceLander
    from evowiki.memory.ports import MemoryPorts
    from evowiki.memory.search import authority_status
    from evowiki.memory.store import PgMemoryStore
    from evowiki.sources.reader import PooledSourceReader
    from evowiki.teach.rule_judge import GatewayRuleJudge
    from evowiki.wikiapp.llm import CountingGateway, require
    from evowiki.wikiapp.preferences import land_preference
    from evowiki.wikiapp.repo import PgRepo, TurnRepo

    # one snapshot per turn for every wiki port below (`TurnRepo`; `TurnDeps.turn_scope` opens it)
    repo = TurnRepo(PgRepo(pool))
    data = WikiData(repo, clock)

    def page_title(user_id: str, page_id: str) -> str | None:
        try:
            view = data.page(user_id, page_id) if page_id else None
        except Exception:
            log.warning("page title lookup failed for %s", page_id, exc_info=True)
            return None
        return view.title if view is not None and view.title else None

    store = PgSessionStore(pool, ledger=ledger, page_title=page_title)
    sources = PooledSourceReader(pool, embedder, page_reader, clock)

    def zone(uid: str) -> str:
        from evowiki.store import bot as bot_store
        with pool.connection() as conn:
            return bot_store.get_settings(conn, uid).timezone
    fresh = PgRepo(pool)
    sections = profile_sections or build_profile(pool, clock).sections

    def authority(uid: str):
        try:
            state = fresh.load(uid).state
        except LookupError:
            state = None
        profile = sections(uid)

        def draft(action_id: str) -> str | None:     # S3's pending card: its ledger row
            from evowiki.store import bot as bot_store
            with pool.connection() as conn:
                row = bot_store.get_action(conn, action_id)
            return row.status if row is not None and row.user_id == uid else None
        return lambda item: (authority_status(item, state, profile, draft)
                             if state is not None or item.kind == "about_me" else "gone")

    def undone(session_id: str, message_id: str) -> list[str]:
        from evowiki.store import bot as bot_store
        ids, turn = [], None
        for e in store.entries(session_id):
            if e.type == "user":
                turn = e.payload.get("message_id")
            elif e.type == "assistant" and turn == message_id:
                ids += e.payload.get("actions") or []
        if not ids:
            return []
        with pool.connection() as conn:
            rows = [bot_store.get_action(conn, i) for i in ids]
        return [r.summary for r in rows if r is not None and r.status == "undone"]

    memory = MemoryPorts(store=PgMemoryStore(pool), embedder=embedder, sessions=store,
                         timezone=zone, clock=clock, known_concepts=data.known_concepts,
                         lander=S3PreferenceLander(land_preference, fresh, clock, new_id),
                         guard=AutoRuleGuard(fresh, lambda uid: GatewayRuleJudge(
                             CountingGateway(gateway, uid), subject=uid)),
                         authority=authority, undone=undone, new_id=new_id,
                         conflicts=build_conflicts(pool, clock, new_id))
    from evowiki.memory.anchors import AnchorMemory
    def memory_background_allowed(uid: str) -> bool:
        from evowiki.bot.settings import effective_paused
        from evowiki.store import bot as bot_store
        with pool.connection() as conn:
            return not effective_paused(bot_store.get_settings(conn, uid), clock())
    memory = replace(memory, anchors=AnchorMemory(pool, store, memory.store, repo=repo),
                     background_allowed=memory_background_allowed)
    actions = None
    if ledger is not None:       # the read / action tools need the ledger (and its executors)
        def counting(uid: str):
            return require(gateway, uid)
        from evowiki.profile.note import owned_lines
        from evowiki.profile.store import PgProfileStore
        from evowiki.service.rsi_feedback import notify as feedback_notify
        actions = ActionDeps(repo=repo, ledger=ledger, clock=clock, new_id=new_id,
                             gateway=counting, import_source=import_source, revise=revise,
                             practice=practice, illustrate=illustrate, sources=sources, timezone=zone,
                             memory_store=memory.store,
                             await_materials=await_materials,
                             feedback_notify=feedback_notify,
                             rule_judge=lambda uid: GatewayRuleJudge(CountingGateway(gateway, uid),
                                                                     subject=uid),
                             profile_lines=lambda uid: owned_lines(PgProfileStore(pool, clock).get(uid)),
                             conflicts=build_conflicts(pool, clock, new_id))
    if forget is not None:       # forget_memory: through a forget request (ledger + audit)
        memory = replace(memory, forget_item=lambda uid, item_id: forget.forget_item(
            uid, item_id, requested_by="assistant:forget_memory"))
    deps = TurnDeps(gateway=gateway, timezone=zone,
                    tools=forge_factory(
                        WikiTools(repo, content=PooledContentSearch(pool, content, embedder),
                                  actions=actions, pool=pool, sources=sources,
                                  sandbox=forge.sandbox if forge is not None else _LazyWasi(),
                                  memory=memory),
                        forge),
                    store=store, data=data,
                    new_id=new_id, clock=clock,
                    trace=PgTraceSink(pool, clock),
                    attachments=PgAttachmentReader(pool),
                    citations=PgCitationReader(pool),
                    sees_images=lambda account: (vision or (lambda ref: supports_images(ref.provider, ref.model)))(
                        gateway.route_ref("generator", account, PURPOSE)),
                    eyes=eyes, turn_scope=repo.turn, ledger=ledger, memory=memory)
    applier = WikiApplier(repo, clock, new_id)
    if ledger is not None:
        from evowiki.assistant.edit_executor import KIND, AssistantEditExecutor
        ledger.register(KIND, AssistantEditExecutor(applier, deps.store, clock, new_id))
    from evowiki.assistant.rating_gaps import RatingGaps
    if durable_turns(runs):
        from evowiki.assistant.wrapup import DbosWrapups
        plain = deps
        deps = replace(deps, wrapups=DbosWrapups(lambda: plain))
    return AssistantService(deps, applier, runs, ledger=ledger,
                            rating_gaps=RatingGaps(repo, lambda uid: CountingGateway(gateway, uid),
                                                   clock, new_id))


def user_model(pool: ConnectionPool, configs: Mapping[str, Any],
               default: str) -> Callable[[str], str | None]:
    """user id -> the model their calls use: their pick when it routes, else `default`."""
    from evowiki.llm.usage_control import PICK_KEY
    from evowiki.runtime.models import ModelChoice
    from evowiki.store.usage import chosen_models

    choice = ModelChoice(configs, default)

    def pick(user_id: str) -> str | None:
        with pool.connection() as conn:
            return choice.current(chosen_models(conn, user_id).get(PICK_KEY))
    return pick


def user_reasoning(pool: ConnectionPool) -> Callable[[str], str | None]:
    from evowiki.store.usage import chosen_models

    def read(user_id: str) -> str | None:
        with pool.connection() as conn:
            return chosen_models(conn, user_id).get("reasoning")
    return read


def build_deep(pool: ConnectionPool, config: GatewayConfig, providers: Mapping[str, Any],
               meter: UsageMeter, prices: PriceTable, clock: Clock,
               new_id: Callable[[str], str] = default_new_id,
               library: "ComponentLibrary | None" = None,
               fallback: GatewayConfig | None = None,
               harness: "HarnessSource | None" = None,
               embedder: Embedder | None = None,
               tiers: Mapping[str, GatewayConfig] | None = None,
               tier_providers: Mapping[str, Any] | None = None,
               forge: "ForgeRuntime | None" = None,
               pick: Callable[[str], str | None] | None = None,
               writers: Mapping[str, GatewayConfig] | None = None,
               model_tiers: Mapping[str, Mapping[str, GatewayConfig]] | None = None,
               profile: Callable[[str, str], str] | None = None,
               visual: GatewayConfig | None = None,
               reasoning: Callable[[str], str | None] | None = None) -> "DeepService":
    """Deep refine over Postgres: E1's wiki (PgRepo), jobs and assets (migration 0022), the default
    user's teaching spec (R1, `WikiSpecSource`), headless Chromium for the render check.
    Without every provider the deep tier needs, jobs fail with `llm_unavailable` (fail closed).
    `tiers`: the "fast" / "good" configs the pack's step tiers and hook steps may pick, routed
    through `tier_providers` (the runtime's) next to the deep ones (a deep provider of the same
    name wins); a tier whose provider is missing is left out, so a pack picking it fails closed.
    `pick` / `writers` / `model_tiers`: the user's model (the composer picker, D27) picks their
    writer and step tiers (`DeepLLM`); a writer whose provider is missing is left out (that user
    then gets `config`)."""
    from evowiki.deep.llm import DeepLLM
    from evowiki.deep.render import PlaywrightChecker
    from evowiki.deep.store import PgAssetStore, PgJobStore
    from evowiki.wikiapp.refine_service import DeepService
    from evowiki.wikiapp.refine_spec import WikiSpecSource
    from evowiki.wikiapp.repo import PgRepo

    llm = None
    own_writers = {m: c for m, c in (writers or {}).items()
                   if not provider_config_problem(c, providers)}
    problem = provider_config_problem(config, providers)
    if problem is not None and not own_writers:
        log.warning("deep refine disabled: %s", problem)
    else:
        if fallback is not None and (why := provider_config_problem(fallback, providers)):
            log.warning("writer fallback disabled: %s", why)
            fallback = None
        usable: dict[str, GatewayConfig] | None = None
        if tiers is not None:
            providers = {**(tier_providers or {}), **providers}
            usable = {}
            for name, cfg in tiers.items():
                if why := provider_config_problem(cfg, providers):
                    log.warning("deep tier %s disabled: %s", name, why)
                else:
                    usable[name] = cfg
        llm = DeepLLM(config=config, providers=providers, log=PgCallLog(pool), prices=prices,
                      meter=meter, attempts=PgAttemptLog(pool), clock=clock, fallback=fallback,
                      harness=harness, trace=PgTraceSink(pool, clock),
                      requests=PgRequestLog(pool), tiers=usable, pick=pick,
                      writers=own_writers or None, model_tiers=model_tiers, profile=profile,
                      visual=visual if visual is not None and not provider_config_problem(visual, providers) else None,
                      reasoning=reasoning)
    repo = PgRepo(pool)
    # the page writer reads the material sections its page maps to (materials Task 6); the
    # reader's search needs the embedder (without one: no material tools, sections still given)
    from evowiki.deep.images import HttpxHttp, OpenImages
    from evowiki.sources.excerpts import PooledSourceExcerpts
    from evowiki.sources.reader import PooledSourceReader
    return DeepService(repo=repo, jobs=PgJobStore(pool), assets=PgAssetStore(pool),
                       specs=WikiSpecSource(repo), checker=PlaywrightChecker(), llm=llm,
                       clock=clock, new_id=new_id, library=library,
                       sources=PooledSourceExcerpts(pool),
                       reader=PooledSourceReader(pool, embedder) if embedder is not None else None,
                       forge=forge, images=OpenImages(HttpxHttp()))


def build_profile(pool: ConnectionPool, clock: Clock) -> "ProfileService":
    """The learner profile over Postgres; its 学到哪了 reads the pages' write runs as GET
    /wiki/progress does."""
    from evowiki.profile.service import ProfileService
    from evowiki.profile.store import PgProfileStore
    from evowiki.wikiapp import progress
    from evowiki.wikiapp.page_run_store import PgPageRunStore
    from evowiki.wikiapp.repo import PgRepo
    runs = PgPageRunStore(pool)
    return ProfileService(PgRepo(pool), PgProfileStore(pool, clock), clock=clock,
                          new_id=default_new_id,
                          runs=lambda uid, state: runs.latest_of(uid, progress.page_ids(state)))


def build_conflicts(pool: ConnectionPool, clock: Clock,
                    new_id: Callable[[str], str] = default_new_id) -> "ConflictService":
    from evowiki.memory.conflicts import ConflictService
    from evowiki.profile.store import PgProfileStore
    from evowiki.wikiapp.repo import PgRepo
    return ConflictService(pool, PgRepo(pool), PgProfileStore(pool, clock), clock=clock,
                           new_id=new_id)


def build_forget(env: Mapping[str, str], pool: ConnectionPool,
                 clock: Clock) -> "tuple[Ledger, ForgetService]":
    """The delete flow: RSI's `forget_session` and S3's preference steps, the ledger at
    `EVOWIKI_FORGET_LEDGER` (default `.evowiki-run/forget-ledger.jsonl`)."""
    from evowiki.memory.forget import ForgetService, RsiHarnessForget, S3SpecForget
    from evowiki.memory.tombstone import Ledger, ledger_path
    from evowiki.profile.store import PgProfileStore
    from evowiki.wikiapp.repo import PgRepo
    ledger = Ledger(ledger_path(env))
    spec = S3SpecForget(PgRepo(pool), PgProfileStore(pool, clock), pool, default_new_id)
    return ledger, ForgetService(pool, ledger, harness=RsiHarnessForget(), spec=spec,
                                 clock=clock, new_id=default_new_id)


def build_services(env: Mapping[str, str] = os.environ, *, clock: Clock | None = None) -> Services:
    clock = clock or SystemClock()
    health_since = clock()
    config = runtime_gateway_config(env)  # rejects a bad EVOWIKI_RUNTIME_PROVIDER before I/O
    # Fast / good tiers route when the resolved provider is DeepSeek (auto or explicit);
    # in anthropic mode every account keeps the one runtime config (behaviour unchanged).
    tiers = runtime_tier_configs(env) if config.generator.provider == "deepseek" else None
    from evowiki.runtime.models import (
        ModelChoice,
        default_model,
        runtime_model_configs,
    )
    model_configs = runtime_model_configs(env)     # the assistant composer's picker
    deep_config = runtime_deep_config(env)
    deep_fallback = runtime_deep_fallback_config(env)
    from evowiki.runtime.models import runtime_writer_configs
    writer_configs = runtime_writer_configs(env)          # the writer of each pickable model
    prices = load_prices()
    if env.get("EVOWIKI_PRICES_FILE"):
        prices = prices.overlay(load_prices(Path(env["EVOWIKI_PRICES_FILE"])))
    catalog = model_specs(env)
    unavailable = {spec.id: "pricing_missing"
                   for spec in catalog if spec.id in model_configs
                   and prices.price(spec.provider, spec.model) is None}
    model_configs = {mid: cfg for mid, cfg in model_configs.items() if mid not in unavailable}
    writer_configs = {mid: cfg for mid, cfg in writer_configs.items() if mid not in unavailable}
    tz = usage_tz(env)  # a bad EVOWIKI_USAGE_TZ fails before the pool
    pool = make_pool(database_url(env))
    try:
        with pool.connection() as conn:
            content = initialize_content(conn, clock())
        ctx = prepare_runtime(pool, content, PolicyBundle.default(), clock())
        embedder = embedder_from_env(env)
        if isinstance(embedder, FastEmbedEmbedder) and embedder.on_gpu:
            try:   # a CUDA session that fell back to the CPU: refuse to start (fail closed)
                embedder.check_gpu()
            except Exception:
                log.exception("GPU embedder (EVOWIKI_EMBED_PROVIDERS=cuda) failed to load")
                raise
    except BaseException:
        pool.close()
        raise
    from evowiki.bot.actions import ActionLedger
    ledger = ActionLedger(pool, clock, default_new_id, executors={})
    from evowiki.bot.change_source import LedgerChangeSource
    from evowiki.bot.executors import PreferenceExecutor
    from evowiki.forge.change_source import forge_change_sources
    from evowiki.harness.change_source import HarnessForgeFilter
    from evowiki.wikiapp.repo import PgRepo
    ledger.register("preference", PreferenceExecutor(pool, clock))
    from evowiki.bot.path_actions import KIND as PATH_EDIT
    from evowiki.bot.path_actions import PathEditExecutor
    ledger.register(PATH_EDIT, PathEditExecutor(PgRepo(pool), clock, default_new_id))
    from evowiki.bot import prompts
    from evowiki.bot.executors import PromptOverrideExecutor
    ledger.register(prompts.KIND, PromptOverrideExecutor(pool, clock))
    from evowiki.harness.source import reader
    harness = reader(pool)                            # the user's pack: read per marked call
    profile = build_profile(pool, clock)
    from evowiki.bot.executors_profile import (
        FORGET_KIND,
        KNOWN_RETRACT,
        KnownRetractExecutor,
        KnownRetractLog,
        ProfileFactExecutor,
        ProfileForgetExecutor,
    )
    from evowiki.bot.executors_profile import KIND as PROFILE_FACT
    from evowiki.profile.store import PgProfileStore
    ledger.register(PROFILE_FACT, ProfileFactExecutor(PgProfileStore(pool, clock), pool))
    ledger.register(FORGET_KIND, ProfileForgetExecutor(PgProfileStore(pool, clock), pool))
    ledger.register(KNOWN_RETRACT, KnownRetractExecutor(PgRepo(pool), PgProfileStore(pool, clock), clock,
                                                        default_new_id))
    from evowiki.memory.forget_fact import KIND as MEMORY_FORGET
    from evowiki.memory.forget_fact import MemoryForgetExecutor
    from evowiki.memory.store import PgMemoryStore as _MemStore
    ledger.register(MEMORY_FORGET, MemoryForgetExecutor(pool, _MemStore(pool), ledger, clock))
    from evowiki.memory.conflicts import RESOLVE_KIND, ConflictResolveExecutor
    ledger.register(RESOLVE_KIND, ConflictResolveExecutor(  # 「保留这条」's undo (memory conflicts)
        pool, PgRepo(pool), PgProfileStore(pool, clock), clock=clock, new_id=default_new_id))
    providers = runtime_providers(env)
    for mid, configs in model_configs.items():
        if any(provider_config_problem(cfg, providers) for cfg in configs.values()):
            unavailable[mid] = "provider_unavailable"
    gateway = None
    routable: dict = {}
    forge_gateway = None
    assistant = None
    problem = (provider_config_problem(config, providers)
               or (provider_config_problem(tiers["fast"], providers) if tiers else None))
    routable = {m: c for m, c in model_configs.items()
                if not any(provider_config_problem(t, providers) for t in c.values())}
    if not providers:
        log.warning("no LLM provider configured; ask/note extraction and bridges degrade "
                    "(extraction jobs stay queued). %s", problem)
    elif problem is not None and not routable:
        # Fail closed: no half-working gateway whose calls to the missing provider would fail
        # (and burn extraction attempts). Pages still work; bridges/answers say unavailable.
        log.warning("LLM disabled: %s", problem)
    else:
        if config.single_provider:
            log.warning("single-provider mode — spec §8 downgrade: generator and verifier "
                        "both use %s", config.generator.provider)
        # Usage control: calls that carry an account (the wiki flows) are budget-checked,
        # metered and routed to their tier; calls without one are unchanged.
        meter = PgUsageMeter(pool, clock=clock, tz=tz)
        # An unavailable default remains unavailable. An explicitly selected, priced model
        # can still serve without changing vendors behind the user's back.
        usage = UsageControl(meter, prices, tier_configs=tiers, tiers=meter, clock=clock,
                             model_configs=routable, default_model=default_model(config))
        # Every call and attempt is persisted: evidence rows reference llm_call by FK.
        gateway = Gateway(config, PgCallLog(pool), mode="record", providers=providers,
                          attempts=PgAttemptLog(pool), clock=clock,
                          record_max_attempts=RUNTIME_RECORD_MAX_ATTEMPTS,  # lease budget
                          usage=usage, harness=harness, requests=PgRequestLog(pool),
                          profile=profile.context)
        # the forge's model calls (request_tool): the good tier, metered per account, not routed
        forge_config = tiers["good"] if tiers else config
        forge_gateway = Gateway(forge_config, PgCallLog(pool), mode="record",
                                providers=providers, attempts=PgAttemptLog(pool), clock=clock,
                                record_max_attempts=RUNTIME_RECORD_MAX_ATTEMPTS,
                                usage=UsageControl(meter, prices), requests=PgRequestLog(pool))
        if provider_config_problem(forge_config, providers) or any(prices.price(ref.provider, ref.model) is None
               for ref in (forge_config.generator, forge_config.verifier)):
            forge_gateway = None
    forge = build_forge(pool, forge_gateway, embedder, clock)
    forget_ledger, forget = build_forget(env, pool, clock)
    local, claude, layout = build_page_readers(env, pool, PgUsageMeter(pool, clock=clock, tz=tz),
                                               prices, providers, clock)
    if gateway is not None:
        from evowiki.assistant.turn_runs import DbosTurnRuns
        from evowiki.sources.claude_pages import ClaudePages
        # pictures the main model cannot get go to Claude (same meter, the user's assistant
        # account); not needed when the main generator is Anthropic already
        eyes = (None if config.generator.provider == "anthropic"
                else build_eyes_gateway(env, pool, meter, prices, providers, clock,
                                        profile=profile.context))
        # add_source / revise_page go through the services, built below (read at call time)
        assistant = build_assistant(
            pool, gateway, ctx.content, embedder, clock, runs=DbosTurnRuns(),  # turns: DBOS
            ledger=ledger, eyes=eyes, forge=forge, forget=forget,
            import_source=lambda uid, req: assistant_import(svc, uid, req),
            await_materials=lambda uid, ids, parse, say: assistant_await_materials(
                svc, layout, uid, ids, parse, say),
            page_reader=claude if isinstance(claude, ClaudePages) else None,
            revise=lambda uid, pid, sids, key: assistant_revise(svc, uid, pid, sids, key),
            practice=lambda uid, pid, kind, covers, key: assistant_practice(svc, uid, pid, kind,
                                                                            covers, key),
            illustrate=lambda uid, pid, sid, request, key: assistant_illustrate(
                svc, uid, pid, sid, request, key),
            profile_sections=profile.sections,
            vision=lambda ref: supports_images(ref.provider, ref.model, env))
    from evowiki.deep.store import PgLibrary
    library = PgLibrary(pool)
    deep = build_deep(pool, deep_config, runtime_deep_providers(env),
                      PgUsageMeter(pool, clock=clock, tz=tz), prices, clock, library=library,
                      fallback=deep_fallback, harness=harness, embedder=embedder,
                      # phase 3a H10 / hooks: the runtime tiers (DeepSeek mode), else the one
                      # runtime config for both (anthropic mode: a fast / good step or hook tier
                      # routes to that config)
                      tiers=tiers if tiers is not None else {"fast": config, "good": config},
                      tier_providers=providers, forge=forge,
                      # D27: the user's model writes their pages too (and its levels are the
                      # pack's step tiers); Claude falls back to DeepSeek, a DeepSeek pick stays
                      pick=user_model(pool, routable, default_model(config)),
                      reasoning=user_reasoning(pool),
                      writers=writer_configs, model_tiers=routable, profile=profile.context,
                      visual=runtime_visual_config(env))
    # with the Bot off this process must not take the `bot` queue's workflows that a Bot-on
    # process enqueued (they would be skipped here and, under their deterministic ids, never run)
    from evowiki.assistant.wrapup import QUEUE as WRAPUP_QUEUE
    from evowiki.service.rsi_feedback import QUEUE as RSI_FEEDBACK_QUEUE
    from evowiki.sources.runs import ASK_QUEUE, SOURCE_QUEUE, TEXT_QUEUE
    from evowiki.wikiapp.group_naming import QUEUE as GROUPS_QUEUE
    from evowiki.wikiapp.page_run_store import PgPageRunStore
    from evowiki.wikiapp.page_runs import PAGE_QUEUE
    # EVOWIKI_DBOS_APP_VERSION (dev only): a live-run server on the shared dev DB takes only the
    # workflows it enqueued itself (DBOS dequeues by application version), never another server's
    durable = DurableRuntime(database_url(env), executor_id=env.get("EVOWIKI_DBOS_EXECUTOR", "web"),
                             app_version=env.get("EVOWIKI_DBOS_APP_VERSION") or dbos_app.APP_VERSION,
                             queues=([PAGE_QUEUE, SOURCE_QUEUE, ASK_QUEUE, TEXT_QUEUE, GROUPS_QUEUE, WRAPUP_QUEUE, RSI_FEEDBACK_QUEUE]
                                     if env.get("EVOWIKI_BOT") == "off" else None),
                             schedules=True)
    if forget is not None:
        durable.forget_retry = forget.retry_user
    if durable.schedules:          # the daily trace retention (0092) rides the same scheduler
        from evowiki.service import retention_job
        from evowiki.store.retention import RetentionPolicy
        retention_job.add_hook(durable, pool, RetentionPolicy.from_env(env))
    profile.known_log = KnownRetractLog(ledger)
    svc = Services(pool, ctx, clock, embedder, gateway,
                    assistant=assistant, deep=deep, library=library,
                    actions=ledger, profile=profile,
                    models=ModelChoice(routable, default_model(config), catalog, unavailable, health_since),
                    change_sources=(*graph_change_sources(pool, default_new_id),
                                    *harness_change_sources(pool, default_new_id),
                                    LedgerChangeSource(ledger, ledger_stale(pool),
                                                       ledger_names(pool)),
                                    # a harness round's tool promotes show as its "h_" row
                                    *(HarnessForgeFilter(src, pool)
                                      for src in forge_change_sources(pool, clock))),
                    runs=DbosPageRuns(PgPageRunStore(pool)), durable=durable,
                    sources=DbosSourceRuns(pool), forget=forget, forget_ledger=forget_ledger,
                    conflicts=build_conflicts(pool, clock))
    durable.sources = import_deps(svc, local=local, claude=claude, layout=layout)
    durable.matches = match_deps(svc)
    durable.exams = exam_deps(svc, claude=claude)
    durable.groups = naming_deps(svc)
    durable.rsi_services = svc

    def sweep_attachments() -> None:
        """Q7 at start-up (after launch: cancelling a run needs DBOS); never fails the start."""
        from evowiki.sources.cleanup import sweep_ask_attachments
        try:
            sweep_ask_attachments(pool, svc.sources, clock())
        except Exception:
            log.exception("the chat attachment cleanup at start-up failed")
    durable.after_launch.append(sweep_attachments)

    def start_unstarted_runs() -> None:
        """Import runs recorded but never started (a crash between the two) start now: the
        user asked for them, nobody has to ask again."""
        from evowiki.sources.uses import start_unstarted
        try:
            start_unstarted(pool, svc.sources)
        except Exception:
            log.exception("starting unstarted import runs at start-up failed")
    durable.after_launch.append(start_unstarted_runs)
    # the user's page runs go through the queue `pages`; a restart recovers the queued ones
    from evowiki.wikiapp.page_runs import register_queue
    durable.after_launch.append(register_queue)
    # source imports go through the queue `source-imports`, one at a time
    from evowiki.sources.runs import register_queue as register_source_queue
    durable.after_launch.append(register_source_queue)
    # rsi_patch / rsi_method rows: mirrors of applied RSI changes, and the changes the user is
    # asked about (teaching_params / rsi = ask), approved / rejected through the user's RSI cycle,
    # which needs the services
    from evowiki.bot.executors import RsiExecutor

    def rsi_cycle_for(uid: str, lock: bool):
        from evowiki.service.rsi_runs import rsi_decider  # rsi_runs imports this module (Services)
        return rsi_decider(svc, uid, lock)
    def rsi_undo(uid: str, ref: str):
        from evowiki.service.rsi_runs import ledger_undo
        return ledger_undo(svc, uid, ref)
    rsi = RsiExecutor(pool, rsi_cycle_for, clock, rsi_undo)
    ledger.register("rsi_patch", rsi)
    ledger.register("rsi_method", rsi)
    from evowiki.wikiapp.repo import PgRepo
    from evowiki.wikiapp.write_queue import PgIdleEnv, WriteQueue
    assistant_service = svc.assistant
    svc.write_queue = WriteQueue(
        svc.runs, lambda: page_deps(svc), PgRepo(pool), clock, default_new_id,
        PgIdleEnv(pool, tz, default_new_id,
                  (lambda uid: assistant_service.user_busy(uid)) if assistant_service else
                  (lambda _uid: False)),
        ledger=ledger, tz=tz,
        can_write=lambda _uid: svc.gateway is not None and (svc.page_writing is not None
                                                            or svc.deep is not None),
        off_peak_writer=lambda _uid: _writer_off_peak(svc))
    _cancel_on_path_changes(svc.write_queue)       # (before the kick: removed rows first)
    _kick_on_path_changes(svc.write_queue)
    register_page_actions(svc, ledger)
    return svc


def _writer_off_peak(svc: "Services") -> bool:
    """The page writer (the deep tier's generator) is billed less off-peak: its model has an
    `off_peak_factor` in the price table (DeepSeek). Then idle writes beyond each path's next page
    wait for the window (spec 2026-10-21-offpeak-writes); a Claude writer never waits."""
    llm = svc.deep.llm if svc.page_writing is None and svc.deep is not None else None
    if llm is None:
        return False
    ref = llm.config.generator
    price = llm.prices.price(ref.provider, ref.model)
    return price is not None and price.off_peak_factor is not None


_path_hook = None
_cancel_hook = None


def _cancel_on_path_changes(queue: "WriteQueue") -> None:
    """A row taken off a path (by the learner, the assistant or the Bot) cancels its page's write."""
    global _cancel_hook
    from evowiki.wikiapp.path_edits import off_path_changed, on_path_changed
    if _cancel_hook is not None:
        off_path_changed(_cancel_hook)
    _cancel_hook = on_path_changed(queue.path_changed)


def _kick_on_path_changes(queue: "WriteQueue") -> None:
    """Every committed path change (the learner's edit, a split, 我会了 / 我想学这个, the
    assistant's and the Bot's path edits all end in `save_paths`) lets the idle Bot pick again."""
    global _path_hook
    from evowiki.wikiapp.path_edits import off_path_changed, on_path_changed
    if _path_hook is not None:
        off_path_changed(_path_hook)

    def hook(user_id, _path_id, _before, _after, _source) -> None:
        queue.kick_soon(user_id)
    _path_hook = on_path_changed(hook)


def _merged(svc: "Services", user_id: str, page_id: str) -> bool:
    """The page is the result of an executed merge (its undo is the merge's own)."""
    from evowiki.store import bot as bot_store
    with svc.pool.connection() as conn:
        return bot_store.merge_making(conn, user_id, page_id) is not None


def register_page_actions(svc: Services, ledger: "ActionLedger") -> None:
    """The assistant's page / path / goal actions (`bot.executors_assistant`) and the Bot's page
    writes (`bot_write`). Body writes go to the user's write queue (`wikiapp.write_queue`: one page
    at a time, the user's own requests first); cancelling discards the page's live runs. A merge's
    originals go to the trash when its merged page is committed."""
    from evowiki.bot.executors_assistant import register_assistant_executors
    from evowiki.bot.executors_write import BotWriteExecutor
    from evowiki.store import bot as bot_store
    from evowiki.wikiapp.llm import require
    from evowiki.wikiapp.repo import PgRepo

    queue = svc.write_queue
    if queue is None:
        raise RuntimeError("register_page_actions needs Services.write_queue")

    def request_write(user_id: str, page_ids: list[str]) -> list[str]:
        return queue.request(user_id, page_ids)

    def cancel_writes(user_id: str, page_ids: list[str]) -> None:
        queue.cancel(user_id, page_ids)

    def gateway(uid):
        return require(svc.gateway, uid) if svc.gateway is not None else None

    practice = practice_writer(svc)
    from evowiki.bot.path_actions import PathEditExecutor as BotPathEditExecutor
    executors = register_assistant_executors(
        ledger, PgRepo(svc.pool), svc.clock, svc.new_id,
        request_write=request_write, cancel_writes=cancel_writes, gateway=gateway,
        pool=svc.pool,
        practice=practice,
        source_runs=lambda: svc.sources,     # read at use: tests set their runner later
        bot_path=BotPathEditExecutor(PgRepo(svc.pool), svc.clock, svc.new_id))  # B5b, same kind
    write_exec = BotWriteExecutor(
        PgRepo(svc.pool), svc.clock, svc.new_id,
        lambda uid, picks: queue.write_batch(uid, picks), queue.cancel, queue.forget,
        merged_by=lambda uid, pid: _merged(svc, uid, pid))
    ledger.register("bot_write", write_exec)
    ledger.register("user_write", write_exec)       # one page written for the learner: same undo
    register_revise(svc, ledger)
    merge = executors["page_merge"]

    def settle_merges(user_id: str, page_id: str) -> None:
        """The merged page was committed: its merge's originals go to the trash."""
        with svc.pool.connection() as conn:
            rows = bot_store.unsettled_merges(conn, user_id, page_id)
        for row in rows:
            merge.settle(user_id, row)
    queue.on_committed(settle_merges)


def register_revise(svc: Services, ledger: "ActionLedger") -> None:
    """`page_revise` (materials spec §7 D): a whole-page draft from materials, swapped in when the
    learner confirms it; its interactives go to the deep tier's asset store (none without one: a
    draft with interactives is then refused)."""
    from evowiki.bot.executors_revise import PageReviseExecutor
    from evowiki.wikiapp.repo import PgRepo
    from evowiki.wikiapp.revise import KIND
    assets = svc.deep.assets if svc.deep is not None else None
    ledger.register(KIND, PageReviseExecutor(PgRepo(svc.pool), assets, svc.clock, svc.new_id))
    from evowiki.bot.executors_hand import HandEditExecutor
    from evowiki.wikiapp.hand_edit import KIND as HAND_EDIT
    ledger.register(HAND_EDIT, HandEditExecutor(PgRepo(svc.pool), svc.clock, svc.new_id))


def assistant_import(svc: Services, user_id: str, req: "ImportRequest") -> "Imported":
    """The assistant's add_source: the `/wiki/sources` import (`routes_sources._import`: stored,
    deduplicated by content, the use recorded, the `ImportJob` started) with the guarded fetcher.
    A refusal is `ImportFailed` (a URL that cannot be read: `fetch=True`). The tool runs inside
    the turn's DBOS step, where no workflow can start: the import runs `durable.outside_dbos`."""
    from fastapi import HTTPException

    from evowiki.agent import durable
    from evowiki.assistant.tools.actions import Imported, ImportFailed
    from evowiki.service.routes_sources import _import  # routes_sources imports this module
    from evowiki.sources.parse import default_fetcher

    fields = {"url": req.url} if req.url else {"text": req.text or ""}
    fields.update({k: v for k, v in (("title", req.title), ("page_id", req.page_id),
                                      ("session_id", req.session_id)) if v})
    try:
        row, _created = durable.outside_dbos(
            lambda: _import(svc, user_id, default_fetcher(), None, fields, req.use,
                            named_by="document"))
    except HTTPException as e:
        raise ImportFailed(str(e.detail), fetch=bool(req.url) and e.status_code == 422) from e
    return Imported(source_id=row.source_id, title=row.title, status=row.status, error=row.error)


def assistant_await_materials(svc: Services, layout: Any, user_id: str,
                              source_ids: "Sequence[str]", parse: bool,
                              say: "Callable[[str], None] | None") -> None:
    """new_goal's wait for its materials (`uses.await_layout`): no parse without a parser. The
    tool runs inside the turn's DBOS step, where no workflow can start."""
    from evowiki.agent import durable
    from evowiki.sources.uses import await_layout

    durable.outside_dbos(lambda: await_layout(
        svc.pool, svc.sources, user_id, source_ids,
        parse=parse and layout is not None and layout.available(), say=say))


def assistant_revise(svc: Services, user_id: str, page_id: str, source_ids: "Sequence[str]",
                     key: str) -> "tuple[ActionRow, str]":
    """The assistant's revise_page: `start_revise` with source "assistant" (raises as it does),
    `durable.outside_dbos` like `assistant_import` (the page run is a workflow)."""
    from evowiki.agent import durable
    from evowiki.wikiapp.repo import PgRepo
    from evowiki.wikiapp.source_revise import start_revise
    ledger = svc.actions
    if ledger is None:
        raise LookupError("没有账本，不能出草稿")
    return durable.outside_dbos(lambda: start_revise(
        ledger=ledger, repo=PgRepo(svc.pool), runs=svc.runs, deps=lambda: page_deps(svc),
        user_id=user_id, page_id=page_id, source_ids=source_ids, source="assistant", key=key,
        now=svc.clock(), origin="open"))


def restart_revise(svc: Services, user_id: str, row: "ActionRow") -> "ActionRow":
    """重试 on a materials draft that no longer fits its page (a whole-page swap is never
    re-anchored: it would overwrite what changed): the old draft is dropped (rejected while it
    waits) and a new one is written from the same materials against the page as it is now (a new
    `page_revise` row, being written). Raises as `start_revise`."""
    from evowiki.agent import durable
    from evowiki.bot.revise_schemas import revise_source_ids
    from evowiki.wikiapp.repo import PgRepo
    from evowiki.wikiapp.source_revise import start_revise
    ledger = svc.actions
    if ledger is None:
        raise LookupError("没有账本，不能出草稿")
    if row.status == "proposed":
        ledger.reject(user_id, row.action_id)
    page_id = str(row.content.get("page_id") or row.page_id or "")
    illustration = row.content.get("illustration")
    if isinstance(illustration, dict):
        from evowiki.store import bot as bot_store
        key = f"section-visual:{page_id}:{svc.new_id('rv')}"
        draft = assistant_illustrate(svc, user_id, page_id, str(illustration["section_id"]),
                                     str(illustration["request"]), key)
        new, created = ledger.propose_unless(
            user_id, lambda conn: bot_store.open_page_revise(conn, user_id, page_id),
            kind="page_revise", scope="page_body", source=row.source, summary=row.summary,
            why=row.why, content={"page_id": page_id, "state": "ready", "confirm": True,
                                  "source_ids": [], "illustration": illustration,
                                  "draft": draft.model_dump(mode="json")},
            key=key, page_id=page_id, page_version=draft.base_version, level="ask")
        if not created:
            from evowiki.bot.actions import ActionRefused
            raise ActionRefused("这一页已经有一份草稿，请先确认或取消")
        return new
    new, _ = durable.outside_dbos(lambda: start_revise(
        ledger=ledger, repo=PgRepo(svc.pool), runs=svc.runs, deps=lambda: page_deps(svc),
        user_id=user_id, page_id=page_id, source_ids=revise_source_ids(row), source=row.source,
        key=f"revise:{page_id}:{svc.new_id('rv')}", now=svc.clock(), origin="open"))
    return new


def practice_writer(svc: Services) -> "DeepPracticeWriter | None":
    """`page_practice`'s writer over the deep tier's asset store, composing with the page writer's
    practice run (`svc.writing()`; on DBOS the page workflows' installed kit); None without the
    deep tier."""
    if svc.deep is None:
        return None
    from evowiki.bot.practice import DeepPracticeWriter
    from evowiki.wikiapp.repo import PgRepo
    return DeepPracticeWriter(PgRepo(svc.pool), svc.writing, svc.deep.assets, svc.clock,
                              svc.new_id, on_dbos=svc.durable is not None)


def assistant_practice(svc: Services, user_id: str, page_id: str, kind: str, covers: str,
                       key: str) -> "PracticeDraft":
    """The assistant's add_practice: the practice composed and render-checked now (the writer's
    practice run, keyed by the tool step's `key`), so the draft shows in the page before the
    learner decides. ActionRefused as `compose` raises it; without the deep tier there is no
    writer, so nothing is composed."""
    from evowiki.bot.actions import ActionRefused
    writer = practice_writer(svc)
    if writer is None:
        raise ActionRefused("加练习的写作还没有接上")
    return writer.compose(user_id, page_id, kind, covers, key)


def assistant_illustrate(svc: Services, user_id: str, page_id: str, section_id: str,
                        request: str, key: str):
    from evowiki.bot.section_visual import VisualJob, compose
    from evowiki.wikiapp.repo import PgRepo
    return compose(VisualJob(user_id=user_id, page_id=page_id, section_id=section_id,
                             request=request, key=key, now=svc.clock()),
                   repo=PgRepo(svc.pool), writing=svc.writing(), on_dbos=svc.durable is not None,
                   new_id=svc.new_id)


def get_services(request: Request) -> Services:
    return request.app.state.services


def current_user(x_evowiki_user: Annotated[str, Header()]) -> str:
    """v1 identity (plan decision 9): the caller names itself; bind the service to localhost."""
    return x_evowiki_user


def check_user(svc: Services, uid: str) -> None:
    """UnknownUser (404) unless `uid` is a registered user."""
    with svc.pool.connection() as conn:
        if get_user(conn, uid) is None:
            raise UnknownUser(f"unknown user {uid}")
