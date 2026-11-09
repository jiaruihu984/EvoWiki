"""/wiki/rsi/*: the per-user RSI cycle (evowiki.teach.cycle) over Postgres, and its background tick.

The cycle reads only what the wiki app observes (`wikiapp.rsi_data.user_data`: the user's aspect
log and wiki state). Track A fits θ_infer,u statistically (`teach.fit`, no LLM call); the harness
round (below) uses the user's metered gateway.

`rsi_tick` runs after the routes that record a signal (check answer, review grade, section
feedback). It first decides `due` cheaply (settings, last cycle time, a count of Observe events
since — no snapshot load or replay); only a due tick builds UserData and calls `maybe_run`. It never
raises: a failed tick is logged and the user's request is unaffected.

One cycle at a time per user: run / tick / undo hold a per-user session-level advisory lock
(`cycle_lock`) on a dedicated pooled connection, outside any transaction, so no LLM call ever
holds a write transaction open. A tick that finds the lock taken skips (the holder's cycle covers
it); a manual run or an undo answers 409. `maybe_run` re-reads `due` after the lock is taken.

Pages marked out of date (`wikiapp.stale`: a spec change's `PagesStaleSet`, a harness change's or
a path order's `PageMarkedStale`) are never rewritten when marked. When the learner next opens a
marked page (POST /wiki/pages/{id}/open?rewrite=1; any other read or open starts nothing), it is
served as it is and one rewrite (a page workflow) under the current spec starts (`stale_rewrite`);
only opened pages cost money. A failed rewrite is recorded and retried at most once per day, never
after two failures under the same spec (`wikiapp.stale.rewrite_due`). Undo covers accepted
track-A patches, applied layer-2 method changes (`RsiCycle.undo_method`) and the rows of the
external change sources (`svc.change_sources`, `teach.change_source`, dispatched by id prefix
first); the change list shows all of them.

With the action ledger wired (`svc.actions`), the cycle asks the user's Bot level first
(`teach.approval`, `bot.rsi_approvals.LedgerApprovals`): a change that passed its Gate while
teaching_params / rsi is `ask` waits in /wiki/bot/actions; the ledger's approve / reject reach it
through `rsi_decider` (approve under the user's cycle lock).

The harness loop (`harness.round`, design 2026-10-21 §5) is the one improvement round (RSI-B and
B2 retired on 2026-10-29): with `RsiSettings.harness` on the cycle runs one harness round
(`_harness`; without a deep tier or gateway the round is recorded skipped, never anything else);
the tick is then due by the signals since the last harness round. A page a harness change marked is
rewritten with refine trigger "harness" (run kind harness_rewrite). Its changes waiting for the
user are `rsi_method` ledger rows (content change_id "h_…") decided through the same
`rsi_decider`.

Sections page feedback left waiting (it named a missing concept; an answer never leaves any) are written
by one scoped refine (trigger "patch", run kind patch_rewrite, `patch_write`) on an explicit open, when
nothing else is due."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING

from evowiki.agent import lifecycle
from evowiki.assistant.pg_store import PgSessionStore
from evowiki.bot.rsi_approvals import BOT_TRIGGER, LedgerApprovals
from evowiki.deep.contracts import RefineScope
from evowiki.memory.forget import generation_guard
from evowiki.service.deps import Services
from evowiki.service.refine_runs import start_refine, start_refine_run
from evowiki.store import harness_rsi
from evowiki.store.aspects import observe_count_since
from evowiki.store.db import fetch_all, fetch_one
from evowiki.store.rsi import PgRsiStore
from evowiki.store.rsi_layer2 import PgLayer2Store
from evowiki.teach.contracts import ChangeOut, RsiSettings
from evowiki.teach.cycle import RsiCycle, UserData, due, within_week
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from evowiki.wikiapp.refine_service import DeepService
from evowiki.wikiapp.repo import PgRepo, WikiConflict
from evowiki.wikiapp.rsi_data import user_data
from evowiki.wikiapp.stale import patch_due, patch_pending, reorder_due, rewrite_due
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.teachspec import effective_version

STALE_EVENT = "pages_stale_set"
REORDER_EVENT = "page_marked_stale"
HARNESS_MARK = "harness"          # PageMarkedStale reason of a harness change (harness.round)

if TYPE_CHECKING:
    from evowiki.harness.forging import ForgeHooks
    from evowiki.harness.patching import PatchHooks
    from evowiki.harness.round import HarnessRounds

log = logging.getLogger("evowiki.service.rsi")

BUSY_DETAIL = "改进正在进行中"


class NotUndoable(RuntimeError):
    """The patch exists but cannot be undone (only accepted patches can): 409."""


class CycleBusy(RuntimeError):
    """Another RSI cycle (or undo) for this user is running: 409."""

    def __init__(self, message: str = BUSY_DETAIL):
        super().__init__(message)
        self.error_code = "rsi_busy"


def _lock_key(uid: str) -> str:
    return f"evowiki.rsi:{uid}"


@contextmanager
def cycle_lock(svc: Services, uid: str) -> Iterator[bool]:
    """Try the user's RSI lock: yields True when this caller holds it (released on exit), False
    when someone else does. Session-level on its own connection, committed right away, so no
    transaction stays open while the cycle (and its LLM call) runs on other connections."""
    with svc.pool.connection() as conn:
        row = fetch_one(conn, "select pg_try_advisory_lock(hashtextextended(%s, 0)) as ok",
                        (_lock_key(uid),))
        conn.commit()
        got = bool(row and row["ok"])
        try:
            yield got
        finally:
            if got:
                conn.execute("select pg_advisory_unlock(hashtextextended(%s, 0))",
                             (_lock_key(uid),))
                conn.commit()


@contextmanager
def _exclusive(svc: Services, uid: str) -> Iterator[None]:
    with cycle_lock(svc, uid) as got:
        if not got:
            raise CycleBusy()
        yield


def _data(svc: Services, uid: str) -> UserData:
    repo = PgRepo(svc.pool)
    snap = repo.load(uid)                        # LookupError for an unknown user
    return user_data(snap.state, repo.aspect_events(uid))


def _approvals(svc: Services) -> LedgerApprovals | None:
    return None if svc.actions is None else LedgerApprovals(svc.actions, svc.pool)


@contextmanager
def rsi_decider(svc: Services, uid: str, lock: bool) -> Iterator[RsiCycle]:
    """The user's cycle for the ledger's `RsiExecutor`. `lock`: under the user's cycle lock
    (CycleBusy when a cycle runs)."""
    if not lock:
        yield _cycle(svc, uid)
        return
    with _exclusive(svc, uid):
        yield _cycle(svc, uid)


def _cycle(svc: Services, uid: str) -> RsiCycle:
    """The user's cycle over Postgres: track A, A2 (`PgLayer2Store`), the change sources and the
    harness loop. `uid` is unused (kept so every caller names the user it builds the cycle for)."""
    return RsiCycle(PgRsiStore(svc.pool), ASPECT_CONFIG_V1, new_id=svc.new_id,
                    layer2=PgLayer2Store(svc.pool), sources=svc.change_sources,
                    approvals=_approvals(svc), harness=_harness(svc))


def _harness(svc: Services) -> "HarnessRounds":
    """The harness loop's track over the service (`harness.round.RoundDeps`). Without a deep tier
    or a gateway every round is recorded `skipped` (NO_DEEP); the cycle never runs anything else
    while the setting is on. The judge's providers are built only when a round runs; signals are
    the wiki's (`wikiapp.spec_store.RepoSignals`), counted for `due` like the cycle's
    (`observe_count_since`). The
    assistant's session store and action ledger (`svc.actions`, None without one) give the
    round its `assistant_rejected` signals and turn cases (harness 3b follow-up 3)."""
    from evowiki.harness.anchors import JUDGE_CALIBRATED
    from evowiki.harness.evalset_redlines import (
        harness_evals,
        harness_patch_regression,
        harness_redlines,
        harness_regression,
        harness_relevant,
    )
    from evowiki.harness.evidence import evaluate
    from evowiki.harness.judge import judge_configs, judge_providers
    from evowiki.harness.replay_assistant import pg_deps
    from evowiki.harness.round import HarnessRounds, RoundDeps
    from evowiki.rules.guard import harness_guard
    from evowiki.runtime.usage import runtime_improver_ref
    from evowiki.service.harness_defaults import defaults
    from evowiki.store.trace import PgTraceSink
    from evowiki.store.usage import usage_tz
    from evowiki.wikiapp.spec_store import RepoSignals, RepoSpecRegistry

    repo = PgRepo(svc.pool)
    sessions = PgSessionStore(svc.pool)
    signals = RepoSignals(repo, sessions)

    def selected_signals(uid: str, since: datetime | None):
        from evowiki.service.rsi_feedback import signals as page_corrections
        rows = signals.signals(uid, since=since)
        eligible = {s.signal_id for s in page_corrections(svc, uid)}
        return [s for s in rows if s.kind not in ("page_feedback", "user_complaint") or s.signal_id in eligible]

    def count(uid: str, since: datetime | None) -> int:
        with svc.pool.connection() as conn:
            return observe_count_since(conn, uid, since)
    store = PgRsiStore(svc.pool)
    trace = PgTraceSink(svc.pool, svc.clock)
    return HarnessRounds(RoundDeps(
        pool=svc.pool, repo=repo, deep=svc.deep, gateway=svc.gateway,
        registry=RepoSpecRegistry(repo, svc.new_id), settings=store.settings,
        signals=selected_signals, signals_since=count,
        defaults=defaults(), judges=judge_configs(), judge_providers=judge_providers,
        tz=usage_tz(), new_id=svc.new_id, approvals=_approvals(svc), memory=store.memory,
        trace=trace,
        library=getattr(svc.deep, "library", None),
        render=getattr(svc.deep, "checker", None),
        patch=_patch_hooks(svc), forge=_forge_hooks(svc),
        sessions=sessions, ledger=svc.actions, guard=harness_guard(repo, svc.gateway),
        assistant_replay=lambda gw: pg_deps(svc.pool, gw, trace=trace),
        evidence=evaluate, propose_only=not JUDGE_CALIBRATED, redlines=harness_redlines(),
        regression=harness_regression(), evals=harness_evals(), relevant=harness_relevant(), patch_regression=harness_patch_regression(),
        improver=runtime_improver_ref(),
        memory_items=lambda uid: _memory_items(svc, uid)))


def _memory_items(svc: Services, uid: str) -> list[str]:
    """His rules, profile lines and decisions in force (`memory.conflicts.library`), by their text."""
    conflicts = getattr(svc, "conflicts", None)
    return [] if conflicts is None else [e.text for e in conflicts.library(uid)]


def _forge_hooks(svc: Services) -> "ForgeHooks | None":
    """The harness round's forged-tool hooks (`harness.forging.build_hooks` over the deep service's
    forge runtime): None without a deep tier or a forge runtime, and on any error building them
    (logged); then the improver is never offered `forge_tool` (fail closed). Whether a round may
    forge is still the forge's tool switch."""
    deep = svc.deep
    if deep is None or getattr(deep, "llm", None) is None or getattr(deep, "forge", None) is None:
        return None
    from evowiki.harness.forging import build_hooks
    try:
        return build_hooks(deep.forge)
    except Exception:  # broad on purpose: no forged tools rather than no harness round
        log.warning("harness forged tools unavailable", exc_info=True)
        return None


def _patch_hooks(svc: Services) -> "PatchHooks | None":
    """The harness round's code-patch hooks (`harness.patching.build_hooks`): None without a deep
    tier, an L2 sandbox or the forge's test database, and on any error building them (logged);
    then the improver is never offered `propose_patch` (fail closed). Whether a round may use
    them is still the forge's patch switch (off by default)."""
    if svc.deep is None or getattr(svc.deep, "llm", None) is None:
        return None
    from evowiki.harness.patching import build_hooks
    from evowiki.store.trace import PgTraceSink
    try:
        return build_hooks(svc.pool, PgTraceSink(svc.pool, svc.clock))
    except Exception:  # broad on purpose: no patches rather than no harness round
        log.warning("harness code patches unavailable", exc_info=True)
        return None


def stale_rewrite(svc: Services, uid: str, page_id: str) -> bool:
    """POST /wiki/pages/{id}/open?rewrite=1: when the page is still out of date after an RSI change and a
    rewrite is due (`wikiapp.stale.rewrite_due`: not stopped after two failures under the current
    spec, a single failure at least a day old), start one rewrite under the current spec (refine
    trigger "rsi") as a page workflow (`page_flows`, kind `rsi_rewrite`) and return True. Else,
    when sections a K-change patch left are waiting and their write is due, start that scoped write
    (`patch_write`; last: a whole-page rewrite writes those sections anyway). False when nothing is
    due, there is no deep service, or a rewrite of the page is already queued or running (one at a
    time per page). A rewrite that fails (ends `failed` or raises) is logged and recorded on the
    page's mark (`PageRewriteFailed`, by the workflow); the page stays marked. Never raises."""
    deep = svc.deep
    if deep is None:
        return False
    try:
        repo = PgRepo(svc.pool)
        # cheap first: a user never marked (most opens) skips the full snapshot load here; the
        # patch check below does its own
        if repo.wiki_events_of(uid, (STALE_EVENT, REORDER_EVENT)):
            state = repo.load(uid).state
            if rewrite_due(state, page_id, svc.clock()):
                _start_rewrite(svc, deep, uid, page_id, state)
                return True
    except WikiConflict:                         # already running (or not organized): nothing new
        log.debug("rsi rewrite of %s/%s not started: one is running", uid, page_id)
        return False
    except Exception:  # broad on purpose: opening a page never fails because of this
        log.warning("rsi rewrite of %s/%s not started", uid, page_id, exc_info=True)
        return False
    return patch_write(svc, uid, page_id)


def _start_rewrite(svc: Services, deep: DeepService, uid: str, page_id: str,
                   state: WikiState) -> None:
    """The due whole-page rewrite: harness / reorder mark first (their own trigger and run kind),
    else the RSI one."""
    version = effective_version(state)
    if reorder_due(state, page_id, svc.clock()):      # (its own trigger and run kind)
        if state.reorder_marks[page_id].reason == HARNESS_MARK:     # a harness RSI change
            job = start_refine(deep, uid, page_id, "harness")
            start_refine_run(svc, uid, page_id, job, kind="harness_rewrite", spec_version=version)
            return
        job = start_refine(deep, uid, page_id, "reorder")
        start_refine_run(svc, uid, page_id, job, kind="reorder_rewrite", spec_version=version)
        return
    job = start_refine(deep, uid, page_id, "rsi")
    start_refine_run(svc, uid, page_id, job, kind="rsi_rewrite", spec_version=version)


def patch_write(svc: Services, uid: str, page_id: str | None = None) -> bool:
    """The page writer writes the sections a K-change patch left waiting (stage 4a: the patch writes
    no text; `wikiapp.stale.patch_pending`): when that write is due (`patch_due`: bounded retries
    on the same waiting set), one scoped refine (trigger "patch", `RefineScope(sections=pending)`)
    starts as a page workflow (kind `patch_rewrite`) and True is returned. `page_id` None: the
    user's current page. Called on an explicit open (`stale_rewrite`); fire and forget: False when nothing is due, there is no deep service, or a write of the page is
    already running. Never raises (failures are logged; the sections stay waiting)."""
    deep = svc.deep
    if deep is None:
        return False
    try:
        state = PgRepo(svc.pool).load(uid).state
        pid = page_id if page_id is not None else state.current_page
        if pid is None or not patch_due(state, pid, svc.clock()):
            return False
        scope = RefineScope(page_id=pid, sections=patch_pending(state, pid))
        job = start_refine(deep, uid, pid, "patch")
        start_refine_run(svc, uid, pid, job, kind="patch_rewrite",
                         spec_version=effective_version(state), scope=scope)
        return True
    except WikiConflict:                         # already running (or not organized): nothing new
        log.debug("patch write of %s/%s not started: one is running", uid, page_id)
        return False
    except Exception:  # broad on purpose: the request that patched the page never fails here
        log.warning("patch write of %s/%s not started", uid, page_id, exc_info=True)
        return False


def _last_run(svc: Services, uid: str, settings: RsiSettings) -> datetime | None:
    """When the user's last cycle ran: the last harness round with the harness on (as
    `RsiCycle.last_run_at`), else the last cycle record."""
    if settings.harness:
        with svc.pool.connection() as conn:
            r = harness_rsi.last_round(conn, uid)
        return None if r is None else r.started_at
    return PgRsiStore(svc.pool).last_cycle_at(uid)


def _due_now(svc: Services, uid: str) -> bool:
    """Cheap pre-check for the tick: settings, the last cycle's time and a count of aspect Observe
    events since then (`observe_count_since`, the same count as `UserData.signals_since`; with the
    harness on, plus the assistant feedback its round counts) — no snapshot load, no log replay. `maybe_run` re-checks `due` on the full data under the lock."""
    store = PgRsiStore(svc.pool)
    settings = store.settings(uid)
    if settings.cycle == "manual":
        return False
    with svc.pool.connection() as conn:
        if settings.harness:
            r = harness_rsi.last_round(conn, uid)          # as `RsiCycle.last_run_at`
            last = None if r is None else r.started_at
        else:
            last = store.last_cycle_at(uid)
        n = observe_count_since(conn, uid, last)
    if settings.harness and due(settings, last, n, svc.clock()) is None:
        # the harness round also counts assistant rejections and 👎 (`harness.round._due_count`)
        from evowiki.harness.round import assistant_feedback_count
        n += assistant_feedback_count(PgSessionStore(svc.pool), svc.actions, uid, last)
    return due(settings, last, n, svc.clock()) is not None


def _corrected(svc: Services, uid: str) -> bool:
    """Review assistant corrections and opted-in page signals with their source IDs."""
    settings = PgRsiStore(svc.pool).settings(uid)
    if settings.cycle == "manual" or not settings.harness:
        return False
    import hashlib
    import json

    from evowiki.harness.round import worth_review
    from evowiki.service.rsi_feedback import pending_ids, remember_review, reviewed, signals
    from evowiki.wikiapp.signals import assistant_feedback
    with svc.pool.connection() as conn:
        r = harness_rsi.last_consuming_round(conn, uid)
    with generation_guard(svc.pool, uid):
        pending = pending_ids(svc, uid)
        since = None if r is None else r.started_at
        pages = [s for s in signals(svc, uid) if since is None or s.at >= since]
        sessions = PgSessionStore(svc.pool)
        sources = [*pages, *(s for s, _ in assistant_feedback(sessions, svc.actions, uid, since))]
        payload = json.dumps([s.model_dump(mode="json") for s in sorted(sources, key=lambda s: s.signal_id)],
                             sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        cached = reviewed(svc, uid, fingerprint) if pages else None
        if cached is not None:
            return cached and bool(pending)
        answer = worth_review(sessions, svc.actions, svc.gateway, uid,
                              since, page_signals=pages, strict=True)
        if pages:
            remember_review(svc, uid, [s.signal_id for s in pages], fingerprint, answer)
        return answer


def bot_rsi_runner(svc: Services) -> Callable[[str, str], dict]:
    """The Bot's RSI runner (B5c, `bot.pipeline.RsiRunner`): one full cycle for a strategy_negative
    trigger, assembled exactly like `rsi_tick` (`_cycle`, LedgerApprovals). The user's manual RSI
    setting is respected (`manual`, nothing runs), and so is the weekly cap (`weekly`: a cycle
    already ran this week, `not_due`; the Bot closes the trigger, the next week's cycle reads its
    signals); a cycle already running is `busy` (the Bot defers and retries). Inside, `BOT_TRIGGER` ties a change waiting for the user to the trigger.
    The cycle record has trigger `bot` and `result.bot_trigger_id`."""
    def run(uid: str, trigger_id: str) -> dict:
        settings = PgRsiStore(svc.pool).settings(uid)
        if settings.cycle == "manual":
            return {"status": "manual"}
        if settings.cycle == "weekly" and within_week(_last_run(svc, uid, settings), svc.clock()):
            return {"status": "not_due"}
        with cycle_lock(svc, uid) as got:
            if not got:
                return {"status": "busy"}
            token = BOT_TRIGGER.set(trigger_id)
            try:
                res = _cycle(svc, uid).run(
                    uid, _data(svc, uid), trigger="bot", now=svc.clock(),
                    context={"bot_trigger_id": trigger_id})
            finally:
                BOT_TRIGGER.reset(token)
        return {"status": "ran", "a": res.a, "b": res.b, "patch_ids": list(res.patch_ids),
                "cost_usd": res.cost_usd}
    return run


def rsi_tick(svc: Services, uid: str) -> None:
    """Recheck eligibility under the cycle lock and acknowledge captured page signals.
    Busy, disabled and failed deliveries stay pending for startup/minute recovery. A completed
    negative triage is acknowledged so duplicate wakeups do not repeat its paid call."""
    try:
        if lifecycle.drained():
            return
        with cycle_lock(svc, uid) as got:
            if not got:
                log.debug("rsi tick skipped for %s: a cycle is already running", uid)
                return
            from evowiki.service.rsi_feedback import acknowledge, pending_ids, reviewed_pending_ids
            pending = pending_ids(svc, uid)
            settings = PgRsiStore(svc.pool).settings(uid)
            by_count = _due_now(svc, uid)
            # Disabled/manual feedback remains durable and can be delivered after settings change.
            if settings.cycle == "manual" and not by_count:
                return
            if settings.cycle == "weekly" and within_week(_last_run(svc, uid, settings), svc.clock()):
                return
            if lifecycle.drained():
                return
            if not by_count:
                if not settings.harness or svc.gateway is None:
                    return
                if not _corrected(svc, uid):
                    acknowledge(svc, uid, pending)
                    return
                pending = list(set(pending) | set(reviewed_pending_ids(svc, uid)))
            if lifecycle.drained():
                return
            now = svc.clock()
            with svc.pool.connection() as conn:
                previous = [r['round_id'] for r in fetch_all(conn,
                    "select round_id from harness_round where user_id=%s and started_at=%s", (uid, now))]
            if by_count:
                _cycle(svc, uid).maybe_run(uid, _data(svc, uid), now=now)
            else:
                log.info("rsi tick: %s corrected the same kind of thing enough times for a round", uid)
                _cycle(svc, uid).run(uid, _data(svc, uid), trigger="correction", now=now)
            with svc.pool.connection() as conn:
                rounds = fetch_all(conn, "select status from harness_round where user_id=%s "
                    "and started_at=%s and not (round_id=any(%s))", (uid, now, previous))
            # A thin round completed this review, but last_consuming_round retains its evidence.
            if rounds and all(r['status'] in (*harness_rsi.CONSUMING, 'thin') for r in rounds):
                acknowledge(svc, uid, pending)
    except Exception:  # broad on purpose: a background tick must never break the user's request
        log.exception("rsi tick failed for %s", uid)


def undo_rsi_change(svc: Services, uid: str, patch_id: str) -> ChangeOut:
    """Undo one RSI change under the user's cycle lock (POST /wiki/rsi/patches/{id}/undo, and the
    chat's undo of an RSI ledger row, `bot.executors.RsiExecutor.undo`): an id with a registered
    source prefix goes to that source; else a track-A patch; else a layer-2 method change (its
    registry rolls back to from_version). LookupError unknown id (or another user's), NotUndoable
    not undoable, CycleBusy a cycle running (only for what a round changes: `rsi_related`)."""
    from evowiki.teach.change_source import source_for
    src = source_for(svc.change_sources, patch_id)

    def by_source() -> ChangeOut:
        try:
            return src.undo(uid, patch_id, now=svc.clock())
        except ValueError as e:
            raise NotUndoable(str(e)) from e

    if src is not None and not getattr(src, "rsi_related", True):
        return by_source()
    with _exclusive(svc, uid):
        if src is not None:
            return by_source()
        store = PgRsiStore(svc.pool)
        rec = next((r for r in store.patches(uid) if r.patch.patch_id == patch_id), None)
        if rec is None:
            return _undo_method(svc, uid, patch_id)
        if rec.patch.status != "personal_accepted":
            raise NotUndoable(f"patch {patch_id} is {rec.patch.track}/{rec.patch.status}; "
                              "only accepted patches can be undone")
        data = _data(svc, uid)
        cycle = _cycle(svc, uid)
        try:
            cycle.undo(uid, patch_id, now=svc.clock())
        except ValueError as e:
            # e.g. a newer fit changed the same values, or a record of the retired track B
            raise NotUndoable(str(e)) from e
        out = next((c for c in cycle.changes(uid, data, now=svc.clock())
                    if c.patch_id == patch_id), None)
    if out is None:
        raise LookupError(f"patch {patch_id} missing from the change list")
    return out


def ledger_undo(svc: Services, uid: str, patch_id: str) -> ChangeOut:
    """`undo_rsi_change` for `bot.executors.RsiExecutor.undo` (a chat undo of an RSI ledger row):
    NotUndoable becomes the executor contract's ValueError (refused, with the reason)."""
    try:
        return undo_rsi_change(svc, uid, patch_id)
    except NotUndoable as e:
        raise ValueError(str(e)) from e


def _undo_method(svc: Services, uid: str, change_id: str) -> ChangeOut:
    """The caller holds the user's cycle lock. LookupError (404) when no such method change."""
    cycle = _cycle(svc, uid)
    try:
        cycle.undo_method(uid, change_id, now=svc.clock())
    except ValueError as e:
        raise NotUndoable(str(e)) from e
    out = next((c for c in cycle.changes(uid, _data(svc, uid), now=svc.clock())
               if c.patch_id == change_id), None)
    if out is None:
        raise LookupError(f"method change {change_id} missing from the change list")
    return out
