"""Confirming a 读懂 goal (reading-mode spec §3.4): `create_reading_goal` writes the goal S1's
understand mode planned (`BlocksResult.blocks` = the prerequisites, `BlocksResult.reading` = the
units) in ONE commit, with no model call except the opening probe round over the prerequisites.

- The prerequisite blocks go through the same code as a learning goal's blocks
  (`goal.block_goal`: graph nodes and K concepts, `ConceptName` with `atomic`, the rename rules,
  his words about a block as a self-report, `GoalMap.words/limits/beginner`); the map holds only
  them, all top-level, `sources=(source_id,)`. No reading block, no 「《T》§…」 node: a unit is never
  a node (§2.3 不变量).
- `ReadingSaved(planned)`: the units as planned (structure only, `analyzed=False`).
- The opening diagnosis (Q7) asks about the prerequisites only (a block not to diagnose never);
  no prerequisites → a map with no nodes and no probe. No path is planned (§3.5: entering a unit
  does that).
- The same goal again (same text, same material: one map id, `reading_map_id`): as
  `create_goal_from_blocks` — a map nobody started is replaced (the reading keeps its id), one
  with progress (a started unit counts) is WikiConflict GOAL_IN_PROGRESS.
- The material's use row (`source_use(use='understand', goal_id=map_id)`) is written in the goal's
  commit by `understand_use` through the same store function `goal_sources._record` uses
  (`sources.uses.record_use`); the import it needs starts after the commit
  (`create_reading_goal_with_source`). No `source_link` yet: `teaches` is known only once a unit
  is entered.

Entering a unit (§3.5, §4):
- `analyze_unit` 「看这一段」: a unit not analyzed gets ONE call (goals/reading_unit.py; asked once
  more when code cannot use the answer, then IntakeError and nothing written); an analyzed one
  none. One commit: its teaches and the new prerequisites into the shared graph and K (an
  existing node only by the id the model wrote, a new name by `goal._block_ids`' same-name
  rules; ConceptName with the model's `atomic` for a new node, a reused one keeps its own; the
  uses as prerequisites of the teaches: `goal._goal_graph`, whose `add_relations` drops a
  self-loop or cycle), every use not on the map onto its top level (GoalMapSaved edited; the
  teaches never, nor a use another unit of this reading teaches: it stays in `unit.uses` only),
  ReadingSaved(unit_analyzed), and through `on_commit` (`teaches_links`) a
  `source_link(role='teaches', by='reading')` from each teaches node to each section the unit
  reads. A unit is never a node (§2.3 不变量).
- `start_unit_page`: no model call. The reading's one path (`reading_path_id`: one PathRecord per
  reading, its pages in unit order, keyed by unit id) gets the unit's PathPage(kind="reading",
  targets = teaches, concepts = teaches + uses); its PageRecord (kind / unit_id / reading_id)
  comes from `goal.page_events_for` (targets 在学); ReadingSaved(page_started) puts the page on
  the unit. Refused before the unit is analyzed or when it is skipped; a started unit hands its
  page back.
- `add_unit_use`: a prerequisite found while reading (an existing or a new node) joins the unit's
  uses and the map's top level (ReadingSaved uses_added + GoalMapSaved edited); no other map.
- `reanchor_readings`: a material's section text changed (re-OCR, `importer.swap_reads`): every
  reading of it re-anchored (goals/reading.reanchor), ReadingSaved(edited); fail closed
  (ReanchorFailed) when the result no longer fits the material.

First version's limit (§2.3): which unit owns a point (`teaches`) depends on the order the units
are opened in — the first one opened that explains it teaches it, later ones use it."""

import dataclasses
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg_pool import ConnectionPool

from evowiki.belief.aspects import AspectKView
from evowiki.goals import reading_unit
from evowiki.goals.blocks import Block
from evowiki.goals.contracts import cell_id
from evowiki.goals.guardrails import PageConcept
from evowiki.goals.intake import (
    DEFAULT_ASPECTS,
    IntakeConcept,
    IntakeError,
    IntakeResult,
    SourceToc,
)
from evowiki.goals.path import LEARNED_BANDS, LearningPath, PageStatus, PathPage, goal_key
from evowiki.goals.reading import (
    Reading,
    ReadingPlan,
    ReadingRefused,
    ReadingUnit,
    anchor_of,
    check_units,
    reanchor,
)
from evowiki.goals.reading_unit import UnitAnalysis, UnitInput, UnitPoint, UnitRow
from evowiki.goals.regions import GoalMap, MapNode
from evowiki.llm.run_context import traced_entry
from evowiki.sources import store as source_store
from evowiki.sources import uses as material_uses
from evowiki.sources.excerpts import PooledSourceExcerpts, SourceExcerpt
from evowiki.sources.importer import ImportJob
from evowiki.sources.labels import source_toc
from evowiki.sources.models import LinkRow
from evowiki.sources.runs import SourceRuns
from evowiki.wikiapp.events import GoalMapSaved, PathRecord, PathSaved, ReadingSaved
from evowiki.wikiapp.goal import (
    DraftResult,
    Ids,
    _annotations,
    _block_ids,
    _decision_log,
    _goal_graph,
    _graph_relations,
    _intake_events,
    _open_or_finish,
    _prior_region,
    _probe,
    _topics,
    _with_result,
    _Work,
    block_goal,
    page_events_for,
)
from evowiki.wikiapp.goal_sources import (
    MSG_LINKS_NOT_SAVED,
    MSG_SOURCE_NOT_FOUND,
    MSG_SOURCE_READING,
    GoalSourceError,
    load_materials,
)
from evowiki.wikiapp.kwrite import concept_name, existing_refs
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.page_folder import goal_folder
from evowiki.wikiapp.reading_view import reading_path_id
from evowiki.wikiapp.repo import Repo, WikiInvalid
from evowiki.wikiapp.state import WikiState

log = logging.getLogger(__name__)

MSG_PLAN_OTHER_SOURCE = "排好的读法和这份资料对不上，请重新排一次"
MSG_PLAN_STALE = "这份资料的内容变了，排好的读法用不了了，请重新排一次"
MSG_PLAN_NOT_FRESH = "排好的读法里有已经开始读的段落，请重新排一次"


def reading_map_id(goal: str, source_id: str) -> str:
    """A 读懂 goal's map id: its text and its one material (never its prerequisites, which change
    from one plan to the next: the same paper read again is the same goal)."""
    return f"map-{goal_key(goal, (source_id,))}"


def _check_plan(plan: ReadingPlan, source: SourceToc) -> None:
    """Fail closed before anything is written: the plan is for this material, its units are
    structure only (nothing analyzed or started: a confirmed plan is a fresh reading), the
    structure still fits the material's sections as they are now (`check_units`, §2.2) and each
    split span still starts on the paragraph its anchor was taken from (the text changed since
    the draft: replan)."""
    if plan.source_id != source.source_id:
        raise WikiInvalid(MSG_PLAN_OTHER_SOURCE)
    if any(u.analyzed or u.page_id is not None or u.teaches or u.uses or u.focus
           for u in plan.units):
        raise WikiInvalid(MSG_PLAN_NOT_FRESH)
    counts = {s.id: len(s.paras) for s in source.sections if s.paras}
    problems = check_units(plan.units, source.sections, counts)
    paras = {s.id: s.paras for s in source.sections}
    moved = [f"{sp.section_id}:{sp.para_from}" for u in plan.units for sp in u.spans
             if not problems and not sp.whole and sp.anchor and sp.para_from is not None
             and anchor_of(paras[sp.section_id][sp.para_from]) != sp.anchor]
    if problems or moved:          # a paragraph range on text that changed since the draft
        log.info("reading plan for %s no longer fits: %s %s", source.source_id, problems, moved)
        raise WikiInvalid(MSG_PLAN_STALE)


@traced_entry("understand", subject="goal")
def create_reading_goal(repo: Repo, gw: CountingGateway | None, user_id: str, goal: str,
                        blocks: Sequence[Block], reading_plan: ReadingPlan, *, now: datetime,
                        new_id: Ids, source: SourceToc, words: Sequence[str] = (),
                        limits: Sequence[str] = (), beginner: str = "", title: str = "",
                        extra: Sequence[Callable[[Any], None]] = (),
                        on_commit: Callable[[Any, DraftResult], None] | None = None
                        ) -> DraftResult:
    """A 读懂 goal from S1's understand mode (module doc): the prerequisite `blocks`, the
    `reading_plan` over the one material `source` (its toc as it is now), the goal record
    (`words`, `limits`, `beginner`, `title`), in one commit with `extra` and `on_commit(conn,
    result)` (`understand_use` records the material's use). The result: `map_id` and
    `reading_id`, or `open` with the opening draft when the probe asked something.
    WikiInvalid: a plan for another material, not fresh, or that no longer fits the material;
    two blocks that are one node. WikiConflict GOAL_IN_PROGRESS: this reading has begun."""
    _check_plan(reading_plan, source)
    map_id = reading_map_id(goal, source.source_id)
    p = block_goal(repo, gw, user_id, goal, blocks, now=now, new_id=new_id, words=words,
                   limits=limits, beginner=beginner, title=title, sources=(source,),
                   source_use="understand", map_id=map_id)
    w, m, draft = p.w, p.map, p.draft
    old = w.snap.state.reading_of(map_id)     # the same goal replaced: the reading keeps its id
    reading = Reading(units=reading_plan.units,
                      reading_id=old.reading_id if old is not None else new_id("rdg"),
                      map_id=map_id, source_id=source.source_id, title=reading_plan.title,
                      left_out=reading_plan.left_out, created_at=now)
    r = _probe(w, draft, m, ()) if m.nodes else None   # only the prerequisites are asked
    if r is not None:
        m = _decision_log(m, r)
    w.add(GoalMapSaved(record=m, action="planned"))
    w.add(ReadingSaved(record=reading, action="planned"))
    out = (_open_or_finish(w, draft, m, r) if r is not None
           else DraftResult(draft=draft, page_id=None, calls=w.calls, map_id=map_id))
    out = dataclasses.replace(out, reading_id=reading.reading_id)
    w.commit(repo, _with_result(extra, on_commit, out))
    return out


def _goal_id(res: DraftResult) -> str | None:
    return res.map_id or (res.draft.map_id if res.draft is not None else None)


def understand_use(user_id: str, source_id: str, now: datetime, reruns: list[ImportJob]
                   ) -> Callable[[Any, DraftResult], None]:
    """The reading goal commit's material write: `source_use(use='understand', goal_id=map_id)`
    through `sources.uses.record_use` (what `goal_sources._record` writes per material); the
    import run it needs is put in `reruns` (started after the commit). No links. A material
    deleted meanwhile is GoalSourceError(404), one with no sections 409, any other failure 500;
    each rolls the goal's commit back with it."""
    def write(conn: Any, res: DraftResult) -> None:
        reruns.clear()                 # a retried commit writes (and collects) again
        try:
            _added, rerun = material_uses.record_use(conn, user_id, source_id, "understand",
                                                     now, goal_id=_goal_id(res))
        except LookupError as exc:
            raise GoalSourceError(404, MSG_SOURCE_NOT_FOUND) from exc
        except material_uses.StillReading as exc:
            raise GoalSourceError(409, MSG_SOURCE_READING) from exc
        except Exception as exc:
            log.exception("the reading goal's material use for %s was not saved", user_id)
            raise GoalSourceError(500, MSG_LINKS_NOT_SAVED) from exc
        if rerun is not None:
            reruns.append(rerun)
    return write


def create_reading_goal_with_source(repo: Repo, pool: ConnectionPool,
                                    gw: CountingGateway | None, user_id: str, goal: str,
                                    blocks: Sequence[Block], reading_plan: ReadingPlan, *,
                                    runs: SourceRuns, now: datetime, new_id: Ids,
                                    words: Sequence[str] = (), limits: Sequence[str] = (),
                                    beginner: str = "", title: str = "",
                                    extra: Sequence[Callable[[Any], None]] = ()
                                    ) -> DraftResult:
    """`create_reading_goal` with its material read from the store: the same checks as any goal's
    materials before anything is written (`goal_sources.load_materials`: GoalSourceError), its
    toc as it is now, the use row in the goal's commit (`understand_use`), and the import that use
    needs started on `runs` after it (`source_note`: it could not start; the goal stays made)."""
    material, = load_materials(pool, user_id, [reading_plan.source_id])
    reruns: list[ImportJob] = []
    res = create_reading_goal(
        repo, gw, user_id, goal, blocks, reading_plan, now=now, new_id=new_id,
        source=material.toc, words=words, limits=limits, beginner=beginner, title=title,
        extra=extra, on_commit=understand_use(user_id, reading_plan.source_id, now, reruns))
    note = None
    for job in reruns:
        try:
            material_uses.start_next(pool, runs, job)
        except material_uses.RunNotStarted as exc:
            log.warning("reading goal of %s: the full import of %s did not start", job.user_id,
                        job.source_id)
            note = exc.detail
    return dataclasses.replace(res, source_note=note) if note else res


# --- entering a unit (reading-mode spec §3.5, §4) -------------------------------------------------

MSG_USE_IS_TAUGHT = "这一段讲的就是它，不能再当成这一段的前置"
MSG_USE_UNKNOWN = "知识图谱里没有这个节点"


@dataclass(frozen=True)
class UnitResult:
    """A unit after an op: the reading and the unit as committed (or as they were: nothing to
    do), `calls` made, the `source_link` rows the commit writes (`teaches_links`), the unit's page
    and the reading's path once started."""

    reading: Reading
    unit: ReadingUnit
    calls: int = 0
    links: tuple[LinkRow, ...] = ()
    source_links: tuple[LinkRow, ...] = ()   # every reading link into this material after it
    page_id: str | None = None
    path_id: str | None = None


def _reading(state: WikiState, reading_id: str) -> Reading:
    r = state.readings.get(reading_id)
    if r is None:
        raise LookupError(f"没有这份阅读：{reading_id}")
    return r


def _unit(reading: Reading, unit_id: str) -> ReadingUnit:
    u = next((x for x in reading.units if x.unit_id == unit_id), None)
    if u is None:
        raise LookupError(f"这份阅读里没有这一段：{unit_id}")
    return u


def _map(state: WikiState, reading: Reading) -> GoalMap:
    m = state.maps.get(reading.map_id)
    if m is None:
        raise LookupError(f"没有这张学习地图：{reading.map_id}")
    return m


def _with_unit(reading: Reading, unit: ReadingUnit) -> Reading:
    return reading.model_copy(update={"units": tuple(
        unit if u.unit_id == unit.unit_id else u for u in reading.units)})


def _result(reading: Reading, unit_id: str, **kw: Any) -> UnitResult:
    unit = _unit(reading, unit_id)
    return UnitResult(reading=reading, unit=unit, page_id=unit.page_id,
                      path_id=(reading_path_id(reading.reading_id) if unit.page_id is not None
                               else None), **kw)


def _unit_text(excerpts: Sequence[SourceExcerpt]) -> str:
    """The unit's text for the 「看这一段」 prompt: each span under its section's label and title;
    a span too long to give in full is its summary."""
    return "\n\n".join(f"{e.label} {e.title}\n" + (e.text.strip() if e.text is not None
                                                     else f"（摘要）{e.summary}")
                       for e in excerpts)


def _unit_input(w: _Work, reading: Reading, unit: ReadingUnit, m: GoalMap,
                excerpts: Sequence[SourceExcerpt]) -> UnitInput:
    st, kv = w.state, w.kview()

    def name(cid: str) -> str:
        return concept_name(st, cid, user_id=w.user_id)

    units = tuple(UnitRow(order=u.order, title=u.title, about=u.about,
                          teaches=tuple(name(t) for t in u.teaches))
                  for u in sorted(reading.units, key=lambda u: u.order))
    taught = {t: name(t) for u in reading.units if u.unit_id != unit.unit_id for t in u.teaches}
    return UnitInput(title=reading.title, text=_unit_text(excerpts), units=units,
                     order=unit.order,
                     blocks=tuple((n.id, name(n.id)) for n in m.nodes),
                     taught=taught, existing=existing_refs(st, w.k),
                     annotations=_annotations(st, kv), topics=_topics(st),
                     graph_relations=_graph_relations(st), language=st.language)


def _resolve(w: _Work, goal: str, points: Sequence[UnitPoint]) -> list[tuple[str, bool]]:
    """Each point's node id and whether the wiki has it: an existing node or a prerequisite block
    by its id; a new name by `_block_ids` (the same goal's region of that name, a goal-local id
    when another node has the name, else a new id)."""
    names = {n.id for n in w.state.graph.nodes} | set(w.state.names)
    fresh = [p for p in points if not (p.existing_id or p.block_id)]
    made = iter(_block_ids(w.state, w.k, goal, [Block(name=p.name) for p in fresh]))
    out = []
    for p in points:
        known = p.existing_id or p.block_id
        out.append((known, known in names) if known else next(made))
    return out


def _concept(w: _Work, cid: str, reused: bool, p: UnitPoint | None,
             prereqs: Sequence[str] = ()) -> IntakeConcept:
    """A node to write: a reused one keeps its name, parts and `atomic` (None: the recorded one);
    a new one takes the model's name, summary and `atomic`."""
    named = w.state.names.get(cid)
    aspects = w.k.aspects(cid) if cid in w.k.concept_ids() else DEFAULT_ASPECTS
    if reused or p is None:
        return IntakeConcept(id=cid, name=concept_name(w.state, cid, user_id=w.user_id),
                             aspects=aspects, subitems=named.subitems if named else (),
                             reused=True, prereqs=tuple(prereqs))
    return IntakeConcept(id=cid, name=p.name, aspects=aspects, definition=p.summary,
                         atomic=p.atomic, prereqs=tuple(prereqs))


def _write_graph(w: _Work, goal: str, title: str, concepts: Sequence[IntakeConcept]) -> None:
    """K concepts (prerequisites unioned), names and the graph (`goal._goal_graph`: the uses as
    prerequisites of the teaches; a self-loop or cycle is dropped there)."""
    res = IntakeResult(goal=goal, title=title or goal, concepts=tuple(concepts), targets=(),
                       observations=(), dropped=(), resolutions=())
    _intake_events(w, res)
    _goal_graph(w, res, {})


def _taught(reading: Reading) -> set[str]:
    return {t for u in reading.units for t in u.teaches}


def _onto_map(w: _Work, m: GoalMap, ids: Sequence[tuple[str, bool]],
              taught: set[str]) -> GoalMap:
    """Every prerequisite not on the map yet onto its top level (as a block: a region, entered
    or opened as stage 1 does; another map's decision about it is carried over:
    `_prior_region`), except a point a unit of this reading teaches (`taught`): it stays a use
    of the unit only, never on the map (§2.3). GoalMapSaved(edited) when anything was added."""
    on = {n.id for n in m.nodes} | taught
    add = [_prior_region(w.state, MapNode(id=i, kind="region", parent=None, reused=reused,
                                          name=concept_name(w.state, i, user_id=w.user_id)))
           for i, reused in dict.fromkeys(ids) if i not in on]
    if not add:
        return m
    m2 = m.model_copy(update={"nodes": (*m.nodes, *add)})
    w.add(GoalMapSaved(record=m2, action="edited"))
    return m2


def _links(reading: Reading, unit: ReadingUnit) -> tuple[LinkRow, ...]:
    secs = list(dict.fromkeys(sp.section_id for sp in unit.spans))
    return tuple(LinkRow(node_id=t, section_id=s, source_id=reading.source_id, role="teaches",
                         by="reading") for t in unit.teaches for s in secs)


def _source_links(state: WikiState, reading: Reading) -> tuple[LinkRow, ...]:
    """Every `source_link(teaches, by='reading')` the readings of this material should have:
    each analyzed unit's teaches to each section it reads (`reading` as it is after the commit,
    the other readings of the material as they are)."""
    readings = [reading, *(r for r in state.readings.values()
                           if r.source_id == reading.source_id
                           and r.reading_id != reading.reading_id)]
    return tuple(dict.fromkeys(k for r in readings for u in r.units if u.analyzed
                               for k in _links(r, u)))


@traced_entry("reading_unit", subject="reading_id")
def analyze_unit(repo: Repo, gw: CountingGateway | None, user_id: str, reading_id: str,
                 unit_id: str, *, now: datetime, new_id: Ids,
                 excerpts: Sequence[SourceExcerpt] = (),
                 extra: Sequence[Callable[[Any], None]] = (),
                 on_commit: Callable[[Any, UnitResult], None] | None = None) -> UnitResult:
    """「看这一段」 (module doc): `excerpts` — the unit's own text (`SourceExcerpts.for_spans`).
    An analyzed unit: no call, nothing written. LookupError: unknown reading / unit / map;
    ReadingRefused skipped; IntakeError: the answer unusable twice (nothing written); the
    gateway's errors propagate."""
    snap = repo.load(user_id)
    reading = _reading(snap.state, reading_id)
    unit = _unit(reading, unit_id)
    if unit.analyzed:
        return _result(reading, unit_id)
    if unit.skipped:
        raise ReadingRefused("skipped", unit_id)
    m = _map(snap.state, reading)
    w = _Work(snap, gw, user_id, now=now, new_id=new_id)
    got: UnitAnalysis = reading_unit.analyze_unit(
        w.llm(), _unit_input(w, reading, unit, m, excerpts))
    both = _resolve(w, m.goal, [*got.teaches, *got.uses])     # one pass: new ids never collide
    t_ids, u_ids = both[:len(got.teaches)], both[len(got.teaches):]
    teaches = list(dict.fromkeys(i for i, _ in t_ids))
    uses = list(dict.fromkeys(i for i, _ in u_ids))
    blocks = {n.id for n in m.nodes}         # every map node, an entered block's parts too
    others = {t for u in reading.units if u.unit_id != unit_id for t in u.teaches}
    if set(teaches) & (others | blocks | set(uses)):
        # the names passed the call's checks but resolve onto another unit's point, a block or
        # a use: never written quietly (reading_unit compares names as _block_ids resolves them)
        raise IntakeError("看这一段的知识点和别的段、前置块或用到的点是同一个节点")
    new_uses = [_concept(w, i, reused, p) for (i, reused), p in zip(u_ids, got.uses, strict=True)
                if not reused]
    taught = [_concept(w, i, reused, p, prereqs=uses)
              for (i, reused), p in zip(t_ids, got.teaches, strict=True)]
    _write_graph(w, m.goal, m.title, [*new_uses, *taught])
    _onto_map(w, m, u_ids, _taught(reading) | set(teaches))
    done = unit.model_copy(update={"analyzed": True, "teaches": tuple(teaches),
                                   "uses": tuple(uses), "focus": got.focus})
    reading = _with_unit(reading, done)
    w.add(ReadingSaved(record=reading, action="unit_analyzed"))
    res = _result(reading, unit_id, calls=w.calls, links=_links(reading, done),
                  source_links=_source_links(w.state, reading))
    w.commit(repo, [*extra] if on_commit is None
             else [*extra, lambda conn: on_commit(conn, res)])
    return res


def teaches_links(user_id: str, now: datetime) -> Callable[[Any, UnitResult], None]:
    """The 「看这一段」 commit's material write (§2.3): the material's reading links become what
    its readings teach now (`UnitResult.source_links`, `sources.store.replace_source_links`):
    the unit's new rows written, a row of a point no unit teaches any more (two units merged
    and read again) gone; reading links into any other material kept. A
    section gone meanwhile is a LookupError (404) and rolls the commit back."""
    def write(conn: Any, res: UnitResult) -> None:
        source_store.replace_source_links(conn, user_id, res.reading.source_id,
                                          res.source_links, by="reading", now=now)
    return write


def analyze_unit_with_source(repo: Repo, pool: ConnectionPool, gw: CountingGateway | None,
                             user_id: str, reading_id: str, unit_id: str, *, now: datetime,
                             new_id: Ids) -> UnitResult:
    """`analyze_unit` with the unit's text read from the store (`PooledSourceExcerpts.for_spans`,
    fail closed: a material gone is a LookupError) and its links written in the same commit
    (`teaches_links`). An analyzed unit reads nothing."""
    snap = repo.load(user_id)
    reading = _reading(snap.state, reading_id)
    unit = _unit(reading, unit_id)
    if unit.analyzed or unit.skipped:
        return analyze_unit(repo, gw, user_id, reading_id, unit_id, now=now, new_id=new_id)
    text = PooledSourceExcerpts(pool).for_spans(user_id, reading.source_id, unit.spans)
    return analyze_unit(repo, gw, user_id, reading_id, unit_id, now=now, new_id=new_id,
                        excerpts=text, on_commit=teaches_links(user_id, now))


def _new_cells(kv: AspectKView, targets: Sequence[str]) -> int:
    return sum(1 for t in targets for a in kv.aspects(t)
               if kv.cell(cell_id(t, a)).band not in LEARNED_BANDS)


def _page_concept(w: _Work, cid: str) -> PageConcept:
    node = next((n for n in w.state.graph.nodes if n.id == cid), None)
    return PageConcept(id=cid, name=concept_name(w.state, cid, user_id=w.user_id),
                       prereqs=w.k.prereqs(cid) if cid in w.k.concept_ids() else (),
                       definition=node.definition if node is not None else "")


def _reading_path(w: _Work, reading: Reading, m: GoalMap, unit: ReadingUnit,
                  page_id: str) -> PathRecord:
    """The reading's path with `unit`'s page added (pages in unit order, keyed by unit id; the
    started one LEARNING, the others as they were): one PathRecord per reading."""
    path_id = reading_path_id(reading.reading_id)
    old = w.state.paths.get(path_id)
    rows: dict[str, tuple[PathPage, str | None]] = (
        {p.page_id: (p, pid) for p, pid in zip(old.path.pages, old.page_ids, strict=True)}
        if old is not None else {})
    rows[unit.unit_id] = (PathPage(
        page_id=unit.unit_id, targets=unit.teaches, concepts=(*unit.teaches, *unit.uses),
        objective=unit.about or unit.title, title=unit.title,
        new_cells=_new_cells(w.kview(), unit.teaches), status=PageStatus.LEARNING,
        kind="reading", unit_id=unit.unit_id), page_id)
    order = {u.unit_id: u.order for u in reading.units}
    keys = sorted(rows, key=lambda k: order.get(k, 1 << 30))
    pages = tuple(rows[k][0] for k in keys)
    ids = list(dict.fromkeys(c for p in pages for c in p.concepts))
    path = LearningPath(path_id=path_id, goal=m.goal, goal_key=goal_key(m.goal, ()),
                        split=len(pages) > 1, source="reading",
                        new_cells=sum(p.new_cells for p in pages),
                        concepts=tuple(_page_concept(w, c) for c in ids), pages=pages)
    return PathRecord(path=path, page_ids=tuple(rows[k][1] for k in keys),
                      created_at=old.created_at if old is not None else w.now,
                      sources=(reading.source_id,))


def start_unit_page(repo: Repo, user_id: str, reading_id: str, unit_id: str, *, now: datetime,
                    new_id: Ids, extra: Sequence[Callable[[Any], None]] = ()) -> UnitResult:
    """The unit's reading page (module doc). No model call. A started unit: its page, nothing
    written. LookupError: unknown reading / unit / map; ReadingRefused not_analyzed (「看这一段」
    first: the API does both in order), skipped."""
    snap = repo.load(user_id)
    reading = _reading(snap.state, reading_id)
    unit = _unit(reading, unit_id)
    if unit.page_id is not None:
        return _result(reading, unit_id)
    if unit.skipped:
        raise ReadingRefused("skipped", unit_id)
    if not unit.analyzed or not unit.teaches:
        raise ReadingRefused("not_analyzed", unit_id)
    m = _map(snap.state, reading)
    w = _Work(snap, None, user_id, now=now, new_id=new_id)
    rec, evs = page_events_for(w.state, w.k, title=unit.title, goal=m.goal,
                               concepts=(*unit.teaches, *unit.uses), targets=unit.teaches,
                               now=now, new_id=new_id, user_id=user_id,
                               reading=(reading_id, unit_id), folder=goal_folder(w.state, m))
    w.add_events(evs)
    w.add(PathSaved(record=_reading_path(w, reading, m, unit, rec.page_id),
                    action="page_started"))
    reading = _with_unit(reading, unit.model_copy(update={"page_id": rec.page_id}))
    w.add(ReadingSaved(record=reading, action="page_started"))
    w.commit(repo, extra)
    return _result(reading, unit_id)


def add_unit_use(repo: Repo, user_id: str, reading_id: str, unit_id: str, *, now: datetime,
                 new_id: Ids, existing_id: str = "", name: str = "", summary: str = "",
                 atomic: bool = False) -> UnitResult:
    """A prerequisite found while reading (§3.5): the node `existing_id`, or a new one by `name`
    (`_block_ids`' rules: this goal's block of that name is that block). It joins the unit's uses
    (a prerequisite of its teaches in the graph) and this map's top level; already a use:
    nothing written. LookupError: unknown reading / unit / map; ReadingRefused not_analyzed;
    WikiInvalid: an unknown node, no name, or a point the unit teaches."""
    snap = repo.load(user_id)
    reading = _reading(snap.state, reading_id)
    unit = _unit(reading, unit_id)
    if not unit.analyzed:
        raise ReadingRefused("not_analyzed", unit_id)
    m = _map(snap.state, reading)
    w = _Work(snap, None, user_id, now=now, new_id=new_id)
    eid = existing_id.strip()
    if eid and eid not in {n.id for n in w.state.graph.nodes} | set(w.state.names):
        raise WikiInvalid(MSG_USE_UNKNOWN)
    if not eid and not name.strip():
        raise WikiInvalid("要写已有节点的 id 或新知识点的名字")
    point = UnitPoint(name=name.strip(), summary=summary.strip(), atomic=atomic, existing_id=eid)
    (cid, reused), = _resolve(w, m.goal, [point])
    if cid in unit.teaches:
        raise WikiInvalid(MSG_USE_IS_TAUGHT)
    if cid in unit.uses:
        return _result(reading, unit_id)
    new = [] if reused else [_concept(w, cid, False, point)]
    taught = [_concept(w, t, True, None, prereqs=(cid,)) for t in unit.teaches]
    _write_graph(w, m.goal, m.title, [*new, *taught])
    _onto_map(w, m, [(cid, reused)], _taught(reading))
    reading = _with_unit(reading, unit.model_copy(update={"uses": (*unit.uses, cid)}))
    w.add(ReadingSaved(record=reading, action="uses_added"))
    w.commit(repo)
    return _result(reading, unit_id)


# --- the material's text changed (§2.1 last rule) ------------------------------------------------

class ReanchorFailed(RuntimeError):
    """Re-anchoring a reading after its material's text changed left a structure that does not
    fit the material (`check_units`); nothing was written. The import step fails with it (fail
    closed: never a quiet move)."""


def reanchor_readings(repo: Repo, pool: ConnectionPool, user_id: str, source_id: str,
                      section_ids: Sequence[str], *, now: datetime, new_id: Ids,
                      gen: int | None = None) -> list[str]:
    """Sections `section_ids` of material `source_id` have new text (`importer.swap_reads`):
    every reading of it that reads one of them is re-anchored on their paragraphs as stored now
    (`reanchor`: an anchor found moves its span, one not found is marked uncertain), one commit of
    ReadingSaved(edited) for the readings that changed. ReanchorFailed (nothing written): a
    changed section is gone, or the result has a structure problem it did not have before. A
    material deleted meanwhile: nothing to do. Returns the changed reading ids."""
    changed = list(dict.fromkeys(section_ids))
    snap = repo.load(user_id)
    readings = [r for r in snap.state.readings.values() if r.source_id == source_id
                and any(sp.section_id in changed for u in r.units for sp in u.spans)]
    if not readings:
        return []
    from evowiki.sources.importer import _Deleted, _generation_connection, _require_generation
    try:
        with _generation_connection(pool, user_id, source_id, gen) as conn:
            src = source_store.get_source(conn, user_id, source_id)
            assert src is not None
            gen = src.run_gen
            secs = source_store.sections(conn, user_id, source_id)
    except _Deleted:
        return []
    toc = source_toc(src, secs)
    paras = {s.id: s.paras for s in toc.sections}
    counts = {s.id: len(s.paras) for s in toc.sections if s.paras}
    w = _Work(snap, None, user_id, now=now, new_id=new_id)
    out: list[str] = []
    for r in readings:
        new = r
        for sid in changed:
            if not any(sp.section_id == sid for u in r.units for sp in u.spans):
                continue
            if sid not in paras:
                raise ReanchorFailed(f"reading {r.reading_id}: section {sid} is gone")
            new = reanchor(new, sid, paras[sid])
        if new == r:
            continue
        before = set(check_units(r.units, toc.sections, counts))
        added = [x for x in check_units(new.units, toc.sections, counts) if x not in before]
        if added:
            log.error("reading %s of %s/%s could not be re-anchored: %s", r.reading_id, user_id,
                      source_id, added)
            raise ReanchorFailed(f"reading {r.reading_id}: {'；'.join(added)}")
        w.add(ReadingSaved(record=new, action="edited"))
        out.append(r.reading_id)
    if out:
        def validate_generation(conn):
            if conn is None:
                with _generation_connection(pool, user_id, source_id, gen):
                    return
            _require_generation(conn, user_id, source_id, gen)
        w.commit(repo, extra=[validate_generation])
    return out
