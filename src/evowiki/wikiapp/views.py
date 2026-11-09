"""Wiki-app state -> `wikiapp.wiki_schemas` output shapes. Structured only (D17/D21): no assembled
labels or sentences; the client formats them."""

import logging
from collections.abc import Iterable
from datetime import datetime

from evowiki.belief.aspects import AspectK, AspectKView
from evowiki.deep.contracts import RefineSettings
from evowiki.goals.contracts import (
    FOLDABLE_ROLES,
    KView,
    OutlineSection,
    SectionRole,
    SectionState,
    SectionText,
    cell_id,
    split_cell,
)
from evowiki.goals.diagnostic import IDK
from evowiki.goals.fold import Page
from evowiki.goals.language import Language
from evowiki.goals.review import ReviewSettings
from evowiki.goals.text import lookups
from evowiki.wiki.catalog import UNNAMED_PAGE, all_known, concept_label
from evowiki.wikiapp.events import DiffNote, PageRecord
from evowiki.wikiapp.goal import DraftResult
from evowiki.wikiapp.kwrite import concept_name, concept_roles, is_page_stale
from evowiki.wikiapp.lineage import check_artifact, section_artifact
from evowiki.wikiapp.page_folder import first_node
from evowiki.wikiapp.page_version import text_fingerprint
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import (
    AnswerResultOut,
    CatalogOut,
    CellBeliefOut,
    CellChangeOut,
    CellOut,
    Changed,
    CheckOut,
    ConflictOut,
    DiffNoteOut,
    DraftConceptOut,
    DraftNotesOut,
    FeedbackMarkOut,
    FileOut,
    GoalDraftOut,
    GoalMapOut,
    GoalNoteOut,
    HandEditMarkOut,
    KConceptOut,
    KGridOut,
    MapCardOut,
    NodeDecisionOut,
    NoteOut,
    PageCardOut,
    PageOut,
    PathCardOut,
    QuestionOut,
    SectionOut,
    SettingsIO,
    ShelfOut,
    WriterOut,
)

log = logging.getLogger(__name__)


def goal_draft_out(res: DraftResult, *, user_id: str = "",
                   map_out: GoalMapOut | None = None) -> GoalDraftOut:
    """An open draft with its last round's questions (answer keys and option meanings of quizzes
    stay server-side; a self-report's meanings are shown), or what it finished as. `map_out`:
    attached by the caller. No draft (entering a node only showed its parts, or reopened a
    point's path: spec 2026-10-28 §2.2): the outcome alone."""
    d = res.draft
    if d is None:
        return GoalDraftOut(draft_id=None, page_id=res.page_id, path_id=res.path_id,
                            map_id=res.map_id, title="", concepts=[], questions=[],
                            notes=DraftNotesOut(from_background=0, reused_concepts=0,
                                                dropped_questions=0), map=map_out)
    names = {c.id: c.name for c in d.concepts}
    if map_out is not None:
        names |= {n.id: n.name for n in map_out.nodes}   # regions are topics, not concepts
    questions: list[QuestionOut] = []
    last = d.rounds[-1] if d.rounds else None
    if res.open:
        assert last is not None
        for i, q in enumerate(last.questions):
            if q.kind == "quiz" and q.cell:
                cid, aspect = split_cell(q.cell)
                cell, means = q.cell, None
            elif q.kind == "quiz":                       # a mastery quiz on a cell-less region
                cid, aspect, cell, means = q.node, None, "", None
            else:
                cid, aspect, cell, means = q.target, None, "", list(q.means)
            questions.append(QuestionOut(
                index=i, kind=q.kind, target=q.target, cell=cell, concept_id=cid,
                concept_name=concept_label(names.get(cid), cid, user_id=user_id),
                aspect=aspect, stem=q.stem, options=list(q.options), option_means=means,
                intent=q.intent or None))
    results = [AnswerResultOut(index=i, kind=q.kind, choice=a,
                               answer=q.answer if q.kind == "quiz" else None,
                               correct=(a == q.answer) if q.kind == "quiz" else None,
                               means=(q.means[a] if q.kind == "self_report" and a != IDK
                                      else None),
                               intent=q.intent or None, node_id=q.node)
               for i, (q, a) in enumerate(res.answered)]
    decisions = [NodeDecisionOut(node_id=x.node, name=x.name, decision=x.decision, p=x.p,
                                 source=x.source)
                 for x in (last.decisions if last is not None else ())]
    return GoalDraftOut(
        draft_id=d.draft_id if res.open else None, page_id=res.page_id, path_id=res.path_id,
        map_id=res.map_id, title=d.title,
        concepts=[DraftConceptOut(id=c.id, name=c.name, target=c.target, reused=c.reused,
                                  aspects=list(c.aspects), prereqs=list(c.prereqs))
                  for c in d.concepts],
        questions=questions,
        notes=DraftNotesOut(from_background=d.from_background, reused_concepts=d.reused,
                            dropped_questions=d.dropped_questions
                            + sum(len(r.dropped) for r in d.rounds)),
        round=len(d.rounds) if res.open else 0,
        round_title=last.title if last is not None else "",
        round_why=last.why if last is not None else "",
        results=results, map=map_out, decisions=decisions)


def cell_out(state: WikiState, k: KView, cell: str, *, user_id: str = "") -> CellOut:
    cid, aspect = split_cell(cell)
    b = k.cell(cell)
    return CellOut(cell=cell, concept_id=cid,
                   concept_name=concept_name(state, cid, user_id=user_id),
                   aspect=aspect, band=b.band, declared=b.declared)


def cell_belief_out(k: KView, cell: str) -> CellBeliefOut:
    """One K cell with its numbers. `inferred_only`: no direct evidence, only inferred."""
    cid, aspect = split_cell(cell)
    b = k.cell(cell)
    return CellBeliefOut(cell=cell, concept_id=cid, aspect=aspect, band=b.band, m=b.m, u=b.u,
                         recall=b.recall, direct=b.direct, inferred_pos=b.inferred_pos,
                         inferred_neg=b.inferred_neg,
                         inferred_only=b.direct == 0 and b.inferred_pos + b.inferred_neg > 0,
                         declared=b.declared)


def cells_out(state: WikiState, k: KView, cells: Iterable[str], *,
              user_id: str = "") -> list[CellOut]:
    return [cell_out(state, k, c, user_id=user_id) for c in cells]


def check_out(section: OutlineSection, answered: int | None) -> CheckOut | None:
    """The embedded question; the key shows only after the user answered. An unverified item
    (stored before verification dropped such sections) has no question and no key."""
    item = section.check
    if item is None or not item.verified:
        return None
    if answered is None:
        return CheckOut(stem=item.stem, options=list(item.options))
    return CheckOut(stem=item.stem, options=list(item.options), answered=answered,
                    correct_index=item.answer,
                    correct=None if answered == IDK else answered == item.answer)


def section_out(state: WikiState, k: KView, section: OutlineSection, st: SectionState,
                text: SectionText | None, *, changed: Changed | None = None,
                answered: int | None = None, user_id: str = "") -> SectionOut:
    """One section as the client sees it; `text` None = not written yet (empty cue / body)."""
    body = text.body if text is not None else ""
    return SectionOut(
        id=section.id, heading=section.heading, role=section.role,
        cue=text.cue if text is not None else "", body=body,
        note_body=text.note_body if text is not None else None,
        folded=st.folded, fold_locked=st.fold_locked,
        foldable=section.role in FOLDABLE_ROLES, pinned=st.pinned, changed=changed,
        why=section.why, cells=cells_out(state, k, section.cells, user_id=user_id), lookups=list(lookups(body)),
        check=check_out(section, answered) if section.role is SectionRole.CHECK else None,
        fingerprint=text_fingerprint(section.heading, body))


def conflicts_out(page: Page | None) -> list[ConflictOut]:
    """The structured twin of `page.unresolved_conflicts` (same source; [] before organizing)."""
    if page is None:
        return []
    return [ConflictOut(kind=c.kind, section_id=c.section_id, cell=c.cell)
            for c in page.conflicts]


def diff_out(diff: DiffNote | None) -> DiffNoteOut | None:
    if diff is None:
        return None
    return DiffNoteOut(
        cell_changes=[CellChangeOut(cell=c.cell, **{"from": c.before}, to=c.after)
                      for c in diff.cell_changes],
        folded=list(diff.folded), unfolded=list(diff.unfolded), patched=list(diff.patched))


def record_section_out(state: WikiState, k: KView, record: PageRecord,
                       sid: str, *, user_id: str = "") -> SectionOut:
    """One section of an organized page, with its stored state, text, change mark and answer."""
    page = record.page
    assert page is not None
    s = next(x for x in page.outline.sections if x.id == sid)
    out = section_out(state, k, s, page.state(sid), page.texts.get(sid),
                      changed=record.changed.get(sid), answered=record.checks.get(sid),
                      user_id=user_id)
    if not user_id:                      # no owner, no artifact ids (they are per user)
        return out
    # the versions shown (0091): the client sends them back with its feedback
    up: dict = {"artifact_id": section_artifact(user_id, page, sid)}
    if out.check is not None and s.check is not None:
        up["check"] = out.check.model_copy(update={
            "artifact_id": check_artifact(user_id, record.page_id, sid, s.check)})
    return out.model_copy(update=up)


def shown(section: OutlineSection) -> bool:
    """False for a CHECK section whose item is unverified: it never reaches the page."""
    return not (section.role is SectionRole.CHECK
                and (section.check is None or not section.check.verified))


def parent_name(state: WikiState, record: PageRecord) -> str:
    """The name of the part_of parent of the page's main node (its first parent that exists), "" when
    it has none: the plugin adds it to a file name that clashes in its folder (「定义（食品添加剂）」)."""
    node = first_node(record)
    if node is None:
        return ""
    for pid in state.graph.parents(node):
        named = state.graph.node(pid)
        if named is not None and named.name.strip():
            return named.name.strip()
    return ""


def _reuse(state: WikiState, record: PageRecord) -> dict:
    """Which goals reuse the page as it is now (a judgement of an older version no longer counts)."""
    from evowiki.wikiapp.page_version import page_text_fingerprint
    version = page_text_fingerprint(record)
    fits = [f for f in state.page_fits.values() if f.page_id == record.page_id and f.verdict != "none"
            and f.page_version == version and f.map_id in state.maps]
    if not fits:
        return {}
    return {"written_for": fits[0].written_for,
            "goal_notes": [GoalNoteOut(map_id=f.map_id, goal=state.maps[f.map_id].title or state.maps[f.map_id].goal, note=f.note)
                           for f in fits]}


def file_out(state: WikiState, record: PageRecord) -> FileOut:
    """The page as its file needs it (`GET /wiki/files`): the same folder / title / parent_name as `page_out`."""
    return FileOut(page_id=record.page_id, title=record.title,
                   folder=record.folder,
                   parent_name=parent_name(state, record),
                   trashed=record.page_id in state.trashed_pages, organized=record.page is not None)


def page_out(state: WikiState, k_view: KView, record: PageRecord, *, calls: int = 0,
             user_id: str = "", budget_exceeded: bool = False) -> PageOut:
    pid = record.page_id
    trashed = pid in state.trashed_pages
    base = {"page_id": pid, "title": record.title, "goal": record.goal, "trashed": trashed,
            "folder": record.folder,
            "parent_name": parent_name(state, record),
            "collapsed": record.shelved,
            "can_shelve": not trashed and shelvable(record, k_view),
            "shelf_line": shelf_line(record) if record.shelved else "", "done": record.done,
            "llm_calls": calls, "budget_exceeded": budget_exceeded, **_reuse(state, record)}
    page = record.page
    if page is None or trashed:
        return PageOut(organized=False, **base)
    o = page.outline
    return PageOut(
        organized=True, source=o.source, profile=o.profile,
        assumed=cells_out(state, k_view, o.assumed, user_id=user_id),
        sections=[record_section_out(state, k_view, record, s.id, user_id=user_id)
                  for s in o.sections if shown(s)],
        diff=diff_out(record.diff), unresolved_conflicts=list(page.unresolved_conflicts),
        conflicts=conflicts_out(page), writer=writer_out(record),
        feedback=feedback_out(record), hand_edit=hand_edit_out(record), **base)


def shelvable(record: PageRecord, k: KView) -> bool:
    """收为参考卡 is offered (spec 2026-10-29 retire-curate §4): the page is organized, not shelved,
    and every cell of every target is 确定会."""
    return record.page is not None and not record.shelved and all_known(k, record.targets)


def shelf_line(record: PageRecord) -> str:
    """The reference card's line: the page's first non-empty recall cue, in outline order, else its
    first written section's heading ("" when none is written). No model call."""
    page = record.page
    if page is None:
        return ""
    written = [s for s in page.outline.sections
               if s.id in page.texts and s.role is not SectionRole.CHECK]
    return next((c for s in written if (c := page.texts[s.id].cue.strip())),
                written[0].heading if written else "")


def feedback_out(record: PageRecord) -> FeedbackMarkOut | None:
    """The feedback mark while its text is still on the page (any of its sections keeps the
    feedback's cache key); `can_undo` while nothing else rewrote those sections."""
    m = record.feedback
    if m is None or record.page is None:
        return None
    texts = record.page.texts
    live = [s for s in m.sections
            if (t := texts.get(s)) is not None and t.cache_key == f"feedback:{m.feedback_id}:{s}"]
    if not live:
        return None
    return FeedbackMarkOut(feedback_id=m.feedback_id, at=m.at, summary=m.summary,
                           reading=m.reading, sections=live, assets=list(m.assets),
                           can_undo=len(live) == len(m.sections))


def hand_edit_out(record: PageRecord) -> HandEditMarkOut | None:
    """The hand-edit mark while its text is still on the page (the sections keep its cache key);
    `can_undo` while nothing else rewrote those sections."""
    m = record.hand_edit
    if m is None or record.page is None:
        return None
    texts = record.page.texts
    live = [s for s in m.sections
            if (t := texts.get(s)) is not None and t.cache_key == f"hand:{m.edit_id}:{s}"]
    if not live and not m.removed:
        return None
    heading = {s.id: s.heading for s in record.page.outline.sections}
    parts = "、".join(f"「{heading[s]}」" for s in live if s in heading)
    gone = "、".join(f"「{h}」" for h in m.removed)
    summary = "；".join(x for x in (f"已写入你的改动：{parts}" if parts else "",
                                   f"已删除{gone}" if gone else "") if x)
    return HandEditMarkOut(edit_id=m.edit_id, at=m.at, summary=summary, sections=live,
                           can_undo=len(live) == len(m.sections))


def writer_out(record: PageRecord) -> WriterOut | None:
    """Who wrote the page's current text (`PageRecord.written`); None for a page written before the
    single writer (its legacy `refined` mark is not shown any more), and while a run is writing
    the page (its mark is an earlier text's)."""
    m = record.written
    if m is None or not record.has_body:
        return None
    return WriterOut(model=m.model, at=m.at, cost_usd=m.cost_usd, fallback_reason=m.fallback_reason)


# ---- catalog, K grid (Task 10) ----

NOTE_KINDS = {"goal": "self", "quiz": "quiz", "check": "check", "feedback": "feedback",
              "ask": "ask", "declare": "declare", "probe": "quiz", "probeself": "self",
              "review": "review"}                         # evidence_ref prefix -> NoteOut.kind


def page_title(state: WikiState, page_id: str, *, user_id: str = "") -> str:
    """What a user sees for a page: its title, else 「未命名页面」 (logged; never the raw id)."""
    rec = state.pages.get(page_id)
    if rec is not None and rec.title:
        return rec.title
    log.warning("page without a title: user_id=%s page_id=%s", user_id, page_id)
    return UNNAMED_PAGE


def names_out(state: WikiState, *, pages: Iterable[str] = (), user_id: str = "") -> dict[str, str]:
    """`names` for the catalog: every page id -> its title (「未命名页面」 when it has none, logged)."""
    return {p: page_title(state, p, user_id=user_id) for p in dict.fromkeys(pages)}


def settings_io(review: ReviewSettings, language: Language = "zh",
                refine: RefineSettings | None = None) -> SettingsIO:
    return SettingsIO(review_per_day=review.max_per_day, language=language,
                      page_limit_usd=(refine or RefineSettings()).per_page_limit_usd)


def page_card_out(state: WikiState, k: AspectK, record: PageRecord,
                  now: datetime, *, user_id: str = "") -> PageCardOut:
    return PageCardOut(page_id=record.page_id, title=record.title, goal=record.goal,
                       stale=is_page_stale(state, k, record, now, user_id=user_id),
                       done=record.done,
                       organized=record.page is not None)


def catalog_out(state: WikiState, k: AspectK, now: datetime, *, user_id: str = "") -> CatalogOut:
    """GET /wiki (0 calls). 在学 = active pages (U2); 参考书架 = the shelved pages."""
    order = [p for p in state.page_order if p not in state.trashed_pages]    # the trash is not listed
    active = set(state.active_pages())
    cards = {p: page_card_out(state, k, state.pages[p], now, user_id=user_id) for p in order}
    return CatalogOut(
        learning=[cards[p] for p in order if p in active],
        shelf=ShelfOut(pages=[cards[p] for p in order if p not in active]),
        settings=settings_io(state.review, state.language, state.refine),
        current_page=state.current_page,
        names=names_out(state, pages=[*order, *([state.current_page] if state.current_page
                                                and state.current_page not in state.trashed_pages
                                                else [])], user_id=user_id),
        paths=_path_cards(state, k, now), maps=_map_cards(state, k, now))


def _map_cards(state: WikiState, k: AspectK, now: datetime) -> list[MapCardOut]:
    from evowiki.wikiapp.maps import map_cards  # maps imports views

    return map_cards(state, k, now)


def _path_cards(state: WikiState, k: AspectK, now: datetime) -> list[PathCardOut]:
    from evowiki.wikiapp.paths import path_cards  # paths imports views

    return path_cards(state, k, now)


def k_grid_out(state: WikiState, k: AspectK, now: datetime, *, user_id: str = "") -> KGridOut:
    """GET /wiki/k: every concept defined in K, in definition order. A note whose evidence_ref
    has an unknown prefix is left out (its kind cannot be told)."""
    kview = AspectKView(k, now)
    roles = concept_roles(state)
    rec = state.pages.get(state.current_page) if state.current_page is not None else None
    on_page = set(rec.concepts) if rec is not None else set()
    out = []
    for c in k.concept_ids():
        aspects = k.aspects(c)
        notes = [NoteOut(entry_id=n.entry_id, cell=n.cell, kind=kind, text=n.text,
                         evidence_ref=n.evidence_ref, label=n.label)
                 for n in k.notes(c)
                 if (kind := NOTE_KINDS.get(n.evidence_ref.partition(":")[0])) is not None]
        out.append(KConceptOut(id=c, name=concept_name(state, c, user_id=user_id), role=roles.get(c),
                               aspects=list(aspects), prereqs=list(k.prereqs(c)),
                               on_current_page=c in on_page,
                               cells=[cell_belief_out(kview, cell_id(c, a)) for a in aspects],
                               notes=notes))
    return KGridOut(concepts=out)
