"""One source of learning progress (usability reset 2026-10-27, item 1). Pure functions over a
loaded state, K and the pages' latest write runs; 0 LLM calls, no IO. Every view that says where
the learner is (今天, the end of a page, a path row, 学点新的, the chat goal run) reads it here, so
the plugin never guesses.

A page row's state: planned → writing → written → read → mastered, plus failed and trashed.
- planned: no wiki page, or one without a body and no live / failed run;
- writing / failed: no body yet (none, or the sections a run has committed so far:
  `PageRecord.writing`), its latest run is writing / failed;
- written: it has a body; read: the learner said 学完 (only `page.mark_done`; opening never counts);
- mastered: written, and K puts every target cell at likely or above (goals.path.page_learned);
- trashed: the page is in the trash. A trashed row is out of the way: not the next step, not
  counted (the learner put it away; the path view offers 重新写).
A skipped row counts as done. Only a page with a body counts as taught or done (D04, D39).

A path is done when every row is done; a block (any node of a goal map) when it is known — not
entered and K puts every cell it covers at likely or above (`regions.block_known`; read from K
every time, never from the map) — or its path / page / parts are done; a goal when every block is
(U2). A node entered whose parts none has begun is `choosing`: its step re-enters it (the parts
are shown, the learner picks one; spec 2026-10-28 §4). A path hangs on any map node. The next step of a path is its
first row in the SHOWN order that is not done (D40: a reordered page never hides earlier rows).
The shape follows DeepTutor's learning policy (next_objective / is_mastered)."""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from evowiki.belief.aspects import AspectKView
from evowiki.goals.contracts import cell_id
from evowiki.goals.path import LEARNED_BANDS, PageStatus, PathPage
from evowiki.goals.path_edit import Item
from evowiki.goals.regions import GoalMap, MapNode, block_known, next_region, ordered
from evowiki.wikiapp.events import PathRecord
from evowiki.wikiapp.node_state import grid_known, live_page, taught_on_paths
from evowiki.wikiapp.page_run_store import PageRunRow
from evowiki.wikiapp.path_order import shown, stored_index
from evowiki.wikiapp.review_first import suggestion
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import (
    BlockOut,
    GoalProgressOut,
    PageProgressOut,
    PageState,
    PathPageStatus,
    ProgressOut,
    StepOut,
    WriteProgressOut,
)

DONE: frozenset[str] = frozenset({"read", "mastered"})


def learned(targets: Iterable[str], kv: AspectKView) -> bool:
    """Every cell of these target concepts is likely or known (no cells: cannot tell)."""
    cells = [cell_id(c, a) for c in targets for a in kv.aspects(c)]
    return bool(cells) and all(kv.cell(x).band in LEARNED_BANDS for x in cells)


def page_state(state: WikiState, kv: AspectKView, page_id: str | None,
               run: PageRunRow | None, *, row: PathPage | None = None,
               assume_read: Collection[str] = ()) -> PageState:
    """The state of a wiki page (a path row's: `row`, whose targets K is read for; else the
    page's own). `run`: the page's latest write run. `assume_read`: pages to count as read (what
    the end of a page shows is the step after 学完)."""
    if page_id is None:
        return "planned"
    if page_id in state.trashed_pages:
        return "trashed"
    rec = state.pages.get(page_id)
    if rec is None:
        return "planned"
    if not rec.has_body:              # no page yet, or one a run is writing / left part-way
        if run is not None and run.discarded_at is None and run.status in ("writing", "failed"):
            return run.status
        return "planned"
    if rec.done or page_id in assume_read:
        return "read"
    targets = row.targets if row is not None else rec.targets
    if (row is not None and row.status is PageStatus.DONE) or learned(targets, kv):
        return "mastered"
    return "written"


def status_of(st: PageState, started: bool) -> PathPageStatus:
    """The older three-way status, derived: done = read / mastered; not_started = no wiki page
    yet (`started` False) or trashed; else learning."""
    if st in DONE:
        return "done"
    return "not_started" if st == "trashed" or not started else "learning"


def write_progress(run: PageRunRow | None) -> WriteProgressOut | None:
    p = run.progress if run is not None and run.status == "writing" else None
    if not p or not isinstance(p.get("section"), int):
        return None
    of, pas = p.get("of"), p.get("pass")
    return WriteProgressOut(section=p["section"], of=of if isinstance(of, int) else None,
                            pass_=pas if isinstance(pas, int) else None)


def row_title(state: WikiState, it: Item) -> str:
    """The row's title. Paths planned before path@v7 have no page titles (the row shows the
    objective sentence); a one-node row of such a path is titled with the node's name."""
    p = it.page
    if p is None or p.title or len(it.node_ids) != 1:
        return it.title
    named = state.names.get(it.node_ids[0])
    return named.name if named is not None and named.name.strip() else it.title


@dataclass(frozen=True)
class Row:
    """One shown row of a path that has a page to teach."""

    item: Item
    index: int                       # stored, 1-based
    page_id: str | None
    state: PageState
    run: PageRunRow | None

    @property
    def done(self) -> bool:
        return self.item.skipped or self.state in DONE

    @property
    def counted(self) -> bool:
        return self.state != "trashed"


@dataclass(frozen=True)
class Ctx:
    state: WikiState
    kv: AspectKView
    runs: Mapping[str, PageRunRow]
    assume_read: Collection[str] = ()

    def page(self, page_id: str | None, row: PathPage | None = None) -> PageState:
        run = self.runs.get(page_id) if page_id is not None else None
        return page_state(self.state, self.kv, page_id, run, row=row,
                          assume_read=self.assume_read)


def path_rows(ctx: Ctx, rec: PathRecord) -> list[Row]:
    """The path's shown rows that teach something (title-only rows of the learner left out)."""
    out = []
    for it in shown(rec):
        if it.page is None:
            continue
        i = stored_index(rec, it.key)
        pid = rec.page_ids[i]
        out.append(Row(item=it, index=i + 1, page_id=pid, state=ctx.page(pid, rec.path.pages[i]),
                       run=ctx.runs.get(pid) if pid is not None else None))
    return out


def row_states(ctx: Ctx, rec: PathRecord) -> dict[str, tuple[PageState, WriteProgressOut | None]]:
    """Row key -> (state, write progress) for the path views."""
    return {r.item.key: (r.state, write_progress(r.run)) for r in path_rows(ctx, rec)}


def _title(state: WikiState, r: Row) -> str:
    """The row's wiki page's own title once it has one (a merged page took the row), else the
    row's."""
    rec = state.pages.get(r.page_id) if r.page_id is not None else None
    return rec.title if rec is not None else row_title(state, r.item)


def _before(rec: PathRecord, item: Item) -> list[Item]:
    items = shown(rec)
    return items[:next(i for i, x in enumerate(items) if x.key == item.key)]


def _row_step(ctx: Ctx, rec: PathRecord, r: Row, base: StepOut) -> StepOut:
    kind = {"writing": "writing", "failed": "failed", "written": "read"}.get(r.state, "start_page")
    run = r.run
    return base.model_copy(update={
        "kind": kind, "path_id": rec.path.path_id, "index": r.index, "page_id": r.page_id,
        "title": _title(ctx.state, r), "progress": write_progress(run),
        "failure": run.failure if kind == "failed" and run is not None else None,
        "review_first": suggestion(ctx.state, ctx.kv, r.item, _before(rec, r.item), r.page_id)})


def path_step(ctx: Ctx, rec: PathRecord, base: StepOut) -> StepOut | None:
    """The path's next step: its first shown row not done (None: every row is)."""
    r = next((r for r in path_rows(ctx, rec) if r.counted and not r.done), None)
    return None if r is None else _row_step(ctx, rec, r, base)


def _counts(rows: list[Row]) -> tuple[int, int]:
    counted = [r for r in rows if r.counted]
    return sum(r.done for r in counted), len(counted)


# --- goal maps -----------------------------------------------------------------------------------

def _open_draft(state: WikiState, map_id: str, region_id: str | None) -> str | None:
    return next((d.draft_id for d in state.drafts.values()
                 if d.map_id == map_id and d.region_id == region_id
                 and d.draft_id not in state.closed_drafts), None)


@dataclass(frozen=True)
class Block:
    out: BlockOut
    step: StepOut | None             # None: done (or known)


def _parts(ctx: Ctx, m: GoalMap, n: MapNode) -> list[MapNode]:
    """The node's parts on the map, any kind (spec 2026-10-28: a part can be entered again or
    opened to learn). An older map's member — a concept node always `expanded`, with nothing
    of its own (no path, page, parts or open diagnosis) — is learned on its region's path and
    is no part here; nor is a part not entered that another node's path teaches (a point's
    prerequisite: `node_state.taught_on_paths`)."""
    taught = taught_on_paths(ctx.state, m)

    def own(c: MapNode) -> bool:
        if (c.path_id is not None or c.page_id is not None or m.children(c.id)
                or _open_draft(ctx.state, m.map_id, c.id) is not None):
            return True
        if c.state == "collapsed":
            return c.id not in taught
        return c.kind != "concept"
    return ordered([c for c in m.children(n.id) if own(c)])


def _pick(blocks: list[Block]) -> Block | None:
    """The block learning goes on in: the first one already begun (diagnosing / learning), else
    the first not started."""
    open_ = [b for b in blocks if b.step is not None]
    return next((b for b in open_ if b.out.state in ("learning", "diagnosing", "choosing")),
                open_[0] if open_ else None)


def _begun_elsewhere(ctx: Ctx, m: GoalMap, b: Block) -> bool:
    rec = ctx.state.paths.get(b.out.path_id) if b.out.path_id else None
    return rec is not None and not rec.path.goal.startswith(m.goal)


def _pick_top(ctx: Ctx, m: GoalMap, tops: list[Block]) -> Block | None:
    """Where a goal goes on. A page or a diagnosis under way comes first; otherwise the node to enter is `next_region`,
    the one answer the map, the chat's 「接下来从…开始」 and 开始学习 all read (his order wins: 从 X 开始, a dragged block)."""
    # a block shared with an earlier goal, begun there, is not this goal's work under way: its order stands
    working = [b for b in tops if b.step is not None and b.step.kind != "enter_block"
               and b.out.state in ("learning", "diagnosing") and not _begun_elsewhere(ctx, m, b)]
    if working:
        return working[0]
    nid = next_region(m, ctx.kv, taught_on_paths(ctx.state, m), grid_known(ctx.state, m))
    node = m.node(nid) if nid is not None else None
    if node is None:
        return next((b for b in tops if b.step is not None), None)
    top = node
    while top.parent is not None and m.node(top.parent) is not None:
        top = m.node(top.parent)
    owner = next(b for b in tops if b.out.region_id == top.id)
    base = StepOut(kind="enter_block", goal_id=m.map_id, goal=m.title or m.goal, map_id=m.map_id,
                   region_id=node.id, region_name=node.name)
    return Block(owner.out, base)


def block(ctx: Ctx, m: GoalMap, n: MapNode, base: StepOut) -> Block:
    n = n.model_copy(update={"page_id": live_page(ctx.state, m, n)})
    out = BlockOut(region_id=n.id, name=n.name, level=n.level, state="not_started",
                   path_id=n.path_id)
    here = base.model_copy(update={"map_id": m.map_id, "region_id": n.id, "region_name": n.name})
    if block_known(m, n, ctx.kv) or (n.path_id is None and n.page_id is None and n.id in grid_known(ctx.state, m)):
        return Block(out.model_copy(update={"state": "known"}), None)
    if n.path_id is not None and n.path_id in ctx.state.paths:
        rec = ctx.state.paths[n.path_id]
        done, total = _counts(path_rows(ctx, rec))
        step = path_step(ctx, rec, here)
        return Block(out.model_copy(update={"state": "learning" if step else "done",
                                            "pages_done": done, "pages_total": total}), step)
    if n.page_id is not None:
        st = ctx.page(n.page_id)
        rec = ctx.state.pages.get(n.page_id)
        if st in DONE:
            return Block(out.model_copy(update={"state": "done", "pages_done": 1,
                                                "pages_total": 1}), None)
        kind = {"writing": "writing", "failed": "failed", "written": "read"}.get(st, "start_page")
        run = ctx.runs.get(n.page_id)
        return Block(out.model_copy(update={"state": "learning", "pages_total": 1}),
                     here.model_copy(update={
                         "kind": kind, "page_id": n.page_id,
                         "title": rec.title if rec is not None else n.name,
                         "progress": write_progress(run),
                         "failure": run.failure if kind == "failed" and run else None}))
    draft = _open_draft(ctx.state, m.map_id, n.id)
    if draft is not None:
        return Block(out.model_copy(update={"state": "diagnosing", "draft_id": draft}),
                     here.model_copy(update={"kind": "diagnosis", "draft_id": draft}))
    subs = _parts(ctx, m, n)
    if n.state == "expanded" and subs:
        kids = [block(ctx, m, c, base) for c in subs]
        done = sum(k.out.pages_done for k in kids)
        total = sum(k.out.pages_total for k in kids)
        nxt = _pick(kids)
        if nxt is not None and not any(k.out.state in ("learning", "diagnosing", "choosing", "done")
                                       for k in kids):
            # entered, parts shown, none begun: the learner picks one (never the first by itself,
            # spec 2026-10-28 §4); the step re-enters it, which only shows the parts. Once a part is
            # read, going on is the next part: naming this node again would go nowhere.
            return Block(out.model_copy(update={"state": "choosing", "pages_done": done,
                                                "pages_total": total}),
                         here.model_copy(update={"kind": "enter_block"}))
        state = "done" if nxt is None else "learning"
        return Block(out.model_copy(update={"state": state, "pages_done": done,
                                            "pages_total": total}),
                     None if nxt is None else nxt.step)
    state = "learning" if n.state == "expanded" else "not_started"
    return Block(out.model_copy(update={"state": state}),
                 here.model_copy(update={"kind": "enter_block"}))


def map_progress(ctx: Ctx, m: GoalMap) -> GoalProgressOut:
    base = StepOut(kind="goal_done", goal_id=m.map_id, goal=m.title or m.goal, map_id=m.map_id)
    tops = [block(ctx, m, n, base) for n in ordered([c for c in m.children(None)
                                                       if c.kind == "region"])]
    opening = _open_draft(ctx.state, m.map_id, None)
    nxt = _pick_top(ctx, m, tops)
    if opening is not None:
        step = base.model_copy(update={"kind": "diagnosis", "draft_id": opening})
    else:
        step = base if nxt is None or nxt.step is None else nxt.step
    begun = opening is not None or any(b.out.state != "not_started" for b in tops)
    state = "done" if tops and nxt is None and opening is None else (
        "learning" if begun else "not_started")
    if state != "done" and step.kind == "goal_done":
        step = base.model_copy(update={"kind": "none"})   # a map without blocks: nothing to do
    return GoalProgressOut(
        goal_id=m.map_id, kind="map", goal=base.goal, state=state, blocks=[b.out for b in tops],
        path_ids=[b.out.path_id for b in tops if b.out.path_id is not None],
        pages_done=sum(b.out.pages_done for b in tops),
        pages_total=sum(b.out.pages_total for b in tops), step=step)


def lone_path_progress(ctx: Ctx, rec: PathRecord) -> GoalProgressOut:
    pid = rec.path.path_id
    base = StepOut(kind="goal_done", goal_id=pid, goal=rec.path.goal)
    rows = path_rows(ctx, rec)
    done, total = _counts(rows)
    step = path_step(ctx, rec, base)
    begun = any(r.page_id is not None for r in rows)
    state = "done" if step is None else ("learning" if begun else "not_started")
    return GoalProgressOut(goal_id=pid, kind="path", goal=rec.path.goal, state=state,
                           path_ids=[pid], pages_done=done, pages_total=total,
                           step=step or base)


# --- the learner -------------------------------------------------------------------------------

def _archived(state: WikiState, goal_id: str) -> bool:
    gs = state.goal_settings.get(goal_id)
    return gs is not None and gs.archived


def map_paths(state: WikiState) -> dict[str, str]:
    """Path id -> the goal map whose node (a point, an older map's region) it hangs on."""
    return {n.path_id: m.map_id for m in state.maps.values() for n in m.nodes
            if n.path_id is not None}


def goals(ctx: Ctx) -> list[GoalProgressOut]:
    """Every goal not archived: goal maps, then the paths on no map, each in planning order."""
    st = ctx.state
    on_map = map_paths(st)
    out = [map_progress(ctx, st.maps[mid]) for mid in st.map_order
           if mid in st.maps and not _archived(st, mid)]
    out += [lone_path_progress(ctx, st.paths[pid]) for pid in st.path_order
            if pid not in on_map and not _archived(st, pid)]
    return out


def goal_of_page(state: WikiState, page_id: str) -> str | None:
    """The goal (map id, else lone path id) a page belongs to; None: on no goal."""
    on_map = map_paths(state)
    for pid, rec in state.paths.items():
        if page_id in rec.page_ids:
            return on_map.get(pid, pid)
    return next((m.map_id for m in state.maps.values()
                 for n in m.nodes if n.page_id == page_id), None)


def progress(state: WikiState, kv: AspectKView, runs: Mapping[str, PageRunRow]) -> ProgressOut:
    """GET /wiki/progress. `runs`: the latest write run of each page (`page_ids`)."""
    gs = goals(Ctx(state, kv, runs))
    current = goal_of_page(state, state.current_page) if state.current_page else None
    open_ = [g for g in gs if g.state != "done"]
    pick = next((g for g in open_ if g.goal_id == current), open_[0] if open_ else None)
    return ProgressOut(today=pick.step if pick is not None else StepOut(kind="none"), goals=gs)


def goal_step(state: WikiState, kv: AspectKView, map_id: str) -> StepOut:
    """Where this goal goes on, as /wiki/progress says it (no page's write state: for the chat's 「接下来…」)."""
    return map_progress(Ctx(state, kv, {}), state.maps[map_id]).step


def page_progress(state: WikiState, kv: AspectKView, runs: Mapping[str, PageRunRow],
                  page_id: str) -> PageProgressOut:
    """The page's state and the step after it is read.
    Unknown page → LookupError."""
    if page_id not in state.pages:
        raise LookupError(f"没有这一页：{page_id}")
    row = next((rec.path.pages[i] for rec in state.paths.values()
                for i, p in enumerate(rec.page_ids) if p == page_id), None)
    st = page_state(state, kv, page_id, runs.get(page_id), row=row)
    gid = goal_of_page(state, page_id)
    after = StepOut(kind="none")
    if gid is not None:
        ctx = Ctx(state, kv, runs, assume_read=(page_id,))
        g = (map_progress(ctx, state.maps[gid]) if gid in state.maps
             else lone_path_progress(ctx, state.paths[gid]))
        after = g.step
    return PageProgressOut(page_id=page_id, state=st, goal_id=gid, after=after)


def page_ids(state: WikiState) -> list[str]:
    """Every page a progress view may read a write run for."""
    ids = {p for rec in state.paths.values() for p in rec.page_ids if p is not None}
    ids |= {n.page_id for m in state.maps.values() for n in m.nodes if n.page_id is not None}
    return sorted(ids)
