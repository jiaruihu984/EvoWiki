from datetime import UTC, datetime

from evowiki.bot.bot_schemas import ActionOut
from evowiki.bot.presentation import gate_detail, trigger_text
from evowiki.i18n import language_context
from evowiki.store.bot import ActionRow


def test_old_system_summary_renders_from_facts_without_rewriting_history():
    row = ActionRow(action_id="a", user_id="u", kind="page_new", scope="path", level="ask",
                    status="proposed", source="assistant", summary="写一页旧系统标题",
                    content={"title": "用户原文"}, content_hash="h", idempotency_key="k",
                    created_at=datetime.now(UTC), why="原来的模型理由")
    with language_context("en"):
        shown = ActionOut.of(row)
    assert shown.summary == "Create page: 用户原文"
    assert shown.status == "proposed" and shown.action_id == "a"
    assert shown.why == row.why
    assert row.summary == "写一页旧系统标题"
    assert row.content == {"title": "用户原文"}


def test_gate_and_trigger_display_use_codes_and_payload_not_old_chinese():
    with language_context("en"):
        assert gate_detail("paused", "skip") == "The assistant is paused."
        title, reason = trigger_text("review_due", {"count": 7})
    assert title == "Review 7 cards" and reason == "7 review cards are due."
