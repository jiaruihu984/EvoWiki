"""A knowledge node's decomposition state (on-demand decomposition, spec 2026-10-28 §2.2).

Two independent properties: `atomic` is stored on the knowledge node (`ConceptName.atomic`,
shared by every goal), `expanded` is derived from the graph (the node has part_of children).
A map node's `state` only says whether this goal has entered it; `kind` no longer decides
whether it can be entered. What entering a node does (`enter_kind`):

| case                          | rule                                        | entering            |
|-------------------------------|---------------------------------------------|---------------------|
| non-atomic, not expanded      | atomic False, no graph children              | "split": one level  |
| expanded                      | graph children (whatever atomic says)       | "children": shown   |
| atomic                        | atomic True, no children                    | "learn"             |
| not judged (older data)       | atomic None, no children: a top-level region | "split"            |
|                               | any other node                              | "learn" (as atomic, |
|                               |                                             | never written back) |

A region of an old goal's map (it has `members`: the all-at-once route, and 「读懂这篇」 before the
reading mode; both gone, their maps stay usable) is "learn": opening it plans its members' path
(goal._enter_members).
Every "learn" node can still be split further (「继续拆细」, wikiapp.node_split). Pure functions."""

from collections.abc import Iterable, Mapping
from typing import Literal

from evowiki.belief.aspects import AspectKView
from evowiki.goals.contracts import cell_id
from evowiki.goals.regions import LEARNED_BANDS, GoalMap, MapNode, covered
from evowiki.wikiapp.events import ConceptName, ConceptsNamed
from evowiki.wikiapp.state import WikiState

EnterKind = Literal["split", "children", "learn"]
from evowiki.goals.page_fit import PageFit, fit_context
from evowiki.wikiapp.page_version import page_text_fingerprint


def graph_children(state: WikiState, node_id: str) -> tuple[str, ...]:
    """The node's part_of children in the shared graph (none when the graph lacks it)."""
    if state.graph.node(node_id) is None:
        return ()
    return tuple(state.graph.children(node_id))


def atomic_of(state: WikiState, node_id: str) -> bool | None:
    named = state.names.get(node_id)
    return named.atomic if named is not None else None


def enter_kind(state: WikiState, m: GoalMap, n: MapNode) -> EnterKind:
    if n.kind == "region" and n.members:
        return "learn"          # the older all-at-once route: its members learned on one path
    if graph_children(state, n.id):
        return "children"
    atomic = atomic_of(state, n.id)
    if atomic is False:
        return "split"
    if atomic is True:
        return "learn"
    return "split" if n.kind == "region" and n.parent is None else "learn"


def foreign_parts(state: WikiState, m: GoalMap, node: MapNode) -> set[str]:
    """Parts of this shared node that only another goal's split put there: 黑壳虾's 「适宜水温范围」 under the
    水温 孔雀鱼 shares. They stay that goal's: this goal's first entry splits the node itself (the rows
    list them, the model reuses what holds here), and neither goal's map takes the other's."""
    others = {c.id for x in state.maps.values() if x.map_id != m.map_id for c in x.children(node.id)}
    return others - {c.id for c in m.children(node.id)}


def pages_elsewhere(state: WikiState, m: GoalMap, node_id: str) -> list[str]:
    """Written pages teaching this point that are not on this map, newest first: what opening it
    looks at before writing (goals/page_fit.py). Reading pages and trashed ones are not candidates."""
    own = {p for n in m.nodes for p in ((n.page_id,) + (
        state.paths[n.path_id].page_ids if n.path_id in state.paths else ())) if p is not None}
    out = []
    for pid in reversed(state.page_order):
        page = state.pages[pid]
        if (node_id in page.targets and pid not in own and pid not in state.trashed_pages
                and page.has_body and page.kind == "learn"):
            out.append(pid)
    return out


def fit_key(m: GoalMap, node: MapNode) -> str:
    return fit_context(m.goal, node.name, node.scope, m.limits)


def fit_current(state: WikiState, m: GoalMap, node: MapNode, fit: PageFit) -> bool:
    """Does the judgement still hold: same page version, same goal context, same way of judging."""
    rec = state.pages.get(fit.page_id)
    return rec is not None and rec.has_body and fit.fresh(page_text_fingerprint(rec), fit_key(m, node))


def live_page(state: WikiState, m: GoalMap, n: MapNode) -> str | None:
    """The node's page, unless it is another goal's page whose judgement no longer holds (or which
    was trashed): then the node has none and opening it judges again."""
    fit = state.page_fits.get(f"{m.map_id}:{n.id}:{n.page_id}")
    if fit is None:
        return n.page_id
    return n.page_id if fit_current(state, m, n, fit) and n.page_id not in state.trashed_pages else None


def enter_shown(state: WikiState, m: GoalMap, n: MapNode) -> str:
    """What entering looks like from the map (MapNodeOut.enter): `enter_kind`, but a shared node another
    goal split is split again here, and a point with a page elsewhere looks at that page first ("page")."""
    kind = enter_kind(state, m, n)
    if kind == "children" and n.state != "expanded" and foreign_parts(state, m, n):
        return "split"
    if kind == "learn" and n.path_id is None and n.page_id is None and pages_elsewhere(state, m, n.id):
        return "page"
    return kind


def taught_on_paths(state: WikiState, m: GoalMap) -> frozenset[str]:
    """The nodes a path hung on ANOTHER node of this map teaches (a page target): a point's
    prerequisite learned on that point's path is no step of its own (wikiapp/progress,
    regions.next_region)."""
    out: set[str] = set()
    for n in m.nodes:
        rec = state.paths.get(n.path_id) if n.path_id is not None else None
        if rec is not None:
            out |= {t for p in rec.path.pages for t in p.targets if t != n.id}
    return frozenset(out)


def marked(state: WikiState, values: Mapping[str, bool], names: Mapping[str, str] | None = None
           ) -> ConceptsNamed | None:
    """`ConceptsNamed` setting `atomic` on these nodes (name and subitems kept; a node with no
    recorded name takes `names`' or the graph's); None when nothing changes."""
    out = []
    for cid, atomic in values.items():
        old = state.names.get(cid)
        if old is not None:
            if old.atomic != atomic:
                out.append(old.model_copy(update={"atomic": atomic}))
            continue
        rec = state.graph.node(cid)
        name = (names or {}).get(cid) or (rec.name if rec is not None else "")
        if name.strip():
            out.append(ConceptName(concept_id=cid, name=name, atomic=atomic))
    return ConceptsNamed(names=tuple(out)) if out else None


def child_nodes(state: WikiState, m: GoalMap, parent: MapNode, ids: Iterable[str],
                names: Mapping[str, str] | None = None,
                prereqs: Mapping[str, tuple[str, ...]] | None = None,
                said: Mapping[str, tuple[str, ...]] | None = None,
                scopes: Mapping[str, str] | None = None
                ) -> tuple[list[MapNode], list[str]]:
    """These children as map nodes under `parent` (not entered yet: `collapsed`; a non-atomic
    child is a `region`, an atomic or not-judged one a `concept` — display only, entering reads
    `enter_kind`), and a log line per child already on the map elsewhere (not added twice).
    `names`: what this split named them (else the recorded / graph name); `said`: his own words
    about each new child (S2's estimate kept them on its cells; verbatim); `scopes`: what a reused
    child is about in this goal (its node keeps the first goal's definition)."""
    taken = {n.id for n in m.nodes}
    nodes, log = [], []
    ids = [i for i in dict.fromkeys(ids) if i != parent.id]
    for cid in ids:
        name = ((names or {}).get(cid) or (state.names[cid].name if cid in state.names else "")
                or (rec.name if (rec := state.graph.node(cid)) is not None else cid))
        if cid in taken:
            here = m.node(cid)
            if here is None or here.parent != parent.id:
                log.append(f"「{name}」已在地图别处，不重复加节点")
            continue
        kind = "region" if atomic_of(state, cid) is False else "concept"
        nodes.append(MapNode(id=cid, kind=kind, name=name, parent=parent.id,
                             scope=(scopes or {}).get(cid, ""),
                             prereqs=tuple(p for p in (prereqs or {}).get(cid, ())
                                           if p in ids and p != cid),
                             reused=cid in state.names, said=(said or {}).get(cid, ())))
        taken.add(cid)
    return nodes, log

def grid_missed(state: WikiState, m: GoalMap, kv: AspectKView) -> frozenset[str]:
    """Blocks marked 不会 on the 摸底 grid, or 掌握 / 熟悉 with a quiz then missed (wrong or 不知道), while K has no cell
    of theirs at likely or above: 不会 as a whole. K only has weak self-reports and one miss, which can read 熟悉 (more so
    on a point shared with an older goal). Once he learns its pages K is what shows again. Read from the recorded
    draft, as `grid_known`."""
    d = next((d for d in reversed(list(state.drafts.values()))
              if d.map_id == m.map_id and d.region_id is None and d.rough), None)
    if d is None:
        return frozenset()
    out: set[str] = set()
    for b, mark in d.rough.items():
        n = m.node(b)
        if n is None:
            continue
        inside = set(covered(m, n))
        missed = mark == "dont" or any(q.kind == "quiz" and (q.node or "") in inside and a != q.answer
                                       for r in d.rounds for q, a in zip(r.questions, r.answers or (), strict=False))
        cells = [cell_id(c, x) for c in inside for x in kv.aspects(c)]
        if missed and not any(kv.cell(c).band in LEARNED_BANDS for c in cells):
            out.add(b)
    return frozenset(out)


def grid_known(state: WikiState, m: GoalMap) -> frozenset[str]:
    """Blocks the opening diagnosis showed he knows (user ruling 2026-10-29: 「算会，跳过（和 demo 一致）」): marked
    掌握 (know) on the 摸底 grid and every quiz of that diagnosis on the block or its points answered right — at
    least one. Such a block counts as known as a whole (shown 掌握, skipped by next_region) whatever K says of
    its other cells; a wrong answer or 不知道, a 熟悉 mark, or no quiz leaves it to K. Read from the recorded draft
    (its grid marks and answered rounds), never stored apart."""
    d = next((d for d in reversed(list(state.drafts.values()))
              if d.map_id == m.map_id and d.region_id is None and d.rough), None)
    if d is None:
        return frozenset()
    out: set[str] = set()
    for b, mark in d.rough.items():
        n = m.node(b)
        if mark != "know" or n is None:
            continue
        inside = set(covered(m, n))
        got = [(q, a) for r in d.rounds for q, a in zip(r.questions, r.answers or (), strict=False)
               if q.kind == "quiz" and (q.node or "") in inside]
        if got and all(a == q.answer for q, a in got):
            out.add(b)
    return frozenset(out)
