"""The graph's groups named soon after the graph changes (spec 2026-10-29 retire-curate §5): code's
names first (`graph_groups.settle`), the model step `graph_group_naming` only for the groups code
cannot name (`graph_groups.name_groups`), as a DBOS workflow `evowiki.graph_groups` on the queue
`graph-groups`, one run at a time per user. It never changes the graph or K: its only event is
`GroupsNamed`.

Trigger: the groups change only when the graph does, and every graph change is a commit with
`graph_saved` (a goal's nodes, a split, a merge, an edit, an undo). PgRepo
calls the graph listener after such a commit (`repo.listen_graph`; not for a `relayout`, which
moves nodes only); `request_naming` then enqueues a run when some group has no name and inherits
none (`graph_groups.to_name`). Reads never name anything: GET /wiki/graph serves the fallback
names until a run has stored the real ones.

Debounce: a run first waits until the unnamed groups stop changing (DEBOUNCE_S between looks, at
most MAX_LOOKS), then names whatever is unnamed then, in one call. The workflow id is
`graph_groups:<user>:<digest of the unnamed member sets>` (a set is named at most once: DBOS hands
back the first workflow for a known id, also a failed one), and the per-user partition runs one at
a time, so the runs a burst of commits enqueued after the first find nothing left: one call.

Commit boundary: step `groups.propose` makes the names (the model call traced as a run of step
`graph_group_naming`, `llm.run_context.traced_step`); step `groups.commit` stores them against
the wiki as it is then (`graph_groups.named_event`: a group takes its exact or inherited name, the
rest keep their fallback names). A failed model call (no model, a bad answer, a budget stop) is
logged; code's names are still stored, the other groups keep their fallback names.

Registered by the decorator at import: import this module before `dbos_app.launch`
(`DurableRuntime.start` does). `install` before launch (recovery needs the deps), `register`
after it (queue + listener), `uninstall` at stop."""

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dbos import DBOS

from evowiki.agent import durable
from evowiki.llm.run_context import traced_step
from evowiki.nodes import member_key
from evowiki.wikiapp.events import DomainsGrouped, WikiEvent
from evowiki.wikiapp.graph_groups import (
    STEP,
    GroupLabel,
    asks,
    name_groups,
    named_event,
    settle,
    to_name,
)
from evowiki.wikiapp.group_domains import group_domains, needs_grouping, tops_now
from evowiki.wikiapp.llm import (
    LLM_BUDGET,
    LLM_FAILURES,
    LLM_UNAVAILABLE,
    CountingGateway,
    LLMUnavailable,
)
from evowiki.wikiapp.repo import Commit, Repo, WikiConflict, listen_graph
from evowiki.wikiapp.state import WikiState

log = logging.getLogger("evowiki.wikiapp.group_naming")

QUEUE = "graph-groups"
DEBOUNCE_S = 3.0
MAX_LOOKS = 10
COMMIT_TRIES = 3


@dataclass(frozen=True)
class NamingDeps:
    repo: Repo
    gateway: Callable[[str], CountingGateway | None]   # the user's metered gateway; None: no LLM
    clock: Callable[[], datetime]
    new_id: Callable[[str], str]
    sleeper: Callable[[float], None] = time.sleep       # outside DBOS (tests): the pause


_deps: NamingDeps | None = None
_queue: Any = None


def install(deps: NamingDeps) -> None:
    global _deps
    _deps = deps


def register() -> None:
    """After the DBOS launch: the queue (a restart's queued runs are dispatched again), then the
    graph listener (from now on a graph change enqueues its naming)."""
    global _queue
    _queue = DBOS.register_queue(QUEUE, partition_concurrency=1)
    listen_graph(request_naming)


def uninstall() -> None:
    global _deps, _queue
    listen_graph(None)
    _deps, _queue = None, None


def installed() -> NamingDeps:
    if _deps is None:
        raise RuntimeError("group_naming.install(deps) was not called: no deps for group naming")
    return _deps


def unnamed_keys(state: WikiState) -> list[str]:
    """What the job has to do: the member keys of the groups with no name that inherit none, and the top groups when they are not the ones
    last grouped into domains, sorted."""
    keys = {member_key(g.members) for g in to_name(state)[0]}
    if needs_grouping(state):
        keys.add("domains:" + member_key(g.id for g in tops_now(state)[0]))
    return sorted(keys)


def group_into_domains(deps: NamingDeps, user_id: str) -> int:
    """The model's domains stored (`DomainsGrouped`), after the names so it sees them. A failure leaves the last grouping and is logged. How many
    domains were stored."""
    state = deps.repo.load(user_id).state
    if not needs_grouping(state):
        return 0
    try:
        gw = deps.gateway(user_id)
        if gw is None:
            raise LLMUnavailable("请切换模型重试")
        with traced_step(STEP, "graph/domains", user_id, input={"tops": [g.id for g in tops_now(state)[0]]}):
            tops, domains = group_domains(gw, state, user_id)
    except (ValueError, LLMUnavailable, *LLM_UNAVAILABLE, *LLM_FAILURES, *LLM_BUDGET) as exc:
        log.warning("domains for %s failed (%s: %s); the last grouping stands", user_id, type(exc).__name__, exc)
        return 0
    for _ in range(COMMIT_TRIES):
        snap = deps.repo.load(user_id)
        if tuple(sorted(g.id for g in tops_now(snap.state)[0])) != tops:
            # The model classified the old top groups. A graph edit already requested
            # another naming pass; never label this newer graph with the old result.
            return 0
        try:
            deps.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=deps.new_id("wev"), ts=deps.clock(), body=DomainsGrouped(tops=tops, domains=domains))]))
            return len(domains)
        except WikiConflict:
            continue
    log.warning("domains for %s: %d commit tries lost to other writes; the last grouping stands", user_id, COMMIT_TRIES)
    return 0


def workflow_id(user_id: str, keys: list[str]) -> str:
    digest = hashlib.sha256("\n".join(keys).encode()).hexdigest()[:24]
    return f"graph_groups:{user_id}:{digest}"


def _enqueue(wid: str, user_id: str) -> None:
    from dbos import SetEnqueueOptions, SetWorkflowID
    queue = _queue
    if queue is None:
        raise RuntimeError("the graph-groups queue is not registered")

    def go() -> None:
        with SetWorkflowID(wid), SetEnqueueOptions(queue_partition_key=user_id):
            queue.enqueue(naming_workflow, user_id)
    # the commit may run inside a page run's step: a workflow cannot be started from there
    durable.outside_dbos(go)


def request_naming(user_id: str, state: WikiState) -> None:
    """The graph listener: enqueue the naming of `state`'s groups that have no name (nothing when
    every group is named or inherits a name). Never raises."""
    try:
        keys = unnamed_keys(state)
        if keys:
            _enqueue(workflow_id(user_id, keys), user_id)
    except Exception:
        log.exception("group naming for %s could not be enqueued; the groups keep their "
                      "fallback names until the graph changes again", user_id)


# ---- the body ---------------------------------------------------------------------------------

def _propose(deps: NamingDeps, user_id: str) -> dict[str, Any]:
    """{"labels": [GroupLabel json]} (none: nothing left to name), plus "failed": why when the
    model call failed (the labels are then code's only)."""
    state = deps.repo.load(user_id).state
    unnamed, groups = to_name(state)
    if not unnamed:
        return {"labels": []}
    mine, rest = settle(state, unnamed)
    out: dict[str, Any] = {"labels": [x.model_dump(mode="json") for x in mine]}
    if not rest:
        return out
    try:
        gw = deps.gateway(user_id)
        if gw is None:
            raise LLMUnavailable("请切换模型重试")
        with traced_step(STEP, "graph/groups", user_id,
                         input={"groups": [sorted(g.members) for g in rest]}):
            labels = name_groups(gw, asks(state, rest, groups, mine), language=state.language,
                                 user_id=user_id)
    except (ValueError, LLMUnavailable, *LLM_UNAVAILABLE, *LLM_FAILURES, *LLM_BUDGET) as exc:
        return {**out, "failed": f"{type(exc).__name__}: {exc}"}
    out["labels"] += [x.model_dump(mode="json") for x in labels]
    return out


def commit_names(deps: NamingDeps, user_id: str, labels: list[GroupLabel]) -> int:
    """Store the names against the wiki as it is NOW (`graph_groups.named_event`); a write landing
    meanwhile makes it load and look again (COMMIT_TRIES, no model call). Returns how many names
    were stored."""
    for _ in range(COMMIT_TRIES):
        snap = deps.repo.load(user_id)
        events = named_event(snap.state, labels, now=deps.clock(), new_id=deps.new_id)
        if not events:
            return 0
        try:
            deps.repo.commit(snap, Commit(wiki=events))
            return len(events[0].body.names)   # type: ignore[union-attr]
        except WikiConflict:
            continue
    log.warning("group naming for %s: %d commit tries lost to other writes; the names are "
                "dropped", user_id, COMMIT_TRIES)
    return 0


def run_naming(user_id: str, deps: NamingDeps) -> int:
    """How many names were stored (0: nothing to name, a failure, or every name dropped)."""
    def look() -> list[str]:
        return unnamed_keys(deps.repo.load(user_id).state)

    seen = durable.step("groups.look", look)
    for _ in range(MAX_LOOKS):                     # debounce: until the groups stop changing
        durable.sleep(DEBOUNCE_S, deps.sleeper)
        now = durable.step("groups.look", look)
        if now == seen:
            break
        seen = now
    if not seen:
        return 0
    proposed = durable.step("groups.propose", lambda: _propose(deps, user_id))
    if "failed" in proposed:
        log.warning("group naming for %s failed (%s); the groups code could not name keep their "
                    "fallback names", user_id, proposed["failed"])
    labels = [GroupLabel.model_validate(x) for x in proposed["labels"]]
    stored = durable.step("groups.commit", lambda: commit_names(deps, user_id, labels)) if labels else 0
    durable.step("groups.domains", lambda: group_into_domains(deps, user_id))
    return stored


@DBOS.workflow(name="evowiki.graph_groups")
def naming_workflow(user_id: str) -> int:
    return run_naming(user_id, installed())
