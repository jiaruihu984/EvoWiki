"""The refine agent's tools (plan rev 3.3 §10.2 point 6): plan_page, read_k, read_page, read_spec,
write_section, place_component, render_check, finish. They share the agent tool contract
(`agent.tools.Tool`: validate, never raise, short results) and read_k / read_page tools.

Interactives come only from the component library (B5): `place_component` puts a registered component
(by id) on a section; its html, height and probes are the library's, never the model's. A configurable
component also takes the model's `config` for this page's topic — validated against the component's
schema (invalid → an error back to the model, nothing placed) and injected into the HTML
(`deep.components.instantiate`). Every placement says `why` it fits. The placed component is an ordinary
`InteractiveDraft` (interactive id = `placement_id`, default the component id) and goes through the
render check (on component + config) and the asset store like any interactive. With an empty library
`place_component` is not offered at all — refine then writes text only (fail closed).

`plan_page` (deep@v28, LLM-call consolidation stage 4) comes first: the plan (`PlanAnswers`; `known`
is code's) and, when the page has no outline yet or is being reorganized (`OutlineSetup`), the
outline, checked by code (`goals.outline_context.accept_draft`; what it refuses goes back as a tool
error). Every write tool refuses until a plan is accepted; a re-plan replaces it only while no
section is written.

Every write goes to a `Workspace` in memory — nothing here touches the page, the asset store or the
log; read_page reads the current page as the draft has it (`DraftPageStore`, the same merge the commit
uses). `finish` validates the whole draft (`RefineDraft`) and freezes it; the caller commits it once.
`Workspace.required`: sections that must be written before `finish` accepts (a page written from its
outline: every non-check section). `Workspace.on_change(ws, section_id)` is called after every write that
changes what a section shows (its text, or an interactive / block placed on it) — the page writer streams
sections from it."""

import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from evowiki.agent.read_k import ReadKTool
from evowiki.agent.read_page import ReadPageTool
from evowiki.agent.tool_helpers import clip, fail, run_guarded, spec
from evowiki.agent.tools import Tool, ToolContext, ToolResult
from evowiki.deep.components import ConfigInvalid, cell_problems, instantiate
from evowiki.deep.contracts import (
    BUILTIN_REVIEW_RULES,
    BlockChecker,
    Depth,
    ImageDraft,
    InteractiveDraft,
    LibraryComponent,
    PlanAnswers,
    RefineDraft,
    RefinedSection,
    RefinePlan,
    RenderCheck,
    RenderChecker,
    ReviewFinding,
    image_placeholder,
)
from evowiki.deep.evoui import app_images, block_json, quiz_cells
from evowiki.deep.evoui import validate as validate_block
from evowiki.deep.evoui.contracts import BlockDraft, EvoUIBlock, block_placeholder
from evowiki.deep.images import MIME, PROVIDER_NAMES, ImageCandidate, ImageRejected, ImageSearch
from evowiki.deep.visual import Brief, Decision, Drawn, VisualFailed, VisualStep
from evowiki.goals.contracts import (
    KView,
    OutlineSection,
    PageOutline,
    SectionRole,
    SectionText,
    split_cell,
)
from evowiki.goals.fold import Page, k_snapshot, states_for
from evowiki.goals.guardrails import RETIRED_ROLES, DraftOutline, PageSpec
from evowiki.goals.outline_context import accept_draft
from evowiki.llm.chat import ImagePart
from evowiki.llm.run_context import reject
from evowiki.teach.contracts import TeachingSpec
from evowiki.teach.render import item_text
from evowiki.teach.standard import CHECKLIST
from evowiki.wikiapp.checks import accept_outline_checks
from evowiki.wikiapp.page_reader import InMemoryPageStore, WikiPage
from evowiki.wikiapp.series import SeriesUpdate

if TYPE_CHECKING:   # the material tools are the assistant's: imported where they are built
    from evowiki.sources.reader import SourceReader

log = logging.getLogger("evowiki.deep.tools")

SPEC_CHUNK = 6000
# What `agent.loop.elide` puts in place of an old read result / an old write call's long arguments
# (deep.agent.WRITER_ELIDE). A write whose body is one of these is the model copying the elided
# history (live run 2026-10-21: 19 such writes on one page, two of them committed as section text):
# refused, see `section_problem`.
READ_STUB = ("（这是之前读过的内容，已从对话里省略。你已经读过它，不要为了核对再读；"
             "只有要改这一段、需要它的原文时才再读。）")
WRITE_STUB = "（系统省略：这里原来是你写的正文，已经存进草稿，只是从对话记录里删掉了。这不是正文，不要照抄）"
ELIDED_BODY = ("正文是对话记录里的省略标记，不是正文。草稿里这一段还是你之前写的完整正文"
               "（用 read_page 可以看）；只有要改它时才重写，而且要给出完整正文")
UNWRITTEN_ELIDED = "正文是对话记录里的省略标记，不是正文。这一段还没写：body 要写这一段的完整正文"
PLACEHOLDER = "{{{{interactive:{}}}}}"
PLACED = re.compile(r"\{\{(?:interactive|block|image):([^{}\s]+)\}\}")   # every kind of placeholder
# deep@v32: organizer / compare are no longer offered (stored sections keep them)
WRITABLE_ROLES = ("explain", "recap", "line")
NO_PLAN = "先调用 plan_page"


def placeholder(interactive_id: str) -> str:
    return PLACEHOLDER.format(interactive_id)


def check_key(d: InteractiveDraft) -> str:
    """A render check is valid for exactly this html, height and probe list."""
    return d.model_dump_json(include={"html", "height", "probes"})


def page_order(outline_ids: Sequence[str], sections: Mapping[str, RefinedSection]) -> list[str]:
    """The final page order: the outline's ids, plus every new section (an id not on the outline)
    after its `after` (None = page top). New sections sharing an anchor keep their write order, and
    one may hang after another new section written later. An anchor that never resolves puts the
    section at the end (write_section refuses such anchors, so only a stale draft gets here)."""
    order = list(outline_ids)
    on_page = set(order)
    pending = [x for sid, x in sections.items() if sid not in on_page]
    tail: dict[str | None, str] = {}            # anchor -> last section placed after it
    top = 0
    while pending:
        placed = False
        for x in list(pending):
            if x.after is None:
                order.insert(top, x.section_id)
                top += 1
            elif x.after in order:
                anchor = tail.get(x.after, x.after)
                order.insert(order.index(anchor) + 1, x.section_id)
                tail[x.after] = x.section_id
            else:
                continue
            pending.remove(x)
            placed = True
        if not placed:
            order += [x.section_id for x in pending]
            break
    return order


def merge_sections(page: Page, drafted: Mapping[str, RefinedSection],
                   text: Callable[[RefinedSection], SectionText],
                   ) -> tuple[Page, dict[str, Literal["rewritten", "inserted"]]]:
    """`page` with the drafted sections written in, and how each changed (page order). A drafted id
    on the page rewrites that section (its id, `check`, notes and fold state are kept; cells too
    when the draft leaves them empty); a new id is inserted after `after` (`page_order`). Nothing
    is deleted. The one merge both `apply.apply_draft` (the commit) and the writer's read_page (the
    draft as it reads now) use, so the two cannot drift."""
    old_by = {o.id: o for o in page.outline.sections}
    texts = dict(page.texts)
    changed: dict[str, Literal["rewritten", "inserted"]] = {}
    secs = []
    for sid in page_order(list(old_by), drafted):           # the same order the draft was written in
        s, old = drafted.get(sid), old_by.get(sid)
        if s is None:
            secs.append(old)
            continue
        if old is not None:
            secs.append(OutlineSection(**{**old.model_dump(), "heading": s.heading, "role": s.role,
                                          "cells": s.cells or old.cells, "check": old.check}))
            changed[sid] = "rewritten"
        else:
            secs.append(OutlineSection(id=sid, heading=s.heading, role=s.role, cells=s.cells))
            changed[sid] = "inserted"
        texts[sid] = text(s)
    outline = PageOutline(**{**page.outline.model_dump(), "sections": tuple(secs)})
    new_page = Page(outline=outline, states=states_for(outline, page.states), texts=texts,
                    snapshot=page.snapshot, unresolved_conflicts=page.unresolved_conflicts,
                    conflicts=page.conflicts)
    return new_page, {s.id: changed[s.id] for s in outline.sections if s.id in changed}


class Workspace:
    """The draft of one refine, in memory. `scope`: section ids that may be rewritten (empty = every
    non-check section); new sections may be inserted anywhere either way. `page` is None only for
    a page whose outline plan_page has not set yet."""

    def __init__(self, page: Page | None, *, scope: Collection[str] = (), page_cells: Collection[str] = (),
                 components: Mapping[str, LibraryComponent] | None = None,
                 required: Collection[str] = (),
                 on_change: "Callable[[Workspace, str], None] | None" = None,
                 image_search: ImageSearch | None = None):
        self.page = page
        self.image_search = image_search                     # None = no image tools
        self.required = tuple(x for x in required if page is None or not page.state(x).pinned)
        self.on_change = on_change
        self.components: dict[str, LibraryComponent] = dict(components or {})   # the library, by id
        self.scope = tuple(scope)
        self.page_cells = frozenset(page_cells)
        self.sections: dict[str, RefinedSection] = {}
        self.interactives: dict[str, InteractiveDraft] = {}
        self.checks: dict[str, tuple[str, RenderCheck]] = {}   # iid -> (check_key, result)
        self.finished: RefineDraft | None = None
        self.series_update: SeriesUpdate | None = None         # from the last finish that gave one
        self.self_check: tuple[ReviewFinding, ...] = ()        # from the last accepted finish
        self.placements: dict[str, tuple[str, str]] = {}       # interactive id -> (component, why)
        self.blocks: dict[str, BlockDraft] = {}                 # EvoUI blocks the writer composed
        self.block_checks: dict[str, tuple[str, RenderCheck]] = {}   # block id -> (block json, pass)
        self.images: dict[str, ImageDraft] = {}                 # images the writer placed
        self.image_candidates: dict[str, ImageCandidate] = {}   # find_image hits, by candidate id
        self.visual: VisualStep | None = None                   # draw_visual's step; None = no draw_visual
        self.drawn_images: set[str] = set()                     # images that live inside a drawn app
        self.failed_draws: dict[str, int] = {}                  # section id -> pictures that did not work out
        self.failed_why: dict[str, str] = {}                    # section id -> why the last try failed (the next one says so)
        self.decisions: dict[str, Decision] = {}                # section id -> the visual model's call on it
        self.queued: dict[str, tuple[str, Decision]] = {}       # section id -> block id and what to show, drawn at finish
        self.drawn_sections: dict[str, str] = {}                # section id -> the block drawn for it
        self.open_conflicts: dict[str, str] = {}                # section id -> where its text and picture disagree
        self.conflict_refusals = 0                              # finish calls refused over them
        # the step has a model of its own: pictures go there, and the writer's blocks are practice (`deep.agent`)
        self.visual_first = False
        self.plan: RefinePlan | None = None                     # plan_page's, once accepted
        self.planned: Page | None = None    # the page plan_page outlined (None: the input's own)
        # a practice run (`deep.agent.practice`): no plan, no text; one interactive is submitted
        self.practice = False
        self.submitted: str | None = None   # the interactive / block id its finish submitted
        # the pack's H8 EvoUI templates (read_ref adds them to their group's reference)
        self.evoui_extra: tuple[Mapping[str, Any], ...] = ()

    def drop_drawn(self, sid: str) -> None:
        """The section's picture leaves the draft, its placeholder with it; the text stays."""
        self.open_conflicts.pop(sid, None)
        bid = self.drawn_sections.pop(sid, None)
        if bid is None:
            return
        self.blocks.pop(bid, None)
        self.block_checks.pop(bid, None)
        s = self.sections.get(sid)
        if s is not None:
            body = "\n".join(ln for ln in s.body.splitlines() if block_placeholder(bid) not in ln)
            self.sections[sid] = s.model_copy(update={"body": body or s.body})

    def unplanned(self) -> bool:
        """The write tools refuse: no plan accepted yet (a practice run needs none)."""
        return self.plan is None and not self.practice

    def outline_ids(self) -> list[str]:
        return [] if self.page is None else [s.id for s in self.page.outline.sections]

    def outline_section(self, sid: str) -> OutlineSection | None:
        if self.page is None:
            return None
        return next((o for o in self.page.outline.sections if o.id == sid), None)

    def section_problem(self, s: RefinedSection) -> str | None:
        old = self.outline_section(s.section_id)
        if s.role is SectionRole.CHECK or (old is not None and old.role is SectionRole.CHECK):
            return "确认题段不能由精修改写（它们由出题流程单独核对）"
        if old is not None and self.page is not None and self.page.state(old.id).pinned:
            return f"段 {old.id} 已被用户锁定，不能改写，保留原样"
        if old is not None and self.scope and s.section_id not in self.scope:
            return f"段 {s.section_id} 不在这次精修的范围内：{', '.join(self.scope)}"
        if old is None and s.after is not None and s.after not in self.outline_ids() \
                and s.after not in self.sections:
            return f"after={s.after} 既不是页面上的段，也不是已写的新段"
        if bad := [c for c in s.cells if c not in self.page_cells]:
            return f"这些格子不属于本页概念：{', '.join(bad)}"
        if WRITE_STUB in s.body or READ_STUB in s.body:
            return ELIDED_BODY if s.section_id in self.sections else UNWRITTEN_ELIDED
        return None

    def ordered(self) -> list[RefinedSection]:
        """The written sections in final page order (`page_order`), not in write order."""
        return [self.sections[sid] for sid in page_order(self.outline_ids(), self.sections)
                if sid in self.sections]

    def draft(self, page_id: str) -> RefineDraft:
        """Raises ValueError (pydantic's ValidationError included) when the draft is unusable.
        Sections come in final page order."""
        if self.plan is None:
            raise ValueError(NO_PLAN)
        if not self.sections:
            raise ValueError("还没有写任何段")
        in_apps = {im.get("asset") for b in self.blocks.values() for im in app_images(b.block)}
        return RefineDraft(page_id=page_id, plan=self.plan, sections=tuple(self.ordered()),
                           interactives=tuple(self.interactives.values()),
                           blocks=tuple(self.blocks.values()),
                           # a picture of a picture that was drawn again is gone with it
                           images=tuple(m for m in self.images.values()
                                        if m.image_id not in self.drawn_images or m.image_id in in_apps))

    def best_draft(self, page_id: str) -> RefineDraft | None:
        """Whatever can be kept of an unfinished workspace (for `RefineJobOut.last_draft`): the
        draft, else the draft without its interactives (placeholders removed), else None."""
        if self.plan is None or not self.sections:
            return None
        try:
            return self.draft(page_id)
        except ValueError:
            pass
        try:
            return without_interactives(RefineDraft(
                page_id=page_id, plan=self.plan, sections=tuple(self.ordered())),
                set(placed_ids("\n".join(x.body for x in self.sections.values()))))
        except ValueError:
            return None

    def valid_check(self, iid: str) -> RenderCheck | None:
        d = self.interactives.get(iid)
        got = self.checks.get(iid)
        return got[1] if d is not None and got is not None and got[0] == check_key(d) else None

    def valid_block_check(self, bid: str) -> RenderCheck | None:
        b = self.blocks.get(bid)
        got = self.block_checks.get(bid)
        return got[1] if b is not None and got is not None and got[0] == block_json(b.block) \
            else None

    def section_known(self, sid: str) -> bool:
        return sid in self.outline_ids() or sid in self.sections

    def changed(self, sid: str) -> None:
        """Tells `on_change` that section `sid` shows something new; a failing listener never fails
        the write (the stream is a view; the draft is what gets committed)."""
        if self.on_change is None:
            return
        try:
            self.on_change(self, sid)
        except Exception:  # a listener problem is not the model's
            log.exception("on_change for %s failed", sid)

    def missing(self) -> list[str]:
        """Required sections not written yet, in page order."""
        return [sid for sid in self.required if sid not in self.sections]


# ---- read_spec ----

class ReadSpecArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part: Literal["items", "exemplar"] = Field(description="items = 教学规范条目；exemplar = 样板页")
    exemplar: int = Field(default=0, ge=0, le=2, description="第几个样板页（从 0 开始）")
    offset: int = Field(default=0, ge=0, description="样板页从第几个字开始读，每次最多 6000 字")


def spec_items_text(spec: TeachingSpec) -> str:
    lines = [f"教学规范 v{spec.version}（{len(spec.items)} 条）："]
    lines += [f"- [{i.item_id}] {item_text(i)}" for i in spec.items]
    if spec.exemplars:
        lines.append("样板页：" + "；".join(f"{n}. 《{e.title}》（{len(e.text)} 字）"
                                         for n, e in enumerate(spec.exemplars)))
    return "\n".join(lines)


class ReadSpecTool:
    def __init__(self, teaching: TeachingSpec):
        self._spec = teaching
        self.spec = spec("read_spec", "读用户的教学规范条目，或分段读样板页（用来校准讲法，不是要照抄的模板）。",
                         ReadSpecArgs, "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(ReadSpecArgs, args, self._run)

    def _run(self, a: ReadSpecArgs) -> ToolResult:
        if a.part == "items":
            return ToolResult(content=clip(spec_items_text(self._spec), 9000))
        if a.exemplar >= len(self._spec.exemplars):
            return fail("没有这个样板页")
        text = self._spec.exemplars[a.exemplar].text
        chunk = text[a.offset:a.offset + SPEC_CHUNK]
        if not chunk:
            return fail(f"offset 超出样板页长度 {len(text)}")
        end = a.offset + len(chunk)
        more = f"\n…[还有 {len(text) - end} 字，offset={end} 继续读]" if end < len(text) else ""
        return ToolResult(content=chunk + more)


def spec_part_text(teaching: TeachingSpec, part: str) -> str:
    """One part of the opening read_spec: "items" (the standard's items) or "exemplar" (the first
    exemplar's opening chunk; "" when there is none)."""
    if part == "items":
        return ReadSpecTool(teaching).run({"part": "items"}, _PREREAD).content
    if part == "exemplar":
        if not teaching.exemplars:
            return ""
        first = ReadSpecTool(teaching).run({"part": "exemplar"}, _PREREAD).content
        return f"样板页 0《{teaching.exemplars[0].title}》开头：\n{first}"
    raise ValueError(f"unknown spec part: {part}")


def preread_spec(teaching: TeachingSpec, parts: Sequence[str] = ("items", "exemplar")) -> str:
    """What the writer's opening read_spec calls returned (the items, then the first chunk of the
    first exemplar), given up front in the write prompt (deep@v17): every page began with these
    reads, two turns before the first section. `parts` narrows it (H8); "" when empty."""
    return "\n\n".join(t for p in parts if (t := spec_part_text(teaching, p)))


def preread_k(kview: KView, names: Mapping[str, str], cells: Sequence[str]) -> str:
    """What read_k returns for the concepts of the page's cells ("" when K has none of them)."""
    concepts = list(dict.fromkeys(split_cell(c)[0] for c in cells))
    if not concepts:
        return ""
    got = ReadKTool(kview, names).run({"concepts": concepts}, _PREREAD)
    return "" if got.error is not None else got.content


_PREREAD = ToolContext(user_id="", session_id="preread")


# ---- plan_page ----

@dataclass(frozen=True)
class OutlineSetup:
    """What plan_page needs to accept an outline: the page's spec and K (its cells and bands). Given
    only when the page has no outline yet or is being reorganized; None = the stored outline stays
    and plan_page takes the plan alone."""

    spec: PageSpec
    kview: KView


Outlined = Callable[[Page], None]        # the page plan_page outlined (OutlineEvent, the commit)


class PlanPageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outline: DraftOutline | None = Field(
        default=None, description="这一页的大纲（只在这一页还没有大纲时给；已经有大纲时不要给）")
    plan: PlanAnswers = Field(description="动笔前的规划")

    @field_validator("outline", "plan", mode="before")
    @classmethod
    def _decoded(cls, v: Any) -> Any:
        """Claude Haiku often sends a nested object as a JSON string (eight rejected plans on one real page)."""
        if isinstance(v, str):
            with contextlib.suppress(ValueError):
                return json.loads(v)
        return v


class PlanPageTool:
    """The writer's first call: the plan, and the outline when `setup` is given. The outline goes
    through `accept_draft`; what it refuses comes back as a tool error listing every problem
    (nothing is set). Accepted: `ws.plan`, and with an outline `ws.page` / `ws.planned` (fresh
    fold states, no texts, the K snapshot) and `ws.required` (every non-check section), then
    `on_outline(page)`. A listener that raises is not swallowed: nothing is set and the call is
    an error (the page writer's listener halts its run itself, `wikiapp.organize`). A second call
    replaces both while no section is written (a call with `plan` alone after an accepted outline
    keeps that outline); after that it is refused."""

    def __init__(self, ws: Workspace, *, known: str, setup: OutlineSetup | None = None,
                 on_outline: Outlined | None = None):
        self._ws, self._known, self._setup, self._on_outline = ws, known, setup, on_outline
        self.spec = spec("plan_page", PLAN_PAGE_DESCRIPTION, PlanPageArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(PlanPageArgs, args, self._run)

    def _run(self, a: PlanPageArgs) -> ToolResult:
        ws = self._ws
        if ws.sections:
            return fail("已经开始写了，规划和大纲不能再改；接着写")
        if self._setup is None and a.outline is not None:
            return fail("这一页已经有大纲了：只给 plan，不给 outline")
        if self._setup is not None and a.outline is None and ws.planned is None:
            return fail("这一页还没有大纲：outline 必填")
        try:
            plan = RefinePlan(known=self._known, observable=a.plan.observable,
                              one_at_a_time=tuple(a.plan.one_at_a_time), contrast=a.plan.contrast,
                              spoken_answer=a.plan.spoken_answer, practice=a.plan.practice,
                              visual_questions=tuple(a.plan.visual_questions))
        except ValidationError as e:
            return fail(f"plan 不合法：{e.errors(include_url=False)[0]['msg']}")
        if self._setup is None or a.outline is None:
            ws.plan = plan
            return ToolResult(content="规划已记下。开始写第一段。")
        got = accept_draft(a.outline, self._setup.spec, self._setup.kview)
        if got.outline is None:
            lines = "\n".join(f"- {v.message}" for v in got.problems)
            # the outline the model gave, refused: a rejection on the write run (plan 2026-10-29)
            reject("outline refused", problems=[v.message for v in got.problems])
            return ToolResult(content=clip(f"大纲没有记下，这些问题要改（改好后再调用 plan_page，"
                                           f"给完整的大纲）：\n{lines}", 4000),
                              error="outline refused")
        outline = accept_outline_checks(got.outline)
        page = Page(outline=outline, states=states_for(outline), texts={},
                    snapshot=k_snapshot(self._setup.spec, self._setup.kview),
                    unresolved_conflicts=(), conflicts=())
        if self._on_outline is not None:
            self._on_outline(page)          # raises: nothing is set (run_guarded: an error)
        ws.plan, ws.page, ws.planned = plan, page, page
        ws.required = tuple(x.id for x in outline.sections if x.role is not SectionRole.CHECK)
        parts = [f"大纲和规划已记下：{len(outline.sections)} 段（"
                 + "、".join(f"{x.id} {x.heading}" for x in outline.sections) + "）。"]
        if got.dropped:
            parts.append("去掉了这些确认题：\n" + "\n".join(f"- {v.message}" for v in got.dropped))
        if got.notes:
            parts.append("对照每个格子的建议，系统看到这些地方（供参考，不是错误）：\n"
                         + "\n".join(f"- {v.message}" for v in got.notes))
        return ToolResult(content=clip("\n\n".join(parts), 4000))


PLAN_PAGE_DESCRIPTION = ("动笔前先调用：交上这一页的规划（plan）；这一页还没有大纲时同时交上大纲（outline）。"
                         "大纲不合格会返回要改的问题。第一段写好之前可以再调用来修改。")
# The outline review (pass 2's effect, stage 4) is asked for in the writer's first message
# (deep.agent.outline_part), not here: every deep tool result is fenced as material
# (AgentSpec.fence_reads, all deep tools have effect "read"), so a request in a result would
# read as data.


# ---- write_section ----

class WriteSectionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(min_length=1, max_length=64,
                            description="已有段的 id = 改写这一段；新 id = 插入新段")
    after: str | None = Field(default=None, description="新段插在哪一段之后（已有段或已写的新段）；"
                                                        "省略 = 页首。改写已有段时忽略")
    heading: str = Field(min_length=1, max_length=120)
    role: Literal["explain", "recap", "line"] = Field(description=(
        "explain：正常讲解；recap：学过但模糊的内容，简短回顾；line：确定会的，一句带过"))

    @field_validator("role", mode="before")
    @classmethod
    def _retired(cls, v: object) -> object:
        return RETIRED_ROLES.get(v, v) if isinstance(v, str) else v
    depth: Depth
    cells: list[str] = Field(default_factory=list, description="这一段讲的 K 格子 id；改写时省略 = 保留原来的")
    cue: str = Field(min_length=1, max_length=60, description="复习题，只有一个问号；学习者看不到页面，不提这一页的例子")
    answer: str = Field(min_length=1, max_length=200, description="一两句话回答 cue，不提这一页的例子")
    body: str = Field(min_length=1, max_length=20_000,
                      description="Markdown 正文；交互单独一行写 {{interactive:<id>}}")
    simplifications: list[str] = Field(default_factory=list, max_length=6,
                                       description="为了好懂做的简化，会在页面上注明")


def _without_heading(body: str, heading: str) -> str:
    """The note writes `heading` above the body: the same heading again as the body's first line
    (live run 2026-11-04) would show twice."""
    first, _, rest = body.lstrip().partition("\n")
    return rest.lstrip("\n") if first.lstrip("#").strip() == heading.strip() and first.startswith("#") else body


class WriteSectionTool:
    def __init__(self, ws: Workspace):
        self._ws = ws
        self.spec = spec("write_section", "写一段（改写已有段，或插入新段）。同一 id 再写一次会覆盖草稿。",
                         WriteSectionArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(WriteSectionArgs, args, self._run)

    def _run(self, a: WriteSectionArgs) -> ToolResult:
        from evowiki.agent.text_visuals import has_character_diagram
        if has_character_diagram(a.body):
            return fail("不要用字符拼示意图；使用 draw_visual 画示意图，或用 find_image/place_image 配真实图片。代码、公式和表格照常写。")
        ws = self._ws
        if ws.plan is None:
            return fail(NO_PLAN)
        old = ws.outline_section(a.section_id)
        cells = tuple(a.cells) if a.cells or old is None else old.cells
        try:
            s = RefinedSection(section_id=a.section_id, after=None if old else a.after,
                               heading=a.heading, role=SectionRole(a.role), depth=a.depth,
                               cells=cells, cue=a.cue, answer=a.answer,
                               body=_without_heading(a.body, a.heading),
                               simplifications=tuple(a.simplifications))
        except ValidationError as e:
            return fail(f"段不合法：{e.errors(include_url=False)[0]['msg']}")
        if why := ws.section_problem(s):
            return fail(why)
        ws.sections[s.section_id] = s
        ws.finished = None
        ws.changed(s.section_id)
        kind = ("新段" if old is None else
                "改写" if ws.page is not None and s.section_id in ws.page.texts else "写好")
        left = ws.missing()
        todo = f"；还没写：{', '.join(left)}" if left else ""
        return ToolResult(content=f"已写入草稿：{s.section_id}（{kind}，{len(s.body)} 字）{todo}{_visual_note(ws, s)}")


def _visual_note(ws: Workspace, s: RefinedSection) -> str:
    """After a section is written, with a visualization step of its own: the corrected text is held against the picture
    it disagreed with, and a section not decided yet gets the visual model's call."""
    if not ws.visual_first or ws.visual is None:
        return ""
    sid = s.section_id
    if sid in ws.open_conflicts:
        block = ws.blocks.get(ws.drawn_sections.get(sid, ""))
        drawn = "" if block is None else block.block.body.get("html") or block_json(block.block)
        left = ws.visual.conflict_left(s.body, ws.open_conflicts[sid], drawn)
        if left:
            ws.open_conflicts[sid] = left
            return f"\n正文和图仍然对不上：{left}"
        del ws.open_conflicts[sid]
        return "\n正文和图对上了。"
    if sid in ws.decisions or sid in ws.drawn_sections or "```evoui" in s.body:
        return ""
    decided = tuple(f"{ws.sections[i].heading}：{d.show}" for i, d in ws.decisions.items() if d.draw and i in ws.sections)
    got = ws.visual.decide(s.heading, s.body, ws.plan.visual_questions if ws.plan else (), decided,
                           tuple(o.heading for o in ws.page.outline.sections) if ws.page is not None else ())
    ws.decisions[sid] = got
    if got.unanswered:
        return f"\n（这一段没有得到判断，按不画处理：{got.why}）"
    if not got.draw:
        return ""
    return (f"\n这一段会配一张图（{got.why}）。图上：{got.show} 动手：{got.touch}\n"
            f"不用调用 draw_visual：调用 finish 时所有的图一起画好，放在这一段末尾；想放在别处，正文里单独一行写 "
            f"{block_placeholder(_block_id_for(ws, sid))}。")


def _block_id_for(ws: Workspace, sid: str) -> str:
    """The block of a picture nobody named: a placeholder the writer left in the section, else one of its own."""
    s = ws.sections.get(sid)
    left = [] if s is None else [x for x in re.findall(r"\{\{block:([^{}\s]+)\}\}", s.body) if x not in ws.blocks]
    return left[0] if left else re.sub(r"[^a-z0-9-]", "-", f"viz-{sid}".lower())


def _without_placeholder(ws: Workspace, sid: str, bid: str) -> None:
    s = ws.sections[sid]
    body = "\n".join(ln for ln in s.body.splitlines() if block_placeholder(bid) not in ln)
    ws.sections[sid] = s.model_copy(update={"body": body or s.body})


def _place_drawn(ws: Workspace, sid: str, bid: str, got: Drawn, why: str) -> None:
    """A finished picture goes into the workspace (either route, or a library component)."""
    ws.failed_why.pop(sid, None)
    ws.drawn_sections[sid] = bid
    if got.interactive is not None and got.component is not None:
        ws.blocks.pop(bid, None)
        ws.interactives[bid] = got.interactive
        ws.placements[bid] = (got.component, why)
    else:
        assert got.block is not None
        ws.interactives.pop(bid, None)
        for m in got.images:
            ws.images[m.image_id] = m
            ws.drawn_images.add(m.image_id)
        ws.blocks[bid] = got.block
        ws.block_checks[bid] = (block_json(got.block.block), got.check)
    if got.conflict:
        ws.open_conflicts[sid] = got.conflict
    else:
        ws.open_conflicts.pop(sid, None)
    ws.finished = None
    ws.changed(sid)


def draw_waiting(ws: Workspace) -> str:
    """Draws what the page is waiting for, side by side, on the text as it now stands (`VisualStep.draw_many`): what draw_visual
    queued, and every section the visual model decided to draw. Returns what the writer has to know, "" when nothing waited.
    A finished picture lands in its section at once: at the end of it, unless the writer left a placeholder elsewhere."""
    if ws.visual is None:
        return ""
    waiting = dict(ws.queued)
    if ws.visual_first:
        for sid, d in ws.decisions.items():
            if d.draw and sid in ws.sections and sid not in ws.drawn_sections and sid not in ws.failed_draws:
                waiting.setdefault(sid, (_block_id_for(ws, sid), d))
    if not waiting:
        return ""
    briefs = []
    for sid in page_order(ws.outline_ids(), ws.sections):
        if sid in waiting:
            bid, w = waiting[sid]
            s = ws.sections[sid]
            briefs.append(Brief(block_id=bid, section_id=sid, heading=s.heading, body=s.body, invisible=w.invisible, draw=w.show,
                                touch=w.touch, photos=w.photos if ws.image_search is not None else (),
                                tried=ws.failed_why.get(sid, "")))

    def land(b: Brief, got: Drawn) -> None:
        s = ws.sections[b.section_id]
        if not re.search(rf"\{{\{{(?:block|interactive):{re.escape(b.block_id)}\}}\}}", s.body):
            place = placeholder(b.block_id) if got.component is not None else block_placeholder(b.block_id)
            ws.sections[b.section_id] = s.model_copy(update={"body": f"{s.body.rstrip()}\n\n{place}"})
        _place_drawn(ws, b.section_id, b.block_id, got, b.invisible[:200] or b.heading)

    results = ws.visual.draw_many(briefs, land)
    ws.queued.clear()
    lines = []
    for b in briefs:
        got = results[b.section_id]
        if isinstance(got, str):
            ws.failed_draws[b.section_id] = ws.failed_draws.get(b.section_id, 0) + 1
            ws.failed_why[b.section_id] = got
            _without_placeholder(ws, b.section_id, b.block_id)
            ws.finished = None
            ws.changed(b.section_id)
            lines.append(f"{b.section_id}：这张图没有画成（{got}）。这一段用文字讲清楚。")
            continue
        land(b, got)                  # a durable replay did not run `land` during the step
        if got.conflict:
            lines.append(f"{b.section_id}：图画好了（{b.block_id}），但和正文对不上：{got.conflict}。不能两边各写各的：正文错了就用 "
                         f"write_section 改这一段（改完会再核对）；图错了就调用 draw_visual（section_id={b.section_id}，revise=要改什么）。")
        else:
            lines.append(f"{b.section_id}：{b.block_id} 图上画的：{got.design}")
    return ("这一页的图画完了，都已放进各段：\n" + "\n".join(lines)
            + "\n正文对图的说法要和上面一致，要改就用 write_section；没有要改的就再调用 finish。")


# ---- place_component ----

class PlaceComponentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component_id: str = Field(description="组件库里的组件 id（见「可用的交互组件」清单）")
    section_id: str = Field(description="放在哪一段（正文里写 {{interactive:<placement_id>}}）")
    why: str = Field(min_length=1, max_length=300,
                     description="为什么选这个组件：它让学习者动手看见什么，一句话")
    config: dict[str, Any] | None = Field(
        default=None, description="[可配置] 组件必填：按 title 是这个组件 id 的那一项格式写这一页主题的内容；"
                                  "[固定内容] 组件给 null")
    placement_id: str | None = Field(
        default=None, description="同一个组件在一页里放第二次时给一个新 id（小写字母、数字、连字符）")


def place_component_schema(components: Mapping[str, LibraryComponent]) -> dict[str, Any]:
    """`PlaceComponentArgs`' JSON Schema typed for this library: `component_id` an enum of its ids,
    `config` null or one configurable component's own `config_schema` (each branch titled with its
    component id), so the model reads every config's fields, types, enums and descriptions as JSON
    Schema (the live run 2026-10-28 could not decode the old one-line shape notation). Not strict on
    either provider: `instantiate` still validates the config against the chosen component's schema."""
    out = PlaceComponentArgs.model_json_schema()
    if not components:
        return out
    props = out["properties"]
    props["component_id"] = {"type": "string", "enum": list(components),
                             "description": props["component_id"]["description"]}
    branches = [{**c.config_schema, "title": cid} for cid, c in components.items()
                if c.config_schema is not None]
    props["config"] = {"description": props["config"]["description"],
                       "anyOf": [{"type": "null"}, *branches]}
    return out


class PlaceComponentTool:
    """Places a library component on a section. Only ids in the library are accepted; the same
    component placed again moves to the new section. With a `checker` the placement is render-checked
    at once and the result says how it went (stage 4 T4; a failure is a tool error, the component
    stays placed for the writer to fix or drop). The tool's schema is this library's
    (`place_component_schema`): a workspace without one (the harness's empty build) gets the plain args."""

    def __init__(self, ws: Workspace, checker: "Checker | None" = None):
        self._ws, self._checker = ws, checker
        base = spec("place_component",
                    "从交互组件库里选一个练习组件放到某一段（只能用库里已有的）。要画出来让他看懂的用 draw_visual，不用组件。"
                    "同一个组件只放一次，再放会换到新的段。",
                    PlaceComponentArgs, "read", "draft")
        comps = ws.components if ws is not None else {}
        self.spec = base.model_copy(update={"parameters": place_component_schema(comps)})

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(PlaceComponentArgs, args, self._run)

    def _run(self, a: PlaceComponentArgs) -> ToolResult:
        if self._ws.unplanned():
            return fail(NO_PLAN)
        c = self._ws.components.get(a.component_id)
        if c is None:
            return fail(f"组件库里没有 {a.component_id}；可用：{', '.join(self._ws.components)}")
        try:
            html, probes, title, question = instantiate(c, a.config)
        except ConfigInvalid as e:
            return fail(f"config 不合法，没有放置（{e}）；按 config 参数里 title 为 {c.component_id} 的那一项改好再放")
        if problems := cell_problems(a.config, self._ws.page_cells):
            return fail("config 不合法，没有放置：" + "；".join(problems))
        pid = a.placement_id or c.component_id
        try:
            d = InteractiveDraft(interactive_id=pid, section_id=a.section_id, title=title,
                                 learning_question=question, html=html, height=c.height,
                                 probes=probes)
        except ValidationError as e:
            return fail(f"组件不合法：{e.errors(include_url=False)[0]['msg']}")
        self._ws.interactives[d.interactive_id] = d
        self._ws.placements[d.interactive_id] = (c.component_id, a.why)
        self._ws.finished = None
        self._ws.changed(d.section_id)
        head = f"已放置组件：{d.interactive_id}《{d.title}》。" + (
            "" if self._ws.practice else f"正文里单独一行放 {placeholder(d.interactive_id)}。")
        if self._checker is None:
            return ToolResult(content=head)
        return checked(head, check(self._ws, self._checker, d))


# ---- render_check ----

class RenderCheckArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interactive_id: str = Field(description="组件的 placement id 或 EvoUI 块的 block_id")


def check_text(c: RenderCheck) -> str:
    head = "通过" if c.ok else "没通过"
    parts = [f"{c.interactive_id}：{head}；试操作 {c.probes_passed}/{c.probes_total}"]
    if c.console_errors:
        parts.append("控制台报错：" + " | ".join(c.console_errors))
    if c.page_errors:
        parts.append("页面异常：" + " | ".join(c.page_errors))
    if c.network_requests:
        parts.append(f"网络请求 {c.network_requests} 次（交互必须离线）")
    if c.overflow_px:
        parts.append(f"横向溢出 {c.overflow_px}px")
    if c.detail:
        parts.append(c.detail)
    if c.readings:
        parts.append("屏幕读数（第一行是初始文字，之后是每次试操作时会变的那些行）：\n" + "\n".join(c.readings)
                     + "\n对照你写的结论和数字，都成立吗？对不上就改块或正文。")
    return "\n".join(parts)


class RenderCheckTool:
    def __init__(self, ws: Workspace, checker: "Checker"):
        self._ws, self._checker = ws, checker
        self.spec = spec("render_check", "在无头浏览器里按 375px 和 1280px 打开组件或 EvoUI 块，查报错、"
                                         "联网、横向溢出，动一次每个控件看文字是否变化，并返回试操作后的屏幕读数。", RenderCheckArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(RenderCheckArgs, args, self._run)

    def _run(self, a: RenderCheckArgs) -> ToolResult:
        b = self._ws.blocks.get(a.interactive_id)
        d = self._ws.interactives.get(a.interactive_id)
        if b is not None:
            result = check_block(self._ws, self._checker, b)
        elif d is not None:
            result = self._ws.valid_check(d.interactive_id) or check(self._ws, self._checker, d)
        else:
            return fail(f"没有交互或块 {a.interactive_id}")
        return ToolResult(content=clip(check_text(result), 4000),
                          error=None if result.ok else "render check failed")


def checked(head: str, c: RenderCheck) -> ToolResult:
    """A placement's result with its render check (a failure is a tool error)."""
    return ToolResult(content=clip(f"{head}\n渲染检查：{check_text(c)}", 4000),
                      error=None if c.ok else "render check failed")


def check(ws: Workspace, checker: RenderChecker, d: InteractiveDraft) -> RenderCheck:
    """Runs (or reuses) the check of this exact draft. A pass is remembered; a failure is not: the
    html is the library's and cannot be edited, so only a re-run (the browser may have flaked) can
    change the outcome."""
    if (got := ws.valid_check(d.interactive_id)) is not None:
        return got
    result = checker.check(d)
    if result.ok:
        ws.checks[d.interactive_id] = (check_key(d), result)
    return result


def check_block(ws: Workspace, checker: BlockChecker, b: BlockDraft) -> RenderCheck:
    """Runs (or reuses) the check of this exact block; a pass is remembered."""
    if (got := ws.valid_block_check(b.block_id)) is not None:
        return got
    pictures = {im["id"]: ws.images[im["asset"]].data_b64 for im in app_images(b.block)
                if im.get("asset") in ws.images}
    result = checker.check_block(b.block_id, b.block, pictures) if pictures else checker.check_block(b.block_id, b.block)
    if result.ok:
        ws.block_checks[b.block_id] = (block_json(b.block), result)
    return result


class Checker(RenderChecker, BlockChecker, Protocol):
    """What refine's render step needs: components and EvoUI blocks."""


# ---- write_block ----

class WriteBlockArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(description="页内唯一，小写字母、数字、连字符；正文里单独一行写 {{block:<block_id>}}")
    section_id: str = Field(description="放在哪一段")
    why: str = Field(min_length=1, max_length=300,
                     description="它的用处，一句话：练熟（他要记住或会做的是什么）或辨析（哪几种情况容易混）。要让他看懂的用 draw_visual")
    block: dict[str, Any] = Field(description="EvoUI 块（JSON 对象，格式见 EvoUI 格式说明）")

    @field_validator("block", mode="before")
    @classmethod
    def _decoded(cls, v: Any) -> Any:
        if isinstance(v, str):
            with contextlib.suppress(ValueError):
                return json.loads(v)
        return v


MAX_BLOCK_ERRORS = 8


def _draws_a_picture(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("kind") in ("plot", "chart"):
        return True
    kids = [*(node.get("children") or ()), node.get("then"), node.get("else")]
    return any(_draws_a_picture(k) for k in kids)


class WriteBlockTool:
    """Composes an EvoUI block. The block must pass the validator (the same rules as the renderer);
    the problems go back to the model and nothing is stored until it does. A stored block is
    render-checked at once when there is a `checker` (stage 4 T4), as place_component does."""

    def __init__(self, ws: Workspace, checker: "Checker | None" = None):
        self._ws, self._checker = ws, checker
        self.spec = spec("write_block",
                         "写一个 EvoUI 交互块放到某一段（声明式 JSON：练习、测验、闪卡这些；要画出来让他看懂的用 draw_visual）。"
                         "同一 block_id 再写一次会覆盖。", WriteBlockArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(WriteBlockArgs, args, self._run)

    def _run(self, a: WriteBlockArgs) -> ToolResult:
        ws = self._ws
        if ws.unplanned():
            return fail(NO_PLAN)
        if ws.visual_first and (_draws_a_picture(a.block.get("body"))):
            return fail("图表和坐标图是让他看懂的东西，用 draw_visual（说清看不见的是什么、画什么、动手时什么变）；write_block 只写练习和辨析，不画图")
        if a.block_id in ws.interactives:
            return fail(f"{a.block_id} 已经是一个组件的 id，换一个 block_id")
        if not ws.section_known(a.section_id):
            return fail(f"没有段 {a.section_id}：先用 write_section 写这一段")
        problems = validate_block(a.block)
        if app_images(a.block) and not all(im.get("asset") in ws.drawn_images for im in app_images(a.block)):
            problems.append("小程序的 images 只能由 draw_visual 放")
        foreign = sorted(c for c in quiz_cells(a.block) if c not in ws.page_cells)
        if foreign:          # a quiz answer / flashcard rating / app item becomes K evidence: only this page's cells
            problems.append(f"quiz、闪卡、填空和拼句的 cell 只能用这一页的格子（小程序 items 的 cell 也一样："
                            f"{', '.join(sorted(ws.page_cells))}），"
                            f"不能用 {', '.join(foreign)}")
        if problems:
            more = f"（还有 {len(problems) - MAX_BLOCK_ERRORS} 处）" if len(problems) > MAX_BLOCK_ERRORS else ""
            return fail("块不合法，没有保存：" + "；".join(problems[:MAX_BLOCK_ERRORS]) + more)
        try:
            draft = BlockDraft(block_id=a.block_id, section_id=a.section_id, why=a.why,
                               block=EvoUIBlock.model_validate(a.block))
        except ValidationError as e:
            return fail(f"块不合法：{e.errors(include_url=False)[0]['msg']}")
        ws.blocks[draft.block_id] = draft
        ws.finished = None
        ws.changed(draft.section_id)
        head = f"已写入块：{draft.block_id}《{draft.block.title}》。" + (
            "" if ws.practice else f"正文里单独一行放 {block_placeholder(draft.block_id)}。")
        if self._checker is None:
            return ToolResult(content=head)
        return checked(head, check_block(ws, self._checker, draft))


# ---- find_image / place_image ----

class FindImageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=120,
                       description="图库里的检索词，用英文效果最好（例如 human heart anatomy diagram）")
    purpose: str = Field(min_length=1, max_length=200,
                         description="这张图要让学习者看见什么、帮他懂哪一点，一句话")
    limit: int = Field(default=5, ge=1, le=8, description="最多返回几张候选")


FIND_IMAGE_DESCRIPTION = (
    "在开放授权图库（Wikimedia Commons，再到 Openverse）里找真实的照片或示意图。只返回允许转载的授权"
    "（公有领域、CC0、CC BY、CC BY-SA），每张带缩略图、标题、说明、作者和授权。看图挑一张真正能回答 purpose 的，"
    "用 place_image 放到页面上；都不合适就不放。生物、矿物、器官用学名或解剖学名搜。")


class FindImageTool:
    def __init__(self, ws: Workspace):
        self._ws = ws
        self.spec = spec("find_image", FIND_IMAGE_DESCRIPTION, FindImageArgs, "network",
                         parallel=True)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(FindImageArgs, args, self._run)

    def _run(self, a: FindImageArgs) -> ToolResult:
        ws = self._ws
        if ws.image_search is None:
            return fail("这次写作不能找图")
        hits = ws.image_search.search(a.query, a.limit)
        if not hits:
            return ToolResult(content=f"没有找到可以转载的图片：{a.query}。换一个检索词，或者不放图。")
        lines = [f"找到 {len(hits)} 张可以转载的图片（{a.purpose}），能看到缩略图的按这个顺序附在后面："]
        thumbs: list[ImagePart] = []
        for c in hits:
            cid = f"img{len(ws.image_candidates) + 1}"
            ws.image_candidates[cid] = c
            if (t := ws.image_search.thumbnail(c)) is not None:
                thumbs.append(ImagePart(media_type=MIME, sha256=hashlib.sha256(t.data).hexdigest(),
                                        data_b64=t.data_b64, name=cid))
            size = f"{c.width}×{c.height}" if c.width and c.height else "尺寸未知"
            desc = f" — {clip(c.description, 160)}" if c.description else ""
            lines.append(f"[{cid}] {c.title}{desc} | 作者 {c.author or '未署名'} | {c.license} | "
                         f"{size} | {PROVIDER_NAMES[c.provider]}")
        lines.append("用 place_image 放其中一张（candidate 填方括号里的 id）；图和说明对不上这一段要讲的就不要放。")
        return ToolResult(content=clip("\n".join(lines), 4000), images=tuple(thumbs))


class PlaceImageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str = Field(description="find_image 返回的候选 id（如 img2）")
    image_id: str = Field(description="页内唯一，小写字母、数字、连字符；正文里单独一行写 {{image:<image_id>}}")
    section_id: str = Field(description="放在哪一段")
    alt: str = Field(min_length=1, max_length=200,
                     description="用中文说清图里有什么（给看不见图的读者），一句话")


class PlaceImageTool:
    """Downloads the chosen candidate (provider host only), re-encodes it and keeps it in the draft.
    The attribution comes from the library's metadata, never from the model."""

    def __init__(self, ws: Workspace):
        self._ws = ws
        self.spec = spec("place_image",
                         "把 find_image 找到的一张图放到某一段。图下面会自动用小字写上来源、作者和授权，"
                         "正文里不用再写。同一 image_id 再放一次会替换。", PlaceImageArgs, "network", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(PlaceImageArgs, args, self._run)

    def _run(self, a: PlaceImageArgs) -> ToolResult:
        ws = self._ws
        if ws.plan is None:
            return fail(NO_PLAN)
        c = ws.image_candidates.get(a.candidate)
        if ws.image_search is None or c is None:
            return fail(f"没有候选 {a.candidate}：先用 find_image 找图，candidate 填它返回的 id")
        if a.image_id in ws.interactives or a.image_id in ws.blocks:
            return fail(f"{a.image_id} 已经是一个交互或块的 id，换一个 image_id")
        if not ws.section_known(a.section_id):
            return fail(f"没有段 {a.section_id}：先用 write_section 写这一段")
        try:
            img = ws.image_search.fetch(c)
        except ImageRejected as e:
            return fail(f"这张图不能用：{e}。换一张候选，或者不放图")
        try:
            d = ImageDraft(image_id=a.image_id, section_id=a.section_id, alt=a.alt, title=c.title,
                           author=c.author, license=c.license, license_url=c.license_url,
                           source=PROVIDER_NAMES[c.provider], source_url=c.source_url,
                           width=img.width, height=img.height, data_b64=img.data_b64)
        except ValidationError as e:
            return fail(f"图片不合法：{e.errors(include_url=False)[0]['msg']}")
        ws.images[d.image_id] = d
        ws.finished = None
        ws.changed(d.section_id)
        return ToolResult(content=f"已放置图片：{d.image_id}《{d.title}》（{d.width}×{d.height}，"
                                  f"{d.license}）。记得在段 {d.section_id} 的正文里单独一行放 "
                                  f"{image_placeholder(d.image_id)}，并在前后用一句话告诉学习者看图里的什么。")


# ---- draw_visual ----

class DrawVisualArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(description="页内唯一，小写字母、数字、连字符；正文里单独一行写 {{block:<block_id>}}")
    section_id: str = Field(description="放在哪一段（先写好这一段）")
    invisible: str = Field(default="", max_length=400, description="（建议，可不填）这段内容里看不见的是什么")
    draw: str = Field(default="", max_length=400, description="（建议，可不填）想让他在屏幕上看到什么")
    touch: str = Field(default="", max_length=400, description="（建议，可不填）他动手时什么跟着变")
    photos: list[str] = Field(default_factory=list, max_length=3, description="（建议，可不填）图库检索词")
    revise: str = Field(default="", max_length=600, description=(
        "这一段的图已经画好、要改时写要改什么（比如让图按正文的数字来）；不从零重画，已放的图片留着"))


MAX_FAILED_DRAWS = 2                  # per section


class DrawVisualTool:
    """Hands one 看懂 picture to the visualization step (`deep.visual`), which writes, checks and looks at it."""

    def __init__(self, ws: Workspace):
        self._ws = ws
        self.spec = spec("draw_visual",
                         "把看不见的东西画出来，让他看着图、动手试就懂。调用 finish 时所有的图一起画好，放到各段里，"
                         "写完正文再看结果。一段只画一次；画好的要改就用 revise，当场改。",
                         DrawVisualArgs, "network", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(DrawVisualArgs, args, self._run)

    def _run(self, a: DrawVisualArgs) -> ToolResult:
        ws = self._ws
        if ws.unplanned():
            return fail(NO_PLAN)
        if ws.visual is None:
            return fail("这次写作不能画图")
        if a.block_id in ws.images:
            return fail(f"{a.block_id} 已经是一张图片的 id，换一个 block_id")
        sec = ws.sections.get(a.section_id)
        old = ws.outline_section(a.section_id)
        if sec is None and not ws.practice:
            return fail(f"段 {a.section_id} 还没写：先用 write_section 写这一段")
        if not ws.section_known(a.section_id):
            return fail(f"没有段 {a.section_id}")
        done = ws.drawn_sections.get(a.section_id)
        if a.revise and done is None:
            return fail(f"段 {a.section_id} 还没有画过，没有可改的")
        if done is not None and not a.revise:
            return fail(f"段 {a.section_id} 已经画过（{done}），一段只画一次。要改就用 revise 说改什么，正文里放 {block_placeholder(done)}。")
        if a.revise and done not in ws.blocks:
            return fail(f"{done} 是预制组件，不能改")
        decision = ws.decisions.get(a.section_id)
        if decision is not None and not decision.draw and not a.revise:
            return fail(f"这一段不用画（{decision.why}）。")
        if ws.failed_draws.get(a.section_id, 0) >= MAX_FAILED_DRAWS:
            return fail("这一段的图已经试过了，没有画成。用文字讲清楚，正文里不要放它的占位符。")
        queued = ws.queued.get(a.section_id)
        if not a.revise and not ws.practice:
            block_id = queued[0] if queued else a.block_id
            ws.queued[a.section_id] = (block_id, decision if decision is not None else Decision(
                True, invisible=a.invisible, show=a.draw, touch=a.touch, photos=tuple(a.photos)))
            ws.finished = None
            return ToolResult(content=f"排上了：调用 finish 时和其他图一起画，放在这一段末尾；想放在别处，正文里单独一行写 {block_placeholder(block_id)}。")
        heading = sec.heading if sec is not None else (old.heading if old is not None else a.section_id)
        body = sec.body if sec is not None else section_text_of(ws, a.section_id)
        wanted = decision if decision is not None else Decision(True, invisible=a.invisible, show=a.draw, touch=a.touch,
                                                                photos=tuple(a.photos))
        block_id = done or a.block_id
        brief = Brief(block_id=block_id, section_id=a.section_id, heading=heading, body=body,
                      invisible=wanted.invisible, draw=wanted.show, touch=wanted.touch,
                      photos=wanted.photos if ws.image_search is not None else (),
                      tried=ws.failed_why.get(a.section_id, ""))
        try:
            if a.revise:
                used = {im["id"]: (ws.images[im["asset"]], im) for im in app_images(ws.blocks[block_id].block)
                        if im.get("asset") in ws.images}
                got = ws.visual.revise(brief, ws.blocks[block_id], a.revise, used)
            else:
                got = ws.visual.draw(brief)
        except VisualFailed as e:
            ws.failed_why[a.section_id] = str(e)
            if a.revise:
                return fail(f"没有改成（{e}），原来的图还在。")
            ws.failed_draws[a.section_id] = ws.failed_draws.get(a.section_id, 0) + 1
            return fail(f"这张图没有画成（{e}）。这一段用文字讲清楚，正文里不要放它的占位符。")
        _place_drawn(ws, a.section_id, block_id, got, wanted.invisible[:200] or heading)
        if got.interactive is not None and got.component is not None:
            where = "" if ws.practice else f"正文里单独一行放 {placeholder(block_id)}。"
            return ToolResult(content=clip(f"已放置组件 {got.component}：{block_id}。{where}\n图上画的：{got.design}\n"
                                           f"渲染检查：{check_text(got.check)}\n正文对这张图的说法要和上面一致。", 4000))
        place = "" if ws.practice else f"正文里单独一行放 {block_placeholder(block_id)}。"
        if got.conflict:
            return ToolResult(content=clip(
                f"图画好了，但和正文对不上：{got.conflict}\n{place}\n"
                f"不能两边各写各的，二选一：正文错了就用 write_section 改段 {a.section_id}（改完会再核对）；"
                f"图错了就调用 draw_visual（section_id={a.section_id}，revise=要改什么）。解决前不能 finish。", 4000),
                error="the picture and the text disagree")
        return ToolResult(content=clip(f"已画好：{block_id}。{place}\n图上画的：{got.design}\n"
                                       f"渲染检查：{check_text(got.check)}\n"
                                       + "\n".join(got.notes)
                                       + "\n正文对这张图的说法要和上面一致。", 4000))


def section_text_of(ws: Workspace, sid: str) -> str:
    """The stored text of a written page's section (a practice run writes none of its own)."""
    if ws.page is None:
        return ""
    t = ws.page.texts.get(sid)
    return t.body if t is not None else ""


# ---- finish ----

MAX_CONFLICT_REFUSALS = 2
BUILTIN_TEXT = {c.id: c.text for c in CHECKLIST}


def check_rules(teaching: TeachingSpec) -> dict[str, str]:
    """Every rule the self-check covers: the builtin keys, then each spec item in force."""
    return {**{r: BUILTIN_TEXT[r] for r in BUILTIN_REVIEW_RULES},
            **{i.item_id: item_text(i) for i in teaching.items}}


class SelfCheckArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: str = Field(min_length=1, max_length=80, description="规则 id（自查清单里的一条，每条一项）")
    ok: bool = Field(description="整页都满足这条规则")
    section_id: str | None = Field(default=None, description="不满足或改过的段")
    note: str = Field(default="", max_length=500, description="怎么满足的，或改了什么、还差什么")


class SeriesArgs(BaseModel):
    """The page's series update as the writer states it; the limits live in `SeriesUpdate`, so a
    too-long field comes back as a refusal the writer can fix."""
    model_config = ConfigDict(extra="forbid")

    example: str = Field(default="", description="贯穿全系列的例子：第一页选定，之后沿用；没有就留空")
    terms: list[str] = Field(default_factory=list, description="这一页新讲的术语（第一次出现的正式名称）")
    notation: list[str] = Field(default_factory=list, description="这一页用的记号约定")
    summary: str = Field(description="这一页讲了什么，一句话")


class FinishArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    self_check: list[SelfCheckArgs] = Field(
        description="写完后按自查清单逐条检查的结果，每条规则一项；发现问题先用工具改好再填")
    series: SeriesArgs | None = Field(
        default=None, description="这一页给整个系列留下的笔记（例子、新术语、记号、一句话概括）")


def finish_schema(rules: Mapping[str, str]) -> dict[str, Any]:
    """`FinishArgs`' JSON Schema with `self_check[].rule` an enum of this run's rule ids
    (`check_rules(teaching)`: the builtin rules plus the spec items in force, so it follows the spec;
    the live run 2026-10-28 mixed up two id series and was refused twice). No rules (the harness's
    empty build): the plain string."""
    out = FinishArgs.model_json_schema()
    if rules:
        rule = out["$defs"]["SelfCheckArgs"]["properties"]["rule"]
        for k in ("minLength", "maxLength"):
            rule.pop(k, None)
        rule["enum"] = list(rules)
    return out


class FinishTool:
    """Freezes the draft. The self-check must name every rule once (a format check, so the
    writer actually goes through the list); what it says is recorded, never a reason to refuse."""

    def __init__(self, ws: Workspace, page_id: str, plan: object, rules: Mapping[str, str]):
        # `plan` is unused (the plan is `ws.plan`, set by plan_page); the four-argument form stays
        # because the frozen harness (harness/improver.deep_tool_texts) builds the tool with it
        del plan
        self._ws, self._page_id, self._rules = ws, page_id, dict(rules)
        base = spec("finish", "草稿写完并自查过了：提交草稿和逐条自查结果。有问题会返回原因，改完再调用。",
                    FinishArgs, "read", "draft")
        self.spec = base.model_copy(update={"parameters": finish_schema(self._rules)})

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(FinishArgs, args, self._run)

    def _run(self, a: FinishArgs) -> ToolResult:
        ws = self._ws
        if ws.plan is None:
            return fail(NO_PLAN)
        if ws.open_conflicts:
            ws.conflict_refusals += 1
            if ws.conflict_refusals <= MAX_CONFLICT_REFUSALS:
                return fail("这些段的正文和图对不上，改正文或用 revise 改图之后再提交："
                            + "；".join(f"{sid}：{c}" for sid, c in ws.open_conflicts.items()))
            for sid in list(ws.open_conflicts):      # never both: the text stays, the picture goes
                log.warning("page %s section %s: picture dropped, it disagrees with the text: %s",
                            self._page_id, sid, ws.open_conflicts[sid])
                ws.drop_drawn(sid)
        try:
            draft = ws.draft(self._page_id)
        except ValueError as e:
            msg = e.errors(include_url=False)[0]["msg"] if isinstance(e, ValidationError) else str(e)
            return fail(f"草稿还不能提交：{msg}")
        if left := ws.missing():
            return fail(f"这些段还没写，写完再提交：{', '.join(left)}")
        if told := draw_waiting(ws):      # the gate: a page is not submitted before its pictures are drawn
            return ToolResult(content=clip(told, 6000))
        try:
            draft = ws.draft(self._page_id)
        except ValueError as e:           # what the pictures changed
            return fail(f"草稿还不能提交：{e}")
        unknown = [x for sec in draft.sections for x in placed_ids(sec.body)
                   if x not in ws.interactives and x not in ws.blocks and x not in ws.images]
        if unknown:
            return fail(f"正文引用了不存在的交互、块或图片：{', '.join(dict.fromkeys(unknown))}")
        named = [c.rule for c in a.self_check]
        if extra := [r for r in dict.fromkeys(named) if r not in self._rules]:
            return fail(f"自查里有清单外的规则：{', '.join(extra)}")
        if missing := [r for r in self._rules if r not in named]:
            return fail(f"自查还缺这些规则（每条一项）：{', '.join(missing)}")
        first = {}
        for c in a.self_check:
            first.setdefault(c.rule, c)
        ws.self_check = tuple(ReviewFinding(rule=r, ok=first[r].ok, section_id=first[r].section_id,
                                            note=first[r].note) for r in self._rules)
        update = None
        if a.series is not None:
            try:
                update = SeriesUpdate(example=a.series.example, terms=tuple(a.series.terms),
                                      notation=tuple(a.series.notation), summary=a.series.summary)
            except ValidationError as e:
                err = e.errors(include_url=False)[0]
                loc = ".".join(str(x) for x in err["loc"])
                return fail(f"series 不合法（{loc}）：{err['msg']}")
        if update is not None:      # a finish without `series` keeps an earlier finish's update in this run
            ws.series_update = update
        ws.finished = draft
        return ToolResult(content="已提交。")


class SubmitArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interactive_id: str = Field(description="要提交的组件 placement id 或块的 block_id")


class SubmitTool:
    """A practice run's `finish` (`deep.agent.practice`): submits ONE placed interactive (a
    component or a block) on a written, non-check section of the page, once its render check has
    passed. Nothing else is in the draft: the text is not touched."""

    def __init__(self, ws: Workspace):
        self._ws = ws
        self.spec = spec("finish", "交互放好、渲染检查通过后调用：提交这一个交互。", SubmitArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(SubmitArgs, args, self._run)

    def _run(self, a: SubmitArgs) -> ToolResult:
        ws = self._ws
        d = ws.interactives.get(a.interactive_id)
        b = ws.blocks.get(a.interactive_id)
        if d is None and b is None:
            return fail(f"没有交互或块 {a.interactive_id}：先用 place_component 或 write_block 放一个")
        sid = d.section_id if d is not None else b.section_id  # type: ignore[union-attr]
        sec = ws.outline_section(sid)
        if sec is None or sec.role is SectionRole.CHECK or ws.page is None or sid not in ws.page.texts:
            return fail(f"段 {sid} 不是这一页写好的正文段：换一段再放")
        ok = ws.valid_check(a.interactive_id) if d is not None else ws.valid_block_check(a.interactive_id)
        if ok is None:
            return fail("它的渲染检查还没通过：按放置时返回的问题改好再提交")
        ws.submitted = a.interactive_id
        return ToolResult(content="已提交。")


def _read_ref(ws: Workspace, guidance: Mapping[str, str] | None) -> Tool:
    """The write-page skill's read_ref (deep@v32); the pack's H8 templates are the workspace's."""
    from evowiki.deep.skill import read_ref_tool
    return read_ref_tool(lambda: ws.evoui_extra, guidance=guidance)


def practice_tools(ws: Workspace, *, user_id: str, page_id: str, title: str, kview: KView,
                   names: Mapping[str, str], teaching: TeachingSpec, checker: Checker,
                   guidance: Mapping[str, str] | None = None) -> list[Tool]:
    """A practice run's tools: the same reads, place_component (with a library), write_block and
    render_check as a write round, and `finish` = `SubmitTool`. No plan_page, no write_section."""
    pages = DraftPageStore(ws, user_id, page_id, title)
    place: list[Tool] = [PlaceComponentTool(ws, checker)] if ws.components else []
    # real pictures reach a practice run through draw_visual: an image alone is not something it can submit
    return [ReadKTool(kview, names), ReadPageTool(pages), ReadSpecTool(teaching), *place,
            _read_ref(ws, guidance), DrawVisualTool(ws), WriteBlockTool(ws, checker), RenderCheckTool(ws, checker),
            SubmitTool(ws)]


def placed_ids(body: str) -> list[str]:
    """Ids placed in a body as `{{interactive:<id>}}`, `{{block:<id>}}` or `{{image:<id>}}`, in order."""
    return PLACED.findall(body)


class DraftPageStore:
    """read_page's pages in the writer: the current page as the draft reads now (every section
    write_section accepted, merged as the commit will merge it, placeholders as written), the
    other pages as stored. Built per call, so a write is readable right after it."""

    def __init__(self, ws: Workspace, user_id: str, page_id: str, title: str,
                 others: Sequence[WikiPage] = ()):
        self._ws, self._user, self._page_id, self._title = ws, user_id, page_id, title
        self._others = InMemoryPageStore({user_id: others})

    def _now(self) -> WikiPage | None:
        """None while the page has no outline yet (plan_page sets it)."""
        page = self._ws.page
        if page is None:
            return None
        if self._ws.sections:
            page, _ = merge_sections(page, self._ws.sections, lambda s: SectionText(
                section_id=s.section_id, cue=s.cue, answer=s.answer, body=s.body,
                cache_key=f"draft:{s.section_id}"))
        return WikiPage(page_id=self._page_id, title=self._title, page=page)

    def get(self, user_id: str, page_id: str) -> WikiPage | None:
        if user_id == self._user and page_id == self._page_id:
            return self._now()
        return self._others.get(user_id, page_id)

    def all(self, user_id: str) -> Sequence[WikiPage]:
        if user_id != self._user:
            return ()
        now = self._now()
        return (*([now] if now is not None else []), *self._others.all(user_id))


def build_tools(ws: Workspace, *, user_id: str, page_id: str, title: str, kview: KView,
                names: Mapping[str, str], teaching: TeachingSpec, checker: Checker,
                known: str, setup: OutlineSetup | None = None,
                on_outline: Outlined | None = None, other_pages: Sequence[WikiPage] = (),
                sources: "SourceReader | None" = None,
                guidance: Mapping[str, str] | None = None) -> list[Tool]:
    """plan_page first (`known`, `setup`, `on_outline`: `PlanPageTool`); place_component only with
    a non-empty library; write_block and render_check always (a placement is render-checked when it
    is made; render_check runs a check again) (the writer composes interactives
    only as EvoUI blocks, never as HTML). `sources`: the user's materials — list_sources /
    read_source / search_source (shared read tools; the loop fences their results as
    material); None = none of them."""
    from evowiki.agent.source_tools import ListSourcesTool, ReadSourceTool, SearchSourceTool
    pages = DraftPageStore(ws, user_id, page_id, title, other_pages)
    place: list[Tool] = [PlaceComponentTool(ws, checker)] if ws.components else []
    material: list[Tool] = ([ListSourcesTool(sources), ReadSourceTool(sources),
                             SearchSourceTool(sources)] if sources is not None else [])
    images: list[Tool] = ([FindImageTool(ws), PlaceImageTool(ws)] if ws.image_search is not None
                          else [])
    return [PlanPageTool(ws, known=known, setup=setup, on_outline=on_outline),
            ReadKTool(kview, names), ReadPageTool(pages), ReadSpecTool(teaching), *material,
            *images, WriteSectionTool(ws), *place, _read_ref(ws, guidance), DrawVisualTool(ws), WriteBlockTool(ws, checker),
            RenderCheckTool(ws, checker),
            FinishTool(ws, page_id, None, check_rules(teaching))]


def without_interactives(draft: RefineDraft, ids: Collection[str]) -> RefineDraft:
    """`draft` without these interactives, blocks or images: their placeholder lines leave the bodies
    (a body that would be left empty keeps one line saying the interactive was left out)."""
    gone = set(ids)
    # the pictures inside a dropped block go with it
    gone |= {im.get("asset") for b in draft.blocks if b.block_id in ids for im in app_images(b.block)}

    def body(text: str) -> str:
        lines = [ln for ln in text.splitlines()
                 if not (ln.strip() and set(placed_ids(ln)) & gone and not PLACED.sub("", ln).strip())]
        out = "\n".join(lines)
        for iid in gone:
            out = (out.replace(placeholder(iid), "").replace(block_placeholder(iid), "")
                   .replace(image_placeholder(iid), ""))
        return out.strip() or "（这里原本有一个交互，没通过检查，已去掉。）"

    return RefineDraft(
        page_id=draft.page_id, plan=draft.plan,
        sections=tuple(s.model_copy(update={"body": body(s.body)}) for s in draft.sections),
        interactives=tuple(i for i in draft.interactives if i.interactive_id not in gone),
        blocks=tuple(b for b in draft.blocks if b.block_id not in gone),
        images=tuple(m for m in draft.images if m.image_id not in gone))
