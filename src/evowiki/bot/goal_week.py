"""The weekly goal review's facts (B5 spec §1.2): `load_goal_week` reads, through the caller's
connection only, one `GoalWeek` per active learning path (a page not done, not parked). All
deterministic; nothing is written.

Window: the 7 local days before the review's local day, plus that day up to `now` (local days
of the Bot settings' time zone). Path order is the one the user sees (`wikiapp.path_order`).

- Done: a path page whose wiki page is marked done (the same rule as `facts._goals`); done this
  week = its first `done` page record falls in the window. New this week: the wiki page was created
  in the window.
- Next page: the first shown, startable page not done; remaining = it and the startable pages
  after it that are not done.
- Active days: local days in the window with a path page opened, a signal on a path page, or an
  aspect Observe on a cell of the path's concepts.
- Review: fading cells (`goals.review.fading`, the user's review settings) of the path's concepts.
- Wrong: path cells answered wrong on >= `wrong_count` different questions in the window
  (`facts._wrong_answers` rules: slips excluded, one key per question).
- Weak prerequisites: graph prerequisites of the next page and the two after it (nodes the page
  itself does not teach) with a K cell at gap / shaky, or answered wrong this week. Where the path
  teaches it decides what the Bot may do (`WeakPrereq.taught`): the Bot never touches a page the
  user moved, removed, added or skipped, and never re-adds a node whose page the user removed.
- Known share: the remaining pages' target cells at band known (None: no cells).
- Reading time: seconds on the path's wiki pages in the window, known only when the user's first
  report is not later than the window's first day (else None: unknown, never 0).
"""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from evowiki.belief.aspects import AspectKView, Observe
from evowiki.bot.facts import _wrong_answers
from evowiki.bot.facts_types import GoalWeek, StuckPage, WeakPrereq
from evowiki.bot.settings import BotSettings
from evowiki.goals.contracts import Band, split_cell
from evowiki.goals.guardrails import ASPECT_ZH
from evowiki.goals.path_edit import Item, apply_edits
from evowiki.goals.review import fading
from evowiki.localday import local_date
from evowiki.store import bot as store_bot
from evowiki.store import bot_goals
from evowiki.store.aspects import aspect_events
from evowiki.store.wiki import wiki_events_of_type
from evowiki.wikiapp import path_order
from evowiki.wikiapp.events import PageOpened, PageSaved, SignalRecorded
from evowiki.wikiapp.kwrite import concept_name
from evowiki.wikiapp.path_order import page_needs
from evowiki.wikiapp.repo import _from_row, load_in
from evowiki.wikiapp.views import page_title

WEAK = (Band.GAP, Band.SHAKY)
PREREQ_PAGES = 3                 # the next page and the two after it
DECLINE_DAYS = 30


def window(now: datetime, tz: str) -> tuple[datetime, date, date]:
    """(UTC start, first local day, last local day) of the review window ending at `now`."""
    zone = ZoneInfo(tz)
    today = local_date(now, tz)
    first = today - timedelta(days=7)
    return datetime.combine(first, time(0), tzinfo=zone), first, today


def _user_touched(i: Item) -> bool:
    return i.moved or i.removed or i.mine or bool(i.skipped_nodes)


def _declines(conn: Any, user_id: str, path_id: str, since: datetime) -> int:
    return store_bot.count_declined(conn, user_id, kind="goal_idle",
                                    dedup_key=f"goal_idle:{path_id}", since=since)


def load_goal_week(conn: Any, user_id: str, *, now: datetime, settings: BotSettings,
                   week_start: date | None = None) -> tuple[GoalWeek, ...]:
    """One GoalWeek per active path, in planning order. `week_start`: the ISO week being reviewed
    (its Monday), for the previous week's snapshot (None: no snapshot read)."""
    snap = load_in(conn, user_id)
    state, k = snap.state, snap.k
    tz = settings.timezone
    zone = ZoneInfo(tz)
    start, first_day, last_day = window(now, tz)
    kv = AspectKView(k, now)
    parked = bot_goals.parked(conn, user_id)

    events = [s.event for s in aspect_events(conn, user_id, upto_seq=snap.aspect_seq)]
    wrong = [w for w in _wrong_answers(conn, user_id, events, now, settings,
                                      hidden_pages=state.trashed_pages) if w.at >= start]
    fading_cells = {f.cell for f in fading(k, now, state.review, postponed=state.review_postponed)}

    opened: list[tuple[str, datetime]] = []
    signal_pages: list[tuple[str, datetime]] = []
    done_at: dict[str, datetime] = {}
    for s in wiki_events_of_type(conn, user_id, ["page_opened", "signal_recorded", "page_saved"]):
        if s.seq > snap.wiki_seq:
            continue
        body = _from_row(s.event_id, s.occurred_at, s.payload).body
        if isinstance(body, PageOpened):
            opened.append((body.page_id, s.occurred_at))
        elif isinstance(body, SignalRecorded) and body.signal.page_id is not None:
            signal_pages.append((body.signal.page_id, body.signal.at))
        elif isinstance(body, PageSaved) and body.record.done:
            done_at.setdefault(body.record.page_id, s.occurred_at)

    known_since = bot_goals.first_reading_day(conn, user_id)
    reading_known = known_since is not None and known_since <= first_day

    def local_day(t: datetime) -> date:
        return t.astimezone(zone).date()

    out: list[GoalWeek] = []
    for path_id in state.path_order:
        rec = state.paths[path_id]
        if path_id in parked:
            continue
        items = apply_edits(rec.path.pages, rec.edits)
        shown = path_order.shown(rec)
        startable = path_order.startable(rec)
        wiki_of = {i.key: rec.page_ids[path_order.stored_index(rec, i.key)]
                   for i in items if i.page is not None}

        def done(i: Item, wiki_of: dict = wiki_of) -> bool:
            pid = wiki_of.get(i.key)
            return pid is not None and pid in state.pages and state.pages[pid].done

        remaining = [i for i in startable if not done(i)]
        if not remaining:
            continue                                    # finished: not an active goal
        nxt = remaining[0]
        nxt_pid = wiki_of.get(nxt.key)
        pages = {p for p in rec.page_ids if p is not None and p in state.pages}
        concepts = {c for p in rec.path.pages for c in p.concepts}

        def title_of(i: Item, wiki_of: dict = wiki_of) -> str:
            pid = wiki_of.get(i.key)
            return page_title(state, pid, user_id=user_id) if pid in state.pages else i.title

        days: set[date] = set()
        days |= {local_day(t) for p, t in opened if p in pages and t >= start}
        days |= {local_day(t) for p, t in signal_pages if p in pages and t >= start}
        days |= {local_day(e.ts) for e in events if isinstance(e, Observe) and e.ts >= start
                 and split_cell(e.observation.cell)[0] in concepts}

        by_cell: dict[str, set[str]] = {}
        for w in wrong:
            if split_cell(w.cell)[0] in concepts:
                by_cell.setdefault(w.cell, set()).add(w.question)
        wrong_cells = sorted(c for c, qs in by_cell.items() if len(qs) >= settings.wrong_count)
        wrong_concepts = {split_cell(w.cell)[0] for w in wrong}

        weak: list[WeakPrereq] = []
        seen: set[str] = set()
        for page in remaining[:PREREQ_PAGES]:
            for target in page.node_ids:
                for p in state.graph.prereqs(target):
                    if p in seen or p in page.node_ids:
                        continue
                    # 「建议先复习」 on the next step already speaks for a wrong answer still standing
                    if any(kv.open_miss(c) is not None for c in kv.cells((p,))):
                        continue
                    # evidence, not a bare declaration: a measured gap / shaky cell or a wrong
                    # answer this week
                    measured = any(b.band in WEAK and b.declared is None
                                   for b in kv.cells((p,)).values())
                    if not (measured or p in wrong_concepts):
                        continue
                    seen.add(p)
                    weak.append(_weak(p, _name(state, p, user_id), page, items, shown, done,
                                      lambda i, w=wiki_of: w.get(i.key) is not None, title_of,
                                      lambda i: page_needs(state, i)))

        cells = kv.cells(tuple(dict.fromkeys(n for i in remaining for n in i.node_ids)))
        known_share = (sum(b.band is Band.KNOWN for b in cells.values()) / len(cells)
                       if cells else None)

        last = max((state.opened_at[p] for p in pages if p in state.opened_at),
                   default=rec.created_at)
        reading = stuck = None
        if reading_known:
            secs = bot_goals.reading_by_page(conn, user_id, pages, first_day, last_day)
            reading = sum(secs.values())
            unfinished = [(s, p) for p, s in secs.items()
                          if p in state.pages and not state.pages[p].done]
            if unfinished:
                s, p = max(unfinished)
                stuck = StuckPage(page_id=p, title=page_title(state, p, user_id=user_id),
                                  seconds=s)

        prev = (bot_goals.review_before(conn, user_id, path_id, week_start)
                if week_start is not None else None)
        out.append(GoalWeek(
            path_id=path_id, title=rec.path.goal,
            pages_shown=sum(1 for i in shown if i.page is not None),
            pages_done=sum(1 for i in shown if i.page is not None and done(i)),
            done_this_week=sum(1 for i in shown if wiki_of.get(i.key) in done_at
                               and done_at[wiki_of[i.key]] >= start and done(i)),
            new_pages_this_week=sum(1 for p in pages if state.pages[p].created_at >= start),
            next_key=nxt.key, next_page_id=nxt_pid, next_title=title_of(nxt),
            next_position=next(n for n, i in enumerate(shown, 1) if i.key == nxt.key),
            active_days=len(days),
            review_due=sum(1 for c in fading_cells if split_cell(c)[0] in concepts),
            review_cap=state.review.max_per_day,
            wrong_cells=tuple(_cell_name(state, c, user_id) for c in wrong_cells),
            weak_prereqs=tuple(weak), known_share=known_share,
            idle_declines=_declines(conn, user_id, path_id, now - timedelta(days=DECLINE_DAYS)),
            days_since_open=max(0, int((now - last).total_seconds() // 86400)),
            reading_seconds=reading, stuck=stuck,
            last_week=None if prev is None else prev.facts))
    return tuple(out)


def _name(state, node_id: str, user_id: str) -> str:
    """A node's name in the graph, else the concept's recorded name."""
    node = state.graph.node(node_id)
    return node.name if node is not None and node.name else concept_name(state, node_id,
                                                                           user_id=user_id)


def _cell_name(state, cell: str, user_id: str) -> str:
    cid, aspect = split_cell(cell)
    return f"{_name(state, cid, user_id)}（{ASPECT_ZH[aspect]}）"


def _weak(node: str, name: str, dep: Item, items: list[Item], shown: list[Item], done, started,
          title_of, needs) -> WeakPrereq:
    """Where the path teaches `node`, needed by page `dep` (WeakPrereq.taught):
    - "missing": no page teaches it -> the Bot may insert one right before `dep`;
    - "ordered": an unfinished page teaching it already comes before `dep` (nothing to do);
    - "done": a finished page teaches it;
    - "page": every page teaching it comes after `dep`, and the first of them may move right
      before `dep` (not started, not touched by the user, and it needs nothing taught by the
      pages it would jump over: never ahead of a page it depends on);
    - "fixed": anything else (the user moved / removed / added / skipped it, or it started, or
      moving it would put it ahead of what it needs): the Bot leaves it."""
    base = WeakPrereq(node_id=node, name=name, for_title=title_of(dep), taught="missing",
                      before_key=dep.key)
    teaching = [i for i in items if node in i.node_ids]
    if not teaching:
        return base
    if any(i.removed for i in teaching):
        return replace(base, taught="fixed")         # the user took it out: never put back
    if any(done(i) for i in teaching):
        return replace(base, taught="done")
    keys = [i.key for i in shown]
    at = keys.index(dep.key)
    shown_teaching = sorted((i for i in teaching if i.key in keys), key=lambda i: keys.index(i.key))
    if any(keys.index(i.key) < at for i in shown_teaching):
        return replace(base, taught="ordered")
    cand = next((i for i in shown_teaching if i.page is not None and not _user_touched(i)
                 and not started(i)), None)
    if cand is None:
        return replace(base, taught="fixed")
    jumped = {n for i in shown[at:keys.index(cand.key)] for n in i.node_ids}
    if needs(cand) & jumped:
        return replace(base, taught="fixed")
    return replace(base, taught="page", page_key=cand.key, page_title=title_of(cand))
