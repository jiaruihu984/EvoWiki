"""Page revisions from materials (materials spec 2026-10-20 §7 D): a whole-page draft the learner
confirms before it replaces the page. Pure: no IO, no LLM.

The draft is the action-ledger row itself (`page_revise`, always `ask`): `content["draft"]` holds a
`RevisionDraft` once the writer run filled it. The page follows the materials: where the writer
judges a material wrong it still writes the material's version and adds a ⟦和资料不同：…⟧ line with
its reason, so there is nothing for the learner to pick - he confirms or rejects the whole draft.
`revised_record` is the whole-page swap: every section the draft writes, in the draft's order; an
old section the draft leaves out is gone (not a merge) - except check sections, which the writer
never writes (`refine_input` scopes a revision to the non-check sections), so they stay where they
were rather than silently dropping the page's questions.

The draft's citations were checked before it reached the row (`sources.cite.verify`, in
`wikiapp.source_revise`): `cite_issues` lists what was removed or marked. A confirmed draft gives
the page its materials (`PageRecord.sources`) and those issues, like a page written from them."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from evowiki.deep.contracts import RefineDraft, RenderCheck
from evowiki.deep.store import StoredAsset
from evowiki.goals.contracts import SectionRole
from evowiki.goals.fold import Page
from evowiki.sources.cite import CiteIssue
from evowiki.wikiapp.events import PageRecord, PageSource
from evowiki.wikiapp.refine_apply import written_record

KIND = "page_revise"
REVISE_REBUILD_AFTER = 5          # after this many revisions the next one rewrites from the materials
SAVE_ACTION = "source_revise"     # PageSaved.action of a confirmed revision ...
UNDO_ACTION = "source_revise_undo"   # ... and of its undo


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RevisionSource(_M):
    source_id: str
    title: str
    section_ids: tuple[str, ...] = ()                   # the material's sections this draft used
    page_offset: int | None = None                      # PDF page − printed page then (citations)


class RevisionDraft(_M):
    """The ledger row's `content["draft"]`. `checks`: the render check of every interactive / EvoUI
    block the draft places (the writer's result; needed to store them as assets); `cite_issues`:
    the citation / disagreement marks the check did not keep as written (`draft` is the checked
    text)."""

    page_id: str
    base_version: str                                   # page_fingerprint when the draft was written
    draft: RefineDraft
    sources: tuple[RevisionSource, ...] = ()
    rebuild: bool = False                               # rewritten from the materials (revision cap)
    checks: tuple[RenderCheck, ...] = ()
    cite_issues: tuple[CiteIssue, ...] = ()
    scope: tuple[str, ...] = ()  # A scoped revision preserves every other section.
    preview: dict[str, str] = Field(default_factory=dict)  # Renderable, stored draft assets by section.


def page_sources_of(sources: tuple[RevisionSource, ...]) -> tuple[PageSource, ...]:
    """The draft's materials as the page records them (`PageRecord.sources`)."""
    return tuple(PageSource(source_id=s.source_id, title=s.title, page_offset=s.page_offset)
                 for s in sources)


def _whole_page_base(page: Page, draft: RefineDraft) -> tuple[Page, RefineDraft]:
    """The page cut down to what survives a whole-page swap (the drafted sections and the check
    sections), and the draft with any `after` that pointed at a dropped section moved to the nearest
    surviving section before it (None = page top)."""
    drafted = {s.section_id for s in draft.sections}
    keep, moved, last = [], {}, None
    for s in page.outline.sections:
        if s.id in drafted or s.role is SectionRole.CHECK:
            keep.append(s)
            last = s.id
        else:
            moved[s.id] = last
    kept = {s.id for s in keep}
    outline = page.outline.model_copy(update={"sections": tuple(keep)})
    base = page.model_copy(update={
        "outline": outline,
        "texts": {k: v for k, v in page.texts.items() if k in kept},
        "states": {k: v for k, v in page.states.items() if k in kept}})
    sections = tuple(s.model_copy(update={"after": moved[s.after]})
                     if s.after in moved else s for s in draft.sections)
    return base, draft.model_copy(update={"sections": sections})


def revised_record(rec: PageRecord, draft: RefineDraft, stored_assets: list[StoredAsset], *,
                   now: datetime, job_id: str, model: str, rebuild: bool = False,
                   sources: tuple[PageSource, ...] = (),
                   cite_issues: tuple[CiteIssue, ...] = ()) -> PageRecord:
    """`rec` with its page replaced by `draft` and one
    more revision; a `rebuild` (the page rewritten from the materials at the revision cap) starts
    the count again at 0. `sources` / `cite_issues`: the draft's materials and citation check
    (`written_record` keeps an untouched section's earlier ones). ValueError when the page is not
    organized (a user note) or the draft does not fit."""
    if rec.page is None:
        raise ValueError("page is not organized")
    base, fitted = _whole_page_base(rec.page, draft)
    out = written_record(rec, fitted, stored_assets, job_id=job_id, now=now, model=model,
                         page=base, kind="rewrite", sources=sources, cite_issues=cite_issues)
    return out.model_copy(update={"revisions": 0 if rebuild else rec.revisions + 1})
