"""The action ledger as a `ChangeSource` (spec 2026-10-14 §4): its decided rows join the 参数 change
list (`GET /wiki/rsi/changes`, track "G") and are undone through the same route. Rows of source
`rsi` are not listed (their native A/B / method rows already are). A page revision from materials says what it did in the demo's words
(`diffs.revise_label`: 「「X」按《A》更新了 N 节」 and its reason), not its stored 按资料改「X」."""

from collections import Counter
from collections.abc import Callable, Collection
from datetime import datetime

from evowiki.bot.actions import ActionLedger, ActionRow, can_undo
from evowiki.bot.diffs import Names, revise_label
from evowiki.bot.presentation import action_summary
from evowiki.i18n import current_language, tr
from evowiki.teach.contracts import ChangeOut, PatchStatus
from evowiki.wikiapp.revise import KIND as PAGE_REVISE

PREFIX = "act_"
LISTED_SOURCES = ("assistant", "bot", "user")   # user: a page revision it asked for
# failed / rejected rows are listed for the record only: the plugin never draws them
# (rsi-changes.ts `isShown`: an attempt that never took effect is internal process, not a change
# the user has to know about)
LISTED_STATUSES = ("executed", "undone", "failed", "rejected")
TARGET = "知识库"
LIMIT = 500
# the simulator imports `PatchStatus`, so the ledger's statuses map onto it
STATUS: dict[str, PatchStatus] = {
    "executed": "personal_accepted", "undone": "rolled_back", "rejected": "rejected",
    "failed": "gate_failed"}


# (waiting, failed): action id -> pages it marked out of date still waiting for their rewrite, and
# those that stopped retrying (`wikiapp.stale.stale_counts`; a rule row marks them by its action id)
StaleCounts = tuple[Counter[str], Counter[str]]


def change_out(row: ActionRow, stale: StaleCounts | None = None,
               superseded: Collection[str] = (), names: Names | None = None) -> ChangeOut:
    """`kind` carries the action's scope ("teaching_params" / "path" / "preference" ...), so the
    plugin can group a row without the Bot's action list. `names`: the user's wiki, for a page
    revision's words (absent: the page title from the replaced page, else 未命名页面)."""
    waiting, failed = stale if stale is not None else (Counter(), Counter())
    summary, why = revise_label(row, names or Names(None)) or (row.summary, row.why)
    if current_language() == "en" and row.kind != PAGE_REVISE:
        summary = action_summary(row)
    return ChangeOut(
        patch_id=row.action_id, track="G", status=STATUS[row.status], summary=summary,
        why=why, signals=(), at=row.decided_at or row.created_at,
        can_undo=can_undo(row, superseded), target=tr("Knowledge base", lang="en") if current_language() == "en" else TARGET, kind=row.scope,
        pages_waiting=waiting[row.action_id], pages_failed=failed[row.action_id])


class LedgerChangeSource:
    """`stale`: the user's stale-page counts (one snapshot load per list); absent = none counted.
    `names`: the user's wiki names (`diffs.Names`), read only when a page revision is listed."""

    prefix = PREFIX
    rsi_related = False       # an RSI mirror row's executor takes the lock itself (`RsiExecutor.undo`)

    def __init__(self, ledger: ActionLedger, stale: Callable[[str], StaleCounts] | None = None,
                 names: Callable[[str], Names] | None = None):
        self._ledger, self._stale, self._names = ledger, stale, names

    def _names_for(self, user_id: str, rows: list[ActionRow]) -> Names | None:
        if self._names is None or not any(r.kind == PAGE_REVISE for r in rows):
            return None
        return self._names(user_id)

    def list(self, user_id: str) -> list[ChangeOut]:
        rows = self._ledger.list_changes(
            user_id, sources=LISTED_SOURCES, statuses=LISTED_STATUSES, limit=LIMIT)
        stale = self._stale(user_id) if self._stale is not None and rows else None
        superseded = self._ledger.superseded(user_id, rows)
        names = self._names_for(user_id, rows)
        return [change_out(r, stale, superseded, names) for r in rows]

    def undo(self, user_id: str, change_id: str, *, now: datetime) -> ChangeOut:
        """LookupError unknown (or another user's) id; ValueError (NotDecidable) when the row
        cannot be undone."""
        row = self._ledger.undo(user_id, change_id)
        return change_out(row, self._stale(user_id) if self._stale is not None else None,
                          self._ledger.superseded(user_id, [row]),
                          self._names_for(user_id, [row]))
