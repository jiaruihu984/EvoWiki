"""The page run `source_revise` (materials spec 2026-10-20 §7 D): the page writer rewrites a whole page
from the learner's materials (a pasted text or URL is imported as a material first), and the result goes into the `page_revise`
ledger row that asked for it (`wikiapp.revise`, `bot.executors_revise`) as a draft. The page is never
committed here: the learner confirms or rejects the whole draft through the ledger.

The run, as durable steps where it reads or writes anything outside its body:

1. the row: the user's `page_revise` row, still `proposed` and `content.state == "writing"`, for
   this page; anything else ends the run with `internal` and leaves the row alone.
2. the page: a user note is never revised; a page that changed since the row was proposed fails the
   row (its draft could never be approved: the executor checks `page_version`).
3. the materials: the row's own, or - when it names none - at most `PICK_MAX` readable materials
   with a lexical hit on the page's title or a concept name (none → the row fails). A named one still
   `reading` is waited for (`REVISE_POLL_S`, at most `REVISE_WAIT_S`); a failed or deleted one
   fails the row.
   When no material is named but the page's concepts are linked to material sections
   (`source_link`, phases 3+4), the linked materials are the ones used and nothing is picked.
4. the excerpts (`sources.excerpts.SourceExcerpt`, the same shape a page written from materials
   gets): first the sections the page's concepts are linked to (`Writing.sources.for_nodes`;
   with named materials, only theirs; a linked material not readable now - still `reading`,
   `failed` - is left out); a material with no linked section falls back to the
   sections its search finds for the page (title + section headings), in reading order (no hit:
   its first sections, at most `full_text_tokens()`), each its full text when they fit
   what the linked sections given in full left of `full_text_tokens()` (one budget for both),
   else its summary (`sources.excerpts.excerpts_for`). Nothing
   usable at all → the row fails.
5. the write: `refine_input` over every non-check section, every one of them required (the draft
   is a whole-page swap: a section the writer leaves out would be gone). At `REVISE_REBUILD_AFTER`
   revisions the writer starts from the outline only (`mode="page"`, no old text). The excerpts
   go in as `RefineInput.sources` (the fenced 「资料」 block, deep@v18) with the material tools
   (`sources_readable`: read_source / search_source over the run's reader), the revision rules as
   `RefineInput.revise`; sections stream as `section_delta` / `section_done` like a rewrite, so
   the plugin can show the draft as it is written.
6. the citations (one step): `wikiapp.refine_apply.checked_citations` over the draft, as a page written
   from materials is checked before it is stored (`sources.cite.verify`): a citation whose quote is
   on its page becomes `〔名 p.印码｜N〕`, any other is removed; a ⟦和资料不同：…⟧ whose quote is
   not in the excerpts becomes ⟦待查证：…⟧. The issues go into `RevisionDraft.cite_issues` (the
   plugin's ? says how many were removed); the pages come from the run's reader. A read that fails
   fails the row.
7. the row is filled (`ActionLedger.fill_draft`, one step): `state="ready"` with the
   `RevisionDraft`, or `state="failed"` with the reason in `detail` (Chinese, shown in the circled
   ?). Every failure ends there (fail closed): the row never stays "writing" after its run ended.

`start_revise` makes the row and starts the run (the page route, the Bot's 更新 and the assistant's
tool share it): one open draft per page (a second one is `RevisePending`, checked and proposed under
one lock); the run id goes into the row's content once the run started."""

import dataclasses
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from evowiki.agent import durable
from evowiki.deep.agent import RefineFailed, ReviseContext
from evowiki.deep.contracts import RefineDraft
from evowiki.goals.contracts import SectionRole
from evowiki.llm.budget import JobBudget
from evowiki.llm.providers import ProviderRejectedError
from evowiki.sources.cite import CiteIssue
from evowiki.sources.config import full_text_tokens
from evowiki.sources.excerpts import DocumentSource, SourceExcerpt, excerpts_for, pages_of
from evowiki.sources.models import LinkRow
from evowiki.store import bot as bot_store
from evowiki.wikiapp.durable_repo import DurableRepo
from evowiki.wikiapp.kwrite import concept_name
from evowiki.wikiapp.llm import LLM_BUDGET, LLM_FAILURES, LLM_UNAVAILABLE, PROVIDER_REJECTED_DETAIL
from evowiki.wikiapp.organize import PageChanged, SourcesFailed, WriterMissing, _error, _Stream
from evowiki.wikiapp.page_runs import PageJob, PageRun, ReviseArgs
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.refine_apply import checked_citations, refine_input
from evowiki.wikiapp.refine_service import STILL_WRITING
from evowiki.wikiapp.repo import WikiConflict, WikiInvalid
from evowiki.wikiapp.revise import (
    KIND,
    REVISE_REBUILD_AFTER,
    RevisionDraft,
    RevisionSource,
)
from evowiki.wikiapp.teachspec import SpecInvalid
from evowiki.wikiapp.wiki_schemas import ErrorEvent, RewriteDoneEvent, WriterEvent

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from evowiki.bot.actions import ActionLedger
    from evowiki.sources.excerpts import SourceExcerpts
    from evowiki.sources.models import SectionRow
    from evowiki.sources.reader import SourceReader
    from evowiki.store.bot import ActionRow
    from evowiki.wikiapp.events import PageRecord
    from evowiki.wikiapp.page_flows import PageDeps
    from evowiki.wikiapp.page_runs import PageRuns
    from evowiki.wikiapp.repo import Repo

log = logging.getLogger(__name__)

REVISE_WAIT_S = 600              # how long a material still being read is waited for
REVISE_POLL_S = 5
SEARCH_K = 6                     # search hits per material (one without linked sections)
PICK_K = 20                      # search hits when the run picks the materials itself
PICK_MAX = 3                     # materials it picks at most

# the row's `detail` (shown in the circled ?)
NOT_READ = "资料还没读完，这次没改"
UNREADABLE = "资料《{title}》打不开，这次没改"
SOURCE_GONE = "有一份资料已经删掉了，这次没改"
NO_READER = "读不到资料，这次没改"
NO_MATCH = "资料里没找到讲这一页的内容"
CITE_READ = "核对出处时读不到资料，这次没改"
NO_LEDGER = "没有账本，不能出草稿"
NOT_WAITING = "这条草稿不在等着写"
USER_NOTE = "这是你自己的笔记，EvoWiki 不改"
TRASHED = "这一页在回收站里，先恢复再改"
PAGE_GONE = "这一页已经不在了"
PAGE_MOVED = "这一页在写草稿的时候被改过，这次没改"
UNAVAILABLE = "请切换模型重试"
FAILED = "草稿没写成，这次没改"
INTERNAL = "出错了，这次没改"
LOST = "草稿写好了，但这条修改已经不在等着了"
STUCK = "草稿没写完就停下了，这次没改"

REWRITE = "按资料改这一页：每一段都要写（资料没讲到的段保留原页的讲法），不删段。"
REBUILD = "按大纲和资料从头写这一页的每一段。"


class _Stop(Exception):
    """The run ends here: the row fails with `detail`, the stream with `event`."""

    def __init__(self, detail: str, event: ErrorEvent):
        super().__init__(detail)
        self.detail, self.event = detail, event


def _stop(detail: str, code: str, *, retryable: bool = False) -> _Stop:
    return _Stop(detail, ErrorEvent(code=code, retryable=retryable, detail=detail))  # type: ignore[arg-type]


# ---- reads (each a durable step: JSON out) ----------------------------------------------------

def _row(deps: "PageDeps", user_id: str, action_id: str) -> dict | None:
    assert deps.ledger is not None
    row = deps.ledger.get(user_id, action_id)
    if row is None:
        return None
    return {"kind": row.kind, "status": row.status, "page_id": row.page_id,
            "page_version": row.page_version, "content": row.content}


def _statuses(reader: "SourceReader", user_id: str, source_ids: tuple[str, ...]) -> dict:
    """{source_id: [title, status]}; a deleted or unknown material is absent."""
    rows = {s.source_id: s for s in reader.list(user_id)}
    return {sid: [rows[sid].title, rows[sid].status] for sid in source_ids if sid in rows}


def _pick(reader: "SourceReader", user_id: str, phrases: list[str]) -> list[str]:
    """At most `PICK_MAX` materials, readable (`ready` / `partial`) and not deleted, that talk
    about the page: a lexical hit on its title or one of its concept names (`reader.lexical`; a
    vector neighbour alone never counts, there always is one), in hit order."""
    readable = {s.source_id for s in reader.list(user_id) if s.status in ("ready", "partial")}
    hits = reader.lexical(user_id, phrases, None, PICK_K)
    return list(dict.fromkeys(h.source_id for h in hits if h.source_id in readable))[:PICK_MAX]


def _leading(secs: "list[SectionRow]", budget: int) -> "list[SectionRow]":
    """A material's first sections with text, as many as fit in `budget` tokens together."""
    out, used = [], 0
    for s in secs:
        if not s.text.strip():
            continue
        if used + s.tokens > budget:
            break
        out.append(s)
        used += s.tokens
    return out


def _excerpt_json(e: SourceExcerpt) -> dict:
    return dataclasses.asdict(e)


def _excerpt(d: dict) -> SourceExcerpt:
    return SourceExcerpt(**{**d, "glossary": tuple(d.get("glossary") or ())})


def _linked(links: "SourceExcerpts | None", reader: "SourceReader", user_id: str,
            concepts: list[str]) -> list[dict]:
    """The sections the page's concepts are linked to (`source_link`) in materials readable now
    (`ready` / `partial`), as [excerpt JSON, tokens it takes of the budget (0 for a summary)];
    [] without a links reader or concepts."""
    if links is None or not concepts:
        return []
    readable = {s.source_id for s in reader.list(user_id) if s.status in ("ready", "partial")}
    got = [e for e in links.for_nodes(user_id, concepts) if e.source_id in readable]
    sizes: dict[tuple[str, str], int] = {}
    for sid in dict.fromkeys(e.source_id for e in got if e.text is not None):
        sizes.update({(sid, s.section_id): s.tokens for s in reader.sections(user_id, sid)})
    return [[_excerpt_json(e), sizes.get((e.source_id, e.section_id), 0) if e.text is not None
             else 0] for e in got]


def _searched(reader: "SourceReader", user_id: str, source_ids: tuple[str, ...],
              query: str, used: int) -> list[dict]:
    """The fallback for materials with no linked section (module doc, 4), as JSON excerpts; given
    in full only when they fit what the linked sections (`used` tokens) left of the budget."""
    rows = {s.source_id: s for s in reader.list(user_id)}
    by_source: dict = {}
    links: list[LinkRow] = []
    for sid in source_ids:
        if sid not in rows:
            continue
        secs = reader.sections(user_id, sid)
        by_id = {s.section_id: s for s in secs}
        hit = dict.fromkeys(h.section_id for h in reader.search(user_id, query, [sid], SEARCH_K)
                            if h.section_id in by_id)
        # nothing found: the material's opening, at most `full_text_tokens()` of it
        chosen = [by_id[x] for x in hit] or _leading(secs, full_text_tokens())
        if chosen:
            by_source[sid] = (rows[sid], secs)
            links += [LinkRow(node_id="", section_id=s.section_id, source_id=sid, role="teaches",
                              by="curator") for s in sorted(chosen, key=lambda s: s.ord)]
    budget = max(full_text_tokens() - used, 0)
    return [_excerpt_json(e) for e in excerpts_for(by_source, links, budget=budget)]


def revision_sources(excerpts: "Sequence[SourceExcerpt]") -> tuple[RevisionSource, ...]:
    """Per material, in first-use order: its title then and the sections the draft was given."""
    out: dict[str, RevisionSource] = {}
    for e in excerpts:
        have = out.get(e.source_id)
        ids = (*have.section_ids, e.section_id) if have is not None else (e.section_id,)
        out[e.source_id] = RevisionSource(source_id=e.source_id, title=e.source_title,
                                          section_ids=tuple(dict.fromkeys(ids)),
                                          page_offset=e.page_offset)
    return tuple(out.values())


class _ReaderPages:
    """`checked_citations`' page reader over the run's `SourceReader`: the PDF pages of a live
    material of the user, from its sections' 〔p.N〕 marks (LookupError otherwise: fail closed)."""

    def __init__(self, reader: "SourceReader"):
        self._reader = reader

    def for_nodes(self, user_id: str, node_ids: "Sequence[str]",
                  parents: "Mapping[str, Sequence[str]] | None" = None) -> list[SourceExcerpt]:
        return []      # never called: `checked_citations` only reads pages (the protocol wants it)

    def document(self, user_id: str, source_id: str):
        return (self._reader.document(user_id, source_id)
                if isinstance(self._reader, DocumentSource) else None)

    def page_texts(self, user_id: str, source_id: str, pages: "Sequence[int]") -> dict[int, str]:
        if source_id not in {s.source_id for s in self._reader.list(user_id)}:
            raise LookupError(f"没有这份资料：{source_id}")
        return pages_of(self._reader.sections(user_id, source_id), pages)


def _checked(d: RefineDraft, inp, reader: "SourceReader") -> dict:
    """{"draft": RefineDraft JSON, "issues": [CiteIssue JSON]} (module doc, 6)."""
    try:
        draft, issues = checked_citations(d, inp, _ReaderPages(reader))
    except Exception as exc:
        raise SourcesFailed(f"出处核对时读资料失败：{type(exc).__name__}: {exc}") from exc
    return {"draft": draft.model_dump(mode="json"),
            "issues": [i.model_dump(mode="json") for i in issues]}


# ---- the run ---------------------------------------------------------------------------------

def _caused_by(exc: BaseException | None, cls: type[BaseException]) -> bool:
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, cls):
            return True
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return False


def _row_detail(exc: BaseException, event: ErrorEvent) -> str:
    """What the row says when the write failed (the stream keeps `_error`'s technical detail)."""
    if isinstance(exc, PageChanged):
        return PAGE_MOVED
    if isinstance(exc, SourcesFailed):
        return CITE_READ
    if event.code == "budget_exceeded":
        return event.detail
    if event.code in ("llm_busy", "llm_timeout"):
        return event.detail                 # 「请切换模型重试」 / 「请切换模型重试」
    if event.code == "llm_unavailable":
        return (f"{PROVIDER_REJECTED_DETAIL}，这次没改" if _caused_by(exc, ProviderRejectedError)
                else UNAVAILABLE)
    return FAILED


def run_revise(job: PageJob, run: PageRun, deps: "PageDeps", *,
               reader: "SourceReader | None") -> None:
    """The body of a `source_revise` run (module doc). Always ends the stream with `done` or
    `error`; never raises for a failed write (the row says why)."""
    args = job.revise
    if args is None:
        raise ValueError("a source_revise run carries its ledger row")
    ledger = deps.ledger
    if ledger is None:
        run.emit("error", ErrorEvent(code="internal", retryable=False, detail=NO_LEDGER))
        return
    uid, aid = job.user_id, args.action_id
    row = durable.step("revise.row", lambda: _row(deps, uid, aid))
    if (row is None or row["kind"] != KIND or row["status"] != "proposed"
            or row["page_id"] != job.page_id or row["content"].get("state") != "writing"):
        run.emit("error", ErrorEvent(code="internal", retryable=False, detail=NOT_WAITING))
        return
    content: dict[str, Any] = row["content"]

    def fill(update: dict[str, Any]) -> bool:
        new = {**content, **update}
        return bool(durable.step("revise.fill", lambda: ledger.fill_draft(uid, aid, new)))

    try:
        done = _write(job, run, deps, reader, args, row["page_version"])
    except _Stop as s:
        fill({"state": "failed", "detail": s.detail, "draft": None})
        run.emit("error", s.event)
        return
    except Exception as exc:     # fail closed: the row never stays "writing" after its run
        log.exception("source_revise %s of %s/%s crashed", aid, uid, job.page_id)
        fill({"state": "failed", "detail": INTERNAL, "draft": None})
        run.emit("error", ErrorEvent(code="internal", retryable=True,
                                     detail=f"{type(exc).__name__}: {exc}"[:500]))
        return
    draft, result = done
    if not fill({"state": "ready", "detail": "", "draft": draft.model_dump(mode="json"),
                 "cost_usd": result.cost_usd, "model": result.model}):
        # rejected (or filled) while it was written: the draft is dropped, nothing else changed
        log.warning("source_revise %s: row no longer waits for its draft", aid)
        run.emit("error", ErrorEvent(code="conflict", retryable=False, detail=LOST))
        return
    run.emit("done", RewriteDoneEvent(page_id=job.page_id, llm_calls=result.calls,
                                      cost_usd=result.cost_usd, model=result.model,
                                      fallback=bool(result.fallback_reason)))


def _wait_for_sources(job: PageJob, deps: "PageDeps", reader: "SourceReader",
                      source_ids: tuple[str, ...]) -> None:
    started = durable.now(deps.clock)
    while True:
        got = durable.step("revise.sources",
                           lambda: _statuses(reader, job.user_id, source_ids))
        if any(sid not in got for sid in source_ids):
            raise _stop(SOURCE_GONE, "not_found")
        if bad := [title for title, status in got.values() if status == "failed"]:
            raise _stop(UNREADABLE.format(title=bad[0]), "invalid")
        if all(status != "reading" for _, status in got.values()):
            return
        if (durable.now(deps.clock) - started).total_seconds() >= REVISE_WAIT_S:
            raise _stop(NOT_READ, "conflict", retryable=True)
        durable.sleep(REVISE_POLL_S, deps.sleep)


def _write(job: PageJob, run: PageRun, deps: "PageDeps", reader: "SourceReader | None", args,
           page_version: str | None):
    """(RevisionDraft, WriteResult); _Stop with the row's reason on any failure."""
    from evowiki.wikiapp.page_flows import durable_writing  # page_flows imports this module

    repo = DurableRepo(deps.repo)
    writing = durable_writing(deps.writing, repo)
    snap = repo.load(job.user_id)
    rec = snap.state.pages.get(job.page_id)
    if rec is None:
        raise _stop(PAGE_GONE, "not_found")
    if rec.page is None:
        raise _stop(USER_NOTE, "invalid")
    if not rec.has_body:              # its first write is running or stopped part-way
        raise _stop(STILL_WRITING, "conflict", retryable=True)
    base = page_fingerprint(rec)
    if base != page_version:     # the executor would refuse the draft as stale: don't write it
        raise _stop(PAGE_MOVED, "conflict", retryable=True)
    page = rec.page
    scope = tuple(s.id for s in page.outline.sections if s.role is not SectionRole.CHECK)
    query = " ".join([rec.title, *(s.heading for s in page.outline.sections
                                   if s.role is not SectionRole.CHECK)])
    phrases = [rec.title, *(concept_name(snap.state, c, user_id=snap.user_id)
                            for c in rec.concepts)]
    if reader is None:
        raise _stop(NO_READER, "internal")
    links = writing.sources if writing is not None else None
    source_ids = args.source_ids
    if source_ids:
        _wait_for_sources(job, deps, reader, source_ids)
    try:
        linked_sized = [(_excerpt(d), n) for d, n in durable.step(
            "revise.linked", lambda: _linked(links, reader, job.user_id, list(rec.concepts)))]
    except Exception as exc:     # fail closed: never revised without the sections it maps to
        log.exception("source_revise %s: the linked sections could not be read", args.action_id)
        raise _stop(INTERNAL, "internal", retryable=True) from exc
    if source_ids:
        linked_sized = [(e, n) for e, n in linked_sized if e.source_id in source_ids]
    linked = [e for e, _ in linked_sized]
    used = sum(n for _, n in linked_sized)     # the linked sections given in full
    if not source_ids and linked:      # none named: the materials the page is linked to
        source_ids = tuple(dict.fromkeys(e.source_id for e in linked))
    elif not source_ids:     # none named, none linked: the materials that talk about this page
        source_ids = tuple(durable.step("revise.pick", lambda: _pick(reader, job.user_id, phrases)))
        if not source_ids:
            raise _stop(NO_MATCH, "not_found")
    have = {e.source_id for e in linked}
    unlinked = tuple(sid for sid in source_ids if sid not in have)
    searched = [_excerpt(d) for d in durable.step(
        "revise.excerpts", lambda: _searched(reader, job.user_id, unlinked, query, used))
                ] if unlinked else []
    excerpts = (*linked, *searched)
    if not excerpts:                 # nothing usable in the materials (e.g. only empty sections)
        raise _stop(NO_MATCH, "not_found")
    rebuild = rec.revisions >= REVISE_REBUILD_AFTER
    try:
        if writing is None:
            raise WriterMissing("没有配置写作模型，不能改写")
        if deps.gateway(job.user_id) is None:
            raise WriterMissing("请切换模型重试")
        target = page.model_copy(update={"texts": {}}) if rebuild else page
        now = job.now
        inp = refine_input(snap, job.page_id, now, writing.specs, scope=scope, page=target,
                           mode="page" if rebuild else "rewrite",
                           instruction=REBUILD if rebuild else REWRITE)
        inp = dataclasses.replace(inp, required=scope, revise=ReviseContext(rebuild=rebuild),
                                  sources=excerpts, sources_readable=True)
        stream = _Stream(run, snap.state, snap.kview(now), [s.id for s in page.outline.sections],
                         user_id=snap.user_id)
        result = writing.writer.write(
            inp, budget=JobBudget(snap.state.refine.per_page_limit_usd),
            on_writer=lambda model, fb, why: run.emit("writer", WriterEvent(
                model=model, fallback=fb, reason=why)),
            on_change=stream.on_change, reader=reader)
        outcome = result.outcome
        got = durable.step("revise.cite", lambda: _checked(outcome.draft, inp, reader))
        d = RefineDraft.model_validate(got["draft"])
        if d.needs_assets and writing.assets is None:
            raise RefineFailed("internal", "没有交互资源库，不能出带交互的草稿")
        draft = RevisionDraft(
            page_id=job.page_id, base_version=base, draft=d,
            sources=revision_sources(excerpts), rebuild=rebuild,
            checks=tuple(outcome.checks.values()),
            cite_issues=tuple(CiteIssue.model_validate(i) for i in got["issues"]))
        cur = repo.load(job.user_id).state.pages.get(job.page_id)
        if page_fingerprint(cur) != base:
            raise PageChanged("这一页在写草稿的时候被别的流程改过")
    except (PageChanged, WriterMissing, SourcesFailed, RefineFailed, SpecInvalid, *LLM_FAILURES,
            *LLM_UNAVAILABLE, *LLM_BUDGET) as exc:
        run.spent[0] = float(getattr(exc, "cost_usd", 0.0) or 0.0)
        event = _error(exc)
        raise _Stop(_row_detail(exc, event), event) from exc
    return draft, result


# ---- starting one (the HTTP routes, the assistant's revise tool, the Bot's 更新) ---------------

NOT_STARTED = "草稿没能开始写，这次没改"
BUSY = "这一页已经有一份草稿了"
NO_SOURCE = "找不到这份资料"


class RevisePending(Exception):
    """The page already has an open draft (being written or waiting for the learner)."""

    def __init__(self, row: "ActionRow"):
        super().__init__(BUSY)
        self.row = row


# a `writing` row without a run id this long after it was made lost its start (the process died
# between the proposal and `runs.start`); younger, it is a start that is about to write the id
NO_RUN_GRACE = timedelta(seconds=60)


def _stuck(row: "ActionRow", runs: "PageRuns", now: "datetime") -> bool:
    """A `writing` row whose run can no longer fill it: no run id past the grace, or a run that is
    not writing any more (it ended, was cancelled or swept, or its row is gone)."""
    if row.content.get("state") != "writing":
        return False
    run_id = row.content.get("run_id")
    if not run_id:
        return now - row.created_at > NO_RUN_GRACE
    run = runs.store.get(str(run_id))
    return run is None or run.status != "writing"


def open_draft(ledger: "ActionLedger", runs: "PageRuns", user_id: str, page_id: str,
               now: "datetime") -> "ActionRow | None":
    """The page's open draft (`ActionLedger.open_page_revise`). A `writing` one that can never be
    filled (`_stuck`) is failed (`STUCK`, fail closed) instead of blocking the page forever."""
    while True:
        row = ledger.open_page_revise(user_id, page_id)
        if row is None or not _stuck(row, runs, now):
            return row
        log.warning("page_revise %s of %s/%s: its run can no longer fill it, failed", row.action_id,
                    user_id, page_id)
        if not ledger.fill_draft(user_id, row.action_id, {"state": "failed", "detail": STUCK}):
            return ledger.open_page_revise(user_id, page_id)    # moved meanwhile: as it is now


def revisable(repo: "Repo", ledger: "ActionLedger", runs: "PageRuns", user_id: str, page_id: str,
              now: "datetime") -> "PageRecord":
    """The page, when a revision can start: LookupError when it is not there, WikiInvalid for a
    user note (never revised) or a page in the trash, WikiConflict for a page still being
    written, RevisePending when it has an open draft."""
    state = repo.load(user_id).state
    rec = state.pages.get(page_id)
    if rec is None:
        raise LookupError(f"没有这个页面：{page_id}")
    if rec.page is None:
        raise WikiInvalid(USER_NOTE, public_message="EvoWiki does not modify your own notes.", code="user_note_readonly")
    if not rec.has_body:
        raise WikiConflict(STILL_WRITING)
    if page_id in state.trashed_pages:
        raise WikiInvalid(TRASHED, public_message="Restore this page from the trash before editing it.", code="page_trashed")
    open_row = open_draft(ledger, runs, user_id, page_id, now)
    if open_row is not None:
        raise RevisePending(open_row)
    return rec


def start_revise(*, ledger: "ActionLedger", repo: "Repo", runs: "PageRuns",
                 deps: "Callable[[], PageDeps]", user_id: str, page_id: str,
                 source_ids: "Sequence[str]", source: str, key: str, now: "datetime",
                 origin: Literal["open", "background"] = "open") -> "tuple[ActionRow, str]":
    """The `page_revise` row (`state="writing"`, always `ask`) and the `source_revise` run that
    fills it: (row, run_id). `source_ids` () = the run picks the materials. A repeat of `key`
    returns its row and run (nothing new starts). Raises as `revisable`, plus LookupError for a
    material that is not the user's live one; a run that does not start fails the row (fail
    closed) and raises."""
    have = ledger.get_by_key(key)
    if have is not None and have.user_id == user_id:
        return have, str(have.content.get("run_id") or "")
    rec = revisable(repo, ledger, runs, user_id, page_id, now)
    page_deps = deps()
    ids = tuple(dict.fromkeys(source_ids))
    if ids:
        if page_deps.sources is None:
            raise LookupError(NO_SOURCE)
        live = {s.source_id for s in page_deps.sources.list(user_id)}
        if any(sid not in live for sid in ids):
            raise LookupError(NO_SOURCE)
    content = {"state": "writing", "confirm": True, "page_id": page_id, "source_ids": list(ids),
               "detail": "", "draft": None}
    row, created = ledger.propose_unless(
        user_id, lambda conn: bot_store.open_page_revise(conn, user_id, page_id),
        kind=KIND, scope="page_body", source=source, summary=f"按资料改「{rec.title}」", why="",
        content=content, key=key, page_id=page_id, page_version=page_fingerprint(rec),
        level="ask")
    if not created:
        raise RevisePending(row)
    job = PageJob(kind="source_revise", user_id=user_id, page_id=page_id, now=now, origin=origin,
                  revise=ReviseArgs(action_id=row.action_id, source_ids=ids))
    try:
        run_id, _ = runs.start(job, page_deps, attach=False)
    except Exception:
        log.exception("source_revise %s of %s/%s: the run did not start", row.action_id, user_id,
                      page_id)
        ledger.fill_draft(user_id, row.action_id, {"state": "failed", "detail": NOT_STARTED})
        raise
    ledger.set_content_keys(user_id, row.action_id, {"run_id": run_id})
    return ledger.get(user_id, row.action_id) or row, run_id
