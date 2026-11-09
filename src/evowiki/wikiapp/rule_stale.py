"""The pages an automatic rule change left out of date (the retired RSI-B's `mark_stale`, made
generic for the harness loop's H7 changes; `wikiapp.stale` has the lazy rewrite that serves them).

`mark_stale` compares every RSI spec save (`rsi` / `rsi_undo` versions, RSI_SPEC_ACTIONS) not
compared yet (`WikiState.stale_checked_version`) with the pages it affects and commits one
`PagesStaleSet`:
- the change's ops come from `ops_of(user_id, origin)` (the harness: the change row of round
  "harness:<round id>"; None = not a change this module follows: counted compared, nothing marked);
- the pages it affects (`population`): every written page whose sections were written with an
  edited or removed item in force, and every written page for an added item or a param;
- a page written under an older version that differs from the new one in what an op changes
  (`stale.touches_differently`) is marked (one mark per page, the latest change; its `patch_id` is
  the change's origin);
- a marked page whose rules the change restored to what they were when it was written (an undo,
  `stale.same_rules`) is cleared; one still behind is kept marked, credited to the newest earlier
  RSI change it is still behind on.
Versions saved before STALE_SINCE count as compared but mark nothing (no surprise rewrites of pages
from changes made before this shipped). Idempotent; WikiConflict after STALE_TRIES conflicting
commits. Nothing here undoes a change: callers run it after the change committed and log a
failure."""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from evowiki.teach.contracts import ParamPatchOp, PatchBody, SpecPatchOp, TeachingSpec
from evowiki.wikiapp.events import PagesStaleSet, StaleMark, WikiEvent
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict
from evowiki.wikiapp.stale import page_version, same_rules, touches_differently
from evowiki.wikiapp.teach import read_version

log = logging.getLogger("evowiki.wikiapp.rule_stale")

STALE_TRIES = 3                   # commit attempts of one marking pass
RSI_SPEC_ACTIONS = ("rsi", "rsi_undo")
# When rule-wide marking of harness changes shipped (RSI-B retired, 2026-10-29): rule changes saved
# before it count as compared but never mark a page. Read at call time, so tests can move it.
STALE_SINCE = datetime(2026, 10, 29, tzinfo=UTC)

# (user id, the spec version's origin) -> the change's spec ops, None when it is not one to follow
OpsOf = Callable[[str, str], Sequence[PatchBody] | None]


def population(snap: Snapshot, ops: Sequence[PatchBody]) -> list[str]:
    """The written pages (page order) the ops can affect: those whose section logs name an item an
    op edits or removes; every written page when an op adds an item or sets a param."""
    state = snap.state
    items = {o.item_id for o in ops
             if isinstance(o, SpecPatchOp) and o.op != "add_item" and o.item_id}
    every = any(isinstance(o, ParamPatchOp) or o.op == "add_item" for o in ops)
    out = []
    for p in state.page_order:
        logs = state.section_logs.get(p)
        rec = state.pages.get(p)
        if not logs or rec is None or rec.page is None:
            continue
        if every or any(i in g.spec_items for g in logs.values() for i in items):
            out.append(p)
    return out


def _differs(old: TeachingSpec, new: TeachingSpec, ops: Sequence[PatchBody]) -> bool:
    return any(touches_differently(old, new, o) for o in ops)


def stale_event(repo: Repo, snap: Snapshot, ops_of: OpsOf,
                since: datetime | None = None) -> PagesStaleSet | None:
    """The `PagesStaleSet` of the RSI spec saves not compared yet (module doc), None when there are
    none."""
    state = snap.state
    saves = [m for m in state.spec_versions
             if m.action in RSI_SPEC_ACTIONS and m.version > state.stale_checked_version]
    if not saves:
        return None
    cutoff = since or STALE_SINCE
    specs: dict[int, TeachingSpec | None] = {}

    def spec(v: int) -> TeachingSpec | None:
        if v not in specs:
            try:
                specs[v] = read_version(repo, snap, v)
            except LookupError:
                specs[v] = None
        return specs[v]

    ops_cache: dict[str, Sequence[PatchBody] | None] = {}

    def ops(origin: str) -> Sequence[PatchBody] | None:
        if origin not in ops_cache:
            ops_cache[origin] = ops_of(snap.user_id, origin)
        return ops_cache[origin]

    marks = dict(state.stale_marks)
    marked: dict[str, StaleMark] = {}
    cleared: set[str] = set()
    for m in saves:
        if m.at < cutoff:                          # before this shipped: compared only
            continue
        origin = m.origin or ""
        change = ops(origin) if origin else None
        if not change:
            continue
        new = spec(m.version)
        if new is None:
            log.warning("spec v%d of %s unreadable; no pages marked", m.version, snap.user_id)
            continue
        for pid in population(snap, change):
            pv = page_version(state, pid)
            if pv >= m.version:
                continue
            old = spec(pv)
            if old is None:                        # fail toward not spending: logged, unmarked
                log.warning("page %s/%s: spec v%d unreadable; not marked", snap.user_id, pid, pv)
                continue
            mark: StaleMark | None = None
            if _differs(old, new, change):
                mark = StaleMark(patch_id=origin, spec_version=m.version)
            elif pid in marks:
                if same_rules(old, new):           # back to what the page was written under
                    del marks[pid]
                    marked.pop(pid, None)
                    cleared.add(pid)
                elif marks[pid].patch_id == origin:
                    # this change was the mark's, but the page is still behind on another one
                    mark = StaleMark(patch_id=_behind_on(snap, ops, old, new, pv, m.version,
                                                         origin), spec_version=m.version)
            if mark is not None:
                marks[pid] = marked[pid] = mark
                cleared.discard(pid)
    return PagesStaleSet(marked=marked, cleared=tuple(sorted(cleared)),
                         checked_version=max(m.version for m in saves))


def _behind_on(snap: Snapshot, ops: Callable[[str], Sequence[PatchBody] | None],
               old: TeachingSpec, new: TeachingSpec, page_v: int, before: int,
               fallback: str) -> str:
    """The origin of the newest RSI change applied after the page was written (version in
    (page_v, before)) whose change the page still lacks; `fallback` when none is found."""
    for meta in reversed(snap.state.spec_versions):
        if meta.action != "rsi" or not page_v < meta.version < before or not meta.origin:
            continue
        change = ops(meta.origin)
        if change and _differs(old, new, change):
            return meta.origin
    return fallback


def mark_stale(repo: Repo, new_id: Ids, user_id: str, *, ops_of: OpsOf, now: datetime,
               since: datetime | None = None) -> int:
    """Commits the `stale_event` of the user's RSI spec saves not compared yet; returns how many
    pages were marked (0 when there was nothing to compare)."""
    for attempt in range(STALE_TRIES):
        snap = repo.load(user_id)
        body = stale_event(repo, snap, ops_of, since)
        if body is None:
            return 0
        try:
            repo.commit(snap, Commit(wiki=[WikiEvent(event_id=new_id("wev"), ts=now,
                                                     body=body)]))
            return len(body.marked)
        except WikiConflict:
            if attempt == STALE_TRIES - 1:
                raise
    return 0                                                            # pragma: no cover
