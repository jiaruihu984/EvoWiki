"""Who owns a rule the assistant wrote down from the user's words (rule provenance contract §6,
decision 1, as the user ruled it: a drafted rule must carry his verbatim quote). Decided in code,
no model call: the rule is the user's (`authority=user`) only when
1. the quote is verbatim in his message (`words`: this message and his replies to this turn's
   questions), at least MIN_QUOTE characters or his whole message (whitespace ignored), and
2. the rule's text is a near-duplicate of the quote (`teach.textsim.near_duplicate`: it says what
   he said, nothing wider; the negation guard keeps 「先给例子」 and 「不要先给例子」 apart).
Anything else — no quote, a quote not in his message, a rule reworded away from his words — is
`pending` (origin `assistant_drafted`) until he confirms it. The assistant's
`remember_preference` asks it before `wikiapp.preferences.land_preference` (`pending=`)."""

from collections.abc import Sequence

from evowiki.teach.contracts import Authority
from evowiki.teach.textsim import near_duplicate, squash

MIN_QUOTE = 4


def _quoted(quote: str, words: Sequence[str]) -> bool:
    q = squash(quote)
    return bool(q) and any(q in squash(w) and (len(q) >= MIN_QUOTE or q == squash(w))
                           for w in words)


def drafted_authority(*, quote: str | None, text: str, words: Sequence[str]) -> Authority:
    if not quote or not _quoted(quote, words):
        return "pending"
    return "user" if near_duplicate(" ".join(text.split()), " ".join(quote.split())) else "pending"
