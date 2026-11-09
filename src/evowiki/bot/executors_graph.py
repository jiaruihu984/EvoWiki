"""Executors of the assistant's graph, review-card, rule and settings actions (assistant-actions
spec §1.4, §1.5, §1.7, §7, §10): `node_split`, `node_relation`, `node_known`, `node_move`,
 `card_new`, `card_fix` (fix / drop), `review_postpone`, `rule`, `settings`,
`exemplar`.
(`bot.executors_assistant` has the page / path / goal kinds and `register_assistant_executors`,
which also registers these.)

Same pattern as there: the change is ONE commit that carries the ledger's `effect` and the row's
`undo` (`Commit.extra`); `revise` (only `rule` and `settings` take a draft) and `execute` turn every
error into `ActionRefused`; a proposal made against a graph / page that has changed since is
refused, naming what moved; an undo that a later change depends on is refused with the reason.
Model calls (a split plan, the rewrite of a card, the confirm question) go through the injected
gateway - tests use a scripted one.

Content shapes (what Task 5's tools propose):
- node_split `{node_id, graph_seen, reason?: "requested"|"confused"|"inconsistent", words?,
  pages?: [{key, title, covers}]}` - with `pages` (the parts he named, editable on the card) the
  graph gets exactly those and no model is called; without, the executor asks the shared one-level
  split (wikiapp.node_split, source chat) once; it may leave the node whole (`kept` in the result:
  nothing but the atomic mark). `graph_seen` = `graph_fingerprint(graph, ids, parts_only=True)`
  (refused without it, like `path_seen`).
- node_relation `{op: "remove"|"add"|"change", kind, src, dst, phrase?, to?: {kind?, src?, dst?,
  phrase?}, graph_seen}`: an added / changed relation has source `user` and stays an UNVERIFIED
  soft order (unified node graph §7); K cannot forget a prerequisite (`nodes.ksync`).
- node_move `{node_id, parent, keep_old?, graph_seen}`: a new `part_of` parent (the old ones go
  unless `keep_old`).
- node_known `{node_id}`: 「这个我早会了」 - K evidence only (a stored path is not touched: 可以跳过
  is derived from K, skill design A7 #8), then ONE confirm item in
  `result["item"]`; `NodeKnownExecutor.confirm` records the answer and takes the claim back when it
  was wrong.
- card_new `{page_id, cards: [{section_id, cue, answer}]}` (recall cues for sections that have none; a
  hidden cue card of such a section comes back); card_fix `{op: "fix", card_id, opinion}` (one
  model call rewrites the card) or `{op: "drop", card_id}`; review_postpone `{days, scope: "all"|
  "goal"|"cells", goal_id?, cells?}`.
- exemplar `{kind: "wiki_page", page_id}` or `{kind: "vault_note", source_id}` (a 样板页).
- rule `{text, scope?, domain?}`; settings `{changes: {<BotSettings field>: value}}` (`levels`, the
  assistant's own approval levels, only to a STRICTER level - loosening is refused).
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from evowiki.agent.effects import payload_hash
from evowiki.belief.aspects import AspectEvent, Declare, Observe, RetireNote, note_id
from evowiki.bot.actions import ActionRefused, ActionRow, strictest
from evowiki.bot.executors_assistant import (
    Ids,
    _Base,
    _GoalBase,
    _write_undo,
    revise_pages,
)
from evowiki.bot.settings import BotSettings, Scope
from evowiki.goals.contracts import CheckItem, OutlineSection, SectionRole, cell_id
from evowiki.goals.diagnostic import IDK, answer_observation
from evowiki.goals.fold import Page
from evowiki.goals.intake import IntakeError, IntakeResult
from evowiki.goals.review import fading
from evowiki.nodes import GraphError, NodeGraph, Relation, UndoConflict, restore_relations, rewire
from evowiki.nodes import undo as graph_undo
from evowiki.store import bot as store
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.events import (
    PageSaved,
    ReviewCardHidden,
    ReviewCardRestored,
    ReviewPostponed,
    WikiEvent,
)
from evowiki.wikiapp.followup import node_confirm_item
from evowiki.wikiapp.graph_changes import NOT_UNDOABLE
from evowiki.wikiapp.graph_sync import graph_events
from evowiki.wikiapp.kchange import k_changed
from evowiki.wikiapp.kwrite import apply_in_memory, observation_events
from evowiki.wikiapp.llm import (
    LLM_BUDGET,
    LLM_FAILURES,
    LLM_UNAVAILABLE,
    CountingGateway,
    LLMFailed,
    LLMUnavailable,
)
from evowiki.wikiapp.node_split import WHY as SPLIT_WHY
from evowiki.wikiapp.node_split import ask_split, named_split, split_events, split_step
from evowiki.wikiapp.path_edits import commit_paths, skip_plan
from evowiki.wikiapp.path_order import shown
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict, WikiInvalid
from evowiki.wikiapp.review import card_id, deadline_days
from evowiki.wikiapp.review_cards import (
    ANSWER_MAX,
    CUE_MAX,
    Card,
    find_card,
    summary_cue,
)
from evowiki.wikiapp.sectionlog import partial_write
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.teach import (
    MAX_EXEMPLAR_TEXT,
    NOT_ORGANIZED,
    add_exemplar_flow,
    page_exemplar,
    remove_exemplar_flow,
)
from evowiki.wikiapp.teachspec import SpecInvalid
from evowiki.wikiapp.wiki_schemas import VaultNoteExemplarIn, WikiPageExemplarIn

__all__ = [
    "GRAPH_SCOPES",
    "CardFixExecutor",
    "CardNewExecutor",
    "ExemplarExecutor",
    "NodeKnownExecutor",
    "NodeMoveExecutor",
    "NodeRelationExecutor",
    "NodeSplitExecutor",
    "ReviewPostponeExecutor",
    "SettingsExecutor",
    "build_graph_executors",
    "graph_fingerprint",
]

log = logging.getLogger(__name__)

# a write that landed during the action (nothing says a page or this proposal is out of date)
MOVED = "执行时数据刚被别的操作改过，这条没有改动，请再说一次"

Gateway = Callable[[str], CountingGateway | None]

# the scopes each kind touches (spec §1.4 / §1.5 / §1.7); `bot.executors_assistant.SCOPES` has them
GRAPH_SCOPES: dict[str, tuple[Scope, ...]] = {
    "node_split": ("structure",),
    "node_relation": ("structure",),
    "node_known": ("structure",),
    "node_move": ("structure",),
    "card_new": ("suggestion",),
    "card_fix": ("suggestion",),
    "review_postpone": ("suggestion",),
    "rule": ("teaching_params",),
    "stated_rule": ("teaching_params",),
    "restyle_pages": ("teaching_params",),
    "settings": ("preference",),
    "exemplar": ("teaching_params",),
}


def graph_fingerprint(graph: NodeGraph, node_ids: Sequence[str], *,
                      parts_only: bool = False) -> str:
    """The nodes a proposal looked at and the relations touching them, as it saw them
    (`content["graph_seen"]`); positions do not count. `parts_only` (a split): of the relations
    only the nodes' parts: a split does not depend on an edge background work adds to the node."""
    ids = set(node_ids)
    return payload_hash({
        "nodes": [n.model_dump(mode="json", exclude={"x", "y"}) for n in graph.nodes
                  if n.id in ids],
        "relations": [r.model_dump(mode="json") for r in graph.relations
                      if (r.kind == "part_of" and r.dst in ids if parts_only
                          else r.src in ids or r.dst in ids)]})


@contextmanager
def _refusing():
    """Every failure of a library call is an ActionRefused (nothing else may escape `execute`,
    `undo` or `revise`: it would be a 500); a write that landed meanwhile says just that."""
    try:
        yield
    except ActionRefused:
        raise
    except WikiConflict as exc:
        raise ActionRefused(MOVED) from exc
    except LLM_UNAVAILABLE + (LLMUnavailable,) as exc:
        raise ActionRefused("请切换模型重试") from exc
    except LLM_BUDGET as exc:
        raise ActionRefused(exc.line("这条没改成")) from exc
    except (*LLM_FAILURES, LLMFailed) as exc:
        log.warning("a graph action's model answer was unusable: %s", exc)
        raise ActionRefused("这条没改成，请切换模型重试") from exc
    except (WikiInvalid, LookupError, GraphError, SpecInvalid, ValidationError,
            ValueError) as exc:
        raise ActionRefused(str(exc)) from exc


class _Graph(_GoalBase):
    """What the graph executors share (`_GoalBase`: repo, clock, ids, `_mark`)."""

    _undo_data = staticmethod(_Base._undo_data)

    def __init__(self, repo: Repo, gateway: Gateway, clock: Callable[[], datetime], new_id: Ids):
        super().__init__(repo, clock, new_id)
        self._gateway = gateway

    def _gw(self, user_id: str, what: str) -> CountingGateway:
        gw = self._gateway(user_id)
        if gw is None:
            raise ActionRefused("请切换模型重试")
        return gw

    @staticmethod
    def _graph_fresh(graph: NodeGraph, content: Mapping[str, Any], ids: Sequence[str], *,
                     parts_only: bool = False) -> None:
        """The nodes are still as the proposal saw them (`graph_seen`); absent: refused (no
        fail-open). The refusal names the graph's nodes, not a page: no page is involved."""
        for i in ids:
            if graph.node(i) is None:
                raise ActionRefused(f"图谱里没有这个点：{i}")
        seen = content.get("graph_seen")
        if seen is None:
            raise ActionRefused("这条动作没有记下提议时的图谱，不能确认它还适用")
        if seen != graph_fingerprint(graph, ids, parts_only=parts_only):
            names = "、".join(f"「{n.name}」" for i in ids if (n := graph.node(i)) is not None)
            raise ActionRefused(f"{names}在提议之后改过了，这条修改已作废，图谱没有改动")

    @staticmethod
    def _used(state: WikiState, node_ids: Sequence[str]) -> set[str]:
        """Which of `node_ids` a page or a path row uses now."""
        ids = set(node_ids)
        used = {t for pid, pg in state.pages.items() if pid not in state.trashed_pages
                for t in (*pg.targets, *pg.concepts)}
        used |= {n for rec in state.paths.values() for it in shown(rec) for n in it.node_ids}
        return used & ids


# --- node_split ------------------------------------------------------------------------------

COMMIT_TRIES = 3


class NodeSplitExecutor(_Graph):
    """`node_split`: the parts he named (`pages`, no model call), else the shared one-level split
    (wikiapp.node_split, source chat); the parts — new or reused nodes — hang under the node; its
    cells, evidence and relations stay. One commit with the effect and the undo data; a write that
    lands meanwhile (K or the graph moved: a page being written, an answer) only means building the
    events again on the new snapshot, unless the node itself changed. Left whole (`kept`): only the
    atomic mark, and undo only marks the row. Undo: `nodes.ops.undo` of the graph change; refused
    when a later change touched the parts or a page / path uses a new one."""

    def revise(self, action: ActionRow, draft: Any) -> dict[str, Any]:
        return revise_pages(action.content, draft, allow_new=True)

    @staticmethod
    def _named(snap: Snapshot, node_id: str, parts: Sequence[tuple[str, str]]) -> IntakeResult:
        """His parts; what is wrong with them is said as it is (no model was asked)."""
        try:
            return named_split(snap, node_id, parts)
        except IntakeError as exc:
            raise ActionRefused(str(exc)) from exc

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        with _refusing():
            node_id = c.get("node_id")
            if not isinstance(node_id, str):
                raise ActionRefused("要说明拆哪个点")
            snap = self._repo.load(user_id)
            self._graph_fresh(snap.state.graph, c, [node_id], parts_only=True)
            reason, words, now = c.get("reason", "requested"), str(c.get("words") or ""), self._clock()
            if reason not in SPLIT_WHY:
                raise ActionRefused(f"不认识的拆分原因：{reason}")
            named = [(str(p.get("title") or ""), str(p.get("covers") or ""))
                     for p in c["pages"]] if "pages" in c else None
            gw = None if named is not None else self._gw(user_id, "拆这个点")
            extra = {"named": "yes"} if named is not None else {}
            # one run for the call and the commit (current_run_id() at the commit point)
            with split_step(user_id, node_id, source="chat", reason=reason, **extra):
                asked = None if gw is None else ask_split(gw, snap, node_id, reason=reason,
                                                          source="chat", words=words, now=now)
                for left in range(COMMIT_TRIES - 1, -1, -1):
                    res = asked if asked is not None else self._named(snap, node_id, named or ())
                    ev = split_events(snap, node_id, res, reason=reason,
                                      words=" ".join(words.split()), now=now, new_id=self._new_id)
                    change = ev.change
                    undo = {"change_id": change.change_id if change is not None else None,
                            "children": list(ev.added), "node_id": node_id}
                    try:
                        self._repo.commit(snap, Commit(aspect=list(ev.aspect), wiki=ev.wiki,
                                                       extra=[effect, _write_undo(action, undo)]))
                        break
                    except WikiConflict:
                        if left == 0:
                            raise
                        snap = self._repo.load(user_id)
                        self._graph_fresh(snap.state.graph, c, [node_id], parts_only=True)
        made = [(i, n) for i in ev.children if (n := ev.graph.node(i)) is not None]
        return {"node_id": node_id, "children": list(ev.children),
                "parts": [{"key": i, "title": n.name, "covers": n.definition} for i, n in made],
                "change_id": change.change_id if change is not None else None,
                "summary": change.summary if change is not None else ev.kept, "kept": ev.kept,
                "wiki_event_ids": [e.event_id for e in ev.wiki]}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = self._undo_data(action)
        with _refusing():
            snap = self._repo.load(user_id)
            if u.get("change_id") is None:          # left whole: nothing in the graph to undo
                self._repo.commit(snap, Commit(extra=[self._mark(action)]))
                return {"restored": u["node_id"]}
            if clash := self._used(snap.state, u["children"]):
                raise ActionRefused(f"后来别处用到了这几个点：{', '.join(sorted(clash))}，不能撤销")
            try:
                back = graph_undo(snap.state.graph, u["change_id"])
            except UndoConflict as exc:
                raise ActionRefused(NOT_UNDOABLE) from exc
            aspect, wiki = graph_events(snap.state, snap.k, back, "undone", now=self._clock(),
                                        new_id=self._new_id)
            self._repo.commit(snap, Commit(aspect=list(aspect), wiki=wiki,
                                           extra=[self._mark(action)]))
        return {"restored": u["node_id"]}


# --- node_relation / node_move ----------------------------------------------------------------

class _Rewire(_Graph):
    """A relation edit: `rewire` the graph, K + names + `GraphSaved(user_edit)` in one commit with
    the effect and the exact `added` / `removed` relations as the undo data."""

    def _commit(self, user_id: str, action: ActionRow, effect: Callable[[Any], None],
                snap: Snapshot, *, remove: Sequence[tuple[str, str, str]],
                add: Sequence[Relation]) -> dict:
        graph = snap.state.graph
        before = {r.key: r for r in graph.relations}
        new = rewire(graph, remove=remove, add=add)
        after = {r.key: r for r in new.relations}
        added = [r for k, r in after.items() if before.get(k) != r]
        removed = [r for k, r in before.items() if after.get(k) != r]
        if not added and not removed:
            raise ActionRefused("图谱里已经是这样了，没有要改的")
        aspect, wiki = graph_events(snap.state, snap.k, new, "user_edit", now=self._clock(),
                                    new_id=self._new_id)
        undo = {"added": [r.model_dump(mode="json") for r in added],
                "removed": [r.model_dump(mode="json") for r in removed]}
        self._repo.commit(snap, Commit(aspect=list(aspect), wiki=wiki,
                                       extra=[effect, _write_undo(action, undo)]))
        return {"added": undo["added"], "removed": undo["removed"],
                "wiki_event_ids": [e.event_id for e in wiki]}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = self._undo_data(action)
        with _refusing():
            snap = self._repo.load(user_id)
            try:
                back = restore_relations(
                    snap.state.graph, added=[Relation.model_validate(r) for r in u["added"]],
                    removed=[Relation.model_validate(r) for r in u["removed"]])
            except UndoConflict as exc:
                raise ActionRefused("这条关系之后又被改过了，不能撤销：" + str(exc)) from exc
            aspect, wiki = graph_events(snap.state, snap.k, back, "undone", now=self._clock(),
                                        new_id=self._new_id)
            self._repo.commit(snap, Commit(aspect=list(aspect), wiki=wiki,
                                           extra=[self._mark(action)]))
        return {"restored": u["removed"]}


class NodeRelationExecutor(_Rewire):
    """`node_relation`: remove / add / change one relation (「A 不需要 B」). Added and changed
    relations have source `user` and are unverified."""

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        with _refusing():
            op, kind = c.get("op"), c.get("kind", "prerequisite")
            src, dst = c.get("src"), c.get("dst")
            if op not in ("remove", "add", "change") or not isinstance(src, str) \
                    or not isinstance(dst, str):
                raise ActionRefused("要说明改哪一条关系")
            snap = self._repo.load(user_id)
            self._graph_fresh(snap.state.graph, c, [src, dst])
            key = (kind, src, dst)
            if op == "add":
                rel = Relation(kind=kind, src=src, dst=dst, phrase=str(c.get("phrase") or ""),
                               source="user")
                res = self._commit(user_id, action, effect, snap, remove=[], add=[rel])
            elif op == "remove":
                res = self._commit(user_id, action, effect, snap, remove=[key], add=[])
            else:
                to = c.get("to")
                if not isinstance(to, Mapping) or not to:
                    raise ActionRefused("要说明改成什么")
                old = next((r for r in snap.state.graph.relations if r.key == key), None)
                if old is None:
                    raise ActionRefused("图谱里没有这条关系")
                new = Relation(kind=to.get("kind", kind), src=to.get("src", src),
                               dst=to.get("dst", dst), phrase=str(to.get("phrase", old.phrase)),
                               source="user")
                res = self._commit(user_id, action, effect, snap, remove=[key], add=[new])
        return {"op": op, **res}


class NodeMoveExecutor(_Rewire):
    """`node_move`: the node becomes a part of `parent` (the old parents go unless `keep_old`)."""

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        with _refusing():
            node_id, parent = c.get("node_id"), c.get("parent")
            if not isinstance(node_id, str) or not isinstance(parent, str):
                raise ActionRefused("要说明把哪个点挪到哪里")
            snap = self._repo.load(user_id)
            graph = snap.state.graph
            old_parents = list(graph.parents(node_id))
            self._graph_fresh(graph, c, [node_id, parent, *old_parents])
            keep_old = bool(c.get("keep_old", False))
            remove = [] if keep_old else [("part_of", node_id, p) for p in old_parents
                                          if p != parent]
            add = [Relation(kind="part_of", src=node_id, dst=parent, source="user")]
            res = self._commit(user_id, action, effect, snap, remove=remove, add=add)
        return {"node_id": node_id, "parent": parent, "kept_old": keep_old, **res}


# --- node_known -------------------------------------------------------------------------------

class NodeKnownExecutor(_Graph):
    """`node_known`: 「这个我早会了」. K evidence only (`Declare(know)` + the note 「你说：我会了」 on every
    cell, `skip_plan` without a goal: no path is touched; the path shows 可以跳过 from K), then ONE
    confirm question (`followup.node_confirm_item`, written BEFORE anything is recorded: no
    model, no claim) returned in `result["item"]`. `confirm(user_id, action, choice)` records the
    answer as `check` evidence; a wrong one takes the claim back (= `undo`, in the same commit).
    Undo: the declarations go back to what they were, the notes retire; refused when a later
    declaration changed the cells."""

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        with _refusing():
            node_id = c.get("node_id")
            if not isinstance(node_id, str):
                raise ActionRefused("要说明哪个点")
            now = self._clock()
            gw = self._gw(user_id, "出确认题")
            try:
                plan = skip_plan(self._repo, gw, user_id, node_id, None, now=now,
                                 new_id=self._new_id)
            except WikiConflict as exc:           # a node without cells: a reason, not staleness
                raise ActionRefused(str(exc)) from exc
            snap = plan.snap
            cell = next(iter(plan.declared))
            item = node_confirm_item(gw, snap, node_id, cell, now=now)
            undo = {"node_id": node_id, "declared": plan.declared, "notes": list(plan.notes)}
            commit_paths(self._repo, snap, {}, now=now, new_id=self._new_id,
                         source="assistant", aspect=plan.aspect, wiki=plan.wiki,
                         extra=[effect, _write_undo(action, undo)])
        return {"node_id": node_id, "cells": list(plan.declared),
                "item": item.model_dump(mode="json")}

    def _retract(self, user_id: str, action: ActionRow, observed: Sequence[AspectEvent] = ()
                 ) -> None:
        u = self._undo_data(action)
        now = self._clock()
        snap = self._repo.load(user_id)
        for cell in u["declared"]:
            if snap.k.belief(cell, now).declared != "know":
                raise ActionRefused("这个点的“会了”之后又改过了，不能撤销")
        aspect: list[AspectEvent] = list(observed)
        aspect += [Declare(event_id=self._new_id("aev"), ts=now, cell=cell, value=prev)
                   for cell, prev in u["declared"].items()]
        aspect += [RetireNote(event_id=self._new_id("aev"), ts=now, entry_id=note_id(eid))
                   for eid in u["notes"]]
        kc = k_changed(snap, apply_in_memory(snap, aspect), snap.state.current_page,
                       self._gateway(user_id), skip=None, now=now,
                       new_id=self._new_id, action="declare")
        commit_paths(self._repo, snap, {}, now=now, new_id=self._new_id, source="assistant",
                     aspect=tuple(aspect), wiki=tuple(kc.wiki), extra=[self._mark(action)])

    def undo(self, user_id: str, action: ActionRow) -> dict:
        with _refusing():
            self._retract(user_id, action)
        return {"restored": action.undo["node_id"]}

    def confirm(self, user_id: str, action: ActionRow, choice: int) -> dict[str, Any]:
        """Record the answer to the confirm question (`choice`: an option index, or -1 = 不知道).
        Right: the evidence is kept. Wrong (or 不知道): the answer is recorded and the claim taken
        back in ONE commit; `message` says so (shown to the learner). Answered already or the
        claim undone already -> ActionRefused."""
        with _refusing():
            if action.status != "executed" or not action.result or "item" not in action.result:
                raise ActionRefused("这条「我早会了」已经不在了，没有确认题可答")
            item = CheckItem.model_validate(action.result["item"])
            if choice != IDK and not 0 <= choice < len(item.options):
                raise ActionRefused(f"没有这个选项：{choice}")
            ref = f"known:{action.action_id}"
            if self.answered(user_id, action):
                raise ActionRefused("这道确认题已经答过了")
            now = self._clock()
            correct = choice != IDK and choice == item.answer
            obs = answer_observation(item, choice, kind="check", evidence_ref=ref)
            text = item.stem if choice == IDK else item.options[choice]
            observed = observation_events([obs] if obs is not None else [], ts=now,
                                          new_id=self._new_id, note_text=lambda _o: text)
            name = self._node_name(user_id, action)
            if correct:
                self._repo.commit(self._repo.load(user_id), Commit(aspect=observed))
                return {"correct": True, "correct_index": item.answer, "undone": False,
                        "message": f"答对了，「{name}」确实记成会了。"}
            self._retract(user_id, action, observed)
        return {"correct": False, "correct_index": item.answer, "undone": True,
                "message": f"这道确认题没答对，我把「{name}」的“已会”撤回了。"}

    def answered(self, user_id: str, action: ActionRow) -> bool:
        """The confirm question of this 「我早会了」 was answered already (its check is in K)."""
        ref = f"known:{action.action_id}"
        return any(isinstance(e, Observe) and e.observation.evidence_ref == ref
                   for e in self._repo.aspect_events(user_id))

    def _node_name(self, user_id: str, action: ActionRow) -> str:
        rec = self._repo.load(user_id).state.graph.node(action.content["node_id"])
        return rec.name if rec is not None else action.content["node_id"]


# --- review cards -----------------------------------------------------------------------------

def _page_event(new_id: Ids, now: datetime, rec, page: Page, action: str,
                checks: dict[str, int] | None = None) -> WikiEvent:
    record = rec.model_copy(update={"page": page, **({} if checks is None else {"checks": checks})})
    return WikiEvent(event_id=new_id("wev"), ts=now, body=PageSaved(record=record, action=action))


def _marked_page_events(snap: Snapshot, new_id: Ids, now: datetime, rec, page: Page, action: str,
                        sections: Sequence[str], ref: str,
                        checks: dict[str, int] | None = None) -> list[WikiEvent]:
    """`_page_event` for a write that changed `sections`: the record carries a card WriteMark
    and a SectionLog event follows the page (`sectionlog.partial_write`)."""
    ev = _page_event(new_id, now, rec, page, action, checks)
    record, logs = partial_write(snap, ev.body.record, sections, kind="card", ref=ref,
                                 producer="edit", now=now, new_id=new_id)
    return [ev.model_copy(update={"body": ev.body.model_copy(update={"record": record})}), *logs]


def _set_check(page: Page, sid: str, item: CheckItem) -> Page:
    sections = tuple(s.model_copy(update={"check": item}) if s.id == sid else s
                     for s in page.outline.sections)
    return page.model_copy(update={"outline": page.outline.model_copy(update={"sections": sections})})


def _set_cue(page: Page, sid: str, cue: str, answer: str) -> Page:
    t = page.texts[sid]
    return page.model_copy(update={"texts": {**page.texts, sid: t.model_copy(
        update={"cue": cue, "answer": answer})}})


class CardNewExecutor(_Graph):
    """`card_new`: cards for a page's sections (the draft's `cards`). `{section_id, cue, answer}` sets
    a recall card on a section that has none (a hidden cue card of it comes back); `{section_id,
    check: <CheckItem>}` adds a 确认题: a new check section (`{id}-q`, right after the section) with
    the item the assistant wrote (checked at draft time, `assistant.tools.items`; no model call
    here), on its cell (one of the section's). One `PageSaved(card_new)` commit. Undo takes the cues
    and the check sections away again (refused when one was changed since) and re-hides the
    restored cards."""

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        with _refusing():
            page_id, cards = c.get("page_id"), c.get("cards")
            if not isinstance(page_id, str) or not isinstance(cards, list) or not cards:
                raise ActionRefused("要说明给哪一页做哪些卡")
            snap = self._repo.load(user_id)
            _Base._fresh(snap.state, action)
            rec = snap.state.pages.get(page_id)
            if rec is None or not rec.has_body or page_id in snap.state.trashed_pages:
                raise ActionRefused("这页还没写好，做不了复习卡")
            now, page = self._clock(), rec.page
            cues: list[dict[str, str]] = []
            checks: list[str] = []
            restored: list[str] = []
            seen: set[tuple[str, str]] = set()
            for card in cards:
                sid = card.get("section_id") if isinstance(card, Mapping) else None
                sec = next((s for s in page.outline.sections if s.id == sid), None)
                if sec is None:
                    raise ActionRefused(f"这页没有这一段：{sid}")
                for kind in ("cue", "check"):
                    if card.get(kind) in (None, ""):
                        continue
                    if (sid, kind) in seen:
                        raise ActionRefused(f"同一段出现了两次：{sid}")
                    seen.add((sid, kind))
                    if kind == "cue":
                        page = self._add_cue(page, sid, card["cue"], card.get("answer"), cues)
                        cid = card_id(page_id, sid, "cue")
                        if cid in snap.state.hidden_cards:
                            restored.append(cid)
                    else:
                        page = self._add_check(page, sec, card["check"], checks)
                if not any(card.get(k) not in (None, "") for k in ("cue", "check")):
                    raise ActionRefused(f"这一段没有说要做什么卡：{sid}")
            wiki = [*_marked_page_events(
                        snap, self._new_id, now, rec, page, "card_new",
                        [*(m["section_id"] for m in cues), *(f"{x}-q" for x in checks)],
                        action.action_id),
                    *(WikiEvent(event_id=self._new_id("wev"), ts=now,
                                body=ReviewCardRestored(card_id=cid)) for cid in restored)]
            items = {f"{sid}-q": next(x.check for x in page.outline.sections
                                      if x.id == f"{sid}-q").model_dump(mode="json")
                     for sid in checks}
            undo = {"page_id": page_id, "cards": cues, "check_sections": checks,
                    "check_items": items,
                    "restored": restored}
            self._repo.commit(snap, Commit(wiki=wiki, extra=[effect, _write_undo(action, undo)]))
        return {"page_id": page_id,
                "cards": [card_id(page_id, m["section_id"], "cue") for m in cues]
                         + [card_id(page_id, f"{sid}-q", "check") for sid in checks],
                "wiki_event_ids": [e.event_id for e in wiki]}

    @staticmethod
    def _add_cue(page: Page, sid: str, cue: Any, answer: Any, made: list[dict[str, str]]) -> Page:
        t = page.texts.get(sid)
        if t is None:
            raise ActionRefused(f"「{sid}」这一段还没写好")
        if t.cue.strip() and t.answer.strip():
            raise ActionRefused(f"「{sid}」这一段已经有回想卡了")
        cue = " ".join(str(cue).split())
        if not cue or len(cue) > CUE_MAX or summary_cue(cue):
            raise ActionRefused(f"回想提示要问正文里一个具体的点，不超过 {CUE_MAX} 字：{cue or '（空）'}")
        answer = str(answer or "").strip()
        if not answer or len(answer) > ANSWER_MAX:
            raise ActionRefused(f"回想提示要带一两句话的答案，不超过 {ANSWER_MAX} 字：{cue}")
        made.append({"section_id": sid, "cue": cue})
        return _set_cue(page, sid, cue, answer)

    @staticmethod
    def _add_check(page: Page, sec: OutlineSection, raw: Any, made: list[str]) -> Page:
        if sec.role is SectionRole.CHECK or not sec.cells or sec.id not in page.texts:
            raise ActionRefused(f"「{sec.heading}」这一段不能做确认题")
        try:
            item = CheckItem.model_validate(raw)
        except ValueError as exc:
            raise ActionRefused("这道确认题的内容不对") from exc
        cell = item.cell
        if cell not in sec.cells:
            raise ActionRefused(f"确认题的格子不是「{sec.heading}」这一段的")
        if any(s.role is SectionRole.CHECK and s.check is not None and s.check.cell == cell
               for s in page.outline.sections):
            raise ActionRefused(f"「{sec.heading}」已经有确认题了")
        new_id = f"{sec.id}-q"
        if any(s.id == new_id for s in page.outline.sections):
            raise ActionRefused(f"「{sec.heading}」已经有确认题了")
        new = OutlineSection(id=new_id, heading=f"确认一下：{sec.heading}", role=SectionRole.CHECK,
                             cells=(cell,), why="复习用的确认题", check=item)
        sections = list(page.outline.sections)
        sections.insert(next(n for n, s in enumerate(sections) if s.id == sec.id) + 1, new)
        made.append(sec.id)
        return page.model_copy(update={"outline": page.outline.model_copy(
            update={"sections": tuple(sections)})})

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = self._undo_data(action)
        with _refusing():
            snap = self._repo.load(user_id)
            rec = snap.state.pages.get(u["page_id"])
            if rec is None or rec.page is None:
                raise ActionRefused("这页已经不在了，不能撤销")
            page = rec.page
            for m in u["cards"]:
                t = page.texts.get(m["section_id"])
                if t is None or t.cue != m["cue"]:
                    raise ActionRefused("这几张卡之后被改过了，不能撤销")
                page = _set_cue(page, m["section_id"], "", "")
            drop = {f"{sid}-q" for sid in u.get("check_sections", [])}
            have = {s.id for s in page.outline.sections}
            if drop - have:
                raise ActionRefused("这几张确认题之后被改过了，不能撤销")
            for sec in page.outline.sections:
                if sec.id in drop and (sec.check is None or sec.check.model_dump(mode="json")
                                       != u.get("check_items", {}).get(sec.id)):
                    raise ActionRefused("这几张确认题之后被改过了，不能撤销")
            if drop:
                page = page.model_copy(update={"outline": page.outline.model_copy(update={
                    "sections": tuple(s for s in page.outline.sections if s.id not in drop)})})
            checks = {k: v for k, v in rec.checks.items() if k not in drop}
            now = self._clock()
            wiki = [_page_event(self._new_id, now, rec, page, "card_new_undone", checks),
                    *(WikiEvent(event_id=self._new_id("wev"), ts=now,
                                body=ReviewCardHidden(card_id=cid, kind="drop"))
                      for cid in u["restored"])]
            self._repo.commit(snap, Commit(wiki=wiki, extra=[self._mark(action)]))
        return {"removed": [m["section_id"] for m in u["cards"]] + sorted(drop)}


class CardFixExecutor(_Graph):
    """`card_fix`: `fix` puts the new cue and answer, or question, the assistant wrote (checked at draft time:
    `assistant.tools.actions._new_face`; no model call here; the signal of the 这张卡有问题 box is
    not recorded - the chat holds the words), `drop` hides it. Undo puts the old cue / question back
    (refused when the card was changed since) or shows the card again."""

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        with _refusing():
            op, cid = c.get("op"), c.get("card_id")
            if op not in ("fix", "drop") or not isinstance(cid, str):
                raise ActionRefused("要说明改哪张卡、是改还是删")
            snap = self._repo.load(user_id)
            card = find_card(snap.state, cid)
            if card is None:
                raise ActionRefused("没有找到这张复习卡（可能已经删掉了）")
            _Base._fresh(snap.state, action)
            now = self._clock()
            if op == "drop":
                undo = {"op": "drop", "card_id": cid}
                self._repo.commit(snap, Commit(
                    wiki=[WikiEvent(event_id=self._new_id("wev"), ts=now,
                                    body=ReviewCardHidden(card_id=cid, kind="drop"))],
                    extra=[effect, _write_undo(action, undo)]))
                return {"card_id": cid, "dropped": True}
            page = self._fixed(card, c)
            sid = card.section.id
            undo = self._fix_undo(card, page)
            checks = None
            if card.mode == "check":                   # a new question: unanswered again
                checks = {k: v for k, v in card.rec.checks.items() if k != sid}
            self._repo.commit(snap, Commit(
                wiki=_marked_page_events(snap, self._new_id, now, card.rec, page, "card_fix",
                                         [sid], action.action_id, checks),
                extra=[effect, _write_undo(action, undo)]))
        return {"card_id": cid, "page_id": card.rec.page_id, "section_id": sid,
                "mode": card.mode}

    @staticmethod
    def _fixed(card: Card, c: Mapping[str, Any]) -> Page:
        """The page with the card's new text from the row (the mode's field only)."""
        sid = card.section.id
        if card.mode == "cue":
            cue, answer = c.get("cue"), c.get("answer")
            if not isinstance(cue, str) or not cue.strip() or not isinstance(answer, str) or not answer.strip():
                raise ActionRefused("这条改卡没有带新的回想提示和答案")
            return _set_cue(card.page, sid, cue.strip(), answer.strip())
        try:
            item = CheckItem.model_validate(c.get("check"))
        except ValueError as exc:
            raise ActionRefused("这条改卡没有带新的题") from exc
        if card.section.check is None or item.cell != card.section.check.cell:
            raise ActionRefused("新题测的不是这张卡的格子")
        return _set_check(card.page, sid, item)

    @staticmethod
    def _fix_undo(card: Card, page: Page) -> dict[str, Any]:
        sid = card.section.id
        if card.mode == "cue":
            old, new = card.page.texts[sid], page.texts[sid]
            return {"op": "fix", "mode": "cue", "page_id": card.rec.page_id, "section_id": sid,
                    "old": old.cue, "new": new.cue, "old_answer": old.answer, "new_answer": new.answer}
        new = next(s.check for s in page.outline.sections if s.id == sid)
        assert card.section.check is not None and new is not None
        return {"op": "fix", "mode": "check", "page_id": card.rec.page_id, "section_id": sid,
                "old": card.section.check.model_dump(mode="json"),
                "new": new.model_dump(mode="json"), "old_answer": card.rec.checks.get(sid)}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = self._undo_data(action)
        with _refusing():
            snap = self._repo.load(user_id)
            now = self._clock()
            if u["op"] == "drop":
                if u["card_id"] not in snap.state.hidden_cards:
                    raise ActionRefused("这张卡已经显示出来了")
                self._repo.commit(snap, Commit(
                    wiki=[WikiEvent(event_id=self._new_id("wev"), ts=now,
                                    body=ReviewCardRestored(card_id=u["card_id"]))],
                    extra=[self._mark(action)]))
                return {"restored": u["card_id"]}
            rec = snap.state.pages.get(u["page_id"])
            if rec is None or rec.page is None:
                raise ActionRefused("这页已经不在了，不能撤销")
            sid, checks = u["section_id"], None
            if u["mode"] == "cue":
                t = rec.page.texts.get(sid)
                if t is None or (t.cue, t.answer) != (u["new"], u["new_answer"]):
                    raise ActionRefused("这张卡之后又被改过了，不能撤销")
                page = _set_cue(rec.page, sid, u["old"], u["old_answer"])
            else:
                sec = next((s for s in rec.page.outline.sections if s.id == sid), None)
                if sec is None or sec.check is None \
                        or sec.check.model_dump(mode="json") != u["new"]:
                    raise ActionRefused("这张卡之后又被改过了，不能撤销")
                page = _set_check(rec.page, sid, CheckItem.model_validate(u["old"]))
                if u.get("old_answer") is not None and sid not in rec.checks:
                    checks = {**rec.checks, sid: u["old_answer"]}
            self._repo.commit(snap, Commit(
                wiki=[_page_event(self._new_id, now, rec, page, "card_fix_undone", checks)],
                extra=[self._mark(action)]))
        return {"restored": sid}


class ReviewPostponeExecutor(_Graph):
    """`review_postpone`: the due date of the chosen cells moves `days` later
    (`ReviewPostponed`; `goals.review.fading(postponed=...)`) - the forgetting model is not
    touched. Undo gives each cell its earlier postponement back (refused when one moved since)."""

    MAX_DAYS = 365

    def __init__(self, repo: Repo, gateway: Gateway, clock: Callable[[], datetime], new_id: Ids,
                 *, pool):
        super().__init__(repo, gateway, clock, new_id)
        self._pool = pool                     # the Bot settings' time zone (deadline days)

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        with _refusing():
            days, scope = c.get("days"), c.get("scope", "all")
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= self.MAX_DAYS:
                raise ActionRefused(f"推迟的天数要在 1 到 {self.MAX_DAYS} 之间")
            snap = self._repo.load(user_id)
            now, state = self._clock(), snap.state
            if scope == "cells":
                cells = c.get("cells")
                if not isinstance(cells, list) or not cells:
                    raise ActionRefused("要说明推迟哪些复习")
                known = {cell_id(k, a) for k in snap.k.concept_ids() for a in snap.k.aspects(k)}
                if bad := [x for x in cells if x not in known]:
                    raise ActionRefused(f"不认识的格子：{bad[0]}")
            elif scope in ("all", "goal"):
                with self._pool.connection() as conn:
                    tz = store.get_settings(conn, user_id).timezone
                due = [f.cell for f in fading(snap.k, now, state.review,
                                              deadline_days(state, now, tz),
                                              state.review_postponed)]
                if scope == "goal":
                    gid = c.get("goal_id")
                    if not isinstance(gid, str) or gid not in state.paths:
                        raise ActionRefused("这个目标已经不在了")
                    wanted = {x for p in state.paths[gid].path.pages for x in p.concepts}
                    due = [x for x in due if x.rsplit(".", 1)[0] in wanted]
                cells = due
                if not cells:
                    raise ActionRefused("现在没有到期的复习可以推迟")
            else:
                raise ActionRefused(f"不认识的范围：{scope}")
            cells = list(dict.fromkeys(cells))
            until = now + timedelta(days=days)
            earlier = {x: state.review_postponed.get(x) for x in cells}
            undo = {"until": until.isoformat(),
                    "earlier": {x: (t.isoformat() if t else None) for x, t in earlier.items()}}
            self._repo.commit(snap, Commit(
                wiki=[WikiEvent(event_id=self._new_id("wev"), ts=now,
                                body=ReviewPostponed(until=dict.fromkeys(cells, until)))],
                extra=[effect, _write_undo(action, undo)]))
        return {"cells": cells, "until": until.isoformat(), "days": days}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = self._undo_data(action)
        with _refusing():
            snap = self._repo.load(user_id)
            now = self._clock()
            until = datetime.fromisoformat(u["until"])
            for cell in u["earlier"]:
                if snap.state.review_postponed.get(cell) != until:
                    raise ActionRefused("这几项复习之后又推迟过了，不能撤销")
            back = {cell: (datetime.fromisoformat(t) if t else None)
                    for cell, t in u["earlier"].items()}
            self._repo.commit(snap, Commit(
                wiki=[WikiEvent(event_id=self._new_id("wev"), ts=now,
                                body=ReviewPostponed(until=back))],
                extra=[self._mark(action)]))
        return {"restored": list(back)}


# --- settings (teaching preferences and restyling: bot.executors_prefs) -------------------------

class SettingsExecutor(_Graph):
    """`settings`: `bot.settings.BotSettings` fields (「今天别提醒了」「卡少一点」), stored through
    `store.put_settings` in the effect's transaction. The approval `levels` are the learner's own:
    only a stricter level is accepted (「新建页面先问我」), a loosening one is refused. Undo puts the old values back (refused when a field changed since)."""

    def __init__(self, pool, repo: Repo, clock: Callable[[], datetime], new_id: Ids):
        super().__init__(repo, lambda uid: None, clock, new_id)
        self._pool = pool

    @staticmethod
    def _changes(cur: BotSettings, c: Mapping[str, Any]) -> dict[str, Any]:
        """The new value of every field that changes (validated as a whole BotSettings)."""
        changes = c.get("changes")
        if not isinstance(changes, Mapping) or not changes:
            raise ActionRefused("要说明改哪些设置")
        unknown = set(changes) - set(BotSettings.model_fields)
        if unknown:
            raise ActionRefused(f"没有这些设置：{', '.join(sorted(unknown))}")
        if "levels" in changes:
            lv = changes["levels"]
            if not isinstance(lv, Mapping):
                raise ActionRefused("级别的格式不对")
            for scope, new_level in lv.items():
                old_level = cur.levels.get(scope)
                if new_level not in ("never", "ask", "auto_undo", "auto") or old_level is None:
                    raise ActionRefused(f"不认识的级别设置：{scope} = {new_level}")
                if strictest([old_level, new_level]) != new_level:
                    raise ActionRefused("放宽“要不要我批准”要在设置里自己改；我只能把它改得更严（比如改成先问你）")
        base = cur.model_dump(mode="json")
        new = dict(base)
        for k, v in changes.items():
            new[k] = {**base[k], **v} if isinstance(base[k], dict) and isinstance(v, Mapping) else v
        try:
            checked = BotSettings.model_validate(new).model_dump(mode="json")
        except ValidationError as exc:
            raise ActionRefused(f"这个设置不行：{exc.errors()[0]['msg']}") from exc
        return {k: checked[k] for k in changes if checked[k] != base[k]}

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        now = self._clock()
        with self._pool.connection() as conn, conn.transaction():
            cur = store.get_settings(conn, user_id)
            new = self._changes(cur, action.content)
            if not new:
                raise ActionRefused("这些设置已经是这样了")
            base = cur.model_dump(mode="json")
            store.put_settings(conn, user_id, BotSettings.model_validate({**base, **new}), now)
            undo = {"old": {k: base[k] for k in new}, "new": new}
            store.set_action_undo(conn, action.action_id, undo)
            effect(conn)
        return {"changed": sorted(new), "old": undo["old"], "new": new}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = self._undo_data(action)
        now = self._clock()
        with self._pool.connection() as conn, conn.transaction():
            cur = store.get_settings(conn, user_id)
            base = cur.model_dump(mode="json")
            if any(base[k] != v for k, v in u["new"].items()):
                raise ActionRefused("这些设置之后又改过了，不能撤销")
            store.put_settings(conn, user_id, BotSettings.model_validate({**base, **u["old"]}),
                               now)
            self._mark(action)(conn)
        return {"restored": sorted(u["old"])}


# --- exemplar ----------------------------------------------------------------------------------

EXEMPLAR_FULL = f"样板页最多 {tspec.MAX_EXEMPLARS} 个，要先去掉一个"
EXEMPLAR_TWICE = "这一页已经是样板页了"


class PoolNotes:
    """The two `SourceReader` reads `exemplar` needs (get / sections) over the source tables; a
    deleted source reads as unknown."""

    def __init__(self, pool):
        self._pool = pool

    def get(self, user_id: str, source_id: str):
        from evowiki.sources import store as sources_store
        with self._pool.connection() as conn:
            row = sources_store.get_source(conn, user_id, source_id)
        return row if row is not None and row.deleted_at is None else None

    def sections(self, user_id: str, source_id: str):
        from evowiki.sources import store as sources_store
        with self._pool.connection() as conn:
            return sources_store.sections(conn, user_id, source_id)


def note_markdown(sections: Sequence[Any]) -> str:
    """A note sent to the chat (a material) as Markdown again: each section's heading (the table
    of contents took the `#` lines out of the text) and its text, page marks dropped."""
    from evowiki.sources.config import PAGE_MARK_RE
    from evowiki.sources.toc import LEADING_TEXT_TITLE
    parts: list[str] = []
    for s in sorted(sections, key=lambda x: x.ord):
        if s.read_by == "none":
            continue
        if s.title.strip() and s.title != LEADING_TEXT_TITLE:
            parts.append(f"{'#' * (s.depth + 1)} {s.title.strip()}")
        if body := PAGE_MARK_RE.sub("", s.text).strip():
            parts.append(body)
    return "\n\n".join(parts).strip()


def exemplar_in(state: WikiState, content: Mapping[str, Any], sources: Any,
                user_id: str) -> tuple[Any, str]:
    """(the `ExemplarIn` body, its title) of an `exemplar` row's content - `{"kind": "wiki_page",
    "page_id"}` or `{"kind": "vault_note", "source_id"}` (a note the learner sent to the chat) -
    checked against the current spec: at most 3, not one that already is. ActionRefused otherwise."""
    spec = tspec.effective_spec(state)
    kind = content.get("kind")
    if kind == "wiki_page":
        pid = content.get("page_id")
        rec = state.pages.get(pid) if isinstance(pid, str) else None
        if rec is None or pid in state.trashed_pages:
            raise ActionRefused(f"没有这一页：{pid}")
        if not rec.has_body:
            raise ActionRefused(NOT_ORGANIZED)
        with _refusing():
            ex = page_exemplar(state, pid, "check")          # the size check of the real one
        body: Any = WikiPageExemplarIn(kind="wiki_page", ref=pid)
        title = ex.title
    elif kind == "vault_note":
        sid = content.get("source_id")
        src = sources.get(user_id, sid) if sources is not None and isinstance(sid, str) else None
        if src is None:
            raise ActionRefused(f"没有这份笔记：{sid}")
        if src.status == "reading":
            raise ActionRefused("这篇笔记还没读完")
        text = note_markdown(sources.sections(user_id, sid))
        if not text:
            raise ActionRefused("这篇笔记里没有读出文字")
        if len(text) > MAX_EXEMPLAR_TEXT:
            raise ActionRefused(f"样板页最多 {MAX_EXEMPLAR_TEXT} 字，这篇有 {len(text)} 字")
        ref = (src.origin or sid)[:400]
        title = (src.title.strip() or ref)[:200]
        body = VaultNoteExemplarIn(kind="vault_note", ref=ref, title=title, text=text)
    else:
        raise ActionRefused("要说明是哪一页或哪篇笔记")
    if any(e.kind == body.kind and e.ref == body.ref for e in spec.exemplars):
        raise ActionRefused(EXEMPLAR_TWICE)
    if len(spec.exemplars) >= tspec.MAX_EXEMPLARS:
        raise ActionRefused(EXEMPLAR_FULL + "：" + "、".join(f"「{e.title}」" for e in spec.exemplars))
    return body, title


class ExemplarExecutor(_GoalBase):
    """`exemplar`: a page or note set as a 样板页 (the teaching spec's exemplars, at most 3) through
    `wikiapp.teach.add_exemplar_flow` - the same save as 设置 → 样板页 - with the ledger's effect and
    the undo data in that commit. A wiki page is snapshotted at execution (the organized page as
    it is then); a note is the text of the material the plugin sent. Undo removes it
    (`remove_exemplar_flow`); refused when it was removed since."""

    def __init__(self, repo: Repo, clock: Callable[[], datetime], new_id: Ids, *, sources: Any):
        super().__init__(repo, clock, new_id)
        self._sources = sources

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        snap = self._repo.load(user_id)
        body, title = exemplar_in(snap.state, action.content, self._sources, user_id)
        exemplar_id = self._new_id("ex")
        with _refusing():
            add_exemplar_flow(self._repo, user_id, body, now=self._clock(), new_id=self._new_id,
                              exemplar_id=exemplar_id, commit_extra=[
                                  effect, _write_undo(action, {"exemplar_id": exemplar_id})])
        return {"exemplar_id": exemplar_id, "title": title}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = _Base._undo_data(action)
        spec = tspec.effective_spec(self._repo.load(user_id).state)
        if all(e.exemplar_id != u["exemplar_id"] for e in spec.exemplars):
            raise ActionRefused("这个样板页之后已经去掉了，不用再撤销")
        with _refusing():
            remove_exemplar_flow(self._repo, user_id, u["exemplar_id"], now=self._clock(),
                                 new_id=self._new_id, commit_extra=[self._mark(action)])
        return {"removed": u["exemplar_id"]}


def build_graph_executors(repo: Repo, clock: Callable[[], datetime], new_id: Ids, *,
                          gateway: Gateway, pool) -> dict[str, Any]:
    """The executors of every kind in `GRAPH_SCOPES`, by kind."""
    from evowiki.bot.executors_prefs import PreferenceExecutor, RestyleExecutor
    base = (repo, gateway, clock, new_id)
    return {
        "node_split": NodeSplitExecutor(*base),
        "node_relation": NodeRelationExecutor(*base),
        "node_known": NodeKnownExecutor(*base),
        "node_move": NodeMoveExecutor(*base),
        "card_new": CardNewExecutor(*base),
        "card_fix": CardFixExecutor(*base),
        "review_postpone": ReviewPostponeExecutor(*base, pool=pool),
        "rule": PreferenceExecutor(*base),
        "stated_rule": PreferenceExecutor(*base),
        "restyle_pages": RestyleExecutor(*base),
        "settings": SettingsExecutor(pool, repo, clock, new_id),
        "exemplar": ExemplarExecutor(repo, clock, new_id, sources=PoolNotes(pool)),
    }
