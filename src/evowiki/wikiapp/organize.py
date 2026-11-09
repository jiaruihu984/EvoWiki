"""Writing a page: first generation and explicit reorganize, as a stream of SSE items (plan Task 7; single
writer 2026-10-10).

planning → outline → writer → (section_added? section_delta section_done)* → done, or error at any
point.

No model call runs before the writer's loop (LLM-call consolidation stage 4): `planning` goes out
first, the page's teaching domain is the embedding's nearest label (`teach_domain.pick_domain`), and
the writer (`deep.writer.PageWriter`: Claude when an Anthropic key is set, DeepSeek's good tier as its
fallback / the only writer otherwise) does the rest in one loop:
1. Outline: its first tool call, plan_page, carries the outline (sections, roles, cells, check items;
   code checks only, `goals.outline_context.accept_draft` + `checks.accept_outline_checks`) and the
   plan; on accept `outline` goes out (`on_outline`). The writer's own `writer` event is held until
   then, so the stream keeps its order.
2. Writing: the writer writes every non-check section of that outline, in page order. Each written section goes out as it is written (`section_delta` +
   `section_done`; the same id may come again — last one wins); a section the writer inserts is
   announced first (`section_added`). `writer` says which model writes, and again when the fallback
   takes over (fail closed: never a silent downgrade).
3. Commit: after the render gate the page is committed once — with retries on a fresh snapshot, as long
   as the page's content is still what this write started from (else `error{conflict}`, nothing
   written). Sections whose committed body differs from what was streamed (an interactive's embed, a
   dropped interactive) go out once more, then `done` (writer spend, model, fallback). There is no second
   pass: nothing replaces the page after `done`.

The work runs in a page run (`wikiapp.page_runs`, a DBOS workflow in the service): `organize_stream`
follows the page's run (a page the idle Bot wrote ahead, a stream the client lost) or starts one.
A follower that leaves does not cancel the run; 「停止」 does (`POST /wiki/pages/{id}/run/cancel`).

- First time (`record.page is None`) and reorganize alike: the writer plans a new outline.
- Reorganize (D10 rev 2): the page is replaced whole — pins and fold states are dropped,
  `unresolved_conflicts=()`, every stored check answer is cleared (the items are new, I1), and a
  page shelved as a reference card gets `collapse_applied=False` so its next open folds it again
  (M3).

Teaching spec (R1): the prompts carry the items in force for the page plus the 4 params lines
(`wikiapp.teachspec.page_teach`). A page without a current domain gets the embedding's nearest label
first, only when the spec has domain items (none → None, asked again next write). Check sections get
an `organizer` SectionLog (the outline's), written sections a `writer` one."""

import dataclasses
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from threading import Event
from typing import Protocol

from pydantic import BaseModel

from evowiki.agent import durable
from evowiki.deep.agent import Change, RefineFailed, RefineInput
from evowiki.deep.contracts import RefineDraft
from evowiki.deep.store import AssetStore
from evowiki.deep.tools import Outlined, Workspace, merge_sections, page_order
from evowiki.deep.writer import PracticeResult, WriteResult
from evowiki.goals.contracts import KView, OutlineSection, SectionText
from evowiki.goals.fold import Page, SectionState
from evowiki.llm.budget import JobBudget
from evowiki.llm.providers import ProviderTransientError, notice_error
from evowiki.llm.usage import BudgetExceededError
from evowiki.retrieval.embed import Embedder
from evowiki.sources.cite import CiteIssue
from evowiki.sources.excerpts import SourceExcerpts
from evowiki.sources.reader import SourceReader
from evowiki.teach.contracts import TeachingSpec
from evowiki.wikiapp.events import (
    PageRecord,
    PageSaved,
    PagesTrashed,
    SeriesUpdated,
    WikiEvent,
    WritingMark,
)
from evowiki.wikiapp.kwrite import Ids, page_spec
from evowiki.wikiapp.llm import (
    LLM_BUDGET,
    LLM_FAILURES,
    LLM_UNAVAILABLE,
    CountingGateway,
    retryable,
)
from evowiki.wikiapp.page_merge import merge_instruction, merge_spec
from evowiki.wikiapp.page_runs import PageJob, PageRun, PageRuns
from evowiki.wikiapp.refine_apply import (
    checked_citations,
    concept_ancestors,
    content,
    make_assets,
    page_sources,
    path_of,
    refine_input,
    stream_body,
    with_sources,
    written_record,
)
from evowiki.wikiapp.refine_spec import SpecSource
from evowiki.wikiapp.repo import Commit, Repo, WikiConflict
from evowiki.wikiapp.sectionlog import logs_event, section_logs
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.teach_domain import pick_domain
from evowiki.wikiapp.teachspec import (
    SpecInvalid,
    effective_spec,
    norm_domain,
    page_teach,
    spec_domains,
)
from evowiki.wikiapp.views import cells_out, section_out
from evowiki.wikiapp.wiki_schemas import (
    ErrorEvent,
    OrganizeDoneEvent,
    OutlineEvent,
    PlanningEvent,
    SectionAddedEvent,
    SectionDeltaEvent,
    SectionDoneEvent,
    WriterEvent,
)

OrganizeItem = tuple[str, BaseModel]   # (sse event name, payload model from wiki_schemas)
COMMIT_TRIES = 3
log = logging.getLogger(__name__)


class Writer(Protocol):
    """`deep.writer.PageWriter` (tests use a scripted double)."""

    def write(self, inp: RefineInput, *, budget: JobBudget | None = None,
              on_writer: Callable[[str, bool, str], None] = ...,
              on_change: Change | None = None,
              on_spend: Callable[[float, int], None] | None = None,
              on_outline: Outlined | None = None,
              cancel: Event | None = None,
              reader: SourceReader | None = None) -> WriteResult: ...

    def practice(self, inp: RefineInput, *, budget: JobBudget | None = None,
                 on_writer: Callable[[str, bool, str], None] = ...,
                 cancel: Event | None = None) -> PracticeResult:
        """The assistant's add_practice (`bot.practice`); a double that never composes practice
        need not have it."""
        ...


@dataclass(frozen=True)
class Writing:
    """What writing a page needs besides the wiki: the writer, the teaching spec source (the page's
    items in force + exemplar) and the asset store for the interactives it places (None: a draft
    with a component or block cannot be committed — fail closed); `embedder`: the service's
    embedder, which picks the page's domain (None: no domain). `sources`: the material sections a page's concepts map to (handed to
    the writer as `RefineInput.sources`; None: no materials); `reader`: the user's materials for
    the writer's read_source / search_source (configure both together: a summary-only section
    tells the writer to read it with read_source)."""

    writer: Writer
    specs: SpecSource
    assets: AssetStore | None = None
    embedder: Embedder | None = None
    sources: SourceExcerpts | None = None
    reader: SourceReader | None = None


class PageChanged(RuntimeError):
    """The page's content moved while it was being written: nothing is committed."""


class WriterMissing(RuntimeError):
    """No page writer is configured (no deep tier): llm_unavailable."""


class SourcesFailed(RuntimeError):
    """The page's material sections could not be read: the page is not written without them."""


def read_sources(inp: RefineInput, concepts: Sequence[str], writing: Writing,
                 state: WikiState) -> RefineInput:
    """`inp` with the page's material sections (`wikiapp.refine_apply.with_sources`; a point with
    no links uses its parent block's, from `state`'s graph); SourcesFailed when the read fails."""
    try:
        return with_sources(inp, concepts, writing.sources, readable=writing.reader is not None,
                            parents=concept_ancestors(state, concepts))
    except Exception as exc:
        raise SourcesFailed(f"资料读取失败：{type(exc).__name__}: {exc}") from exc


def check_citations(draft: RefineDraft, inp: RefineInput,
                    writing: Writing) -> tuple[RefineDraft, tuple[CiteIssue, ...]]:
    """The draft with its citations checked (`wikiapp.refine_apply.checked_citations`); SourcesFailed when
    the material pages cannot be read."""
    try:
        return checked_citations(draft, inp, writing.sources)
    except Exception as exc:
        raise SourcesFailed(f"出处核对时读资料失败：{type(exc).__name__}: {exc}") from exc


def writer_reader(inp: RefineInput, writing: Writing) -> SourceReader | None:
    """The material tools' reader: only for a page that has material sections."""
    return writing.reader if inp.sources else None


def _error(exc: BaseException) -> ErrorEvent:
    detail = f"{type(exc).__name__}: {exc}"[:500]
    if isinstance(exc, (WikiConflict, PageChanged)):
        return ErrorEvent(code="conflict", retryable=True, detail=detail)
    if isinstance(exc, WriterMissing):
        return ErrorEvent(code="llm_unavailable", retryable=False, detail=detail)
    if isinstance(exc, SourcesFailed):
        return ErrorEvent(code="internal", retryable=True, detail=detail)
    if isinstance(exc, RefineFailed):
        return _write_error(exc)
    notice = notice_error(exc)
    if notice is not None:
        return ErrorEvent(code=notice.code, retryable=True,  # type: ignore[arg-type]
                          detail=notice.user_text)
    if isinstance(exc, (*LLM_UNAVAILABLE, ProviderTransientError)):
        return ErrorEvent(code="llm_unavailable", retryable=retryable(exc), detail=detail)
    if isinstance(exc, BudgetExceededError):
        return ErrorEvent(code="budget_exceeded", retryable=False, detail=exc.line("这次没有写完"))
    if isinstance(exc, SpecInvalid):
        return ErrorEvent(code="invalid", retryable=False, detail=str(exc)[:500])
    return ErrorEvent(code="llm_failed", retryable=True, detail=detail)


def _write_error(exc: RefineFailed) -> ErrorEvent:
    """A failed write (`deep.writer.WriteFailed`): its code, the writer and what it spent so far."""
    spent = getattr(exc, "cost_usd", 0.0)
    model = getattr(exc, "model", "")
    if exc.failure in ("llm_busy", "llm_timeout"):
        return ErrorEvent(code=exc.failure, retryable=True, detail=exc.detail[:500])
    if exc.failure == "budget_exceeded":
        return ErrorEvent(code="budget_exceeded", retryable=False, detail=exc.detail[:500])
    detail = f"{exc.failure} ({model}, ${spent:.4f}): {exc.detail}"[:500]
    code = {"llm_unavailable": "llm_unavailable", "budget_exceeded": "budget_exceeded",
            "internal": "internal"}.get(exc.failure, "llm_failed")
    return ErrorEvent(code=code, retryable=exc.failure != "budget_exceeded", detail=detail)


def organize_stream(repo: Repo, gw: CountingGateway, user_id: str, page_id: str, *,
                    now: datetime, new_id: Ids, writing: Writing | None, runs: PageRuns,
                    on_done: Callable[[], None] | None = None,
                    fresh: bool = False) -> Iterator[OrganizeItem]:
    """Loads eagerly: an unknown user or page raises LookupError here, before any item, so the
    route answers 404 before opening the stream. Follows the page's live run in `runs` (the Bot's
    idle write, a lost stream) or starts one (`write_page`); `runs` is required (inline runs use the
    arguments given here, a DBOS run the service's installed deps; a throwaway store would restart
    run ids at 1). A page written ahead is the page's live run: `start(attach=True)` follows it.

    `on_done`: called once the run's `done` reaches this follower, before it is yielded. The page is
    committed by then: a run emits `done` only after its commit.

    `fresh` (重新组织 on a written page): the page is written anew. A run still writing is followed (never two
    writes of one page), but a finished one is not replayed: its replay is for a follower that lost its stream,
    and a page whose write was watched another way (GET /wiki/progress after a reload) never consumed it, so the
    first 重新组织 got the old run back and wrote nothing."""
    snap = repo.load(user_id)
    if page_id not in snap.state.pages:
        raise LookupError(f"没有这个页面：{page_id}")
    last = runs.store.latest_of(user_id, [page_id]).get(page_id)
    if fresh and last is not None and last.status == "done" and last.consumed_at is None:
        runs.store.consume(last.run_id, now)
        last = runs.store.latest_of(user_id, [page_id]).get(page_id)
    if (not fresh and last is not None and last.origin == "idle" and last.failure == "after_write"
            and last.consumed_at is None and last.discarded_at is None
            and page_id not in snap.state.trashed_pages
            and snap.state.pages[page_id].has_body):
        # the Bot wrote this page and committed it; only its bookkeeping failed: just open it
        runs.store.consume(last.run_id, now)
        return iter([("done", OrganizeDoneEvent(
            page_id=page_id, llm_calls=last.llm_calls or 0,
            cost_usd=last.cost_usd or 0.0, model="", fallback=False, prefetched=True))])
    from evowiki.wikiapp.page_flows import PageDeps  # page_flows imports this module

    deps = PageDeps(repo=repo, gateway=lambda _uid: gw, writing=writing, store=runs.store,
                    clock=lambda: now, new_id=new_id)
    run_id, _ = runs.start(PageJob(kind="organize", user_id=user_id, page_id=page_id, now=now),
                           deps, attach=True)
    items = runs.follow(run_id)
    return items if on_done is None else _calling_on_done(items, on_done)


def _calling_on_done(items: Iterator[OrganizeItem],
                     on_done: Callable[[], None]) -> Iterator[OrganizeItem]:
    for name, payload in items:
        if name == "done":
            try:
                on_done()
            except Exception:              # the page is committed: the stream still says done
                log.exception("after-done hook failed")
        yield name, payload


def _domain_current(domain: str | None, spec: TeachingSpec) -> bool:
    """The stored domain still names a spec domain (casefold, stripped). A spec without domain
    items needs no domain: the stored one is kept (it simply matches nothing), no call."""
    domains = spec_domains(spec)
    if not domains:
        return True
    d = norm_domain(domain)
    return d is not None and d.casefold() in {x.casefold() for x in domains}


class _Stream:
    """Turns the writer's workspace changes into section events; remembers what each section last
    showed so the commit sends only what differs."""

    def __init__(self, run: PageRun, state: WikiState, kview: KView, outline_ids: list[str], *,
                 user_id: str):
        self.run, self.state, self.kview, self.user_id = run, state, kview, user_id
        self.outline_ids = outline_ids
        self.added: set[str] = set()
        self.shown: dict[str, tuple[str, str, str]] = {}     # sid -> (heading, cue, body)

    def on_change(self, ws: Workspace, sid: str) -> None:
        s = ws.sections.get(sid)
        if s is None:                      # an interactive on a section not written yet
            return
        if sid not in self.outline_ids and sid not in self.added:
            order = page_order(self.outline_ids, ws.sections)
            i = order.index(sid)
            sec = OutlineSection(id=sid, heading=s.heading, role=s.role, cells=s.cells)
            self.run.emit("section_added", SectionAddedEvent(
                section=section_out(self.state, self.kview, sec, SectionState(section_id=sid),
                                    None, user_id=self.user_id),
                after=order[i - 1] if i > 0 else None))
            self.added.add(sid)
        self.send(sid, s.heading, s.cue, stream_body(ws, sid))

    def send(self, sid: str, heading: str, cue: str, body: str) -> None:
        if self.shown.get(sid) == (heading, cue, body):
            return
        self.shown[sid] = (heading, cue, body)
        self.run.emit("section_delta", SectionDeltaEvent(section_id=sid, cue=cue, body=body,
                                                         heading=heading))
        self.run.emit("section_done", SectionDoneEvent(section_id=sid, cached=False))

    def final(self, page: Page) -> None:
        """What the commit put on the page, where it differs from what was streamed."""
        heads = {s.id: s.heading for s in page.outline.sections}
        for sid in self.shown:
            t = page.texts.get(sid)
            if t is not None:
                self.send(sid, heads.get(sid, self.shown[sid][0]), t.cue, t.body)


def locked_instruction(page: Page) -> str:
    """For a reorganize: the sections the user locked stay on the page as they are, so the new
    outline does not teach them again ("" when none)."""
    locked = [s for s in page.outline.sections if page.state(s.id).pinned]
    if not locked:
        return ""
    lines = "\n".join(f"- 「{s.heading}」：{t.body if (t := page.texts.get(s.id)) else ''}"
                      for s in locked)
    return f"这些段学习者锁定了，会原样留在页上，你不用写，也不要重复它们讲过的内容：\n{lines}"


class _Progress:
    """Commit as you go (stage 4 T3) on a first write (`live`; a page with its finished text keeps
    it until the final commit): the outline plan_page accepted, then each section's text as the
    writer writes it, each in its own commit under the run's `WritingMark` (the page is not
    taught, read or done until the final commit clears it).

    `base` is the page content (`content`) this write stands on; it moves to each commit of ours,
    so another flow's change to the outline or the texts is still a `PageChanged`. A commit that
    loses a race (`WikiConflict`) is retried on a fresh snapshot, COMMIT_TRIES in all. Every load
    and commit goes through the run's repo (`DurableRepo`: each a recorded step, the commit
    keyed exactly-once), and the commits are derived from the workspace alone, so a replayed run
    re-derives the same commits and gets the recorded ones back. A commit that fails is kept in
    `failure` and sets `halt` (the writer's cancel event): the writer stops before its next model
    call, and the run fails with that error (the listener cannot raise into the writer: the
    workspace and plan_page log a listener's exception and go on)."""

    def __init__(self, repo: Repo, user_id: str, page_id: str, *, base: tuple | None,
                 now: datetime, new_id: Ids, mark: WritingMark, domain: str | None,
                 live: bool = True):
        self.live = live                    # False: commits nothing (a page with a body)
        self.repo, self.user_id, self.page_id = repo, user_id, page_id
        self.base, self.now, self.new_id, self.mark, self.domain = base, now, new_id, mark, domain
        self.halt = Event()
        self.failure: Exception | None = None
        self.planned: Page | None = None
        self.texts: dict[str, tuple[str, str, str]] = {}    # sid -> (heading, cue, body) committed

    def outline(self, page: Page) -> None:
        """plan_page accepted `page` (its outline, no texts): stored as the page now. Check answers,
        change marks and the 「自你上次来」 note of an earlier text go (the items are new, I1)."""
        self.planned, self.texts = page, {}
        self._commit(lambda cur: {"page": page, "changed": {}, "checks": {}, "diff": None,
                                  "domain": self.domain}, "outlined", untrash=True)

    def section(self, ws: Workspace) -> None:
        """The workspace changed: the sections whose text (heading, cue, body without its
        interactives: `stream_body(blocks=False)`) differs from what is stored are committed."""
        if self.planned is None or self.failure is not None:
            return
        texts = {sid: (s.heading, s.cue, stream_body(ws, sid, blocks=False))
                 for sid, s in ws.sections.items()}
        if texts == self.texts:
            return
        page, _ = merge_sections(self.planned, ws.sections, lambda s: SectionText(
            section_id=s.section_id, cue=s.cue, answer=s.answer, body=texts[s.section_id][2],
            cache_key=f"writing:{self.mark.job_id}:{s.section_id}"))
        self.texts = texts
        self._commit(lambda cur: {"page": page}, "section_written")

    def check(self, cost_usd: float = 0.0) -> None:
        """Raises the failure of a commit of ours, carrying `cost_usd` (what the writer spent
        until it stopped: the run records it)."""
        if self.failure is not None:
            self.failure.cost_usd = cost_usd   # type: ignore[attr-defined]
            raise self.failure

    def _commit(self, update: Callable[[PageRecord], dict], action: str, *,
                untrash: bool = False) -> None:
        if self.failure is not None or not self.live:
            return
        # (a DBOS cancellation is a BaseException: it is not caught here and stops the run)
        try:
            self._try(update, action, untrash)
        except (WikiConflict, PageChanged) as exc:
            self.fail(exc)
        except Exception as exc:     # any store failure stops the write (fail closed)
            log.exception("write run %s: commit %s failed", self.mark.job_id, action)
            self.fail(exc)

    def fail(self, exc: Exception) -> None:
        """The write stops before the writer's next call and fails with `exc` (the first one)."""
        if self.failure is None:
            self.failure = exc
        self.halt.set()

    def _try(self, update: Callable[[PageRecord], dict], action: str, untrash: bool) -> None:
        for _ in range(COMMIT_TRIES):
            snap = self.repo.load(self.user_id)
            cur = snap.state.pages.get(self.page_id)
            if cur is None or content(cur) != self.base:
                raise PageChanged("页面在写的时候被别的流程改过，这次没有写完")
            record = cur.model_copy(update={**update(cur), "writing": self.mark})
            wiki = [WikiEvent(event_id=self.new_id("wev"), ts=self.now,
                              body=PageSaved(record=record, action=action))]
            if untrash and self.page_id in snap.state.trashed_pages:   # written again
                wiki.append(WikiEvent(event_id=self.new_id("wev"), ts=self.now,
                                      body=PagesTrashed(page_ids=(self.page_id,), trashed=False)))
            try:
                done = self.repo.commit(snap, Commit(wiki=wiki))
            except WikiConflict:
                continue
            self.base = content(done.state.pages[self.page_id])
            return
        raise WikiConflict()


def write_page(run: PageRun, repo: Repo, gw: CountingGateway, *, now: datetime, new_id: Ids,
               writing: Writing | None) -> None:
    """The work of a page run (module doc): the page is written and committed. A page in the
    trash (the user undid the Bot's write, or asks for it again) comes back with the write.

    The page job's one budget (「每页最多花」, `RefineSettings.per_page_limit_usd`; None = no cap)
    is made here, around everything the run calls: `gw.budgeted` for any runtime call (none
    before the writer since stage 4: the domain is an embedding pick), and the writer (plan_page
    and every write turn) gets the same budget."""
    page_id, user_id = run.page_id, run.user_id
    scope = ExitStack()
    try:
        if writing is None:
            raise WriterMissing("没有配置写作模型，不能写页面")
        snap = repo.load(user_id)
        state = snap.state
        budget = JobBudget(state.refine.per_page_limit_usd)
        scope.enter_context(gw.budgeted(budget))
        rec = state.pages[page_id]
        # a first write: no page yet, or the sections an unfinished first write left (T3; only
        # a first write is committed as it goes, so the mark means exactly that)
        first = rec.page is None or rec.writing is not None
        base = content(rec)
        kview = snap.kview(now)
        domain = rec.domain
        current = effective_spec(state)
        run.emit("planning", PlanningEvent(pass_=1))      # no silent start
        # only when the spec has domain items and the page has none of them (none stored, or a
        # stored one no spec item names any more, e.g. after a rename); a step: it embeds
        if not _domain_current(domain, current):
            domain = durable.step("pick_domain", lambda: pick_domain(
                state, current, page_id, user_id=user_id, embedder=writing.embedder))
        teach = page_teach(state, page_id, domain)
        spec = page_spec(state, snap.k, page_id, user_id=user_id, teach=teach)
        if first:                                    # a merged page: the originals are its input
            spec = merge_spec(spec, rec)
        inp = refine_input(snap, page_id, now, writing.specs, domain=domain, mode="page",
                           outline_spec=spec)
        if first and rec.merged_from:
            inp = dataclasses.replace(inp, instruction=merge_instruction(rec))
        elif rec.page is not None and (held := locked_instruction(rec.page)):
            inp = dataclasses.replace(inp, instruction=held)
        inp = read_sources(inp, rec.concepts, writing, state)
        stream = _Stream(run, state, kview, [], user_id=user_id)
        held: list[WriterEvent] = []          # the writer's events before the outline is out

        def on_writer(model: str, fb: bool, why: str) -> None:
            ev = WriterEvent(model=model, fallback=fb, reason=why)
            if stream.outline_ids:
                run.emit("writer", ev)
            else:
                held.append(ev)

        def on_outline(page: Page) -> None:
            outline = page.outline
            stream.outline_ids = [s.id for s in outline.sections]
            run.emit("outline", OutlineEvent(
                page_id=page_id, source=outline.source, profile=outline.profile,
                assumed=cells_out(state, kview, outline.assumed, user_id=user_id),
                sections=[section_out(state, kview, s, page.states[s.id], None, user_id=user_id)
                          for s in outline.sections]))
            for ev in held:
                run.emit("writer", ev)
            held.clear()

        # commit as you go only on a first write: a page that has its finished text keeps it
        # stored until the final commit (fail closed: a failed reorganize loses nothing); the
        # stream shows the progress either way
        progress = _Progress(repo, user_id, page_id, base=base, now=now, new_id=new_id,
                             mark=WritingMark(job_id=run.run_id), domain=domain, live=first)

        def on_change(ws: Workspace, sid: str) -> None:
            progress.section(ws)            # stored first: whatever was shown is on the page
            stream.on_change(ws, sid)

        def on_planned(page: Page) -> None:
            progress.outline(page)
            try:
                on_outline(page)
            except Exception as exc:      # the outline did not reach the stream: fail visibly
                log.exception("write run %s: the outline event failed", run.run_id)
                progress.fail(exc)

        try:
            result = writing.writer.write(
                inp, budget=budget, on_writer=on_writer, on_change=on_change,
                on_outline=on_planned, reader=writer_reader(inp, writing),
                cancel=progress.halt, on_spend=lambda cost, n: run.report_spend(cost, 0, n))
        except RefineFailed as exc:
            # a commit of ours stopped the writer: that error, with what the writer spent
            progress.check(getattr(exc, "cost_usd", 0.0))
            raise
        progress.check(result.cost_usd)
        base = progress.base
        run.report_spend(result.cost_usd, 0, result.calls)
        fresh = result.outcome.page
        if fresh is None:                    # plan_page sets it before any write: fail closed
            raise RefineFailed("internal", "写作者没有定下大纲")
        # citations checked against the materials before anything is stored (Task 6b)
        draft, cite_issues = check_citations(result.outcome.draft, inp, writing)
        if draft.needs_assets and writing.assets is None:
            raise RefineFailed("internal", "没有交互资源库，不能提交带交互的页面")
        stored = make_assets(draft, result.outcome.checks, user_id=user_id, job_id=run.run_id,
                             now=now, new_id=new_id)
        extra = [writing.assets.writer(stored)] if stored and writing.assets is not None else []

        def events_for(cur: PageRecord, cur_state: WikiState) -> tuple[list[WikiEvent], Page]:
            record = written_record(cur, draft, stored, job_id=run.run_id, now=now,
                                    model=result.model, spec_version=inp.teaching.version,
                                    page=fresh, cost_usd=result.cost_usd,
                                    fallback_reason=result.fallback_reason, kind="page",
                                    sources=page_sources(inp), cite_issues=cite_issues)
            record = record.model_copy(update={
                "diff": None, "domain": domain, "writing": None,     # written: taught from now
                "checks": {},     # check items are new: no stored answer survives (I1)
                # a reference-card page is folded again on its next open (M3)
                "collapse_applied": False if cur.shelved else cur.collapse_applied})
            assert record.page is not None
            written = set(record.written.sections if record.written else ())
            order = {s.id: i for i, s in enumerate(record.page.outline.sections)}
            logs = sorted(
                [*section_logs(record.page.outline, order.keys() - written, producer="organizer",
                               spec=spec, k=kview, teach=teach, at=now,
                               infer_version=snap.infer_version),
                 *section_logs(record.page.outline, written, producer="writer", spec=spec,
                               k=kview, teach=teach, at=now, infer_version=snap.infer_version)],
                key=lambda g: order[g.section_id])   # page order; checks are the organizer's
            wiki = [WikiEvent(event_id=new_id("wev"), ts=now, body=PageSaved(
                record=record, action="organized" if first else "reorganized"))]
            if page_id in cur_state.trashed_pages:        # written again: out of the trash
                wiki.append(WikiEvent(event_id=new_id("wev"), ts=now,
                                      body=PagesTrashed(page_ids=(page_id,), trashed=False)))
            wiki += logs_event(logs, new_id=new_id, now=now)
            update = result.outcome.series_update
            on_path = path_of(cur_state, page_id)
            if update is not None and on_path is not None:    # the state merges it (any order)
                wiki.append(WikiEvent(event_id=new_id("wev"), ts=now, body=SeriesUpdated(
                    path_id=on_path[0], page_id=page_id, title=record.title, update=update)))
            return wiki, record.page

        committed: Page | None = None
        for _ in range(COMMIT_TRIES):
            cur_snap = repo.load(user_id)
            cur = cur_snap.state.pages.get(page_id)
            if cur is None or content(cur) != base:
                raise PageChanged("页面在写的时候被别的流程改过，这次没有写入")
            wiki, committed = events_for(cur, cur_snap.state)
            try:
                repo.commit(cur_snap, Commit(wiki=wiki, extra=extra))
                break
            except WikiConflict:
                continue
        else:
            raise WikiConflict()
    except (WikiConflict, PageChanged, WriterMissing, SourcesFailed, RefineFailed, SpecInvalid,
            *LLM_FAILURES, *LLM_UNAVAILABLE, *LLM_BUDGET) as exc:
        # a failed run records what it spent: the writer's turns
        run.spent[0] = float(getattr(exc, "cost_usd", 0.0) or 0.0)
        run.emit("error", _error(exc))
        return
    finally:
        scope.close()
    try:                                   # committed: a display problem must not say "error"
        stream.final(committed)
    except Exception:
        log.exception("write run %s: final section events failed", run.run_id)
    run.emit("done", OrganizeDoneEvent(
        page_id=page_id, llm_calls=result.calls,
        cost_usd=result.cost_usd,
        model=result.model,
        fallback=bool(result.fallback_reason), prefetched=run.origin == "idle"))
