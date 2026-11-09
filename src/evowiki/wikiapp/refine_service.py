"""Deep refine jobs (plan rev 3.3 §10.2): start, run in the background, commit once.

`DeepService.start` records a queued `RefineJob`; `run` (on a worker thread, after the response) loads
the page, runs the agent and commits the draft through one wiki commit — the page record plus its
interactive assets (`Commit.extra`) — or records why not. It never raises.

- **Spending:** every call is charged to the user's 「页面」 group (`Account(user, "page")`) through the
  normal meter, and to a per-job meter that stops the job before a call once the page's own limit
  (`RefineSettings.per_page_limit_usd`, when the user set one) is spent: `budget_exceeded`, at
  whatever stage it was. No limit set (None, the default) = never refused for cost; still metered.
- **page_changed:** the draft was written against the page's outline and texts; if another flow changed
  them before the commit, nothing is written. Display state (folds, pins, answers) may change freely.
- **dry_run:** returns the draft and writes nothing — no job rows, no assets, no page.
- **replay_section** (harness RSI, design 2026-10-21 §5.4): a dry run of one section on the snapshot a
  past run was built from (`Repo.load_at`), under a given teaching spec and harness pack source. Its
  trace is written like any run's, under `REPLAY_RUN_PREFIX` ids with the `harness_replay` job marker
  (and the candidate it ran, `job.candidate`). Its writer is the model the past run wrote with
  (`writer`, `pinned_writer`: no fallback; a model no writer config routes to any more is a
  LookupError, MODEL_GONE). Its tools: image search is off the network (`NoImages`: finds nothing,
  so the tool is offered as in the past run but places no image); the component library, the
  materials (`sources` / `reader`, whose search may call the embedder) and the forged tools are
  read as they are today, not as they were at the snapshot.
- **Gates:** only code gates the commit — the render check (an interactive that never passes is
  dropped, the text is committed without it, `job.dropped` says why). The writer's self-check is
  recorded on the job (`job.self_check`), never a reason to drop the draft.
- **Failures** (LLM, budget, no draft, page_changed) keep the skeleton and store the last draft the
  agent had with the job's final snapshot (`RefineJobOut.last_draft`, read-only)."""

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from evowiki.agent import durable
from evowiki.agent.tools import ToolResult
from evowiki.agent.trace import REPLAY_RUN_PREFIX, REPLAY_TRIGGER, TraceSink
from evowiki.deep.agent import (
    RefineFailed,
    RefineOutcome,
    SnapshotSeqs,
    refine,
)
from evowiki.deep.contracts import (
    RefineDraft,
    RefineFailure,
    RefineJob,
    RefineJobOut,
    RefineScope,
    RefineStage,
    RefineTrigger,
    RenderChecker,
)
from evowiki.deep.durable import DurableChecker, DurableImages, DurableJobStore, DurableLibrary
from evowiki.deep.images import ImageCandidate, ImageRejected, ImageSearch, PreparedImage
from evowiki.deep.library import ComponentLibrary
from evowiki.deep.llm import FORGE_UNAVAILABLE, GROUP, DeepLLM, JobMeter, forge_opener
from evowiki.deep.store import AssetStore, JobStore, StoredAsset
from evowiki.goals.fold import Page
from evowiki.goals.page_update import recheck, writer_instruction
from evowiki.harness.source import HarnessSource
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import ModelRef
from evowiki.llm.usage import Account
from evowiki.sources.cite import CiteIssue
from evowiki.sources.excerpts import SourceExcerpts
from evowiki.sources.reader import SourceReader
from evowiki.teach.contracts import TeachingSpec
from evowiki.wikiapp.durable_repo import DurableRepo
from evowiki.wikiapp.events import PageSaved, PageSource, WikiEvent
from evowiki.wikiapp.kwrite import page_spec
from evowiki.wikiapp.refine_apply import (
    KEEP_DOMAIN,
    checked_citations,
    concept_ancestors,
    content,
    make_assets,
    page_sources,
    refine_input,
    with_sources,
    written_record,
)
from evowiki.wikiapp.refine_spec import OnlySpec, SpecSource, WikiSpecSource
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict
from evowiki.wikiapp.sectionlog import logs_event, section_logs
from evowiki.wikiapp.teachspec import PageTeach

if TYPE_CHECKING:
    from evowiki.forge.run import ForgeRuntime

log = logging.getLogger("evowiki.wikiapp.refine_service")

COMMIT_TRIES = 3
LOST_AFTER = timedelta(minutes=30)   # a job still running after this is treated as dead
DONE: frozenset[RefineStage] = frozenset({"done", "failed", "cancelled"})
STILL_WRITING = "这一页还在写，写完再改"   # a page whose first write has not finished
MODEL_GONE = "那次写作用的模型现在用不上了，不能重放"
MODEL_MISMATCH = "重放用的模型和那次写作不一样，不能比"
NO_IMAGES = "重放时不搜图片"


@dataclass
class RefineResult:
    job: RefineJob
    draft: RefineDraft | None = None
    outcome: RefineOutcome | None = None
    assets: list[StoredAsset] = field(default_factory=list)
    last_draft: RefineDraft | None = None      # a failed job's last draft (never committed)


class JobRunning(RuntimeError):
    """A refine of this page is already queued or running (409)."""


class ReplayFailed(RuntimeError):
    """A replay ended without a draft of its section (the job's failure and detail). `cost`: what
    its calls were charged anyway; `run_ids`: the trace runs it started."""

    def __init__(self, failure: str, detail: str, *, cost: float = 0.0,
                 run_ids: tuple[str, ...] = ()):
        super().__init__(f"{failure}: {detail}"[:500])
        self.failure, self.detail = failure, detail
        self.cost, self.run_ids = cost, run_ids


@dataclass(frozen=True)
class ReplayDraft:
    """One replayed section. `writer_provider`: the provider family of the call that wrote it (after
    any fallback); `run_id`: the replay's last write round (the one that produced the draft);
    `cost`: what the replay's calls were charged (a call served from the call log costs nothing)."""

    body: str
    cue: str | None
    writer_provider: str
    run_id: str
    cost: float


class NoImages:
    """A replay's image search (module doc): finds nothing and downloads nothing, so a replay never
    reaches the network for images."""

    def search(self, query: str, limit: int) -> list[ImageCandidate]:
        return []

    def fetch(self, candidate: ImageCandidate) -> PreparedImage:
        raise ImageRejected(NO_IMAGES)


def pinned_writer(llm: DeepLLM, user_id: str, want: ModelRef) -> DeepLLM:
    """`llm` writing with `want` (a replay's past writer, module doc) and no fallback: the user's
    own pick when its writer is `want` (its step tiers too), else the model whose writer config is,
    else the default config. LookupError (MODEL_GONE) when no writer config routes to `want`."""
    def wanted(cfg: Any) -> bool:
        return (cfg.generator.provider, cfg.generator.model) == (want.provider, want.model)
    base = replace(llm, fallback=None)
    if wanted(llm.writer_config(user_id)):
        return base
    for m, cfg in (llm.writers or {}).items():
        if wanted(cfg):
            return replace(base, pick=lambda _uid, m=m: m)
    if wanted(llm.config):
        return replace(base, pick=None)
    log.info("replay for %s: the past run wrote with %s:%s, no writer config routes to it",
             user_id, want.provider, want.model)
    raise LookupError(MODEL_GONE)


class _ReplayTrace:
    """The trace of one replay: each run id gets `harness-replay:<replay id>:` in front (unique per
    replay, also inside a workflow whose runs share the workflow id) and each run's input the job
    marker (with the `candidate` it ran, None = the current pack); everything else goes to `inner`
    unchanged."""

    def __init__(self, inner: TraceSink, replay_id: str, candidate: str | None = None):
        self._inner = inner
        self._prefix = f"{REPLAY_RUN_PREFIX}{replay_id}:"
        self._job = {"trigger": REPLAY_TRIGGER, "job_id": replay_id, "candidate": candidate}
        self.started: list[str] = []
        self.run_of_call: dict[str, str] = {}     # llm_call id -> the replay run that made it

    def start(self, run_id: str, *, user_id: str, agent: str, subject: str, pack_version: int,
              input: dict) -> None:
        rid = self._prefix + run_id
        self.started.append(rid)
        self._inner.start(rid, user_id=user_id, agent=agent, subject=subject,
                          pack_version=pack_version, input={**input, "job": self._job})

    def model(self, run_id: str, seq: int, request: Any, turn: Any, call_id: str | None,
              error: str | None) -> None:
        if call_id is not None:
            self.run_of_call[call_id] = self._prefix + run_id
        self._inner.model(self._prefix + run_id, seq, request, turn, call_id, error)

    def tool(self, run_id: str, seq: int, call_id: str, name: str, args: dict,
             result: ToolResult) -> None:
        self._inner.tool(self._prefix + run_id, seq, call_id, name, args, result)

    def finish(self, run_id: str, *, stop: str | None, error: str | None) -> None:
        self._inner.finish(self._prefix + run_id, stop=stop, error=error)


@dataclass
class DeepService:
    repo: Repo
    jobs: JobStore
    assets: AssetStore
    specs: SpecSource
    checker: RenderChecker
    llm: DeepLLM | None                       # None: no deep tier configured → llm_unavailable
    clock: Callable[[], datetime]
    new_id: Callable[[str], str]
    library: ComponentLibrary | None = None   # the interactives refine may place; None = text only
    # the material sections a page's concepts map to, and the materials' reader for the writer's
    # read_source / search_source (materials Task 6); None = no materials
    sources: SourceExcerpts | None = None
    reader: SourceReader | None = None
    # forged tools for the writer (coding-agent RSI phase 2); None = none (tests, forge not built)
    forge: "ForgeRuntime | None" = None
    # the writer's find_image / place_image (Commons, Openverse); None = no image tools
    images: ImageSearch | None = None

    # ---- reads ----

    def out(self, user_id: str, page_id: str) -> RefineJobOut:
        job = self.jobs.latest(user_id, page_id)
        if job is None:
            return RefineJobOut(job=None)
        ids = set(job.interactives)
        draft = self.jobs.last_draft(user_id, job.job_id) if job.stage == "failed" else None
        return RefineJobOut(job=job, assets=tuple(m for m in self.assets.for_page(user_id, page_id)
                                                  if m.asset_id in ids), last_draft=draft)

    def running(self, user_id: str, page_id: str) -> RefineJob | None:
        job = self.jobs.latest(user_id, page_id)
        if job is None or job.stage in DONE or self.clock() - job.started_at > LOST_AFTER:
            return None
        return job

    # ---- start / run ----

    def durable_copy(self) -> "DeepService":
        """The service for a page workflow body: repo reads and the commit, job saves, render
        checks, library reads, the clock and ids are steps, so `run` replays to where a crash
        stopped. Model calls are steps in the gateways `llm` builds. Outside a workflow every
        wrapper is a plain delegate."""
        repo = DurableRepo(self.repo)
        specs = WikiSpecSource(repo) if isinstance(self.specs, WikiSpecSource) else self.specs
        return dataclasses.replace(
            self, repo=repo, specs=specs, jobs=DurableJobStore(self.jobs),
            checker=DurableChecker(self.checker),
            library=DurableLibrary(self.library) if self.library is not None else None,
            images=DurableImages(self.images) if self.images is not None else None,
            clock=lambda: durable.now(self.clock), new_id=durable.ids(self.new_id))

    def start(self, user_id: str, page_id: str, trigger: RefineTrigger) -> RefineJob:
        """Records a queued job. LookupError: unknown user / page; ValueError: page not organized
        or still being written (`STILL_WRITING`);
        JobRunning: one is already queued or running."""
        rec = self.repo.load(user_id).state.pages.get(page_id)
        if rec is None:
            raise LookupError(f"没有这个页面：{page_id}")
        if rec.page is None:
            raise ValueError("页面还没生成，不能精修")
        if not rec.has_body:            # its first write is running or stopped part-way
            raise ValueError(STILL_WRITING)
        if (cur := self.running(user_id, page_id)) is not None:
            raise JobRunning(f"页面正在精修（{cur.job_id}）")
        job = RefineJob(job_id=self.new_id("rf"), page_id=page_id, trigger=trigger,
                        stage="queued", started_at=self.clock(),
                        model=self.llm.model_for(user_id) if self.llm is not None else "")
        self.jobs.save(user_id, job)
        return job

    def run(self, user_id: str, job: RefineJob, scope: RefineScope | None = None) -> RefineResult:
        """Runs a started job (or, with `scope.dry_run`, an unsaved one). Never raises. A "patch"
        job writes the sections a K change handed to the writer (`page_update.writer_sections`):
        they are its required sections, and the page's conflicts are its instruction."""
        scope = scope or RefineScope(page_id=job.page_id)
        if job.trigger != "patch":
            return self._run(user_id, job, scope, self.repo.load)
        try:
            rec = self.repo.load(user_id).state.pages.get(job.page_id)
            messages = rec.page.unresolved_conflicts if rec is not None and rec.page else ()
        except Exception:  # noqa: BLE001 - _run records the load failure on the job
            messages = ()
        return self._run(user_id, job, scope, self.repo.load, required=scope.sections,
                         instruction=writer_instruction(messages) if messages else "")

    def replay_section(self, user_id: str, *, snapshot: SnapshotSeqs, page_id: str,
                       section_id: str, spec: TeachingSpec, harness: HarnessSource,
                       mode: Literal["page", "rewrite", "feedback"] = "rewrite",
                       instruction: str = "", page: Page | None = None,
                       domain: Any = KEEP_DOMAIN,
                       forge_candidate: str | None = None, writer: ModelRef | None = None,
                       candidate: str | None = None) -> ReplayDraft:
        """Rewrites one section on the state at `snapshot` (K, the other pages, the page's record),
        under `spec` and the packs of `harness`, writing nothing but the trace (dry run, scope
        `(section_id,)`, no required sections). `page` / `domain`: what the past run was given in
        place of the stored page and domain (its traced `page` / `domain`: a first write's fresh
        outline, a feedback outline), or for a section the run's job added, the page as that job's
        commit saved it (a later wiki seq than `snapshot`); default the snapshot's own. Raises LookupError when the snapshot cannot be loaded or has no
        such page / section, ReplayFailed when the writer produced no draft of it, RuntimeError
        without a deep tier. `forge_candidate`: a forged-tool candidate the replay's writer may use
        (run-scoped overlay); ReplayFailed("forge_unavailable") without a forge runtime, or when
        the run the forge opens does not reach the candidate (switch off, pin failed): never a
        replay without it. `writer`: the model the past run wrote with (module doc); the replay
        writes with it and nothing else (LookupError MODEL_GONE when it no longer routes,
        ReplayFailed("model_mismatch") if a write was answered by another); None = the user's
        writer now (a case with no traced run). `candidate`: what the trace's job marker records
        of the candidate (`harness.replay.candidate_ref`)."""
        if self.llm is None:
            raise RuntimeError("没有配置精修模型")
        llm = self.llm if writer is None else pinned_writer(self.llm, user_id, writer)
        if forge_candidate is not None and self.forge is None:
            raise ReplayFailed("forge_unavailable", "没有可用的工具锻造，无法重放这个新工具")
        snap = self.repo.load_at(user_id, snapshot.wiki_seq, snapshot.aspect_seq,
                                 snapshot.infer_version)
        rec = snap.state.pages.get(page_id)
        written = page if page is not None else rec.page if rec is not None else None
        if rec is None or written is None:
            raise LookupError(f"快照里没有这个页面：{page_id}")
        if section_id not in {s.id for s in written.outline.sections}:
            raise LookupError(f"快照里的页面 {page_id} 没有这一段：{section_id}")
        replay_id = self.new_id("hr")
        tr = _ReplayTrace(llm.trace, replay_id, candidate)
        svc = dataclasses.replace(self, specs=OnlySpec(spec), llm=replace(
            llm.with_harness(harness), trace=tr, salt=replay_id),
            images=NoImages() if self.images is not None else None)
        job = RefineJob(job_id=replay_id, page_id=page_id, trigger="harness_replay",
                        stage="queued", started_at=self.clock(), model=llm.model_for(user_id))
        scope = RefineScope(page_id=page_id, sections=(section_id,), dry_run=True,
                            spec_version=spec.version)
        missing: list[bool] = []
        res = svc._run(user_id, job, scope, lambda uid: snap, mode=mode,
                       instruction=instruction, required=(), page=page, domain=domain,
                       forge_candidate=forge_candidate,
                       on_missing=(lambda: missing.append(True)) if forge_candidate else None)
        spent = {"cost": res.job.cost_usd, "run_ids": tuple(tr.started)}   # a failure was paid too
        if missing:
            raise ReplayFailed("forge_unavailable", FORGE_UNAVAILABLE, **spent)
        if res.draft is None or res.outcome is None:
            raise ReplayFailed(res.job.failure or "internal", res.job.failure_detail or "",
                               **spent)
        sec = next((s for s in res.draft.sections if s.section_id == section_id), None)
        if sec is None:
            raise ReplayFailed("no_draft", f"草稿里没有这一段：{section_id}", **spent)
        writes = [c for c in res.outcome.calls if c.purpose == "deep.write"]
        run_id = tr.run_of_call.get(writes[-1].id) if writes else None
        if run_id is None:
            raise ReplayFailed("internal", "重放没有写作调用记录", **spent)
        if writer is not None and any((c.provider, c.model) != (writer.provider, writer.model)
                                      for c in writes):
            raise ReplayFailed("model_mismatch", MODEL_MISMATCH, **spent)
        return ReplayDraft(body=sec.body, cue=sec.cue or None, writer_provider=writes[-1].provider,
                           run_id=run_id, cost=res.job.cost_usd)

    def _run(self, user_id: str, job: RefineJob, scope: RefineScope,
             load: Callable[[str], Snapshot], *,
             mode: Literal["page", "rewrite", "feedback"] = "rewrite", instruction: str = "",
             required: tuple[str, ...] | None = None, page: Page | None = None,
             domain: Any = KEEP_DOMAIN, forge_candidate: str | None = None,
             on_missing: Callable[[], None] | None = None) -> RefineResult:
        """`run` on the snapshot `load` returns (the page's own cost limit is always today's);
        `required` replaces the input's required sections (None = refine_input's own); `page` /
        `domain` as refine_input's, `forge_candidate` / `on_missing` for forge_opener (replays
        only). Never raises."""
        dry = scope.dry_run
        state = {"job": job}

        def save(draft: RefineDraft | None = None, **up: Any) -> RefineJob:
            state["job"] = state["job"].model_copy(update=up)
            if not dry:
                try:
                    self.jobs.save(user_id, state["job"], draft)
                except Exception:
                    log.exception("refine %s: could not save the job", job.job_id)
            return state["job"]

        def fail(failure: RefineFailure, detail: str, meter: JobMeter | None,
                 draft: RefineDraft | None = None, **up: Any) -> RefineResult:
            """The job ends failed; `draft` (the last one the agent had) is kept read-only."""
            stopped = state["job"].stage
            log.warning("refine %s for %s/%s failed at %s: %s %s", job.job_id, user_id,
                        job.page_id, stopped, failure, detail)
            return RefineResult(job=save(
                draft, stage="failed", failure=failure,
                failure_detail=f"stage={stopped}: {detail}"[:500], finished_at=self.clock(),
                cost_usd=round(meter.spent, 6) if meter else 0.0, **up), last_draft=draft)

        try:
            limit = self.repo.load(user_id).state.refine.per_page_limit_usd
        except Exception as exc:  # noqa: BLE001 - recorded on the job
            return fail("internal", f"{type(exc).__name__}: {exc}", None)
        if self.llm is None:
            return fail("llm_unavailable", "没有配置精修模型", None)
        roster: dict[str, list[Gateway]] | None = None
        if self.llm.tiers is not None:
            roster, meter = self.llm.roster(limit, user_id)
            gws = roster["deep"]
        else:
            gws, meter = self.llm.gateways(limit, user_id)
        account = Account(user_id=user_id, group=GROUP)

        writer = {"model": self.llm.model_for(user_id), "reason": ""}

        def on_stage(stage: RefineStage, rnd: int) -> None:
            save(stage=stage, round=min(rnd, 3), cost_usd=round(meter.spent, 6))

        def on_writer(model: str, fallback: bool, reason: str) -> None:
            writer["model"] = model
            if fallback:
                writer["reason"] = reason
                save(model=model)

        try:
            snap = load(user_id)
            inp = refine_input(snap, job.page_id, self.clock(), self.specs,
                               scope=scope.sections, spec_version=scope.spec_version, mode=mode,
                               instruction=instruction, page=page, domain=domain)
            inp = replace(inp, dry_run=dry, job_id=job.job_id,
                          **({"required": required} if required is not None else {}))
            # a failing material read fails the job (internal), never a write without them
            concepts = snap.state.pages[job.page_id].concepts
            inp = with_sources(inp, concepts, self.sources, readable=self.reader is not None,
                               parents=concept_ancestors(snap.state, concepts))
            base = content(snap.state.pages[job.page_id])
            save(spec_version=inp.teaching.version)
            outcome = refine(inp, gws[0], account, self.checker, on_stage=on_stage,
                             library=self.library, fallbacks=gws[1:], on_writer=on_writer,
                             trace=self.llm.trace, roster=roster,
                             reader=self.reader if inp.sources else None,
                             forge=forge_opener(self.forge, inp, meter.charge,
                                                candidate=forge_candidate, on_missing=on_missing),
                             images=self.images)
            # citations checked against the materials (Task 6b); a failing read fails the job
            draft, cite_issues = checked_citations(outcome.draft, inp, self.sources)
            outcome = dataclasses.replace(outcome, draft=draft)
        except RefineFailed as exc:
            return fail(exc.failure, exc.detail, meter, exc.draft)
        except (LookupError, ValueError) as exc:
            return fail("internal", str(exc), meter)
        except Exception as exc:
            log.exception("refine %s crashed", job.job_id)
            return fail("internal", f"{type(exc).__name__}: {exc}", meter)
        rounds = {"round": min(outcome.rounds, 3), "cost_usd": round(meter.spent, 6),
                  "self_check": outcome.self_check, "dropped": outcome.dropped}
        if dry:
            return RefineResult(job=save(stage="done", finished_at=self.clock(), **rounds),
                                draft=outcome.draft, outcome=outcome)
        save(stage="commit", **rounds)
        try:
            assets = self._commit(user_id, job, outcome, base, inp.teaching,
                                  model=writer["model"], reason=writer["reason"],
                                  cost_usd=meter.spent, sources=page_sources(inp),
                                  cite_issues=cite_issues)
        except _PageChanged as exc:
            return fail("page_changed", str(exc), meter, outcome.draft)
        except Exception as exc:
            log.exception("refine %s: commit failed", job.job_id)
            return fail("internal", f"commit: {type(exc).__name__}: {exc}", meter, outcome.draft)
        changed = tuple(s.section_id for s in outcome.draft.sections)
        done = save(stage="done", finished_at=self.clock(), changed_sections=changed,
                    interactives=tuple(a.meta.asset_id for a in assets), **rounds)
        return RefineResult(job=done, draft=outcome.draft, outcome=outcome, assets=assets)

    def _commit(self, user_id: str, job: RefineJob, outcome: RefineOutcome, base: tuple | None,
                teaching: TeachingSpec, *, model: str = "", reason: str = "",
                cost_usd: float = 0.0,
                sources: tuple[PageSource, ...] = (),
                cite_issues: tuple[CiteIssue, ...] = ()) -> list[StoredAsset]:
        """The refined record + a SectionLog per changed section (producer "refine", the spec
        items the refine ran under) + the assets, in one commit. A "patch" job's page gets its
        conflicts found again (`page_update.recheck`)."""
        now = self.clock()
        assets = make_assets(outcome.draft, outcome.checks, user_id=user_id, job_id=job.job_id,
                             now=now, new_id=self.new_id)
        for _ in range(COMMIT_TRIES):
            snap = self.repo.load(user_id)
            rec = snap.state.pages.get(job.page_id)
            if rec is None or content(rec) != base:
                raise _PageChanged("页面在精修期间被别的流程改过，这次精修没有写入")
            new = written_record(rec, outcome.draft, assets, job_id=job.job_id, now=now,
                                 model=model or (self.llm.model if self.llm else ""),
                                 spec_version=teaching.version, kind="rewrite",
                                 cost_usd=cost_usd, fallback_reason=reason, sources=sources,
                                 cite_issues=cite_issues)
            assert new.page is not None
            teach = PageTeach(items=teaching.items, params=teaching.params,
                              version=teaching.version)
            spec = page_spec(snap.state, snap.k, job.page_id, user_id=user_id, teach=teach)
            if job.trigger == "patch":
                new = new.model_copy(update={"page": recheck(spec, new.page, snap.kview(now))})
            ev = WikiEvent(event_id=self.new_id("wev"), ts=now,
                           body=PageSaved(record=new, action="refined"))
            logs = section_logs(
                new.page.outline, new.written.sections if new.written else (), producer="refine",
                spec=spec, k=snap.kview(now), teach=teach, at=now,
                infer_version=snap.infer_version)
            try:
                self.repo.commit(snap, Commit(wiki=[ev, *logs_event(logs, new_id=self.new_id,
                                                                     now=now)],
                                              extra=[self.assets.writer(assets)]))
                return assets
            except WikiConflict:
                continue
        raise _PageChanged("wiki kept changing during the commit")


class _PageChanged(RuntimeError):
    pass
