"""Pages a teaching-spec change left out of date, rewritten lazily (round 2, task 19).

A spec change (written for the retired RSI-B; today the Bot's preference executor,
`bot.executors_prefs`) marks the pages it affects (`PagesStaleSet`); when the learner next
opens a page that is still out of date (an explicit open, `POST /wiki/pages/{id}/open?rewrite=1`; other reads
start nothing), the service starts one background rewrite under the current spec
(`service.rsi_runs.stale_rewrite`). Nothing is rewritten before it is opened, and there is no
page cap.

Retries are bounded (`rewrite_due`): a failed rewrite is counted on the mark (`PageRewriteFailed`);
after one failure the page is retried at most once per RETRY_AFTER, after MAX_FAILURES failures in a
row under the same current spec version it stops (still marked, counted as failed in 参数). A later
spec change re-arms it: a new mark starts at 0, and failures under an older version do not count.

Sections a K-change patch left waiting (stage 4a, single writer): the patch itself writes no text,
so a prose section of the current outline whose latest SectionLog has producer "patch" waits for the
page writer (`patch_pending`; no event of its own: any later write logs the section again, as
"refine", "writer" or "organizer"). `patch_due` bounds the scoped writes like the rewrites above
(`WikiState.patch_failures`, counted only while the waiting set is the one they failed on).

Pure reads over `WikiState`. A mark only counts while the page was last written under a spec version
older than the one the change produced: a rewrite under the current spec (or any later one) ends it
without another event, and a later change that does not touch the page does not revive it."""

from collections import Counter
from datetime import datetime, timedelta

from evowiki.goals.contracts import SectionRole
from evowiki.teach.contracts import ParamPatchOp, PatchBody, TeachingSpec
from evowiki.wikiapp.events import WHOLE_PAGE_WRITES, StaleMark
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.teachspec import effective_version

RETRY_AFTER = timedelta(hours=24)        # a page whose rewrite failed once: next try after this
MAX_FAILURES = 2                         # failures in a row under one spec version: no more tries


def page_version(state: WikiState, page_id: str) -> int:
    """The teaching-spec version the page's text was written under: the last whole-page write
    (organize / rewrite), else the oldest version among its section logs (a one-section feedback
    rewrite leaves the other sections as they were), else 1."""
    rec = state.pages.get(page_id)
    if rec is None:
        return 1
    w = rec.written
    if w is not None and w.kind in WHOLE_PAGE_WRITES and w.spec_version is not None:
        return w.spec_version
    logs = state.section_logs.get(page_id)
    if logs:
        return min(g.spec_version for g in logs.values())
    return 1


def stale_pages(state: WikiState) -> dict[str, StaleMark]:
    """Page id → its mark, for the organized pages that are still out of date."""
    out = {}
    for pid, m in state.stale_marks.items():
        rec = state.pages.get(pid)
        if rec is not None and rec.has_body and page_version(state, pid) < m.spec_version:
            out[pid] = m
    return out


def reorder_stale(state: WikiState, page_id: str) -> bool:
    """The page was written for a path order that has changed (`PageMarkedStale`); a whole-page
    write ends it. A page that is not organized yet or is in the trash is never due."""
    rec = state.pages.get(page_id)
    return (page_id in state.reorder_marks and rec is not None and rec.has_body
            and page_id not in state.trashed_pages)


def is_stale(state: WikiState, page_id: str) -> bool:
    return page_id in stale_pages(state)


def failures(mark: StaleMark, current_version: int) -> int:
    """Failed rewrites in a row that still count: those under the current spec version only."""
    return mark.failures if mark.failed_version == current_version else 0


def has_failed(mark: StaleMark, current_version: int) -> bool:
    """Stopped retrying: MAX_FAILURES failed rewrites under the current spec version."""
    return failures(mark, current_version) >= MAX_FAILURES


def rewrite_due(state: WikiState, page_id: str, now: datetime) -> bool:
    """Whether opening the page should start its rewrite: it is still out of date, has not failed
    MAX_FAILURES times under the current spec, and a single failure is at least RETRY_AFTER old."""
    if reorder_stale(state, page_id):
        r = state.reorder_marks[page_id]            # (the same bound; a new mark starts at 0)
        if _retry_ok(r.failures, r.failed_at, now):
            return True
    mark = stale_pages(state).get(page_id)
    if mark is None:
        return False
    return _retry_ok(failures(mark, effective_version(state)), mark.failed_at, now)


def _retry_ok(n: int, failed_at: datetime | None, now: datetime) -> bool:
    if n >= MAX_FAILURES:
        return False
    return not (n and failed_at is not None and now - failed_at < RETRY_AFTER)


def reorder_due(state: WikiState, page_id: str, now: datetime) -> bool:
    """The rewrite that is due is for a path-order mark (not an RSI one): it gets its own trigger
    so the RSI rewrite's data stays clean. A page marked both ways is rewritten as an RSI rewrite, unless
    that mark has run out of retries."""
    r = state.reorder_marks.get(page_id)
    if not (reorder_stale(state, page_id) and r is not None
            and _retry_ok(r.failures, r.failed_at, now)):
        return False
    mark = stale_pages(state).get(page_id)       # an RSI mark that is still due wins
    return mark is None or not _retry_ok(failures(mark, effective_version(state)),
                                         mark.failed_at, now)


def patch_pending(state: WikiState, page_id: str) -> tuple[str, ...]:
    """The prose sections of the page's current outline that a K-change patch inserted or rewrote
    and no writer has written since (their latest SectionLog has producer "patch"), page order;
    () for an unknown, unorganized or trashed page."""
    rec = state.pages.get(page_id)
    if rec is None or rec.page is None or page_id in state.trashed_pages:
        return ()
    logs = state.section_logs.get(page_id, {})
    return tuple(s.id for s in rec.page.outline.sections
                 if s.role is not SectionRole.CHECK
                 and (g := logs.get(s.id)) is not None and g.producer == "patch")


def patch_failures(state: WikiState, page_id: str, pending: tuple[str, ...]) -> int:
    """Failed scoped writes in a row that still count: those of exactly this waiting set."""
    f = state.patch_failures.get(page_id)
    return f.failures if f is not None and f.sections == pending else 0


def patch_due(state: WikiState, page_id: str, now: datetime) -> bool:
    """Whether the page's waiting sections should be written now: some wait (`patch_pending`), the
    write has not failed MAX_FAILURES times on this same set, and a single failure is at least
    RETRY_AFTER old."""
    pending = patch_pending(state, page_id)
    if not pending:
        return False
    n = patch_failures(state, page_id, pending)
    f = state.patch_failures.get(page_id)
    return _retry_ok(n, f.failed_at if f is not None and n else None, now)


def stale_counts(state: WikiState) -> tuple[Counter[str], Counter[str]]:
    """(waiting, failed): patch id → how many pages it marked are still waiting for their rewrite,
    and how many stopped retrying after failed rewrites (`has_failed`)."""
    v = effective_version(state)
    waiting: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    for m in stale_pages(state).values():
        (failed if has_failed(m, v) else waiting)[m.patch_id] += 1
    return waiting, failed


def _item_key(spec: TeachingSpec, item_id: str | None):
    i = next((x for x in spec.items if x.item_id == item_id), None)
    return None if i is None else (i.text, i.scope, i.domain)


def touches_differently(old: TeachingSpec, new: TeachingSpec, body: PatchBody) -> bool:
    """Whether what `body` changes (its item, or its param) differs between the two specs."""
    if isinstance(body, ParamPatchOp):
        return getattr(old.params, body.path, None) != getattr(new.params, body.path, None)
    return _item_key(old, body.item_id) != _item_key(new, body.item_id)


def same_rules(old: TeachingSpec, new: TeachingSpec) -> bool:
    """The same items (id, text, scope, domain) and params, whatever the version numbers."""
    def key(s: TeachingSpec):
        return tuple((i.item_id, i.text, i.scope, i.domain) for i in s.items), s.params
    return key(old) == key(new)
