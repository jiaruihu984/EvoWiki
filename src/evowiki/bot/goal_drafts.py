"""A goal draft whose question is settled stops waiting; one nobody settled waits for his 取消.

A `goal_new` draft has no clock (a pending item never expires, `bot.actions`: nothing of his is dropped
behind his back). It is settled when the goal it would make exists (`goal.same_goal`, as its 确认 refuses:
「已经有这个目标了」) or a newer draft asks for the same goal, whichever conversation wrote it. A draft made
from materials or with 「重新分块」 (`again`) asks for something new on purpose: an existing goal does not
settle it. Done when the ledger is read, so it holds however the goal came to exist."""

from collections.abc import Sequence

from evowiki.bot.actions import ActionLedger, ActionRow, NotDecidable
from evowiki.wikiapp.goal import GOAL_LEAD, norm_name, same_goal
from evowiki.wikiapp.repo import Repo
from evowiki.wikiapp.state import WikiState

KIND = "goal_new"
GOAL_EXISTS = "已经有这个目标了"
REPLACED = "replaced"       # the detail of a row a newer draft replaced (the card says so)


def _names(row: ActionRow) -> set[str]:
    return {norm_name(GOAL_LEAD.sub("", str(n).strip()))
            for n in (row.content.get("goal"), row.content.get("title")) if n and str(n).strip()}


def settled(state: WikiState, rows: Sequence[ActionRow]) -> list[tuple[ActionRow, str]]:
    """The waiting drafts among `rows` (newest first) that stop waiting, each with why."""
    out: list[tuple[ActionRow, str]] = []
    newer: set[str] = set()
    for row in rows:
        if row.kind != KIND or row.status != "proposed":
            continue
        names = _names(row)
        if row.content.get("source_ids") or row.content.get("again"):
            continue
        goal, title = row.content.get("goal"), row.content.get("title")
        if same_goal(state, str(goal or ""), str(title or "")) is not None:
            out.append((row, GOAL_EXISTS))
        elif names & newer:
            out.append((row, REPLACED))
        newer |= names
    return out


def settle(ledger: ActionLedger, repo: Repo, user_id: str, rows: Sequence[ActionRow]) -> bool:
    """Takes the settled drafts among `rows` off the waiting list; True when one was."""
    if not any(r.kind == KIND and r.status == "proposed" for r in rows):
        return False
    try:
        state = repo.load(user_id).state
    except LookupError:
        return False
    done = settled(state, rows)
    for row, why in done:
        try:
            ledger.reject(user_id, row.action_id, detail=why)
        except NotDecidable:      # decided a moment ago: the next read shows it
            pass
    return bool(done)
