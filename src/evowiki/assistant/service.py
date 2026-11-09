"""The reading assistant as one service object (plan rev 3.2 §9.1): sessions, turns and
proposal decisions over `run_turn`, for the `/assistant/*` routes.

Every entry point checks ownership before doing anything (unknown session or proposal ->
LookupError, someone else's -> PermissionError), so the routes answer 404 / 403 instead of
failing inside a stream. One turn or decision at a time per session: a second one while the
session's lock is held raises `SessionBusy` (HTTP 409) rather than waiting.

Locks are in-process; one service process per deployment (v1, localhost)."""

import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Any

from evowiki.agent.tools import ProposalDraft, Tool
from evowiki.assistant.attachments import AttachmentGone, attachment_out, ensure_usable
from evowiki.assistant.contracts import (
    DECISION_INTERNAL,
    DETAIL_MAX,
    AssistantEvent,
    AttachmentOut,
    KnownAnswerOut,
    KnownItemOut,
    MessageIn,
    ProposalDecisionOut,
    ProposalStateOut,
    RatingIn,
    RatingOut,
    SessionCreateIn,
    SessionCreateOut,
    SessionListOut,
    SessionOut,
    SessionSummaryOut,
)
from evowiki.assistant.loop import TurnDeps, turn_tools_of
from evowiki.assistant.ports import ProposalApplier
from evowiki.assistant.session import (
    SessionInfo,
    answer_turn,
    conversation_title,
    decision_state,
    fold,
    log_decision,
    rating_out,
    rating_payload,
    session_out,
    turn_asks,
)
from evowiki.assistant.turn_runs import (
    AssistantUnavailable,
    DbosTurnRuns,
    InlineTurnRuns,
    TurnJob,
    TurnRuns,
)
from evowiki.assistant.wiki import ApplyRefused
from evowiki.llm.usage import BudgetExceededError
from evowiki.sources.chat_cite import cited_ids

if TYPE_CHECKING:
    from evowiki.bot.actions import ActionLedger

log = logging.getLogger(__name__)

GONE_TITLE = "（已删除的附件）"    # a card whose material row is gone: never its raw id


class SessionBusy(RuntimeError):
    """A turn or decision is already running in this session. HTTP 409 with `code` session_busy
    (clients branch on the code, not on the text: a refused attachment is a 409 too)."""

    error_code = "session_busy"


class _Locked:
    """Iterates `inner` while holding `lock`; releases it exactly once - on exhaustion, error,
    `close()` or garbage collection (so a stream that is never started does not leak it)."""

    def __init__(self, inner: Iterator[AssistantEvent], lock: threading.Lock,
                 message_id: str | None = None):
        self._inner, self._lock, self._held = inner, lock, True
        self.message_id = message_id

    @property
    def cursor(self) -> int | None:
        """Stream events read so far, for a durable turn (resume point); None inline."""
        return getattr(self._inner, "cursor", None)

    def __iter__(self) -> "_Locked":
        return self

    def __next__(self) -> AssistantEvent:
        if not self._held:
            raise StopIteration
        try:
            return next(self._inner)
        except BaseException:
            self._release()
            raise

    def close(self) -> None:
        try:
            close = getattr(self._inner, "close", None)
            if close is not None:
                close()
        finally:
            self._release()

    def _release(self) -> None:
        if self._held:
            self._held = False
            self._lock.release()

    def __del__(self) -> None:
        self._release()


class AssistantService:
    def __init__(self, deps: TurnDeps, applier: ProposalApplier, runs: TurnRuns | None = None,
                 *, ledger: "ActionLedger | None" = None,
                 rating_gaps: "Callable[[str, Sequence[Any], dict[str, Any]], int] | None" = None):
        self._deps = deps
        self._runs: TurnRuns = runs if runs is not None else InlineTurnRuns(deps)
        if hasattr(self._runs, "bind"):
            self._runs.bind(self)
        self._applier = applier
        self._ledger = ledger
        # a 👎 「我说过不会」 / 「没学过」 on a diagnosis question answers it as a gap (rating_gaps.py)
        self._rating_gaps = rating_gaps
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @property
    def durable(self) -> bool:
        """Turns run as DBOS workflows (the service must install this object before launch)."""
        return isinstance(self._runs, DbosTurnRuns)

    def user_busy(self, user_id: str) -> bool:
        """A turn of this user is running (the Bot's idle page writing waits for it)."""
        return self._runs.user_busy(user_id)

    @property
    def turn_deps(self) -> TurnDeps:
        """What the turn workflow runs over (also after a restart: `turn_runs.install`)."""
        return self._deps

    # ---- helpers ----

    def _acquire(self, session_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.setdefault(session_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise SessionBusy(f"assistant session {session_id} is busy")
        return lock

    def _own(self, user_id: str, session_id: str) -> SessionInfo:
        info = self._deps.store.info(session_id)          # LookupError -> 404
        if info.user_id != user_id:
            raise PermissionError(f"assistant session {session_id} is not yours")
        return info

    def _log_decision(self, session_id: str, payload: dict[str, Any], *,
                      once: bool = False) -> None:
        log_decision(self._deps.store, session_id, self._deps.new_id("ent"), payload,
                     self._deps.clock(), once=once)

    # ---- API ----

    def create_session(self, user_id: str, body: SessionCreateIn) -> SessionCreateOut:
        if body.page_id is not None and self._deps.data.page(user_id, body.page_id) is None:
            raise LookupError(f"unknown page {body.page_id}")
        info = SessionInfo(session_id=self._deps.new_id("asess"), user_id=user_id,
                           page_id=body.page_id, note_concept_id=body.note_concept_id,
                           created_at=self._deps.clock())
        self._deps.store.create(info)
        return SessionCreateOut(session_id=info.session_id)

    def get_session(self, user_id: str, session_id: str) -> SessionOut:
        info = self._own(user_id, session_id)
        out = session_out(fold(info, self._deps.store.entries(session_id),
                               language=self._deps.data.language(user_id)),
                          lambda ids: self._cards(user_id, ids))
        cited = {i for m in out.messages if m.role == "assistant" for i in cited_ids(m.text)}
        gone = self._deps.citations.gone(user_id, cited) if cited else set()
        return self._with_ledger_states(user_id, out).model_copy(
            update={"gone_sources": tuple(sorted(gone))})

    def _cards(self, user_id: str, ids: list[str]) -> dict[str, AttachmentOut]:
        """The attachment cards of a stored session (metadata only, no content)."""
        reader = self._deps.attachments
        if reader is None:
            raise AssistantUnavailable("this assistant has no attachment reader wired")
        try:
            loaded = reader.load(user_id, ids, content=False)
        except AttachmentGone:
            # one id no longer resolves (row removed): the others still show, that one as deleted
            loaded = []
            for i in ids:
                try:
                    loaded += reader.load(user_id, [i], content=False)
                except AttachmentGone:
                    log.warning("attachment %s of user %s is gone", i, user_id)
        cards = {a.source_id: attachment_out(a) for a in loaded}
        for i in ids:
            cards.setdefault(i, AttachmentOut(source_id=i, title=GONE_TITLE, kind="unknown",
                                              status="deleted", deleted=True))
        return cards

    def _with_ledger_states(self, user_id: str, out: SessionOut) -> SessionOut:
        """A resumed card shows the ledger's word: a proposal the session log holds no decision
        for, whose `assistant_edit` row the ledger has already decided (e.g. in 参数, or a
        mirror write that was lost), is reported as decided; a proposal whose row was undone is
        `undone` even when the session log says accepted (the ledger wins); an action card (the
        assistant's draft / action rows) carries its row's status, list (a draft the learner
        edited shows his page list) and whether it can still be undone. Ledger errors propagate."""
        if self._ledger is None:
            return out
        out = self._with_action_cards(user_id, out)
        from evowiki.assistant.edit_executor import assistant_key
        from evowiki.bot.actions import can_undo
        decisions = list(out.decisions)
        at = {d.proposal_id: i for i, d in enumerate(decisions)}
        extra: list[ProposalStateOut] = []
        for pr in out.proposals:
            row = self._ledger.get_by_key(assistant_key(pr.proposal_id))
            if row is None:
                continue
            if row.status == "undone":
                state = ProposalStateOut(proposal_id=pr.proposal_id, status="undone",
                                         applied=tuple((row.result or {}).get("applied", ())))
                if pr.proposal_id in at:
                    decisions[at[pr.proposal_id]] = state
                else:
                    extra.append(state)
                continue
            logged = decisions[at[pr.proposal_id]] if pr.proposal_id in at else None
            # executed: the page changed, whatever failure the log holds (a rejection never gets here)
            if row.status == "executed" and (logged is None or logged.status != "rejected"):
                state = ProposalStateOut(
                    proposal_id=pr.proposal_id, status="accepted",
                    applied=tuple((row.result or {}).get("applied", ())),
                    action_id=row.action_id, can_undo=can_undo(row))
                if logged is not None:
                    decisions[at[pr.proposal_id]] = state
                else:
                    extra.append(state)
                continue
            if pr.proposal_id in at:
                continue
            if row.status == "rejected":
                extra.append(ProposalStateOut(proposal_id=pr.proposal_id, status="rejected"))
            elif row.status == "failed":
                detail = (row.detail or "")[:DETAIL_MAX]
                extra.append(decision_state({"proposal_id": pr.proposal_id,
                                             "status": "failed", "detail": detail}))
        new = (*decisions, *extra)
        return out.model_copy(update={"decisions": new}) if new != out.decisions else out

    def _with_action_cards(self, user_id: str, out: SessionOut) -> SessionOut:
        from evowiki.assistant.tools.actions import card_of
        assert self._ledger is not None
        cards = []
        for card in out.actions:
            row = self._ledger.get(user_id, card.action_id)
            if row is None:               # the log names a row the ledger does not have: keep it
                cards.append(card.model_copy(update={"status": "unknown"}))
                continue
            fresh = card_of(row, language=self._deps.data.language(user_id))
            # kinds without a page list keep the items the tool showed
            cards.append(fresh if fresh.items or not card.items
                         else fresh.model_copy(update={"items": card.items}))
        return out.model_copy(update={"actions": tuple(cards)}) if cards else out

    def list_sessions(self, user_id: str, q: str = "", limit: int = 100) -> SessionListOut:
        """The user's non-empty sessions, newest activity first. Errors propagate."""
        needle = q.strip().casefold()
        rows: list[SessionSummaryOut] = []
        texts: dict[str, list[str]] = {}
        for info in self._deps.store.user_sessions(user_id):
            entries = self._deps.store.entries(info.session_id)
            state = fold(info, entries)
            if not state.turns:
                continue
            first = state.turns[0]
            title = conversation_title(first.user_text)
            if not title and first.selection:
                title = conversation_title(str(first.selection.get("text", "")))
            if not title and first.attachments and self._deps.attachments is not None:
                cards = self._cards(user_id, list(first.attachments[:1]))
                title = conversation_title(cards[first.attachments[0]].title)
            page = self._deps.data.page(user_id, info.page_id) if info.page_id else None
            rows.append(SessionSummaryOut(
                session_id=info.session_id, title=title, page_id=info.page_id,
                page_title=page.title if page is not None else "",
                note_concept_id=info.note_concept_id, created_at=info.created_at,
                # neither a rating of an old answer nor a wrap-up written later in the background
                # is new conversation: they do not move the session up
                last_activity_at=max((e.at for e in entries if e.type not in (
                                         "rating", "compaction", "consolidation", "wrapup_failed")),
                                     default=info.created_at),
                message_count=len(state.messages)))
            texts[info.session_id] = [m.text for m in state.messages]
        if needle:
            rows = [r for r in rows if needle in r.title.casefold()
                    or needle in r.page_title.casefold()
                    or any(needle in t.casefold() for t in texts[r.session_id])]
        rows.sort(key=lambda r: (r.last_activity_at, r.session_id), reverse=True)
        return SessionListOut(sessions=tuple(rows[:limit]))

    def read_tools(self, session_id: str) -> dict[str, Tool]:
        """This session's read-only tools as a turn would build them now, by name (a harness replay
        answers a read the traced turn never made with them, `harness.replay_assistant`)."""
        from evowiki.harness.pack import EMPTY
        tools = turn_tools_of(self._deps.tools, self._deps.store.info(session_id), self._deps.clock(), EMPTY)
        return {t.spec.name: t for t in tools if t.spec.effect == "read"}

    def rate(self, user_id: str, session_id: str, message_id: str, body: RatingIn) -> RatingOut:
        """Store the learner's 👍 / 👎 (or a clear) of one answer as a `rating` entry and return
        it. `message_id` is the assistant answer's own id; the entry also names its turn (the user
        message), whose run `assistant:{session}:{turn}` the rating attaches to, and the questions
        that turn asked the learner (a turn that ended on ask_user is rated too). Someone else's
        session or an unknown answer -> LookupError (404, so another user's session is not
        revealed). No session lock: a rating may land while a later turn runs."""
        info = self._deps.store.info(session_id)          # LookupError -> 404
        if info.user_id != user_id:
            raise LookupError(f"unknown assistant session {session_id}")
        entries = self._deps.store.entries(session_id)
        turn = answer_turn(entries, message_id)
        payload = rating_payload(message_id, turn, body, turn_asks(entries, turn))
        entry = self._deps.store.append(session_id, self._deps.new_id("ent"), "rating", payload,
                                        self._deps.clock())
        if self._deps.memory is not None:        # the turn's episode carries the feedback
            self._deps.memory.rated(info, [*entries, entry], turn)
        if self._rating_gaps is not None:        # never fails the rating (it logs)
            try:
                self._rating_gaps(user_id, entries, entry.payload)
            except Exception:
                log.exception("rating gaps failed for %s", entry.entry_id)
        if self._deps.wrapups is not None:
            self._deps.wrapups.opened(info)
        return rating_out(entry.payload) or RatingOut(message_id=message_id, value=None)

    def send(self, user_id: str, session_id: str, msg: MessageIn) -> Iterator[AssistantEvent]:
        """Checks ownership and takes the session lock *now* (errors raise here, before any
        event); the returned iterator runs the turn and releases the lock when it ends or is
        closed. New attachments must be this user's and readable now: unknown / someone else's /
        deleted -> AttachmentGone (HTTP 409 at send), still reading or failed -> AttachmentUnusable (409)."""
        self._own(user_id, session_id)
        attached = list(dict.fromkeys(msg.attachments))
        reader = self._deps.attachments
        if attached:
            if reader is None:
                raise AssistantUnavailable("this assistant has no attachment reader wired")
            ensure_usable(reader.load(user_id, attached, content=False))
        if self._runs.busy(session_id):
            raise SessionBusy(f"assistant session {session_id} is busy")
        lock = self._acquire(session_id)
        try:
            if attached:     # this question uses them: each is young again for the cleanup (D20)
                reader.asked(user_id, session_id, attached, self._deps.clock())  # type: ignore[union-attr]
            job = TurnJob(session_id=session_id, user_id=user_id,
                          message_id=self._deps.new_id("msg"), msg=msg)
            self._runs.start(job)
            return _Locked(self._runs.follow(user_id, job), lock, job.message_id)
        except BaseException:
            lock.release()
            raise

    def resume(self, user_id: str, session_id: str, message_id: str,
               after: int) -> Iterator[AssistantEvent]:
        """A client that lost a turn's stream follows it again from its cursor `after` (spec
        2026-10-15 §3). Read-only: no lock (the running turn holds the session busy). Only a
        durable turn can be resumed; anything else is LookupError (404)."""
        self._own(user_id, session_id)
        resume = getattr(self._runs, "resume", None)
        if resume is None:
            raise LookupError("this assistant runs turns inline: nothing to resume")
        return resume(f"turn:{session_id}:{message_id}", after)

    def stop(self, user_id: str, session_id: str) -> None:
        """He stopped the session's running turn (assistant.loop says how it ends; `follow` sees that
        end). LookupError (404): no turn is running."""
        self._own(user_id, session_id)
        message_id = self._runs.running(session_id)
        if message_id is None:
            raise LookupError(f"no running turn in assistant session {session_id}")
        self._deps.store.request_stop(session_id, message_id, self._deps.clock())
        self._runs.stop_now(session_id, message_id)

    def decide(self, user_id: str, proposal_id: str, accept: bool) -> ProposalDecisionOut:
        info, entry = self._deps.store.find_proposal(proposal_id)    # LookupError -> 404
        if info.user_id != user_id:
            raise PermissionError(f"proposal {proposal_id} is not yours")
        sid = info.session_id
        if self._runs.busy(sid):
            raise SessionBusy(f"assistant session {sid} is busy")
        lock = self._acquire(sid)
        try:
            prior = fold(info, self._deps.store.entries(sid)).decisions.get(proposal_id)
            if prior is not None:
                return ProposalDecisionOut(proposal_id=proposal_id, status="already_decided",
                                           detail=prior)
            if self._ledger is not None:
                return self._decide_via_ledger(user_id, sid, proposal_id, entry.payload, accept)
            if not accept:
                self._log_decision(sid, {"proposal_id": proposal_id, "status": "rejected",
                                         "applied": [], "detail": ""})
                return ProposalDecisionOut(proposal_id=proposal_id, status="rejected")
            draft = ProposalDraft.model_validate(entry.payload["draft"])
            try:
                applied = tuple(self._applier.apply(user_id, draft))
            except BudgetExceededError:
                raise                     # 429; WikiApplier calls no LLM today, this guards it
            except ApplyRefused as exc:   # a user-facing reason; the user may accept again
                log.info("proposal %s refused: %s", proposal_id, exc)
                detail = str(exc)[:DETAIL_MAX]
            except Exception:             # never leak internals; the user may accept again
                log.exception("applying proposal %s failed", proposal_id)
                detail = DECISION_INTERNAL
            else:
                self._log_decision(sid, {"proposal_id": proposal_id, "status": "accepted",
                                         "applied": list(applied), "detail": ""})
                return ProposalDecisionOut(proposal_id=proposal_id, status="accepted",
                                           applied=applied)
            self._log_decision(sid, {"proposal_id": proposal_id, "status": "failed",
                                     "applied": [], "detail": detail})
            return ProposalDecisionOut(proposal_id=proposal_id, status="failed", detail=detail)
        finally:
            lock.release()

    def _title(self, user_id: str, page_id: str) -> str | None:
        try:
            view = self._deps.data.page(user_id, page_id) if page_id else None
        except Exception:
            log.warning("page title lookup failed for %s", page_id, exc_info=True)
            return None
        return view.title if view is not None and view.title else None

    def _decide_via_ledger(self, user_id: str, sid: str, proposal_id: str,
                           payload: dict[str, Any], accept: bool) -> ProposalDecisionOut:
        """The action ledger decides (exactly once, version-checked); the executor mirrors the
        final status into the session's `decision` entry. The user's `page_body` level decides
        what the panel's click means: `ask` -> the proposal was mirrored as a pending row at
        proposal time (a proposal from before the ledger gets its row now; its payload has no
        page_version: no check); `auto_undo` -> the click is the approval: a row is created
        approved and executed at once (undoable), and a decline leaves no row. A pending row
        never expires."""
        from evowiki.assistant.edit_executor import assistant_key, proposal_action
        from evowiki.bot.actions import NotDecidable, can_undo
        ledger = self._ledger
        assert ledger is not None
        key = assistant_key(proposal_id)
        row = ledger.get_by_key(key)
        if row is None:
            level = ledger.level(user_id, "page_body")      # read once: used for the whole branch
            if level == "auto_undo" and not accept:
                self._log_decision(sid, {"proposal_id": proposal_id, "status": "rejected",
                                         "applied": [], "detail": ""}, once=True)
                return ProposalDecisionOut(proposal_id=proposal_id, status="rejected")
            row = ledger.propose(user_id, level=level, approved_now=level == "auto_undo",
                                 **proposal_action(payload, sid, page_title=self._title(
                                     user_id, payload["draft"].get("page_id", ""))))
        if row.status in ("executed", "rejected", "failed"):
            prior = {"executed": "accepted", "rejected": "rejected"}.get(row.status, row.detail)
            return ProposalDecisionOut(proposal_id=proposal_id, status="already_decided",
                                       detail=prior)
        try:
            row = (ledger.approve(user_id, row.action_id) if accept
                   else ledger.reject(user_id, row.action_id))
        except NotDecidable as exc:
            cur = ledger.get_by_key(key)
            if cur is not None and cur.status == "approved":    # stuck mid-approval (e.g. budget)
                return ProposalDecisionOut(proposal_id=proposal_id, status="already_decided",
                                           detail="approved")
            return ProposalDecisionOut(proposal_id=proposal_id, status="failed",
                                       detail=str(exc)[:DETAIL_MAX])
        if row.status == "executed":
            return ProposalDecisionOut(proposal_id=proposal_id, status="accepted",
                                       applied=tuple((row.result or {}).get("applied", ())),
                                       action_id=row.action_id, can_undo=can_undo(row))
        if row.status == "rejected":
            return ProposalDecisionOut(proposal_id=proposal_id, status="rejected")
        return ProposalDecisionOut(proposal_id=proposal_id, status="failed",
                                   detail=(row.detail or DECISION_INTERNAL)[:DETAIL_MAX])

    # ---- the 「我早会了」 confirm question ----

    def _known_row(self, user_id: str, action_id: str):
        if self._ledger is None:
            raise LookupError("actions are not wired")
        row = self._ledger.get(user_id, action_id)
        if row is None or row.kind != "node_known":
            raise LookupError(f"unknown action {action_id}")
        return row

    def known_item(self, user_id: str, action_id: str) -> KnownItemOut:
        """The confirm question of an executed 「我早会了」, WITHOUT its answer. LookupError (404)
        when there is none to answer: not executed (undone), no question, or answered already (a
        client that reloads asks again only while it is still open)."""
        row = self._known_row(user_id, action_id)
        item = (row.result or {}).get("item") if row.status == "executed" else None
        if not item:
            raise LookupError("this action has no confirm question")
        assert self._ledger is not None
        if self._ledger.executor_of("node_known").answered(user_id, row):
            raise LookupError("this confirm question was answered already")
        return KnownItemOut(stem=item["stem"], options=tuple(item["options"]))

    def known_answer(self, user_id: str, action_id: str, choice: int) -> KnownAnswerOut:
        """Answer it: right keeps the claim, wrong takes it back (the executor's `confirm`);
        `ActionRefused` (already answered / undone) is the caller's 409."""
        row = self._known_row(user_id, action_id)
        assert self._ledger is not None
        out = self._ledger.executor_of("node_known").confirm(user_id, row, choice)
        return KnownAnswerOut(correct=out["correct"], correct_index=out["correct_index"],
                              undone=out["undone"], message=out["message"])
