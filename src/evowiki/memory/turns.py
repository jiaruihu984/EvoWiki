"""Reads of the session log by id: the only source of a learner's own words (a recall block, a
tool result or a rendered profile is never his words)."""

from evowiki.assistant.session import SessionStore, fold
from evowiki.memory.contracts import PastTurn
from evowiki.teach.textsim import squash  # wikiapp.guidelines.quoted_in uses the same squash

QUOTE_MIN = 4


def verify_quote(store: SessionStore, user_id: str, session_id: str, message_id: str,
                 quote: str) -> bool:
    """`quote` (whitespace ignored, at least QUOTE_MIN characters, a short Latin name (c++, Go), or his whole message: a bare
    「可以」) is in the text of that `user` message of that user's session."""
    q = squash(quote)
    if not q:
        return False
    hit = store.find_message(session_id, message_id)
    if hit is None:
        return False
    info, entry = hit
    text = squash(entry.payload.get("text") or "")
    return (info.user_id == user_id and entry.type == "user" and q in text
            and (len(q) >= QUOTE_MIN or q == text or (len(q) >= 2 and q[0].isascii() and q[0].isalpha())))


def read_turn(store: SessionStore, user_id: str, session_id: str,
              message_id: str) -> PastTurn | None:
    """The answered turn whose user message is `message_id`, whole; None when it is not this
    user's, not a user message, or has no answer."""
    hit = store.find_message(session_id, message_id)
    if hit is None or hit[0].user_id != user_id or hit[1].type != "user":
        return None
    info = hit[0]
    state = fold(info, store.entries(session_id))
    turn = next((t for t in state.turns if t.message_id == message_id), None)
    if turn is None or turn.answer is None:
        return None
    return PastTurn(session_id=session_id, message_id=message_id, at=hit[1].at,
                    page_id=info.page_id, user_text=turn.user_text, answer_text=turn.answer)
