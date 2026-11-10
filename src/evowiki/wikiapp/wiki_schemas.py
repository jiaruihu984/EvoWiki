"""/wiki/* wire shapes (the E2 web window builds against these). Inputs `*In` forbid extra
fields; outputs `*Out`. SSE payloads are `*Event` models; event names are ORGANIZE_EVENTS.
Validation messages are Chinese.

SSE streams end (connection closed) right after `done` or `error`. Results are structured; the
only free text is LLM-written content (why, profile, cue, body, link text, proposal why, guideline
evidence, ask answer, group name) and organizer-lane conflict messages. CatalogOut carries `names` (id ->
display name; 「未命名概念」/「未命名页面」 when unknown) for every id its structured fields reference."""

# Endpoints (method path [In] → Out):
# (a goal is made by the assistant's goal_new action: /wiki/bot/actions/{id}/approve)
# POST /wiki/goals/{draft_id}/answers AnswersIn → GoalDraftOut (201; the next round, or finished)
# POST /wiki/goals/{draft_id}/stop → GoalDraftOut (201; the user's 「够了，开始学」: finish now)
# GET /wiki/goals/{draft_id} → GoalDraftOut (an open draft's current round, or what it finished as;
#   404 unknown draft, 409 a draft from before S1 / a result that is gone)
# GET /wiki/maps/{map_id} → GoalMapOut
# POST /wiki/maps/{map_id}/edits MapEditIn → MapEditOut (reorder / drop / rename / add / insert,
#   all or none; `undo` = the inverse ops; 404 unknown map, 422 unknown id or an edit that cannot
#   apply, 409 a block whose diagnosis is open / that is being learned, a stale undo)
# POST /wiki/maps/{map_id}/regions/{node_id}/enter → GoalDraftOut (201; any map node: its parts
#   shown → map_id; a point → its round or its path)
# POST /wiki/readings/{reading_id}/units/{unit_id}/open ReadingUnitOpenIn → ReadingUnitOpenOut
#   (「看这一段」 when not analyzed: 0–2 calls; `start` → the reading page; 404 unknown reading /
#   unit, 409 a skipped unit, 502 an analysis unusable twice)
# POST /wiki/readings/{reading_id}/units/{unit_id}/uses ReadingUseIn → ReadingUnitOut (409 not
#   analyzed yet; 422 unknown node / the unit's own point)
# POST /wiki/pages/{id}/organize → SSE ORGANIZE_EVENTS (payloads: ORGANIZE_PAYLOADS)
# GET /wiki/pages/{id} → PageOut
# POST /wiki/pages/{id}/sections/{sid}/fold FoldIn → SectionOut
# POST /wiki/pages/{id}/sections/{sid}/pin PinIn → SectionOut
# POST /wiki/pages/{id}/sections/{sid}/feedback FeedbackIn → FeedbackOut
# POST /wiki/pages/{id}/checks/{sid} CheckAnswerIn → CheckAnswerOut
# POST /wiki/pages/{id}/done → DoneOut
# POST /wiki/pages/{id}/collapse → ShelveOut (收为参考卡; 409 not shelvable)
# POST /wiki/pages/{id}/expand → ShelveOut (恢复成完整页; 409 not shelved)
# GET /wiki → CatalogOut
# GET /wiki/k → KGridOut
# POST /wiki/k/cells/{cell}/declare DeclareIn → DeclareOut
# GET /wiki/settings → SettingsIO
# PUT /wiki/settings SettingsIO → SettingsIO
# GET /wiki/guidelines → GuidelinesOut
# POST /wiki/guidelines GuidelineIn → GuidelineOut (201)
# DELETE /wiki/guidelines/{id} → 204
# GET /wiki/paths/{path_id} → PathOut
# POST /wiki/paths/{path_id}/next → PathNextOut (then POST /wiki/pages/{page_id}/organize)
# POST /wiki/paths/{path_id}/pages/{index}/done → PathPageDoneOut (index is 1-based; 409 not started)
# POST /wiki/paths/{path_id}/edits PathEditIn → PathEditOut (indices 0-based into the shown path)
# POST /wiki/paths/{path_id}/replan → PathEditOut (LLM; the user's edits are kept)
# POST /wiki/nodes/{node_id}/want NodeWantIn → NodeActionOut (我想学这个)
# GET /wiki/graph → GraphOut (0 calls)
# GET /wiki/review/today → ReviewTodayOut
# POST /wiki/review/grade ReviewGradeIn → ReviewGradeOut
# POST /wiki/pages/{id}/refine → RefineJobOut (202; a whole-page rewrite, no UI uses it)
# GET /wiki/pages/{id}/refine → RefineJobOut (latest job; job None = never refined)
# GET /wiki/assets/{asset_id} → the interactive's HTML (CSP: no network; sandbox allow-scripts)
# GET /wiki/assets/{asset_id}/meta → AssetMeta
# POST /wiki/assets/{asset_id}/results BlockResultsIn → BlockResultsOut
# GET /wiki/assets/{asset_id}/progress → BlockProgressOut
# GET /wiki/pages/{id}/exam → PageExamOut (the page's 真题: a quiz asset for the keyed objective items,
#   the rest as open items; 404 unknown page)
# POST /wiki/exam/items/{item_id}/self ExamSelfIn → ExamSelfOut (404 unknown item, 422 right / wrong
#   on an item without a key)
# GET /wiki/teach/spec → TeachSpecOut
# GET /wiki/teach/versions → TeachVersionsOut
# GET /wiki/teach/versions/{v} → TeachSpecOut (that version; 404 unknown)
# POST /wiki/teach/versions/{v}/restore[?base_version=] → TeachSpecOut (a new version)
# POST /wiki/teach/items[?base_version=] SpecItemIn → TeachSpecOut (201; the new item is last)
# PATCH /wiki/teach/items/{id}[?base_version=] SpecItemPatchIn → TeachSpecOut
# DELETE /wiki/teach/items/{id}[?base_version=] → TeachSpecOut
# PUT /wiki/teach/params[?base_version=] RepParams → TeachSpecOut
# POST /wiki/teach/exemplars[?base_version=] ExemplarIn → TeachSpecOut (201; the new one is last)
# DELETE /wiki/teach/exemplars/{id}[?base_version=] → TeachSpecOut
# POST /wiki/pages/{id}/feedback PageFeedbackIn → SignalCreatedOut (201)
# POST /wiki/pages/{id}/hand-edit HandEditIn → HandEditOut
# POST /wiki/pages/{id}/hand-edit/{edit_id}/undo → HandEditOut
# GET /wiki/teach/signals[?after=&limit=&kinds=] → SignalsOut
# GET /wiki/engine → EngineOut (the 参数 tab: prompts in use + θ_infer)
# GET /wiki/prompts → list[PromptStepOut] (Prompt 设置: every model step's guidance)
# PUT /wiki/prompts/{step} PromptIn → PromptStepOut (422 plain reason; 404 unknown step)
# DELETE /wiki/prompts/{step} → PromptStepOut (back to the default wording)

import json
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from evowiki.deep.evoui.contracts import MAX_OPTIONS
from evowiki.goals.contracts import Aspect, Band, ConceptRole, SectionRole
from evowiki.goals.language import Language
from evowiki.goals.regions import MapNode
from evowiki.goals.review import Grade, Period
from evowiki.sources.models import SourceKind
from evowiki.store.artifacts import ARTIFACT_PATTERN
from evowiki.teach.contracts import (
    MAX_ITEM_TEXT,
    SUGGESTED_SPEC_ITEMS,
    CardFeedbackKind,
    Exemplar,
    RepParams,
    Signal,
    SpecItem,
    SpecScope,
)
from evowiki.wikiapp.events import DecisionVia, SpecAction

Changed = Literal["cached", "rewritten", "inserted", "folded", "unfolded"]  # 缓存/改了/新加/已折叠/已展开
IDK = -1                                                          # 「不知道」

ConflictKind = Literal["line_weak", "pinned_line_weak", "assumed_lost",
                       "uncovered_untaught", "uncovered_absent"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- goals ----
class DraftConceptOut(BaseModel):
    id: str
    name: str
    target: bool
    reused: bool
    aspects: list[Aspect]
    prereqs: list[str]


QuestionKind = Literal["self_report", "quiz"]
OptionMeans = Literal["know", "partly", "forgot", "dont", "unsure"]     # 会 / 熟悉 / 学过但忘了 / 不会 / 说不清
# what the KST diagnosis asked (goals/probe): a region's mastery question, a goal-use question on
# a concept / member, or a self-report. None: a round from before probe@v4
QuestionIntent = Literal["mastery", "goal_use", "self_report"]


class QuestionOut(BaseModel):
    """One diagnostic question. The key stays on the server; the client adds 「不知道」 (= -1).
    self_report: 「学过线性代数吗？」, `target` = the node (region / concept) id, `cell` "" and
    `aspect` None, `option_means` aligned with options (weak evidence). quiz: `target` = `cell`
    (a mastery quiz on a region without cells of its own: `target` = the region id, `cell` "").
    `intent`: which kind of item the diagnosis picked (additive; None for older rounds)."""
    index: int
    kind: QuestionKind = "quiz"
    target: str = ""
    cell: str
    concept_id: str                      # the node the question is about
    concept_name: str                    # as the intake LLM named it
    aspect: Aspect | None
    stem: str
    options: list[str]
    option_means: list[OptionMeans] | None = None
    intent: QuestionIntent | None = None


class AnswerResultOut(BaseModel):
    """One question of the round just answered, for the closed round card: the choice and, for a
    quiz, the key (revealed now) and whether the choice was right."""
    index: int
    kind: QuestionKind
    choice: int                          # -1 = 不知道
    answer: int | None = None            # quiz: the key; self_report: None
    correct: bool | None = None          # quiz only
    means: OptionMeans | None = None     # self_report: what the choice meant
    intent: QuestionIntent | None = None
    node_id: str = ""                    # the node the question was about ("" before probe@v4)


class NodeDecisionOut(BaseModel):
    """The diagnosis's verdict on one node (goals/probe): known (skip) or teach, the probability
    that it is known, and the evidence that decided it."""
    node_id: str
    name: str
    decision: Literal["known", "teach"]
    p: float
    # opening: an entered region's member the map's opening diagnosis decided; none: no evidence
    source: Literal["quiz", "self_report", "described", "k", "inferred", "opening", "none"]


# ---- goal map (big goals) ----
MapNodeKind = Literal["region", "concept"]
# collapsed = this goal has not entered the node yet, expanded = entered (its parts shown, or a
# point opened), known = not entered and K puts every cell it covers at likely or above (derived
# from K on every read, regions.block_known; never stored): skipped. An older map's concept node is
# always "expanded".
MapNodeState = Literal["collapsed", "expanded", "known"]
MapNodeEnter = Literal["split", "children", "learn", "page"]


class MapNodeOut(BaseModel):
    """One map node. Colour = a mastery-weighted mix (no numbers on the cloud): `mix` is the
    share of the node's cells per band (sums to 1; {} = no cells), `mastery` their mean m. Cells:
    a collapsed / known region → its own K cells; an expanded region → its descendant concepts'
    cells; a concept → its cells. `counts` stays for the API, not the cloud. `id` is the K concept
    id, the same on every map and in the K grid (one node for one thing: merge by id); `reused`:
    the wiki already had it before this goal."""
    id: str
    kind: MapNodeKind
    name: str
    parent: str | None
    scope: str                           # region: 「矩阵、矩阵乘法、向量和点积」; concept: ""
    prereqs: list[str]                   # same-level edges, acyclic
    target: bool
    state: MapNodeState
    path_id: str | None                  # the region's learning path once entered
    page_id: str | None
    reused: bool
    members: list[str]                   # region: its knowledge points (concept ids); concept: []
    level: str = ""                      # a chat-first block's level word (基础 / 核心 / 进阶 / 拓展); else ""
    # spec 2026-10-28 §2.2, computed when read (never stored on the map): the knowledge node's
    # `atomic` (None: not judged) and what entering it does — split: one level is split (a model
    # call); children: its parts are shown (0 calls); learn: it is a point, opening it learns it
    # (it can still be split further: POST /wiki/nodes/{id}/split with this map_id)
    atomic: bool | None = None
    enter: MapNodeEnter = "learn"
    mix: dict[Band, float]
    mastery: float | None
    counts: dict[Band, int]


class GoalMapOut(BaseModel):
    map_id: str
    goal: str
    title: str                         # the existing intake's summarized topic name
    nodes: list[MapNodeOut]              # regions first-level in prerequisite order, children after
    next_region: str | None              # the region to enter next; None: nothing left
    reading: "ReadingOut | None" = None   # a 读懂 goal's units (its nodes are the prerequisites)


def _name_text(v: str) -> str:
    if not v.strip():
        raise ValueError("名字不能为空")
    return v.strip()


class MapLinksIn(_In):
    """Relations an edit took off block `id` (its prerequisites / members that pointed at a dropped
    block or backwards after a reorder): its inverse (撤销) puts them back where they were."""

    id: str = Field(min_length=1)
    prereqs: list[str] = Field(default_factory=list, max_length=500)
    members: list[str] = Field(default_factory=list, max_length=500)
    before_prereqs: list[str] = Field(default_factory=list, max_length=500)
    before_members: list[str] = Field(default_factory=list, max_length=500)


class MapReorderIn(_In):
    """The top-level blocks in their new order (every one of them, once). `links` (an inverse op
    only): the prerequisites the reordered-away edit dropped, put back."""

    op: Literal["reorder"]
    ids: list[str] = Field(min_length=1, max_length=200)
    links: list[MapLinksIn] = Field(default_factory=list, max_length=500)


class MapDropIn(_In):
    """Remove a block from the map (its graph node stays)."""

    op: Literal["drop"]
    id: str = Field(min_length=1)


class MapRenameIn(_In):
    op: Literal["rename"]
    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)

    _name = field_validator("name")(_name_text)


class MapAddIn(_In):
    """A new block at the end (collapsed, no members: it is split when entered)."""

    op: Literal["add"]
    name: str = Field(min_length=1, max_length=120)
    covers: list[str] = Field(default_factory=list, max_length=50)

    _name = field_validator("name")(_name_text)


class MapInsertIn(_In):
    """撤销 of a drop (its inverse op, `MapEditOut.undo`): the dropped block `node` back among the
    top-level blocks at `index` (0-based; past the end = last), with the nodes that hung under it
    (`under`). Refused (409) when the map changed so it cannot go back: its id or its name is
    on the map again."""

    op: Literal["insert"]
    node: MapNode
    under: list[MapNode] = Field(default_factory=list, max_length=500)
    index: int = Field(ge=0)
    # the relations of other blocks that pointed at the dropped ones (D35: 撤销 brings them back too)
    links: list[MapLinksIn] = Field(default_factory=list, max_length=500)


MapEditOpIn = Annotated[MapReorderIn | MapDropIn | MapRenameIn | MapAddIn | MapInsertIn,
                        Field(discriminator="op")]


class MapEditIn(_In):
    """Edits of the 学习路径 card, applied in order, all or none."""

    ops: list[MapEditOpIn] = Field(min_length=1, max_length=50)


class ReadingPointOut(BaseModel):
    """A point a reading unit teaches or uses (a shared-graph node; reading-mode spec §2.3). K
    colour as a map node's (`mix` per band of its cells, `mastery` their mean m); `known`: every
    cell likely or better (no cells: False); `page_id`: a page that teaches it (None: none yet),
    the 先补 chip's target; `on_map`: a top-level node of this goal's map (a prerequisite to 补)."""
    id: str
    name: str
    atomic: bool | None = None
    known: bool
    mix: dict[Band, float]
    mastery: float | None
    page_id: str | None = None
    on_map: bool = False


# 未开始 / 在读 / 读完 / 不读, from the unit's page only (wikiapp.reading_view.unit_status; never K)
ReadingUnitStatus = Literal["not_started", "reading", "done", "skipped"]


class ReadingUnitOut(BaseModel):
    """One unit of a 读懂 goal's reading (reading-mode spec §2.1, §5). `teaches` / `uses` / `focus`
    are empty until `analyzed`; `page_id`: its reading page once started; `location`: 「§3.2
    p.4–5」 ("" where the material's sections were not read)."""
    unit_id: str
    order: int
    title: str
    about: str
    analyzed: bool
    skipped: bool
    page_id: str | None
    status: ReadingUnitStatus = "not_started"
    location: str = ""
    teaches: list[ReadingPointOut]
    uses: list[ReadingPointOut]
    focus: list[str]
    uncertain: bool = False              # a span re-anchoring could not find (「原文位置可能不准」)


class ReadingProgressOut(BaseModel):
    """Units read / units to read (a skipped unit is in neither; read = its page 学完, never K)."""
    done: int
    total: int


class ReadingOut(BaseModel):
    """A 读懂 goal's reading as the learn view shows it (reading-mode spec §5), computed at read
    time: the units in the material's order and the progress. `path_id`: the reading's path once
    a unit's page is started."""
    reading_id: str
    source_id: str
    title: str
    path_id: str | None
    units: list[ReadingUnitOut]
    progress: ReadingProgressOut


GoalMapOut.model_rebuild()       # GoalMapOut.reading refers to ReadingOut, defined after it


class ReadingUnitOpenIn(_In):
    """POST /wiki/readings/{reading_id}/units/{unit_id}/open: `start` also starts its page."""
    start: bool = False


class ReadingUnitOpenOut(BaseModel):
    """The unit after 「看这一段」 (`llm_calls`: 1, 2 when its first answer was asked again, 0 when
    it was analyzed already); `page_id` / `path_id`: its reading page and the reading's path
    (GET /wiki/paths/{path_id}) once started."""
    reading_id: str
    map_id: str
    unit: ReadingUnitOut
    page_id: str | None = None
    path_id: str | None = None
    llm_calls: int = 0


class ReadingUseIn(_In):
    """A prerequisite found while reading: an existing node (`existing_id`) or a new one
    (`name`, `summary`, `atomic`)."""
    existing_id: str = ""
    name: str = ""
    summary: str = ""
    atomic: bool = False

    @model_validator(mode="after")
    def _one(self) -> "ReadingUseIn":
        if not self.existing_id.strip() and not self.name.strip():
            raise ValueError("要写已有节点的 id 或新知识点的名字")
        return self


class MapEditOut(BaseModel):
    """The map after the edits, and `undo`: the inverse ops of the edits (撤销 sends them back as
    `{"ops": undo}`): a drop → insert, a rename → the old name, a reorder → the old order, an add
    → drop. An inverse op undoes only its own edit, never a later one (a stale one is refused)."""

    map: GoalMapOut
    undo: list[MapEditOpIn]


class MapCardOut(BaseModel):             # catalog
    map_id: str
    goal: str
    regions: int
    expanded: int
    known: int
    next_region: str | None


class DraftNotesOut(BaseModel):
    from_background: int      # 从描述读出几条
    reused_concepts: int      # 复用了几个概念
    dropped_questions: int    # 丢了几道题（没通过独立核题）


class GoalDraftOut(BaseModel):
    """Exactly one of: `draft_id` (a diagnosis round is waiting: `questions`, `round` 1-based),
    `page_id` (the page was created), `path_id` (a learning path: GET /wiki/paths/{path_id}),
    `map_id` (a big goal's map is ready to enter). `map` is attached whenever
    the draft belongs to a goal map (also while its rounds run). `results`: the round just
    answered (POST …/answers), shown on the closed round card."""
    draft_id: str | None
    page_id: str | None
    path_id: str | None = None
    map_id: str | None = None
    title: str
    concepts: list[DraftConceptOut]
    questions: list[QuestionOut]
    notes: DraftNotesOut
    round: int = 0                       # 第 round 轮 (0 when no round is waiting)
    round_title: str = ""                # the model's short heading, e.g. 「了解你的基础」
    round_why: str = ""                  # one sentence: why these questions, or why it ended
    results: list[AnswerResultOut] = []
    map: GoalMapOut | None = None
    # the per-node verdicts of the last round (the final ones once the diagnosis ended)
    decisions: list[NodeDecisionOut] = []

    @model_validator(mode="after")
    def _one(self) -> "GoalDraftOut":
        if sum(x is not None for x in (self.draft_id, self.page_id, self.path_id,
                                       self.map_id)) != 1:
            raise ValueError("只能四选一：draft_id、page_id、path_id 或 map_id")
        if self.draft_id is None and self.questions:
            raise ValueError("只有等待作答的草稿才带题目")
        return self


class AnswersIn(_In):
    answers: list[int] | None = Field(default=None, min_length=1)   # aligned with questions; -1 = 不知道
    skip: bool = False

    @model_validator(mode="after")
    def _one(self) -> "AnswersIn":
        if (self.answers is not None) == self.skip:
            raise ValueError("请给出 answers，或 skip=true")
        if self.answers is not None and any(a < IDK for a in self.answers):
            raise ValueError("答案必须是选项序号或 -1")
        return self


# ---- shared pieces ----
class CellOut(BaseModel):
    cell: str
    concept_id: str
    concept_name: str     # as the intake LLM named it
    aspect: Aspect
    band: Band
    declared: Literal["know", "dont"] | None = None


class ConflictOut(BaseModel):
    """One unresolved conflict, structured (one per cell)."""
    kind: ConflictKind
    section_id: str | None = None        # the related section (a pinned one-line section too); None if none
    cell: str | None = None


class ChangeOut(BaseModel):
    """What one action changed (section ids). `llm_calls` = LLM calls this request made."""
    rewritten: list[str] = []
    inserted: list[str] = []
    moved: list[str] = []
    folded: list[str] = []
    unfolded: list[str] = []
    llm_calls: int = 0
    other_pages_stale: list[str] = []          # page ids now marked 待更新
    # conflicts: structured twin of unresolved_conflicts (K2)
    unresolved_conflicts: list[str] = []       # organizer lane messages, as-is
    conflicts: list[ConflictOut] = []          # same source as unresolved_conflicts, structured
    budget_exceeded: bool = False              # the piggybacked page update only folded (over budget, no LLM)


class CellChangeOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    cell: str
    from_: Band = Field(alias="from")
    to: Band


class DiffNoteOut(BaseModel):                  # 「自你上次来」
    cell_changes: list[CellChangeOut] = []
    folded: list[str] = []
    unfolded: list[str] = []
    patched: list[str] = []                    # sections the open-time patch rewrote / inserted


# ---- page ----
class CheckOut(BaseModel):
    stem: str
    options: list[str]
    answered: int | None = None          # the user's choice, -1 = 不知道
    correct_index: int | None = None     # only after answering
    correct: bool | None = None          # None before answering or for 不知道
    artifact_id: str | None = None       # this question's version (0091): sent back with the answer


class SectionOut(BaseModel):
    id: str
    heading: str
    role: SectionRole
    cue: str
    body: str
    note_body: str | None = None         # a hand edit, as the note shows it (SectionText.note_body)
    folded: bool
    fold_locked: bool
    foldable: bool
    pinned: bool
    changed: Changed | None = None
    why: str
    cells: list[CellOut]
    lookups: list[str] = []              # ⟦待查证：…⟧ items in body
    check: CheckOut | None = None
    # the version of this section's text shown (0091; None while unwritten): the client sends it
    # back with know / deeper / other, page feedback and a selection for the assistant
    artifact_id: str | None = None
    # heading + body as stored now (`page_version.text_fingerprint`): a hand edit sends back the one
    # its note was written from, and is refused when the section has changed since
    fingerprint: str = ""


class FeedbackMarkOut(BaseModel):
    """The page-feedback patch on the page now; undo: POST /wiki/pages/{id}/feedback/{feedback_id}/undo
    (only while the page still holds what it left)."""

    feedback_id: str
    at: AwareDatetime
    summary: str
    reading: str                         # 「系统的理解」
    sections: list[str]                  # added to / inserted, page order
    assets: list[str] = []               # library components placed (GET /wiki/assets/{id})
    can_undo: bool


class HandEditMarkOut(BaseModel):
    """The user's last hand edit still on the page (`wikiapp.hand_edit`); undo: POST
    /wiki/pages/{id}/hand-edit/{edit_id}/undo (while its sections still hold what it left)."""

    edit_id: str
    at: AwareDatetime
    summary: str
    sections: list[str]                  # the sections it wrote that still hold it, page order
    can_undo: bool


class WriterOut(BaseModel):
    """Who wrote the page's current text (single writer): the model that finished, when, what it
    cost (USD, this write only), and why the fallback wrote it (None = the primary writer did)."""

    model: str
    at: AwareDatetime
    cost_usd: float
    fallback_reason: str | None = None


class PageSourceOut(BaseModel):
    """A material the page was written from (materials task 7). `title`: the name when the page
    was written (what its 〔…〕 marks say); `origin`: where the original lies in the vault / the url."""

    id: str
    title: str
    current_title: str
    kind: SourceKind
    origin: str | None
    deleted: bool


class GoalSourceOut(BaseModel):
    """A material a goal came from; `uncovered`: titles of its topmost sections no node of the goal
    comes from (the client words the line; [] once the material is deleted)."""

    id: str
    title: str
    deleted: bool
    uncovered: list[str]


class GoalNoteOut(BaseModel):
    """A goal that reuses this page: what it adds (goals/page_fit.py; "" = it fits as it is)."""

    map_id: str
    goal: str
    note: str = ""


class PageOut(BaseModel):
    page_id: str
    # the goal the page was written for, when another goal reuses it ("" = none does)
    written_for: str = ""
    goal_notes: list[GoalNoteOut] = []
    title: str                           # the page's name, with no path position (the file name)
    # EvoWiki/Wiki/<folder>/<title>.md: chosen when the page was created, it never moves
    # (`wikiapp.page_folder`; "" = the Wiki root)
    folder: str = ""
    # the name of the part_of parent of the page's main node ("" = none): added to the file name when
    # the title is taken in the folder (「定义（食品添加剂）」)
    parent_name: str = ""
    # in the trash (a merge's original, a taken-back bot page): the record is all there is -
    # not organized, no sections, nothing is updated or written
    trashed: bool = False
    goal: str
    organized: bool                      # False: call POST …/organize (SSE) first
    # the step once this page is read (wikiapp.progress, PageProgressOut.after); None: on no goal
    next_step: "StepOut | None" = None
    source: Literal["organizer", "fallback"] | None = None
    profile: str = ""
    assumed: list[CellOut] = []
    sections: list[SectionOut] = []      # the outline, in order
    diff: DiffNoteOut | None = None      # 「自你上次来」; None when nothing changed since last visit
    # conflicts: structured twin of unresolved_conflicts (K2)
    unresolved_conflicts: list[str] = []  # organizer lane messages, as-is
    conflicts: list[ConflictOut] = []     # same source as unresolved_conflicts, structured
    budget_exceeded: bool = False         # this open's page update only folded (over budget, no LLM)
    collapsed: bool = False              # 已收成参考卡 (the learner shelved it)
    can_shelve: bool = False             # 收为参考卡 is offered: organized, not shelved, every target 确定会
    shelf_line: str = ""                 # the reference card's line: the page's first recall cue
    done: bool = False
    llm_calls: int = 0                      # LLM calls spent by this open (lazy patch)
    writer: WriterOut | None = None         # the writer run behind the text (None: written before it)
    feedback: "FeedbackMarkOut | None" = None   # 「这一页按你的反馈改了：…· 撤回」 (B1)
    hand_edit: "HandEditMarkOut | None" = None  # 「已写入你的改动：…· 撤销」
    # True only on the read of a page-view open (?rewrite=1) that started the background rewrite of a page an
    # RSI change left out of date: `writer.at` changes when it lands (the client re-reads, then).
    rewriting: bool = False
    sources: list[PageSourceOut] = []       # materials it was written from (GET /wiki/pages/{id})


# ---- organize SSE: outline → writer → (section_added? section_delta section_done)* → done | error ----
# One writer writes the page (single writer, 2026-10-10); a section's events may repeat (the last one
# wins: the writer fixed it, an interactive landed on it, or its committed body differs from what was
# streamed). `writer` comes before the first section and again when the fallback takes over.
ORGANIZE_EVENTS = ("planning", "outline", "writer", "section_added", "section_delta", "section_done", "done",
                   "error")


class PlanningEvent(BaseModel):
    """The page is being planned: one event (`pass` 1) before the writer's loop, which sends
    `outline` once its plan_page is accepted; the plugin counts the seconds itself."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    pass_: int = Field(alias="pass", ge=1)


class DroppedCheckOut(BaseModel):
    section_id: str
    reason: str                          # no_answer | multiple_correct | verifier_error | …


class OutlineEvent(BaseModel):
    page_id: str
    source: Literal["organizer", "fallback"]
    profile: str
    assumed: list[CellOut]
    sections: list[SectionOut]           # body empty until its section_delta
    dropped_checks: list[DroppedCheckOut] = []   # always [] now: no verification call drops checks


class SectionDeltaEvent(BaseModel):
    section_id: str
    cue: str
    body: str                            # whole text (D3: not token-streamed yet)
    heading: str | None = None           # the section's heading as written (None: unchanged)


class WriterEvent(BaseModel):
    """The model writing this page / section: at the start, and again (fallback=True, reason) when
    the primary writer was unavailable and the fallback took over (fail closed: never silent)."""

    model: str                           # "provider:model", e.g. anthropic:claude-sonnet-5-5
    fallback: bool = False
    reason: str = ""


class SectionAddedEvent(BaseModel):
    """A section the writer inserted (not in `outline`): insert it after `after` (None = page top);
    its text follows in `section_delta`."""

    section: SectionOut
    after: str | None


class SectionDoneEvent(BaseModel):
    section_id: str
    cached: bool


class OrganizeDoneEvent(BaseModel):
    page_id: str
    llm_calls: int
    cost_usd: float = 0.0         # the writer's spend on this page (USD; the outline call is metered apart)
    model: str = ""               # the writer that finished the page
    fallback: bool = False        # True: the fallback writer finished it (see the  event)
    prefetched: bool = False      # True: written ahead (path prefetch); the stream was a replay / attach


class ErrorEvent(BaseModel):
    code: Literal["llm_unavailable", "llm_busy", "llm_timeout", "llm_failed", "conflict",
                  "not_found", "invalid", "budget_exceeded", "internal"]
    retryable: bool
    # technical detail for logs, not for display; except llm_busy (a provider overloaded) and
    # llm_timeout (a request timed out): the text to show, 「请切换模型重试」 /
    # 「请切换模型重试」
    detail: str = ""


ORGANIZE_PAYLOADS: dict[str, type[BaseModel]] = {
    "planning": PlanningEvent,
    "outline": OutlineEvent,
    "writer": WriterEvent,
    "section_added": SectionAddedEvent,
    "section_delta": SectionDeltaEvent,
    "section_done": SectionDoneEvent,
    "done": OrganizeDoneEvent,
    "error": ErrorEvent,
}


# ---- section actions ----
class FeedbackIn(_In):
    kind: Literal["know"]
    # the artifact version the user was shown (the `artifact_id` this view carried; 0091)
    artifact_id: str | None = Field(default=None, pattern=ARTIFACT_PATTERN)


class FeedbackOut(BaseModel):            # 0 LLM calls for the fold
    section: SectionOut
    change: ChangeOut


class RewriteDoneEvent(BaseModel):       # the end of a section rewrite (wikiapp.kchange)
    page_id: str
    llm_calls: int
    cost_usd: float = 0.0                # the writer's spend on this rewrite (USD)
    model: str = ""                      # the writer that finished it
    fallback: bool = False               # True: the fallback writer finished it


REWRITE_PAYLOADS: dict[str, type[BaseModel]] = {
    "writer": WriterEvent,
    "section_added": SectionAddedEvent,
    "section_delta": SectionDeltaEvent,
    "section_done": SectionDoneEvent,
    "change": ChangeOut,
    "done": RewriteDoneEvent,
    "error": ErrorEvent,
}


class FoldIn(_In):
    folded: bool


class PinIn(_In):
    pinned: bool


class CheckAnswerIn(_In):
    choice: int = Field(ge=IDK)
    # the artifact version the user was shown (the `artifact_id` this view carried; 0091)
    artifact_id: str | None = Field(default=None, pattern=ARTIFACT_PATTERN)


class CheckAnswerOut(BaseModel):
    correct: bool | None                 # None = 不知道
    correct_index: int
    change: ChangeOut
    answer_ref: str = ""                 # this answer's evidence ref (for POST …/followup)


# ---- EvoUI block results ----
# POST /wiki/assets/{asset_id}/results  BlockResultsIn → BlockResultsOut
#   what the reader did in an evoui block: quiz answers (graded server-side from the stored block;
#   a question with a `cell` becomes quiz evidence in K), result events (evoui.js `onEvent` /
#   `events()`, EvoUI v2 D3; graded the same way), the final state (kept in the signal) and the
#   progress object (`progress()`; stored when its revision is newer)
# GET /wiki/assets/{asset_id}/progress → BlockProgressOut ({revision: 0, data: null} when none)

MAX_BLOCK_EVENTS = 200
MAX_EVENT_RESPONSE_CHARS = 2000
MAX_PROGRESS_CHARS = 20000
MAX_REVISION = 2_147_483_647          # block_progress.revision is a Postgres integer
BlockEventType = Literal["item", "summary", "route", "request"]
BlockEventStatus = Literal["k", "score", "signal", "duplicate"]


class BlockAnswerIn(_In):
    quiz: int = Field(ge=0, le=50)        # the block's quiz nodes in document order (onQuizResult)
    question: int = Field(ge=0)
    chosen: int = Field(ge=0, le=MAX_OPTIONS - 1)


class BlockEventIn(_In):
    """One evoui.js result event, exactly as `onEvent` sends it (EvoUI v2 D3). The client's
    `correct` is ignored except for `app` items (graded "client")."""

    type: BlockEventType
    block: str | None = Field(default=None, max_length=200)
    node: str = Field(min_length=1, max_length=64)     # node key (`deep.evoui.node_keys`)
    kind: str | None = Field(default=None, min_length=1, max_length=32)
    revision: int = Field(ge=0, le=MAX_REVISION)
    # the render that sent it (evoui.js makeCtx: random hex per render); part of the dedup key, so a
    # fresh render whose attempt / revision restarted (no progress restored) is not taken for a resend.
    # Absent (older clients): the key is node / item / attempt / revision only.
    session: str | None = Field(default=None, pattern=r"^[0-9a-f]{8,32}$")
    attempt: int | None = Field(default=None, ge=1, le=100000)
    # item
    item: int | str | None = None          # int index; app: the item id (1–64 characters)
    response: Any = None                   # ≤ 2000 characters as JSON
    correct: bool | None = None
    hint_used: bool = Field(default=False, alias="hintUsed")
    latency_ms: int | None = Field(default=None, ge=0, le=3_600_000, alias="latencyMs")   # ≤ 1 h (evoui APP.latency)
    # summary
    total: int | None = Field(default=None, ge=0, le=10000)
    right: int | None = Field(default=None, ge=0, le=10000)
    missed: list[int | str] | None = Field(default=None, max_length=200)
    time_ms: int | None = Field(default=None, ge=0, alias="timeMs")
    timer_ms: int | None = Field(default=None, ge=0, alias="timerMs")
    hints_used: int | None = Field(default=None, ge=0, alias="hintsUsed")
    # route
    option: Any = None
    next: str | None = Field(default=None, max_length=600)
    preference: str | None = Field(default=None, min_length=1, max_length=64)
    # request
    what: Literal["more"] | None = None

    @model_validator(mode="after")
    def _shape(self) -> "BlockEventIn":
        if self.type == "item":
            if self.kind is None or self.item is None or self.attempt is None:
                raise ValueError("an item event needs kind, item and attempt")
            if isinstance(self.item, int) and not 0 <= self.item <= 10000:
                raise ValueError("item index out of range")
            if isinstance(self.item, str) and not 1 <= len(self.item) <= 64:
                raise ValueError("item id: 1–64 characters")
            if len(json.dumps(self.response, ensure_ascii=False)) > MAX_EVENT_RESPONSE_CHARS:
                raise ValueError("response too large")
        elif self.type == "summary":
            if self.attempt is None:
                raise ValueError("a summary event needs attempt")
        elif self.type == "route":
            if self.option is None:
                raise ValueError("a route event needs option")
            if len(json.dumps(self.option, ensure_ascii=False)) > 600:
                raise ValueError("option too large")
        elif self.what is None:
            raise ValueError("a request event needs what")
        return self


class AssetGeneratedIn(_In):
    text: str = Field(max_length=2_000_000)   # a fence's JSON or an asset file, as the plugin read it
    kind: Literal["evoui", "html"] = "evoui"


class AssetGeneratedOut(BaseModel):
    generated: bool


class BlockProgressIn(_In):
    revision: int = Field(ge=0, le=MAX_REVISION)
    data: dict[str, Any]                  # evoui.js progress() (≤ 20000 characters as JSON)

    @model_validator(mode="after")
    def _size(self) -> "BlockProgressIn":
        if len(json.dumps(self.data, ensure_ascii=False)) > MAX_PROGRESS_CHARS:
            raise ValueError("progress too large")
        return self


class BlockResultsIn(_In):
    answers: list[BlockAnswerIn] = Field(default=[], max_length=64)
    state: dict[str, Any] | None = None   # the block's final state (≤ 4000 characters as JSON)
    events: list[BlockEventIn] = Field(default=[], max_length=MAX_BLOCK_EVENTS)
    progress: BlockProgressIn | None = None

    @model_validator(mode="after")
    def _something(self) -> "BlockResultsIn":
        if not self.answers and self.state is None and not self.events and self.progress is None:
            raise ValueError("send answers, state, events or progress")
        if self.state is not None and len(json.dumps(self.state, ensure_ascii=False)) > 4000:
            raise ValueError("state too large")
        return self


class BlockEventOut(BaseModel):
    """`k`: graded and went into K (answer_ref set); `score`: graded, counted in right/total only;
    `signal`: recorded in the block_result signal text (summary / route / request, or an app item
    the app did not judge); `duplicate`: already recorded by an earlier post, skipped."""

    index: int                            # position in BlockResultsIn.events
    status: BlockEventStatus
    correct: bool | None = None           # item events
    graded: Literal["server", "client"] | None = None
    answer_ref: str | None = None         # status k (POST …/checks/{sid}/followup)


class BlockProgressOut(BaseModel):
    revision: int
    data: dict[str, Any] | None = None


class BlockAnswerOut(BaseModel):
    quiz: int
    question: int
    correct: bool
    case: str | None = None
    cell: str | None = None
    answer_ref: str | None = None         # set when it went into K (POST …/checks/{sid}/followup)


class BlockCaseOut(BaseModel):
    case: str
    label: str
    right: int
    total: int
    advice: str = ""


class BlockResultsOut(BaseModel):
    asset_id: str
    page_id: str
    section_id: str
    answers: list[BlockAnswerOut]
    right: int
    total: int
    cases: list[BlockCaseOut] = []        # diagnostic quizzes: per case
    signal_id: str | None = None          # None: nothing new to record (progress only / duplicates)
    change: ChangeOut | None = None       # K evidence → the K-change pass on the block's page
    events: list[BlockEventOut] = []      # one per BlockResultsIn.events, same order
    progress_revision: int | None = None  # the stored progress revision after this post (progress sent)
    progress_stale: bool = False          # the sent progress was not newer than the stored one


# ---- wrong-answer follow-up ----
# POST /wiki/pages/{id}/checks/{sid}/followup  CheckFollowupIn → CheckFollowupOut
#   after a wrong answer (page check, review card in check or cue mode): 粗心选错了 / 我学会了 / 看不懂
# POST /wiki/review/confirms/{followup_id}     ConfirmAnswerIn → ConfirmAnswerOut
#   the confirmation question of a slip / learned follow-up (listed in GET /wiki/review/today `confirms`)

FollowupChoice = Literal["slip", "learned", "unclear"]


class CheckFollowupIn(_In):
    choice: FollowupChoice
    # the wrong answer it is about (CheckAnswerOut / ReviewGradeOut `answer_ref`); None = the latest
    # wrong answer on this section (page check or review card)
    answer_ref: str | None = Field(default=None, min_length=1, max_length=200)
    # the artifact version the user was shown (the `artifact_id` this view carried; 0091)
    artifact_id: str | None = Field(default=None, pattern=ARTIFACT_PATTERN)


class ReviewTargetOut(BaseModel):
    """Where to (re)read a cell: a page section, or (section_id None) the page itself."""

    page_id: str
    page_title: str
    section_id: str | None = None
    heading: str = ""


class SuggestedPageOut(BaseModel):
    """No page teaches the cell yet: what a new page would cover (`goal`: the text for a new goal)."""

    concept_id: str
    concept_name: str
    goal: str


class AssistantQuoteOut(BaseModel):
    kind: Literal["text", "wrong_answer"] = "text"
    """Open the assistant with this as the quoted selection (assistant `Selection`)."""

    text: str = Field(max_length=4000)   # 题目 + 选项 + 你选的 + 正确答案 (+ 解释)
    heading: str | None = Field(default=None, max_length=300)


class CheckFollowupOut(BaseModel):
    followup_id: str
    choice: FollowupChoice
    cell: str
    concept_name: str
    status: Literal["pending", "done"]   # pending: a confirmation question is scheduled
    confirm_due: AwareDatetime | None = None
    band: CellChangeOut                  # the cell, before → after this follow-up
    review: ReviewTargetOut | None = None          # unclear: where to go back to
    suggest: SuggestedPageOut | None = None        # unclear and no page teaches it
    quote: AssistantQuoteOut | None = None         # unclear: for the assistant
    signal_id: str | None = None                   # unclear: the check_unclear signal
    llm_calls: int = 0


class ConfirmCardOut(BaseModel):
    """A pending confirmation question (a new question on the same cell, never the original)."""

    followup_id: str
    kind: Literal["slip", "learned"]
    cell: str
    concept_id: str
    concept_name: str
    aspect: Aspect
    page_id: str
    page_title: str
    check: "ReviewCheckOut"
    due: AwareDatetime
    artifact_id: str | None = None       # this question's version (0091): sent back with the answer


class ConfirmAnswerIn(_In):
    choice: int = Field(ge=IDK)
    # the artifact version the user was shown (the `artifact_id` this view carried; 0091)
    artifact_id: str | None = Field(default=None, pattern=ARTIFACT_PATTERN)


class ConfirmAnswerOut(BaseModel):
    followup_id: str
    correct: bool | None                 # None = 不知道 (counts as not confirmed)
    correct_index: int
    outcome: Literal["confirmed", "refuted"]
    band: CellChangeOut
    reteach: ReviewTargetOut | None = None     # learned, refuted: the section to re-teach
    signal_id: str | None = None               # learned, refuted: the check_unclear signal


class DoneOut(BaseModel):                      # POST /wiki/pages/{id}/done (nothing else runs)
    pass


class ShelveOut(BaseModel):                    # POST /wiki/pages/{id}/collapse | expand
    change: ChangeOut                          # the sections folded / unfolded


# ---- learning path ----
PathPageStatus = Literal["not_started", "learning", "done"]    # 还没开始 / 在学 / 学完
# the one page state (wikiapp.progress): planned → writing → written → read → mastered, plus
# failed (no body, its write failed) and trashed. read = the learner said 学完 (opening never);
# mastered = K puts every target at likely or above on a written page.
PageState = Literal["planned", "writing", "written", "read", "mastered", "failed", "trashed"]


class WriteProgressOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    section: int                              # sections written so far
    of: int | None = None                     # sections planned (None: not known yet)
    pass_: int | None = Field(default=None, alias="pass")   # the writer's pass, when it has them


class ReviewFirstOut(BaseModel):
    """「建议先复习 `name`」 on a step (wikiapp.review_first). `page_id`: the page that teaches it, the
    chat turn's page (None: no page yet)."""

    node_id: str
    name: str
    page_id: str | None = None
    page_title: str = ""


class ReviewFirstIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=200)
    choice: Literal["review", "skip"]


class StepOut(BaseModel):
    """The learner's one next step (wikiapp.progress). `kind`:
    diagnosis - a diagnosis round waits (`draft_id`; GET /wiki/goals/{draft_id});
    read - a written page not finished yet: open `page_id`;
    writing - the current page is being written (`progress`; attach to its run stream);
    failed - the current page's write failed (`failure`): 重试 writes it like opening does;
    start_page - the next row has no page / no body yet: POST /paths/{path_id}/next (page_id None)
      or open `page_id`;
    enter_block - the current block is done, or an entered block waits for the learner to pick a
      part: POST /maps/{map_id}/regions/{region_id}/enter (any map node; spec 2026-10-28);
    goal_done - every block / page of the goal is done; none - nothing to learn."""

    kind: Literal["diagnosis", "read", "writing", "failed", "start_page", "enter_block",
                  "goal_done", "none"]
    goal_id: str | None = None                # map id of a big goal, path id of a lone path
    goal: str = ""
    map_id: str | None = None
    region_id: str | None = None
    region_name: str = ""
    path_id: str | None = None
    index: int | None = None                  # the row's stored 1-based index (PathPageOut.index)
    page_id: str | None = None
    title: str = ""
    draft_id: str | None = None
    progress: WriteProgressOut | None = None
    failure: str | None = None
    review_first: ReviewFirstOut | None = None


PageOut.model_rebuild()          # PageOut.next_step refers to StepOut, defined after it


class BlockOut(BaseModel):
    region_id: str
    name: str
    level: str = ""
    # choosing: entered, its parts are shown and none is begun — the learner picks one
    state: Literal["not_started", "diagnosing", "learning", "choosing", "done", "known"]
    path_id: str | None = None
    draft_id: str | None = None
    pages_done: int = 0
    pages_total: int = 0


class GoalProgressOut(BaseModel):
    """A goal's progress: done only when every block is done (known, or all its pages read /
    mastered / skipped). A lone path (no goal map) has no blocks."""

    goal_id: str
    kind: Literal["map", "path"]
    goal: str
    state: Literal["not_started", "learning", "done"]
    blocks: list[BlockOut] = []
    path_ids: list[str] = []
    pages_done: int = 0
    pages_total: int = 0
    step: StepOut


class ProgressOut(BaseModel):
    """GET /wiki/progress: `today` is the step of the goal the learner was last on (else the first
    goal not done; `none` when every goal is done or there is none)."""

    today: StepOut
    goals: list[GoalProgressOut]


class PageProgressOut(BaseModel):
    """The page's state and the step that follows once it is read (`after`, `PageOut.next_step`).
    `goal_id` None: the page is on no goal."""

    page_id: str
    state: PageState
    goal_id: str | None = None
    after: StepOut


class PathConceptOut(BaseModel):
    concept_id: str
    name: str
    target: bool            # False: a prerequisite this page uses from an earlier page


class PathPageOut(BaseModel):
    index: int = Field(ge=1)                 # 第 index 页
    aim: str                                 # 「学完这页你能……」 (LLM-written or the rule version)
    concepts: list[PathConceptOut]           # prerequisites first, then the page's targets
    status: PathPageStatus
    state: PageState = "planned"             # wikiapp.progress (status is derived from it)
    progress: WriteProgressOut | None = None  # state writing: how far the write is
    page_id: str | None                      # the wiki page once started
    # that page is in the trash: the row counts as unwritten (status not_started), 「下一页」 does not
    # write it again by itself; the plugin offers 重新写
    trashed: bool = False
    skipped: bool = False                    # the user skipped it: 「下一页」 passes it (path_order.startable)
    key: str = ""                            # the row's path-edit key (PathItemOut.key): 学点新的 edits a block's pages by it
    title: str = ""                          # path@v6: the model's page title ("" on older paths)
    abstract: str = ""                       # path@v6: two or three sentences on the page (writer-only: never shown as the row's line)
    sections: list[str] = []                 # the materials' § labels of its target nodes (as PathItemOut.sections)
    exam: int = 0                            # the most items one target node has in the exams; 0 below EXAM_FREQUENT


class PathOut(BaseModel):
    path_id: str
    goal: str
    source: Literal["single", "llm", "fallback", "reading"]   # who grouped the pages (reading: units in order)
    pages: list[PathPageOut]
    next_index: int | None                   # the page 「下一页」 starts; None when all started
    prefetch: "PrefetchOut | None" = None    # the next page written ahead, if one is
    sources: list[GoalSourceOut] = []        # the materials this path came from (coverage, as GoalOut.sources)


class PrefetchOut(BaseModel):
    """The first page of the path the idle Bot wrote (or is writing) that nobody has read yet.
    writing: 「下一页」 attaches to the running stream; ready: it opens at once (it is a committed
    wiki page); failed: 「下一页」 writes it as usual."""

    index: int = Field(ge=1)
    page_id: str
    status: Literal["writing", "ready", "failed"]


class FileOut(BaseModel):
    """GET /wiki/files: one of the user's pages as its file needs it - where it goes and what it is
    called (the plugin moves / renames / trashes files to match). No sections; reading it records
    nothing."""

    page_id: str
    title: str
    folder: str = ""
    parent_name: str = ""
    trashed: bool = False
    organized: bool                          # False: the page has no text (yet)


class WriteOut(BaseModel):
    """GET /wiki/writes: where one page's write is (spec 2026-10-15 §4). `queued`: behind the
    user's other page run; `planning`: running, no outline yet; `writing`: `section` of `of`
    sections done; `done`; `failed`; `undone`: a Bot write the learner took back (the page is in
    the trash); `unwritten` (only when asked, `?unwritten=true`): no run, and the page holds no
    content yet. `pass` is the planning pass while `planning`; `started_at`: when the run began
    (queued: when it was queued); `cost_usd`: what the run cost (known when it ends)."""

    model_config = ConfigDict(populate_by_name=True)

    page_id: str
    run_id: str | None
    kind: str = "organize"                   # the run kind (organize / feedback / refine / ...)
    state: Literal["queued", "planning", "writing", "done", "failed", "undone", "unwritten"]
    pass_: int | None = Field(default=None, alias="pass")
    section: int | None = None
    of: int | None = None
    started_at: AwareDatetime | None = None
    cost_usd: float = 0.0


class PathNextIn(BaseModel):
    """POST /wiki/paths/{id}/next: no body or no key = the next page; `key` = the row he picked."""
    model_config = ConfigDict(extra="forbid")
    key: str | None = Field(default=None, max_length=200)


class PathNextOut(BaseModel):
    path_id: str
    index: int = Field(ge=1)                 # 1-based, as PathPageOut.index
    page_id: str                             # then POST /wiki/pages/{page_id}/organize (SSE)
    prefetched: bool = False                 # True: written ahead; organize replays / attaches to it


class PathPageDoneOut(BaseModel):
    path: PathOut
    done: DoneOut                            # as POST /wiki/pages/{page_id}/done


class PathCardOut(BaseModel):                # catalog
    path_id: str
    goal: str
    pages: int
    done: int
    next_index: int | None


# ---- editable path (unified node graph, plan §4) ----
class PathItemOut(BaseModel):
    """One page of a path as the user sees it (goals.path_edit). `key`: stable identity (edits
    refer to it); `node_ids`: the nodes the page teaches; `mine`: the user added it; `skipped`:
    the learner said 「我会了」 for every node of it; `page_id`: the wiki page once started;
    `skippable`: a row not started whose cells K all holds as 确定会 (可以跳过, a hint only)."""

    key: str
    title: str
    summary: str             # writer-facing (abstract / objective); the plugin shows `question`, never this
    question: str = ""       # the question the page answers, in the learner's words ("" when none was written)
    node_ids: list[str]
    status: PathPageStatus
    skipped: bool
    mine: bool
    state: PageState = "planned"             # wikiapp.progress (status is derived from it)
    progress: WriteProgressOut | None = None  # state writing: how far the write is
    page_id: str | None
    trashed: bool = False      # the page is in the trash: shown as unwritten, 重新写 offered
    skippable: bool = False    # K: the learner already knows it (可以跳过); still enterable
    opened: bool = False       # the learner opened the page (state.opened_at): 读了还是不懂, no 拆成多页
    sections: list[str] = []     # 「§3.2」 of the material sections its nodes come from
    exam: int = 0                # the most exam items one node has; 0 below EXAM_FREQUENT (常考)


class PathMoveIn(_In):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    op: Literal["move"]
    from_: int = Field(alias="from", ge=0)
    to: int = Field(ge=0)


class PathRemoveIn(_In):
    op: Literal["remove"]
    page_key: str = Field(min_length=1)


class PathUnremoveIn(_In):
    """Undo of a remove: the row with this key (hidden by the learner's remove) is shown again where it was."""

    op: Literal["unremove"]
    page_key: str = Field(min_length=1)


class PathAddIn(_In):
    op: Literal["add"]
    request_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=120)
    node_id: str | None = None
    to_index: int | None = Field(default=None, ge=0)        # None: the end of the shown path

    @field_validator("title")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("标题不能为空")
        return v.strip()


# indices refer to the shown path (PathEditOut.path / GoalOut.path), 0-based
class PathRetitleIn(_In):
    """重命名: the row's page by a new title (its note follows); 撤销 sends the old one."""

    op: Literal["retitle"]
    page_key: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("标题不能是空的")
        return v


PathEditIn = Annotated[PathMoveIn | PathRemoveIn | PathUnremoveIn | PathAddIn | PathRetitleIn, Field(discriminator="op")]


class PathEditOut(BaseModel):
    """`undo`: only after 「拆成多页」 — the graph change to undo it with
    (POST /wiki/paths/{id}/rows/{key}/split/undo). `kept`: 「拆成多页」 left the row's node whole
    (the path unchanged; one line to the learner why)."""

    path: list[PathItemOut]
    undo: str | None = None
    kept: str = ""


class SplitUndoIn(_In):
    change_id: str = Field(min_length=1)


class NodeWantIn(_In):
    map_id: str = Field(min_length=1)


class NodeActionOut(BaseModel):
    """「我会了，跳过」 / 「我想学这个」. `path`: the goal's path as shown now ([] without a goal
    or when the goal has no path). The graph-level response comes with GraphOut."""

    ok: bool = True
    path_id: str | None = None
    path: list[PathItemOut] = Field(default_factory=list)


# ---- the node graph (plan 2026-10-13-unified-node-graph §4) ----
RelationKind = Literal["part_of", "prerequisite", "recommended", "kind_of", "related"]
ShareBand = Literal["known", "shaky", "gap", "none"]                # likely counts as known


class GraphNodeOut(BaseModel):
    """One node of the graph. `direct`: shares per band of the node's own cells; `aggregate`: of
    every part_of descendant's cells; `mix`: both, direct cells weighted 2:1 ({} = no cells,
    else the shares sum to 1); `band` from the weighted score. `parents`: ALL part_of parents;
    `evidence`: direct evidence lines in plain words (quiz results, the learner's words);
    `kind`: the kind_of phrase (「一种分类模型」) or ""; `x`, `y` stable in [-1, 1];
    `can_split`: the node has no children (看不懂 can split it)."""

    id: str
    name: str
    definition: str
    band: Band
    mix: dict[Band, float]
    direct: dict[Band, float]
    aggregate: dict[Band, float]
    parents: list[str]
    evidence: list[str]
    kind: str
    x: float
    y: float
    can_split: bool


class RelationOut(BaseModel):
    """part_of: `src` is a part of `dst`; prerequisite: `src` is strictly required before `dst`;
    recommended: `src` helps understanding `dst` (not necessary); `phrase`: one line; `verified`:
    the edge was checked (only a verified prerequisite feeds inference and hard ordering); `split`:
    a part_of made by 看不懂 (`nodes.ops.split`): the cloud grows `src` out of `dst` (a topic's or
    a goal's parts are drawn in their own groups)."""

    kind: RelationKind
    src: str
    dst: str
    phrase: str
    verified: bool
    split: bool


class GroupOut(BaseModel):
    """A group of the graph (level 1 fine, level 2 coarse): a goal and its blocks (`map_id`), else an automatic
    group of the points on no goal; `members`: node ids (a point on two goals is in a block of each); `parent`: the
    level-2 group of a level-1 group; `x`, `y`: the centroid of its members; `at`: a top group's
    place among the top groups in [-1, 1] (similar groups near each other, `wikiapp.group_places`;
    None for a level-1 group inside a level-2 one, or when there is one top group)."""

    id: str
    level: Literal[1, 2]
    name: str
    summary: str
    members: list[str]
    parent: str | None
    x: float
    y: float
    at: tuple[float, float] | None = None
    map_id: str | None = None
    # a point on an earlier goal too: where its further dot stands in this block, world position by point id
    copies: dict[str, tuple[float, float]] = {}


class GoalPathOut(BaseModel):
    """One of a goal's paths: a goal split on demand has one per point or block opened."""
    path_id: str
    name: str                # the map node it hangs on
    path: list[PathItemOut]


class GoalOut(BaseModel):
    """One goal: its node subset, the share bar (sums to 1; all 0 without nodes), its path as
    shown (`path_id` None and `path` [] when it has none)."""

    map_id: str
    goal: str                # the learner's own sentence (shown as a tooltip)
    name: str                # the goal's topic: the intake target node's name, else its short title
    nodes: list[str]
    shares: dict[ShareBand, float]
    path_id: str | None
    path: list[PathItemOut]
    paths: list[GoalPathOut] = []            # all its paths, in the map's order (`path` is the last planned)
    sources: list[GoalSourceOut] = []
    reading: ReadingOut | None = None        # a 读懂 goal's units (reading-mode spec §5)


class DomainOut(BaseModel):
    """A broad domain over top groups whose subjects are close (`wikiapp.group_domains`); `kids`: their group ids."""

    id: str
    name: str
    summary: str
    kids: list[str]


class GraphOut(BaseModel):
    nodes: list[GraphNodeOut]
    relations: list[RelationOut]
    groups: list[GroupOut]
    goals: list[GoalOut]
    domains: list[DomainOut] = []


class GraphBlockIn(_In):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(max_length=400)
    scope: str = Field(default="", max_length=4000)


class GraphPlacesIn(_In):
    """POST /wiki/graph/places: a new goal's blocks, confirmed or not (学点新的 draws them on his map)."""

    blocks: list[GraphBlockIn] = Field(max_length=200)


class GraphPlacesOut(BaseModel):
    """Where his top groups and the blocks stand, similar ones near (`wikiapp.group_places`, [-1, 1]), and each block's
    links to what is nearest it. Read only; no LLM (a local embedder)."""

    places: dict[str, tuple[float, float]]
    links: list[tuple[str, str]]


class SplitIn(_In):
    """POST /wiki/nodes/{id}/split (看不懂 / 继续拆细): optional; `words` = the learner's own
    words; `map_id`: the goal map it was split from (its children go on that map only);
    `reason`: confused (看不懂, the default) or requested (继续拆细)."""

    words: str | None = Field(default=None, max_length=2000)
    map_id: str | None = Field(default=None, max_length=200)
    reason: Literal["confused", "requested"] = "confused"


class SplitOut(BaseModel):
    """The new children (ids, in the model's order) and the graph to redraw. `kept`: the node was
    left whole (no children; one line to the learner why — e.g. 看不懂 needs another way of
    explaining, not smaller parts); "" when split."""

    children: list[str]
    graph: GraphOut
    kept: str = ""


# ---- review ----
ReviewMode = Literal["cue", "check"]   # cue: cover the cue, recall, compare, grade; check: answer it


class ReviewCheckOut(BaseModel):             # the answer key stays server-side
    stem: str
    options: list[str]


class ReviewItemOut(BaseModel):
    card_id: str                             # stable per (page, section, mode); POST /wiki/review/cards/{id}/feedback
    cell: str                                # the card's lead cell (concept / aspect / band below are its)
    cells: list[str]                         # every due cell this card reviews, lead first; POST grade sends them
    concept_id: str
    concept_name: str
    aspect: Aspect
    band: Band                               # as shown now (forgetting included)
    mode: ReviewMode
    page_id: str
    page_title: str
    section_id: str
    heading: str
    cue: str = ""                            # the recall cue: the card's question ("" in check mode)
    answer: str = ""                         # cue mode: one or two sentences (Markdown); "": none written
    check: ReviewCheckOut | None = None      # check mode only
    recall_now: float
    days_to_threshold: float                 # < 0: already below the threshold
    in_goal: bool
    exam: bool = False                       # the concept is examined often (常考)
    artifact_id: str | None = None           # this card's version (0091): sent back with grade / feedback


class CardFeedbackIn(_In):
    """The review card's 这张卡有问题… box. kind None (the default) = the user's own words only:
    `text` is then required (non-blank) and the model decides from them what to do. The fixed
    kinds (unclear 题目没说清楚 / wrong 答案不对 / too_easy 太简单 / drop 不想再看) are still
    accepted, their words optional."""

    kind: CardFeedbackKind | None = None
    text: str = Field(default="", max_length=1000)    # the words, go into the signal and the rewrite
    # the artifact version the user was shown (the `artifact_id` this view carried; 0091)
    artifact_id: str | None = Field(default=None, pattern=ARTIFACT_PATTERN)

    @model_validator(mode="after")
    def _words(self) -> "CardFeedbackIn":
        if self.kind is None and not self.text.strip():
            raise ValueError("请写下这张卡哪里有问题")
        return self


class CardFeedbackOut(BaseModel):
    """too_easy / drop: hidden for good (`hidden`). kind None (own words): rewritten, or hidden
    when the model reads the words as a clear ask to never show the card again (`kind` stays
    None). unclear / wrong: the cue or check is rewritten
    in the page (`rewritten`, `card` = the new card); if it could not be (no LLM, bad output, the
    verifier does not confirm a single right answer) the old card stays and `reason` says why.
    The signal is always recorded."""

    card_id: str
    kind: CardFeedbackKind | None
    signal_id: str
    hidden: bool
    rewritten: bool
    card: ReviewItemOut | None = None
    reason: Literal["llm_unavailable", "llm_failed", "budget_exceeded", "invalid_output",
                    "not_verified", "conflict"] | None = None


class ReviewTodayOut(BaseModel):
    items: list[ReviewItemOut]               # cards, most urgent × important first, at most limit - done_today
    due: int                                 # every due card (beyond the cap too)
    done_today: int                          # cards answered since local midnight (Bot time zone), what the batch counts
    page_round_today: int = 0                # answers in 考考我这页's rounds since then: shown, outside the batch
    limit: int                               # settings review_per_day
    streak_days: int = 0                     # consecutive local days (Bot time zone) with a review / check answer, ending today (or yesterday while today has none)
    confirms: list[ConfirmCardOut] = []      # due confirmation questions (wrong-answer follow-ups)


class ReviewGradeIn(_In):
    cell: str = Field(min_length=1)
    cells: list[str] = Field(default_factory=list, max_length=64)   # a cue card's other cells (ReviewItemOut.cells): one grade for each
    grade: Grade | None = None               # 想起来了 / 有点模糊 / 没想起来
    choice: int | None = Field(default=None, ge=IDK)   # check mode: the option, -1 = 不知道
    page_id: str | None = Field(default=None, min_length=1)
    section_id: str | None = Field(default=None, min_length=1)
    # the artifact version the user was shown (the `artifact_id` this view carried; 0091)
    artifact_id: str | None = Field(default=None, pattern=ARTIFACT_PATTERN)
    page_round: bool = False                 # 考考我这页: not counted against the daily batch

    @model_validator(mode="after")
    def _one(self) -> "ReviewGradeIn":
        if (self.grade is None) == (self.choice is None):
            raise ValueError("请给出 grade，或 choice")
        if self.choice is not None and (self.page_id is None or self.section_id is None):
            raise ValueError("答确认题要给出 page_id 和 section_id")
        if self.section_id is not None and self.page_id is None:
            raise ValueError("给出 section_id 时也要给出 page_id")
        return self


class ReviewGradeOut(BaseModel):
    band: CellChangeOut                      # the graded cell, before → after
    correct: bool | None = None              # check mode; None for 不知道 and in cue mode
    correct_index: int | None = None         # check mode only
    page_id: str | None                      # the page the K change updated (given, else current)
    change: ChangeOut                        # folds / patch there; other_pages_stale = 待更新
    answer_ref: str = ""                     # this answer's evidence ref (for POST …/followup)


class BandChangeOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    cell: str
    concept_id: str
    concept_name: str
    aspect: Aspect
    from_: Band = Field(alias="from")
    to: Band
    forgot: bool = False                     # the drop is (partly) forgetting


class FadingOut(BaseModel):
    cell: str
    concept_id: str
    concept_name: str
    aspect: Aspect
    band: Band
    recall_now: float
    days_to_threshold: float
    relearning: bool


class ReviewCountsOut(BaseModel):
    recalled: int
    fuzzy: int
    forgot: int


class DigestOut(BaseModel):
    period: Period
    start: AwareDatetime
    end: AwareDatetime
    progressed: list[BandChangeOut]          # 进步了什么
    slipped: list[BandChangeOut]             # 哪些在退步 / 遗忘
    fading: list[FadingOut]                  # 快忘了的
    reviews: ReviewCountsOut                 # 这段时间的自评复习
    today: ReviewTodayOut                    # 今天该复习什么


# ---- catalog ----
class PageCardOut(BaseModel):
    page_id: str
    title: str
    goal: str
    stale: bool                          # 「待更新」
    done: bool
    organized: bool


class ShelfOut(BaseModel):
    pages: list[PageCardOut]             # the pages shelved as reference cards


class SettingsIO(_In):
    review_per_day: int = Field(default=10, ge=0)   # 每天最多复习几条 (a batch; more on request)
    # 单页写作花费上限（美元）；None = 不限（开发期默认）
    page_limit_usd: float | None = Field(default=None, ge=0.05)     # no upper bound: the user sets it
    language: Language = "zh"                               # 生成内容的语言

class CatalogOut(BaseModel):
    learning: list[PageCardOut]          # 在学
    shelf: ShelfOut                      # 参考书架
    settings: SettingsIO
    current_page: str | None
    names: dict[str, str] = {}                 # every concept / page id referenced -> display name
    paths: list[PathCardOut] = []                          # 学习路径
    maps: list[MapCardOut] = []                            # 大目标的学习地图


# ---- K grid ----
class NoteOut(BaseModel):
    entry_id: str
    cell: str
    kind: Literal["quiz", "check", "self", "feedback", "ask", "declare", "review"]
    text: str                            # raw content: user's quote / LLM reason / option text
    evidence_ref: str
    label: str | None = None             # quiz evidence: 「题目：<题干>；选了「…」」 shown as-is; else / old None


class CellBeliefOut(BaseModel):
    cell: str
    concept_id: str                      # concept name is on KConceptOut.name
    aspect: Aspect
    band: Band
    m: float
    u: float
    recall: float
    direct: float
    inferred_pos: float
    inferred_neg: float
    inferred_only: bool
    declared: Literal["know", "dont"] | None


class KConceptOut(BaseModel):
    id: str
    name: str
    role: ConceptRole | None
    aspects: list[Aspect]
    prereqs: list[str]
    on_current_page: bool
    cells: list[CellBeliefOut]
    notes: list[NoteOut]


class KGridOut(BaseModel):
    concepts: list[KConceptOut]


class DeclareIn(_In):
    value: Literal["know", "dont"] | None


class DeclareOut(BaseModel):
    cell: CellBeliefOut
    change: ChangeOut


# ---- guidelines ----
class GuidelineOut(BaseModel):
    guideline_id: str
    text: str
    source: Literal["user", "feedback"]
    evidence: str | None = None                # LLM-written evidence when source == "feedback"


class GuidelinesOut(BaseModel):
    items: list[GuidelineOut]


class KnownRetractedOut(BaseModel):
    lines: list[str]
    action_id: str | None                      # the ledger row whose undo says them again


class ProfileOut(BaseModel):                   # GET / PUT /wiki/profile: the 「我」 note's text
    text: str
    locale: Literal["zh", "en"] = "zh"
    format_version: Literal[2] = 2
    version: str                               # of this copy: the next PUT names it as `base`
    kept: list[str] = []                       # PUT: lines his note lacks that it could not retract
    retracted: KnownRetractedOut | None = None  # PUT: lines of 已经会的 he deleted that K took back


class ProfileIn(_In):                          # PUT /wiki/profile: the note as he saved it
    text: str = Field(max_length=20000)
    base: str | None = None                    # the version of the copy it is based on


class GuidelineIn(_In):
    text: str = Field(min_length=1, max_length=300)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("原则不能为空")
        return v


# ---- R1: teaching spec θ_rep,u (/wiki/teach) ----
# Every change below saves one whole new version (version = previous + 1). A user who never saved
# one reads the virtual v1 (default items + their old guidelines, `stored` false); their first
# change commits v1 (action "migrated") and then the change as v2. Each write takes an optional
# `?base_version=` query parameter: when it is not the current version → 409, nothing changed.
# Caps → 422 (3 exemplars, 500-character rule, 60-character domain, 60_000-character
# exemplar); an unknown item / exemplar / version → 404.

class RuleDecisionOut(BaseModel):
    """A user's decision in force on a rule no longer in the spec (个性化 · 管理偏好 「没采用和撤回的」):
    rejected = a pending / proposed rule not adopted, revoked = a rule the user removed."""

    decision_id: str
    rule_id: str
    text: str
    action: Literal["rejected", "revoked"]
    via: DecisionVia


class TeachSpecOut(BaseModel):
    version: int                               # 1 for the virtual v1
    stored: bool                               # False: nothing saved yet (virtual v1)
    items: list[SpecItem]                      # spec order (what the writers read, in order)
    exemplars: list[Exemplar]
    params: RepParams
    evidence: dict[str, str] = {}              # item id -> why it was adopted (feedback / proposal)
    decisions: list[RuleDecisionOut] = []      # rejected / revoked rules not in the spec, newest first
    max_items: int = SUGGESTED_SPEC_ITEMS      # a suggestion only: never enforced (no item count cap)
    max_exemplars: int = 3


class TeachVersionOut(BaseModel):
    version: int
    at: AwareDatetime
    action: SpecAction                         # migrated|edit|params|import|exemplar|restore|guideline|rsi|rsi_undo
    origin: str | None = None                  # import name / "v{n}" for a restore / proposal id
    n_items: int
    n_exemplars: int


class TeachVersionsOut(BaseModel):
    current: int                               # effective version (1 when nothing is stored)
    items: list[TeachVersionOut]               # oldest first; [] when nothing is stored


class SpecItemIn(_In):
    text: str = Field(min_length=1, max_length=MAX_ITEM_TEXT)
    scope: SpecScope = "any"
    domain: str | None = Field(default=None, max_length=60)      # None / blank = every domain

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("规则不能为空")
        return v


class SpecItemPatchIn(_In):
    """Only the fields sent change; `"domain": null` clears the domain, leaving it out keeps it."""

    text: str | None = Field(default=None, min_length=1, max_length=MAX_ITEM_TEXT)
    scope: SpecScope | None = None
    domain: str | None = Field(default=None, max_length=60)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("规则不能为空")
        return v

    @model_validator(mode="after")
    def _something(self) -> "SpecItemPatchIn":
        if not self.model_fields_set:
            raise ValueError("请至少改一项：text、scope 或 domain")
        if "text" in self.model_fields_set and self.text is None:
            raise ValueError("规则不能为空")
        return self


class WikiPageExemplarIn(_In):
    """The server snapshots the organized page (title + each section's heading / cue / body)."""

    kind: Literal["wiki_page"]
    ref: str = Field(min_length=1, max_length=400)              # page id


class VaultNoteExemplarIn(_In):
    """A vault note the plugin uploaded as a snapshot."""

    kind: Literal["vault_note"]
    ref: str = Field(min_length=1, max_length=400)              # vault-relative path
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=60_000)


ExemplarIn = Annotated[WikiPageExemplarIn | VaultNoteExemplarIn, Field(discriminator="kind")]


# ---- R1: feedback signals ----
# Page feedback (body `teach.contracts.PageFeedbackIn`) and hand edits each record one signal and a
# usage record (「整页反馈」 / 「改了笔记」). Unknown page → 404; a section_id not in the page's outline
# → 422; an empty page feedback (no chip, blank text) → 422.
# GET /wiki/teach/signals: the user's signals paged by COMMIT order (stored signals by wiki_event
# seq, assistant selections by the assistant store's insertion order — never by `at`, which can be
# older than a signal committed later). `after` = the opaque, URL-safe cursor ("w{seq}.a{pos}",
# only [A-Za-z0-9.-]) of an earlier response; a signal committed after that response is returned
# from its cursor even if its `at` is older. Items within one page are sorted by (at, signal_id).
# `cursor` is always set (continue / poll from it); `next` = the same cursor while more signals are
# waiting, None once both sources are exhausted. A cursor belongs to its `kinds` filter. limit
# 1..500 (default 200); kinds = comma-separated SignalKind values. Bad cursor / unknown kind → 422.
MAX_NOTE_SUMMARY = 2000
DEFAULT_SIGNAL_LIMIT = 200
MAX_SIGNAL_LIMIT = 500


class HandEditSectionIn(_In):
    """One section the user changed in his note. `body`: the text as everything reads it; `note_body`:
    the same text as he typed it, the note shows this. `heading` / `cue` / `folded`: only when he changed
    them."""

    section_id: str = Field(min_length=1)
    body: str = Field(min_length=1)
    note_body: str = Field(min_length=1)
    heading: str | None = Field(default=None, min_length=1)
    cue: str | None = Field(default=None, max_length=60)
    folded: bool | None = None
    fingerprint: str | None = None       # SectionOut.fingerprint of the copy the note was written from


class HandEditRemovedIn(_In):
    section_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)


class HandEditIn(_In):
    """What the user did in the page's note (plugin side): the sections he changed, those he deleted,
    and a short summary of it (the `note_edit` signal's text)."""

    sections: list[HandEditSectionIn] = []
    removed: list[HandEditRemovedIn] = []
    summary: str = Field(min_length=1, max_length=MAX_NOTE_SUMMARY)


class HandEditOut(BaseModel):
    """The page after a hand edit or its undo. `edit_id`: the edit, None when nothing changed."""

    edit_id: str | None = None
    page: "PageOut"


class SignalCreatedOut(BaseModel):
    signal_id: str


class SignalsOut(BaseModel):
    items: list[Signal]                        # this page, sorted by (at, signal_id)
    cursor: str                                # always: `after` to continue / poll from here
    next: str | None = None                    # = cursor while more are waiting; None at the end


# ---- GET /wiki/engine (the plugin's 参数 tab) ----

class EnginePromptOut(BaseModel):
    """One step of the pipeline: the prompt version ids it uses (joined with " · "), the model this
    user's calls would go to right now (human label), and the prompt text verbatim (per-call parts
    as 〈…〉 placeholders; several prompts each under a one-line 【id】 header)."""

    step: str
    ver: str
    model: str
    text: str


class EngineParamOut(BaseModel):
    """One tunable θ_infer path (teach/infer_paths.PATHS order): its Chinese label, the formatted
    value ("0.6", "× 1.0"), whether it is the default, and — when tuned — the date (ISO, in
    EVOWIKI_USAGE_TZ) and why of the latest live track-A patch that set it (None when unknown)."""

    path: str
    label: str
    description: str
    value: str
    default: bool
    tuned_at: str | None = None
    why: str | None = None


class EngineOut(BaseModel):
    prompts: list[EnginePromptOut]
    params: list[EngineParamOut]


# ---- /wiki/prompts (Prompt 设置, plan 2026-10-21 参数 redesign D1 / D3) ----

class PromptIn(_In):
    """The user's own guidance for one step (checked by `bot.prompts.validate`: 422 with a plain
    Chinese reason)."""

    text: str


class PromptStepOut(BaseModel):
    """One model step (llm/prompt_steps.STEPS order): its key, learner-facing name, the guidance
    the model reads now (`text`: the user's override, else the default), the default wording
    (per-call parts as 〈…〉 placeholders) and whether the user changed it. `format_note`: on
    `understand` when his own wording was kept as 拿不准 by the old-format rules
    (`harness.understand_format`, reading-mode spec §7 item 1), else ""."""

    step: str
    name: str
    category: Literal["global", "planning", "materials", "writing", "review", "assistant", "memory"]
    description: str
    text: str
    default_text: str
    edited: bool
    format_note: str = ""


# ---- 真题 (materials phase 5, F): the page's exam practice, self-reports, the material's summary ----

class ExamOpenItemOut(BaseModel):
    """An item the plugin draws natively (open questions, objective ones without a key or that did
    not fit the quiz): the reader looks at the key and reports how it went."""

    item_id: str
    number: str | None                     # the printed question number, verbatim
    context: str | None                    # the passage of its 大题 (group), if any
    stem: str
    options: list[str]
    kind: Literal["single", "multi", "tf", "open"]
    has_key: bool
    answer: str | None
    explanation: str | None
    source_title: str
    page: int | None                       # PDF page
    printed_page: str | None               # the number printed on that page (None: show `page`)
    done: bool                             # reported today (right / wrong / done)


class PageExamOut(BaseModel):
    """GET /wiki/pages/{id}/exam. `asset_id`: the evoui quiz of the keyed objective items (graded by
    POST /wiki/assets/{id}/results); None when there is none. Nothing attached: asset_id None and
    no open items (the plugin draws nothing)."""

    asset_id: str | None
    quiz_count: int
    open_items: list[ExamOpenItemOut]
    sources: list[str]                     # the materials' titles (the ? hover)
    frequent: bool                         # one of the page's nodes is 常考


class ExamSelfIn(_In):
    result: Literal["right", "wrong", "done"]


class ExamSelfOut(BaseModel):
    """k: weak self-report evidence in K; score: recorded, the item's cell is not in K; done: marked
    done (no evidence); duplicate: already reported today."""

    status: Literal["k", "score", "done", "duplicate"]


class ExamSummaryOut(BaseModel):
    """SourceDetail.exam: the extraction state (progress) with the stored counts; questions only
    (a group is a passage). `unattached_items`: the first 20, 「第 3 题：题干前 60 字」 (+ reason)."""

    state: Literal["extracting", "done", "failed"]
    items: int
    attached: int
    unattached: int
    pending: int
    unreadable: int
    incomplete: int
    unattached_items: list[str]
    detail: str | None
