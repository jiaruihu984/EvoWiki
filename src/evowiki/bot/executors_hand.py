"""`hand_edit` rows: a hand edit in a note (`wikiapp.hand_edit`), recorded when it happened. 活动记录's
撤销 is the edit's own undo; it refuses with the reason when a section of it changed since."""

from collections.abc import Callable
from datetime import datetime

from evowiki.bot.actions import ActionRefused, ActionRow
from evowiki.bot.executors import RsiMirrorExecutor
from evowiki.wikiapp.hand_edit import undo_hand_edit
from evowiki.wikiapp.repo import Repo, WikiConflict


class HandEditExecutor(RsiMirrorExecutor):
    def __init__(self, repo: Repo, clock: Callable[[], datetime], new_id: Callable[[str], str]):
        self._repo, self._clock, self._new_id = repo, clock, new_id

    def undo(self, user_id: str, action: ActionRow) -> dict:
        try:
            undo_hand_edit(self._repo, user_id, action.page_id or "", action.undo["edit_id"],
                           now=self._clock(), new_id=self._new_id)
        except (WikiConflict, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {}
