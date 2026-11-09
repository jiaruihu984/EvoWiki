"""Wiki events (plan D1: snapshot events). Every page change appends a whole `PageRecord`
(`page_saved`, the last one wins). All models are frozen and forbid extra fields; bodies are told apart by `type`."""

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from evowiki.deep.contracts import RefineSettings
from evowiki.goals.contracts import Band, CheckItem, OutlineSection, SectionState, SectionText
from evowiki.goals.fold import Page
from evowiki.goals.intake import IntakeConcept, Subitem
from evowiki.goals.language import Language
from evowiki.goals.page_fit import PageFit
from evowiki.goals.path import LearningPath
from evowiki.goals.path_edit import PathEdit
from evowiki.goals.probe import NodeLevel, ProbeQuestion, ProbeRound
from evowiki.goals.reading import Reading
from evowiki.goals.regions import GoalMap
from evowiki.goals.review import ReviewSettings
from evowiki.nodes.model import NodeGraph
from evowiki.sources.cite import CiteIssue
from evowiki.teach.contracts import (
    SectionLog,
    Signal,
    SpecScope,
    TeachingSpec,
)
from evowiki.wikiapp.series import SeriesUpdate

SectionChange = Literal["cached", "rewritten", "inserted", "folded", "unfolded"]


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConceptName(_M):
    """`atomic` (on-demand decomposition, spec 2026-10-28 §2.2): the current graph's call on
    whether this node is already one independently learnable and checkable thing (True), still
    to be split (False), or not judged yet (None: every older event). Written only by a
    decomposition (a block: False; a split's parts: the model's call) and by 「继续拆细」 (False);
    never inferred from page size or names. Whether it is expanded is the graph's part_of edges,
    never stored."""

    concept_id: str
    name: str
    subitems: tuple[Subitem, ...] = ()
    atomic: bool | None = None


class Draft(_M):
    draft_id: str
    goal: str
    title: str
    concepts: tuple[IntakeConcept, ...]
    targets: tuple[str, ...]
    items: tuple[CheckItem, ...]                 # verified; answer keys stay server-side
    from_background: int
    reused: int
    dropped_questions: int
    # big goal: adaptive probe rounds (`items`: an old draft's, read only), and the goal map /
    # region this draft diagnoses (region None: the map's opening diagnosis; map None: an old
    # flat goal's draft, refused by goal.OLD_DRAFT)
    rounds: tuple[ProbeRound, ...] = ()
    map_id: str | None = None
    region_id: str | None = None
    # node ids K had evidence for before this draft: shown with their bands, never asked about
    evidenced: tuple[str, ...] = ()
    # concept ids the learner's own description in this goal already gave evidence on (intake
    # observations): no self-report is asked about them (goals/probe)
    described: tuple[str, ...] = ()
    # aligned with `described`: what the description said per concept ("+" every observation
    # positive, "-" every one negative, "~" mixed); it enters the KST diagnosis as weak evidence.
    # Empty in drafts from before probe@v4 (those ids then only get no self-report)
    described_as: tuple[str, ...] = ()
    # probe@v5: the intake gap identifier's level + confidence per node (concepts, and regions by
    # their map id), the KST diagnosis's prior (goals/probe, kst.level_p0). Empty in older drafts:
    # the prior then falls back to `described_as`, then 1/2. A region draft carries the opening
    # draft's levels of its members
    levels: tuple[NodeLevel, ...] = ()
    # the materials the goal was created from (source ids) and how: "learn" (按这份学) /
    # "understand" (读懂这篇); none in older drafts and goals without a material
    sources: tuple[str, ...] = ()
    source_use: str | None = None
    # the 摸底 grid (ask_rough, stage 3 phase 2): block id -> his mark (know / partly / dont), and
    # the calls whose marks are recorded (once per call). A block marked dont is taught as it is
    rough: dict[str, str] = {}
    rough_calls: tuple[str, ...] = ()


class Guideline(_M):
    guideline_id: str
    text: str
    source: Literal["user", "feedback"]
    evidence: str | None = None                  # LLM-written evidence when source == "feedback"


class GuidelineProposal(_M):
    proposal_id: str
    text: str
    evidence: str


class UsageRecord(_M):
    action: str
    page: str | None = None
    heading: str | None = None
    text: str | None = None


class CellChange(_M):
    cell: str
    before: Band                                 # wire: "from"
    after: Band                                  # wire: "to"


class DiffNote(_M):
    """「自你上次来」, structured (D17)."""

    cell_changes: tuple[CellChange, ...] = ()
    folded: tuple[str, ...] = ()
    unfolded: tuple[str, ...] = ()
    patched: tuple[str, ...] = ()


WriteKind = Literal["page", "rewrite", "feedback", "assistant", "card", "practice", "revise",
                    "hand"]
# writes of the whole page (from its outline / a full rewrite); the others change only the
# `sections` they name and leave the rest of the page as it was
WHOLE_PAGE_WRITES = frozenset({"page", "rewrite"})


class WriteMark(_M):
    """Who wrote this page's current text (single writer, 2026-10-10): the deep writer's run, the
    model that finished it, what it cost, and — when the primary writer was unavailable and the
    fallback wrote it — why (`fallback_reason`; None = the primary). `sections`: the sections that
    run wrote (page order); `assets`: interactive asset ids it placed. `kind`: a whole page from its
    outline, a rewrite (user / RSI), or a partial write (feedback, assistant apply, review card,
    practice, materials revision): only `sections` changed."""

    job_id: str
    at: AwareDatetime
    model: str
    sections: tuple[str, ...]
    assets: tuple[str, ...] = ()
    spec_version: int | None = None
    cost_usd: float = Field(default=0.0, ge=0.0)
    fallback_reason: str | None = None
    kind: WriteKind = "page"


class WritingMark(_M):
    """A page's first write is running (LLM-call consolidation stage 4, commit as you go), or one
    stopped part-way: `PageRecord.page` holds the outline it planned and the sections it has
    committed so far. Such a page is never taught, read or done (`PageRecord.has_body`); the
    run's final commit clears the mark, a failed or stopped run leaves it (the sections stay
    visible, the next write overwrites them and is still a first write). Only first writes get
    it: a write over a finished page keeps that page until its final commit."""

    job_id: str


class PageSource(_M):
    """A material a page's current text was written from, with its title at that time: the
    page's citation marks name it by that title (a later rename still resolves through the id)."""

    source_id: str
    title: str
    page_offset: int | None = None               # PDF page − printed page then (citation check)


class MergedFrom(_M):
    """A page a merged page is written from (`page_merge`, the merge job's input): its id, title,
    the version the learner approved (`page_fingerprint`) and its text then (headings + bodies)."""

    page_id: str
    title: str
    version: str
    text: str


class PageRecord(_M):
    page_id: str
    title: str
    goal: str
    concepts: tuple[str, ...]
    targets: tuple[str, ...]
    # the wiki folder the page lives in: chosen when it was created (`wikiapp.page_folder`), it never
    # moves (spec 2026-10-29 retire-curate). "" = the Wiki root.
    folder: str = ""
    page: Page | None = None                     # None until organized; carries unresolved_conflicts
    changed: dict[str, SectionChange] = Field(default_factory=dict)
    checks: dict[str, int] = Field(default_factory=dict)    # section id -> choice (-1 = 不知道)
    diff: DiffNote | None = None
    # 收为参考卡 (spec 2026-10-29 retire-curate §4): the learner shelved the page; its fold
    # (`collapse_applied`) is applied on shelving, or on the next open after a regeneration
    shelved: bool = False
    collapse_applied: bool = False               # the reference-card fold has been applied
    done: bool = False
    created_at: AwareDatetime
    domain: str | None = None                    # teaching-spec domain (R1), set when organized
    written: WriteMark | None = None             # the writer run behind the current text
    feedback: "FeedbackMark | None" = None       # 「这一页按你的反馈改了：…· 撤回」 (B1)
    hand_edit: "HandEditMark | None" = None      # the user's last hand edit in the note (· 撤销)
    # confirmed revisions from materials (`wikiapp.revise`; PageSaved actions "source_revise" /
    # "source_revise_undo"); at REVISE_REBUILD_AFTER the next one rewrites from the materials
    revisions: int = Field(default=0, ge=0)
    # the materials the last write used (each write replaces it: materials Task 6)
    sources: tuple[PageSource, ...] = ()
    # citation / disagreement marks the last write's check did not keep as written (Task 6b; for
    # RSI, never shown)
    cite_issues: tuple[CiteIssue, ...] = ()
    # a merged page: the pages it merges, as approved (its first write keeps their content)
    merged_from: tuple[MergedFrom, ...] = ()
    writing: WritingMark | None = None           # a run is writing the page (or stopped part-way)
    # a reading page (reading-mode spec §4): one unit `unit_id` of the reading `reading_id`, read
    # along the material; the writer knows it by `kind` (older pages: "learn", None, None)
    kind: Literal["learn", "reading"] = "learn"
    unit_id: str | None = None
    reading_id: str | None = None

    @property
    def has_body(self) -> bool:
        """The page has its finished text: organized, and no run is writing it. Only such a
        page counts as taught, read or done (D04, D39); a page a run is writing, or one it left
        part-way, shows its committed sections and nothing more."""
        return self.page is not None and self.writing is None


class WikiStarted(_M):
    type: Literal["wiki_started"] = "wiki_started"
    aspect_config: str


class LanguageSet(_M):
    type: Literal["language_set"] = "language_set"
    language: Language


class ConceptsNamed(_M):
    type: Literal["concepts_named"] = "concepts_named"
    names: tuple[ConceptName, ...]


class DraftCreated(_M):
    type: Literal["draft_created"] = "draft_created"
    draft: Draft


class DraftClosed(_M):
    type: Literal["draft_closed"] = "draft_closed"
    draft_id: str
    outcome: Literal["answered", "skipped", "stopped"]     # stopped: the user's 「够了，开始学」
    page_id: str | None = None
    path_id: str | None = None
    map_id: str | None = None


class ProbeAnswered(_M):
    """Answers to round `round_index` (1-based) of a draft, and the next round (its pick; None:
    the diagnosis ended). `questions`: the question the assistant wrote on the round's pick
    (stage 3 T11: the round carried only the pick until it was asked)."""

    type: Literal["probe_answered"] = "probe_answered"
    draft_id: str
    round_index: int
    answers: tuple[int, ...]
    next_round: ProbeRound | None = None
    questions: tuple[ProbeQuestion, ...] | None = None


class RoughMarked(_M):
    """The 摸底 grid's marks on a draft (ask_rough call `call_id`), and its open round picked again
    with them (`next_round`: replaces the draft's last, unanswered round)."""

    type: Literal["rough_marked"] = "rough_marked"
    draft_id: str
    call_id: str
    marks: dict[str, str]
    next_round: ProbeRound


class PageFitSaved(_M):
    """A judgement of an existing page for a goal's point (goals/page_fit.py). The last one of a
    map's point and page wins."""

    type: Literal["page_fit_saved"] = "page_fit_saved"
    record: PageFit


class GoalMapSaved(_M):
    """A whole goal map (snapshot; the last one wins). `edited`: the learner's edits of the
    学习路径 card or their 撤销 (wikiapp/map_edits.py)."""

    type: Literal["goal_map_saved"] = "goal_map_saved"
    record: GoalMap
    action: Literal["planned", "expanded", "path_planned", "page_created", "diagnosed", "edited"]


class ReadingSaved(_M):
    """A whole reading of a 读懂 goal (reading-mode spec §2.1; snapshot, the last one wins, as
    GoalMapSaved): `planned` on confirm, `edited` by a merge / skip / unskip, `unit_analyzed`
    after a unit's 「看这一段」 call, `page_started` when a unit's page is made, `uses_added` when
    a prerequisite found while reading joins a unit."""

    type: Literal["reading_saved"] = "reading_saved"
    record: Reading
    action: Literal["planned", "edited", "unit_analyzed", "page_started", "uses_added"]


class FeedbackMark(_M):
    """The page-feedback patch now on the page (Obsidian flow §4.2): what it changed and how to undo
    it (POST /wiki/pages/{id}/feedback/{feedback_id}/undo)."""

    feedback_id: str
    at: AwareDatetime
    summary: str
    reading: str                                 # 「系统的理解」 shown with the change
    sections: tuple[str, ...] = ()               # sections it added to or inserted, page order
    assets: tuple[str, ...] = ()                 # library components placed (asset ids)


class HandEditMark(_M):
    """The user's last hand edit of the page's note (POST /wiki/pages/{id}/hand-edit), for the page's
    top line and its undo. `removed`: the headings of the sections he deleted."""

    edit_id: str
    at: AwareDatetime
    sections: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


PageRecord.model_rebuild()       # resolves the forward references to FeedbackMark, HandEditMark


class HandEditChange(_M):
    """One section of a hand edit, what its undo puts back. `after_text`: what the edit left (None:
    the section was deleted); `after_section`: the section before it on the page then."""

    section_id: str
    before_section: OutlineSection
    before_text: SectionText | None = None
    before_state: SectionState | None = None
    after_section: str | None = None
    after_text: SectionText | None = None


class HandEdited(_M):
    """A hand edit in the note became the page's text. `prev_sources` / `prev_issues`: what the page
    cited before, since the edit may have taken its last mark of a material out."""

    type: Literal["hand_edited"] = "hand_edited"
    edit_id: str
    page_id: str
    signal_id: str
    changes: tuple[HandEditChange, ...]
    prev_sources: tuple[PageSource, ...] = ()
    prev_issues: tuple[CiteIssue, ...] = ()


class HandEditUndone(_M):
    type: Literal["hand_edit_undone"] = "hand_edit_undone"
    edit_id: str


class FeedbackApplied(_M):
    """One page feedback that changed the page. `before` is the record it replaced (undo restores it
    while the page's content is still `after`'s)."""

    type: Literal["feedback_applied"] = "feedback_applied"
    feedback_id: str
    page_id: str
    signal_id: str
    before: "PageRecord"
    after: "PageRecord"


class FeedbackUndone(_M):
    type: Literal["feedback_undone"] = "feedback_undone"
    feedback_id: str


class PageSaved(_M):
    type: Literal["page_saved"] = "page_saved"
    record: PageRecord
    action: str


class PageOpened(_M):
    type: Literal["page_opened"] = "page_opened"
    page_id: str


class UsageAdded(_M):
    type: Literal["usage_added"] = "usage_added"
    record: UsageRecord


class GuidelineAdded(_M):
    type: Literal["guideline_added"] = "guideline_added"
    guideline: Guideline


class GuidelineRemoved(_M):
    type: Literal["guideline_removed"] = "guideline_removed"
    guideline_id: str


class GuidelineProposed(_M):
    type: Literal["guideline_proposed"] = "guideline_proposed"
    proposal: GuidelineProposal


class GuidelineProposalClosed(_M):
    """The proposal was adopted (a `guideline_added` right before records the guideline itself)
    or superseded by a newer proposal (not a rejection: its text is not remembered)."""

    type: Literal["guideline_proposal_closed"] = "guideline_proposal_closed"
    proposal_id: str


class GuidelineProposalRejected(_M):
    type: Literal["guideline_proposal_rejected"] = "guideline_proposal_rejected"
    proposal_id: str


class PathRecord(_M):
    """A learning path (spec §9.2) as V holds it: the goal hub with ordered pages
    (`path.hub()`), plus the wiki page each page became once started (`page_ids`, aligned with
    `path.pages`). Statuses shown to the user are derived (paths.path_out). `edits`: the user's
    edits in order (goals.path_edit; what is shown = `apply_edits(path.pages, edits)`); older
    records have none."""

    path: LearningPath
    page_ids: tuple[str | None, ...]
    created_at: AwareDatetime
    edits: tuple[PathEdit, ...] = ()
    sources: tuple[str, ...] = ()               # materials the goal came from (Draft.sources)

    @model_validator(mode="after")
    def _aligned(self) -> "PathRecord":
        if len(self.page_ids) != len(self.path.pages):
            raise ValueError("page_ids must align with path.pages")
        return self


class PathSaved(_M):
    type: Literal["path_saved"] = "path_saved"
    record: PathRecord
    action: Literal["planned", "page_started", "user_edit", "replanned"]


class ReviewSettingsSet(_M):
    type: Literal["review_settings_set"] = "review_settings_set"
    settings: ReviewSettings


class RefineSettingsSet(_M):
    type: Literal["refine_settings_set"] = "refine_settings_set"
    settings: RefineSettings


# rsi / rsi_undo: an RSI-B patch the user accepted / its rollback (origin = the patch id)
SpecAction = Literal["migrated", "edit", "params", "import", "exemplar", "restore", "guideline",
                     "rsi", "rsi_undo"]


class TeachingSpecSaved(_M):
    """A whole teaching-spec version (snapshot); `spec.version` = previous + 1 (the first save of a
    user with no stored spec is v1 `migrated`). `evidence` (item id → text) is merged into
    `WikiState.item_evidence` (the old guideline evidence; `SpecItem` has no evidence field)."""

    type: Literal["teaching_spec_saved"] = "teaching_spec_saved"
    spec: TeachingSpec
    action: SpecAction
    origin: str | None = None
    evidence: dict[str, str] = Field(default_factory=dict)


DecisionAction = Literal["rejected", "revoked", "reaccepted", "taken_over"]
DecisionVia = Literal["ui", "chat", "ledger_undo", "ledger_reject", "profile"]


class RuleDecided(_M):
    """A user's decision on a teaching rule (rule provenance contract §1.2). Only user actions write
    it; RSI's own rollbacks and gate refusals never do. `rule_id` stays after the rule is gone;
    `rule_rev` is the content version decided on; `text_key` = `teach.textsim.normalize(text)`;
    `scope` / `domain` bound the decision (one rejection never becomes a ban on a whole topic).
    rejected: a proposal or pending rule refused · revoked: a rule in force removed by the user ·
    reaccepted: the user re-added / re-said / restored / confirmed a rule (an earlier rejection or
    revocation of it no longer holds) · taken_over: the user made an RSI rule their own (「设为我的
    规则」, decision 2: origin unchanged, authority user)."""

    type: Literal["rule_decided"] = "rule_decided"
    decision_id: str
    rule_id: str
    rule_rev: int = Field(default=1, ge=1)
    text: str = Field(min_length=1, max_length=2000)
    text_key: str
    scope: SpecScope = "any"
    domain: str | None = None
    action: DecisionAction
    actor: Literal["user"] = "user"
    via: DecisionVia
    ref: str | None = Field(default=None, max_length=400)
    reason: str | None = Field(default=None, max_length=600)


class StaleMark(_M):
    """A page an RSI (track B) change left out of date: `patch_id` = the latest change that marked
    it, `spec_version` = the teaching-spec version that change produced. The page stays out of
    date while it was last written under an older version (`wikiapp.stale`). `failures` = how many
    lazy rewrites of the page failed in a row under the teaching-spec version `failed_version`
    (`PageRewriteFailed`; the last at `failed_at`); a new mark (a later change) starts at 0, and
    failures under an older version than the current one do not count (`wikiapp.stale.failures`)."""

    patch_id: str
    spec_version: int = Field(ge=1)
    failures: int = Field(default=0, ge=0)
    failed_at: AwareDatetime | None = None
    failed_version: int | None = None


class PagesStaleSet(_M):
    """The pages a track-B spec change (apply, rollback, undo) left out of date, rewritten lazily
    when the learner next opens them. `marked` replaces each page's mark (one mark per page, the
    latest change), `cleared` drops marks (an undo restored what the page was written under),
    `checked_version` = the teaching-spec version up to which RSI changes were compared."""

    type: Literal["pages_stale_set"] = "pages_stale_set"
    marked: dict[str, StaleMark] = Field(default_factory=dict)
    cleared: tuple[str, ...] = ()
    checked_version: int = Field(ge=0)


class PageRewriteFailed(_M):
    """A lazy rewrite of a marked page (refine trigger "rsi") ended failed or raised; it ran under
    teaching-spec version `spec_version`. Counted on the page's mark (`StaleMark.failures`), which
    bounds the automatic retries (`wikiapp.stale.rewrite_due`); ignored when the page has no mark.
    `failure` = the refine failure code (or "error"), for logs. Kind "patch": the scoped write of
    the sections a K-change patch left waiting (`sections`, page order) failed; counted on
    `WikiState.patch_failures` (`wikiapp.stale.patch_due`)."""

    type: Literal["page_rewrite_failed"] = "page_rewrite_failed"
    page_id: str
    spec_version: int = Field(ge=1)
    failure: str = Field(default="", max_length=200)
    kind: Literal["rsi", "reorder", "patch"] = "rsi"    # which mark the failed rewrite counts on
    sections: tuple[str, ...] = ()                       # kind "patch": the sections it wrote


class PatchFailure(_M):
    """State only: the scoped writes of a page's patched sections that failed in a row
    (`PageRewriteFailed(kind="patch")`, the last at `failed_at`), for the waiting set `sections`
    they failed on. They count only while the page still waits for exactly that set: a new patch
    re-arms the retries (`wikiapp.stale.patch_due`)."""

    sections: tuple[str, ...]
    failures: int = Field(default=0, ge=0)
    failed_at: AwareDatetime | None = None


class ReorderMark(_M):
    """State only: a page written for a path order that changed (`PageMarkedStale`). `failures` =
    rewrites of it that failed in a row (`PageRewriteFailed`, the last at `failed_at`): they bound
    the retries like an RSI mark's (`wikiapp.stale.rewrite_due`); a new mark starts at 0."""

    job_id: str
    failures: int = Field(default=0, ge=0)
    failed_at: AwareDatetime | None = None
    # the mark's PageMarkedStale reason: "reorder" (a path order changed) or "harness" (a harness
    # RSI change, `harness.round`); it picks the rewrite's refine trigger and run kind
    reason: str = "reorder"


class PageMarkedStale(_M):
    """A page was written for a path order that has changed since (assistant-actions spec 2026-10-15
    §4): the run `job_id` wrote it after its predecessors were different. It is rewritten when next
    opened (`wikiapp.stale.rewrite_due`) and the mark ends with the next whole-page write."""

    type: Literal["page_marked_stale"] = "page_marked_stale"
    page_id: str
    job_id: str
    reason: str = Field(default="reorder", max_length=200)


class PageMarksCleared(_M):
    """The marks of run `job_id` (`PageMarkedStale`) are void: the change that made them was undone
    or rolled back (a harness change, job "h_<id>"), so its pages need no rewrite. A page whose
    mark is another job's keeps it."""

    type: Literal["page_marks_cleared"] = "page_marks_cleared"
    job_id: str


class SpecVersionMeta(_M):
    """State only: one row per stored spec version (full old specs are read back from the log)."""

    version: int
    at: AwareDatetime
    action: SpecAction
    origin: str | None = None
    n_items: int
    n_exemplars: int


class SectionLogsSaved(_M):
    """Written in the same commit as the `page_saved` that produced the sections."""

    type: Literal["section_logs_saved"] = "section_logs_saved"
    logs: tuple[SectionLog, ...]


class SignalRecorded(_M):
    type: Literal["signal_recorded"] = "signal_recorded"
    signal: Signal


class ReviewFirstAnswered(_M):
    """先复习 / 跳过 on 「建议先复习 X」 (`wikiapp.review_first`): these wrong answers no longer suggest it."""

    type: Literal["review_first_answered"] = "review_first_answered"
    node_id: str
    answer_ids: tuple[str, ...]
    choice: Literal["review", "skip"]


class ReviewCardHidden(_M):
    """A review card the user will not see again (too easy / dropped). `card_id` is
    `wikiapp.review.card_id(page, section, mode)`; state keeps the set."""

    type: Literal["review_card_hidden"] = "review_card_hidden"
    card_id: str
    kind: Literal["too_easy", "drop"]


class ReviewCardRestored(_M):
    """A hidden review card shown again (the undo of the assistant's drop_card)."""

    type: Literal["review_card_restored"] = "review_card_restored"
    card_id: str


class ReviewPostponed(_M):
    """Reviews moved later (assistant `postpone_reviews`): per cell, the time before which it is
    not due; None takes the postponement away again (its undo). Only the due date moves - the
    forgetting model (`goals.review`) is untouched."""

    type: Literal["review_postponed"] = "review_postponed"
    until: dict[str, AwareDatetime | None] = Field(min_length=1)


class Followup(_M):
    """A wrong-answer follow-up (POST /wiki/pages/{id}/checks/{sid}/followup). `source_event_id`
    is the wrong answer's Observe; slip / learned carry their K `Claim` and a confirmation
    question (`item`, a new verified question on the same cell) due at `due`; unclear has neither.
    `teach_*` is the section that teaches the cell (None: no page does)."""

    followup_id: str
    choice: Literal["slip", "learned", "unclear"]
    cell: str
    page_id: str                                  # where the wrong answer was given
    section_id: str
    answer_ref: str                               # the wrong answer's evidence_ref
    source_event_id: str
    claim_event_id: str | None = None
    item: CheckItem | None = None
    due: AwareDatetime | None = None
    teach_page_id: str | None = None
    teach_section_id: str | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _shape(self) -> "Followup":
        claim = self.choice != "unclear"
        if claim != (self.claim_event_id is not None and self.item is not None
                     and self.due is not None):
            raise ValueError("slip / learned need a claim, a confirmation question and a due time; "
                             "unclear has none")
        return self


class FollowupScheduled(_M):
    type: Literal["followup_scheduled"] = "followup_scheduled"
    followup: Followup


class FollowupResolved(_M):
    """The confirmation question was answered: correct → confirmed, else refuted."""

    type: Literal["followup_resolved"] = "followup_resolved"
    followup_id: str
    outcome: Literal["confirmed", "refuted"]
    choice: int


GraphAction = Literal["migrated", "goal_nodes", "split", "undone",
                      "user_edit", "reset", "relayout"]


class GraphSaved(_M):
    """The whole unified node graph (spec 2026-10-13; snapshot, the last one wins). `migrated` / `reset`:
    written by the removed legacy two-tier conversion (still replayed for stored events);
    `relayout`: every node placed again once by the current layout (`wikiapp.graph_layout`).
    `layout`: the `nodes.place.LAYOUT_VERSION` this snapshot's positions were made by when it
    placed every node from scratch (a migration, a relayout); 0 = it kept the earlier positions.
    The state keeps the highest one since the last reset (`WikiState.graph_layout`)."""

    type: Literal["graph_saved"] = "graph_saved"
    graph: NodeGraph
    action: GraphAction
    layout: int = 0
    arranged: tuple[str, ...] = ()         # a relayout: the top groups the map was arranged for (`wikiapp.graph_layout`)


class GroupName(_M):
    """The model's name and one-line summary for an automatic group, keyed by its member set
    (`evowiki.nodes.member_key`), so it survives as long as the members do. `level` and `members`
    (the node ids, sorted) let a later group with nearly the same members inherit the name
    (`wikiapp.graph_out.inherited_name`); 0 / () on names written before they were recorded."""

    key: str
    name: str
    summary: str
    level: int = 0
    members: tuple[str, ...] = ()


class Domain(_M):
    """A broad domain over top groups of the graph (`wikiapp.group_domains`); `kids`: their group ids."""

    id: str
    name: str
    summary: str = ""
    kids: tuple[str, ...]


class DomainsGrouped(_M):
    """The model's domains for the top groups `tops` (`wikiapp.group_domains`; the last one wins)."""

    type: Literal["domains_grouped"] = "domains_grouped"
    tops: tuple[str, ...]
    domains: tuple[Domain, ...] = ()


class GroupsNamed(_M):
    type: Literal["groups_named"] = "groups_named"
    names: tuple[GroupName, ...]


class SeriesUpdated(_M):
    """The page `page_id` (titled `title`) of path `path_id` was written and reported its series
    update; the state merges it onto the path's note (`series.merge`), so the order in which pages
    commit decides nothing but the order of their lines."""

    type: Literal["series_updated"] = "series_updated"
    path_id: str
    page_id: str
    title: str
    update: SeriesUpdate


class GoalSettings(_M):
    """One goal's own settings (assistant-actions §1.3; goal id = its path id, or its map id for a
    big goal): `deadline` - the exam date the review scheduler works towards (goals.review); `archived` - the
    goal was taken back (undo of a new goal), hidden from the goal lists; `skippable` - no longer
    written or read (skill design A7: 可以跳过 is derived from K, `wikiapp.paths.skippable`); kept
    so stored settings still replay."""

    goal_id: str = Field(min_length=1)
    deadline: date | None = None
    archived: bool = False
    skippable: tuple[str, ...] = ()


class GoalSettingsSet(_M):
    """A goal's settings (snapshot; the last one per goal wins)."""

    type: Literal["goal_settings_set"] = "goal_settings_set"
    settings: GoalSettings


class PagesTrashed(_M):
    """Pages moved to the trash (`trashed`; a merge's originals once the merged page is written) or
    put back (`trashed=False`; the undo). The pages stay in `WikiState.pages`; the plugin moves
    their files."""

    type: Literal["pages_trashed"] = "pages_trashed"
    page_ids: tuple[str, ...] = Field(min_length=1)
    trashed: bool = True


class MatchedPage(_M):
    """One EvoWiki page a supplement material covers (`sources.match`)."""

    page_id: str
    title: str
    section_ids: tuple[str, ...]      # the material's sections that cover this page
    why: str = ""                     # one line: what the material adds / says differently here
    # per section_ids entry: where that section starts in the stored document (`sources.locate`
    # JSON), None when unknown; () in events recorded before it
    locators: tuple[dict | None, ...] = ()


class SourceMatched(_M):
    """A supplement material finished importing and was matched against the user's pages
    (`sources.match_flow`, once per source): the pages judged to be about the same concept, most
    relevant first, at most 3; () when none is. The state ignores it; the Bot reads it as a fact."""

    type: Literal["source_matched"] = "source_matched"
    source_id: str
    title: str                        # the material's title
    pages: tuple[MatchedPage, ...]


WikiEventBody = Annotated[
    WikiStarted | ConceptsNamed | DraftCreated | DraftClosed | PageSaved
    | PageOpened | UsageAdded | GuidelineAdded | GuidelineRemoved
    | GuidelineProposed | GuidelineProposalClosed | GuidelineProposalRejected
    | PathSaved | ReviewSettingsSet | LanguageSet
    | TeachingSpecSaved | SectionLogsSaved | SignalRecorded
    | RefineSettingsSet | FeedbackApplied | FeedbackUndone | HandEdited | HandEditUndone
    | ReviewCardHidden
    | ReviewCardRestored | ReviewPostponed
    | FollowupScheduled | FollowupResolved | ProbeAnswered | RoughMarked | GoalMapSaved | PageFitSaved
    | GraphSaved
    | GroupsNamed | DomainsGrouped | PagesStaleSet | PageRewriteFailed | PageMarkedStale | PageMarksCleared
    | SeriesUpdated | GoalSettingsSet
    | PagesTrashed | SourceMatched | RuleDecided | ReadingSaved | ReviewFirstAnswered,
    Field(discriminator="type"),
]


class WikiEvent(_M):
    event_id: str
    ts: AwareDatetime
    body: WikiEventBody
