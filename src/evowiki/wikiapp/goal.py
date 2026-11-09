"""Goal flow (big-goal design; S1 「定目标」, spec 2026-10-28-llm-call-consolidation): a goal is one S1
call (goals/blocks.py) → a goal map of a few blocks → adaptive diagnosis rounds → the blocks are
entered one at a time.

- `plan_goal_blocks`: S1, one call: the blocks and the goal record (the learner's words, his
  limits, each block's covers / level / whether to diagnose it / his words about it). The chat's
  `new_goal` shows them as a card the learner edits and confirms. A 读懂 goal's S1 also plans its
  reading units (reading-mode spec §3.2, `BlocksResult.reading`); it is made by the reading flow
  (wikiapp/reading.py), never here.
- `create_goal_from_blocks`: the map from the blocks (no further call): the blocks are the
  top-level regions with no members, the record on `GoalMap` / `MapNode`, then the opening round
  about the blocks. A round with questions is
  stored as a draft; with none the goal finishes at once.
- `answer_check` (stage 3 T11): a round is the KST pick only (goals/probe, goals/kst: the node and
  the cell; no model call); the assistant asks it in the chat (`ask_check`) and his answer becomes
  evidence on that cell (self-report weak, typed `self`; quiz strong), then the next pick or the
  end. Each answered question and, at the end, each node's decision with its source go to the map
  log. `stop_draft`: the user's 「够了，开始学」.
- finish: a map draft → the map (a region K shows learned is skipped: `regions.block_known`, read
  from K every time, never stored on the map); a region draft → that region's (or point's)
  learning path. A draft from before S1 (fixed questions, or no map) is refused (OLD_DRAFT).
- `enter_region` (on-demand decomposition, spec 2026-10-28 §2.2): entering any node of the map
  splits a non-atomic one ONE level (scoped intake@v32: its own level's covers, the blocks above
  it, the map's limits, his words about it) or shows the parts it already has, and plans
  nothing; opening a point (atomic) diagnoses only it and its unlearned prerequisites when K has
  no evidence on them (none for a block not to diagnose), then plans its path, which hangs on
  that map node (`_open_point`).
  The 学习路径 card's edits are wikiapp/map_edits.py; `draft_status` reads a draft back (GET).

One kind of node (unified node graph, spec 2026-10-13; plans T4, T8): a region is a graph node that
has children, and its members are its knowledge points (grey until there is evidence). The intake
of an entered block sees the whole graph — every node with its parents and parts, its
prerequisites with their phrases, what K has evidence for (`_graph_relations`, `_topics`,
`_annotations`) — writes nodes and relations with phrases, and reuses ids at any level, so one
thing is one node everywhere. Goal creation writes the graph in the same commit as the map
(`GraphSaved(action="goal_nodes")`); every node is a K concept (`nodes.ksync`), so a region holds
direct evidence of its own next to its members'.
Nothing K had evidence for before the draft, nothing the description already said
(`Draft.described`) and nothing answered earlier in the draft is asked about again (goals/probe);
a region not entered whose every cell K puts at likely or above is known and skipped
(`regions.block_known`: K is the only source, user ruling 2026-10-28); entering a region only
starts learning it (`expanded`) and plans its pages, with no re-diagnosis of evidenced members: a
member the map's opening diagnosis decided from an answer (a quiz, a self-report, K), also one
decided only by inference through the prerequisites, keeps that decision as it is
(`_opening_decisions`, 有证据就不再问; teaching corrects it); only members with no answer at all may
be asked.
The diagnosis's prior (probe@v5): the intake gap identifier's level + confidence per concept and
region (`Draft.levels`, `_levels`; a region draft takes the opening's levels of its members).

Which question to ask, when to stop and what to teach in this draft is the KST algorithm's call
(goals/probe; whether a block is known afterwards is K's);
how many concepts and pages is the model's (goals/path); code refuses only what cannot work. K is written as soon as evidence exists (D5)."""

import dataclasses
import hashlib
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from evowiki.belief.aspects import AspectK, AspectKView
from evowiki.goals.blocks import (
    READING_PLAN_STEP,
    Block,
    BlocksResult,
    MaterialMode,
    block_links,
    plan_blocks,
)
from evowiki.goals.contracts import Aspect, Band, Observation, cell_id
from evowiki.goals.diagnostic import IDK
from evowiki.goals.intake import (
    DEFAULT_ASPECTS,
    GoalUse,
    IntakeConcept,
    IntakeRegion,
    IntakeRelation,
    IntakeResult,
    IntakeScope,
    NodeSectionLink,
    SkillGap,
    SourceToc,
    SourceUse,
    TopicRef,
    _skill_gaps,
    decompose_one_level,
)
from evowiki.goals.observe import ASPECT_LABEL, BAND_LABEL, self_observations
from evowiki.goals.page_fit import PageFit, PageText, judge_fit
from evowiki.goals.path import LEARNED_BANDS, LearningPath, goal_key, plan_path
from evowiki.goals.probe import (
    NodeDecision,
    NodeLevel,
    ProbeNode,
    ProbeQuestion,
    ProbeRound,
    answer_text,
    check_answers,
    decision_lines,
    failed_nodes,
    next_round,
    probe_observations,
    question_line,
)
from evowiki.goals.regions import GoalMap, MapNode, block_known, covered, ordered, region_order
from evowiki.goals.selfreport import MEANS_CLAIM
from evowiki.goals.selfreport import self_observations as self_report_observations
from evowiki.llm.deadline import on_reply_started
from evowiki.llm.run_context import traced_entry
from evowiki.nodes.model import NodeRec, Relation
from evowiki.plain_text import norm_name
from evowiki.wikiapp.events import (
    ConceptName,
    ConceptsNamed,
    Draft,
    DraftClosed,
    DraftCreated,
    GoalMapSaved,
    GoalSettingsSet,
    MergedFrom,
    PageFitSaved,
    PageRecord,
    PageSaved,
    PathRecord,
    PathSaved,
    ProbeAnswered,
    RoughMarked,
    WikiEvent,
)
from evowiki.wikiapp.goal_nodes import entered_nodes, related_nodes
from evowiki.wikiapp.graph_sync import add_relations, graph_events, same_structure
from evowiki.wikiapp.kwrite import (
    IDK_LABEL,
    Ids,
    answer_label,
    apply_in_memory,
    define_events,
    existing_refs,
    observation_events,
)
from evowiki.wikiapp.llm import CountingGateway, GatewayJsonLLM, require
from evowiki.wikiapp.node_state import child_nodes as node_children
from evowiki.wikiapp.node_state import (
    enter_kind,
    fit_current,
    fit_key,
    foreign_parts,
    graph_children,
    live_page,
    marked,
    pages_elsewhere,
)
from evowiki.wikiapp.page_folder import page_folder
from evowiki.wikiapp.page_version import page_text_fingerprint
from evowiki.wikiapp.path_trace import traced_plan
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict, WikiInvalid
from evowiki.wikiapp.state import WikiState, apply_event

log = logging.getLogger(__name__)
TO_LEARN = frozenset({Band.GAP, Band.SHAKY, Band.NONE})


@dataclass(frozen=True)
class DraftResult:
    """Exactly one outcome: `open` (the draft waits for answers to its last round), `page_id`
    (a page was created), `path_id` (a learning path was planned) or `map_id` (the goal map is
    ready to enter). `draft` carries the intake result (title, concepts, notes) and the rounds;
    it is stored only when it was ever open. `answered`: the round just answered, each question
    with the choice (right / wrong in the UI)."""

    draft: Draft | None
    page_id: str | None
    calls: int
    path_id: str | None = None
    map_id: str | None = None
    open: bool = False
    answered: tuple[tuple[ProbeQuestion, int], ...] = ()
    links: tuple[NodeSectionLink, ...] = ()     # the material sections each node comes from
    source_note: str | None = None    # a material's import did not start (goal_sources.py)
    reading_id: str | None = None     # a 读懂 goal's reading (wikiapp/reading.py)


def _wev(new_id: Ids, now: datetime, body) -> WikiEvent:
    return WikiEvent(event_id=new_id("wev"), ts=now, body=body)


def new_page(state: WikiState, draft_like: Draft, *, now: datetime, new_id: Ids) -> PageRecord:
    """The page record for a goal. `state` must already know the concept names."""
    return _new_page(state, title=draft_like.title, goal=draft_like.goal,
                     concepts=tuple(c.id for c in draft_like.concepts),
                     targets=draft_like.targets, now=now, new_id=new_id)


def _new_page(state: WikiState, *, title: str, goal: str, concepts: tuple[str, ...],
              targets: tuple[str, ...], now: datetime, new_id: Ids) -> PageRecord:
    rec = PageRecord(page_id=new_id("pg"), title=title, goal=goal,
                     concepts=concepts, targets=targets, created_at=now)
    return rec.model_copy(update={"folder": page_folder(state, rec)})   # it never moves


def page_events_for(state: WikiState, k: AspectK, *, title: str, goal: str,
                    concepts: tuple[str, ...], targets: tuple[str, ...], now: datetime,
                    new_id: Ids, user_id: str = "",
                    merged_from: tuple[MergedFrom, ...] = (),
                    reading: tuple[str, str] | None = None,
                    folder: str | None = None) -> tuple[PageRecord, list[WikiEvent]]:
    """A new page record (PageSaved created). Shared by path pages, the assistant's pages and reading
    pages; `merged_from`: the pages a merged page is written from (`page_merge`); `reading`
    (reading id, unit id): a reading page (`kind="reading"`, reading-mode spec §4: the writer knows it by its kind)."""
    rec = _new_page(state, title=title, goal=goal, concepts=concepts, targets=targets, now=now,
                    new_id=new_id)
    if folder is not None:
        rec = rec.model_copy(update={"folder": folder})
    if merged_from:
        rec = rec.model_copy(update={"merged_from": merged_from})
    if reading is not None:
        rec = rec.model_copy(update={"kind": "reading", "reading_id": reading[0],
                                     "unit_id": reading[1]})
    return rec, [_wev(new_id, now, PageSaved(record=rec, action="created"))]


# --- one request's work ----------------------------------------------------------------------

class _Work:
    """The events one request will commit, with the wiki state and K they lead to (the snapshot
    stays untouched until the commit)."""

    def __init__(self, snap: Snapshot, gw: CountingGateway | None, user_id: str, *,
                 now: datetime, new_id: Ids) -> None:
        self.snap, self.gw, self.user_id, self.now, self.new_id = snap, gw, user_id, now, new_id
        self.calls0 = gw.calls if gw is not None else 0
        self.state = snap.state
        self.k = snap.k
        self.aspect: list = []
        self.wiki: list[WikiEvent] = []

    def llm(self, source: str = "") -> GatewayJsonLLM:
        """`source`: which entrance asks (a one-level split's `params["source"]`)."""
        return GatewayJsonLLM(require(self.gw, self.user_id), subject=self.user_id,
                              params={"source": source} if source else None)

    def add_aspect(self, events: Sequence) -> None:
        self.aspect += events
        self.k = apply_in_memory(self.snap, self.aspect)

    def add(self, body) -> None:
        self.add_events([_wev(self.new_id, self.now, body)])

    def add_events(self, evs: Sequence[WikiEvent]) -> None:
        for ev in evs:
            self.wiki.append(ev)
            self.state = apply_event(self.state, ev)

    def kview(self) -> AspectKView:
        return AspectKView(self.k, self.now)

    @property
    def calls(self) -> int:
        return self.gw.calls - self.calls0 if self.gw is not None else 0

    def commit(self, repo: Repo, extra: Sequence[Callable[[Any], None]] = ()) -> None:
        repo.commit(self.snap, Commit(aspect=self.aspect, wiki=self.wiki, extra=list(extra)))


def _intake_events(w: _Work, res: IntakeResult) -> None:
    """Concept definitions (regions too) and self-report observations into K, and the names
    (with each node's `atomic`: the decomposition's call, else the one already recorded)."""
    w.add_aspect([*define_events(res.concepts, ts=w.now, new_id=w.new_id),
                  *observation_events(res.observations, ts=w.now, new_id=w.new_id)])
    w.add(ConceptsNamed(names=tuple(ConceptName(
        concept_id=c.id, name=c.name, subitems=c.subitems,
        atomic=c.atomic if c.atomic is not None else _atomic(w.state, c.id))
        for c in res.concepts)))


def _atomic(state: WikiState, cid: str) -> bool | None:
    """What the graph already says (a decomposition that did not judge a node keeps it)."""
    named = state.names.get(cid)
    return named.atomic if named is not None else None


def _described(res: IntakeResult) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The concepts the description spoke to, and per concept what it said: "+" when every
    observation on it is positive, "-" when every one is negative, else "~"."""
    pols: dict[str, set[str]] = {}
    for o in res.observations:
        pols.setdefault(o.cell.rpartition(".")[0], set()).add(o.polarity)
    return tuple(pols), tuple(next(iter(p)) if len(p) == 1 else "~" for p in pols.values())


def _levels(res: IntakeResult, region_ids: dict[str, str] | None = None
            ) -> tuple[NodeLevel, ...]:
    """The gap identifier's informative ratings (a level kst.level_p0 knows; unknown is left
    out), once per node: a concept by its id; a region (`region_ids`: the intake's region id →
    its id on the map) by its intake id or its exact name."""
    concepts = {c.id for c in res.concepts}
    regions = {r.id: (region_ids or {}).get(r.id, r.id) for r in res.regions}
    by_name = {norm_name(r.name): regions[r.id] for r in res.regions}
    out: dict[str, NodeLevel] = {}
    for g in res.skill_gaps:
        node = (g.concept if g.concept in concepts else regions.get(g.concept)
                or by_name.get(norm_name(g.concept)) or by_name.get(norm_name(g.name)))
        lv = NodeLevel(node=node or "", level=g.level, confidence=g.confidence)
        if node and node not in out and lv.p0 is not None:
            out[node] = lv
    return tuple(out.values())


def _draft(draft_id: str, goal: str, res: IntakeResult, *,
           map_id: str | None = None, region_id: str | None = None,
           evidenced: tuple[str, ...] = (), levels: tuple[NodeLevel, ...] | None = None,
           sources: tuple[str, ...] = (), source_use: str | None = None) -> Draft:
    described, described_as = _described(res)
    return Draft(draft_id=draft_id, goal=goal, title=res.title,
                 concepts=res.concepts, targets=res.targets, items=(),
                 from_background=len(res.observations),
                 reused=sum(c.reused for c in res.concepts), dropped_questions=0,
                 map_id=map_id, region_id=region_id, evidenced=evidenced,
                 described=described, described_as=described_as,
                 levels=_levels(res) if levels is None else levels,
                 sources=sources, source_use=source_use)


# --- what the wiki already has -------------------------------------------------------------------

def _has_evidence(kv: AspectKView, cid: str) -> bool:
    """Any cell with direct evidence or a declaration (inferred evidence does not count)."""
    return any((b := kv.cell(cell_id(cid, a))).direct > 0 or b.declared is not None
               for a in kv.aspects(cid))


def _evidenced(kv_before: AspectKView, ids) -> tuple[str, ...]:
    """The concept ids K had evidence for before this request (`kv_before`: the snapshot's K)."""
    return tuple(i for i in dict.fromkeys(ids) if _has_evidence(kv_before, i))


def _latest_nodes(state: WikiState) -> dict[str, tuple[GoalMap, MapNode]]:
    """Every node id on any map → its latest map (planning order) and node there."""
    out: dict[str, tuple[GoalMap, MapNode]] = {}
    for mid in state.map_order:
        m = state.maps[mid]
        for n in m.nodes:
            out[n.id] = (m, n)
    return out


def _topics(state: WikiState) -> list[TopicRef]:
    """Every grouping the wiki has: each graph node that has children (a curator topic, a goal's
    region, …) with its children as members, in node id order."""
    g = state.graph
    children: dict[str, list[str]] = {}
    for r in g.relations:
        if r.kind == "part_of":
            children.setdefault(r.dst, []).append(r.src)
    return [TopicRef(id=n.id, name=n.name, members=tuple(children[n.id]))
            for n in g.nodes if n.id in children]


def _graph_relations(state: WikiState) -> list[IntakeRelation]:
    """The graph's relations as decompose sees them (kind, ends, phrase): its parents / parts and
    its prerequisites with their phrases, its recommended edges (intake@v20 CREATE)."""
    return [IntakeRelation(kind=r.kind, src=r.src, dst=r.dst, phrase=r.phrase)
            for r in state.graph.relations]


def _annotations(state: WikiState, kv: AspectKView) -> dict[str, str]:
    """Per existing node, the evidence K already has (bands per aspect): what decompose should
    know besides the graph's structure (parents, parts and prerequisites are on its row)."""
    out: dict[str, str] = {}
    for cid in state.names:
        if _has_evidence(kv, cid):
            bands = "、".join(f"{ASPECT_LABEL[a]} {BAND_LABEL[kv.cell(cell_id(cid, a)).band.value]}"
                             for a in kv.aspects(cid))
            out[cid] = f"已有证据：{bands}"
    return out


def _existing_lines(state: WikiState) -> list[str]:
    """The user's pages and not-yet-started path pages, one line each (title, what it teaches,
    status), for the path model (prompt rule: do not re-teach)."""
    def names(ids) -> str:
        return "、".join(state.names[c].name if c in state.names else c for c in ids)

    lines = [f"- 页《{rec.title}》（{'学完' if rec.done and rec.has_body else '在学' if rec.has_body else '还没写好'}）：讲 {names(rec.targets)}"
             + (f"，也用到 {names(c for c in rec.concepts if c not in rec.targets)}"
                if set(rec.concepts) - set(rec.targets) else "")
             for rec in (state.pages[p] for p in state.page_order if p not in state.trashed_pages)]
    for pid in state.path_order:
        rec = state.paths[pid]
        for i, (pg, page_id) in enumerate(zip(rec.path.pages, rec.page_ids, strict=True), 1):
            if page_id is None:
                title = f"《{pg.title}》" if pg.title else ""
                lines.append(f"- 学习路径「{rec.path.goal}」第 {i} 页{title}（还没开始）："
                             f"{pg.objective}；讲 {names(pg.targets)}")
    return lines


def _taught(state: WikiState) -> dict[str, str]:
    """Concept id → title of the user's existing page that teaches it (a target of the page): the
    path's code side counts these as settled (goals.path `fringe`). Only a written page teaches
    (D04): a page still being written, or whose write failed, does not."""
    out: dict[str, str] = {}
    for rec in (state.pages[p] for p in state.page_order
                if p not in state.trashed_pages and state.pages[p].has_body):
        for c in rec.targets:
            out.setdefault(c, rec.title)
    return out


# --- probe -----------------------------------------------------------------------------------

def _node_cells(kv: AspectKView, ids) -> tuple[str, ...]:
    return tuple(cell_id(c, a) for c in ids for a in kv.aspects(c))


def _k_prior(kv: AspectKView, ev: set[str], ids: Sequence[str]):
    """What K's evidence from before the draft says about a node covering `ids` when every one
    of them had evidence: teach when a cell with evidence is still to learn (gap / shaky), else
    known (cells without evidence say nothing); None when not all had evidence (it may be
    asked)."""
    ids = [c for c in ids if kv.aspects(c)]
    if not ids or not all(c in ev for c in ids):
        return None
    bands = [b.band for c in ids for a in kv.aspects(c)
             if (b := kv.cell(cell_id(c, a))).direct > 0 or b.declared is not None]
    return "teach" if any(b in TO_LEARN for b in bands) else "known"


def _probe_nodes(w: _Work, draft: Draft, m: GoalMap | None) -> list[ProbeNode]:
    """What a draft diagnoses (the KST items, goals/probe): the map's top-level regions followed
    by their members as concept items — only the TOP-LEVEL regions are region items: a region
    nested under another (the `_build_map` parent fix) is a concept item with its own cells and
    its parts follow it, so a parent and its sub-region are never asked as two regions (the 黑壳虾
    run) — a region's concepts once entered, or the flat goal's concepts. Prerequisites (the surmise relation, among the items): only VERIFIED graph
    prerequisites (relation layers, user ruling 2026-10-14: a wrong prerequisite makes KST infer
    mastery wrongly) — the graph's, the intake's, the map's and K's stored prereqs all pass the
    same filter, so an intake / map / K prereq counts only when the graph has that edge as a
    verified prerequisite; `recommended` never enters. A region K shows known
    (`block_known`) and its members count as known (K prior); a node K had evidence for before the draft has a K prior and is never
    asked. `builds_on`: the unverified ones (the graph's `recommended` edges and unverified
    prerequisites, the intake's / map's prereqs the graph does not verify) — the probe uses them
    only downward, after a failure (goals/probe.py §Failed below).
    An entered block the opening decided *teach* because it failed there (不会/没学过, 不知道,
    wrong: probe.failed_nodes, on the block or a block above it) passes that decision to every point under it, the ones its lazy split made since
    included (user ruling 2026-10-24, 有证据就不再问: 「怎么进入了还问啊，我都说了我不会了」): they
    are all fixed, so entering it asks nothing and plans its pages at once.
    The goal record (S1, D07): a block with `diagnose` False (a stated beginner's; an older 读懂
    goal's reading block) is decided *teach* with every point under it, at the opening and when entered: it is
    never asked about."""
    kv = w.kview()
    ev = set(draft.evidenced)
    names = {c.id: c.name for c in draft.concepts} | {c: n.name for c, n in w.state.names.items()}
    intake_pre = {c.id: c.prereqs for c in draft.concepts}
    p0 = {lv.node: lv.p0 for lv in draft.levels}
    opening = (_opening_decisions(w.state, m) if m is not None and draft.region_id is not None
               else {})

    trusted = w.state.graph.trusted_pairs()             # verified prerequisites only
    graph_pre: dict[str, list[str]] = {}
    for p, c in sorted(trusted):
        graph_pre.setdefault(c, []).append(p)
    soft_pre: dict[str, list[str]] = {}                  # recommended / unverified prerequisites
    for rel in w.state.graph.relations:
        if rel.kind in ("recommended", "prerequisite") and (rel.src, rel.dst) not in trusted:
            soft_pre.setdefault(rel.dst, []).append(rel.src)
    block_teach = False
    if m is not None and draft.region_id is not None:
        up, cur = [], m.node(draft.region_id)        # the draft's node and the blocks above it
        while cur is not None and cur.id not in up:
            up.append(cur.id)
            cur = m.node(cur.parent) if cur.parent is not None else None
        if any(opening.get(u) == "teach" for u in up):
            first = _opening(w.state, m)
            failed = failed_nodes(first.rounds) if first is not None else set()
            block_teach = any(opening.get(u) == "teach" and u in failed for u in up)
        # the goal record (S1): a block not to diagnose is taught as it is, its points too; so
        # is a block he marked 不会 in the opening's 摸底 grid (ask_rough)
        block_teach = block_teach or any(not n.diagnose for u in up
                                         if (n := m.node(u)) is not None)
        first_rough = _opening_rough(w.state, m)
        block_teach = block_teach or any(first_rough.get(u) == "dont" for u in up)

    def surmise(cid: str, ps: Sequence[str]) -> tuple[str, ...]:
        return tuple(p for p in dict.fromkeys(ps) if (p, cid) in trusted)

    def prereqs(cid: str, extra: Sequence[str] = ()) -> tuple[str, ...]:
        k_pre = w.k.prereqs(cid) if cid in w.k.concept_ids() else ()
        return surmise(cid, (*extra, *graph_pre.get(cid, ()), *intake_pre.get(cid, ()), *k_pre))

    def builds_on(cid: str, extra: Sequence[str] = ()) -> tuple[str, ...]:
        ps = (*extra, *soft_pre.get(cid, ()), *intake_pre.get(cid, ()))
        return tuple(p for p in dict.fromkeys(ps) if p != cid and (p, cid) not in trusted)

    def concept(cid: str, name: str, *, target: bool = False, scope: str = "",
                extra: Sequence[str] = (), known: bool = False,
                teach: bool = False) -> ProbeNode:
        cells = _node_cells(kv, [cid])
        decided = opening.get(cid) or ("teach" if block_teach or teach else None)
        return ProbeNode(id=cid, name=name, cells=cells, target=target, scope=scope,
                         prereqs=prereqs(cid, extra), builds_on=builds_on(cid, extra),
                         evidenced=(cid,) if cid in ev or decided else (),
                         k_prior="known" if known else _k_prior(kv, ev, [cid]),
                         decided=decided, p0=p0.get(cid))

    if m is None:
        return [concept(c.id, c.name, target=c.target) for c in draft.concepts]
    members: list[ProbeNode] = []
    if draft.region_id is None:
        nodes = list(m.children(None))
        regions = [n for n in nodes if n.kind == "region"]
        seen = {n.id for n in nodes}

        def walk(r: MapNode, known: bool, teach: bool) -> None:
            for c in r.members:
                if c in seen:
                    continue
                seen.add(c)
                mn = m.node(c)
                members.append(concept(c, names.get(c, c), extra=mn.prereqs if mn else (),
                                       target=bool(mn and mn.target), known=known,
                                       teach=teach))
                if mn is not None and mn.kind == "region":      # a nested region's parts too
                    walk(mn, known or block_known(m, mn, kv), teach or not mn.diagnose)
        for r in regions:
            walk(r, block_known(m, r, kv), not r.diagnose or draft.rough.get(r.id) == "dont")
    else:
        region = _draft_region(m, draft)
        if region.members:                  # its knowledge points, wherever they sit on the map
            nodes = [m.node(c) or MapNode(id=c, kind="concept", name=names.get(c, c),
                                          state="expanded") for c in region.members]
        else:                                 # an older map's region: its expansion
            nodes = list(m.children(draft.region_id))
            if not nodes or any(c.id == region.id for c in draft.concepts):
                nodes.append(region)          # that region is a K concept itself
    item_ids = {n.id for n in nodes} | {p.id for p in members}
    out = []
    for n in nodes:
        if n.kind == "concept":
            out.append(concept(n.id, n.name, target=n.target, scope=n.scope, extra=n.prereqs))
            continue
        cells = _node_cells(kv, covered(m, n))
        concepts = tuple(dict.fromkeys(c.rpartition(".")[0] for c in cells))
        known = block_known(m, n, kv)
        evidenced = concepts if known else tuple(c for c in concepts if c in ev)
        if known:
            prior = "known"
        elif any(c in item_ids for c in n.members):    # its members are items of their own
            prior = _k_prior(kv, ev, [n.id]) if n.id in w.k.concept_ids() else None
        else:
            prior = _k_prior(kv, ev, concepts)
        out.append(ProbeNode(id=n.id, name=n.name, cells=cells, scope=n.scope, region=True,
                             target=n.target,
                             prereqs=surmise(n.id, (*n.prereqs, *graph_pre.get(n.id, ()))),
                             builds_on=builds_on(n.id, n.prereqs),
                             members=tuple((c, names.get(c, c)) for c in n.members),
                             evidenced=evidenced, k_prior=prior, p0=p0.get(n.id),
                             # the goal record (S1): a block not to diagnose is taught as it
                             # is; so is one he marked 不会 in the 摸底 grid
                             decided=None if known or (n.diagnose
                                                       and draft.rough.get(n.id) != "dont")
                             else "teach"))
    return [*out, *members]


REGION_GONE = "这一块已经不在学习地图上了，这次摸底没法接着做"


def _draft_region(m: GoalMap, draft: Draft) -> MapNode:
    """A region draft's region on its map; gone (an older map edit removed it) → WikiConflict
    (fail closed: map_edits refuses to drop / rename / reorder a region whose diagnosis is
    open, so only older data gets here)."""
    region = m.node(draft.region_id or "")
    if region is None:
        raise WikiConflict(REGION_GONE)
    return region


def _opening(state: WikiState, m: GoalMap) -> Draft | None:
    """The map's opening diagnosis (the draft over its regions), if it asked anything: the latest
    one (a map replaced by the same goal's new blocks has a newer opening)."""
    return next((d for d in reversed(list(state.drafts.values()))
                 if d.map_id == m.map_id and d.region_id is None and d.rounds), None)


def _opening_rough(state: WikiState, m: GoalMap) -> dict[str, str]:
    """The 摸底 grid's marks of the map's opening diagnosis (the latest one with marks)."""
    d = next((d for d in reversed(list(state.drafts.values()))
              if d.map_id == m.map_id and d.region_id is None and d.rough), None)
    return dict(d.rough) if d is not None else {}


def _opening_decisions(state: WikiState, m: GoalMap) -> dict[str, str]:
    """What the map's opening diagnosis decided per node and that an entered region keeps as it
    is (有证据就不再问; teaching corrects it): the decisions of its last round that has any (the
    final ones once it ended), for every node decided from an answer — a quiz, a self-report, K —
    or inferred from answers through the prerequisite structure. Nodes it had no answer on
    (source none, or only the description's prior: a description is not an answer) are left
    open and may be asked. An older map without decisions: nothing."""
    d = _opening(state, m)
    rounds = d.rounds if d is not None else ()
    last: tuple[NodeDecision, ...] = next((r.decisions for r in reversed(rounds)
                                           if r.decisions), ())
    return {x.node: x.decision for x in last if x.source not in ("none", "described")}


def _grid_submitted(draft: Draft) -> bool:
    """The 摸底 grid came back through its 提交 (a typed answer's call is marked: he may still mark the rest)."""
    return any(not c.startswith("typed:") for c in draft.rough_calls)


def _probe(w: _Work, draft: Draft, m: GoalMap | None, rounds: Sequence[ProbeRound]) -> ProbeRound:
    """The next round over the draft's own points: the KST pick (its cell included) or the end.
    No model call: the assistant writes the question in the chat (stage 3 T11)."""
    # marked 掌握 / 熟悉 on the 摸底 grid: verified with a quiz, never self-reported again; a block he left blank on a
    # submitted grid was asked already, its one-by-one self-report would be the same question again
    blank = ([n.id for n in m.children(None) if n.kind == "region" and n.diagnose]
             if m is not None and draft.region_id is None and _grid_submitted(draft) else [])
    return next_round(nodes=_probe_nodes(w, draft, m), k=w.kview(), rounds=rounds,
                      described=_described_of(draft),
                      pick_id=f"{draft.draft_id}:{len(rounds) + 1}",
                      rough=[*blank, *(b for b, v in draft.rough.items() if v != "dont")])


def _described_of(draft: Draft) -> dict[str, str] | tuple[str, ...]:
    """concept id → what the description said; a draft from before probe@v4 has the ids only."""
    if len(draft.described_as) == len(draft.described) and draft.described:
        return dict(zip(draft.described, draft.described_as, strict=True))
    return draft.described


def _names(w: _Work, draft: Draft, m: GoalMap | None) -> dict[str, str]:
    out = {c: n.name for c, n in w.state.names.items()} | {c.id: c.name for c in draft.concepts}
    return out | ({n.id: n.name for n in m.nodes} if m is not None else {})


def _log_lines(m: GoalMap, lines: Sequence[str]) -> GoalMap:
    return m.model_copy(update={"log": (*m.log, *lines)}) if lines else m


def _round_log(w: _Work, draft: Draft, m: GoalMap, r: ProbeRound, answers: Sequence[int],
               round_index: int) -> GoalMap:
    """The map with a log line per answered question (round, kind, target node, result)."""
    names = _names(w, draft, m)
    return _log_lines(m, [question_line(round_index, q, a, names)
                          for q, a in zip(r.questions, answers, strict=True)])


def _decision_log(m: GoalMap, r: ProbeRound) -> GoalMap:
    """The done round's decisions go to the map log. Nothing else on the map changes: whether a
    region is known is K's (`block_known`), the answers already reached K (`_answer_events`)."""
    return _log_lines(m, decision_lines(r)) if r.done else m


def _save_map(w: _Work, before: GoalMap, after: GoalMap, action) -> GoalMap:
    if before != after:
        w.add(GoalMapSaved(record=after, action=action))
    return after


# --- finish ----------------------------------------------------------------------------------

def _sinks(concepts: Sequence[IntakeConcept]) -> tuple[str, ...]:
    """Concepts nothing else among `concepts` builds on (the fallback targets)."""
    ids = {c.id for c in concepts}
    used = {p for c in concepts for p in c.prereqs if p in ids}
    return tuple(c.id for c in concepts if c.id not in used) or (concepts[-1].id,)


def order_relations(state: WikiState, ids) -> tuple[IntakeRelation, ...]:
    """The graph's prerequisite and recommended relations among `ids`, with `verified`: the
    path's ordering edges (goals.path: hard only when a verified prerequisite, else soft)."""
    ids = set(ids)
    return tuple(IntakeRelation(kind=r.kind, src=r.src, dst=r.dst, phrase=r.phrase,
                                verified=r.verified)
                 for r in state.graph.relations
                 if r.kind in ("prerequisite", "recommended") and r.src in ids and r.dst in ids)


def _path_for(w: _Work, draft: Draft, concepts: Sequence[IntakeConcept],
              targets: Sequence[str]) -> tuple[str, LearningPath | None]:
    """The path id, and a freshly planned path (None: a path with this goal key is stored). The
    path model sees the user's existing pages and paths; the order sees the graph's prerequisite
    and recommended edges among the concepts (`order_relations`)."""
    res = IntakeResult(goal=draft.goal, title=draft.title, concepts=tuple(concepts),
                       targets=tuple(targets), observations=(), dropped=(), resolutions=(),
                       relations=order_relations(w.state, (c.id for c in concepts)))
    key_id = f"path-{goal_key(draft.goal, res.targets)}"
    if key_id in w.state.paths:
        return key_id, None
    return key_id, traced_plan(w.user_id, key_id, w.llm(), lambda llm: plan_path(
        res, w.kview(), llm, language=w.state.language, existing=_existing_lines(w.state),
        taught=_taught(w.state)))


def _save_path(w: _Work, draft: Draft, path: LearningPath) -> None:
    w.add(PathSaved(record=PathRecord(path=path,
                                      page_ids=(None,) * len(path.pages), created_at=w.now,
                                      sources=draft.sources),
                    action="planned"))


def _finish(w: _Work, draft: Draft, m: GoalMap | None, *, stored: bool,
            outcome: str = "answered") -> DraftResult:
    """The draft's outcome (module doc); `stored`: the draft is in the log and gets closed."""
    path_id = map_id = None
    if m is not None and draft.region_id is None:
        map_id = m.map_id
    elif m is not None:
        region = _draft_region(m, draft)
        subs = {n.id for n in m.children(region.id) if n.kind == "region"}
        concepts = [c for c in draft.concepts if c.id not in subs]
        if concepts:
            ids = {c.id for c in concepts}
            targets = [t for t in draft.targets if t in ids] or list(_sinks(concepts))
            path_id, path = _path_for(w, draft, concepts, targets)
            if path is not None:
                _save_path(w, draft, path)
            _save_map(w, m, m.replace(region.model_copy(update={"path_id": path_id})),
                      "path_planned")
        else:
            map_id = m.map_id                     # only sub-regions: those are entered next
    else:                                         # a draft with no map: refused at load
        raise WikiConflict(OLD_DRAFT)
    if stored:
        w.add(DraftClosed(draft_id=draft.draft_id, outcome=outcome, path_id=path_id,
                          map_id=map_id))
    return DraftResult(draft=draft, page_id=None, calls=w.calls, path_id=path_id, map_id=map_id)


def _open_or_finish(w: _Work, draft: Draft, m: GoalMap | None, r: ProbeRound) -> DraftResult:
    """A first round with a pick stores the draft; a done one finishes at once."""
    draft = draft.model_copy(update={"rounds": (r,)})
    if not r.done:
        w.add(DraftCreated(draft=draft))
        return DraftResult(draft=draft, page_id=None, calls=w.calls, open=True)
    return _finish(w, draft, m, stored=False)


# --- map: regions are graph nodes -----------------------------------------------------------

def _local_id(map_id: str, name: str) -> str:
    return "rg-" + hashlib.sha256(f"{map_id}\n{name}".encode()).hexdigest()[:10]


def _goal_graph(w: _Work, res: IntakeResult, final: dict[str, str], *,
                parent: tuple[str, str] | None = None) -> None:
    """Write this goal's nodes and relations into the graph (`GraphSaved(action="goal_nodes")`
    with its K and name events; nothing when the graph does not change or the user is not
    converted yet): every intake node is a node (named as its K definition; a new node takes the
    model's definition), every region (`final` ids) a node; the intake's relations go in with their
    kinds and phrases (part_of / recommended / kind_of / related, source "intake", unverified), then what an
    older result implies (members `part_of` their region, region and node prerequisites
    `prerequisite`, phrase ""; a relation already there keeps its phrase). `parent` (id, name): a
    region being expanded by a scoped intake — every first-level node of the outline (a node
    with no parent in the result) `part_of` it; deeper nodes keep their own parents. A member or prerequisite
    the graph lacks is added from the recorded names; what the graph cannot hold (unknown id,
    cycle) is skipped and logged."""
    g = w.state.graph
    nodes = {n.id: n for n in g.nodes}

    def put(node_id: str, name: str, *, rename: bool) -> None:
        old = nodes.get(node_id)
        if old is None and name.strip():
            nodes[node_id] = NodeRec(id=node_id, name=name)
        elif old is not None and rename and name.strip() and old.name != name:
            nodes[node_id] = old.model_copy(update={"name": name})

    for c in res.concepts:
        put(c.id, c.name, rename=True)
        if c.definition and nodes.get(c.id) is not None and not nodes[c.id].definition:
            nodes[c.id] = nodes[c.id].model_copy(update={"definition": c.definition})
    for r in res.regions:
        put(final[r.id], r.name, rename=False)
    if parent is not None:
        put(parent[0], parent[1], rename=False)

    def rel(kind, src: str, dst: str, phrase: str = "") -> Relation:
        for end in (src, dst):
            if end not in nodes and end in w.state.names:
                put(end, w.state.names[end].name, rename=False)
        return Relation(kind=kind, src=src, dst=dst, phrase=phrase, source="intake")

    rels = [rel(r.kind, final.get(r.src, r.src), final.get(r.dst, r.dst), r.phrase)
            for r in res.relations]
    rels += [rel("part_of", m, final[r.id]) for r in res.regions for m in r.members]
    rels += [rel("prerequisite", final[p], final[r.id]) for r in res.regions for p in r.prereqs
             if final.get(p, p) != final[r.id]]
    rels += [rel("prerequisite", p, c.id) for c in res.concepts for p in c.prereqs]
    if parent is not None:
        has_parent = {r.src for r in res.relations if r.kind == "part_of"}
        rels += [rel("part_of", c.id, parent[0]) for c in res.concepts
                 if c.id != parent[0] and c.id not in has_parent]
    graph, skipped = add_relations(g.updated(nodes=nodes.values()), rels)
    for x in skipped:
        log.warning("goal nodes for %s skipped %s", w.user_id, x)
    if same_structure(graph, g):
        return
    aspect, wiki = graph_events(w.state, w.k, graph, "goal_nodes", now=w.now, new_id=w.new_id)
    w.add_aspect(aspect)
    w.add_events(wiki)


def _same_name(a: str, b: str) -> bool:
    return bool(norm_name(a)) and norm_name(a) == norm_name(b)


def _prior_region(state: WikiState, node: MapNode) -> MapNode:
    """What another map already decided for this region id: being learned with its path / page
    (known is K's, never carried over)."""
    prior = _latest_nodes(state).get(node.id)
    if prior is None or prior[1].kind != "region":
        return node
    pn = prior[1]
    if pn.path_id is not None or pn.page_id is not None:
        return node.model_copy(update={"state": "expanded", "path_id": pn.path_id,
                                       "page_id": pn.page_id})
    return node


def _open_draft(state: WikiState, map_id: str, region_id: str | None) -> Draft | None:
    return next((d for d in state.drafts.values()
                 if d.map_id == map_id and d.region_id == region_id
                 and d.draft_id not in state.closed_drafts), None)


def _source_goal(goal: str, sources: Sequence[SourceToc], use: SourceUse
                 ) -> tuple[str, tuple[str, ...]]:
    """The goal text S1 works on, and the words of his it replaced: with materials, understand is
    always 「读懂《标题》」 and learn with no text 「按《标题》学」 (titles joined by 、); what he did
    write stays his words (`GoalMap.words`)."""
    if not sources:
        return goal, ()
    titles = "、".join(t.title for t in sources)
    if use == "understand":
        text = f"读懂《{titles}》"
    elif not goal.strip():
        text = f"按《{titles}》学"
    else:
        return goal, ()
    return text, ((goal.strip(),) if goal.strip() else ())


def _block_ids(state: WikiState, k: AspectK, goal: str, blocks: Sequence[Block]
               ) -> list[tuple[str, bool]]:
    """Per block, its graph node id and whether the node already existed. A block reuses an
    existing node ONLY when the model wrote its `existing_id` (it saw the node's parts in the
    prompt) and the wiki has it. Otherwise, in order: the region of the same name on a map of
    this same goal (the same goal again gets the same ids, so the same map); a goal-local
    `rg-…` id when the name equals some other node's (scoped by this goal: two goals' 「繁殖」
    are two nodes — the 斗鱼 / 黑壳虾 collision, where a name match took over another goal's
    node with its path and K); else a new `c<k>` id (as intake numbers new nodes: after the
    existing concepts, skipping taken ids). The intake path's duplicate guard is unchanged."""
    names = {n.id: n.name for n in state.graph.nodes} | {c: n.name for c, n in state.names.items()}
    taken = set(names) | set(k.concept_ids())
    own = {norm_name(n.name): n.id for m in state.maps.values() if _same_name(m.goal, goal)
           for n in m.nodes if n.kind == "region" and n.parent is None}
    scope = f"goal-{goal_key(goal, ())}"
    nxt = len(existing_refs(state, k)) + 1
    out: list[tuple[str, bool]] = []
    for b in blocks:
        if b.existing_id and b.existing_id in names:
            out.append((b.existing_id, True))
            continue
        if (mine := own.get(norm_name(b.name))) is not None:
            out.append((mine, mine in names))
            continue
        if any(_same_name(n, b.name) for n in names.values()):
            local = _local_id(scope, b.name)
            out.append((local, local in names))
            continue
        while f"c{nxt}" in taken:
            nxt += 1
        out.append((f"c{nxt}", False))
        taken.add(f"c{nxt}")
    return out


GOAL_IN_PROGRESS = "这个目标已经在学了：在学习路径里改分块"
# a draft from before S1 (fixed questions, or no goal map): none is answered or read back any more
OLD_DRAFT = "这是旧版的草稿，请重新开始这个目标"


def _map_progress(state: WikiState, m: GoalMap, kv: AspectKView) -> bool:
    """Has learning on this map begun: a node with a path / page, a region entered (expanded) or
    known (K: `block_known`), an open draft other than its opening (a region's diagnosis), or a
    unit of its reading entered (a 读懂 goal: the unit was analyzed — 「看这一段」 paid for and
    wrote its points — or has a page, reading-mode spec §3.4)? A concept node alone is not
    progress: an older 读懂 goal's reading leaves are on its map from the start."""
    reading = state.reading_of(m.map_id)
    if reading is not None and any(u.analyzed or u.page_id is not None for u in reading.units):
        return True
    if any(n.path_id is not None or n.page_id is not None
           or (n.kind == "region" and (n.state != "collapsed" or block_known(m, n, kv)))
           for n in m.nodes):
        return True
    return any(d.map_id == m.map_id and d.region_id is not None
               and d.draft_id not in state.closed_drafts for d in state.drafts.values())


def _said_gaps(ids: Sequence[str], blocks: Sequence[Block], words: Sequence[str],
               concepts: Sequence[IntakeConcept], *, evidence_ref: str
               ) -> tuple[tuple[SkillGap, ...], tuple[Observation, ...], tuple[Any, ...]]:
    """His own words about a block (S1's `said` with its level; a stated beginner's on every block,
    D07): the diagnosis's prior for that block and a self-report on its cells, kept only when
    the words are his verbatim (`self_observations`, against `words` — never a model-written
    text). Nothing said: nothing."""
    # one self-report per block (its first quote; a merged block's other quotes stay on its node
    # as his words about it, and are not counted twice as evidence)
    raw = [{"concept": i, "level": b.said_level, "quote": b.said[0],
            "confidence": "high" if b.said_level == "unlearned" else "medium"}
           for i, b in zip(ids, blocks, strict=True) if b.said and b.said_level]
    if not raw:
        return (), (), ()
    gaps, obs = _skill_gaps(raw, concepts, set())
    valid = {cell_id(c.id, a) for c in concepts for a in c.aspects}
    kept, dropped = self_observations(obs, "\n".join(words), valid, evidence_ref=evidence_ref)
    return tuple(gaps), kept, dropped


GOAL_LEAD = re.compile(r"^(?:我?[要想]?学(?:习|点)?|(?i:(?:I (?:want|would like) to )?(?:learn|study|master))\b)\s*")


def same_goal(state: WikiState, *names: str) -> GoalMap | None:
    """A goal not taken back that `names` already name (his words or a title, without 我要学; case and
    spaces do not count). Reading goals are about one material, so they never match."""
    want = {norm_name(GOAL_LEAD.sub("", n.strip())) for n in names if n.strip()}
    for m in state.maps.values():
        gs = state.goal_settings.get(m.map_id)
        if gs is not None and gs.archived or state.reading_of(m.map_id) is not None:
            continue
        if want & {norm_name(GOAL_LEAD.sub("", x.strip())) for x in (m.goal, m.title) if x.strip()}:
            return m
    return None


@traced_entry("blocks", subject="goal")
def create_goal_from_blocks(repo: Repo, gw: CountingGateway, user_id: str, goal: str,
                            blocks: Sequence[Block], *, now: datetime, new_id: Ids,
                            words: Sequence[str] = (), limits: Sequence[str] = (),
                            beginner: str = "", title: str = "",
                            sources: Sequence[SourceToc] = (),
                            source_use: SourceUse = "learn",
                            material_use: GoalUse | None = None,
                            extra: Sequence[Callable[[Any], None]] = (),
                            on_commit: Callable[[Any, DraftResult], None] | None = None
                            ) -> DraftResult:
    """A goal from S1's blocks (spec 2026-10-23-chat-first-blocks; 2026-10-28 S1): the ones the
    learner confirmed in chat (the assistant's goal_new row). No intake call. The
    blocks become the map (`block_goal`: graph nodes, K concepts, top-level regions, the goal
    record, the same goal's replace / GOAL_IN_PROGRESS rules), then the first probe round over
    the blocks (top-level regions only; a block not to diagnose is never asked); the draft
    carries `map_id`. `material_use`: how the goal uses its materials (`GoalMap.material_use`;
    None = follow). The blocks' section labels (`Block.sections` / `exercises`, resolved against
    `sources` by `block_links`; an unknown label is only logged) are the result's `links` (by
    "blocks"). WikiInvalid: no block, or two blocks that are one node."""
    if not blocks:
        raise WikiInvalid("至少要有一块")
    p = block_goal(repo, gw, user_id, goal, blocks, now=now, new_id=new_id, words=words,
                   limits=limits, beginner=beginner, title=title, sources=sources,
                   source_use=source_use, material_use=material_use)
    w, m, draft = p.w, p.map, p.draft
    r = _probe(w, draft, m, ())
    m = _decision_log(m, r)
    w.add(GoalMapSaved(record=m, action="planned"))
    named, unknown = block_links(blocks, sources)
    if unknown:
        log.info("block sections not in the materials for %s: %s", user_id, unknown)
    links = tuple(NodeSectionLink(node_id=p.ids[at], section_id=sid, source_id=src, role=role,
                                  by="blocks") for at, sid, src, role in named)
    out = dataclasses.replace(_open_or_finish(w, draft, m, r), links=links)
    w.commit(repo, _with_result(extra, on_commit, out))
    return out


@dataclass(frozen=True)
class BlockGoal:
    """What `block_goal` prepared, uncommitted: the request's work (`w`: the K, name and graph
    events so far), the map (not saved yet: the caller logs the opening round's decisions on it
    first), the opening draft over its blocks (not stored yet) and each block's node id (in block
    order)."""

    w: _Work
    map: GoalMap
    draft: Draft
    ids: tuple[str, ...] = ()


@traced_entry("blocks", subject="goal")
def block_goal(repo: Repo, gw: CountingGateway | None, user_id: str, goal: str,
               blocks: Sequence[Block], *, now: datetime, new_id: Ids,
               words: Sequence[str] = (), limits: Sequence[str] = (), beginner: str = "",
               title: str = "", sources: Sequence[SourceToc] = (),
               source_use: SourceUse = "learn", material_use: GoalUse | None = None,
               map_id: str | None = None) -> BlockGoal:
    """The blocks part of a goal, shared by `create_goal_from_blocks` and the reading flow's
    prerequisite blocks (wikiapp/reading.py, reading-mode spec §3.4). No call. Each
    block is a graph node (new, or the node it reuses by `existing_id` only: `_block_ids`) and a
    K concept (so the opening diagnosis can ask about it); a reused node takes the block's name
    (a node rename, `GraphSaved("user_edit")` as the map card's 改名: the map and the wiki say the
    same); the map's regions are the blocks in this order, top-level (`parent=None`), with no
    members yet (`scope` = its covers joined by 、) — a block is split into pages only when it is
    entered (`enter_region`: siblings + one level); each region keeps its block's level word
    (`MapNode.level`, "" = none). Targets: the blocks marked
    target, none marked → all. The goal record (D09): `words`, `limits` and `beginner` (a stated
    beginner's words, D07: map_edits' 加一块 reads it) on the map, each
    block's `diagnose` / `said` on its node; his words about a block are a self-report in this
    commit (`_said_gaps`). `title`: S1's topic name ("" → the goal text).
    `map_id`: the map's id (default `map-{goal_key(goal, targets)}`). The same big goal again (that
    map exists): a map nobody has started learning (`_map_progress`) is REPLACED by these blocks
    (a new GoalMapSaved "planned" for the same map id; its opening, if still open, is closed as
    skipped); a map with progress → WikiConflict GOAL_IN_PROGRESS (its blocks are edited on
    学习路径). `sources` / `source_use`: the materials the goal is made from (their ids are
    recorded on the draft and the map); `material_use`: how the goal uses them
    (`GoalMap.material_use`; None = follow with materials, understand for a 读懂 goal). No blocks
    (a reading with no prerequisites): a map with no nodes. WikiInvalid: two blocks that are one node."""
    snap = repo.load(user_id)
    w = _Work(snap, gw, user_id, now=now, new_id=new_id)
    ids = _block_ids(snap.state, snap.k, goal, blocks)
    if len({i for i, _ in ids}) != len(ids):
        raise WikiInvalid("有两块其实是同一个点，请合并它们")
    marked = [i for (i, _), b in zip(ids, blocks, strict=True) if b.target]
    targets = tuple(marked or (i for i, _ in ids))
    map_id = map_id or f"map-{goal_key(goal, targets)}"
    old = snap.state.maps.get(map_id)
    settings = snap.state.goal_settings.get(map_id)
    if settings is not None and settings.archived:   # taken back, asked for again: the id is the words'
        w.add(GoalSettingsSet(settings=settings.model_copy(update={"archived": False})))
    if old is not None:
        if _map_progress(snap.state, old, AspectKView(snap.k, now)):
            raise WikiConflict(GOAL_IN_PROGRESS)
        opening = _open_draft(snap.state, map_id, None)
        if opening is not None:              # the replaced map's diagnosis: closed, never answered
            w.add(DraftClosed(draft_id=opening.draft_id, outcome="skipped", map_id=map_id))
    draft_id = new_id("dr")
    source_ids = tuple(t.source_id for t in sources)
    names = {c: n.name for c, n in snap.state.names.items()} | \
        {n.id: n.name for n in snap.state.graph.nodes}
    renamed = {i: b.name for (i, reused), b in zip(ids, blocks, strict=True)
               if reused and names.get(i) != b.name}
    if renamed and any(n.id in renamed for n in w.state.graph.nodes):
        g = w.state.graph
        aspect, wiki = graph_events(w.state, w.k, g.updated(nodes=[
            n.model_copy(update={"name": renamed[n.id]}) if n.id in renamed else n
            for n in g.nodes]), "user_edit", now=now, new_id=new_id)
        w.add_aspect(aspect)
        w.add_events(wiki)

    def aspects(i: str) -> tuple[Aspect, ...]:       # a new node: the aspects K will give it
        return snap.k.aspects(i) if i in snap.k.concept_ids() else DEFAULT_ASPECTS

    concepts = tuple(IntakeConcept(      # the blocks call's atomic (spec 2026-10-28 §2.2)
        id=i, name=b.name, aspects=aspects(i), definition=b.definition or b.summary, target=i in targets,
        reused=reused, atomic=b.atomic)
                     for (i, reused), b in zip(ids, blocks, strict=True))
    regions = tuple(IntakeRegion(id=i, name=b.name, members=(), scope="、".join(b.covers),
                                 target=i in targets, reused=reused)
                    for (i, reused), b in zip(ids, blocks, strict=True))
    # the learner's own words count as evidence (D07: 「完全零基础」 must not be asked six times),
    # checked against his words, never a model-written text. No call.
    gaps, observations, gap_dropped = _said_gaps(
        [i for i, _ in ids], blocks, words, concepts, evidence_ref=f"goal:{draft_id}")
    res = IntakeResult(goal=goal, title=title.strip() or goal, concepts=concepts, targets=targets,
                       observations=observations, dropped=gap_dropped, resolutions=(),
                       regions=regions, skill_gaps=gaps)
    kv_before = AspectKView(snap.k, now)
    if concepts:                            # no blocks: nothing to name (never an empty event)
        _intake_events(w, res)              # K concepts, names, his self-reports
    _goal_graph(w, res, {i: i for i, _ in ids})
    nodes = [_prior_region(snap.state, MapNode(
        id=r.id, kind="region", name=r.name, scope=r.scope, target=r.target, reused=r.reused,
        members=r.members, level=b.level, diagnose=b.diagnose, said=b.said))
        for r, b in zip(regions, blocks, strict=True)]
    m = GoalMap(map_id=map_id, goal=goal, created_at=now, title=res.title, nodes=tuple(nodes),
                sources=source_ids, words=tuple(words), limits=tuple(limits),
                beginner=beginner.strip(),
                material_use=(material_use or ("understand" if source_use == "understand"
                                               else "follow")) if sources else "")
    draft = _draft(draft_id, goal, res, map_id=map_id,
                   evidenced=_evidenced(kv_before, [i for i, _ in ids]), levels=_levels(res),
                   sources=source_ids, source_use=source_use if sources else None)
    return BlockGoal(w=w, map=m, draft=draft, ids=tuple(i for i, _ in ids))


def plan_goal_blocks(snap: Snapshot, gw: CountingGateway, user_id: str, goal: str,
                     words: Sequence[str] = (), *, now: datetime,
                     sources: Sequence[SourceToc] = (), use: SourceUse = "learn",
                     mode: MaterialMode = "follow", notes: str = "",
                     progress: Callable[[str], None] | None = None,
                     new_id: Ids | None = None) -> BlocksResult:
    """S1 「定目标」 over what the wiki has (every node with its parts, a hub's members, K's
    evidence, the relations), so the blocks name the same nodes `create_goal_from_blocks`
    resolves: `goals.blocks.plan_blocks`, one call. The goal text may be replaced by the
    materials' (`_source_goal`; what he wrote stays in his words). `words`: what he said for this
    goal beyond the goal itself (the chat card shows the learner-facing lines); `notes`: what the
    assistant found on the web (fenced); `new_id`: the reading units' ids (required for
    use="understand", reading-mode spec §3.2). Nothing is written. Calls and errors are
    plan_blocks' (IntakeError, the gateway's)."""
    goal, replaced = _source_goal(goal, sources, use)
    answering = (lambda: progress(READING_PLAN_STEP)) if progress and use == "understand" else None
    with on_reply_started(answering) if answering else nullcontext():
        return plan_blocks(GatewayJsonLLM(require(gw, user_id), subject=user_id), goal,
                           (*replaced, *words), existing_refs(snap.state, snap.k),
                           language=snap.state.language,
                           shown=related_nodes(snap.state, goal, words=(*replaced, *words)),
                           annotations=_annotations(snap.state, AspectKView(snap.k, now)),
                           topics=_topics(snap.state), graph_relations=_graph_relations(snap.state),
                           sources=sources, use=use, mode=mode, notes=notes,
                           progress=progress, new_id=new_id)


def _with_result(extra: Sequence[Callable[[Any], None]],
                 on_commit: Callable[[Any, DraftResult], None] | None,
                 res: DraftResult) -> list[Callable[[Any], None]]:
    return [*extra] if on_commit is None else [*extra, lambda conn: on_commit(conn, res)]


def draft_status(repo: Repo, user_id: str, draft_id: str) -> DraftResult:
    """GET /wiki/goals/{draft_id} (the learn view after the assistant created the goal): an open
    draft with its current round, or what it finished as — read back from what the state keeps
    (the closed draft's outcome is not stored on its own): a map draft → its map; a region draft
    → the region's path or page on the map (only sub-regions left → the map). 0 LLM calls.
    LookupError: no such draft; WikiConflict: a draft from before S1 (OLD_DRAFT), or a result the
    state no longer has (fail closed: never a guessed answer)."""
    st = repo.load(user_id).state
    draft = st.drafts.get(draft_id)
    if draft is None:
        raise LookupError(f"没有这个草稿：{draft_id}")
    if not draft.rounds or draft.map_id is None:
        raise WikiConflict(OLD_DRAFT)
    if draft_id not in st.closed_drafts:
        return DraftResult(draft=draft, page_id=None, calls=0, open=True)
    m = st.maps.get(draft.map_id)
    if m is not None and draft.region_id is None:
        return DraftResult(draft=draft, page_id=None, calls=0, map_id=m.map_id)
    if m is not None:
        region = m.node(draft.region_id or "")
        if region is not None and (region.path_id or region.page_id):
            return DraftResult(draft=draft, page_id=None if region.path_id else region.page_id,
                               calls=0, path_id=region.path_id)
        if region is not None:
            return DraftResult(draft=draft, page_id=None, calls=0, map_id=m.map_id)
    raise WikiConflict("这个草稿的结果已经不在了")


def _load_open(snap: Snapshot, draft_id: str) -> Draft:
    draft = snap.state.drafts.get(draft_id)
    if draft is None:
        raise LookupError(f"没有这个草稿：{draft_id}")
    if draft_id in snap.state.closed_drafts:
        raise WikiConflict("这个草稿已经答过或跳过了")
    if not draft.rounds or draft.map_id is None:
        raise WikiConflict(OLD_DRAFT)
    return draft


def _answer_events(w: _Work, draft: Draft, r: ProbeRound, answers: Sequence[int],
                   round_index: int) -> None:
    m = w.state.maps.get(draft.map_id) if draft.map_id else None
    cells = {n.id: n.cells for n in _probe_nodes(w, draft, m)}
    obs = probe_observations(r, answers, draft_id=draft.draft_id, round_index=round_index,
                             cells=cells)
    by_ref: dict[str, tuple[ProbeQuestion, int]] = {}
    for i, qa in enumerate(zip(r.questions, answers, strict=True)):
        for prefix in ("probe", "probeself"):
            by_ref[f"{prefix}:{draft.draft_id}:{round_index}:{i}"] = qa

    def text(o: Observation) -> str:
        q, a = by_ref[o.evidence_ref]
        return q.stem if a == IDK else q.options[a]

    def label(o: Observation) -> str:
        q, a = by_ref[o.evidence_ref]
        return answer_label(q.stem, IDK_LABEL if a == IDK else answer_text(q, a))

    w.add_aspect(observation_events(obs, ts=w.now, new_id=w.new_id, note_text=text,
                                    note_label=label))


def answer_check(repo: Repo, gw: CountingGateway | None, user_id: str, draft_id: str,
                 pick_id: str, question: ProbeQuestion, answer: int, *, now: datetime,
                 new_id: Ids) -> DraftResult:
    """The learner's answer (-1 = 不知道) to the question the assistant wrote on the draft's open
    pick (stage 3 T11): evidence on the pick's cell(s), then the next pick or the end (finished: the
    path / map / page, as before). No model call here, but finishing may plan a path (goals.path).
    LookupError: no such draft; WikiConflict: closed, or `pick_id` is not the open pick (answered
    already, or an older one); WikiInvalid: the question does not fit the pick or the answer is
    out of range."""
    snap = repo.load(user_id)
    draft = _load_open(snap, draft_id)
    r = draft.rounds[-1] if draft.rounds else None
    if r is None or r.done or r.pick is None or r.answers is not None:
        raise WikiConflict("这个摸底现在没有在等的题")
    if r.pick.pick_id != pick_id:
        raise WikiConflict("这道题已经问过了，或者不是现在这一题")
    if question.node != r.pick.node or (r.pick.cell and question.cell != r.pick.cell):
        raise WikiInvalid("这道题不是这一轮选中的点")
    asked = r.model_copy(update={"questions": (question,)})
    if (problem := check_answers(asked, (answer,))) is not None:
        raise WikiInvalid(problem)
    w = _Work(snap, gw, user_id, now=now, new_id=new_id)
    index = len(draft.rounds)
    _answer_events(w, draft, asked, (answer,), index)
    m = w.state.maps.get(draft.map_id) if draft.map_id else None
    rounds = (*draft.rounds[:-1], asked.model_copy(update={"answers": (answer,)}))
    nxt = _probe(w, draft, m, rounds)
    w.add(ProbeAnswered(draft_id=draft_id, round_index=index, answers=(answer,),
                        next_round=nxt, questions=(question,)))
    if m is not None:
        after = _round_log(w, draft, m, asked, (answer,), index)
        m = _save_map(w, m, _decision_log(after, nxt), "diagnosed")
    draft = w.state.drafts[draft_id]
    answered = ((question, answer),)
    if not nxt.done:
        out = DraftResult(draft=draft, page_id=None, calls=w.calls, open=True, answered=answered)
    else:
        res = _finish(w, draft, m, stored=True)
        out = DraftResult(draft=draft, page_id=res.page_id, calls=w.calls, path_id=res.path_id,
                          map_id=res.map_id, answered=answered)
    w.commit(repo)
    return out


# --- the 摸底 grid (ask_rough) ----------------------------------------------------------------

ROUGH_QUOTE = {"know": "掌握", "partly": "熟悉", "forgot": "学过但忘了", "dont": "不会"}


def _open_round(draft: Draft) -> ProbeRound | None:
    r = draft.rounds[-1] if draft.rounds else None
    return r if r is not None and not r.done and r.pick is not None and r.answers is None else None


def known_before(kv: AspectKView, m: GoalMap, n: MapNode) -> bool:
    """K already has his word or an answer on this block (a shared block of an earlier goal): it comes in as it is, never asked again."""
    return any(_has_evidence(kv, c) for c in covered(m, n))


def rough_blocks(snap: Snapshot, draft_id: str | None = None, now: datetime | None = None
                 ) -> tuple[str, list[tuple[str, str]]] | None:
    """The open diagnosis (`draft_id`, else the newest open one with a pick) and the blocks its
    摸底 grid shows: the goal map's top-level blocks the goal record diagnoses (`diagnose`) and K has no
    evidence on, for the map's opening diagnosis only: what he marked, typed or was asked never comes back; what he left
    does (he went elsewhere and came back, user ruling 2026-11-05). None: no open diagnosis; an empty list: nothing to
    mark (a stated beginner's goal, a block's own diagnosis, every block answered)."""
    state = snap.state
    ids = [draft_id] if draft_id else list(reversed(list(state.drafts)))
    for d in ids:
        draft = state.drafts.get(d)
        if draft is None or d in state.closed_drafts or _open_round(draft) is None:
            continue
        m = state.maps.get(draft.map_id) if draft.map_id else None
        if m is None or draft.region_id is not None:
            return d, []
        kv = snap.kview(now or datetime.now(UTC))
        asked = {r.pick.node for r in draft.rounds if r.pick is not None and r.answers is not None}
        return d, [(n.id, n.name) for n in m.children(None)
                   if n.kind == "region" and n.diagnose and n.id not in draft.rough and n.id not in asked and not known_before(kv, m, n)]
    return None


def record_rough(repo: Repo, gw: CountingGateway | None, user_id: str, draft_id: str,
                 call_id: str, marks: Mapping[str, str], *, names: Mapping[str, str] | None = None,
                 now: datetime, new_id: Ids) -> DraftResult:
    """His 摸底 grid (ask_rough call `call_id`): each marked block is one self-report on the cells
    it covers (the self-report table: know → can_do, partly → partly, dont → never; quote = the
    mark he chose), the marks are kept on the draft (a block marked dont is taught as it is: no
    question about it or its points), and the open round is picked again. Finished (every block
    decided): the draft's outcome, as answer_check. Idempotent per call id (a second call with
    the same id records nothing). LookupError: no such draft; WikiConflict: no open pick;
    WikiInvalid: a mark on something that is not one of the grid's blocks."""
    snap = repo.load(user_id)
    seen = snap.state.drafts.get(draft_id)
    if seen is not None and call_id in seen.rough_calls:
        return DraftResult(draft=seen, page_id=None, calls=0,
                           open=draft_id not in snap.state.closed_drafts)
    draft = _load_open(snap, draft_id)
    if _open_round(draft) is None:
        raise WikiConflict("这个摸底现在没有在等的题")
    m = snap.state.maps.get(draft.map_id) if draft.map_id else None
    if m is None or draft.region_id is not None:
        raise WikiInvalid("这个摸底没有摸底表")
    blocks = {n.id: n for n in m.children(None) if n.kind == "region" and n.diagnose}
    bad = [b for b, v in marks.items() if b not in blocks or v not in ROUGH_QUOTE]
    if bad:
        raise WikiInvalid(f"摸底表里没有这一块：{bad[0]}")
    w = _Work(snap, gw, user_id, now=now, new_id=new_id)
    kv = w.kview()
    obs = []
    for b, v in marks.items():
        cells = _node_cells(kv, covered(m, blocks[b]))
        obs += self_report_observations(MEANS_CLAIM[v], cells, quote=ROUGH_QUOTE[v],
                                        evidence_ref=f"rough:{draft_id}:{call_id}:{b}")
    names = names or {}
    labels = {o.evidence_ref: f"摸底：{names.get(o.evidence_ref.rsplit(':', 1)[1], '')}"
              for o in obs}
    w.add_aspect(observation_events(obs, ts=now, new_id=new_id,
                                    note_label=lambda o: labels[o.evidence_ref]))
    marked = draft.model_copy(update={"rough": {**draft.rough, **marks}, "rough_calls": (*draft.rough_calls, call_id)})
    nxt = _probe(w, marked, m, draft.rounds[:-1])
    w.add(RoughMarked(draft_id=draft_id, call_id=call_id, marks=dict(marks), next_round=nxt))
    draft = w.state.drafts[draft_id]
    if not nxt.done:
        out = DraftResult(draft=draft, page_id=None, calls=w.calls, open=True)
    else:
        m = w.state.maps.get(draft.map_id) if draft.map_id else None
        res = _finish(w, draft, m, stored=True)
        out = DraftResult(draft=draft, page_id=res.page_id, calls=w.calls, path_id=res.path_id,
                          map_id=res.map_id)
    w.commit(repo)
    return out


NO_PROBE_LINE = "他说不用摸底，这个目标后面也不再问"
DIAGNOSIS_OPEN = "这个目标的摸底还没做完，答完或跳过才能开始学"


def stop_draft(repo: Repo, gw: CountingGateway | None, user_id: str, draft_id: str, *,
               now: datetime, new_id: Ids) -> DraftResult:
    """「够了，开始学」 / skip: finish now, no evidence from the open round (`_load_open`'s
    refusals)."""
    snap = repo.load(user_id)
    draft = _load_open(snap, draft_id)
    w = _Work(snap, gw, user_id, now=now, new_id=new_id)
    m = w.state.maps.get(draft.map_id) if draft.map_id else None
    if m is not None and NO_PROBE_LINE not in m.log:
        m = _save_map(w, m, _log_lines(m, [NO_PROBE_LINE]), "expanded")
    out = _finish(w, draft, m, stored=True, outcome="stopped")
    w.commit(repo)
    return out


# --- entering a node (on-demand decomposition, spec 2026-10-28 §2.2) ----------------------------

def _ancestry(m: GoalMap, node: MapNode) -> tuple[tuple[str, str], ...]:
    """(id, name) of the node's ancestors on the map, nearest first."""
    out: list[tuple[str, str]] = []
    cur = m.node(node.parent) if node.parent is not None else None
    while cur is not None and all(cur.id != i for i, _ in out) and cur.id != node.id:
        out.append((cur.id, cur.name))
        cur = m.node(cur.parent) if cur.parent is not None else None
    return tuple(out)


def _covers(state: WikiState, n: MapNode) -> str:
    """What a node covers as the split prompt shows it: a block's covers, else its definition."""
    rec = state.graph.node(n.id)
    return n.scope or (rec.definition if rec is not None else "")


def _scope_for(state: WikiState, m: GoalMap, node: MapNode) -> IntakeScope:
    """The one-level split of `node` (intake@v31): its own level as siblings (the goal's
    top-level blocks for a block), the blocks above it as ancestors."""
    level = ([n for n in region_order(m) if n.parent is None] if node.parent is None
             else ordered(m.children(node.parent)))
    return IntakeScope(goal=f"{m.goal} · {node.name}", region_id=node.id, region_name=node.name,
                       scope=_covers(state, node), parent_goal=m.goal,
                       siblings=tuple((n.name, _covers(state, n)) for n in level),
                       ancestors=_ancestry(m, node))


def _estimate_words(m: GoalMap, node: MapNode) -> tuple[str, ...]:
    """His own words S2 estimates from when it splits `node` (enter@v2): what he said about it
    (`said`), then what he said for the goal (`GoalMap.words`), each once. None — so nothing is
    estimated or asked (D07) — on a stated beginner's map (`beginner`) or when the node or a
    block above it is not to be diagnosed (`diagnose` False)."""
    if m.beginner or not node.diagnose or any(
            (n := m.node(i)) is not None and not n.diagnose for i, _ in _ancestry(m, node)):
        return ()
    return tuple(dict.fromkeys(x for x in (*node.said, *m.words) if x.strip()))


def _child_said(res: IntakeResult) -> dict[str, tuple[str, ...]]:
    """Per new part, his quotes the estimate kept on its cells (verbatim, in order): the part's
    `said`, so entering it later estimates its own parts from the same words."""
    out: dict[str, list[str]] = {}
    for o in res.observations:
        quotes = out.setdefault(o.cell.rpartition(".")[0], [])
        if o.quote and o.quote not in quotes:
            quotes.append(o.quote)
    return {c: tuple(q) for c, q in out.items()}


def _show_children(w: _Work, m: GoalMap, node: MapNode, ids: Sequence[str],
                   names: dict[str, str] | None = None,
                   said: dict[str, tuple[str, ...]] | None = None,
                   scopes: dict[str, str] | None = None) -> GoalMap:
    """The node entered on this map with these children under it (those not on it yet; `said`:
    his quotes per new child; `scopes`: what a reused child is about in this goal)."""
    kids, log = node_children(w.state, m, node, ids, names, said=said, scopes=scopes)
    after = m.replace(node.model_copy(update={"state": "expanded"}), *kids)
    return _save_map(w, m, _log_lines(after, log), "expanded")


def _point_prereqs(w: _Work, node_id: str) -> list[str]:
    """What a point needs first that K does not show learned: the graph's prerequisite edges
    into it (part_of and recommended do not count)."""
    kv = w.kview()
    out = []
    for p in dict.fromkeys(r.src for r in w.state.graph.relations
                           if r.kind == "prerequisite" and r.dst == node_id and r.src != node_id):
        cells = [cell_id(p, a) for a in kv.aspects(p)]
        if not cells or not all(kv.cell(c).band in LEARNED_BANDS for c in cells):
            out.append(p)
    return out


def _point_concept(w: _Work, m: GoalMap, cid: str, *, target: bool) -> IntakeConcept:
    rec = w.state.graph.node(cid)
    here = m.node(cid)
    scope = here.scope if here is not None and here.parent is not None else ""
    name = w.state.names[cid].name if cid in w.state.names else rec.name if rec else cid
    return IntakeConcept(
        id=cid, name=name, aspects=w.k.aspects(cid) if cid in w.k.concept_ids() else (),
        prereqs=w.k.prereqs(cid) if cid in w.k.concept_ids() else (),
        subitems=w.state.names[cid].subitems if cid in w.state.names else (),
        definition=scope or (rec.definition if rec is not None else ""), target=target, reused=True)


def _page_text(rec: PageRecord) -> PageText:
    """What the page says, for a judgement (the page has its body)."""
    page = rec.page
    assert page is not None
    o = page.outline
    return PageText(title=rec.title, profile=o.profile, sections=tuple(
        (s.heading, page.texts[s.id].body if s.id in page.texts else "") for s in o.sections))


def _written_for(state: WikiState, rec: PageRecord) -> str:
    """The goal a page was written for: its map's title, else the goal it was made with."""
    pid = rec.page_id
    for m in state.maps.values():
        if any(n.page_id == pid and pid not in _fit_pages(state, m)
               or (n.path_id in state.paths and pid in state.paths[n.path_id].page_ids) for n in m.nodes):
            return m.title or m.goal
    return rec.goal.split(" · ")[0]


def _fit_pages(state: WikiState, m: GoalMap) -> set[str]:
    """The pages this map only reuses."""
    return {f.page_id for f in state.page_fits.values() if f.map_id == m.map_id and f.verdict != "none"}


def _reused_page(w: _Work, m: GoalMap, node: MapNode) -> str | None:
    """An existing page that fits this goal's point, newest first (goals/page_fit.py): the first
    one judged full or partial; None: this goal gets its own page. Pages on this map, reading pages,
    unwritten or trashed ones are not candidates. A judgement that still holds is read, not asked
    again; each new one is saved in this request's commit. No K evidence moves."""
    s = w.state
    rec = s.graph.node(node.id)
    definition = rec.definition if rec is not None else ""
    for pid in pages_elsewhere(s, m, node.id):
        page = s.pages[pid]
        fit = s.page_fits.get(f"{m.map_id}:{node.id}:{pid}")
        if fit is None or not fit_current(s, m, node, fit):
            written_for = _written_for(s, page)
            verdict, why, note = judge_fit(
                w.llm(), goal=m.title or m.goal, point=node.name, definition=definition,
                scope=node.scope, page=_page_text(page), written_for=written_for, language=s.language)
            fit = PageFit(map_id=m.map_id, node_id=node.id, page_id=pid,
                          page_version=page_text_fingerprint(page), context=fit_key(m, node),
                          verdict=verdict, why=why, note=note, written_for=written_for)
            w.add(PageFitSaved(record=fit))
        if fit.verdict != "none":
            return pid
    return None


def _open_point(repo: Repo, snap: Snapshot, gw: CountingGateway, user_id: str, m: GoalMap,
                node: MapNode, *, now: datetime, new_id: Ids) -> DraftResult:
    """Open a point to learn (atomic, or not judged and with no parts: learned as one, never
    written back as atomic). Its path on this map → that path (0 calls). A page another goal wrote
    on it that fits → that page, no write (`_reused_page`; its page_id on this map node). Otherwise a draft over
    the point and the prerequisites K does not show learned (`_point_prereqs`): when K has
    evidence on every one of them no diagnosis runs and its pages are planned at once; else one
    probe round over them only (possibly no questions). The path hangs on this map node."""
    if node.path_id is not None and node.path_id in snap.state.paths:
        return DraftResult(draft=None, page_id=None, calls=0, path_id=node.path_id)
    if node.page_id is not None and node.page_id in snap.state.pages:
        if live_page(snap.state, m, node) is not None:
            return DraftResult(draft=None, page_id=node.page_id, calls=0)
        node = node.model_copy(update={"page_id": None})     # the page or the goal changed: judge again
        m = m.replace(node)
    w = _Work(snap, gw, user_id, now=now, new_id=new_id)
    reused = _reused_page(w, m, node)
    if reused is not None:
        _save_map(w, m, m.replace(node.model_copy(update={"state": "expanded", "page_id": reused})),
                  "page_created")
        w.commit(repo)
        return DraftResult(draft=None, page_id=reused, calls=w.calls)
    kv_before = w.kview()
    ids = [node.id, *_point_prereqs(w, node.id)]
    concepts = tuple(_point_concept(w, m, c, target=c == node.id) for c in ids)
    missing = [c for c in concepts if c.id not in w.k.concept_ids()]
    if missing:
        w.add_aspect(define_events(missing, ts=now, new_id=new_id))
    goal = f"{m.goal} · {node.name}"
    res = IntakeResult(goal=goal, title=node.name, concepts=concepts, targets=(node.id,),
                       observations=(), dropped=(), resolutions=())
    opening = _opening(snap.state, m)
    levels = tuple(lv for lv in (opening.levels if opening is not None else ()) if lv.node in ids)
    draft = _draft(new_id("dr"), goal, res, map_id=m.map_id, region_id=node.id,
                   evidenced=_evidenced(kv_before, ids), levels=levels, sources=m.sources)
    entered = _save_map(w, m, m.replace(node.model_copy(update={"state": "expanded"})),
                        "expanded")
    if NO_PROBE_LINE in m.log or all(_has_evidence(kv_before, c) for c in ids if kv_before.aspects(c)):
        out = _finish(w, draft, entered, stored=False)        # K is enough, or he said no 摸底: no diagnosis
    else:
        r = _probe(w, draft, entered, ())
        entered = _save_map(w, entered, _decision_log(entered, r), "diagnosed")
        out = _open_or_finish(w, draft, entered, r)
    w.commit(repo)
    return out


def _member_concepts(w: _Work, region: MapNode) -> tuple[IntakeConcept, ...]:
    """A region's members as the draft's concepts (names, aspects and prerequisites from the
    wiki and K)."""
    out = []
    for c in region.members:
        out.append(IntakeConcept(
            id=c, name=w.state.names[c].name if c in w.state.names else c,
            aspects=w.k.aspects(c) if c in w.k.concept_ids() else (),
            prereqs=w.k.prereqs(c) if c in w.k.concept_ids() else (),
            subitems=w.state.names[c].subitems if c in w.state.names else (), reused=True))
    return tuple(out)


def _enter_members(repo: Repo, snap: Snapshot, gw: CountingGateway, user_id: str, m: GoalMap,
                   region: MapNode, *, now: datetime, new_id: Ids) -> DraftResult:
    """A region of an old goal's map (the all-at-once route, and S1's reading block before the
    reading mode: its members fixed when the goal was made; both routes are gone, their maps
    stay usable — reading-mode spec Q6): its members without evidence are probed, then its pages
    are planned over them. WikiConflict: its pages are planned."""
    if region.path_id is not None or region.page_id is not None:
        raise WikiConflict("这个区域已经排好页了")
    w = _Work(snap, gw, user_id, now=now, new_id=new_id)
    kv_before = w.kview()
    draft_id = new_id("dr")
    expanded = m.replace(region.model_copy(update={"state": "expanded"}))
    concepts = _member_concepts(w, region)
    res = IntakeResult(goal=f"{m.goal} · {region.name}", title=region.name, concepts=concepts,
                       targets=_sinks(concepts), observations=(), dropped=(), resolutions=())
    w.add(GoalMapSaved(record=expanded, action="expanded"))
    opening = _opening(snap.state, m)                  # its levels for these members
    ids = {c.id for c in res.concepts}
    levels = tuple(lv for lv in (opening.levels if opening is not None else ()) if lv.node in ids)
    draft = _draft(draft_id, res.goal, res, map_id=m.map_id, region_id=region.id,
                   evidenced=_evidenced(kv_before, [region.id, *ids]), levels=levels,
                   sources=m.sources)
    r = _probe(w, draft, expanded, ())
    expanded = _save_map(w, expanded, _decision_log(expanded, r), "diagnosed")
    out = _open_or_finish(w, draft, expanded, r)
    w.commit(repo)
    return out


class EnterSources(Protocol):
    """The materials an entered block is split with (stage 3, spec 2026-10-28 §2.5; the DB one is
    goal_sources.PooledEnterSources)."""

    def block_tocs(self, user_id: str, source_ids: Sequence[str],
                   node_ids: Sequence[str]) -> list[SourceToc]:
        """The goal's materials' TOCs trimmed to the sections linked to the first of `node_ids`
        that has links (the node, then its ancestors), with their subsections; [] when none
        has any."""
        ...

    def write_links(self, conn: Any, user_id: str, links: Sequence[NodeSectionLink],
                    now: datetime) -> None:
        """Writes the parts' links inside the split's commit."""
        ...


@traced_entry("decompose", subject="region_id", input=lambda a: {"map_id": a["map_id"]})
def enter_region(repo: Repo, gw: CountingGateway, user_id: str, map_id: str, region_id: str, *,
                 now: datetime, new_id: Ids, sources: EnterSources | None = None
                 ) -> DraftResult:
    """Enter any node of a goal map (spec 2026-10-28 §2.2; what it does is
    `node_state.enter_kind`, never the map node's kind):
    - not atomic, not expanded → ONE call (S2, goals.enter, enter@v2: the block's own level and
      the blocks above it; an invalid split is asked once more, then IntakeError and nothing is
      written) splits it one level and estimates from his own words (`_estimate_words`) which
      cells of the parts he knows (verbatim quotes only, K self-reports; each part keeps its
      quotes as `said`), the parts with the model's `atomic` go into the graph under it and
      onto this map (not entered yet), the node is marked not atomic; nothing is diagnosed or
      planned: the result is the map (`map_id`), its parts wait for the learner to pick one.
      With `sources` and a goal made from materials, the split sees the sections linked to the
      node (else its nearest linked ancestor; `GoalMap.material_use` reference: only linked,
      else followed) and the parts' section links are written in the same commit (by "enter");
    - expanded (the graph has its parts) → its parts are put on this map when missing, 0 calls,
      the map; parts only another goal's split made do not come over: this goal's first entry
      splits the node itself, one call (`node_state.foreign_parts`);
    - a point (atomic, or not judged with no parts) → `_open_point`.
    A diagnosis of the node still open is returned as it is (0 calls). LookupError: no such map
    or node. A node K shows known can be entered like any other (known is a state, not a lock;
    entering writes nothing to K): the goal's own next step skips it, the learner may not."""
    snap = repo.load(user_id)
    m = snap.state.maps.get(map_id)
    node = m.node(region_id) if m is not None else None
    if m is None or node is None:
        raise LookupError(f"没有这个区域：{map_id} / {region_id}")
    open_ = _open_draft(snap.state, map_id, region_id)
    if open_ is not None:
        return DraftResult(draft=open_, page_id=None, calls=0, open=True)
    if _open_draft(snap.state, map_id, None) is not None:
        raise WikiConflict(DIAGNOSIS_OPEN)
    if node.kind == "region" and node.members:
        return _enter_members(repo, snap, gw, user_id, m, node, now=now, new_id=new_id)
    case = enter_kind(snap.state, m, node)
    if case == "learn":
        return _open_point(repo, snap, gw, user_id, m, node, now=now, new_id=new_id)
    w = _Work(snap, gw, user_id, now=now, new_id=new_id)
    # another goal has a page on this point: whether this goal's split calls it atomic does not decide reuse, the page is judged first
    if node.state != "expanded" and pages_elsewhere(snap.state, m, node.id):
        reused = _reused_page(w, m, node)
        if reused is not None:
            _save_map(w, m, m.replace(node.model_copy(update={"state": "expanded", "page_id": reused})), "page_created")
            w.commit(repo)
            return DraftResult(draft=None, page_id=reused, calls=w.calls)
    foreign = foreign_parts(snap.state, m, node)
    if case == "children" and (node.state == "expanded" or not foreign):
        _show_children(w, m, node, [c for c in graph_children(snap.state, node.id) if c not in foreign])
        w.commit(repo)
        return DraftResult(draft=None, page_id=None, calls=0, map_id=map_id)
    scope = _scope_for(snap.state, m, node)
    # S2 (one call): his conditions for the goal shape the split; his own words are what the
    # same call estimates from (`_estimate_words`: none for a block not to diagnose, D07); the
    # block's linked sections, when the goal has materials
    tocs: list[SourceToc] = []
    if sources is not None and m.sources:
        g = snap.state.graph
        chain = [node.id, *(g.ancestors(node.id) if g.node(node.id) is not None else ())]
        tocs = sources.block_tocs(user_id, m.sources, chain)
    res = decompose_one_level(w.llm("enter"), scope.goal, _estimate_words(m, node),
                 existing_refs(snap.state, snap.k), evidence_ref=f"goal:{map_id}:{node.id}",
                 language=snap.state.language, scope=scope, limits=m.limits,
                 annotations=_annotations(snap.state, AspectKView(snap.k, now)),
                 topics=_topics(snap.state), graph_relations=_graph_relations(snap.state),
                 sources=tocs, mode="reference" if m.material_use == "reference" else "follow",
                 shown=entered_nodes(snap.state, m, node.id, scope))
    _intake_events(w, res)
    flag = marked(w.state, {node.id: False}, {node.id: node.name})
    if flag is not None:
        w.add(flag)
    _goal_graph(w, res.model_copy(update={"regions": ()}), {}, parent=(node.id, node.name))
    _show_children(w, m, node, [c.id for c in res.concepts], {c.id: c.name for c in res.concepts},
                   _child_said(res), {c.id: c.in_goal for c in res.concepts if c.in_goal})
    links = res.links
    w.commit(repo, [lambda conn: sources.write_links(conn, user_id, links, now)]
             if sources is not None and links else ())
    return DraftResult(draft=None, page_id=None, calls=w.calls, map_id=map_id, links=links)
