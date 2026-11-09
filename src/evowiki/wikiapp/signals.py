"""Feedback signals (R1): every observable user reaction normalised into one `Signal`.

Written (`signal_recorded {signal}`) by the flows in the SAME commit as the action:
  section know / deeper / other  → section_know / section_deeper / section_other (kchange)
  check answer                   → check_answer, outcome 1 correct / 0 wrong / 0 不知道 (text 不知道)
  review grade                   → review_grade, 想起来了 1 / 有点模糊 0.5 / 没想起来 0; check mode =
                                   correctness (text = the chosen option, or 不知道)
  section unfold (/fold false)   → expand with section_id
  page 「恢复成完整页」          → expand without a section
  page feedback / hand edit      → page_feedback (wikiapp.feedback) / note_edit (wikiapp.hand_edit)
`assistant_rejected` is never written either (`assistant_rejections`): a rejected assistant proposal
or a rejected / undone assistant ledger action, with the id of the turn's run.
`assistant_rated` is never written either (`assistant_ratings`): an answer's current 👎 (its
latest `rating` session entry is "down"), with the id of the turn's run; text = the chips and the
note (and the question, for a turn that ended on one). `assistant_feedback` = both, the assistant signals a harness round reads.
`user_complaint` is written by the assistant's `report_problem` (`record_complaint`): the learner
complained in the chat about what the system does; text = 「problem。原话：「quote」」, page = the
session's page.
`assistant_selection` is never written: it is derived at read time from the assistant sessions
(a `user` entry carrying a selection; page = the session's page), signal_id `asel:{entry_id}`,
text = the selection (≤2000), section = the outline section whose heading matches the selection's.
The end-of-page 内容太简单 / 理解有困难 are sent as a turn on the page, with or without a selection
(`session.PAGE_TAPS`): their words are the signal's text and they carry their chip (`END_CHIPS`).

`spec_version` / `infer_version` come from the section's latest SectionLog, else the page's, else
None (`sectionlog.log_versions`) — read from the state the user acted on (before the action).
Signals carry only observable data: ids, the user's chips / words / choice, the outcome."""

import hashlib
import logging
import re
from collections.abc import Callable, Collection, Iterable
from datetime import datetime
from typing import Any, NamedTuple, Protocol, get_args

from evowiki.assistant.lineage import answer_artifact
from evowiki.assistant.session import PAGE_TAPS, Entry, SessionInfo, SessionStore, latest_ratings
from evowiki.goals.review import Grade
from evowiki.store.artifacts import is_artifact_id
from evowiki.teach.contracts import FeedbackChip, Signal, SignalKind
from evowiki.wikiapp.events import SignalRecorded, WikiEvent
from evowiki.wikiapp.repo import Commit, Repo, WikiInvalid
from evowiki.wikiapp.sectionlog import log_versions
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import DEFAULT_SIGNAL_LIMIT, MAX_SIGNAL_LIMIT, SignalsOut

log = logging.getLogger("evowiki.wikiapp.signals")

MAX_TEXT = 2000
IDK_TEXT = "不知道"
GRADE_OUTCOME: dict[Grade, float] = {"recalled": 1.0, "fuzzy": 0.5, "forgot": 0.0}
GRADE_TEXT: dict[Grade, str] = {"recalled": "想起来了", "fuzzy": "有点模糊", "forgot": "没想起来"}
SIGNAL_KINDS: frozenset[str] = frozenset(get_args(SignalKind))
ASEL_PREFIX = "asel:"
SIGNAL_EVENT = SignalRecorded.model_fields["type"].default


def make_signal(state: WikiState, kind: SignalKind, *, at: datetime, signal_id: str,
                page_id: str | None = None, section_id: str | None = None,
                chips: Iterable[FeedbackChip] = (), text: str = "",
                outcome: float | None = None, target: str | None = None) -> Signal:
    """One Signal; versions from `state` (the state the user acted on). Text is capped. `target`:
    the artifact the user was shown (the id the client sent back; ruling 3: never looked up now);
    the evidence type follows the kind (`teach.contracts.EVIDENCE`)."""
    spec_v, infer_v = (log_versions(state, page_id, section_id) if page_id is not None
                       else (None, None))
    return Signal(signal_id=signal_id, kind=kind, at=at, page_id=page_id, section_id=section_id,
                  chips=tuple(chips), text=text[:MAX_TEXT], outcome=outcome,
                  spec_version=spec_v, infer_version=infer_v, target_artifact_id=target)


def signal_event(state: WikiState, kind: SignalKind, *, now: datetime,
                 new_id: Callable[[str], str], page_id: str | None = None,
                 section_id: str | None = None, chips: Iterable[FeedbackChip] = (),
                 text: str = "", outcome: float | None = None,
                 target: str | None = None) -> WikiEvent:
    """`signal_recorded` for a flow's commit (signal id `sig…`, at = the event ts)."""
    sig = make_signal(state, kind, at=now, signal_id=new_id("sig"), page_id=page_id,
                      section_id=section_id, chips=chips, text=text, outcome=outcome,
                      target=target)
    return WikiEvent(event_id=new_id("wev"), ts=now, body=SignalRecorded(signal=sig))


def complaint_text(problem: str, quote: str) -> str:
    return f"{' '.join(problem.split())}。原话：「{' '.join(quote.split())}」"


def record_complaint(repo: Repo, user_id: str, *, problem: str, quote: str,
                     page_id: str | None, session_id: str, message_id: str,
                     now: datetime) -> str:
    """Commits one `user_complaint` signal (the harness round's improver reads it); returns its
    id. An unknown page is recorded without one (the signal is about the system, not a page)."""
    from evowiki.memory.forget import ForgetBusy
    from evowiki.store.dreaming import sources_visible
    from evowiki.store.wiki import WikiEventConflict
    sources = [f'session:{session_id}', f'turn:{session_id}/{message_id}']
    pool = getattr(repo, 'pool', None) or getattr(getattr(repo, 'inner', None), 'pool', None)
    if pool is not None:
        with pool.connection() as conn:
            if not sources_visible(conn, user_id, sources):
                raise ForgetBusy('complaint source is no longer available')
    digest = hashlib.sha256(f'{user_id}\0{session_id}\0{message_id}'.encode()).hexdigest()[:24]
    signal_id = 'sig_' + digest
    def recorded():
        if pool is not None:
            with pool.connection() as conn:
                return conn.execute('select 1 from wiki_event where user_id=%s and event_id=%s',
                                    (user_id, 'wev_' + digest)).fetchone() is not None
        return any(isinstance(e.body, SignalRecorded) and e.body.signal.signal_id == signal_id
                   for e in repo.wiki_events_of(user_id, (SIGNAL_EVENT,)))
    if recorded():
        return signal_id
    snap = repo.load(user_id)
    page = page_id if page_id is not None and page_id in snap.state.pages else None
    ev = signal_event(snap.state, "user_complaint", now=now,
                      new_id=lambda prefix: prefix + '_' + digest, page_id=page,
                      text=complaint_text(problem, quote))
    assert isinstance(ev.body, SignalRecorded)
    def handoff(conn):
        if conn is None:
            return
        got = conn.execute('select pg_try_advisory_xact_lock(hashtext(%s)) as ok',
                           ('memory:user:' + user_id,)).fetchone()
        if not got['ok']:
            raise ForgetBusy('memory is changing; retry the complaint')
        if not sources_visible(conn, user_id, sources):
            raise ForgetBusy('complaint source is no longer available')
        signal_id = ev.body.signal.signal_id
        conn.execute('insert into rsi_feedback(signal_id,user_id,event_id,eligible) values (%s,%s,%s,true)',
                     (signal_id, user_id, ev.event_id))
        with conn.cursor() as cur:
            cur.executemany('insert into memory_dependency(user_id,source,target,owner) values (%s,%s,%s,%s) '
                            'on conflict do nothing',
                            [(user_id, source, 'signal:' + signal_id, 'signal:' + signal_id) for source in sources])
    try:
        repo.commit(snap, Commit(wiki=[ev], extra=[handoff]))
    except WikiEventConflict:
        if not recorded():
            raise
    return ev.body.signal.signal_id


# ---- assistant selections (derived, never stored) ----

def _section_by_heading(state: WikiState, page_id: str | None, heading: str | None) -> str | None:
    if page_id is None or not heading or not heading.strip():
        return None
    rec = state.pages.get(page_id)
    if rec is None or rec.page is None:
        return None
    want = heading.strip()
    sections = rec.page.outline.sections
    hit = next((s for s in sections if s.heading.strip() == want), None)
    if hit is None:
        folded = want.casefold()
        hit = next((s for s in sections if s.heading.strip().casefold() == folded), None)
    return hit.id if hit is not None else None


END_CHIPS: dict[str, FeedbackChip] = dict(zip(PAGE_TAPS, ("too_shallow", "too_hard"), strict=True))


def selection_signal(state: WikiState, info: SessionInfo, entry: Entry) -> Signal | None:
    """The `assistant_selection` signal of one `user` entry, or None if it has no selection."""
    sel = entry.payload.get("selection")
    words = str(entry.payload.get("text") or "").strip()
    chip = END_CHIPS.get(words) if info.page_id is not None else None
    if not isinstance(sel, dict) or not str(sel.get("text") or "").strip():
        if chip is None:
            return None
        sel = {"text": words}
    page_id = info.page_id
    section_id = _section_by_heading(state, page_id, sel.get("heading"))
    shown = sel.get("artifact_id")       # the section version the text was selected from (client)
    sig = make_signal(state, "assistant_selection", at=entry.at,
                      signal_id=f"{ASEL_PREFIX}{entry.entry_id}", page_id=page_id,
                      section_id=section_id, chips=(chip,) if chip else (),
                      text=words if chip else str(sel["text"]),
                      target=shown if isinstance(shown, str) and is_artifact_id(shown) else None)
    # derived at read time: `state` is today's, so its log versions are not what he was reading
    return sig.model_copy(update={"spec_version": None, "infer_version": None})


# ---- assistant rejections (derived, never stored) ----

AREJ_PREFIX = "arej:"
AACT_PREFIX = "aact:"
REJECT_TEXT_MAX = 200
LEDGER_SCAN = 1000                       # newest ledger rows of one status looked at (a hit logs)
_TURN_KEY = re.compile(r"assistant:turn:([^:]+):([^:]+):\d+")


class ActionLedgerView(Protocol):
    """What `assistant_rejections` reads of the action ledger (`bot.actions.ActionLedger`)."""

    def list(self, user_id: str, *, status: str | None = None, limit: int = 100,
             writes: bool = False) -> list[Any]: ...


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= REJECT_TEXT_MAX else text[:REJECT_TEXT_MAX - 1] + "…"


def _turn_run(session_id: str, message_id: str) -> str:
    return f"assistant:{session_id}:{message_id}"      # the trace run id (assistant/loop.py)


def _draft_text(draft: dict[str, Any]) -> str:
    heading = str(draft.get("heading") or "").strip()
    what = "加一段" if draft.get("kind") == "add_section" else "改一段"
    return f"拒绝了助手的修改建议：{what}「{heading}」" if heading else "拒绝了助手的修改建议"


def assistant_rejections(sessions: SessionStore, ledger: ActionLedgerView | None, user_id: str,
                         since: datetime | None = None) -> list[tuple[Signal, str]]:
    """`assistant_rejected` signals of the user from `since` on (inclusive; None = all), each with
    the run id `assistant:{session}:{message}` of the turn that produced the thing, sorted by
    (at, signal_id). Derived at read time, nothing is stored. Two sources:
      (a) a session `decision` entry with status `rejected`: id `arej:<proposal_id>`, at = the
          decision, the turn = the proposal entry's `message_id`;
      (b) an assistant ledger row (`source == "assistant"`) in status `rejected` or `undone`: id
          `aact:<action_id>`, at = `decided_at` (every status change sets it), the turn = the
          session whose `tool` entry carries the row's card (`action.action_id`), else the
          durable step key `assistant:turn:<session>:<message>:<n>`. A row with no turn found
          gives no signal. `assistant_edit` rows are the session proposals (a) mirrored into the
          ledger: a rejected one is (a) already, so only an undone one (accepted, then undone)
          counts here.
    A `turn_error` is the system's failure, not a judgement of the answer: no signal.
    `ledger` None → source (b) is skipped."""
    out: list[tuple[Signal, str]] = []
    infos = {i.session_id: i for i in sessions.user_sessions(user_id)}
    card_turn: dict[str, tuple[str, str]] = {}           # action id -> (session, user message)
    for sid in infos:
        entries = sessions.entries(sid)
        for e in entries:
            card = e.payload.get("action") if e.type == "tool" else None
            if isinstance(card, dict) and isinstance(card.get("action_id"), str):
                card_turn.setdefault(card["action_id"], (sid, str(e.payload.get("message_id"))))
        for e in entries:
            if e.type != "decision" or e.payload.get("status") != "rejected":
                continue
            if since is not None and e.at < since:
                continue
            pid = str(e.payload.get("proposal_id"))
            try:
                _, prop = sessions.find_proposal(pid)
            except LookupError:
                continue
            draft = prop.payload.get("draft")
            draft = draft if isinstance(draft, dict) else {}
            page_id = draft.get("page_id")
            out.append((Signal(signal_id=f"{AREJ_PREFIX}{pid}", kind="assistant_rejected", at=e.at,
                               page_id=page_id if isinstance(page_id, str) else None,
                               text=_clip(_draft_text(draft))),
                        _turn_run(sid, str(prop.payload["message_id"]))))
    if ledger is not None:
        for status, verb in (("rejected", "拒绝"), ("undone", "撤销")):
            rows = ledger.list(user_id, status=status, limit=LEDGER_SCAN, writes=True)
            if len(rows) >= LEDGER_SCAN:
                log.warning("assistant rejections of %s: the ledger scan of %s rows hit its cap "
                            "of %d; older rows were not read", user_id, status, LEDGER_SCAN)
            for row in rows:
                if row.source != "assistant" or row.decided_at is None:
                    continue
                if since is not None and row.decided_at < since:
                    continue
                if row.kind == "assistant_edit" and status == "rejected":
                    continue                                 # source (a)
                turn = card_turn.get(row.action_id) or _key_turn(row.idempotency_key, infos)
                if turn is None and row.kind == "assistant_edit":
                    turn = _proposal_turn(sessions, row)
                if turn is None:
                    continue
                out.append((Signal(signal_id=f"{AACT_PREFIX}{row.action_id}",
                                   kind="assistant_rejected", at=row.decided_at,
                                   page_id=row.page_id,
                                   text=_clip(f"{verb}了助手的操作：{row.summary}")),
                            _turn_run(*turn)))
    out.sort(key=lambda p: (p[0].at, p[0].signal_id))
    return out


# ---- assistant ratings (derived, never stored) ----

ARAT_PREFIX = "arat:"
RATED_DOWN = "觉得这次回答不好"
RATED_DOWN_ASK = "觉得这次提问不好"


def _rating_text(p: dict[str, Any]) -> str:
    chips = "、".join(str(c) for c in p.get("chips") or ())
    note = str(p.get("note") or "").strip()
    asks = p.get("asks") or ()
    asked = [q for q in (str(a.get("question") or "").strip() for a in asks) if q]
    lead = RATED_DOWN_ASK if asks else RATED_DOWN
    head = f"{lead}：{chips}" if chips else lead
    if asked:      # 「问题：…吗？」 ends on its own mark: no 。 after it
        head += "。问题：" + " / ".join(asked)
    sep = "" if head.endswith(("。", "？", "！", "?", "!")) else "。"
    return _clip(f"{head}{sep}补充：{note}" if note else head)


def assistant_ratings(sessions: SessionStore, user_id: str,
                      since: datetime | None = None) -> list[tuple[Signal, str]]:
    """`assistant_rated` signals of the user from `since` on (inclusive, by the rating's time;
    None = all), each with the run id `assistant:{session}:{turn}` of the rated answer's turn,
    sorted by (at, signal_id). One per answer whose latest `rating` entry is "down" (read now: a
    rating later cleared or turned 👍 is no signal; a 👍 never is): id `arat:<answer message id>`,
    at = when the answer's rating last BECAME down (the first entry of its current run of down
    entries: re-editing the chips or note of a 👎 does not make it new), page = the session's
    page, text = 「觉得这次回答不好：<chips>。补充：<note>」 from the latest entry; a turn that
    ended on a question (the entry's `asks`: a question's 👎, its reason chips and note are the
    complaint about that question, read here, never copied elsewhere) says 「觉得这次提问不好：<chips>。问题：<question>。补充：<note>」."""
    out: list[tuple[Signal, str]] = []
    for info in sessions.user_sessions(user_id):
        latest: dict[str, Entry] = {}
        down_since: dict[str, datetime] = {}
        for e in sorted(sessions.entries(info.session_id), key=lambda x: x.seq):
            if e.type != "rating":
                continue
            mid = str(e.payload.get("message_id"))
            if e.payload.get("value") != "down":
                down_since.pop(mid, None)
            else:
                down_since.setdefault(mid, e.at)
            latest[mid] = e
        for mid, e in latest.items():
            at = down_since.get(mid)
            if at is None or (since is not None and at < since):
                continue
            out.append((Signal(signal_id=f"{ARAT_PREFIX}{mid}", kind="assistant_rated", at=at,
                               page_id=info.page_id, text=_rating_text(e.payload),
                               target_artifact_id=answer_artifact(user_id, mid)),
                        _turn_run(info.session_id, str(e.payload["turn_id"]))))
    out.sort(key=lambda p: (p[0].at, p[0].signal_id))
    return out


def assistant_feedback(sessions: SessionStore, ledger: ActionLedgerView | None, user_id: str,
                       since: datetime | None = None) -> list[tuple[Signal, str]]:
    """`assistant_rejections` + `assistant_ratings` (the assistant signals of a harness round),
    sorted by (at, signal_id); a turn with both appears under both, `harness.cases` makes it one
    case."""
    out = [*assistant_rejections(sessions, ledger, user_id, since),
           *assistant_ratings(sessions, user_id, since)]
    out.sort(key=lambda p: (p[0].at, p[0].signal_id))
    return out


def rated_answers(sessions: SessionStore, user_id: str) -> list[tuple[datetime, bool]]:
    """Every answer of the user with a current rating: (the answer's time, True for 👎), in no
    particular order; a cleared rating counts as none. Read by `harness.observe`
    (assistant_down_rate), which places an answer by when it was given, not when it was rated."""
    out: list[tuple[datetime, bool]] = []
    for info in sessions.user_sessions(user_id):
        entries = sessions.entries(info.session_id)
        rated = latest_ratings(entries)
        if not rated:
            continue
        for e in entries:
            r = rated.get(str(e.payload.get("message_id"))) if e.type == "assistant" else None
            if r is not None and r.payload.get("value") in ("up", "down"):
                out.append((e.at, r.payload["value"] == "down"))
    return out


def _key_turn(key: str, infos: dict[str, SessionInfo]) -> tuple[str, str] | None:
    m = _TURN_KEY.fullmatch(key)
    return (m[1], m[2]) if m is not None and m[1] in infos else None


def _proposal_turn(sessions: SessionStore, row: Any) -> tuple[str, str] | None:
    pid = (row.content or {}).get("proposal_id")
    try:
        info, prop = sessions.find_proposal(str(pid))
    except LookupError:
        return None
    return info.session_id, str(prop.payload["message_id"])


# ---- read API (R2 / R3) ----
#
# Paging follows each source's COMMIT order, never `at`: a flow stamps `at` when its request
# starts but may commit a minute later, so an `at`-ordered cursor would skip a signal that is
# committed after a reader's call with an `at` older than that reader's cursor.
#   stored signals      → their wiki_event seq (a commit's seqs are above every earlier commit's)
#   assistant_selection → the session store's insertion position (`SessionStore.user_selections`)
# The cursor is both positions: "w{seq}.a{pos}" (only [A-Za-z0-9.-], so it goes into a URL as
# is). "w0.a0" is the start. A page takes a prefix of each source; whatever it does not return
# lies after the cursor it hands out, so a signal committed after a call is returned by the next
# call from that call's cursor even if its `at` is older. Within one page the items are sorted
# by (at, signal_id) for readability only.

_CURSOR = re.compile(r"w(\d+)\.a([A-Za-z0-9-]+)")
START = "w0.a0"


class Cursor(NamedTuple):
    wiki_seq: int          # last stored-signal wiki_event seq covered
    asel: str              # last assistant-store position covered ("0" = none)


def cursor_of(c: Cursor) -> str:
    return f"w{c.wiki_seq}.a{c.asel}"


def parse_cursor(cursor: str) -> Cursor:
    """`"w{seq}.a{pos}"` → Cursor; anything else → WikiInvalid (422). The assistant position is
    checked by the session store when it is used."""
    m = _CURSOR.fullmatch(cursor)
    if m is None:
        raise WikiInvalid(f"无效的游标：{cursor}")
    return Cursor(int(m[1]), m[2])


def parse_kinds(kinds: str | Collection[str] | None) -> frozenset[str] | None:
    """None / "" → every kind; a comma-separated string or a collection; unknown → WikiInvalid."""
    if kinds is None:
        return None
    parts = [k.strip() for k in (kinds.split(",") if isinstance(kinds, str) else kinds)]
    parts = [k for k in parts if k]
    if not parts:
        return None
    bad = sorted(set(parts) - SIGNAL_KINDS)
    if bad:
        raise WikiInvalid(f"未知的信号类型：{', '.join(bad)}")
    return frozenset(parts)


# one source in commit order: (position, the signal, or None when it does not count — a
# selection entry with no selection text, or a kind outside the filter)
type _Stream = list[tuple[Any, Signal | None]]


def _streams(repo: Repo, user_id: str, sessions: SessionStore | None, start: Cursor,
             kinds: frozenset[str] | None) -> tuple[_Stream, _Stream]:
    raw = repo.wiki_events_since(user_id, (SIGNAL_EVENT,), start.wiki_seq)
    hidden = set()
    pool = getattr(repo, 'pool', None) or getattr(getattr(repo, 'inner', None), 'pool', None)
    if pool is not None:
        from evowiki.store.dreaming import sources_visible
        with pool.connection() as conn:
            hidden = {e.body.signal.signal_id for _, e in raw
                      if isinstance(e.body, SignalRecorded) and e.body.signal.kind == 'user_complaint'
                      and not sources_visible(conn, user_id, ['signal:' + e.body.signal.signal_id])}
    def keep(sig: Signal | None) -> Signal | None:
        return sig if sig is not None and sig.signal_id not in hidden and (kinds is None or sig.kind in kinds) else None

    wiki: _Stream = [(seq, keep(e.body.signal))
                     for seq, e in raw
                     if isinstance(e.body, SignalRecorded)]
    asel: _Stream = []
    if sessions is not None and (kinds is None or "assistant_selection" in kinds):
        try:
            rows = sessions.user_selections(user_id, after=start.asel)
        except ValueError:
            raise WikiInvalid(f"无效的游标：{cursor_of(start)}") from None
        if rows:
            state = repo.load(user_id).state
            asel = [(pos, keep(selection_signal(state, info, entry)))
                    for pos, info, entry in rows]
    return wiki, asel


def _key(sig: Signal) -> tuple[datetime, str]:
    return sig.at, sig.signal_id


def all_signals(repo: Repo, user_id: str, *, sessions: SessionStore | None = None,
                kinds: frozenset[str] | None = None) -> list[Signal]:
    """Every signal of the user (stored + derived), ordered by (at, signal_id)."""
    wiki, asel = _streams(repo, user_id, sessions, parse_cursor(START), kinds)
    return sorted((s for _, s in [*wiki, *asel] if s is not None), key=_key)


def _advance(stream: _Stream, taken: int, wanted: int, pos: Any) -> Any:
    """The source's new position: the last returned item's, or — once every counting item is
    returned — the last item scanned (skipping the ones that do not count)."""
    counting = [p for p, s in stream if s is not None]
    if taken < wanted:
        return counting[taken - 1] if taken else pos
    return stream[-1][0] if stream else pos


def list_signals(repo: Repo, user_id: str, *, sessions: SessionStore | None = None,
                 after: str | None = None, limit: int = DEFAULT_SIGNAL_LIMIT,
                 kinds: str | Collection[str] | None = None) -> SignalsOut:
    """One page of the user's signals committed after the cursor `after` (the `cursor` / `next`
    of an earlier call; None → from the start), paged by commit order (see above).

    `items`: at most `limit` signals, sorted by (at, signal_id). `cursor`: always set — pass it
    as `after` to continue, or later to poll for signals committed since. `next`: the same
    cursor while more signals are already waiting, None once both sources are exhausted.
    A cursor belongs to the `kinds` filter it was made with (it moves past signals of other
    kinds). `sessions`: the assistant session store (None → no assistant_selection signals and
    the cursor's assistant position is kept as given). limit 1..500; a bad cursor / limit /
    kind → WikiInvalid (422); unknown user → LookupError (404)."""
    if not 1 <= limit <= MAX_SIGNAL_LIMIT:
        raise WikiInvalid(f"limit 要在 1 到 {MAX_SIGNAL_LIMIT} 之间")
    wanted = parse_kinds(kinds)
    start = parse_cursor(after) if after else parse_cursor(START)
    wiki, asel = _streams(repo, user_id, sessions, start, wanted)
    w_items = [s for _, s in wiki if s is not None]
    a_items = [s for _, s in asel if s is not None]
    wi = ai = 0
    while wi + ai < limit and (wi < len(w_items) or ai < len(a_items)):
        if ai >= len(a_items) or (wi < len(w_items) and _key(w_items[wi]) <= _key(a_items[ai])):
            wi += 1
        else:
            ai += 1
    end = Cursor(_advance(wiki, wi, len(w_items), start.wiki_seq),
                 _advance(asel, ai, len(a_items), start.asel))
    more = wi < len(w_items) or ai < len(a_items)
    page = sorted([*w_items[:wi], *a_items[:ai]], key=_key)
    token = cursor_of(end)
    return SignalsOut(items=page, cursor=token, next=token if more else None)
