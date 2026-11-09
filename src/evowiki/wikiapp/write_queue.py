"""Write-all: the page write queue (assistant-actions spec 2026-10-15 §4, Task 7).

A path row is a page the learner is meant to read, so it is written without being asked. Pages are
written one at a time per user, through the page runs' per-user queue (`page_runs`):

- what the user asks for (`WriteQueue.request`: a page opened or asked for, the assistant's actions,
  「重新写」) is queued at the user's priority, ahead of any idle work that has not begun; an idle
  run already writing another page is stopped so the user's starts at once (`page_runs._preempt_idle`;
  the Bot writes that page again later), a write the user waits for is never interrupted;
- when the user has no page run and no assistant turn, the Bot writes the next page nobody wrote
  (`WriteQueue.kick` -> `next_idle`): the next page of every active path first, then the rest of each
  path in the order the learner sees (`path_order`). The page is committed when it is written (no
  hold, no claim); 「下一页」 only opens it (`paths.next_path_page`). Idle writing is not queued
  ahead: the pick is computed from the current paths every time a slot frees (a run finished, a
  path or setting changed, the Bot's tick), so a reordered path is followed at once.

Idle writing stops for: the Bot not serving him (`bot_serves`: paused, or not there) or 「空闲时写页面」 off; a parked / archived goal (not picked);
a trashed page (the learner undid the Bot's write: `no_auto`, only 「重新写」 writes it again); a spending
cap the learner set (a `cap_reached` skip is recorded); three failures of one kind in a row today;
and the learner's `ask` level for new pages (one proposed row 「要写 N 页」, one approval writes the
batch). Every idle write is a `bot_write` ledger row with its cost (`after_write`), undoable: the page
goes back to unwritten (the trash) and is not written again by the Bot.

Off-peak (spec 2026-10-21-offpeak-writes): when the user's page writer has an off-peak rate
(`off_peak_writer`: DeepSeek), idle writing outside the window writes only each path's next page
(the first page new to the reader, `paths.next_item`); the other pages wait for the window (the
Bot's tick kicks again). An approved `ask` batch queues its next pages at once and leaves the rest
to idle writing, which then writes them without asking again. What the user asks for is never held."""

import hashlib
import logging
import threading
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Literal, Protocol

from evowiki.bot.settings import BotSettings, bot_serves
from evowiki.llm.offpeak import off_peak_through
from evowiki.wikiapp import path_order
from evowiki.wikiapp.events import PageMarkedStale, PathRecord, WikiEvent
from evowiki.wikiapp.page_run_store import PageRunRow
from evowiki.wikiapp.page_runs import PageJob, PageRuns, spawn_thread
from evowiki.wikiapp.path_edits import map_paths
from evowiki.wikiapp.paths import ahead_pages, next_item, plan_page
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict
from evowiki.wikiapp.state import WikiState

if TYPE_CHECKING:
    from pydantic import BaseModel

    from evowiki.bot.actions import ActionLedger
    from evowiki.wikiapp.page_flows import PageDeps

log = logging.getLogger("evowiki.wikiapp.write_queue")

# a run that was dropped, not one that failed ("preempted": stopped for the user's own write)
NOT_FAILURES = ("cancelled", "unfinishable", "preempted")
FAILURE_STOP = 3                  # idle failures of one kind in a row: no more idle writing today
# a held idle write starts only when this long ahead is all off-peak (one page write ≈ 16 min)
DEFER_LEAD = timedelta(minutes=20)
WriteState = Literal["queued", "planning", "writing", "done", "failed", "undone", "unwritten"]


@dataclass(frozen=True)
class IdlePick:
    """The page the Bot writes next: row `key` of a path (1-based `index` in its stored order);
    `page_id` None = the row has no wiki page yet (created when the write starts)."""

    path_id: str
    key: str
    index: int
    page_id: str | None
    title: str


def _active_paths(state: WikiState, parked: Collection[str]) -> list[str]:
    inactive = set(parked) | {gid for gid, gs in state.goal_settings.items() if gs.archived}
    for mid in state.maps:
        if mid in inactive:
            inactive.update(map_paths(state, mid))
    return [pid for pid in state.path_order if pid not in inactive]


def idle_candidates(state: WikiState, parked: Collection[str] = (),
                    exclude: Collection[str] = ()) -> list[IdlePick]:
    """The unwritten pages of the active goals in the order the Bot writes them: the first of each
    path (planning order), then the rest of each path in the order the learner sees. Written pages,
    trashed ones (`no_auto`), pages in `exclude`, skipped rows and rows without a page to teach
    (the learner's own title-only rows) are not candidates."""
    per_path: list[list[IdlePick]] = []
    for path_id in _active_paths(state, parked):
        rec = state.paths[path_id]
        picks = []
        for it in path_order.startable(rec):
            i = path_order.stored_index(rec, it.key)
            pid = rec.page_ids[i]
            if pid is not None:
                page = state.pages.get(pid)
                if page is None or page.has_body or pid in state.trashed_pages \
                        or pid in exclude:
                    continue
            picks.append(IdlePick(path_id, it.key, i + 1, pid, it.title))
        per_path.append(picks)
    return [*(p[0] for p in per_path if p), *(x for p in per_path for x in p[1:])]


def next_idle(snap: Snapshot, user_id: str, settings: BotSettings, *,
              parked: Collection[str] = (), exclude: Collection[str] = ()) -> IdlePick | None:
    """The page the idle Bot writes next, None when there is nothing to write or the learner turned
    「空闲时写页面」 off."""
    if snap.user_id != user_id:
        raise ValueError(f"snapshot of {snap.user_id}, not {user_id}")
    if not settings.kinds["idle_write"]:
        return None
    found = idle_candidates(snap.state, parked, exclude)
    return found[0] if found else None


# ---- what kick needs from the Bot's side ----------------------------------------------------------

class IdleEnv(Protocol):
    def settings(self, user_id: str) -> BotSettings: ...
    def last_seen(self, user_id: str) -> datetime | None:
        """When his client was last in front of him (`bot.settings.bot_serves`)."""
        ...
    def parked(self, user_id: str) -> Collection[str]:
        """Path ids of the goals the learner / the Bot paused (B5)."""
        ...
    def cap_hit(self, user_id: str, now: datetime) -> Literal["day", "month"] | None:
        """The spending cap the learner set that is reached (None: no cap, or still under it)."""
        ...
    def turn_busy(self, user_id: str) -> bool:
        """An assistant turn is running for the user."""
        ...
    def note_skip(self, user_id: str, reason: str, detail: str, now: datetime) -> None:
        """Record why idle writing did nothing (once a day per reason)."""
        ...


class WriteQueue:
    """See the module doc. `deps` is read at use (it needs the service). `ledger` None: no
    `bot_write` rows and no `ask` batch (idle writing then needs the default level).
    `off_peak_writer(user_id)`: the user's page writer is billed less off-peak (None: never);
    `off_peak(start, end)`: no peak instant in [start, end) (`llm.offpeak`)."""

    def __init__(self, runs: PageRuns, deps: Callable[[], "PageDeps"], repo: Repo,
                 clock: Callable[[], datetime], new_id: Callable[[str], str], env: IdleEnv, *,
                 ledger: "ActionLedger | None" = None, tz: tzinfo = UTC,
                 spawn: Callable[[Callable[[], None]], None] = spawn_thread,
                 can_write: Callable[[str], bool] | None = None,
                 off_peak_writer: Callable[[str], bool] | None = None,
                 off_peak: Callable[[datetime, datetime], bool] = off_peak_through):
        self._runs, self._deps, self._repo = runs, deps, repo
        self._clock, self._new_id, self._env = clock, new_id, env
        self._ledger, self._tz, self._spawn = ledger, tz, spawn
        self._can_write = can_write      # cheap "is there a model and a writer for this user"
        self._off_peak_writer, self._off_peak = off_peak_writer, off_peak
        self._on_committed: Callable[[str, str], None] | None = None
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def on_committed(self, hook: Callable[[str, str], None]) -> None:
        """`hook(user_id, page_id)` runs when any page run commits its page (a merge's settle)."""
        self._on_committed = hook

    # ---- user requests ----------------------------------------------------------------------

    def request(self, user_id: str, page_ids: Iterable[str], *, origin: str = "user") -> list[str]:
        with self._lock(user_id):          # (a double 重新写 must see the run the first one started)
            return self._request(user_id, page_ids, origin)

    def _request(self, user_id: str, page_ids: Iterable[str], origin: str) -> list[str]:
        """Queues the writes of these pages (in the order given) and returns their run ids. A user's
        request goes ahead of the idle work that has not begun; a page that is queued or writing
        is attached to, a finished-and-unread idle page is already written. A page in the trash is
        written again (and comes back out of it when committed). Unknown page: LookupError."""
        if origin not in ("user", "idle"):
            raise ValueError(f"unknown write origin {origin!r}")
        snap = self._repo.load(user_id)
        deps = self._deps()
        out = []
        for pid in page_ids:
            if pid not in snap.state.pages:
                raise LookupError(f"没有这个页面：{pid}")
            before, path_id = predecessors(snap.state, pid)
            job = PageJob(kind="organize", user_id=user_id, page_id=pid, now=self._clock(),
                          origin="idle" if origin == "idle" else "open",
                          before=before, path_id=path_id)
            # a page in the trash is written again, but a run already queued or writing for it is
            # joined (a double click is one paid run)
            live = next((r for r in self._runs.store.live_of(user_id) if r.page_id == pid), None)
            if pid in snap.state.trashed_pages and live is None:
                run_id, _ = self._runs.start(job, deps, attach=False)
            else:
                run_id, _ = self._runs.start(job, deps, attach=True)
            out.append(run_id)
        return out

    def forget(self, user_id: str, page_id: str) -> None:
        """The Bot's write of this page was taken back: its finished run, unread, is not replayed
        when the page is opened again (it would show a page that is in the trash)."""
        row = self._runs.store.latest_of(user_id, [page_id]).get(page_id)
        if row is not None and row.status == "done" and row.consumed_at is None:
            self._runs.store.consume(row.run_id, self._clock())

    def cancel(self, user_id: str, page_ids: Collection[str]) -> list[str]:
        """Stops the queued / writing runs of these pages (the money already spent stays recorded).
        Returns the run ids discarded."""
        now = self._clock()
        return [r.run_id for r in self._runs.store.live_of(user_id)
                if r.page_id in page_ids and self._runs.discard(r.run_id, now)]

    # ---- idle writing -----------------------------------------------------------------------

    def kick_soon(self, user_id: str) -> None:
        """`kick` off the caller's thread (a request handler, the Bot's tick); never raises."""
        def work() -> None:
            try:
                self.kick(user_id)
            except Exception:
                log.exception("idle write for %s failed", user_id)
        self._spawn(work)

    def _lock(self, user_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(user_id, threading.RLock())

    def kick(self, user_id: str) -> str | None:
        """If the user has no page run and no assistant turn, the Bot writes the next unwritten
        page (origin `idle`); returns its run id, None when nothing was started. Called when a run
        finishes, a path or the setting changes, and on the Bot's tick."""
        with self._lock(user_id):
            return self._kick(user_id)

    def _kick(self, user_id: str) -> str | None:
        now = self._clock()
        settings = self._env.settings(user_id)
        if not settings.kinds["idle_write"] or not bot_serves(settings, self._env.last_seen(user_id), now):
            return None
        if self._can_write is not None and not self._can_write(user_id):
            return None                              # nothing to write with
        if self._runs.store.live_of(user_id) or self._env.turn_busy(user_id):
            return None                              # busy: the user's pages come first
        day = now.astimezone(self._tz).replace(hour=0, minute=0, second=0, microsecond=0)
        recent = [r for r in self._runs.store.finished_since(user_id, day, origin="idle")
                  if r.failure not in NOT_FAILURES]
        if streak(recent) >= FAILURE_STOP:
            log.info("idle writing for %s stopped for today: %d failures in a row", user_id,
                     FAILURE_STOP)
            return None
        cap = self._env.cap_hit(user_id, now)
        if cap is not None:
            self._env.note_skip(user_id, "cap_reached",
                                "今天的花费上限到了，空闲时不写页面" if cap == "day"
                                else "这个月的花费上限到了，空闲时不写页面", now)
            return None
        snap = self._repo.load(user_id)
        # failed today or stopped by the learner: not tried again on the Bot's own
        not_again = ({r.page_id for r in recent if r.status == "failed"}
                     | self._stopped(user_id, snap.state))
        parked = self._env.parked(user_id)
        cands = idle_candidates(snap.state, parked, not_again)   # (`idle_write` checked above)
        if not cands:
            return None
        if self._defers(user_id) and not self._off_peak(now, now + DEFER_LEAD):
            nxt = self._next_pages(user_id, snap.state)              # peak: only the next pages
            cands_now = [c for c in cands if (c.path_id, c.key) in nxt]
        else:
            cands_now = cands
        if self._asks(user_id):
            approved = self._approved(user_id)
            pick = next((c for c in cands_now if f"{c.path_id}/{c.key}" in approved), None)
            if pick is None:
                self._propose_batch(user_id, snap, parked, not_again, skip=approved)
                return None
        elif cands_now:
            pick = cands_now[0]
        else:
            return None
        page_id = pick.page_id or self._create(user_id, pick)
        if page_id is None:
            return None
        return self.request(user_id, [page_id], origin="idle")[0]

    def _stopped(self, user_id: str, state: WikiState) -> set[str]:
        """Unwritten pages whose last run was stopped before it finished (「停止」, the assistant's
        cancel): the Bot does not write them again on its own; asking for the page does. A run
        preempted by the user's own write (`page_runs._preempt_idle`) is not one: the Bot writes
        that page again."""
        unwritten = [pid for pid, rec in state.pages.items() if not rec.has_body]
        latest = self._runs.store.latest_of(user_id, unwritten)
        return {pid for pid, r in latest.items() if r.discarded_at is not None and r.status != "done"
                and r.failure != "preempted"}

    def _asks(self, user_id: str) -> bool:
        return self._ledger is not None and self._ledger.level(user_id, "new_page") == "ask"

    def _defers(self, user_id: str) -> bool:
        return self._off_peak_writer is not None and self._off_peak_writer(user_id)

    def _next_pages(self, user_id: str, state: WikiState) -> set[tuple[str, str]]:
        """(path id, row key) of each path's next page: the first one new to the reader, which
        「下一页」 opens (`paths.next_item`). Never held for the off-peak window."""
        ahead = ahead_pages(self._runs.store, user_id, state)
        return {(path_id, it.key) for path_id, rec in state.paths.items()
                if (it := next_item(rec, ahead)) is not None}

    def _approved(self, user_id: str) -> frozenset[str]:
        """With a deferring writer: the rows of approved batches (`pick_set`), whose pages beyond
        the next ones `write_batch` left to idle writing. Empty otherwise (the batch was queued
        whole)."""
        if self._ledger is None or not self._defers(user_id):
            return frozenset()
        return frozenset(k for r in self._ledger.batches(user_id)
                         if r.status in ("approved", "executed") for k in pick_set(r.content))

    def _create(self, user_id: str, pick: IdlePick) -> str | None:
        """The wiki page of the picked row (its events are the path's `page_started`)."""
        snap = self._repo.load(user_id)
        try:
            planned = plan_page(snap, pick.path_id, pick.key, now=self._clock(), new_id=self._new_id)
            self._repo.commit(snap, Commit(wiki=planned.events))
        except (WikiConflict, LookupError):
            log.info("idle write of %s/%s: the path changed, picked again later", user_id, pick.key)
            return None
        return planned.page_id

    def _propose_batch(self, user_id: str, snap: Snapshot, parked: Collection[str],
                       not_again: Collection[str], skip: Collection[str] = ()) -> None:
        """`ask` level: one proposed 「要写 N 页」 row for everything the Bot would write; idle writing
        waits for it. The same set is proposed once (a rejected row is not asked again). Rows in
        `skip` (approved already) are not asked for."""
        assert self._ledger is not None
        picks = [p for p in idle_candidates(snap.state, parked, not_again)
                 if f"{p.path_id}/{p.key}" not in skip]
        if not picks:
            return
        content = {"picks": [{"path_id": p.path_id, "key": p.key, "title": p.title,
                              "page_id": p.page_id} for p in picks]}
        want = pick_set(content)
        batches = self._ledger.batches(user_id)
        if any(r.status != "proposed" and pick_set(r.content) == want for r in batches):
            return                       # this very set was decided already (rejected, approved)
        summary = f"要生成 {len(picks)} 页"
        pending = [r for r in batches if r.status == "proposed"]
        if pending:                      # one waiting 「要写 N 页」 at most: it follows the paths
            keep, *extra = pending
            if pick_set(keep.content) != want:
                self._ledger.update_pending(user_id, keep.action_id, summary=summary,
                                            content=content)
            for r in extra:              # (a leftover duplicate)
                self._ledger.reject(user_id, r.action_id)
            return
        digest = hashlib.sha1("|".join(sorted(want)).encode()).hexdigest()[:16]
        self._ledger.propose(
            user_id, kind="bot_write", scope="new_page", source="bot",
            summary=summary, why="路径上这些页还没写，空闲时由 Bot 写；批准一次写完这一批",
            content=content, key=f"bot_write_batch:{user_id}:{digest}:{self._new_id('bwb')}")

    def write_batch(self, user_id: str, picks: Iterable[dict[str, Any]]) -> list[str]:
        """Approved `ask` batch: creates the pages that have no wiki page and queues them all as idle
        writes (they go one at a time, behind whatever the user asks for). With a deferring writer
        only each path's next page is queued; the rest is idle writing's (off-peak, `_approved`)."""
        defers = self._defers(user_id)
        nxt = self._next_pages(user_id, self._repo.load(user_id).state) if defers else set()
        page_ids, left = [], False
        for p in picks:
            rec = self._repo.load(user_id).state.paths.get(p.get("path_id"))
            row = None if rec is None else next(
                (i for i in path_order.startable(rec) if i.key == p.get("key")), None)
            if rec is None or row is None:
                continue                              # the row is gone since it was proposed
            if defers and (p.get("path_id"), row.key) not in nxt:
                left = True
                continue
            pid = rec.page_ids[path_order.stored_index(rec, row.key)] or self._create(
                user_id, IdlePick(rec.path.path_id, row.key, 0, None, row.title))
            written = pid is not None and self._repo.load(user_id).state.pages[pid].has_body
            if pid is not None and not written:
                page_ids.append(pid)
        out = self.request(user_id, page_ids, origin="idle")
        if left:
            self.kick_soon(user_id)                   # off-peak now: the rest starts at once
        return out

    # ---- after a run ------------------------------------------------------------------------

    def after_write(self, job: PageJob, run_id: str, status: str, done: "BaseModel | None") -> None:
        """`PageDeps.on_write`: what follows a finished page run, as one durable step. A page the
        Bot wrote is a `bot_write` row (cost, undo); a page a merge asked for lets the merge put
        its originals in the trash."""
        if status != "done" or job.kind != "organize":
            return
        self._mark_if_reordered(job, run_id)
        if self._ledger is not None:
            self._record_write(job, run_id, done)
        if self._on_committed is not None:
            self._on_committed(job.user_id, job.page_id)

    def _mark_if_reordered(self, job: PageJob, run_id: str) -> None:
        """The page was written for the pages that were before it when it was queued; if the path
        changed meanwhile it is marked out of date and rewritten when opened (spec §4). Idempotent
        (a retried step marks once). A run that was discarded meanwhile marks nothing."""
        if job.before is None:
            return
        row = self._runs.store.get(run_id)
        if row is not None and row.discarded_at is not None:
            return
        for _ in range(3):
            snap = self._repo.load(job.user_id)
            state = snap.state
            if (m := state.reorder_marks.get(job.page_id)) is not None and m.job_id == run_id:
                return
            rec = state.pages.get(job.page_id)
            if rec is None or rec.written is None or rec.written.job_id != run_id:
                return                  # not (any more) the text this run wrote
            now_before, now_path = predecessors(state, job.page_id)
            if not predecessors_changed(job.before, job.path_id, now_before, now_path):
                return
            ev = WikiEvent(event_id=self._new_id("wev"), ts=self._clock(),
                           body=PageMarkedStale(page_id=job.page_id, job_id=run_id))
            try:
                self._repo.commit(snap, Commit(wiki=[ev]))
                return
            except WikiConflict:
                continue
        raise WikiConflict(f"could not mark {job.page_id} stale: the wiki kept changing")

    # ---- path edits -------------------------------------------------------------------------

    def path_changed(self, user_id: str, _path_id: str, before: PathRecord, after: PathRecord,
                     _source: str) -> None:
        """`on_path_changed` hook (user, assistant and Bot edits alike). A row taken off the path
        stops its write: a queued run leaves the queue, one writing is cancelled (spent money stays
        recorded); a page another path still holds is kept. Queued page writes that have not begun
        then follow the new order (`_follow_order`). Idle picks are computed live, and a page
        written while the path changed is compared in `after_write`."""
        with self._lock(user_id):
            gone = _shown_pages(before) - _shown_pages(after)
            if gone:
                state = self._repo.load(user_id).state
                held = {p for rec in state.paths.values() for p in _shown_pages(rec)}
                dropped = self.cancel(user_id, gone - held)
                if dropped:
                    log.info("path %s of %s lost rows: %d write run(s) cancelled",
                             after.path.path_id, user_id, len(dropped))
            self._follow_order(user_id, after)

    def _follow_order(self, user_id: str, after: PathRecord) -> None:
        """Queued page runs go in the order the learner now sees the path (the DBOS queue cannot
        be reordered, so a run that has not begun is dropped and queued again behind the ones that
        keep their place). Per priority group (the learner's own writes, then idle ones) only the
        rows from the first one that is out of place are re-queued, so a move that changes nothing
        for the queue touches nothing. Each new run is a fresh request: its `before` is read from
        the new order, its origin stays, and being a later run number does not make
        it the page's "own" write: the ledger asks `has_begun_before`. A follower of a dropped run
        sees a retryable error and attaches to the new run when it asks again.
        Other kinds of run (rewrite, refine, feedback) are left alone."""
        rank = {p: i for i, p in enumerate(path_order.shown_page_ids(after)) if p is not None}
        queued = [r for r in self._runs.store.live_of(user_id)
                  if r.kind == "organize" and r.began_at is None]
        redo: list[PageRunRow] = []
        for idle in (False, True):
            group = [r for r in queued if (r.origin == "idle") == idle]
            slots = [i for i, r in enumerate(group) if r.page_id in rank]
            wanted = list(group)
            for i, r in zip(slots, sorted((group[i] for i in slots),
                                          key=lambda r: rank[r.page_id]), strict=True):
                wanted[i] = r
            first = next((i for i, (a, b) in enumerate(zip(group, wanted, strict=True))
                          if a.run_id != b.run_id), None)
            if first is not None:
                redo.extend(wanted[first:])
        if not redo:
            return
        snap = self._repo.load(user_id)
        deps = self._deps()
        now = self._clock()
        done = []
        for r in redo:
            if not self._runs.discard(r.run_id, now, queued_only=True):
                continue                      # it began (or was dropped) meanwhile: leave it
            done.append(r)
        for r in done:
            old = PageJob.model_validate(r.job)
            fresh, path_id = predecessors(snap.state, r.page_id)
            self._runs.start(old.model_copy(update={"before": fresh, "path_id": path_id}), deps,
                             attach=True)
        if done:
            log.info("path %s of %s reordered: %d queued write(s) re-queued", after.path.path_id,
                     user_id, len(done))

    def _record_write(self, job: PageJob, run_id: str, done: "BaseModel | None") -> None:
        """Every page write is a ledger row of its own: the Bot's idle one is `bot_write`, one written
        for the learner (an assistant action, 看不懂, 我想学这个, a path add, an opened unwritten page,
        重新写) is `user_write`. Cost and calls, and 撤销 (the page back to unwritten)."""
        assert self._ledger is not None
        idle = job.origin == "idle"
        state = self._repo.load(job.user_id).state
        rec = state.pages.get(job.page_id)
        title = rec.title if rec is not None else job.page_id
        where = ""
        for prec in state.paths.values():
            ids = path_order.startable_page_ids(prec)
            if job.page_id in ids:
                where = f"「{prec.path.goal}」的第 {ids.index(job.page_id) + 1} 页"
                break
        content: dict[str, Any] = {"page_id": job.page_id, "title": title, "path_id": job.path_id}
        if idle:
            why = f"空闲时写页面：{where}" if where else "空闲时写页面"
        else:
            # only the page's first run was requested by the action that made it; a later open or
            # 重新写 is the learner's own. A run that was re-queued after a path edit before it began
            # (`path_changed`) has a later number but is still that first write.
            this = self._runs.store.get(run_id)
            first = this is not None and not self._runs.store.has_begun_before(
                job.user_id, job.page_id, this.n)
            asked = (self._ledger.asked_for(job.user_id, job.page_id, self._clock())
                     if first else None)
            content["origin"] = job.origin
            if asked is not None:
                content["asked"] = asked
            why = f"你要的页面：{asked}" if asked is not None else "你要的页面"
        self._ledger.record(
            job.user_id, kind="bot_write" if idle else "user_write", scope="new_page",
            source="bot",
            summary=f"创建了「{title}」页面", why=why, content=content,
            key=f"{'bot_write' if idle else 'user_write'}:{run_id}",
            result={"cost_usd": float(getattr(done, "cost_usd", 0.0) or 0.0),
                    "calls": int(getattr(done, "llm_calls", 0) or 0)},
            undo={"page_id": job.page_id}, page_id=job.page_id, run_id=run_id)

    # ---- the live status --------------------------------------------------------------------

    def runs_since(self, user_id: str, since: datetime) -> list[PageRunRow]:
        """The user's page-writing runs (any origin) that are live or ended at or after `since`,
        oldest first: what 参数 · Bot 最近做了什么 lists."""
        store = self._runs.store
        rows = {r.run_id: r for r in store.live_of(user_id)}
        for origin in ("idle", "open", "background"):
            for r in store.finished_since(user_id, since, origin=origin):
                rows[r.run_id] = r
        return sorted(rows.values(), key=lambda r: (r.began_at or r.started_at, r.n))

    def status(self, user_id: str, page_ids: Collection[str] | None = None, *,
               unwritten: bool = False) -> list[dict[str, Any]]:
        """`GET /wiki/writes`: where each page's write is. `page_ids` None: every page with a run
        still queued or writing. A page without a run is absent, unless `unwritten` asks for the
        asked pages that hold no content yet: those are `unwritten` (a chat card must not take
        「no run」 for 「written」)."""
        store = self._runs.store
        if page_ids is None:
            rows = {r.page_id: r for r in store.live_of(user_id)}
        else:
            rows = store.latest_of(user_id, list(page_ids))
        state = self._repo.load(user_id).state if rows or unwritten else None
        out = [o for r in rows.values()
               if state is not None and (o := write_status(r, state.trashed_pages)) is not None]
        if unwritten and page_ids is not None and state is not None:
            have = {o["page_id"] for o in out}
            out += [{"page_id": p, "run_id": None, "kind": "organize", "state": "unwritten"} for p in page_ids
                    if p not in have and p in state.pages and p not in state.trashed_pages
                    and (state.pages[p].page is None or not state.pages[p].page.texts)]
        return out


def pick_set(content: dict[str, Any]) -> frozenset[str]:
    """The pages a batch proposal covers, whatever order or titles it lists them in."""
    return frozenset(f"{p.get('path_id')}/{p.get('key')}" for p in content.get("picks", []))


def write_status(row: PageRunRow, trashed: Collection[str]) -> dict[str, Any] | None:
    """One page's write as the status API shows it (None: a run that was dropped before it began)."""
    if row.discarded_at is not None and row.status != "done":
        return None
    prog = row.progress or {}
    state: WriteState
    if row.status == "failed":
        state = "failed"
    elif row.status == "done":
        state = "undone" if row.page_id in trashed else "done"
    elif row.began_at is None:
        state = "queued"
    elif row.kind in ("organize", "prefetch") and prog.get("of") is None:     # other kinds do not plan
        state = "planning"
    else:
        state = "writing"
    return {"page_id": row.page_id, "run_id": row.run_id, "kind": row.kind, "state": state, "pass": prog.get("pass"),
            "section": prog.get("section"), "of": prog.get("of"),
            "started_at": row.began_at or row.started_at,
            "cost_usd": row.cost_usd if row.cost_usd is not None else float(prog.get("cost_usd") or 0.0)}


def streak(recent: list[PageRunRow]) -> int:
    """How many of the newest finished runs failed the same way (`recent`: newest first)."""
    n, code = 0, None
    for r in recent:
        if r.status != "failed":
            break
        if n and r.failure != code:
            break
        code = r.failure
        n += 1
    return n


def predecessors(state: WikiState, page_id: str) -> tuple[tuple[str | None, ...] | None, str | None]:
    """The pages before this one on the first path that has it, as the learner sees the path now
    (skipped rows count: 「我会了」 does not change what comes before; removed rows do not show).
    Recorded when a write begins: a page written for a different order is out of date when it
    ends."""
    for path_id, rec in state.paths.items():
        ids = path_order.shown_page_ids(rec)
        if page_id in ids:
            return tuple(ids[:ids.index(page_id)]), path_id
    return None, None


def _shown_pages(rec: PathRecord) -> set[str]:
    """The wiki pages of the rows the learner sees on the path (removed rows are hidden)."""
    return {p for p in path_order.shown_page_ids(rec) if p is not None}


def predecessors_changed(was: tuple[str | None, ...], was_path: str | None,
                         now: tuple[str | None, ...] | None, now_path: str | None) -> bool:
    """Whether the pages before a page differ from those it was queued with. A predecessor that had
    no page then (None) and has one now is the same row, not a change; a different path, a row
    added / removed before it or a different order is."""
    if now is None or was_path != now_path or len(was) != len(now):
        return True
    return any(a is not None and a != b for a, b in zip(was, now, strict=True))


# ---- Postgres env -----------------------------------------------------------------------------

class PgIdleEnv:
    """`IdleEnv` on the service's tables: the Bot's settings, the parked goals (B5), the usage
    caps, the assistant's running turns."""

    def __init__(self, pool: Any, tz: tzinfo, new_id: Callable[[str], str],
                 turn_busy: Callable[[str], bool] = lambda _u: False):
        self._pool, self._tz, self._new_id, self._turn_busy = pool, tz, new_id, turn_busy

    def settings(self, user_id: str) -> BotSettings:
        from evowiki.store import bot as bot_store
        with self._pool.connection() as conn:
            return bot_store.get_settings(conn, user_id)

    def last_seen(self, user_id: str) -> datetime | None:
        from evowiki.store import bot as bot_store
        with self._pool.connection() as conn:
            return bot_store.last_seen_of(conn, user_id)

    def parked(self, user_id: str) -> Collection[str]:
        from evowiki.store import bot_goals
        with self._pool.connection() as conn:
            return set(bot_goals.parked(conn, user_id))

    def cap_hit(self, user_id: str, now: datetime) -> Literal["day", "month"] | None:
        from evowiki.store import usage
        with self._pool.connection() as conn:
            s = usage.get_settings(conn, user_id)
            for period, limit, since in (
                    ("day", s.daily_limit_usd, usage.day_start(now, self._tz)),
                    ("month", s.monthly_limit_usd, usage.month_start(now, self._tz))):
                if limit is not None and usage.total_cost(conn, user_id, since) >= limit:
                    return period  # type: ignore[return-value]
        return None

    def turn_busy(self, user_id: str) -> bool:
        return self._turn_busy(user_id)

    def note_skip(self, user_id: str, reason: str, detail: str, now: datetime) -> None:
        """A skipped idle write as a gated Bot trigger (「显示没做的」), once per day and reason."""
        from evowiki.store import bot as bot_store
        day = now.astimezone(self._tz).date().isoformat()
        row = bot_store.TriggerRow(
            trigger_id=self._new_id("trg"), user_id=user_id, kind="idle_write",
            rule_id="idle_write", rule_version=1, source_key=f"idle_write:{reason}:{day}",
            dedup_key=f"idle_write:{reason}", route="direct", evidence=[], payload={},
            priority=0, reason="空闲时写页面", created_at=now, status="gated",
            gate_outcome="skip", gate_reason=reason, gated_at=now, title="空闲时写页面",
            gate_detail=detail)
        with self._pool.connection() as conn:
            bot_store.insert_trigger(conn, row)
