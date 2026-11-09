"""Rule inputs: what the Bot's pure rules see of one user (B3 spec §3), read in one transaction.

`load_facts(conn, ...)` and `new_event_ids(conn, ...)` use the passed connection only (the
pipeline calls them inside the transaction that also locks the cursor and writes intents).

Adapter rules:
- Wrong answer = an aspect `Observe` whose observation kind is quiz / check / recall and whose
  polarity is not "+", minus those the user claimed as a slip (`Claim` slip) unless a later
  `ResolveClaim` refuted the claim. A later correct answer to that question clears its earlier
  errors. `learned` claims do not exclude it. Future, trashed and hidden evidence is excluded.
- `question_key`: `check:{page}:{sid}` stays whole (one check per section); `review:…:{id}` and
  `block:…:{rid}.{i}` drop their last `:`-segment (the per-attempt id), so repeated attempts at
  one question share a key.
- Misconception: only block quiz answers have one, the `case` of the quiz question, found by
  reading the block asset the reference names; anything unresolvable is None (debug log).
- Due review: the numbers `wikiapp.review.review_today` computes, on the snapshot `load_in` read.
- Goals: learning paths with a page not done (`review.goal_concepts` rule).
- Open questions: the checked `learning.unclear` of each page-bound assistant session's newest
  compaction (compact@v7, `assistant.summary.read_recap`: only items whose evidence is one of his
  messages), concepts joined with "；". Never its to-dos, and nothing from an entry before v7
  (their 还没懂的 mixed in to-dos and the assistant's own questions).

- Page facts (`load_page_facts`, the page-chip rule): the cells the page's outline sections cover;
  the newest 还没懂的 of the page's sessions (opened since or not: the user is on the page now);
  the concepts of those cells answered wrong within `wrong_days`, newest first.
- Labels: a page's title with its path position (`标题（2/5）`, first path by creation order).

Node-graph isolation: this module is the only place that knows rules key on K cells (the
`cell:<id>` dedup keys are built in the rules from `WrongAnswer.cell`); when nodes arrive only
this adapter changes.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from evowiki.assistant.summary import read_recap
from evowiki.belief.aspects import Claim, Observe, ResolveClaim
from evowiki.bot.facts_types import (
    DueReview,
    GoalFact,
    HandEditFact,
    OpenQuestion,
    PageFacts,
    SignalFact,
    SourceMatchFact,
    UserFacts,
    WrongAnswer,
    question_key,
)
from evowiki.bot.rules import TEACHING_KINDS
from evowiki.bot.settings import BotSettings
from evowiki.deep.evoui import node_keys, quizzes
from evowiki.goals.contracts import split_cell
from evowiki.goals.guardrails import ASPECT_ZH
from evowiki.sources.exam_store import exam_counts, frequent_nodes
from evowiki.store import bot_goals, dreaming
from evowiki.store.aspects import aspect_events, aspect_events_after
from evowiki.store.bot import forgotten_words
from evowiki.store.db import fetch_all, fetch_one
from evowiki.store.wiki import wiki_events_after, wiki_events_of_type
from evowiki.teach.contracts import Signal
from evowiki.teach.signal_kinds import polarity
from evowiki.wikiapp import path_order
from evowiki.wikiapp.events import HandEdited, SignalRecorded, SourceMatched
from evowiki.wikiapp.kwrite import concept_name
from evowiki.wikiapp.repo import _from_row, load_in
from evowiki.wikiapp.review import (
    _done_today,
    day_start,
    deadline_days,
    goal_concepts,
    last_answered,
    schedule,
    sources,
)
from evowiki.wikiapp.views import page_title

log = logging.getLogger(__name__)

WRONG_KINDS = frozenset({"quiz", "check", "recall"})
SIGNAL_LIMIT = 20
SOURCE_MATCH_DAYS = 14


def _block_nodes(conn: Any, user_id: str, asset_id: str, cache: dict[str, Any]) -> Any:
    """(node_keys, quiz nodes) of a stored EvoUI block, None when it cannot be read."""
    if asset_id not in cache:
        r = fetch_one(conn, "select html from wiki_asset where asset_id = %s and user_id = %s "
                            "and kind = 'evoui'", (asset_id, user_id))
        try:
            block = None if r is None else json.loads(r["html"])
            cache[asset_id] = None if block is None else (
                node_keys(block), quizzes(block.get("body")))
        except (ValueError, TypeError, KeyError, AttributeError):
            cache[asset_id] = None
    return cache[asset_id]


def _block_question(conn: Any, user_id: str, ref: str, cache: dict[str, Any]
                    ) -> tuple[str, str | None]:
    """(question key, misconception) of a block answer ref
    `block:{page}:{sid}:{asset}:{target}:{rid}`; target is `{node key}.{item}` (v2 item events) or
    `{quiz index}.{question}` (v1 answers). A v1 target is rewritten to the quiz node's key so one
    question has one key. Unreadable asset / target: the raw key and None (debug log)."""
    raw = question_key(ref)
    parts = ref.split(":")
    if len(parts) != 6:
        log.debug("block ref %r is not block:page:sid:asset:target:id", ref)
        return raw, None
    nodes = _block_nodes(conn, user_id, parts[3], cache)
    if nodes is None:
        log.debug("block asset %s unavailable", parts[3])
        return raw, None
    keys, quiz_nodes = nodes
    head, _, item = parts[4].rpartition(".")
    try:
        if head.isdigit():
            node = quiz_nodes[int(head)]
            key = next(k for k, n in keys.items() if n is node)
            raw = ":".join([*parts[:4], f"{key}.{item}"])
        else:
            node = keys[head]
        case = node["questions"][int(item)].get("case")
    except (KeyError, IndexError, ValueError, TypeError, AttributeError, StopIteration):
        log.debug("block target %r not found in %s", parts[4], parts[3])
        return raw, None
    return raw, case if isinstance(case, str) and case else None


def _wrong_answers(conn: Any, user_id: str, events: list, now: datetime,
                   settings: BotSettings, *, hidden_pages: set[str] | frozenset[str] = frozenset()
                   ) -> tuple[WrongAnswer, ...]:
    if dreaming.paused(conn, user_id):
        return ()
    refuted = {e.claim_event_id for e in events
               if isinstance(e, ResolveClaim) and e.outcome == "refuted"}
    slipped = {e.source_event_id for e in events
               if isinstance(e, Claim) and e.claim == "slip" and e.event_id not in refuted}
    since = now - timedelta(days=settings.wrong_days)
    cache: dict[str, Any] = {}
    unresolved: dict[tuple[str, str], list[Observe]] = {}
    questions: dict[str, tuple[str, str | None]] = {}
    # Stable sorting preserves append order for attempts recorded at the same instant.
    for e in sorted(events, key=lambda e: e.ts):
        if (not isinstance(e, Observe) or e.event_id in slipped
                or e.observation.kind not in WRONG_KINDS or not since <= e.ts <= now):
            continue
        ref = e.observation.evidence_ref
        questions[e.event_id] = (_block_question(conn, user_id, ref, cache)
                                 if ref.startswith("block:") else (question_key(ref), None))
        key = e.observation.cell, questions[e.event_id][0]
        if e.observation.polarity == "+":
            unresolved[key] = []
        else:
            unresolved.setdefault(key, []).append(e)
    active = {e.event_id for attempts in unresolved.values() for e in attempts}
    out = []
    for e in events:
        if e.event_id not in active:
            continue
        o = e.observation
        question, misc = questions[e.event_id]
        answer = WrongAnswer(
            event_id=e.event_id, cell=o.cell, question=question, at=e.ts, misconception=misc)
        if answer.page_id in hidden_pages:
            continue
        refs = [o.evidence_ref]
        if answer.page_id is not None:
            refs.append(f"page:{answer.page_id}")
        if dreaming.sources_visible(conn, user_id, refs):
            out.append(answer)
    return tuple(sorted(out, key=lambda w: (w.at, w.event_id)))


def _open_questions(conn: Any, user_id: str, opened_at: dict[str, datetime]
                    ) -> tuple[OpenQuestion, ...]:
    from evowiki.store import dreaming
    if dreaming.paused(conn, user_id):
        return ()
    # Learning records have their own evidence and lifecycle; prose statements and
    # memory_item.open_question are different inputs and never become learning here.
    learning: list[dict] = []
    covered: set[str] = set()
    source_pages = {row['session_id']: row['page_id'] for row in fetch_all(
        conn, 'select session_id,page_id from assistant_session where user_id=%s', (user_id,))}
    forgotten = forgotten_words(conn, user_id)
    for summary in dreaming.list_summaries(conn, user_id, 'anchor'):
        if (summary.dirty or not dreaming.sources_visible(conn, user_id, summary.sources)):
            continue
        for item in summary.payload.get('learning', []):
            nodes = item.get('sources', [])
            if (item.get('state') not in ('unclear', 'understood') or not nodes
                    or not set(nodes) <= set(summary.sources)
                    or not all(node.startswith('turn:') for node in nodes)
                    or not dreaming.sources_visible(conn, user_id, nodes)):
                continue
            sid = nodes[0][5:].split('/', 1)[0]
            page_id = source_pages.get(sid)
            if page_id is None:
                continue
            covered.update(nodes)
            learning.append({'page_id': page_id, 'session_id': sid,
                             'concept': item.get('concept') or item['text'],
                             'text': item['text'], 'state': item['state'],
                             'at': datetime.fromisoformat(item['recorded_at'])
                                   if item.get('recorded_at') else summary.updated_at})
    rows = fetch_all(
        conn,
        "select distinct on (s.session_id) s.session_id, s.page_id, e.payload, e.occurred_at "
        "from assistant_entry e join assistant_session s on s.session_id = e.session_id "
        "where s.user_id = %s and s.page_id is not null and e.type = 'compaction' "
        "order by s.session_id, e.seq desc", (user_id,))
    for row in rows:
        recap = read_recap(row['payload'])
        if recap.legacy:
            continue
        for state in ('unclear', 'understood'):
            for item in getattr(recap.learning, state):
                node = f"turn:{row['session_id']}/{item.evidence_id}"
                if node in covered or not dreaming.sources_visible(conn, user_id, [node]):
                    continue
                learning.append({'page_id': row['page_id'], 'session_id': row['session_id'],
                                 'concept': item.concept, 'text': item.concept,
                                 'state': state, 'at': row['occurred_at']})
    latest = {}
    for item in sorted(learning, key=lambda item: (item['at'], item['session_id'])):
        latest[item['page_id'], item['concept'].strip().casefold()] = item
    grouped: dict[tuple[str, str], list[dict]] = {}
    for item in latest.values():
        if item['state'] == 'unclear' and not any(word in item['text'] for word in forgotten):
            grouped.setdefault((item['page_id'], item['session_id']), []).append(item)
    out = []
    for (page_id, sid), items in grouped.items():
        at = max(item['at'] for item in items)
        opened = opened_at.get(page_id)
        out.append(OpenQuestion(session_id=sid, page_id=page_id,
                                text='；'.join(item['text'] for item in items), at=at,
                                page_opened_since=opened is not None and opened > at))
    return tuple(sorted(out, key=lambda question: (question.at, question.session_id)))


def _source_matches(conn: Any, user_id: str, now: datetime, upto_seq: int
                    ) -> tuple[SourceMatchFact, ...]:
    """`SourceMatched` events of the last `SOURCE_MATCH_DAYS` days. The state reducer ignores
    them, so they are read from the event table; a material that matched no page is left out."""
    since = now - timedelta(days=SOURCE_MATCH_DAYS)
    out = []
    for s in wiki_events_of_type(conn, user_id, ["source_matched"]):
        if s.seq > upto_seq or s.occurred_at < since:
            continue
        body = _from_row(s.event_id, s.occurred_at, s.payload).body
        if isinstance(body, SourceMatched) and body.pages:
            out.append(SourceMatchFact(
                event_id=s.event_id, source_id=body.source_id, title=body.title,
                pages=tuple((p.page_id, p.title) for p in body.pages[:3])))
    return tuple(out)


def _hand_edits(conn: Any, user_id: str, now: datetime, upto_seq: int) -> tuple[HandEditFact, ...]:
    since = now - timedelta(days=SOURCE_MATCH_DAYS)
    out = []
    for s in wiki_events_of_type(conn, user_id, ["hand_edited"]):
        if s.seq > upto_seq or s.occurred_at < since:
            continue
        body = _from_row(s.event_id, s.occurred_at, s.payload).body
        if isinstance(body, HandEdited):
            written = tuple(c.section_id for c in body.changes if c.after_text is not None)
            if written:
                out.append(HandEditFact(event_id=s.event_id, page_id=body.page_id, sections=written))
    return tuple(out)


def _goals(state) -> tuple[GoalFact, ...]:
    out = []
    for rec in state.paths.values():
        st = state.goal_settings.get(rec.path.path_id)
        if st is not None and st.archived:
            continue
        nxt, unfinished = None, False
        for pid in path_order.startable_page_ids(rec):   # the order the user sees, as 「下一页」
            if pid is None:                      # not started yet
                unfinished = True
                break
            if pid not in state.pages or not state.pages[pid].done:
                nxt, unfinished = pid, True
                break
        if not unfinished:
            continue
        opened = [state.opened_at[p] for p in rec.page_ids if p is not None and p in state.opened_at]
        out.append(GoalFact(path_id=rec.path.path_id, title=rec.path.goal, next_page_id=nxt,
                            last_opened=max(opened) if opened else None,
                            created_at=rec.created_at))
    return tuple(sorted(out, key=lambda g: (g.created_at, g.path_id)))


class UnknownPage(LookupError):
    """The user has no page with this id."""


def page_label(state, page_id: str, *, user_id: str = "") -> str:
    """The page's title. A path row is a node and its title is the node's name, so no position
    suffix (`（2/5）`) is added; the path order is shown by the path view, not by the label."""
    return page_title(state, page_id, user_id=user_id)


def page_cells(state, page_id: str) -> frozenset[str]:
    """The K cells a page's outline sections cover (a check section's cell included)."""
    rec = state.pages[page_id]
    if rec.page is None:
        return frozenset()
    cells: set[str] = set()
    for sec in rec.page.outline.sections:
        cells.update(sec.cells)
        if sec.check is not None:
            cells.add(sec.check.cell)
    return frozenset(cells)


def load_page_facts(conn: Any, user_id: str, page_id: str, *, now: datetime,
                    settings: BotSettings) -> PageFacts:
    """What the page-chip rule reads for one page, through `conn` only. LookupError for a page
    the user does not have (`UnknownPage`)."""
    snap = load_in(conn, user_id)
    state = snap.state
    if page_id not in state.pages:
        raise UnknownPage(f"unknown page {page_id!r}")
    here = [q for q in _open_questions(conn, user_id, state.opened_at) if q.page_id == page_id]
    text = max(here, key=lambda q: (q.at, q.session_id)).text if here else None
    return PageFacts(page_id=page_id, label=page_label(state, page_id, user_id=user_id),
                     open_text=text)


def load_facts(conn: Any, user_id: str, *, now: datetime, settings: BotSettings) -> UserFacts:
    """Everything the rules read, through `conn` only. Aspect and signal reads stop at the
    snapshot's seqs, so they agree with the state the snapshot replayed."""
    snap = load_in(conn, user_id)
    state = snap.state
    events = [s.event for s in aspect_events(conn, user_id, upto_seq=snap.aspect_seq)]

    wrong = _wrong_answers(conn, user_id, events, now, settings,
                           hidden_pages=state.trashed_pages)

    done = _done_today(events, now, settings.timezone)
    plan = schedule(snap.k, now, sources(state), goal_concepts(state), state.review,
                    already_reviewed=done,
                    deadline_days=deadline_days(state, now, settings.timezone),
                    postponed=state.review_postponed,
                    exam_concepts=frequent_nodes(exam_counts(conn, user_id)),
                    answered=last_answered(events, day_start(now, settings.timezone)))
    due = DueReview(count=plan.due, cells=tuple(c for i in plan.items for c in i.cells),
                    first_page_id=plan.items[0].page_id if plan.items else None)

    parked = bot_goals.parked(conn, user_id)          # parked goals: no goal_idle card (B5a)
    goals = tuple(g for g in _goals(state) if g.path_id not in parked)
    questions = _open_questions(conn, user_id, state.opened_at)

    sigs = []
    for s in wiki_events_of_type(conn, user_id, ["signal_recorded"]):
        if s.seq > snap.wiki_seq:
            continue
        body = _from_row(s.event_id, s.occurred_at, s.payload).body
        if isinstance(body, SignalRecorded):
            sig: Signal = body.signal
            p = polarity(sig)
            if p != 0:
                sigs.append(SignalFact(
                    event_id=s.event_id, at=sig.at, kind=sig.kind, polarity=p,
                    written=sig.kind == "page_feedback" and bool(sig.text.strip())))
    # only signals about the explanation (strategy_negative v2): a run of answers must not push
    # them out of the window
    signals = tuple([s for s in sigs if s.kind in TEACHING_KINDS][-SIGNAL_LIMIT:])

    names: dict[str, str] = {}
    cells = {w.cell for w in wrong} | set(due.cells)
    for cell in sorted(cells):
        cid, aspect = split_cell(cell)
        names[cid] = concept_name(state, cid, user_id=user_id)
        names[cell] = f"{names[cid]}（{ASPECT_ZH[aspect]}）"
    pages = {due.first_page_id, *(g.next_page_id for g in goals),
             *(q.page_id for q in questions), *(w.page_id for w in wrong)}
    labels: dict[str, str] = {}
    for pid in sorted(p for p in pages if p is not None and p in state.pages
                      and p not in state.trashed_pages):
        names[pid] = page_title(state, pid, user_id=user_id)
        labels[pid] = page_label(state, pid, user_id=user_id)

    return UserFacts(user_id=user_id, wrong_answers=wrong, due_review=due, goals=goals,
                     open_questions=questions, signals=signals, names=names, labels=labels,
                     source_matches=_source_matches(conn, user_id, now, snap.wiki_seq),
                     hand_edits=_hand_edits(conn, user_id, now, snap.wiki_seq))


def remedial_is_current(conn: Any, user_id: str, *, cell: str, evidence: list[str],
                        page_id: str | None, now: datetime, settings: BotSettings) -> bool:
    """Recheck a queued or displayed card after answers, slips, deletion or time changed."""
    facts = load_facts(conn, user_id, now=now, settings=settings)
    if page_id not in facts.labels:
        return False
    ids = set(evidence)
    questions = {w.question for w in facts.wrong_answers
                 if w.cell == cell and w.event_id in ids and w.page_id in facts.labels}
    return len(questions) >= settings.wrong_count


def new_event_ids(conn: Any, user_id: str, wiki_after: int, aspect_after: int
                  ) -> tuple[frozenset[str], int, int]:
    """(ids of the wiki + aspect events after the cursor, new max wiki seq, new max aspect seq);
    a seq with nothing after it stays at the cursor value."""
    wiki = wiki_events_after(conn, user_id, wiki_after)
    aspect = aspect_events_after(conn, user_id, aspect_after)
    ids = frozenset([e.event_id for e in wiki] + [e.event.event_id for e in aspect])
    return (ids, wiki[-1].seq if wiki else wiki_after,
            aspect[-1].seq if aspect else aspect_after)
