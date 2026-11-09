"""Wire shapes of page revisions from materials (materials spec 2026-10-20 §7 D): the start routes
(`routes_revise`) and `ActionOut.revise`, what the plugin needs to show a `page_revise` draft
inside the page as track changes (struck old text, added new text per changed section) with
确认替换 / 取消 (the ledger's approve / reject; approve takes no draft).

`ReviseOut.sections` comes from `bot.diffs.revision_view`, the same helper `ActionOut.diff` is
built from, so the overlay and the 改动对比 can never disagree; interactives show as one
〔互动：title〕 line on both sides.

The assistant's `page_practice` draft (a block composed when it was proposed, `bot.practice`) is
shown the same way while it waits (`practice_out`): one changed section, its text as it is plus
the drafted block as an inline ```evoui fence, so the page draws the block live (an inline block
reports no results: playing the draft records nothing). `ReviseOut.practice` says which kind."""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from evowiki.bot.diffs import Names, SectionStatus, revision_view, shown_body
from evowiki.bot.practice import PracticeDraft, draft_fence, fits
from evowiki.goals.contracts import SectionRole
from evowiki.sources.cite import NO_MISMATCH
from evowiki.store.bot import ActionRow
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.revise import KIND

MAX_SOURCES = 5
PRACTICE_KIND = "page_practice"
MSG_URL_OR_TEXT = "网址和文字只能给一样"


class ReviseIn(BaseModel):
    """POST /wiki/pages/{page_id}/revise. Nothing given: the run picks the materials that talk
    about the page. `url` / `text`: imported first as a material of this page (use=supplement)."""

    source_ids: list[str] = Field(default_factory=list, max_length=MAX_SOURCES)
    url: str | None = Field(default=None, max_length=2000)
    text: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def _one(self) -> "ReviseIn":
        if self.url and self.text:
            raise ValueError(MSG_URL_OR_TEXT)
        return self


class ReviseStarted(BaseModel):       # 202
    action_id: str
    run_id: str
    page_id: str


class SourceReviseIn(BaseModel):      # POST /wiki/sources/{source_id}/revise (the Bot's 更新)
    page_ids: list[str] = Field(min_length=1, max_length=3)


class SourceReviseOut(BaseModel):
    """One entry per page a draft was started for (a page with an open draft is skipped)."""
    started: list[ReviseStarted]


class RevisionSourceOut(BaseModel):
    source_id: str
    title: str
    deleted: bool                      # the material was deleted since


class RevisedSectionOut(BaseModel):
    """A section the draft changes: `old_heading` None for an added one; `old_body` "" for an
    added one, `new_body` "" for a removed one."""
    section_id: str
    heading: str
    status: SectionStatus
    old_heading: str | None
    old_body: str
    new_body: str


class ReviseOut(BaseModel):
    state: Literal["writing", "ready", "failed"]
    page_id: str
    run_id: str | None                 # follow it: GET /wiki/pages/{page_id}/run/stream
    detail: str                        # why it failed (Chinese, for the circled ?)
    sources: list[RevisionSourceOut]
    sections: list[RevisedSectionOut]  # changed / added / removed only, draft order
    unchanged: int                     # sections that stay as they are (check sections not counted)
    rebuild: bool                      # rewritten from the materials (revision cap)
    cite_issues: int = 0               # citations the check removed or marked (the bar's ?)
    # a ready draft waiting for the learner whose page changed since it was written: approving
    # it would fail (stale), so the bar offers 取消 only
    stale: bool = False
    # an assistant's practice draft (page_practice) rather than a revision from materials
    practice: Literal["question", "card", "interactive"] | None = None


def revise_source_ids(row: ActionRow) -> list[str]:
    """The materials a `page_revise` row names (its draft's, else the ones it was asked with)."""
    draft = row.content.get("draft")
    if isinstance(draft, dict):
        return [str(s.get("source_id")) for s in draft.get("sources") or []
                if isinstance(s, dict) and s.get("source_id")]
    return [str(s) for s in row.content.get("source_ids") or []]


def revise_out(row: ActionRow, names: Names,
               sources: Mapping[str, tuple[str, bool]]) -> ReviseOut | None:
    """`sources`: source_id -> (title, deleted) of the materials the row names (an id missing
    there shows as deleted). None for a row of another kind."""
    if row.kind == PRACTICE_KIND:
        return practice_out(row, names)
    if row.kind != KIND:
        return None
    c = row.content
    state = c.get("state")
    state = state if state in ("writing", "ready", "failed") else "failed"
    detail = str(c.get("detail") or "")
    try:
        view = revision_view(row, names)
    except ValueError:                 # a stored draft that no longer validates
        view, state, detail = None, "failed", detail or "这条草稿读不出来了"
    titles = {s.source_id: s.title for s in view.draft.sources} if view is not None else {}
    rec = names.record(view.draft.page_id) if view is not None else None
    stale = (row.status == "proposed" and rec is not None and view is not None
             and page_fingerprint(rec) != view.draft.base_version)
    out_sources = []
    for sid in revise_source_ids(row):
        title, deleted = sources.get(sid, ("", True))       # the current title, when known
        out_sources.append(RevisionSourceOut(source_id=sid, title=title or titles.get(sid, ""),
                                             deleted=deleted))
    return ReviseOut(
        state=state, page_id=str(c.get("page_id") or row.page_id or ""),
        run_id=str(c["run_id"]) if c.get("run_id") else None, detail=detail,
        sources=out_sources,
        sections=[RevisedSectionOut(section_id=s.section_id, heading=s.heading, status=s.status,
                                    old_heading=s.old_heading, old_body=s.old_body,
                                    new_body=view.draft.preview.get(s.section_id, s.new_body))
                  for s in (view.sections if view else ())],
        unchanged=view.unchanged if view is not None else 0,
        rebuild=view.draft.rebuild if view is not None else False,
        # a verified citation or disagreement (its quote found) is no mismatch: not counted
        cite_issues=sum(i.kind not in NO_MISMATCH for i in view.draft.cite_issues)
        if view is not None else 0,
        stale=stale)


def practice_out(row: ActionRow, names: Names) -> ReviseOut | None:
    """A waiting `page_practice` row as an in-page draft: its section with the drafted block added
    at the end. None once decided, or when the draft does not parse or its page / section is gone
    (the card still shows; 确认 then refuses with the reason)."""
    if row.kind != PRACTICE_KIND or row.status != "proposed":
        return None
    kind = row.content.get("kind")
    try:
        draft = PracticeDraft.model_validate(row.content.get("draft"))
    except ValidationError:
        return None
    rec = names.record(draft.page_id)
    if rec is None or rec.page is None or kind not in ("question", "card", "interactive"):
        return None
    sections = [s for s in rec.page.outline.sections if s.role is not SectionRole.CHECK]
    sec = next((s for s in sections if s.id == draft.section_id), None)
    if sec is None:
        return None
    text = rec.page.texts.get(sec.id)
    old = shown_body(text.body if text is not None else "", {}, names.asset)
    new = (old.rstrip() + "\n\n" if old.strip() else "") + draft_fence(draft)
    return ReviseOut(
        state="ready", page_id=draft.page_id, run_id=None, detail="", sources=[],
        sections=[RevisedSectionOut(section_id=sec.id, heading=sec.heading, status="changed",
                                    old_heading=sec.heading, old_body=old, new_body=new)],
        unchanged=len(sections) - 1, rebuild=False,
        # re-anchored on confirm (`practice.fits`): only a renamed section stops it
        stale=not fits(rec, draft), practice=kind)
