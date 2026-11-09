"""Which signal kinds count as helpful / harmful reactions to the teaching (shared by the Bot's
facts and rules, `bot.facts` / `bot.rules`). Pure, no IO."""

from evowiki.teach.contracts import Signal

HARMFUL_KINDS = frozenset({"page_feedback", "section_deeper", "section_other", "expand",
                           "assistant_selection", "check_unclear"})
OUTCOME_KINDS = frozenset({"check_answer", "review_grade", "block_result"})


def polarity(sig: Signal) -> int:
    """+1 helpful, −1 harmful, 0 not counted."""
    if sig.kind in ("section_know", "page_fit"):
        return 1
    if sig.kind in OUTCOME_KINDS and sig.outcome is not None:
        return 1 if sig.outcome >= 0.5 else -1
    if sig.kind in HARMFUL_KINDS:
        return -1
    if sig.kind == "card_feedback" and sig.text.startswith(("题目没说清楚", "答案不对")):
        return -1
    return 0
