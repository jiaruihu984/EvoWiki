"""The page workflows (spec 2026-10-14 §4, B1b): the body of a page run and its two DBOS entry points.

`execute(job, run_id, emit, deps)` is the body, shared by both runners (`page_runs`): inline it runs
in a virtual workflow with the caller's deps, under DBOS it is `page_workflow`, started with the
job JSON as its only argument and the service's deps from `install`. Everything nondeterministic or
external goes through `agent.durable` steps (repo reads and the commit, the render checker, the
component library, ids, the domain pick; model calls are steps in the gateway), so a recovered
workflow replays to where it stopped and continues; the code between steps only computes from the
job and from step results.

`page_workflow` is registered by the decorator when this module is imported:
import it before `dbos_app.launch` (the service's lifespan does), or DBOS cannot find them at
recovery.

Kinds: `organize` (a page written and committed: the one the user asked for, or the next one of a
path the idle Bot writes ahead), `feedback`, `refine` (deep refine), `rsi_rewrite` /
`reorder_rewrite` (the lazy rewrite of a page an RSI change or a path reorder left out of date),
`patch_rewrite` (the scoped write of the sections a K-change patch left waiting) and
`source_revise` (a whole-page draft from materials written into its `page_revise` ledger row, the
page itself untouched: `wikiapp.source_revise`). An unknown kind raises — fail closed, never a
silent no-op. The runs of one user go one at a time through the `pages` queue (`page_runs`); a run
marks when it began (`run.begin`) and reports its section progress on its row for `GET /wiki/writes`."""

import dataclasses
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from dbos import DBOS
from pydantic import BaseModel

from evowiki.agent import durable
from evowiki.deep.contracts import RefineJob, RefineScope
from evowiki.deep.durable import DurableChecker, DurableImages, DurableLibrary
from evowiki.deep.writer import PageWriter
from evowiki.llm.providers import BUSY_TEXT, TIMEOUT_TEXT
from evowiki.llm.run_context import traced_entry
from evowiki.wikiapp.durable_repo import DurableRepo
from evowiki.wikiapp.events import PageRewriteFailed, WikiEvent
from evowiki.wikiapp.kchange import _feedback_target, _rewrite
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import Writing, write_page
from evowiki.wikiapp.page_run_store import PageRunStore, RunStatus
from evowiki.wikiapp.page_runs import END_EVENTS, STREAM_KEY, PageJob, PageRun, encode_item
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import Commit, Repo, WikiConflict, WikiInvalid
from evowiki.wikiapp.wiki_schemas import ErrorEvent, RewriteDoneEvent

if TYPE_CHECKING:
    from evowiki.bot.actions import ActionLedger
    from evowiki.sources.reader import SourceReader
    from evowiki.wikiapp.refine_service import DeepService

log = logging.getLogger("evowiki.wikiapp.page_flows")


@dataclass(frozen=True)
class PageDeps:
    """What a run needs from the service. `gateway(user_id)`: the user's metered gateway (None: no
    LLM, the run says llm_unavailable). `on_write(job, run_id, status, done)`: what
    follows a finished page run (the Bot's ledger row, a merge's settle, the stale mark); one
    durable step that may not raise into the run. `after(user_id)`: called once the run is over,
    outside its workflow (the write queue's kick: it starts runs, which a workflow body must not).
    `sources` / `ledger`: the materials and the action ledger a `source_revise` run reads and fills
    (None: such a run fails closed); `sleep`: its wait for a material still being read, outside
    DBOS (DBOS sleeps durably)."""

    repo: Repo
    gateway: Callable[[str], CountingGateway | None]
    writing: Writing | None
    store: PageRunStore
    clock: Callable[[], datetime]
    new_id: Callable[[str], str]
    deep: "DeepService | None" = None
    on_write: Callable[[PageJob, str, RunStatus, BaseModel | None], None] | None = None
    after: Callable[[str], None] | None = None
    sources: "SourceReader | None" = None
    ledger: "ActionLedger | None" = None
    sleep: Callable[[float], None] = time.sleep


_deps: PageDeps | None = None


def install(deps: PageDeps) -> None:
    """The service's deps for DBOS runs (and for recovery, which has no caller)."""
    global _deps
    _deps = deps


def uninstall() -> None:
    global _deps
    _deps = None


def installed() -> PageDeps:
    if _deps is None:
        raise RuntimeError("page_flows.install(deps) was not called: no deps for the page workflow")
    return _deps


def durable_writing(w: Writing | None, repo: DurableRepo) -> Writing | None:
    """`w` with its render checker, component library, image search and spec source routed through steps. A
    writer that is not a `PageWriter` (a test double) is kept as it is."""
    if w is None:
        return None
    writer = w.writer
    if isinstance(writer, PageWriter):
        writer = dataclasses.replace(
            writer, checker=DurableChecker(writer.checker),
            library=DurableLibrary(writer.library) if writer.library is not None else None,
            images=DurableImages(writer.images) if writer.images is not None else None)
    specs = WikiSpecSource(repo) if isinstance(w.specs, WikiSpecSource) else w.specs
    return dataclasses.replace(w, writer=writer, specs=specs)


def _write(job: PageJob, run_id: str, emit: Callable[[str, BaseModel], None],
           deps: PageDeps, progress: "_Progress | None" = None) -> float:
    """Writes the page; returns what a run that did not finish spent (USD)."""
    gw = deps.gateway(job.user_id)
    if gw is None:
        emit("error", ErrorEvent(code="llm_unavailable", retryable=False, detail="请切换模型重试"))
        return 0.0
    repo = DurableRepo(deps.repo)
    run = PageRun(run_id, job.user_id, job.page_id, job.origin, emit,
                  on_spend=None if progress is None else progress.spend)
    write_page(run, repo, gw, now=job.now, new_id=durable.ids(deps.new_id),
               writing=durable_writing(deps.writing, repo))
    return run.spent[0]


def _feedback(job: PageJob, run_id: str, emit: Callable[[str, BaseModel], None],
              deps: PageDeps) -> None:
    """A section 「再讲深一点」 / 「换个讲法」 (entry point D): `_rewrite` over the durable repo.
    A page that changed so that the click is invalid now ends the run with that error."""
    if job.feedback is None:
        raise ValueError("a feedback run carries its section and kind")
    gw = deps.gateway(job.user_id)
    if gw is None:
        emit("error", ErrorEvent(code="llm_unavailable", retryable=False, detail="请切换模型重试"))
        return
    repo = DurableRepo(deps.repo)
    run = PageRun(run_id, job.user_id, job.page_id, job.origin, emit)
    fb = job.feedback
    snap = repo.load(job.user_id)
    try:
        rec, s = _feedback_target(snap, job.page_id, fb.section_id)
    except (LookupError, WikiInvalid) as exc:
        # the page changed after the eager check (or went away): the click no longer applies
        emit("error", ErrorEvent(code="conflict", retryable=True,
                                 detail=f"{type(exc).__name__}: {exc}"[:500]))
        return
    _rewrite(run, repo, gw, snap, rec, s.heading, fb.section_id, fb.kind, now=job.now,
             new_id=durable.ids(deps.new_id), writing=durable_writing(deps.writing, repo),
             shown=fb.shown)


FAILURE_TRIES = 3                  # commit attempts of one failed-rewrite record
_REFINE_CODES = {"llm_unavailable": "llm_unavailable", "budget_exceeded": "budget_exceeded",
                 "page_changed": "conflict", "internal": "internal"}


# a harness rewrite's failures count on its page mark (a PageMarkedStale, reason "harness"); its
# own run kind and refine trigger keep it apart from path-order rewrites. A patch write's failures
# count on the page's waiting sections (`WikiState.patch_failures`).
MarkKind = Literal["rsi", "reorder", "patch"]
_MARK_OF: dict[str, MarkKind] = {"rsi_rewrite": "rsi", "reorder_rewrite": "reorder",
                                 "harness_rewrite": "reorder", "patch_rewrite": "patch"}
REWRITE_KINDS = tuple(_MARK_OF)


def _record_rewrite_failure(repo: Repo, uid: str, page_id: str, version: int, failure: str, *,
                            kind: MarkKind = "rsi", sections: tuple[str, ...] = (),
                            now: datetime, new_id: Callable[[str], str]) -> None:
    """Commits one `PageRewriteFailed` (retried on a conflicting commit): the page's mark counts it
    (`wikiapp.stale.rewrite_due`; kind "patch": the waiting `sections` it was scoped to,
    `wikiapp.stale.patch_due`). A record that cannot be written is logged: the page then stays
    due on its next open (fails toward trying again)."""
    for attempt in range(FAILURE_TRIES):
        try:
            snap = repo.load(uid)
            repo.commit(snap, Commit(wiki=[WikiEvent(
                event_id=new_id("wev"), ts=now,
                body=PageRewriteFailed(page_id=page_id, spec_version=version,
                                       failure=failure[:200], kind=kind,
                                       sections=sections if kind == "patch" else ()))]))
            return
        except WikiConflict:
            if attempt == FAILURE_TRIES - 1:
                log.error("failed rsi rewrite of %s/%s not recorded (conflicts)", uid, page_id)
        except Exception:  # broad on purpose: a background run must end with its own result
            log.exception("failed rsi rewrite of %s/%s not recorded", uid, page_id)
            return


def _refine(job: PageJob, run_id: str, emit: Callable[[str, BaseModel], None],
            deps: PageDeps) -> None:
    """Deep refine (entry point E) and the RSI lazy rewrite (F): `DeepService.run` over its durable
    copy; it records the job's stages itself (clients poll GET /refine), the stream only ends with
    `done` / `error`. A lazy rewrite (`REWRITE_KINDS`: rsi / reorder / harness / patch) that fails
    (stage failed, or an exception) is recorded on the page's mark through the durable repo."""
    if job.refine is None:
        raise ValueError("a refine run carries its job")
    deep = deps.deep
    if deep is None:
        emit("error", ErrorEvent(code="llm_unavailable", retryable=False,
                                 detail="请切换模型重试"))
        return
    args = job.refine
    rj = RefineJob.model_validate(args.job)
    scope = RefineScope.model_validate(args.scope) if args.scope else None
    failure: str | None = None
    out: ErrorEvent | None = None
    res = None
    try:
        res = deep.durable_copy().run(job.user_id, rj, scope)
        if res.job.stage == "failed":
            failure = str(res.job.failure or "failed")
            if failure in ("llm_busy", "llm_timeout"):     # the detail is the line shown
                out = ErrorEvent(code=failure, retryable=True,
                                 detail=BUSY_TEXT if failure == "llm_busy" else TIMEOUT_TEXT)
            else:
                out = ErrorEvent(code=_REFINE_CODES.get(failure, "llm_failed"),  # type: ignore[arg-type]
                                 retryable=failure != "budget_exceeded",
                                 detail=(res.job.failure_detail.partition(": ")[2]
                                         if failure == "budget_exceeded"     # stage=write: …
                                         else f"{failure}: {res.job.failure_detail}")[:500])
    except Exception as exc:    # run never raises; this is a crashed step (a replayed one included)
        log.exception("refine %s of %s/%s failed", rj.job_id, job.user_id, job.page_id)
        failure = "error"
        out = ErrorEvent(code="internal", retryable=True,
                         detail=f"{type(exc).__name__}: {exc}"[:500])
    if failure is not None and job.kind in REWRITE_KINDS:
        log.warning("%s rewrite of %s/%s failed (%s)", job.kind.split("_")[0], job.user_id,
                    job.page_id, failure)
        if args.spec_version is None:
            raise ValueError("a lazy rewrite carries the spec version it ran under")
        _record_rewrite_failure(DurableRepo(deps.repo), job.user_id, job.page_id,
                                args.spec_version, failure, kind=_MARK_OF[job.kind],
                                sections=scope.sections if scope is not None else (),
                                now=durable.now(deps.clock),
                                new_id=durable.ids(deps.new_id))
    if out is not None:
        emit("error", out)
    else:
        if res is None:
            raise ValueError("a refine run without an error has a result")
        emit("done", RewriteDoneEvent(page_id=job.page_id, llm_calls=0, cost_usd=res.job.cost_usd,
                                      model=res.job.model))


def _revise(job: PageJob, run_id: str, emit: Callable[[str, BaseModel], None],
            deps: PageDeps) -> float:
    """A whole-page draft from materials into its ledger row (`source_revise.run_revise`);
    returns what a run that did not finish spent (USD)."""
    from evowiki.wikiapp.source_revise import run_revise  # it imports this module

    run = PageRun(run_id, job.user_id, job.page_id, job.origin, emit)
    run_revise(job, run, deps, reader=deps.sources)
    return run.spent[0]


class _Progress:
    """The run's section progress on its row (what `GET /wiki/writes` shows while it writes)."""

    def __init__(self, store: PageRunStore, run_id: str):
        self.store, self.run_id = store, run_id
        self.of: int | None = None
        self.done: set[str] = set()
        self.shown: dict = {}              # the progress as stored (each store replaces the row's)

    def see(self, name: str, payload: BaseModel) -> None:
        if name == "planning":      # no outline yet: the row shows the planning pass
            self._store_progress({"pass": getattr(payload, "pass_", None)})
            return
        if name == "outline":       # the writer writes every section but the checks
            self.of = sum(1 for s in getattr(payload, "sections", ()) if s.role != "check")
            self.done = set()
            self.shown.pop("pass", None)
        elif name == "section_added" and self.of is not None:
            self.of += 1
        elif name == "section_done":
            sid = getattr(payload, "section_id", None)
            if sid is None or sid in self.done:
                return
            self.done.add(sid)
        else:
            return
        self._store_progress({"section": len(self.done), "of": self.of})

    def spend(self, cost: float, outline_calls: int, section_calls: int) -> None:
        """The cost so far and the calls made (organizer side / section writing), kept with the
        section progress: `GET /wiki/writes` shows the cost while the page is written, and the
        run's 为什么？ line splits the calls."""
        last = self.shown
        if (section_calls == last.get("section_calls") and outline_calls == last.get("outline_calls")
                and abs(cost - float(last.get("cost_usd") or 0.0)) < SPEND_STEP_USD):
            return                  # not every text delta is a write to the row
        self._store_progress({"cost_usd": round(cost, 6), "outline_calls": outline_calls,
                              "section_calls": section_calls})

    def _store_progress(self, progress: dict) -> None:
        self.shown.update(progress)
        try:
            self.store.set_progress(self.run_id, dict(self.shown))
        except Exception:        # a status display must not fail the page
            log.warning("progress of %s not stored", self.run_id, exc_info=True)


SPEND_STEP_USD = 0.002            # the cost on the row moves at least this much per write
AFTER_TRIES = 3                   # `run.after` step attempts (each its own recorded step)


def _after(deps: PageDeps, job: PageJob, run_id: str, status: RunStatus,
           done: BaseModel | None) -> str | None:
    """`deps.on_write` (the Bot's ledger row, a merge's settle, ...) as durable steps, retried; it
    is idempotent. None: it ran (or there is no hook); else why it never did: the run then ends
    failed, visibly (fail closed), though its page is committed."""
    hook = deps.on_write
    if hook is None:
        return None
    def run_hook() -> bool:
        hook(job, run_id, status, done)
        return True
    last = ""
    for attempt in range(AFTER_TRIES):
        try:
            durable.step("run.after", run_hook)
            return None
        except Exception as exc:  # broad on purpose: any failure of the hook is retried, then shown
            last = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("after-write of %s for %s/%s failed (attempt %d)", run_id, job.user_id,
                          job.page_id, attempt + 1)
    log.error("after-write of %s for %s/%s failed for good: %s", run_id, job.user_id, job.page_id,
              last)
    return last


def _with_predecessors(job: PageJob, deps: PageDeps) -> PageJob:
    """The pages before this one on its path as the writer will see them: taken when the write
    begins (a durable step, so a replay compares against the same pages). A path edit made while
    the page is queued is followed, not stale; one made while it is written is found when it ends
    (`WriteQueue.after_write`)."""
    from evowiki.wikiapp.write_queue import predecessors

    def read() -> list:
        before, path_id = predecessors(deps.repo.load(job.user_id).state, job.page_id)
        return [None if before is None else list(before), path_id]
    before, path_id = durable.step("run.before", read)
    return job.model_copy(update={"before": None if before is None else tuple(before),
                                  "path_id": path_id})


@traced_entry("write", subject=lambda a: a["job"].page_id,
              user=lambda a: a["job"].user_id, input=lambda a: {"job_id": a["run_id"]})
def execute(job: PageJob, run_id: str, emit: Callable[[str, BaseModel], None],
            deps: PageDeps) -> RunStatus:
    """Runs the job; returns its final status (`done` when the last item was `done`). Always ends
    the item stream with `done` or `error` (an unexpected exception becomes `internal`)."""
    last: list[str] = []
    held: list[tuple[str, BaseModel]] = []    # the first end event and anything after it
    progress = _Progress(deps.store, run_id)

    def tee(name: str, payload: BaseModel) -> None:
        # The end event must reach the stream only after `run.finish` marks the row, so a
        # follower that stops at it never sees a still-`writing` row.
        if held or name in END_EVENTS:
            last.append(name)
            held.append((name, payload))
            return
        last.append(name)
        progress.see(name, payload)
        emit(name, payload)

    def ended() -> str | None:
        return next((n for n in last if n in END_EVENTS), None)

    began = durable.now(deps.clock)
    durable.step("run.begin", lambda: deps.store.begin(run_id, began) or True)
    if job.kind == "organize":
        job = _with_predecessors(job, deps)
    spent = 0.0
    try:
        if job.kind == "organize":
            spent = _write(job, run_id, tee, deps, progress)
        elif job.kind == "feedback":
            _feedback(job, run_id, tee, deps)
        elif job.kind in ("refine", *REWRITE_KINDS):
            _refine(job, run_id, tee, deps)
        elif job.kind == "source_revise":
            spent = _revise(job, run_id, tee, deps)
        else:
            raise NotImplementedError(f"page run kind {job.kind!r} is not on workflows")
        if ended() is None:
            tee("error", ErrorEvent(code="internal", retryable=True,
                                    detail="write run ended without done"))
    except Exception as exc:    # fail closed: the follower gets an error event, the row `failed`
        log.exception("page run %s for %s/%s crashed", run_id, job.user_id, job.page_id)
        if ended() is None:
            tee("error", ErrorEvent(code="internal", retryable=True,
                                    detail=f"{type(exc).__name__}: {exc}"[:500]))
        # else: it failed after the first end event: the page is written
        # and the stream ended, so that end event's status stands
    end = next((p for n, p in held if n in END_EVENTS), None)
    status: RunStatus = "done" if ended() == "done" else "failed"
    done = end if status == "done" else None
    cost = getattr(end, "cost_usd", None) if status == "done" else (spent or None)
    calls = getattr(end, "llm_calls", None)
    failure = None if status == "done" else str(getattr(end, "code", "internal"))
    broken = _after(deps, job, run_id, status, done)
    if broken is not None and status == "done":
        # the page is committed but what follows it (the ledger row, a merge's settle) is not: not
        # reported as done
        status, failure = "failed", "after_write"
        held[:] = [(n, p) for n, p in held if n not in END_EVENTS]
        held.append(("error", ErrorEvent(code="internal", retryable=True,
                                         detail=f"页面写好了，但收尾没成功：{broken}"[:500])))
    finished = durable.now(deps.clock)
    durable.step("run.finish", lambda: deps.store.finish(
        run_id, status, finished, cost_usd=cost, llm_calls=calls, failure=failure) or True)
    for name, payload in held:
        emit(name, payload)
    return status


@DBOS.workflow(name="evowiki.page_run")
def page_workflow(job: dict) -> str:
    pj = PageJob.model_validate(job)
    run_id = DBOS.workflow_id
    if run_id is None:
        raise RuntimeError("page_workflow ran outside a DBOS workflow")
    deps: PageDeps | None = None
    try:
        deps = installed()
        return execute(pj, run_id, lambda name, p: DBOS.write_stream(STREAM_KEY,
                                                                      encode_item(name, p)),
                       deps)
    finally:
        DBOS.close_stream(STREAM_KEY)
        if deps is not None and deps.after is not None:
            # starting runs from a workflow body would be recorded as its steps and break a replay:
            # a thread has no workflow context. A lost kick is made up by the Bot's next tick.
            threading.Thread(target=_kick, args=(deps.after, pj.user_id, run_id),
                             name="page-after", daemon=True).start()


def _kick(after: Callable[[str], None], user_id: str, run_id: str) -> None:
    try:
        after(user_id)
    except Exception:
        log.exception("after page run %s failed", run_id)
