"""The learner's content language (trial fix T2): one setting, per user, default zh."""

from typing import Literal

Language = Literal["zh", "en"]
LANGUAGE_NAMES: dict[str, str] = {"zh": "简体中文", "en": "English"}


def language_rule(lang: Language) -> str:
    """One sentence for prompts: all learner-facing text is in `lang`."""
    name = LANGUAGE_NAMES[lang]
    return (f"All learner-facing text must be written in {name}; "
            "technical terms may keep the original wording in parentheses.")
