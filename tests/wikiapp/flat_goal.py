"""Goals as the eager intake made them, for tests of what comes after a goal. The product no
longer makes either — S1 (goals/blocks.py) always makes a map of blocks with no members — but a
map whose regions have members (stored maps, the old 读懂 reading block) is still diagnosed and
entered, and many page / path tests only need a path on the z-score concepts.

- `flat_goal`: one block holding every part — the scripted reply read as one layer (one
  goals.enter call through a scope naming the goal itself; with a description, its estimate as
  the description's self-reports), the graph, a map of that one block (its parts as members) and
  the block's draft with its first probe round: finishing it plans the block's path, as opening
  a block does.
- `flat_page`: a draft finished as one page on all its concepts (no path stored).
- `region_goal`: a map whose regions have their members from the start, from a mapper fixture
  (`mapped_result`: the tree in learning order, as the eager intake numbered it)."""

import json
from datetime import datetime

from evowiki.belief.aspects import AspectKView
from evowiki.goals.contracts import Aspect
from evowiki.goals.intake import (
    DEFAULT_ASPECTS,
    ENTER_PURPOSE,
    ConceptRef,
    IntakeConcept,
    IntakeError,
    IntakeRegion,
    IntakeRelation,
    IntakeResult,
    IntakeScope,
    _EnterOut,
    _known_observations,
    _OneLevelOut,
    _outline_prompt,
    _RawKnown,
    _text,
    _Tree,
)
from evowiki.goals.path import goal_key
from evowiki.goals.regions import GoalMap, MapNode
from evowiki.plain_text import norm_name
from evowiki.wikiapp import goal as g
from evowiki.wikiapp.events import DraftClosed, GoalMapSaved
from evowiki.wikiapp.kwrite import existing_refs
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import Commit, Repo
from tests.goals.fakes_intake import _NEW_ID, known_from_mapper, to_nodes


def flat_goal(repo: Repo, gw: CountingGateway, user_id: str, goal: str, description: str = "",
              *, now: datetime, new_id) -> g.DraftResult:
    snap = repo.load(user_id)
    w = g._Work(snap, gw, user_id, now=now, new_id=new_id)
    draft_id = new_id("dr")
    kv_before = AspectKView(snap.k, now)
    scope = IntakeScope(goal=goal, region_id="", region_name=goal, parent_goal=goal,
                        siblings=((goal, ""),))
    # the scripted reply read as one layer (the parts of the goal itself); a flat goal is no
    # split, so the split check (goals/split_check: at least 2 parts) does not apply
    existing = existing_refs(snap.state, snap.k)
    words = (description,) if description.strip() else ()
    out = w.llm()("generator", ENTER_PURPOSE,
                  _outline_prompt(goal, existing, scope, snap.state.language, words=words),
                  _EnterOut if words else _OneLevelOut)
    tree = _Tree(existing, g._topics(snap.state), g._graph_relations(snap.state))
    tree.add_parts(out.parts, out.recommended)
    if not tree.roots:
        raise IntakeError("the outline returned no usable part")
    concepts, targets, relations, _ = tree.finish(True)
    observations, dropped = _known_observations(
        out.known if isinstance(out, _EnterOut) else (), concepts, words,
        evidence_ref=f"goal:{draft_id}")
    res = IntakeResult(goal=goal, title=_text(out.title) or goal, concepts=concepts,
                       targets=targets, observations=observations,
                       dropped=(*tree.dropped, *dropped), resolutions=tuple(tree.resolutions),
                       relations=relations)
    g._intake_events(w, res)
    g._goal_graph(w, res, {})
    map_id = f"map-{goal_key(goal, res.targets)}"
    region = MapNode(id=f"rg-{goal_key(goal, ())}", kind="region", name=goal, state="expanded",
                     members=tuple(c.id for c in res.concepts), target=True)
    nodes = (region, *(MapNode(id=c.id, kind="concept", name=c.name, parent=region.id,
                               state="expanded", reused=c.reused) for c in res.concepts))
    m = GoalMap(map_id=map_id, goal=goal, created_at=now, title=res.title, nodes=nodes,
                words=tuple(x for x in (goal, description) if x.strip()))
    w.add(GoalMapSaved(record=m, action="planned"))
    draft = g._draft(draft_id, goal, res, map_id=map_id, region_id=region.id,
                     evidenced=g._evidenced(kv_before, [c.id for c in res.concepts]),
                     levels=g._levels(res))
    r = g._probe(w, draft, m, ())
    out = g._open_or_finish(w, draft, m, r)
    w.commit(repo)
    return out


def mapped_result(state, k, goal: str, mapper: dict) -> IntakeResult:
    """A mapper fixture (intake@v10 / v14 shape: nodes + relations, see tests.goals.fakes_intake)
    as the IntakeResult the eager intake made of it: every node in depth-first learning order
    from the nodes without a part_of parent, new ids c<k> after the wiki's concepts (an id the
    wiki has is reused), part_of from the tree, prerequisites as unverified `recommended`
    edges, the nodes with parts as regions, targets = the leaves under the first-level blocks
    marked target (none marked: the last block's)."""
    out = to_nodes(mapper)
    nodes = {n.get("id", ""): n for n in out["nodes"]}
    kids: dict[str, list[str]] = {}
    parents: dict[str, list[str]] = {}
    phrase: dict[tuple[str, str], str] = {}
    for r in out.get("relations", []):
        src, dst = r.get("from", ""), r.get("to", "")
        if r.get("kind") == "part_of" and src != dst:
            for end in (src, dst):
                nodes.setdefault(end, {"id": end})
            kids.setdefault(dst, []).append(src)
            parents.setdefault(src, []).append(dst)
            phrase[(src, dst)] = r.get("phrase", "")
    order: list[str] = []

    def walk(i: str) -> None:
        if i in order:
            return
        order.append(i)
        for c in kids.get(i, ()):
            walk(c)
    roots = [i for i in nodes if i not in parents]
    for r in roots:
        walk(r)
    refs = existing_refs(state, k)
    existing = {c.id: c for c in refs} | {
        n.id: ConceptRef(id=n.id, name=n.name) for n in state.graph.nodes if n.id not in
        {c.id for c in refs}}
    taken, nxt, ids = set(existing), len(refs) + 1, {}
    by_name = {norm_name(c.name): c.id for c in existing.values()}
    for i in order:
        if i in existing or not _NEW_ID.fullmatch(i):
            ids[i] = i
            continue
        same = by_name.get(norm_name(nodes[i].get("name", "")))
        if same is not None:                    # the duplicate guard: an existing node's name
            ids[i] = same
            existing.setdefault(i, existing[same])
            continue
        while f"c{nxt}" in taken:
            nxt += 1
        ids[i] = f"c{nxt}"
        taken.add(f"c{nxt}")

    def has_target(i: str) -> bool:
        return bool(nodes[i].get("target")) or any(has_target(c) for c in kids.get(i, ()))

    tops = [r for r in roots if has_target(r)] or roots[-1:]
    targets: list[str] = []

    def leaves(i: str) -> None:
        if not kids.get(i):
            targets.append(ids[i])
        for c in kids.get(i, ()):
            leaves(c)
    for t in tops:
        leaves(t)
    flagged = set(targets) | {ids[t] for t in tops}

    def name(i: str) -> str:
        return nodes[i].get("name") or (existing[i].name if i in existing else i)

    concepts = tuple(IntakeConcept(
        id=ids[i], name=name(i),
        aspects=tuple(Aspect(a) for a in nodes[i].get("aspects", ()))
        or (existing[i].aspects if i in existing else ()) or DEFAULT_ASPECTS,
        definition=nodes[i].get("definition", ""), target=ids[i] in flagged,
        reused=i in existing) for i in order)
    relations = [IntakeRelation(kind="part_of", src=ids[c], dst=ids[p], phrase=phrase[(c, p)])
                 for p in order for c in kids.get(p, ())]
    relations += [IntakeRelation(kind="recommended", src=ids[r["from"]], dst=ids[r["to"]],
                                 phrase=r.get("phrase", ""))
                  for r in out.get("relations", []) if r.get("kind") == "prerequisite"
                  and r.get("from") in ids and r.get("to") in ids]
    regions = tuple(IntakeRegion(id=ids[i], name=c.name, members=tuple(ids[m] for m in kids[i]),
                                 scope="、".join(name(m) for m in kids[i]),
                                 target=c.target, reused=c.reused)
                    for i, c in zip(order, concepts, strict=True) if kids.get(i))
    return IntakeResult(goal=goal, title=out.get("title") or goal, concepts=concepts,
                        targets=tuple(dict.fromkeys(targets)), observations=(), dropped=(),
                        resolutions=(), regions=regions, relations=tuple(relations))


def region_goal(repo: Repo, gw: CountingGateway, user_id: str, goal: str, description: str,
                mapper: dict, *, now: datetime, new_id) -> g.DraftResult:
    """A goal map whose regions have their members from the start: the mapper fixture's tree
    (`mapped_result`), the description's self-reports (the fixture's observations, checked
    against the description as S2's estimate is: `known_from_mapper`, no model call), the
    regions on the map with their members as concept nodes under them (a region inside another
    hangs under it), the graph, then the opening round. The same goal again with a map: its open
    draft, else the map."""
    snap = repo.load(user_id)
    w = g._Work(snap, gw, user_id, now=now, new_id=new_id)
    draft_id = new_id("dr")
    kv_before = AspectKView(snap.k, now)
    res = mapped_result(snap.state, snap.k, goal, mapper)
    raw = mapper if isinstance(mapper, str) else json.dumps(mapper, ensure_ascii=False)
    known = [_RawKnown(**k) for k in known_from_mapper(raw)]
    obs, dropped = _known_observations(known, res.concepts,
                                       (description,) if description.strip() else (),
                                       evidence_ref=f"goal:{draft_id}")
    res = res.model_copy(update={"observations": obs, "dropped": dropped})
    map_id = f"map-{goal_key(goal, res.targets)}"
    if map_id in snap.state.maps:
        open_ = g._open_draft(snap.state, map_id, None)
        if open_ is not None:
            return g.DraftResult(draft=open_, page_id=None, calls=w.calls, open=True)
        return g.DraftResult(draft=g._draft(draft_id, goal, res), page_id=None, calls=w.calls,
                             map_id=map_id)
    g._intake_events(w, res)
    g._goal_graph(w, res, {r.id: r.id for r in res.regions})
    up = {m: r.id for r in res.regions for m in r.members}
    regions = {r.id for r in res.regions}
    nodes: list[MapNode] = [g._prior_region(snap.state, MapNode(
        id=r.id, kind="region", name=r.name, scope=r.scope, parent=up.get(r.id),
        members=tuple(c for c in r.members if c != r.id), target=r.target, reused=r.reused))
        for r in res.regions]
    taken = set(regions)
    for r in list(nodes):
        for c in r.members:
            if c in taken:
                continue
            nodes.append(MapNode(id=c, kind="concept", name=w.state.names[c].name,
                                 parent=r.id, state="expanded", reused=c in snap.state.names))
            taken.add(c)
    m = GoalMap(map_id=map_id, goal=goal, created_at=now, title=res.title, nodes=tuple(nodes),
                words=tuple(x for x in (goal, description) if x.strip()))
    draft = g._draft(draft_id, goal, res, map_id=map_id,
                     evidenced=g._evidenced(kv_before, [c.id for c in res.concepts]),
                     levels=g._levels(res))
    r = g._probe(w, draft, m, ())
    m = g._decision_log(m, r)
    w.add(GoalMapSaved(record=m, action="planned"))
    out = g._open_or_finish(w, draft, m, r)
    w.commit(repo)
    return out


def flat_page(repo: Repo, gw: CountingGateway, user_id: str, draft_id: str, *, now: datetime,
              new_id, outcome: str = "skipped") -> str:
    """A draft finished as ONE page on all its concepts (its title, goal and targets), as the
    eager route finished a goal the path model kept on one page: with more than one concept the
    path model is asked first (one goals.path call; it must answer one page), then the page and
    the draft's DraftClosed in one commit, no path stored. Page tests that only need a page on the
    z-score concepts start from it."""
    snap = repo.load(user_id)
    draft = snap.state.drafts[draft_id]
    if len(draft.concepts) > 1:
        w = g._Work(snap, gw, user_id, now=now, new_id=new_id)
        _, path = g._path_for(w, draft, draft.concepts, draft.targets)
        assert path is None or len(path.pages) == 1, "flat_page: the path model split the goal"
    rec, evs = g.page_events_for(snap.state, snap.k, title=draft.title, goal=draft.goal,
                                 concepts=tuple(c.id for c in draft.concepts),
                                 targets=draft.targets, now=now, new_id=new_id, user_id=user_id)
    evs.append(g._wev(new_id, now, DraftClosed(draft_id=draft_id, outcome=outcome,
                                                page_id=rec.page_id)))
    repo.commit(snap, Commit(wiki=evs))
    return rec.page_id
