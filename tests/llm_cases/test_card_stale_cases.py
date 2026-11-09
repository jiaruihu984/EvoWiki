"""review.card_stale: the Bot's call on a hand-edited section's recall card. The prompt carries the new text and the old card;
the rule fires once per new hand edit. live/test_live_card_stale.py is the live side."""

from evowiki.bot.card_stale import PURPOSE, Verdict, stale_request
from evowiki.wikiapp.llm import JSON_PROMPT_VERSION


def test_review_card_stale_prompt_has_the_new_text_and_the_old_card():
    req = stale_request("节", "新的正文", "旧问题？", "旧答案", "u")
    assert req.purpose == PURPOSE and req.prompt_version == JSON_PROMPT_VERSION[PURPOSE]
    assert "新的正文" in req.prompt and "旧问题？" in req.prompt and "旧答案" in req.prompt


def test_a_new_card_is_optional_when_it_holds():
    assert Verdict.model_validate({"holds": True}).cue == ""
