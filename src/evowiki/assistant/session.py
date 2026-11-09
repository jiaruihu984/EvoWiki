"""Reading assistant session log (plan rev 3.2 §9.1 point 5): append-only, replayable.

A session is a header (`SessionInfo`) plus a dense, ordered list of `Entry`s (seq 1, 2, ...).
Nothing is ever updated: state is `fold(info, entries)`, a pure function, so replaying the log
rebuilds exactly what the live run saw. Short-term memory only — K, notes and the wiki never
read it.

Payloads are plain JSON (pydantic values go in as `model_dump(mode="json")`). Per type:
  user        {"message_id", "text", "selection": {"text", "heading"} | None,
               "attachments": [source_id...] (optional; ids only, never content),
               "resumes": call_id (optional: the message resumed the turn that ended on that
               question)}
  tool        {"message_id", "call_id", "name", "effect", "args", "ok", "content",
               "sources": [Source...], "action": ActionCard? }   content cut to 2000 chars, audit only;
               `action` = the ledger card the tool wrote (replayed with GET session)
  assistant   {"message_id", "text", "sources": [Source...], "proposals": [proposal_id...],
               "actions": [action_id...], "tool_calls": int, "llm_calls": int,
               "pending_ops": [PendingOp...] (optional: what the next message may confirm),
               "skills": {"carry": [key...], "refs": [path...], "versions": {..}} (optional),
               "reads": {source_id: AttachmentRead} (optional: how the turn's attachments were
               given to the model), "recalled": [{"id", "kind", "content_hash"}...] and
               "retrieval_version" (with cross-session memory: what the turn recalled),
               "paused": PendingAsk (optional: the turn ended on a question to the learner; his
               next message resumes it)}
  proposal    {"proposal_id", "message_id", "draft": ProposalDraft}
  decision    {"proposal_id", "status": "accepted" | "rejected" | "failed", "applied": [...],
               "detail"}
  observed    {"message_id", "observations": int, "changes": [CellChange...]}
  compaction  compact@v7: {"version": "compact@v7", "handoff", "learning", "edits",
               "dropped" (optional: learning items whose evidence did not check), "first_kept":
               message_id | None, "omitted": int (optional), "reason": "summary_failed" (with
               omitted)} (assistant/summary.py); before v7: {"summary", "fields"?, "first_kept",
               ...}, read back as text only
              first_kept = the user message_id of the first turn kept; None = every turn so far
              is folded. omitted > 0 = that many old turns were dropped without a new summary
              (it failed): the summary stays the previous one and the chat shows a notice.
  turn_error  {"message_id", "code", "detail"}
  (an assistant entry with "stopped": true is an answer he stopped: `text` is what was out by then,
  maybe "". It stays in this session's history; it goes to no memory: assistant.loop)
  rating      {"message_id", "turn_id", "value": "up" | "down" | None, "chips": [RatingChip...],
               "note", "asks": [{"call_id", "question"}...] (only when the turn ended on a
               question)}   the
               learner's 👍 / 👎 on one answer (migration 0072): message_id = the
               assistant entry's own message_id, turn_id = the user message of its turn (the
               trace run is `assistant:{session}:{turn_id}`), asks = the ask_user cards that turn
               ended on (their call id and question), so a 👎 on a question names it; the one
               record of a question's rating (the harness reads it at read time). Appended at any time, also while a
               later turn runs; the latest rating entry of an answer wins, value None clears it.
  consolidation {"through_message_id", "item_ids": [...], "dropped": [[kind, reason]...],
               "pending_landing": [Candidate...]} | {"landed": [candidate_key...], "refused":
               [[candidate_key, reason]...]}   cross-session memory (memory/consolidate.py,
               migration 0081): what one consolidation took from turns up to through_message_id,
               or what a later retry of its pending preferences did. Not folded.
  wrapup_failed {"version": int, "attempt": int, "reason": "budget" | "failed" | "postponed",
               "error": exception class}   a background wrap-up of this session did not finish
               (assistant/wrapup.py, migration 0110): the next attempt's workflow id counts these.
               Not folded; holds no words of the session.

`message_id` in tool / proposal / observed / turn_error is the user message of the turn; an
assistant entry carries its own message_id and belongs to the latest turn.

A question to the learner (ask_user, a pausing tool) ends its turn: the tool entry carries the
card (`ask`), the assistant entry what the next message needs to resume the run (`paused`). The
session's pending question (`SessionState.pending`) is the last turn's, and only until the next
user entry: any message clears it (an answer resumes, anything else is a new request)."""

import json
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from evowiki.agent.tools import ActionCard, Pause, ProposalDraft, Source, ToolResult
from evowiki.assistant.contracts import (
    DECISION_INTERNAL,
    RATING_NOTE_MAX,
    AskCardOut,
    AttachmentOut,
    AttachmentRead,
    MessageOut,
    ProposalOut,
    ProposalStateOut,
    RatingIn,
    RatingOut,
    Selection,
    SessionOut,
    SessionRecapOut,
    ToolCallOut,
)
from evowiki.assistant.status_line import StatusNames
from evowiki.assistant.summary import Recap, read_recap, render_handoff
from evowiki.assistant.tool_rows import HIDDEN_ROWS, QUIET_ROWS, done_row
from evowiki.llm.chat import ChatMessage, ToolCall

OMITTED_TEXT = "更早的 {n} 轮对话已省略（摘要生成失败）"

EntryType = Literal["user", "tool", "assistant", "proposal", "decision", "observed",
                    "compaction", "turn_error", "rating", "consolidation", "wrapup_failed"]


class Entry(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    seq: int = Field(ge=1)
    entry_id: str
    type: EntryType
    payload: dict[str, Any]
    at: AwareDatetime


class SessionInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    user_id: str
    page_id: str | None
    note_concept_id: str | None
    created_at: AwareDatetime


# 读完了's 内容太简单 / 理解有困难: the plugin sends the tap as the learner's message in a new
# conversation on the page (WIKI_COPY endSendShallow / endSendHard), with or without a selection
PAGE_TAPS = ("这页内容太简单", "这页理解有困难")


class EntryConflict(RuntimeError):
    """`append` of an `entry_id` that is already stored with a different type or payload (or in
    another session). The same id with the same type and payload is a retry, not a conflict."""


def same_entry(e: "Entry", session_id: str, type: str, payload: dict[str, Any]) -> bool:
    """A stored entry is what `append(session_id, e.entry_id, type, payload)` would have written."""
    return (e.session_id == session_id and e.type == type
            and e.payload == json.loads(json.dumps(payload)))


def session_version(entries: "Sequence[Entry]") -> int:
    """What a session's conversation has reached: the seq of its last user message, answer or
    failed turn (0: none). A summary or a memory written about it does not move it."""
    return max((e.seq for e in entries if e.type in ("user", "assistant", "turn_error")),
               default=0)


class SessionStore(Protocol):
    def create(self, info: SessionInfo) -> None: ...

    def info(self, session_id: str) -> SessionInfo:
        """Raises LookupError for an unknown session."""
        ...

    def append(self, session_id: str, entry_id: str, type: EntryType, payload: dict[str, Any],
               at: datetime) -> Entry:
        """Appends at seq = last + 1. Raises LookupError for an unknown session. Idempotent on
        `entry_id`: the same id with the same type and payload returns the stored entry (no new
        row); the same id with a different type / payload raises EntryConflict."""
        ...

    def entries(self, session_id: str) -> list[Entry]: ...

    def append_if_current(self, session_id: str, entry_id: str, type: EntryType,
                          payload: dict[str, Any], at: datetime, *, version: int) -> bool:
        """Append unless the session moved on while the caller worked from it: False when its
        `session_version` is no longer `version`, or anything of it is being forgotten. The check
        and the insert are one transaction under the session's locks."""
        ...

    def request_stop(self, session_id: str, message_id: str, at: datetime) -> None:
        """He pressed stop on this turn (idempotent)."""
        ...

    def stop_requested(self, session_id: str, message_id: str) -> bool: ...

    def find_proposal(self, proposal_id: str) -> tuple[SessionInfo, Entry]:
        """The session and proposal entry for `proposal_id`; LookupError if there is none."""
        ...

    def find_message(self, session_id: str, message_id: str) -> tuple[SessionInfo, Entry] | None:
        """The `user` or `assistant` entry of `session_id` whose payload message_id is
        `message_id`, with its session; None when there is none (or no such session)."""
        ...

    def user_sessions(self, user_id: str) -> list[SessionInfo]:
        """This user's sessions that have at least one `user` entry, in no particular order."""
        ...

    def recent_sessions(self, user_id: str, *, limit: int, page_id: str | None = None,
                        exclude: str | None = None) -> list[SessionInfo]:
        """This user's sessions, newest first by (created_at, session_id) descending; `page_id`
        given -> only that page's; `exclude` -> that session id left out."""
        ...

    def user_selections(self, user_id: str,
                        after: str | None = None) -> list[tuple[str, SessionInfo, Entry]]:
        """Every `user` entry of this user's sessions that carries a selection (the quote sent
        with "ask") stored after position `after` (None / "0" → from the start), as
        (position, session, entry) in the store's insertion (commit) order: an entry stored
        after a reader's call gets a position above every entry that call returned, whatever
        its `at`. Positions are opaque, URL-safe ([A-Za-z0-9-]) and only meaningful to the
        store that issued them; a malformed `after` → ValueError. Read by `wikiapp.signals`
        (assistant_selection)."""
        ...


class MemorySessionStore:
    """Dict-backed `SessionStore` with the same constraints as the Postgres tables (unique
    session id, entry id and proposal id; payloads must round-trip through JSON)."""

    def __init__(self) -> None:
        self._infos: dict[str, SessionInfo] = {}
        self._entries: dict[str, list[Entry]] = {}
        self._entry_ids: set[str] = set()
        self._proposals: dict[str, Entry] = {}
        self._order: list[Entry] = []        # every entry in insertion order (position = index+1)
        self._stops: set[tuple[str, str]] = set()
        from evowiki.store.artifacts import MemoryArtifacts
        self.artifacts = MemoryArtifacts()   # answers and compactions saved (lineage, 0091)

    def create(self, info: SessionInfo) -> None:
        if info.session_id in self._infos:
            raise ValueError(f"session {info.session_id} already exists")
        self._infos[info.session_id] = info
        self._entries[info.session_id] = []

    def info(self, session_id: str) -> SessionInfo:
        try:
            return self._infos[session_id]
        except KeyError:
            raise LookupError(f"unknown assistant session {session_id}") from None

    def append_decision_once(self, session_id: str, entry_id: str, payload: dict[str, Any],
                             at: datetime) -> bool:
        if any(e.type == "decision" and e.payload.get("proposal_id") == payload["proposal_id"]
               for e in self._entries.get(session_id, ())):
            return False
        self.append(session_id, entry_id, "decision", payload, at)
        return True

    def append(self, session_id: str, entry_id: str, type: EntryType, payload: dict[str, Any],
               at: datetime) -> Entry:
        log = self._entries.get(session_id)
        if log is None:
            raise LookupError(f"unknown assistant session {session_id}")
        if entry_id in self._entry_ids:
            stored = next(e for e in self._order if e.entry_id == entry_id)
            if same_entry(stored, session_id, type, payload):
                return stored
            raise EntryConflict(f"entry id {entry_id} is already stored with other content")
        entry = Entry(session_id=session_id, seq=len(log) + 1, entry_id=entry_id, type=type,
                      payload=json.loads(json.dumps(payload)), at=at)
        if type == "proposal":
            pid = entry.payload.get("proposal_id")
            if pid in self._proposals:
                raise ValueError(f"duplicate proposal id {pid}")
            self._proposals[pid] = entry
        prev = next((e for e in reversed(log) if e.type == "compaction"), None)
        log.append(entry)
        self._order.append(entry)
        self._entry_ids.add(entry_id)
        if type in ("assistant", "compaction"):      # artifacts (0091), as PgSessionStore does
            from evowiki.assistant.lineage import entry_lineage
            self.artifacts.put(entry_lineage(self._infos[session_id].user_id, entry,
                                             prev if type == "compaction" else None))
        return entry

    def append_if_current(self, session_id: str, entry_id: str, type: EntryType,
                          payload: dict[str, Any], at: datetime, *, version: int) -> bool:
        if session_version(self.entries(session_id)) != version:
            return False
        self.append(session_id, entry_id, type, payload, at)
        return True

    def request_stop(self, session_id: str, message_id: str, at: datetime) -> None:
        self._stops.add((session_id, message_id))

    def stop_requested(self, session_id: str, message_id: str) -> bool:
        return (session_id, message_id) in self._stops

    def entries(self, session_id: str) -> list[Entry]:
        return list(self._entries.get(session_id, ()))

    def find_proposal(self, proposal_id: str) -> tuple[SessionInfo, Entry]:
        entry = self._proposals.get(proposal_id)
        if entry is None:
            raise LookupError(f"unknown proposal {proposal_id}")
        return self._infos[entry.session_id], entry

    def find_message(self, session_id: str, message_id: str) -> tuple[SessionInfo, Entry] | None:
        for e in self._entries.get(session_id, ()):
            if e.type in ("user", "assistant") and e.payload.get("message_id") == message_id:
                return self._infos[session_id], e
        return None

    def user_sessions(self, user_id: str) -> list[SessionInfo]:
        return [i for sid, i in self._infos.items() if i.user_id == user_id
                and any(e.type == "user" for e in self._entries[sid])]

    def recent_sessions(self, user_id: str, *, limit: int, page_id: str | None = None,
                        exclude: str | None = None) -> list[SessionInfo]:
        mine = [i for i in self._infos.values()
                if i.user_id == user_id and (page_id is None or i.page_id == page_id)
                and i.session_id != exclude]
        mine.sort(key=lambda i: (i.created_at, i.session_id), reverse=True)
        return mine[:limit]

    def user_selections(self, user_id: str,
                        after: str | None = None) -> list[tuple[str, SessionInfo, Entry]]:
        """Position = the entry's 1-based insertion index over the whole store."""
        if after is None:
            after = "0"
        if not after.isascii() or not after.isdigit():
            raise ValueError(f"bad assistant position {after!r}")
        start = int(after)
        out: list[tuple[str, SessionInfo, Entry]] = []
        for pos, e in enumerate(self._order[start:], start=start + 1):
            info = self._infos[e.session_id]
            if (info.user_id == user_id and e.type == "user"
                    and (e.payload.get("selection") or e.payload.get("text") in PAGE_TAPS)):
                out.append((str(pos), info, e))
        return out


# ---- fold ----

class PendingAsk(BaseModel):
    """A turn that ended on a question to the learner (agent.loop stop "paused"): what his next
    message needs to resume the run. `message_id`: the user message the run answers (a resumed
    run that asks again keeps it); `messages`: the run's own messages (that turn's recall message
    if any, his message, what followed), the paused call's result a placeholder
    (agent.loop.PLACEHOLDER), his message without its attachment parts (the resumed turn places
    them again)."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    call: ToolCall
    pause: Pause
    messages: tuple[ChatMessage, ...] = Field(min_length=1)


class Turn(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str                      # the user message
    user_text: str
    selection: dict | None
    answer: str | None                   # None: no answer (failed turn, or still running)
    edits: tuple[str, ...] = ()          # page edits raised in this turn and accepted (for
                                         # the compaction summary's 改过哪里), in decision order
    attachments: tuple[str, ...] = ()    # source ids sent with the question
    reads: dict[str, Any] = {}           # source_id -> AttachmentRead (json) of this turn
    # the questions the turn ended on (ask_user), so later turns' history keeps them; his answer,
    # if any, is the next turn's message
    asks: tuple[str, ...] = ()
    # the turn ended on a question: what the next message resumes (None: it did not)
    paused: PendingAsk | None = None
    # the ops its answer proposed (assistant.pending.PendingOp as JSON), what the learner's next
    # message may confirm
    pending_ops: tuple[dict[str, Any], ...] = ()
    stopped: bool = False                # he stopped the answer: `answer` is what was out by then


class SessionState(BaseModel):
    model_config = ConfigDict(frozen=True)

    info: SessionInfo
    turns: tuple[Turn, ...]
    summary: str
    first_kept: int                      # index into `turns`; 0 when never compacted
    messages: tuple[MessageOut, ...]
    decisions: dict[str, str]            # proposal_id -> latest "accepted" | "rejected"
    latest: dict[str, ProposalStateOut] = {}  # proposal_id -> latest decision, failed included
    proposals: tuple[ProposalOut, ...] = ()   # every proposal raised, in raise order
    recap: Recap | None = None           # the latest compaction (summary = render_handoff(recap))
    actions: tuple[ActionCard, ...] = ()      # every action card the tools wrote, in order (latest
    #                                           version of a card that was written twice)
    # the Skills the last answer kept active and their references (assistant.skills.carry); a
    # compaction after it clears both (the task is over for the folded history)
    skills: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    # ledger rows a quiet-row tool wrote (remember_about_me: no card in the chat), in order
    quiet_actions: tuple[ActionCard, ...] = ()

    @property
    def pending(self) -> PendingAsk | None:
        """The question the session's last turn ended on, until his next message."""
        return self.turns[-1].paused if self.turns else None


_CJK = re.compile("[\\u4e00-\\u9fff]")  # CJK unified ideographs, as web copy.ts failedText


def decision_state(p: dict[str, Any]) -> ProposalStateOut:
    """A decision entry as the UI sees it. A failed detail without Chinese is an old log's raw
    exception text (before details were only ApplyRefused reasons) -> the generic code."""
    detail = p.get("detail", "")
    if p["status"] == "failed" and not _CJK.search(detail):
        detail = DECISION_INTERNAL
    return ProposalStateOut(proposal_id=p["proposal_id"], status=p["status"],
                            applied=tuple(p.get("applied", ())), detail=detail)


def _tool_call(p: dict[str, Any], *, language: str = "zh") -> ToolCallOut | None:
    """A tool entry as the restored turn shows it; an unknown tool (no effect) showed nothing live
    and shows nothing here. The summary is the live one (logged with the entry); an older entry's is
    the query / url, else the title of the single source the call returned."""
    effect = p.get("effect")
    if p.get("ask") or p.get("resumed"):  # a question to the learner: its card, not a row
        return None                       # (`asks`); his answer: his next message
    if p.get("name") in HIDDEN_ROWS:    # an internal step, never shown (loop._translate)
        return None
    if p.get("name") in QUIET_ROWS:     # shown as a quiet row, like a read (loop._translate)
        effect = "read"
    if effect not in ("read", "network", "propose"):
        return None
    sources = tuple(Source.model_validate(s) for s in p.get("sources", ()))
    stored = p.get("summary")
    if isinstance(stored, str):     # the live line's subject, as logged (`loop.tool_summary`)
        summary = stored
    else:                           # an older log: query / url, else the single source's title
        args = p.get("args")
        value = (args.get("query") or args.get("url") or "") if isinstance(args, dict) else ""
        summary = value if isinstance(value, str) else str(value)
        if not summary and len(sources) == 1:
            summary = sources[0].title
    ok = bool(p.get("ok"))
    if effect == "propose":
        return ToolCallOut(call_id=p["call_id"], name=p["name"], effect=effect, summary=summary,
                           ok=ok, sources=sources)
    args = p.get("args") if isinstance(p.get("args"), dict) else {}
    # a quiet row's ledger card (remember_about_me) is logged as `quiet_action`: its label depends
    # on it (kept / waiting for his yes / used this time only), as live (loop._translate)
    quiet = p.get("quiet_action")
    row = done_row(p["name"], args, ToolResult(
        content="", sources=sources, error=None if ok else "failed",
        action=ActionCard.model_validate(quiet) if isinstance(quiet, dict) else None), StatusNames(language=language))
    return ToolCallOut(call_id=p["call_id"], name=p["name"], effect=effect, summary=summary,
                       ok=ok, sources=sources, label=row.label, group=row.group,
                       group_label=row.group_label, items=row.items[:20], refs=row.refs)


def _edit_label(draft: dict[str, Any]) -> str:
    heading = draft.get("heading") or "?"
    if draft.get("kind") == "rewrite_section":
        return f"改写了小节「{heading}」"
    return f"加了小节「{heading}」"


def fold(info: SessionInfo, entries: Sequence[Entry], *, language: str = "zh") -> SessionState:
    """Rebuild session state from its log. Pure. Raises ValueError on a log that breaks the
    invariants above (assistant entry before any user entry, compaction to an unknown turn)."""
    turns: list[Turn] = []
    messages: list[MessageOut] = []
    pending: list[str] = []              # proposal ids raised in the current turn
    pending_actions: list[str] = []      # action ids the current turn's tools wrote
    cards: dict[str, ActionCard] = {}    # action id -> its card, insertion = first written
    calls: list[ToolCallOut] = []        # tool calls made in the current turn
    asks: list[AskCardOut] = []              # questions the current turn asked the learner
    waiting: int | None = None           # the message whose question waits for the next message
    summary, first_kept = "", 0
    recap: Recap | None = None
    decisions: dict[str, str] = {}
    latest: dict[str, ProposalStateOut] = {}
    drafts: list[ProposalOut] = []
    raised: dict[str, tuple[int, str]] = {}   # proposal_id -> (turn index, edit label)
    ratings: dict[str, RatingOut | None] = {}  # answer message id -> its latest rating
    skills: tuple[str, ...] = ()         # the last answer's carried Skills
    quiet: list[ActionCard] = []
    skill_refs: tuple[str, ...] = ()
    for e in sorted(entries, key=lambda x: x.seq):
        p = e.payload
        if e.type == "user":
            if waiting is not None:          # any message answers it (or drops it quietly)
                m = messages[waiting]
                messages[waiting] = m.model_copy(update={"asks": tuple(
                    a.model_copy(update={"status": "answered"}) for a in m.asks)})
                waiting = None
            turns.append(Turn(message_id=p["message_id"], user_text=p["text"],
                              selection=p.get("selection"), answer=None,
                              attachments=tuple(p.get("attachments") or ())))
            sel = p.get("selection")
            messages.append(MessageOut(message_id=p["message_id"], role="user", text=p["text"],
                                       selection=Selection.model_validate(sel) if sel else None,
                                       intent=p.get("intent"), draft_id=p.get("draft_id")))
            pending, pending_actions, calls, asks = [], [], [], []
        elif e.type == "tool":
            if (call := _tool_call(p, language=language)) is not None:
                calls.append(call)
            if p.get("ask"):
                ask = p["ask"]
                asks.append(AskCardOut(call_id=p["call_id"], status="waiting",
                                       kind=ask["kind"], payload=ask["payload"]))
            if p.get("quiet_action"):
                quiet.append(ActionCard.model_validate(p["quiet_action"]))
            if p.get("action"):
                card = ActionCard.model_validate(p["action"])
                cards[card.action_id] = card
                if card.action_id not in pending_actions:
                    pending_actions.append(card.action_id)
        elif e.type == "proposal":
            pending.append(p["proposal_id"])
            drafts.append(ProposalOut(proposal_id=p["proposal_id"],
                                      draft=ProposalDraft.model_validate(p["draft"]),
                                      change=p.get("change")))
            if turns:
                raised[p["proposal_id"]] = (len(turns) - 1, _edit_label(p.get("draft") or {}))
        elif e.type == "assistant":
            if not turns:
                raise ValueError(f"assistant entry {e.entry_id} before any user entry")
            turns[-1] = turns[-1].model_copy(update={
                "answer": p["text"], "reads": p.get("reads") or {},
                "asks": tuple(str(a.payload.get("question", "")) for a in asks),
                "paused": PendingAsk.model_validate(p["paused"]) if p.get("paused") else None,
                "pending_ops": tuple(p.get("pending_ops") or ()),
                "stopped": bool(p.get("stopped"))})
            if asks:
                waiting = len(messages)
            kept = p.get("skills") or {}
            skills = tuple(kept.get("carry") or ())
            skill_refs = tuple(kept.get("refs") or ())
            proposals = p.get("proposals", pending)
            messages.append(MessageOut(
                message_id=p["message_id"], role="assistant", text=p["text"],
                sources=tuple(Source.model_validate(s) for s in p.get("sources", ())),
                proposals=tuple(proposals), tools=tuple(calls),
                actions=tuple(p.get("actions", pending_actions)),
                asks=tuple(asks), card_at=p.get("card_at"),
                stopped=bool(p.get("stopped"))))
            pending, pending_actions, calls, asks = [], [], [], []
        elif e.type == "compaction":
            kept = p.get("first_kept")
            if kept is None:
                idx = len(turns)             # every turn so far is folded into the summary
            else:
                idx = next((i for i, t in enumerate(turns) if t.message_id == kept), None)
                if idx is None:
                    raise ValueError(f"compaction {e.entry_id} keeps unknown turn {kept}")
            recap = read_recap(p)
            summary, first_kept = render_handoff(recap), idx
            skills, skill_refs = (), ()
            if p.get("omitted"):
                messages.append(MessageOut(message_id=f"omit_{e.entry_id}", role="assistant",
                                           text=OMITTED_TEXT.format(n=p["omitted"])))
        elif e.type == "decision":
            if p["status"] in ("accepted", "rejected"):
                decisions[p["proposal_id"]] = p["status"]
            latest[p["proposal_id"]] = decision_state(p)
            hit = raised.pop(p["proposal_id"], None) if p["status"] == "accepted" else None
            if hit is not None:
                i, label = hit
                turns[i] = turns[i].model_copy(update={"edits": (*turns[i].edits, label)})
        elif e.type == "rating":
            ratings[p["message_id"]] = rating_out(p)
        # observed / turn_error: audit only; a failed turn keeps answer=None (and its tools)
    if any(ratings.values()):
        messages = [m.model_copy(update={"rating": ratings[m.message_id]})
                    if m.role == "assistant" and ratings.get(m.message_id) is not None else m
                    for m in messages]
    return SessionState(info=info, turns=tuple(turns), summary=summary, first_kept=first_kept,
                        messages=tuple(messages), decisions=decisions, latest=latest,
                        proposals=tuple(drafts), recap=recap,
                        actions=tuple(cards.values()),
                        skills=skills, skill_refs=skill_refs, quiet_actions=tuple(quiet))


_TRAILING_Q = re.compile("[？?。]$")
TITLE_MAX = 24


def conversation_title(text: str) -> str:
    """History-list title of a conversation: the first line of its first user text, stripped,
    one trailing ？ / ? / 。 dropped, cut to 24 characters (the plugin's `titleOf`). A goal sent
    from 学点新的 carries a 背景 line after the goal: the title is the goal."""
    first = text.strip().split("\n", 1)[0].strip()
    return _TRAILING_Q.sub("", first, count=1)[:TITLE_MAX]


Describe = Callable[[Sequence[str]], dict[str, AttachmentOut]]


def recap_out(recap: Recap | None) -> SessionRecapOut | None:
    """What the learner's 更早的对话 fold shows of the latest compaction: the checked learning
    items and the accepted edits as plain items (the client words them); never the
    hand-over text written for the model. None: nothing to show (no compaction, an entry before
    compact@v7, or an empty one)."""
    if recap is None or recap.legacy:
        return None
    out = SessionRecapOut(
        understood=tuple(e.concept for e in recap.learning.understood),
        unclear=tuple(e.concept for e in recap.learning.unclear),
        edits=recap.edits)
    return out if any((out.understood, out.unclear, out.edits)) else None


def session_out(state: SessionState, describe: Describe | None = None) -> SessionOut:
    """`describe(ids)` -> the attachments as cards (source_id -> AttachmentOut without `read`);
    needed when a user message carries attachments (none given then → ValueError: fail closed
    rather than drop them)."""
    messages = state.messages
    turns = {t.message_id: t for t in state.turns if t.attachments}
    if turns:
        if describe is None:
            raise ValueError("session has attachments but no way to describe them")
        cards = describe(list(dict.fromkeys(i for t in turns.values() for i in t.attachments)))
        out: list[MessageOut] = []
        for m in messages:
            t = turns.get(m.message_id) if m.role == "user" else None
            if t is not None:
                m = m.model_copy(update={"attachments": tuple(
                    cards[i].model_copy(update={"read": (
                        AttachmentRead.model_validate(t.reads[i]) if i in t.reads else None)})
                    for i in t.attachments)})
            out.append(m)
        messages = tuple(out)
    return SessionOut(session_id=state.info.session_id, page_id=state.info.page_id,
                      recap=recap_out(state.recap), messages=messages,
                      decisions=tuple(state.latest.values()), proposals=state.proposals,
                      actions=state.actions)


# ---- ratings (👍 / 👎 on an answer) ----

def rating_out(p: dict[str, Any]) -> RatingOut | None:
    """A rating entry's payload as the UI sees it; None when it clears the rating."""
    if p.get("value") not in ("up", "down"):
        return None
    return RatingOut(message_id=p["message_id"], value=p["value"],
                     chips=tuple(p.get("chips") or ()), note=str(p.get("note") or ""))


def answer_turn(entries: Sequence[Entry], message_id: str) -> str:
    """The user message id of the turn whose assistant answer is `message_id`; LookupError when
    no assistant entry carries that id (an unknown id, a user message, a notice)."""
    turn: str | None = None
    for e in sorted(entries, key=lambda x: x.seq):
        if e.type == "user":
            turn = str(e.payload["message_id"])
        elif e.type == "assistant" and e.payload.get("message_id") == message_id:
            if turn is None:
                break
            return turn
    raise LookupError(f"unknown assistant answer {message_id}")


def turn_asks(entries: Sequence[Entry], turn_id: str) -> list[dict[str, str]]:
    """The questions (ask_user cards) the turn `turn_id` asked the learner, in order, as
    {"call_id", "question"}; [] for none."""
    return [{"call_id": str(e.payload["call_id"]),
             "question": str(e.payload["ask"]["payload"].get("question", ""))}
            for e in sorted(entries, key=lambda x: x.seq)
            if e.type == "tool" and e.payload.get("message_id") == turn_id and e.payload.get("ask")]


def rating_payload(message_id: str, turn_id: str, body: RatingIn,
                   asks: Sequence[dict[str, str]] = ()) -> dict[str, Any]:
    """The `rating` entry for `body` (RatingIn has checked its shape): duplicate chips dropped,
    the note stripped and clipped to RATING_NOTE_MAX, both empty on a clear. `asks`: the
    questions of a turn that ended on one (`turn_asks`, stored with the rating so it names them)."""
    down = body.value == "down"
    note = " ".join(body.note.split())[:RATING_NOTE_MAX] if body.value is not None else ""
    out: dict[str, Any] = {"message_id": message_id, "turn_id": turn_id, "value": body.value,
                           "chips": list(dict.fromkeys(body.chips)) if down else [], "note": note}
    if asks:
        out["asks"] = [dict(a) for a in asks]
    return out


def latest_ratings(entries: Sequence[Entry]) -> dict[str, Entry]:
    """Answer message id -> its latest `rating` entry (by seq; a clear included)."""
    out: dict[str, Entry] = {}
    for e in sorted(entries, key=lambda x: x.seq):
        if e.type == "rating":
            out[str(e.payload.get("message_id"))] = e
    return out


def log_decision(store: SessionStore, session_id: str, entry_id: str, payload: dict[str, Any],
                 at: datetime, *, once: bool = False) -> bool:
    """Append the `decision` entry of a proposal. `once`: skip when the session already holds a
    decision for it, atomically when the store can (`append_decision_once`: Postgres checks and
    inserts under the session row lock). True when appended."""
    if once:
        atomic = getattr(store, "append_decision_once", None)
        if atomic is not None:
            return atomic(session_id, entry_id, payload, at)
        if any(e.type == "decision" and e.payload.get("proposal_id") == payload["proposal_id"]
               for e in store.entries(session_id)):
            return False
    store.append(session_id, entry_id, "decision", payload, at)
    return True
