"""Wire models of /wiki/sources (资料). `SourceProgress` is the importer's own record."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_core import PydanticCustomError

from evowiki.sources.importer import SourceProgress, failure_detail
from evowiki.sources.models import ReadBy, SectionRow, SourceKind, SourceRow, SourceStatus, Use
from evowiki.wikiapp.wiki_schemas import ExamSummaryOut

MAX_SECTION_DEPTH = 2


class UseIn(BaseModel):
    """`auto`: the material decides (exam, paper → understand, else learn; T14)."""

    model_config = ConfigDict(extra="forbid")

    use: Use | Literal["auto"]
    goal_id: str | None = None
    page_id: str | None = None
    session_id: str | None = None


class UseOut(UseIn):
    use: Use                          # the use recorded (the guessed one for `auto`)
    id: str
    created_at: datetime



class SourceOut(BaseModel):
    id: str
    title: str
    kind: SourceKind
    pages: int | None
    status: SourceStatus
    error: str | None
    error_code: str | None = None
    unreadable_pages: list[int]
    progress: SourceProgress | None
    note: str | None
    origin: str | None
    created_at: datetime
    deleted: bool
    uses: list[UseOut]
    can_retry: bool    # POST …/retry would run it again: it failed, or ended with an error once usable


class SectionOut(BaseModel):
    id: str
    parent_id: str | None
    title: str
    depth: int
    page_from: int | None
    page_to: int | None
    tokens: int
    read_by: ReadBy
    summary: str | None


class LocatorOut(BaseModel):
    """Where a result sits in the material's stored document (`sources.locate`): a block of a page
    and the character range in it; `bbox` page-relative 0..1, null when the parser gave none."""
    source_id: str
    page: int
    bbox: tuple[float, float, float, float] | None
    block_id: str | None
    char_start: int | None
    char_end: int | None


class GlossaryEntryOut(BaseModel):
    term: str                     # as written in the material
    means: str
    page: int | None              # PDF page where it first occurs in the text the model read
    section_id: str | None = None  # the section it first occurs in (None: imported before 0101)
    locator: LocatorOut | None = None  # None: no stored document, or the term not found in it


class GlossaryCoverageOut(BaseModel):
    """Which text the glossary was extracted from: the whole material, or each section's first
    `head_chars` characters (a term only further into a section is not in the glossary)."""
    whole: bool
    head_chars: int | None
    sent_chars: int
    total_chars: int
    cut_sections: int


class SourceDetail(SourceOut):
    sections: list[SectionOut]
    page_offset: int | None = None    # PDF page − printed page; None: show PDF pages
    glossary: list[GlossaryEntryOut] = []
    glossary_coverage: GlossaryCoverageOut | None = None   # None: not recorded (before 0101)
    exam: ExamSummaryOut | None = None   # 真题: the extraction and its counts; None without an exam use


class SourcePatchIn(BaseModel):
    """A new title, and / or where its original now lies in the vault (the plugin saved a copy)."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    origin: str | None = None

    @field_validator("title")
    @classmethod
    def _stripped(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise PydanticCustomError("source_title_empty", "A material name cannot be empty.")
        if len(v) > 200:
            raise PydanticCustomError("source_title_too_long", "A material name can contain at most {limit} characters.", {"limit": 200})
        return v


class SectionEditIn(BaseModel):
    """One row of PATCH /wiki/sources/{id}/sections: a new title and/or a new level (0..2)."""
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None = None
    depth: int | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("标题不能为空")
        if len(v) > 200:
            raise ValueError("标题太长了")
        return v

    @field_validator("depth")
    @classmethod
    def _depth(cls, v: int | None) -> int | None:
        if v is not None and not 0 <= v <= MAX_SECTION_DEPTH:
            raise ValueError(f"层级只能是 0 到 {MAX_SECTION_DEPTH}")
        return v


def _progress(p: dict) -> SourceProgress | None:
    try:
        return SourceProgress(stage=p["stage"], done=p["done"], total=p["total"],
                              message=p["message"])
    except (KeyError, ValueError):      # no record yet, or one this build cannot read
        return None


def source_out(r: SourceRow, uses: list[dict] | None = None) -> SourceOut:
    # a failure after the source became usable keeps status ready / partial and error null; its
    # reason is stored as the end record {"end": "error", "detail"} and surfaces as `note`
    key = "detail" if r.progress.get("end") == "error" else "note"
    note = r.progress.get(key)
    code = r.progress.get("error_code")
    code = code if isinstance(code, str) else None
    params = r.progress.get("error_params", {})
    params = params if isinstance(params, dict) else {}
    if isinstance(note, str):
        note = failure_detail(note, code, params)
    # usable with the run still going: the step in progress (术语 1/1, 段 74/74) is no count of the
    # material; the card says 读好了 until the end record
    ongoing = r.status != "reading" and "end" not in r.progress
    return SourceOut(id=r.source_id, title=r.title, kind=r.kind, pages=r.pages, status=r.status,
                     error=failure_detail(r.error, code, params) if r.error else None, error_code=code, unreadable_pages=list(r.unreadable_pages),
                     progress=None if ongoing else _progress(r.progress), note=note if isinstance(note, str) else None,
                     origin=r.origin, created_at=r.created_at, deleted=r.deleted_at is not None,
                     uses=[use_out(u) for u in uses or []],
                     can_retry=r.status == "failed" or r.progress.get("end") == "error")


def source_detail(r: SourceRow, uses: list[dict], sections: list[SectionRow],
                  exam: ExamSummaryOut | None = None) -> SourceDetail:
    return SourceDetail(**source_out(r, uses).model_dump(),
                        sections=[section_out(s) for s in sections], page_offset=r.page_offset,
                        glossary=[GlossaryEntryOut.model_validate(g) for g in r.glossary],
                        glossary_coverage=None if r.glossary_coverage is None
                        else GlossaryCoverageOut.model_validate(r.glossary_coverage),
                        exam=exam)


def section_out(s: SectionRow) -> SectionOut:
    return SectionOut(id=s.section_id, parent_id=s.parent_id, title=s.title, depth=s.depth,
                      page_from=s.page_from, page_to=s.page_to, tokens=s.tokens,
                      read_by=s.read_by, summary=s.summary)


def use_out(u: dict) -> UseOut:
    return UseOut(id=u["use_id"], use=u["use"], goal_id=u["goal_id"], page_id=u["page_id"],
                  session_id=u["session_id"], created_at=u["created_at"])
