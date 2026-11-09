"""`AssistantEditExecutor`: the reading assistant's page-edit proposals
(spec 2026-10-14 §4 "合并现有三套确认"). The ledger row is the single source of truth for the
decision; the executor writes the page with the ledger's effect in the SAME commit
(`WikiApplier.apply(..., extra=[effect])`, version-checked). The assistant session's `decision`
entry is written only from the ledger's `decided` hook / `rejected` call, i.e. after the row's
final status is visible, so the session card always follows the ledger (never an executor's
local outcome in a race)."""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from evowiki.agent.tools import ProposalDraft
from evowiki.assistant.contracts import DECISION_INTERNAL, DETAIL_MAX
from evowiki.assistant.session import SessionStore, log_decision
from evowiki.assistant.wiki import ApplyRefused, WikiApplier
from evowiki.bot.actions import STALE_DETAIL, ActionRefused, ActionRow, AlreadyExecuted
from evowiki.llm.usage import BudgetExceededError
from evowiki.store import bot as store

log = logging.getLogger(__name__)


KIND = "assistant_edit"


def assistant_key(proposal_id: str) -> str:
    return f"assistant:{proposal_id}"


def proposal_action(payload: dict[str, Any], session_id: str, *,
                    page_title: str | None = None) -> dict[str, Any]:
    """`ActionLedger.propose` / `propose_in` arguments of an assistant `proposal` entry. The
    summary is one plain sentence naming the page (`page_title`; the heading when unknown)."""
    draft = ProposalDraft.model_validate(payload["draft"])
    add = draft.kind == "add_section"
    if page_title:
        summary = f"在「{page_title}」里" + (f"添加一节「{draft.heading}」" if add else f"改写「{draft.heading}」这一节")
    else:       # the page is gone or unknown: name the section instead
        summary = f"添加一节「{draft.heading}」" if add else f"改写「{draft.heading}」这一节"
    return {
        "kind": KIND, "scope": "page_body", "source": "assistant", "summary": summary,
        "why": draft.why,
        "content": {**draft.model_dump(mode="json"), "proposal_id": payload["proposal_id"],
                    "session_id": session_id, "section_version": payload.get("section_version")},
        "key": assistant_key(payload["proposal_id"]), "page_id": draft.page_id,
        "page_version": payload.get("page_version")}


class AssistantEditExecutor:
    def __init__(self, applier: WikiApplier, store: SessionStore, clock: Callable[[], datetime],
                 new_id: Callable[[str], str]):
        self._applier, self._store, self._clock, self._new_id = applier, store, clock, new_id

    def mirror(self, action: ActionRow, status: str, applied=(), detail: str = "") -> None:
        """The session's `decision` entry for the proposal (at most one)."""
        log_decision(self._store, action.content["session_id"], self._new_id("ent"),
                     {"proposal_id": action.content["proposal_id"], "status": status,
                      "applied": list(applied), "detail": detail}, self._clock(), once=True)

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        draft = ProposalDraft.model_validate(action.content)
        undo: dict[str, Any] = {}

        def write_undo(conn) -> None:       # in the page's commit: an executed edit always has it
            store.set_action_undo(conn, action.action_id, dict(undo))
        try:
            applied = tuple(self._applier.apply(
                user_id, draft, expect_version=action.page_version,
                section_version=action.content.get("section_version"),
                extra=[effect, write_undo], undo_sink=undo))
        except BudgetExceededError:
            raise                     # 429; the row stays `approved`, approving again retries
        except ApplyRefused as exc:   # a user-facing reason
            log.info("proposal %s refused: %s", action.content["proposal_id"], exc)
            detail = str(exc)[:DETAIL_MAX]
            raise ActionRefused(detail, stale=exc.error_code == "page_changed",
                                public_message=exc.public_message, code=exc.error_code, params=exc.params) from exc
        except (ActionRefused, AlreadyExecuted):
            raise                     # AlreadyExecuted: another approve won, the commit rolled back
        except Exception as exc:      # never leak internals
            log.exception("applying proposal %s failed", action.content["proposal_id"])
            raise ActionRefused(DECISION_INTERNAL) from exc
        return {"applied": list(applied)}

    def decided(self, user_id: str, action: ActionRow) -> None:
        if action.status == "executed":
            self.mirror(action, "accepted", (action.result or {}).get("applied", ()))
        elif action.status == "failed":
            self.mirror(action, "failed", detail=action.detail[:DETAIL_MAX])

    def voided_by(self, user_id: str, action: ActionRow) -> list[str]:
        """The ledger keys of this session's other drafts of the section this edit just rewrote."""
        c = action.content
        if c.get("kind") != "rewrite_section" or not c.get("session_id"):
            return []
        return [assistant_key(pid) for e in self._store.entries(c["session_id"])
                if e.type == "proposal" and (pid := e.payload.get("proposal_id")) != c.get("proposal_id")
                and (d := e.payload.get("draft", {})).get("kind") == "rewrite_section"
                and d.get("page_id") == c.get("page_id") and d.get("section_id") == c.get("section_id")]

    def void_without_row(self, by: ActionRow, key: str) -> None:
        log_decision(self._store, by.content["session_id"], self._new_id("ent"),
                     {"proposal_id": key.removeprefix(assistant_key("")), "status": "failed",
                      "applied": [], "detail": STALE_DETAIL}, self._clock(), once=True)

    def rejected(self, user_id: str, action: ActionRow) -> None:
        self.mirror(action, "rejected", detail=action.detail[:DETAIL_MAX])

    def undo(self, user_id: str, action: ActionRow) -> dict:
        """Restores the edited section from the undo data stored when the edit was executed;
        refused when the page changed since."""
        if not action.undo:
            raise ActionRefused("这条修改无法撤销")
        def mark_undone(conn) -> None:      # in the page's commit: page and ledger move together
            store.set_action_status(conn, action.action_id, status="undone", at=self._clock(),
                                    from_statuses=("executed",))
        try:
            self._applier.undo_edit(user_id, action.undo, extra=[mark_undone])
        except ApplyRefused as exc:
            raise ActionRefused(str(exc)[:DETAIL_MAX]) from exc
        return {"restored": [action.undo["section_id"]]}
