"""The reading assistant over E1's wiki storage (plan rev 3.2 §9.1): the ports the turn loop is
given (`AssistantData`, `ProposalApplier`), F2's `PageStore`, and the
per-turn tool factory - all reading one `wikiapp.Repo` (PgRepo in the service, MemoryRepo in
tests). Every read loads a snapshot; in the service that repo is a `TurnRepo`, so the reads of one
turn share one snapshot (re-read when the user's logs moved), and nothing outlives the turn.

Question as evidence: the assistant's own `record_evidence` tool (stage 3 T9, built with the action
tools here); there is no observer call after the answer.

Page edits (`WikiApplier`): an accepted proposal is applied as a Phase 1 minimal patch through
goals/patch's own pieces (`apply_ops`, the patch guardrails, `to_outline`) and saved as one
`PageSaved(action="assistant")`. The section text is the assistant's own text (no LLM rewrite).
Anything the patch cannot do raises `ApplyRefused` with a user-facing reason, which the service
records as a `failed` decision."""

import logging
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from psycopg_pool import ConnectionPool

from evowiki.agent import durable
from evowiki.agent.tools import ProposalDraft, Tool
from evowiki.assistant.ports import PageSectionView, PageView
from evowiki.assistant.session import SessionInfo
from evowiki.assistant.tools import VectorContentSearch, build_tools
from evowiki.assistant.tools.fetch_url import Fetcher
from evowiki.assistant.tools.search_wiki import ContentSearch, SegmentHit
from evowiki.assistant.tools.web_search import SearchProvider
from evowiki.belief.aspects import AspectK
from evowiki.bot.actions import STALE_DETAIL
from evowiki.content.loaded import LoadedContent
from evowiki.goals.context import PageDigest, page_summary_of
from evowiki.goals.contracts import Band, PageOutline, SectionRole, SectionText
from evowiki.goals.guardrails import (
    WEAK,
    DraftOutline,
    DraftSection,
    PageSpec,
    Violation,
    check_outline,
    draft_of,
    page_bands,
    section_of,
    to_outline,
)
from evowiki.goals.language import Language
from evowiki.goals.observe import k_text
from evowiki.goals.page_update import DraftOp, _keep_untouched, apply_ops
from evowiki.plain_text import norm_name
from evowiki.retrieval.embed import Embedder
from evowiki.sources.reader import SourceReader
from evowiki.store.users import UnknownUser
from evowiki.wikiapp.durable_repo import DurableRepo
from evowiki.wikiapp.events import (
    PageRecord,
    PageSaved,
    UsageAdded,
    UsageRecord,
    WikiEvent,
    WriteMark,
)
from evowiki.wikiapp.kwrite import page_spec
from evowiki.wikiapp.page_reader import UnwrittenPage, WikiPage, check_status_lines
from evowiki.wikiapp.page_version import page_fingerprint, section_fingerprint
from evowiki.wikiapp.repo import CONFLICT_DETAIL, Commit, Repo, Snapshot, WikiConflict
from evowiki.wikiapp.sectionlog import partial_write
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.teachspec import wiki_texts

if TYPE_CHECKING:
    from evowiki.assistant.tools.actions import ActionDeps

log = logging.getLogger(__name__)

COMMIT_TRIES = 3
APPLY_ACTION = "assistant"           # PageSaved.action of an applied assistant proposal
UNDO_ACTION = "assistant_undo"       # ... and of its undo
UNDO_STALE = "页面在这次修改之后又改过了，不能撤销，避免覆盖新的内容"
UNDO_GONE = "这一页已经不存在或还没有生成，不能撤销"

Clock = Callable[[], datetime]
NewId = Callable[[str], str]


def _load(repo: Repo, user_id: str) -> Snapshot | None:
    """The user's snapshot, or None for a user the wiki does not know."""
    try:
        return repo.load(user_id)
    except UnknownUser:
        return None


def _durable_clock(clock: Clock) -> Clock:
    """The clock as a recorded step inside a durable workflow, a plain read outside one."""
    return lambda: durable.now(clock)


def _names(state: WikiState) -> dict[str, str]:
    return {c: n.name for c, n in state.names.items()}


def _known(k: AspectK, concept_ids: Sequence[str]) -> list[str]:
    """`concept_ids` defined in this user's K, in order, without repeats."""
    defined = set(k.concept_ids())
    return [c for c in dict.fromkeys(concept_ids) if c in defined]


class WikiPages:
    """F2's `PageStore` over the repo: organized pages only (an unorganized page has no text)."""

    def __init__(self, repo: Repo):
        self._repo = repo

    def get(self, user_id: str, page_id: str) -> WikiPage | None:
        snap = _load(self._repo, user_id)
        rec = snap.state.pages.get(page_id) if snap is not None else None
        if rec is None or rec.page is None:
            return None
        return WikiPage(page_id=rec.page_id, title=rec.title, page=rec.page,
                        writing=rec.writing is not None, check_answers=rec.checks)

    def unwritten(self, user_id: str, page_id: str) -> UnwrittenPage | None:
        """A page of the Wiki with no outline yet (a path page not written): its title, goal and
        points, so read_page can still say what it is meant to cover."""
        snap = _load(self._repo, user_id)
        st = snap.state if snap is not None else None
        rec = st.pages.get(page_id) if st is not None else None
        if st is None or rec is None or rec.page is not None or page_id in st.trashed_pages:
            return None
        names = _names(st)
        points = tuple(f"{names.get(c, c)}（{c}）" for c in dict.fromkeys(
            (*rec.targets, *rec.concepts)))
        return UnwrittenPage(page_id=rec.page_id, title=rec.title, goal=rec.goal, points=points)

    def all(self, user_id: str) -> Sequence[WikiPage]:
        snap = _load(self._repo, user_id)
        if snap is None:
            return ()
        recs = (snap.state.pages[p] for p in snap.state.page_order
                if p not in snap.state.trashed_pages)
        return tuple(WikiPage(page_id=r.page_id, title=r.title, page=r.page, check_answers=r.checks)
                     for r in recs if r.page is not None)


NAMED_NODES_MAX = 8


class WikiData:
    """`AssistantData` over the repo; K is read at `clock()`."""

    def __init__(self, repo: Repo, clock: Clock):
        self._repo, self._clock = repo, clock

    def durable(self) -> "WikiData":
        """A copy for a durable workflow: loads are recorded steps, the clock is recorded."""
        return WikiData(DurableRepo(self._repo), _durable_clock(self._clock))

    def page(self, user_id: str, page_id: str) -> PageView | None:
        snap = _load(self._repo, user_id)
        rec = snap.state.pages.get(page_id) if snap is not None else None
        if rec is None:
            return None
        sections: tuple[PageSectionView, ...] = ()
        if rec.page is not None:
            texts = rec.page.texts
            sections = tuple(PageSectionView(section_id=s.id, heading=s.heading,
                                             body=texts[s.id].body
                                             if s.id in texts and s.role is not SectionRole.CHECK else "")
                             for s in rec.page.outline.sections)
        return PageView(page_id=rec.page_id, title=rec.title, concepts=rec.concepts,
                        sections=sections, checks=tuple(check_status_lines(rec.page, rec.checks).values())
                        if rec.page is not None else ())

    def page_version(self, user_id: str, page_id: str) -> str | None:
        """The page's fingerprint now (a proposal remembers it); None for an unknown page."""
        snap = _load(self._repo, user_id)
        rec = snap.state.pages.get(page_id) if snap is not None else None
        return None if rec is None else page_fingerprint(rec)

    def section_version(self, user_id: str, page_id: str, section_id: str) -> str | None:
        """One section's fingerprint now (`section_fingerprint`); None for an unknown page."""
        snap = _load(self._repo, user_id)
        rec = snap.state.pages.get(page_id) if snap is not None else None
        return None if rec is None else section_fingerprint(rec, section_id)

    def pages(self, user_id: str) -> Sequence[PageDigest]:
        snap = _load(self._repo, user_id)
        if snap is None:
            return ()
        out = []
        for pid in snap.state.page_order:
            if pid in snap.state.trashed_pages:
                continue
            rec = snap.state.pages[pid]
            out.append(PageDigest(page_id=pid, title=rec.title or pid, concepts=rec.concepts,
                                  summary=page_summary_of(rec.page) if rec.page else ""))
        return out

    def k_text(self, user_id: str, concept_ids: Sequence[str]) -> str:
        snap = _load(self._repo, user_id)
        if snap is None:
            return ""
        known = _known(snap.k, concept_ids)
        return k_text(snap.kview(self._clock()), known, _names(snap.state)) if known else ""

    def guidelines(self, user_id: str) -> Sequence[str]:
        snap = _load(self._repo, user_id)
        return wiki_texts(snap.state, domains=True) if snap is not None else ()

    def known_concepts(self, user_id: str, concept_ids: Sequence[str]) -> frozenset[str]:
        """Those of `concept_ids` whose every aspect K holds as Band.KNOWN (确定会), read only
        (as `profile.derive`): cross-session recall skips an open question about them."""
        snap = _load(self._repo, user_id)
        if snap is None:
            return frozenset()
        kview = snap.kview(self._clock())
        out = set()
        for c in _known(snap.k, concept_ids):
            aspects = kview.aspects(c)
            if aspects and all(kview.cell(f"{c}.{a.value}").band is Band.KNOWN for a in aspects):
                out.add(c)
        return frozenset(out)

    def open_diagnoses(self, user_id: str, draft_ids: Sequence[str]) -> tuple[str, ...]:
        """Those of `draft_ids` whose diagnosis is still open (a question to ask)."""
        from evowiki.wikiapp.goal import rough_blocks
        snap = _load(self._repo, user_id)
        if snap is None:
            return ()
        return tuple(d for d in draft_ids if rough_blocks(snap, d) is not None)

    def language(self, user_id: str) -> Language:
        snap = _load(self._repo, user_id)
        return snap.state.language if snap is not None else "zh"

    def prereqs(self, user_id: str, concept_ids: Sequence[str]) -> tuple[str, ...]:
        """Prerequisites recorded in the user's K (DefineConcept), deduplicated in order."""
        snap = _load(self._repo, user_id)
        if snap is None:
            return ()
        out: dict[str, None] = {}
        for c in _known(snap.k, concept_ids):
            out.update(dict.fromkeys(snap.k.prereqs(c)))
        return tuple(out)

    def node_names(self, user_id: str) -> dict[str, str]:
        """Graph point id -> name (assistant.plain_ids)."""
        snap = _load(self._repo, user_id)
        return {} if snap is None else {n.id: n.name for n in snap.state.graph.nodes}

    def named_nodes(self, user_id: str, text: str) -> tuple[str, ...]:
        """Names of two characters or more, so a stray character names nothing."""
        snap = _load(self._repo, user_id)
        if snap is None:
            return ()
        said = norm_name(text)
        hits = [n for n in snap.state.graph.nodes
                if len(key := norm_name(n.name)) >= 2 and key in said]
        return tuple(f"{n.id} {n.name}" + (f"：{n.definition}" if n.definition else "")
                     for n in hits[:NAMED_NODES_MAX])


class PooledContentSearch:
    """`ContentSearch` that takes a pool connection for each search and returns it right after
    (F2's VectorContentSearch holds one connection; a turn never keeps one open)."""

    def __init__(self, pool: ConnectionPool, content: LoadedContent, embedder: Embedder):
        self._pool, self._content, self._embedder = pool, content, embedder

    def search(self, query: str, k: int) -> Sequence[SegmentHit]:
        with self._pool.connection() as conn:
            return VectorContentSearch(conn, self._content, self._embedder).search(query, k)


class WikiTools:
    """Per-turn tool factory (`TurnDeps.tools`): F2's six tools for one user at the turn's start,
    with that user's K. Built fresh on every call. With `actions` (a ledger) the turn also gets the
    read tools (path, goals, graph, reviews, why, activity, usage, today), the action tools
    (drafts through the Bot ledger) (assistant-actions Task 5); with `sources`
    also the three materials tools; with `memory` the three memory tools."""

    def __init__(self, repo: Repo, *, content: ContentSearch | None = None,
                 search: SearchProvider | None = None, fetcher: Fetcher | None = None,
                 actions: "ActionDeps | None" = None, pool: Any = None,
                 sources: SourceReader | None = None, sandbox: Any = None,
                 memory: Any = None):
        self._repo, self._content, self._search, self._fetcher = repo, content, search, fetcher
        self._sources, self._sandbox, self._memory = sources, sandbox, memory
        self._pages = WikiPages(repo)
        self._actions, self._pool = actions, pool

    def durable(self) -> "WikiTools":
        """A copy for a durable workflow: the snapshot the tools are built from is a recorded
        load. The tools' own reads (wiki search, web search, fetch, page reads, the read / action /
        quiz tools, and the materials reads list_sources / read_source / search_source) happen in
        `Tool.run`, which the loop runs as a step; nothing is read at construction but this."""
        return WikiTools(DurableRepo(self._repo), content=self._content, search=self._search,
                         fetcher=self._fetcher, actions=self._actions, pool=self._pool,
                         sources=self._sources,
                         sandbox=self._sandbox, memory=self._memory)

    def _more(self, now: datetime) -> list[Tool]:
        if self._actions is None:
            return []
        from evowiki.assistant.tools.actions import build_action_tools
        from evowiki.assistant.tools.check import AskCheckTool
        from evowiki.assistant.tools.evidence import RecordEvidenceTool
        from evowiki.assistant.tools.read_more import ReadDeps, build_read_tools
        from evowiki.assistant.tools.rough import AskRoughTool, SkipDiagnosisTool
        a = self._actions
        reads = ReadDeps(a.repo, a.clock, pool=self._pool)
        return [*build_read_tools(reads), *build_action_tools(a),
                RecordEvidenceTool(a.repo, a.clock, a.new_id),
                AskCheckTool(a.repo, a.gateway, a.clock, a.new_id),
                AskRoughTool(a.repo, a.gateway, a.clock, a.new_id),
                SkipDiagnosisTool(a.repo, a.gateway, a.clock, a.new_id)]

    def __call__(self, info: SessionInfo, now: datetime) -> Sequence[Tool]:
        snap = self._repo.load(info.user_id)
        return tuple(build_tools(pages=self._pages, kview=snap.kview(now), content=self._content,
                                 search=self._search, fetcher=self._fetcher,
                                 names=_names(snap.state), more=self._more(now),
                                 sources=self._sources, sandbox=self._sandbox,
                                 memory=self._memory).values())


# ---- applying an accepted proposal ----

CHECK_SECTION = "确认题不能改写"
NOT_ORGANIZED = "这一页还没有生成，不能加入内容"
EMPTY_BODY = "提议的正文是空的"
NO_SECTION = "改写需要指明是哪一段"
TEACH = WEAK | {Band.NONE}           # bands that need teaching: an added section explains them


class ApplyRefused(RuntimeError):
    """An accepted proposal the wiki cannot apply. `str(exc)` is the user-facing reason (the
    service records it as the `failed` decision's detail). The reason must contain Chinese
    (CJK): the session fold replaces any `failed` detail without CJK by the generic internal
    code, so an English reason would never reach the user."""

    def __init__(self, message: str, *, code: str = "edit_refused", params: dict | None = None):
        super().__init__(message)
        self.public_message = message
        self.error_code = code
        self.params = params or {}



def added_role(cells: Sequence[str], bands: dict[str, Band]) -> SectionRole:
    """Role of an added section, deterministic from its cells' bands: EXPLAIN when it has no
    cells or any cell is shaky / gap / none (it has to teach), else LINE (likely / known cells:
    one line is enough). A cell that is not on the page has no band and does not make the role
    EXPLAIN; the shape rule (「引用了不存在的格子」) refuses it anyway."""
    if not cells or any(bands.get(c) in TEACH for c in cells):
        return SectionRole.EXPLAIN
    return SectionRole.LINE


Key = tuple[str, str | None, str]


def _keyed(draft: DraftOutline, spec: PageSpec, bands: dict[str, Band]) -> list[tuple[Violation, Key]]:
    """`check_outline` on `draft`, each violation with a structural key that does not depend on
    where a section sits or what it is called: (rule, section id, message without its 「第 i 段
    「heading」」 prefix) for a section's violation, else (rule, None, message)."""
    tags = {s.id: f"第 {i + 1} 段「{s.heading}」" for i, s in enumerate(draft.sections)}
    return [(v, (v.rule, v.section, v.message.removeprefix(tags.get(v.section, "")))
             if v.section is not None else (v.rule, None, v.message))
            for v in check_outline(draft, spec, bands)]


def new_violations(cand: DraftOutline, old: PageOutline, spec: PageSpec,
                   bands: dict[str, Band]) -> list[Violation]:
    """What the patched draft breaks that the page did not: only what makes it unusable
    (`check_outline` - cannot be rendered or graded). How the edit fits K (coverage, a known cell
    explained) never refuses the user's edit (user ruling 2026-10-10). Violations are compared by
    structural key (`_keyed`), so renaming or moving a section does not make its old violations
    new."""
    old_keys = {k for _, k in _keyed(draft_of(old), spec, bands)}
    return [v for v, key in _keyed(cand, spec, bands) if key not in old_keys]



def _reanchors(rec: PageRecord | None, draft: ProposalDraft, section_version: str | None) -> bool:
    """The page changed since the proposal, but the change still fits it as it is now: a section
    added after one that is still there (or at the end), or a section rewritten that is itself
    exactly as it was (`section_version`; unknown -> no). Anything else would overwrite or
    misplace what changed."""
    if rec is None or rec.page is None:
        return False
    if draft.kind == "add_section":
        return draft.section_id is None or any(s.id == draft.section_id
                                               for s in rec.page.outline.sections)
    return section_version is not None and         section_fingerprint(rec, draft.section_id) == section_version

def _keep_heading(outline: PageOutline, sid: str, before: SectionText | None) -> PageOutline:
    """A LINE section the note showed under its heading (several paragraphs, or a fenced block:
    `sectionLines` in the plugin) stays a headed section when the rewrite makes it one paragraph;
    it would turn into a bullet that swallows the heading."""
    if before is None or not re.search(r"\n[ \t]*\n|```", before.body.strip()):
        return outline
    return outline.model_copy(update={"sections": tuple(
        s.model_copy(update={"role": SectionRole.EXPLAIN})
        if s.id == sid and s.role is SectionRole.LINE else s for s in outline.sections)})


class WikiApplier:
    """`ProposalApplier` over E1's page records: the accepted draft becomes one minimal patch
    (insert after a section / rewrite one section) checked by the patch guardrails, with the
    assistant's text as the section text (0 LLM calls), saved as `PageSaved(action="assistant")`
    plus `UsageAdded(action="assistant")` in one commit. The text's `cache_key` is
    `"assistant:<sid>"` (a provenance tag). On `WikiConflict` the whole apply is redone on a fresh snapshot (bounded). Refusals raise
    `ApplyRefused`; nothing is written then."""

    def __init__(self, repo: Repo, clock: Clock, new_id: NewId, *, tries: int = COMMIT_TRIES):
        self._repo, self._clock, self._new_id, self._tries = repo, clock, new_id, tries

    def apply(self, user_id: str, draft: ProposalDraft, *, expect_version: str | None = None,
              section_version: str | None = None,
              extra: Sequence[Callable[[Any], None]] = (),
              undo_sink: dict[str, Any] | None = None) -> tuple[str, ...]:
        """`expect_version` (a `page_fingerprint`): refuse with STALE_DETAIL when the page is no
        longer that version - checked on every attempt, so a conflict retry cannot bypass it -
        unless the change is re-anchored onto the page as it is now (`_reanchors`: an added
        section whose anchor is still there; a rewritten section that is itself unchanged, by
        `section_version`).
        `extra` goes into the commit (the action ledger's exactly-once effect). `undo_sink` is
        filled, before each commit attempt, with what `undo_edit` needs (the previous outline and
        section record, and the page's fingerprint after the edit); an `extra` writer reads it at
        commit time, so the undo data lands in the same transaction as the page."""
        for attempt in range(1, self._tries + 1):
            snap = _load(self._repo, user_id)
            if snap is None:
                raise ApplyRefused(f"没有这个用户：{user_id}")
            rec = snap.state.pages.get(draft.page_id)
            if expect_version is not None and page_fingerprint(rec) != expect_version                     and not _reanchors(rec, draft, section_version):
                raise ApplyRefused(STALE_DETAIL, code="page_changed")
            now = self._clock()
            record, sid = self._patched(snap, draft, now)
            assert record.page is not None
            before = snap.state.pages[draft.page_id]
            record, logs = partial_write(snap, record, [sid], kind="assistant",
                                         ref=self._new_id("asw"), producer="edit", now=now,
                                         new_id=self._new_id)
            if undo_sink is not None:
                undo_sink.clear()
                undo_sink.update(self._undo_data(before, record, sid))
            heading = section_of(record.page.outline, sid).heading
            events = [   # the page, then a usage record like E1's feedback flows (curator input)
                WikiEvent(event_id=self._new_id("wev"), ts=now,
                          body=PageSaved(record=record, action=APPLY_ACTION)),
                *logs,
                WikiEvent(event_id=self._new_id("wev"), ts=now, body=UsageAdded(
                    record=UsageRecord(action=APPLY_ACTION, page=record.page_id,
                                       heading=heading)))]
            try:
                self._repo.commit(snap, Commit(wiki=events, extra=list(extra)))
            except WikiConflict:
                if attempt == self._tries:
                    break
                continue
            return (sid,)
        raise ApplyRefused(CONFLICT_DETAIL)

    @staticmethod
    def _undo_data(before: PageRecord, after: PageRecord, sid: str) -> dict[str, Any]:
        page = before.page
        assert page is not None
        return {"page_id": before.page_id, "section_id": sid, "prev_changed": before.changed,
                "prev_written": (before.written.model_dump(mode="json")
                                 if before.written is not None else None),
                "after_version": page_fingerprint(after),
                "prev_outline": page.outline.model_dump(mode="json"),
                "prev_text": page.texts[sid].model_dump(mode="json") if sid in page.texts else None,
                "prev_state": page.states[sid].model_dump(mode="json") if sid in page.states
                else None}

    def undo_edit(self, user_id: str, data: dict[str, Any], *,
                  extra: Sequence[Callable[[Any], None]] = ()) -> None:
        """Take an applied edit back: the outline and the edited section's text / state return to
        what `data` (an `undo_sink`) recorded, saved as one `PageSaved(action="assistant_undo")`.
        Refused (`ApplyRefused`, Chinese) when the page is no longer the edit's result: nothing
        newer is overwritten. Redone on a `WikiConflict` (bounded), the check on every attempt.
        `extra` goes into the commit (the ledger row's executed -> undone move). The page's
        `changed` marks return to what they were before the edit."""
        from evowiki.goals.contracts import PageOutline, SectionState
        sid = data["section_id"]
        for attempt in range(1, self._tries + 1):
            snap = _load(self._repo, user_id)
            rec = snap.state.pages.get(data["page_id"]) if snap is not None else None
            if rec is None or rec.page is None:
                raise ApplyRefused(UNDO_GONE)
            if page_fingerprint(rec) != data["after_version"]:
                raise ApplyRefused(UNDO_STALE)
            page = rec.page
            texts = {k: v for k, v in page.texts.items() if k != sid}
            if data["prev_text"] is not None:
                texts[sid] = SectionText.model_validate(data["prev_text"])
            states = {k: v for k, v in page.states.items() if k != sid}
            if data["prev_state"] is not None:
                states[sid] = SectionState.model_validate(data["prev_state"])
            restored = page.model_copy(update={
                "outline": PageOutline.model_validate(data["prev_outline"]),
                "texts": texts, "states": states})
            now = self._clock()
            record = rec.model_copy(update={"page": restored, "changed": data["prev_changed"]})
            if "prev_written" in data:             # the write mark returns with the text
                prev = data["prev_written"]
                record = record.model_copy(update={
                    "written": None if prev is None else WriteMark.model_validate(prev)})
            event = WikiEvent(event_id=self._new_id("wev"), ts=now,
                              body=PageSaved(record=record, action=UNDO_ACTION))
            try:
                self._repo.commit(snap, Commit(wiki=[event], extra=list(extra)))
            except WikiConflict:
                if attempt == self._tries:
                    break
                continue
            return
        raise ApplyRefused(CONFLICT_DETAIL)

    def _fresh_id(self, taken: set[str]) -> str:
        sid = self._new_id("sec")
        while sid in taken:
            sid = self._new_id("sec")
        return sid

    def _patched(self, snap: Snapshot, draft: ProposalDraft,
                 now: datetime) -> tuple[PageRecord, str]:
        rec = snap.state.pages.get(draft.page_id)
        if rec is None:
            raise ApplyRefused(f"没有这个页面：{draft.page_id}")
        page = rec.page
        if page is None:
            raise ApplyRefused(NOT_ORGANIZED)
        outline = page.outline
        ids = {s.id for s in outline.sections}
        if draft.section_id is not None and draft.section_id not in ids:
            raise ApplyRefused(f"没有这一段：{draft.section_id}")
        spec = page_spec(snap.state, snap.k, rec.page_id, bands_only=True)
        bands = page_bands(spec, snap.kview(now))
        body = draft.text.strip()
        if not body:
            raise ApplyRefused(EMPTY_BODY)
        if draft.kind == "add_section":
            sid = self._fresh_id(ids | set(page.states) | set(page.texts))
            after = draft.section_id or (outline.sections[-1].id if outline.sections else None)
            op = DraftOp(op="insert", after=after, section=DraftSection(
                id=sid, heading=draft.heading, role=added_role(draft.cells, bands).value,
                cells=list(draft.cells), why=draft.why, notes=draft.why))
            mark = "inserted"
        else:
            if draft.section_id is None:
                raise ApplyRefused(NO_SECTION)
            sid = draft.section_id
            # a locked section too: the user asked for this and confirmed the card; it stays locked
            if section_of(outline, sid).role is SectionRole.CHECK:
                raise ApplyRefused(CHECK_SECTION)
            op = DraftOp(op="rewrite", id=sid, heading=draft.heading,
                         cells=list(draft.cells) or None)
            mark = "rewritten"
        cand, _ = apply_ops(outline, [op])
        violations = new_violations(cand, outline, spec, bands)
        if violations:
            raise ApplyRefused("；".join(v.message for v in violations))
        new = _keep_untouched(outline, cand, to_outline(cand, spec, bands, source=outline.source))
        if mark == "rewritten":
            new = _keep_heading(new, sid, page.texts.get(sid))
        text = SectionText(section_id=sid, cue=draft.cue, answer=draft.answer, body=body,
                           cache_key=f"{APPLY_ACTION}:{sid}")
        states = {**page.states, sid: page.state(sid).model_copy(update={"folded": False})}
        new_page = page.model_copy(update={"outline": new, "states": states,
                                           "texts": {**page.texts, sid: text}})
        return rec.model_copy(update={"page": new_page, "changed": {sid: mark}}), sid
