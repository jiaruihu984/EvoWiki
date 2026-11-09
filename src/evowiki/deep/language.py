"""The frozen page language at each writer/visual model boundary."""
from collections.abc import Sequence

from evowiki.goals.language import Language, language_rule
from evowiki.llm.chat import ChatMessage


def output_rule(language: Language) -> str:
    return (language_rule(language)
            + " This applies to every learner-visible field: outline and section headings, body paragraphs, "
              "quiz questions, answer options and explanations, and all HTML/EvoUI labels, instructions, "
              "feedback and accessibility text. Developer instructions and reference examples may use another "
              "language; follow their structure, not their example wording. Preserve machine IDs, enum values, "
              "code syntax, formulas and original source quotations.")


def messages_in_language(messages: Sequence[ChatMessage], language: Language) -> tuple[ChatMessage, ...]:
    """A request copy; stored conversation and image parts remain unchanged."""
    last = messages[-1]
    return (*messages[:-1], last.model_copy(update={"text": last.text + "\n\n" + output_rule(language)}))
