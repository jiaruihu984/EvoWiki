"""Replayable wiki state: a pure reducer over `WikiEvent`s. Applying events one by one at runtime
and `replay` over the stored log give byte-identical `to_json()` (D1)."""

import json
from collections.abc import Iterable
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic_core import to_jsonable_python

from evowiki.deep.contracts import RefineSettings
from evowiki.goals.language import Language
from evowiki.goals.page_fit import PageFit
from evowiki.goals.reading import Reading
from evowiki.goals.regions import GoalMap
from evowiki.goals.review import ReviewSettings
from evowiki.nodes.model import NodeGraph
from evowiki.teach.contracts import SectionLog, TeachingSpec
from evowiki.wikiapp.events import (
    WHOLE_PAGE_WRITES,
    ConceptName,
    ConceptsNamed,
    Domain,
    DomainsGrouped,
    Draft,
    DraftClosed,
    DraftCreated,
    FeedbackApplied,
    FeedbackUndone,
    Followup,
    FollowupResolved,
    FollowupScheduled,
    GoalMapSaved,
    GoalSettings,
    GoalSettingsSet,
    GraphSaved,
    GroupName,
    GroupsNamed,
    Guideline,
    GuidelineAdded,
    GuidelineProposal,
    GuidelineProposalClosed,
    GuidelineProposalRejected,
    GuidelineProposed,
    GuidelineRemoved,
    HandEdited,
    HandEditUndone,
    LanguageSet,
    PageFitSaved,
    PageMarkedStale,
    PageMarksCleared,
    PageOpened,
    PageRecord,
    PageRewriteFailed,
    PageSaved,
    PagesStaleSet,
    PagesTrashed,
    PatchFailure,
    PathRecord,
    PathSaved,
    ProbeAnswered,
    ReadingSaved,
    RefineSettingsSet,
    ReorderMark,
    ReviewCardHidden,
    ReviewCardRestored,
    ReviewFirstAnswered,
    ReviewPostponed,
    ReviewSettingsSet,
    RoughMarked,
    RuleDecided,
    SectionLogsSaved,
    SeriesUpdated,
    SignalRecorded,
    SourceMatched,
    SpecVersionMeta,
    StaleMark,
    TeachingSpecSaved,
    UsageAdded,
    UsageRecord,
    WikiEvent,
    WikiStarted,
)
from evowiki.wikiapp.series import SeriesNote
from evowiki.wikiapp.series import merge as merge_series

ASPECT_CONFIG_VERSION = "aspect-config@v1"
USAGE_KEEP = 50
CARD_FEEDBACK_USAGE = "复习卡反馈"     # usage action derived from a `card_feedback` signal


def _card_feedback_usage(s: "WikiState", sig) -> UsageRecord:
    """Usage record 「复习卡反馈」 for a card_feedback signal: page id, the section heading when
    the page and section are known (else None), the signal text (kind label + the words)."""
    rec = s.pages.get(sig.page_id) if sig.page_id else None
    heading = None
    if rec is not None and rec.page is not None and sig.section_id is not None:
        heading = next((x.heading for x in rec.page.outline.sections if x.id == sig.section_id),
                       None)
    return UsageRecord(action=CARD_FEEDBACK_USAGE, page=sig.page_id, heading=heading,
                       text=sig.text.strip())


class ReplayMismatch(ValueError):
    """The stored wiki log was written under a different aspect config (D12: fail closed)."""


def _canon(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _canon(v) for k, v in x.items()}
    if isinstance(x, (set, frozenset)):
        items = [_canon(v) for v in x]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False,
                                                      default=to_jsonable_python))
    if isinstance(x, (list, tuple)):
        return [_canon(v) for v in x]
    return x


class WikiState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    started: bool = False
    names: dict[str, ConceptName] = Field(default_factory=dict)
    drafts: dict[str, Draft] = Field(default_factory=dict)
    closed_drafts: frozenset[str] = frozenset()
    pages: dict[str, PageRecord] = Field(default_factory=dict)
    page_order: tuple[str, ...] = ()
    current_page: str | None = None
    opened_at: dict[str, AwareDatetime] = Field(default_factory=dict)
    usage: tuple[UsageRecord, ...] = ()                            # last USAGE_KEEP kept
    guidelines: tuple[Guideline, ...] = ()
    guideline_proposals: dict[str, GuidelineProposal] = Field(default_factory=dict)  # open ones
    rejected_guidelines: tuple[str, ...] = ()
    # rule provenance contract: the user's rule decisions in order (`rules.decisions` reads them)
    rule_decisions: tuple[RuleDecided, ...] = ()
    paths: dict[str, PathRecord] = Field(default_factory=dict)
    page_fits: dict[str, PageFit] = Field(default_factory=dict)  # map:node:page
    path_order: tuple[str, ...] = ()
    series: dict[str, SeriesNote] = Field(default_factory=dict)   # path id -> series note (B2)
    review: ReviewSettings = ReviewSettings()
    language: Language = "zh"
    # teaching spec θ_rep,u (R1). `spec` None = never saved: the effective spec is the virtual v1
    # (`wikiapp.teachspec.effective_spec`). Full old versions live in the event log only.
    spec: TeachingSpec | None = None
    spec_versions: tuple[SpecVersionMeta, ...] = ()
    item_evidence: dict[str, str] = Field(default_factory=dict)   # item id -> evidence text
    section_logs: dict[str, dict[str, SectionLog]] = Field(default_factory=dict)  # latest per section
    signals_recorded: int = 0                                     # count only; signals stay in the log
    feedbacks: dict[str, FeedbackApplied] = Field(default_factory=dict)  # page-feedback patches (B1)
    feedbacks_undone: frozenset[str] = frozenset()
    hand_edits: dict[str, HandEdited] = Field(default_factory=dict)      # hand edits in notes
    hand_edits_undone: frozenset[str] = frozenset()
    refine: RefineSettings = RefineSettings()
    hidden_cards: frozenset[str] = frozenset()                    # review cards never shown again
    review_first_answered: frozenset[str] = frozenset()           # wrong answers 先复习 / 跳过 closed
    review_postponed: dict[str, AwareDatetime] = Field(default_factory=dict)  # cell -> not due before
    followups: dict[str, Followup] = Field(default_factory=dict)  # wrong-answer follow-ups, all
    followup_outcomes: dict[str, str] = Field(default_factory=dict)   # id -> confirmed / refuted
    maps: dict[str, GoalMap] = Field(default_factory=dict)        # big-goal maps by map id
    map_order: tuple[str, ...] = ()
    readings: dict[str, Reading] = Field(default_factory=dict)    # 读懂 goals' readings by id
    # the unified node graph (spec 2026-10-13)
    graph: NodeGraph = NodeGraph()
    graph_layout: int = 0       # the layout version of the graph's positions (GraphSaved.layout)
    domains: tuple[Domain, ...] = ()          # the model's broad domains (`group_domains`) and the top groups they were made for
    domains_for: tuple[str, ...] = ()
    graph_arranged: tuple[str, ...] = ()      # the top groups the map was last arranged for (GraphSaved.arranged)
    groups: dict[str, GroupName] = Field(default_factory=dict)    # member key -> latest name
    # pages an RSI (track B) change left out of date (`wikiapp.stale`), and the teaching-spec version
    # up to which RSI changes were compared
    stale_marks: dict[str, StaleMark] = Field(default_factory=dict)
    # page id -> the write run whose text is out of date for the path order (`PageMarkedStale`)
    reorder_marks: dict[str, ReorderMark] = Field(default_factory=dict)
    # page id -> failed scoped writes of its patched sections (`wikiapp.stale.patch_due`)
    patch_failures: dict[str, PatchFailure] = Field(default_factory=dict)
    stale_checked_version: int = 0
    goal_settings: dict[str, GoalSettings] = Field(default_factory=dict)   # goal id -> settings
    # points a goal's map edit took off the map, until a map has them again (`graph_out`: left out of the picture while nothing uses them)
    dropped_points: frozenset[str] = frozenset()
    trashed_pages: frozenset[str] = frozenset()   # pages in the trash (still in `pages`)

    def open_followups(self) -> list[Followup]:
        """slip / learned follow-ups whose confirmation question is still unanswered."""
        return [f for f in self.followups.values()
                if f.item is not None and f.followup_id not in self.followup_outcomes]

    def reading_of(self, map_id: str) -> Reading | None:
        """The reading of goal `map_id` (a 读懂 goal has one; reading-mode spec §2.1), else None."""
        return next((r for r in self.readings.values() if r.map_id == map_id), None)

    def to_json(self) -> str:
        """Canonical JSON (sorted keys, sets sorted) for replay equality."""
        return json.dumps(_canon(self.model_dump(mode="python")), sort_keys=True,
                          ensure_ascii=False, separators=(",", ":"), default=to_jsonable_python)

    def active_pages(self) -> tuple[str, ...]:
        """Pages not shelved as a reference card (U2); pages marked done stay active."""
        return tuple(p for p in self.page_order if not self.pages[p].shelved)

    def principles(self) -> tuple[str, ...]:
        """The user's own principle texts: the old guidelines, or once a teaching spec is stored,
        its items that are not default / seed / import ones, without a domain (the texts `/wiki/guidelines` lists). Generation,
        the curator and the assistant do NOT read this (they read the teaching spec:
        `wikiapp.teachspec.page_teach` / `wiki_texts`); never imports `wikiapp.teachspec` (cycle)."""
        if self.spec is not None:
            return tuple(i.text for i in self.spec.items
                         if i.origin not in ("default", "seed") and i.domain is None)
        return tuple(g.text for g in self.guidelines)

    def section_log(self, page_id: str, section_id: str | None = None) -> SectionLog | None:
        """The latest log of that section; else (or with no section) the page's most recent log
        (latest `at`, ties by section id); None when the page has none."""
        logs = self.section_logs.get(page_id, {})
        if section_id is not None and section_id in logs:
            return logs[section_id]
        return max(logs.values(), key=lambda g: (g.at, g.section_id), default=None)


def _without(d: dict, key: str) -> dict:
    return {k: v for k, v in d.items() if k != key}


def _map_points(m: GoalMap) -> frozenset[str]:
    return frozenset(i for n in m.nodes for i in (n.id, *n.members))


def apply_event(s: WikiState, ev: WikiEvent) -> WikiState:
    """Pure: returns a new state; `s` is left untouched."""
    b = ev.body
    match b:
        case WikiStarted():
            if b.aspect_config != ASPECT_CONFIG_VERSION:
                raise ReplayMismatch(
                    f"wiki log aspect config {b.aspect_config!r} != {ASPECT_CONFIG_VERSION!r}")
            up = {"started": True}
        case ConceptsNamed():
            up = {"names": {**s.names, **{n.concept_id: n for n in b.names}}}
        case DraftCreated():
            up = {"drafts": {**s.drafts, b.draft.draft_id: b.draft}}
        case DraftClosed():
            up = {"closed_drafts": s.closed_drafts | {b.draft_id}}
        case PageSaved():
            pid = b.record.page_id
            up = {"pages": {**s.pages, pid: b.record}}
            if pid not in s.pages:
                up["page_order"] = (*s.page_order, pid)
            w = b.record.written
            old = s.reorder_marks.get(pid)
            if (old is not None and w is not None and w.kind in WHOLE_PAGE_WRITES
                    and w.job_id != old.job_id):          # a new whole-page write ends it
                up["reorder_marks"] = {p: j for p, j in s.reorder_marks.items() if p != pid}
            if b.action == "created":
                up["current_page"] = pid
        case FeedbackApplied():
            up = {"feedbacks": {**s.feedbacks, b.feedback_id: b}}
        case FeedbackUndone():
            up = {"feedbacks_undone": s.feedbacks_undone | {b.feedback_id}}
        case HandEdited():
            up = {"hand_edits": {**s.hand_edits, b.edit_id: b}}
        case HandEditUndone():
            up = {"hand_edits_undone": s.hand_edits_undone | {b.edit_id}}
        case PageOpened():
            up = {"current_page": b.page_id, "opened_at": {**s.opened_at, b.page_id: ev.ts}}
        case UsageAdded():
            up = {"usage": (*s.usage, b.record)[-USAGE_KEEP:]}
        case GuidelineAdded():
            up = {"guidelines": (*s.guidelines, b.guideline)}
        case GuidelineRemoved():
            up = {"guidelines": tuple(g for g in s.guidelines if g.guideline_id != b.guideline_id)}
        case GuidelineProposed():
            pid = b.proposal.proposal_id
            up = {"guideline_proposals": {**_without(s.guideline_proposals, pid), pid: b.proposal}}
        case GuidelineProposalClosed():
            up = {"guideline_proposals": _without(s.guideline_proposals, b.proposal_id)}
        case GuidelineProposalRejected():
            p = s.guideline_proposals.get(b.proposal_id)
            up = {"guideline_proposals": _without(s.guideline_proposals, b.proposal_id)}
            if p is not None:
                up["rejected_guidelines"] = (*s.rejected_guidelines, p.text)
        case PathSaved():
            pid = b.record.path.path_id
            up = {"paths": {**s.paths, pid: b.record}}
            if pid not in s.paths:
                up["path_order"] = (*s.path_order, pid)
        case SeriesUpdated():
            note = merge_series(s.series.get(b.path_id), b.update, page_id=b.page_id,
                                title=b.title)
            up = {"series": {**s.series, b.path_id: note}}
        case LanguageSet():
            up = {"language": b.language}
        case ReviewSettingsSet():
            up = {"review": b.settings}
        case RefineSettingsSet():
            up = {"refine": b.settings}
        case TeachingSpecSaved():
            meta = SpecVersionMeta(version=b.spec.version, at=ev.ts, action=b.action,
                                   origin=b.origin, n_items=len(b.spec.items),
                                   n_exemplars=len(b.spec.exemplars))
            up = {"spec": b.spec, "spec_versions": (*s.spec_versions, meta)}
            if b.evidence:
                up["item_evidence"] = {**s.item_evidence, **b.evidence}
        case RuleDecided():
            up = {"rule_decisions": (*s.rule_decisions, b)}
        case SectionLogsSaved():
            logs = {p: dict(d) for p, d in s.section_logs.items()}
            for g in b.logs:
                logs.setdefault(g.page_id, {})[g.section_id] = g
            up = {"section_logs": logs}
        case SignalRecorded():
            up = {"signals_recorded": s.signals_recorded + 1}
            if b.signal.kind == "card_feedback" and b.signal.text.strip():
                # the review card's 这张卡有问题… words are the user's own feedback: they join
                # the usage records (guideline proposal, curator) like page feedback does. Derived
                # at replay, so old logs gain the record too; no new event type.
                up["usage"] = (*s.usage, _card_feedback_usage(s, b.signal))[-USAGE_KEEP:]
        case ReviewFirstAnswered():
            up = {"review_first_answered": s.review_first_answered | set(b.answer_ids)}
        case ReviewCardHidden():
            up = {"hidden_cards": s.hidden_cards | {b.card_id}}
        case ReviewCardRestored():
            up = {"hidden_cards": s.hidden_cards - {b.card_id}}
        case ReviewPostponed():
            post = {c: u for c, u in s.review_postponed.items() if c not in b.until}
            post.update({c: u for c, u in b.until.items() if u is not None})
            up = {"review_postponed": post}
        case FollowupScheduled():
            up = {"followups": {**s.followups, b.followup.followup_id: b.followup}}
        case FollowupResolved():
            up = {"followup_outcomes": {**s.followup_outcomes, b.followup_id: b.outcome}}
        case ProbeAnswered():
            d = s.drafts[b.draft_id]
            rounds = list(d.rounds)
            rounds[b.round_index - 1] = rounds[b.round_index - 1].model_copy(
                update={"answers": b.answers,
                        **({"questions": b.questions} if b.questions is not None else {})})
            if b.next_round is not None:
                rounds.append(b.next_round)
            up = {"drafts": {**s.drafts, b.draft_id: d.model_copy(update={"rounds": tuple(rounds)})}}
        case RoughMarked():
            d = s.drafts[b.draft_id]
            up = {"drafts": {**s.drafts, b.draft_id: d.model_copy(update={
                "rough": {**d.rough, **b.marks}, "rough_calls": (*d.rough_calls, b.call_id),
                "rounds": (*d.rounds[:-1], b.next_round)})}}
        case GoalMapSaved():
            mid = b.record.map_id
            on = _map_points(b.record)
            gone = _map_points(s.maps[mid]) - on if mid in s.maps else frozenset()
            up = {"maps": {**s.maps, mid: b.record}, "dropped_points": (s.dropped_points | gone) - on}
            if mid not in s.maps:
                up["map_order"] = (*s.map_order, mid)
        case PageFitSaved():
            f = b.record
            up = {"page_fits": {**s.page_fits, f"{f.map_id}:{f.node_id}:{f.page_id}": f}}
        case ReadingSaved():
            up = {"readings": {**s.readings, b.record.reading_id: b.record}}
        case GoalSettingsSet():
            up = {"goal_settings": {**s.goal_settings, b.settings.goal_id: b.settings}}
        case PagesTrashed():
            ids = frozenset(b.page_ids)
            # a trashed page is unread: when it is written again it counts as not opened
            up = {"trashed_pages": s.trashed_pages | ids if b.trashed else s.trashed_pages - ids,
                  "opened_at": {k: v for k, v in s.opened_at.items() if k not in ids}
                  if b.trashed else s.opened_at}
        case SourceMatched():   # a fact for the Bot (`bot.facts`); nothing in the state
            return s
        case GraphSaved():
            up = {"graph": b.graph,
                  "graph_layout": 0 if b.action == "reset" else max(s.graph_layout, b.layout),
                  "graph_arranged": b.arranged if b.action == "relayout" else s.graph_arranged}
        case DomainsGrouped():
            up = {"domains": b.domains, "domains_for": b.tops}
        case GroupsNamed():
            up = {"groups": {**s.groups, **{g.key: g for g in b.names}}}
        case PagesStaleSet():
            marks = {p: m for p, m in s.stale_marks.items() if p not in b.cleared}
            up = {"stale_marks": {**marks, **b.marked},
                  "stale_checked_version": max(s.stale_checked_version, b.checked_version)}
        case PageMarkedStale():
            up = {"reorder_marks": {**s.reorder_marks, b.page_id: ReorderMark(job_id=b.job_id,
                                                                      reason=b.reason)}}
        case PageMarksCleared():
            up = {"reorder_marks": {p: m for p, m in s.reorder_marks.items()
                                    if m.job_id != b.job_id}}
        case PageRewriteFailed() if b.kind == "patch":
            f = s.patch_failures.get(b.page_id)
            n = f.failures + 1 if f is not None and f.sections == b.sections else 1
            up = {"patch_failures": {**s.patch_failures, b.page_id: PatchFailure(
                sections=b.sections, failures=n, failed_at=ev.ts)}}
        case PageRewriteFailed():
            m = s.stale_marks.get(b.page_id) if b.kind == "rsi" else None
            r = s.reorder_marks.get(b.page_id) if b.kind == "reorder" else None
            if m is None and r is None:
                return s
            up = {}
            if m is not None:
                n = m.failures + 1 if m.failed_version == b.spec_version else 1
                m = m.model_copy(update={"failures": n, "failed_at": ev.ts,
                                         "failed_version": b.spec_version})
                up["stale_marks"] = {**s.stale_marks, b.page_id: m}
            if r is not None:
                r = r.model_copy(update={"failures": r.failures + 1, "failed_at": ev.ts})
                up["reorder_marks"] = {**s.reorder_marks, b.page_id: r}
        case _:  # pragma: no cover - the discriminated union is exhaustive
            raise TypeError(f"unknown wiki event body {type(b).__name__}")
    return s.model_copy(update=up)


def replay(events: Iterable[WikiEvent]) -> WikiState:
    s = WikiState()
    for ev in events:
        s = apply_event(s, ev)
    return s
