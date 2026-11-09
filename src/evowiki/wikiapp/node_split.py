"""Split one node of the graph one level finer from a 细分 entrance (spec 2026-10-29: graph.split
merged into goals.enter). There is ONE rule of what a part is — `goals.intake.decompose_one_level`,
the same call (purpose `goals.enter`, enter@v2) that entering a block makes; this module only adds
what a 细分 knows (`SplitTrigger`: the reason, the learner's words, the evidence lines K has on the
node) and commits the result the 细分 way (`nodes.ops.split`: change kind "split", undoable, with
the reason and his words).

The entrances keep their own trigger, confirmation and commit; each has its own source, written
into the request's `params["source"]` (the llm_call row; RSI analyses the splits per entrance):
- 看不懂 (`POST /wiki/nodes/{id}/split`): reason confused, source `dont_get`;
- 继续拆细 (the same route with reason requested): source `continue`;
- asked in chat (the assistant's `split_node` → `node_split` executor): requested, `chat`;
- 拆成多页 (`path_edits.split_row`): requested, `path_row` — the only one that asks each part's
  `question` (the new path row's line).

Where the node sits: on a goal map (the one passed, else the first that has it) the call sees
what entering it there would (`goal._scope_for`: its level on the map, the blocks above it); off
any map its place in the graph (the first parent's parts as its level, the first-parent chain as
its ancestors). Graph ancestors the scope does not show are `blocked` (no part may be one).

The result is either parts — new nodes (`new_id("n")`) and reused existing ones (`existing_id`:
only their part_of is added), the model's `atomic` per part (its first call, never a verified
fact), `recommended` among them — or the node left whole (`IntakeResult.kept`: one line to the
learner; a normal result, not a failure): then only `ConceptName.atomic` True is written (relative
to the current grain; 继续拆细 can still split it). Either way K's evidence on the node stays on
it: part_of carries no evidence and the parts start with none. An unusable answer, asked once
more, is `IntakeError` (fail closed, nothing written).

When the learner named the parts himself (the chat's `split_node` with `parts`, shown and editable
on the card), `named_split` makes the same result from his list and no model is called."""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from evowiki.belief.aspects import AspectEvent, AspectKView
from evowiki.goals.intake import (
    IntakeResult,
    IntakeScope,
    SplitTrigger,
    decompose_one_level,
    named_parts,
)
from evowiki.goals.regions import GoalMap
from evowiki.llm.run_context import StepRun, traced_step
from evowiki.nodes import ops
from evowiki.nodes.model import GraphChange, NodeGraph, NodeRec, Relation
from evowiki.wikiapp.events import GoalMapSaved, WikiEvent
from evowiki.wikiapp.goal import _annotations, _graph_relations, _scope_for, _topics
from evowiki.wikiapp.goal_nodes import entered_nodes
from evowiki.wikiapp.graph_sync import graph_events
from evowiki.wikiapp.kwrite import Ids, existing_refs
from evowiki.wikiapp.llm import CountingGateway, GatewayJsonLLM
from evowiki.wikiapp.node_state import child_nodes, marked
from evowiki.wikiapp.node_view import node_view
from evowiki.wikiapp.repo import Commit, Repo, Snapshot
from evowiki.wikiapp.state import WikiState, apply_event

STEP = "decompose"         # the prompt step 「知识点拆分」 (goals.enter): the traced step of every 细分
SplitReason = Literal["confused", "inconsistent", "requested"]
SplitSource = Literal["dont_get", "continue", "chat", "path_row"]
# what the learner sees as the change's reason (参数 tab / the graph's change list)
WHY: dict[str, str] = {"confused": "你点了看不懂", "inconsistent": "你在这个点上有的答对有的答错",
                       "requested": "你要求把它拆细"}


def _map_of(state: WikiState, node_id: str, map_id: str | None) -> GoalMap | None:
    """The goal map a split of the node is seen from: the one passed, else the first that has it."""
    maps = [state.maps[map_id]] if map_id is not None else list(state.maps.values())
    return next((x for x in maps if x.node(node_id) is not None), None)


def split_scope(snap: Snapshot, node_id: str, map_id: str | None = None
                ) -> tuple[IntakeScope, tuple[str, ...]]:
    """The node's scope (module doc) and his conditions for the map's goal. LookupError: no such
    node or map."""
    state = snap.state
    g = state.graph
    rec = g.node(node_id)
    if rec is None:
        raise LookupError(f"没有这个节点：{node_id}")
    if map_id is not None and map_id not in state.maps:
        raise LookupError(f"没有这张学习地图：{map_id}")
    m = _map_of(state, node_id, map_id)
    up = g.ancestors(node_id)
    if m is not None:
        here = m.node(node_id)
        assert here is not None
        scope = _scope_for(state, m, here)
        shown = {i for i, _ in scope.ancestors}
        return (scope.model_copy(update={"blocked": tuple(a for a in up if a not in shown)}),
                m.limits)
    parents = g.parents(node_id)
    level = g.children(parents[0]) if parents else ()
    chain: list[tuple[str, str]] = []
    cur = parents[0] if parents else None
    while cur is not None and all(cur != i for i, _ in chain) and cur != node_id:
        node = g.node(cur)
        chain.append((cur, node.name if node is not None else cur))
        ps = g.parents(cur)
        cur = ps[0] if ps else None
    shown = {i for i, _ in chain}
    sib = [(n.name, n.definition) for i in level if (n := g.node(i)) is not None]
    return IntakeScope(goal="", region_id=node_id, region_name=rec.name, scope=rec.definition,
                       parent_goal="", siblings=tuple(sib), ancestors=tuple(chain),
                       blocked=tuple(a for a in up if a not in shown)), ()


def split_step(user_id: str, node_id: str, *, source: SplitSource, reason: SplitReason,
               **extra: str) -> AbstractContextManager[StepRun]:
    """The 细分's traced run (observability plan §5 1a; step `decompose`, the entrance's `source`
    and `reason` in its input, so RSI reads each entrance's splits apart). An entrance wraps its
    call and its commit in it (current_run_id() at the commit point); `ask_split` joins it."""
    return traced_step(STEP, f"graph/{node_id}", user_id,
                       input={"source": source, "reason": reason, **extra})


def ask_split(gw: CountingGateway, snap: Snapshot, node_id: str, *, reason: SplitReason,
              source: SplitSource, words: str = "", map_id: str | None = None, now: datetime,
              question: bool = False) -> IntakeResult:
    """ONE call (asked once more when unusable): the parts, or `kept`. IntakeError: unusable
    twice; LookupError: no such node or map; LLM errors propagate."""
    scope, limits = split_scope(snap, node_id, map_id)
    state, k = snap.state, snap.k
    trigger = SplitTrigger(reason=reason, words=" ".join(words.split()),
                           evidence=node_view(snap, node_id, now).notes, question=question)
    llm = GatewayJsonLLM(gw, subject=f"graph/{node_id}", params={"source": source})
    with split_step(gw.user_id or snap.user_id, node_id, source=source, reason=reason) as step:
        res = decompose_one_level(
            llm, scope.goal, (), existing_refs(state, k), evidence_ref=f"split:{node_id}",
            scope=scope, language=state.language,
            annotations=_annotations(state, AspectKView(k, now)), topics=_topics(state),
            graph_relations=_graph_relations(state), limits=limits, trigger=trigger,
            shown=entered_nodes(state, _map_of(state, node_id, map_id), node_id, scope))
        step.stop("kept" if res.kept else "split")
        return res


def named_split(snap: Snapshot, node_id: str, parts: Sequence[tuple[str, str]],
                map_id: str | None = None) -> IntakeResult:
    """His own parts, each (name, one line), as a split of the node, against this snapshot.
    IntakeError: they cannot be a split; LookupError: no such node or map."""
    scope, _ = split_scope(snap, node_id, map_id)
    return named_parts(parts, existing_refs(snap.state, snap.k), scope=scope,
                       topics=_topics(snap.state), graph_relations=_graph_relations(snap.state))


@dataclass(frozen=True)
class SplitEvents:
    """The split as the events of one commit (`apply_split` commits them; `path_edits.split_row`
    and the `node_split` executor add theirs to the same commit). Left whole (`kept`): no
    children, no graph change, only the atomic mark."""

    children: tuple[str, ...]          # the parts' ids (new and reused), in the model's order
    change: GraphChange | None
    graph: NodeGraph                   # after the split
    questions: dict[str, str]          # part id -> the question its page answers ("" if none)
    aspect: tuple[AspectEvent, ...]
    wiki: list[WikiEvent]
    kept: str = ""
    added: tuple[str, ...] = field(default=())   # the new nodes among `children`


def _apply(state: WikiState, evs: list[WikiEvent]) -> WikiState:
    for ev in evs:
        state = apply_event(state, ev)
    return state


def split_events(snap: Snapshot, node_id: str, res: IntakeResult, *, reason: SplitReason,
                 words: str, now: datetime, new_id: Ids, map_id: str | None = None
                 ) -> SplitEvents:
    """The result's events (module doc): the parts and their part_of into the shared graph, each
    part's `atomic`, the node marked not atomic, and only the map the split came from (`map_id`,
    when it has the node) gets the parts under it, not entered yet. K: new parts become concepts
    with the model's aspects and no evidence; the node keeps its own."""
    state, g = snap.state, snap.state.graph
    parent = g.node(node_id)
    if parent is None:
        raise LookupError(f"没有这个节点：{node_id}")
    if res.kept:
        flag = marked(state, {node_id: True}, {node_id: parent.name})
        wiki = [WikiEvent(event_id=new_id("wev"), ts=now, body=flag)] if flag is not None else []
        return SplitEvents(children=(), change=None, graph=g, questions={}, aspect=(),
                           wiki=wiki, kept=res.kept)
    ids = {c.id: (c.id if c.reused else new_id("n")) for c in res.concepts}
    new = [c for c in res.concepts if not c.reused]
    # a reused concept K names but the graph lacks (e.g. a part of an undone split): it comes
    # back as a node of this split under its recorded name, no new K concept
    back = [c for c in res.concepts if c.reused and g.node(c.id) is None]
    kids = [NodeRec(id=ids[c.id], name=c.name, definition=c.definition) for c in new]
    kids += [NodeRec(id=c.id, name=state.names[c.id].name if c.id in state.names else c.name,
                     definition=c.definition) for c in back]
    known = {n.id for n in g.nodes} | {k.id for k in kids}
    links = [Relation(kind=r.kind, src=ids.get(r.src, r.src), dst=ids.get(r.dst, r.dst),
                      phrase=r.phrase, source="split")
             for r in res.relations if r.kind == "recommended"]
    on = {ids[c.id] for c in res.concepts}
    links = [r for r in links if r.src in known and r.dst in known and {r.src, r.dst} & on]
    graph = ops.split(g, node_id, kids, links, change_id=new_id("g"), at=now, why=WHY[reason],
                      summary=f"把「{parent.name}」拆成了几个小点", words=words,
                      reused=[c.id for c in res.concepts if c.reused and g.node(c.id) is not None])
    aspect, wiki = graph_events(state, snap.k, graph, "split", now=now, new_id=new_id,
                                aspects={ids[c.id]: c.aspects for c in new})
    after = _apply(state, wiki)
    flag = marked(after, {node_id: False, **{ids[c.id]: c.atomic for c in res.concepts
                                             if c.atomic is not None}},
                  {node_id: parent.name, **{ids[c.id]: c.name for c in res.concepts}})
    if flag is not None:
        ev = WikiEvent(event_id=new_id("wev"), ts=now, body=flag)
        wiki.append(ev)
        after = apply_event(after, ev)
    m = after.maps.get(map_id) if map_id is not None else None
    here = m.node(node_id) if m is not None else None
    order = tuple(ids[c.id] for c in res.concepts)
    if m is not None and here is not None:
        shown = {ids[c.id]: c.name for c in res.concepts}
        children, _ = child_nodes(after, m, here, order, shown,
                                  scopes={ids[c.id]: c.in_goal for c in res.concepts if c.in_goal})
        saved = m.replace(here.model_copy(update={"state": "expanded"}), *children)
        wiki.append(WikiEvent(event_id=new_id("wev"), ts=now,
                              body=GoalMapSaved(record=saved, action="expanded")))
    return SplitEvents(children=order, change=graph.changes[-1], graph=graph,
                       questions={ids[c.id]: c.question for c in res.concepts},
                       aspect=tuple(aspect), wiki=wiki, added=tuple(ids[c.id] for c in new))


@dataclass(frozen=True)
class SplitDone:
    children: tuple[str, ...]          # the parts' ids, in the model's order (() when kept)
    change: GraphChange | None
    snap: Snapshot                     # after the commit
    kept: str = ""


def split_node(repo: Repo, gw: CountingGateway, user_id: str, node_id: str, *,
               reason: SplitReason, source: SplitSource, words: str = "", now: datetime,
               new_id: Ids, map_id: str | None = None) -> SplitDone:
    """Ask and commit (module doc; `map_id`: the goal map it was split from). LookupError: no
    such node or map; IntakeError: unusable answer; WikiConflict: the user's logs moved during
    the call (nothing written)."""
    snap = repo.load(user_id)
    with split_step(user_id, node_id, source=source, reason=reason):
        res = ask_split(gw, snap, node_id, reason=reason, source=source, words=words,
                        map_id=map_id, now=now)
        ev = split_events(snap, node_id, res, reason=reason, words=" ".join(words.split()),
                          now=now, new_id=new_id, map_id=map_id)
        done = repo.commit(snap, Commit(aspect=list(ev.aspect), wiki=ev.wiki))
    return SplitDone(children=ev.children, change=ev.change, snap=done, kept=ev.kept)
