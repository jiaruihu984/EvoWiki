"""Goal maps in the wiki app (big-goal design): GET a map, catalog cards. A map is planned in
goal.create_goal_from_blocks and its nodes are entered in goal.enter_region.

A node's colour is a mastery-weighted mix (user ruling: no numbers on the cloud): `mix` is the
share of its cells per band and `mastery` their mean m. A region counts its own K cells (direct
evidence: since the node graph every region is a K concept, plan T4) and its members' cells (plus,
on an older map, its expanded concepts'; `regions.covered`); a concept uses its own. A region's id
is its graph node's id (an existing node reused by id or name, else a goal-local `rg-…`). A
node's `state` "known" is derived from K (`regions.block_known`), never stored; so are its
`atomic` and `enter` (wikiapp/node_state: what entering it does, spec 2026-10-28 §2.2)."""

from collections.abc import Mapping
from datetime import datetime

from evowiki.belief.aspects import AspectK, AspectKView
from evowiki.goals.contracts import Band, cell_id
from evowiki.goals.regions import GoalMap, MapNode, block_known, covered, next_region, ordered
from evowiki.wikiapp.node_state import (
    atomic_of,
    enter_shown,
    grid_known,
    grid_missed,
    live_page,
    taught_on_paths,
)
from evowiki.wikiapp.reading_view import reading_out
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import GoalMapOut, MapCardOut, MapNodeOut


def _record(state: WikiState, map_id: str) -> GoalMap:
    m = state.maps.get(map_id)
    if m is None:
        raise LookupError(f"没有这张学习地图：{map_id}")
    return m


def _cells(m: GoalMap, n: MapNode, kv: AspectKView) -> list[str]:
    return [cell_id(c, a) for c in covered(m, n) for a in kv.aspects(c)]


def _name(state: WikiState, n: MapNode, user_id: str) -> str:
    del user_id
    return state.names[n.id].name if n.id in state.names else n.name


def node_out(state: WikiState, m: GoalMap, n: MapNode, kv: AspectKView, *,
             user_id: str = "", known: frozenset[str] | None = None) -> MapNodeOut:
    n = n.model_copy(update={"page_id": live_page(state, m, n)})
    beliefs = [kv.cell(c) for c in _cells(m, n, kv)]
    counts = {b: 0 for b in Band}
    for x in beliefs:
        counts[x.band] += 1
    total = len(beliefs)
    grid = grid_known(state, m) if known is None else known
    return MapNodeOut(
        id=n.id, kind=n.kind, name=_name(state, n, user_id),
        parent=n.parent, scope=n.scope, prereqs=list(n.prereqs), target=n.target,
        state="known" if (n.id in grid and n.path_id is None and n.page_id is None)
        or block_known(m, n, kv) else n.state,
        path_id=n.path_id,
        page_id=n.page_id, reused=n.reused,
        members=list(n.members), level=n.level, atomic=atomic_of(state, n.id),
        enter=enter_shown(state, m, n),
        # a block the grid and its quizzes showed he knows is 掌握 as a whole, entered or not (`grid_known`)
        mix={Band.KNOWN: 1.0} if n.id in grid else {Band.GAP: 1.0} if n.id in grid_missed(state, m, kv)
        else {b: c / total for b, c in counts.items()} if total else {},
        mastery=sum(x.m for x in beliefs) / total if total else None, counts=counts)


def _tree(m: GoalMap, parent: str | None) -> list[MapNode]:
    """Siblings in prerequisite order, each followed by its children."""
    out: list[MapNode] = []
    for n in ordered(m.children(parent)):
        out += [n, *_tree(m, n.id)]
    return out


def map_out(state: WikiState, k: AspectK, map_id: str, now: datetime, *,
            user_id: str = "", labels: Mapping[str, str] = {}) -> GoalMapOut:
    """GET /wiki/maps/{map_id} (0 calls). Unknown map → LookupError. A 读懂 goal's map also
    carries its reading (`labels`: section_id -> 「§3.2」 of its material, for the locations)."""
    m = _record(state, map_id)
    kv = AspectKView(k, now)
    known = grid_known(state, m)
    reading = state.reading_of(map_id)
    return GoalMapOut(map_id=m.map_id, goal=m.goal, title=m.title,
                      nodes=[node_out(state, m, n, kv, user_id=user_id, known=known) for n in _tree(m, None)],
                      next_region=next_region(m, kv, taught_on_paths(state, m), known),
                      reading=(reading_out(state, kv, reading, labels=labels)
                               if reading is not None else None))


def _archived(state: WikiState, goal_id: str) -> bool:
    st = state.goal_settings.get(goal_id)
    return st is not None and st.archived


def map_cards(state: WikiState, k: AspectK, now: datetime) -> list[MapCardOut]:
    """One catalog card per map, in planning order."""
    kv = AspectKView(k, now)
    out = []
    for mid in state.map_order:
        if _archived(state, mid):
            continue
        m = state.maps[mid]
        regions = [n for n in m.nodes if n.kind == "region"]
        out.append(MapCardOut(map_id=mid, goal=m.goal, regions=len(regions),
                              expanded=sum(n.state == "expanded" for n in regions),
                              known=sum(block_known(m, n, kv) or n.id in grid_known(state, m) for n in regions),
                              next_region=next_region(m, kv, taught_on_paths(state, m), grid_known(state, m))))
    return out
