"""A 👎 on a diagnosis question that says 「我说过不会」 / 「没学过」 is his answer to it (stage 3 phase 2,
rating → K). Code only, at the rating POST: no model call.

When the learner rates down a turn that ended on an ask_check question and the rating says he
does not know it — the chip 「我说过不会」, or 「我说过不会」 / 「没学过」 in the note — the question's
pick is answered as a gap through the diagnosis's own path (`wikiapp.goal.answer_check`): a quiz
as 不知道 (IDK), a self-report as its `dont` option (没学过 → the self-report table's `never`). The
round closes, so next_check never picks that cell again.

Idempotent: a pick is answered once (answer_check refuses an answered pick), so a second rating,
or the same rating posted twice, records nothing more. A pick no longer open (he answered it, the
diagnosis ended) is left as it is. Failures are logged and never fail the rating."""

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from evowiki.assistant.session import Entry
from evowiki.assistant.tools.check import ASK, AskCheckArgs, AskCheckTool, open_pick
from evowiki.goals.diagnostic import IDK
from evowiki.wikiapp.repo import Repo, WikiConflict, WikiInvalid

log = logging.getLogger(__name__)

GAP_CHIP = "我说过不会"
GAP_WORDS = ("我说过不会", "没学过")


def says_gap(rating: Mapping[str, Any]) -> bool:
    """A down rating whose chip or note says he does not know the asked thing."""
    if rating.get("value") != "down":
        return False
    note = str(rating.get("note") or "")
    return GAP_CHIP in (rating.get("chips") or ()) or any(w in note for w in GAP_WORDS)


class RatingGaps:
    """`AssistantService.rate`'s hook: the rated turn's ask_check questions answered as gaps."""

    def __init__(self, repo: Repo, gateway: Callable[[str], Any], clock: Callable[[], datetime],
                 new_id: Callable[[str], str]):
        self._repo, self._clock, self._new_id = repo, clock, new_id
        self._ask = AskCheckTool(repo, gateway, clock, new_id)
        self._gateway = gateway

    def __call__(self, user_id: str, entries: Sequence[Entry], rating: Mapping[str, Any]) -> int:
        """Answers the asks of `rating`'s turn as gaps; returns how many picks it answered."""
        if not says_gap(rating):
            return 0
        asked = {str(a.get("call_id")) for a in rating.get("asks") or ()}
        done = 0
        for e in entries:
            p = e.payload
            if (e.type != "tool" or p.get("name") != ASK or str(p.get("call_id")) not in asked
                    or p.get("message_id") != rating.get("turn_id")):
                continue
            try:
                done += self._gap(user_id, p.get("args") or {})
            except (WikiConflict, WikiInvalid, LookupError, ValueError) as exc:
                log.info("rating gap not recorded for %s: %s", p.get("call_id"), exc)
        return done

    def _gap(self, user_id: str, raw: Mapping[str, Any]) -> int:
        a = AskCheckArgs.model_validate(raw)
        draft_id = a.pick_id.rsplit(":", 1)[0]
        pick = open_pick(self._repo.load(user_id), draft_id)
        if pick is None or pick.pick_id != a.pick_id:
            return 0                     # answered already, or the diagnosis moved on
        q = self._ask._question(a, pick)
        if q.kind == "quiz":
            choice = IDK
        elif "dont" in q.means:
            choice = q.means.index("dont")
        else:
            return 0
        from evowiki.wikiapp.goal import answer_check
        answer_check(self._repo, self._gateway(user_id), user_id, draft_id, pick.pick_id, q,
                     choice, now=self._clock(), new_id=self._new_id)
        return 1
