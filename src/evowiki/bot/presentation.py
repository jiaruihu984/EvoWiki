"""System copy from stable ledger facts. Historical user/model prose is never rewritten."""
from collections.abc import Mapping
from typing import Any

from evowiki.i18n import current_language, tr

ACTION_NAMES = {
    "assistant_edit": "Edit page", "page_revise": "Revise page from sources",
    "page_split": "Split page", "page_merge": "Merge pages", "page_practice": "Add practice",
    "page_new": "Create page", "path_edit": "Adjust learning order", "goal_deadline": "Set deadline",
    "goal_new": "Create learning goal", "goal_pause": "Pause or resume goal",
    "goal_limits": "Update goal constraints", "path_blocks": "Update learning path",
    "node_split": "Split concept", "node_relation": "Edit concept relation",
    "node_known": "Record mastery self-report", "node_move": "Move concept",
    "card_new": "Create review cards", "card_fix": "Edit review card",
    "review_postpone": "Postpone reviews", "restyle_pages": "Rewrite pages with new rules",
    "settings": "Update assistant settings", "source_use": "Set material use",
    "exemplar": "Set example page", "profile_forget": "Remove background entry",
    "profile_known_retract": "Retract mastery self-report", "profile_edit": "Update background",
    "rule": "Update teaching rule", "preference": "Update preference",
    "prompt_override": "Update prompt guidance", "rsi_patch": "Adjust teaching parameters",
    "rsi_method": "Improve teaching method", "bot_write": "Create pages", "user_write": "Create page",
}
KIND_NAMES = {
    "review_due": "Review reminder", "wrong_answers": "Revisit difficult concepts",
    "goal_idle": "Continue learning", "strategy_negative": "Adjust explanations",
    "source_added": "Update from material", "card_stale": "Review card update",
    "open_question": "Unresolved question", "chat": "Page suggestion",
    "idle_write": "Write pages while idle", "user_write": "Create page", "goal_review": "Weekly review",
}
GATE_REASONS = {
    "paused": "The assistant is paused.", "kind_off": "This suggestion type is disabled.",
    "cap_reached": "The daily suggestion limit has been reached.",
    "cooldown": "This suggestion is in its reminder cooldown.",
    "suppressed": "Reminders are reduced after your Not interested response.",
    "empty": "There is nothing to suggest now.", "busy": "Another operation is running.",
}


def gate_detail(reason: str | None, outcome: str | None, *, language: str | None = None) -> str:
    lang = language or current_language()
    message = GATE_REASONS.get(reason or "", "Check passed." if outcome == "pass"
                               else "Waiting for the next check.")
    return tr(message, lang=lang)


def action_summary(row: Any, *, language: str | None = None, target: str = "") -> str:
    lang = language or current_language()
    if lang != "en":
        return row.summary
    content = row.content
    title = next((str(content[k]) for k in ("title", "page_title", "goal_title", "name")
                  if isinstance(content.get(k), str) and content[k]), target)
    label = tr(ACTION_NAMES.get(row.kind, "Change"), lang=lang)
    return f"{label}: {title}" if title else label


def action_detail(row: Any, *, language: str | None = None) -> str:
    lang = language or current_language()
    if lang != "en":
        return row.detail
    # A model's decision to keep a concept whole is original reasoning, not a status label.
    if (row.result or {}).get("kept"):
        return str(row.result["kept"])
    if row.status != "failed":
        return ""
    error = (row.result or {}).get("error", {})
    if isinstance(error.get("message"), str):
        return tr(error["message"], lang=lang, **error.get("params", {}))
    message = ("The page changed after this draft was proposed. Create a new draft."
               if error.get("code") == "page_changed" else
               "This action did not complete. Check its status before retrying.")
    return tr(message, lang=lang)


def trigger_text(kind: str, payload: Mapping[str, Any], *, language: str | None = None) -> tuple[str, str]:
    lang = language or current_language()
    title = str(payload.get("title") or payload.get("goal") or payload.get("name") or "")
    label = tr(KIND_NAMES.get(kind, "Suggestion"), lang=lang)
    reason = label
    if kind == "review_due":
        count = payload.get("count", 0)
        return (tr("Review {count} cards", lang=lang, count=count),
                tr("{count} review cards are due.", lang=lang, count=count))
    if kind == "goal_idle":
        reason = tr("This goal has not been opened for {days} days.", lang=lang, days=payload.get("days", 0))
    elif kind == "wrong_answers":
        reason = tr("{count} distinct questions were answered incorrectly.", lang=lang, count=payload.get("count", 0))
    elif kind == "open_question":
        # The question itself remains verbatim.
        return str(payload.get("text") or label), tr("This question was unresolved in the previous conversation.", lang=lang)
    elif kind == "goal_review":
        situation = payload.get("situation")
        messages = {"normal": "Weekly progress is on track.",
                    "prereq_gap": "Review a prerequisite before continuing.",
                    "prereq_missing": "Review a prerequisite before continuing.",
                    "stuck_page": "Try another explanation for this page.",
                    "review_debt": "Review due cards before studying new pages.",
                    "mostly_known": "Much of the remaining material is already known. Review the plan.",
                    "want_park": "Consider pausing this goal."}
        reason = tr(messages.get(str(situation), "Review this learning goal."), lang=lang)
    elif kind == "strategy_negative":
        reason = tr("Recent feedback indicates that the explanation should change.", lang=lang)
    elif kind == "card_stale":
        reason = tr("A changed section may require an updated review card.", lang=lang)
    return (f"{label}: {title}" if title else label), reason
