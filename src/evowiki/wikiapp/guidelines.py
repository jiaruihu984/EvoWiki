"""User guidelines (原则): the learner's own rules, and the preferences he states in the chat.

The `/wiki/guidelines` API is implemented over the teaching spec (`wikiapp.teach`): GET lists the
spec items that are not default / seed / import ones (the user's → "user"; RSI's, pending ones and
those with evidence → "feedback", evidence from `WikiState.item_evidence`). POST text adds an item (source
`user`); DELETE removes an item (404 if it is not one). Each is one spec version (action
`guideline`; a user without a stored spec gets the migrated v1 first, same commit).
`GuidelineAdded` / `GuidelineRemoved` are still replayed from old logs but no longer written —
except DELETE for a user with no stored spec and more than 30 old guidelines (cannot be migrated,
every spec write is 422): it writes the legacy `GuidelineRemoved` so they can get under the cap.
Until a spec is stored, GET lists the old guidelines as they are (same result as the migration,
and it works even past the 30-item cap). No model proposes a guideline: the rules are the user's own
words, or a preference he states in the chat.

Stated in the chat: `wikiapp.preferences.land_preference` (the assistant's `remember_preference`,
Memory's backfill) is the one landing point; this module keeps the texts and lookups it shares:
`blocked_rule_texts` (no resurrection), `said_rules` (the profile's 「怎么学比较好」), `stated_evidence`.
Removing a pending rule here also closes its waiting ledger card (`preferences.close_pending`).

Every write here is the user's (`commit_spec(actor="user")`): the guidelines page adds origin
`user_ui`; a delete records `revoked`."""

from collections.abc import Iterable
from datetime import datetime

from evowiki.rules.decisions import Decisions
from evowiki.teach.contracts import SpecItem
from evowiki.teach.textsim import squash
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.events import Guideline
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Repo
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.teach import commit_spec
from evowiki.wikiapp.wiki_schemas import GuidelineIn, GuidelineOut, GuidelinesOut

MAX_TEXT = 300            # = GuidelineIn.text max_length: the LLM's text gets the user's cap


def _out(g: Guideline) -> GuidelineOut:
    return GuidelineOut(guideline_id=g.guideline_id, text=g.text, source=g.source,
                        evidence=g.evidence)


def _item_out(i: SpecItem, state: WikiState) -> GuidelineOut:
    """An item with evidence (a chat statement, an old feedback guideline) reads as "feedback"
    with it; any other the user owns as "user"."""
    if i.authority == "user" and i.item_id not in state.item_evidence:
        return GuidelineOut(guideline_id=i.item_id, text=i.text, source="user")
    return GuidelineOut(guideline_id=i.item_id, text=i.text, source="feedback",
                        evidence=state.item_evidence.get(i.item_id))


def list_guidelines(state: WikiState) -> GuidelinesOut:
    if state.spec is None:            # = the migrated v1's user / rsi items, without the cap
        return GuidelinesOut(items=[_out(g) for g in state.guidelines])
    return GuidelinesOut(items=[_item_out(i, state) for i in state.spec.items
                                if i.origin not in ("default", "seed")])


def add_guideline(repo: Repo, user_id: str, body: GuidelineIn, *, now: datetime,
                  new_id: Ids) -> GuidelineOut:
    """`text` → a spec item with source `user`. A 31st item → SpecInvalid (422)."""
    snap = repo.load(user_id)
    item_id = new_id("si")
    text = body.text
    new = commit_spec(repo, snap,
                      lambda s: tspec.write(s, tspec.AddRule(item_id, text), actor="user"),
                      actor="user", action="guideline", now=now, new_id=new_id)
    assert new.state.spec is not None
    return _item_out(next(i for i in new.state.spec.items if i.item_id == item_id), new.state)


def remove_guideline(repo: Repo, user_id: str, guideline_id: str, *, now: datetime,
                     new_id: Ids) -> None:
    """Removes the spec item (any source); not an item → LookupError (404). A pending one is
    rejected, and its waiting card closes in the same commit."""
    from evowiki.wikiapp.preferences import close_pending
    snap = repo.load(user_id)
    commit_spec(repo, snap,
                lambda s: tspec.write(s, tspec.RemoveRule(guideline_id), actor="user"),
                actor="user", action="guideline", now=now, new_id=new_id,
                commit_extra=[close_pending(user_id, guideline_id, now)])


def quoted_in(quote: str, words: Iterable[str]) -> bool:
    """`quote` is verbatim (whitespace ignored) part of one of `words`."""
    q = squash(quote)
    return bool(q) and any(q in squash(w) for w in words)


# ---- preferences stated in the chat (`wikiapp.preferences.land_preference`) ----

NOT_QUOTED = "quote 要一字不改地抄他在这次对话里自己写的话"
STATED_REJECTED = "他拒绝过或撤销过相近的原则，不要再记"
STATED_KNOWN = "已经有相近的原则了，不用再记"
STATED_PENDING = "已经有一条相近的原则在等他确认"
STATED_EMPTY = "原则不能为空"
STATED_LONG = f"原则太长了，最多 {MAX_TEXT} 字"

# the profile's 「怎么学比较好」 (rule provenance contract §5, §7 #10): only rules he owns and stated himself
SAID_ORIGINS = frozenset({"user_ui", "user_chat"})


def stated_evidence(quote: str) -> str:
    return f"你说过「{' '.join(quote.split())}」"


def blocked_rule_texts(state: WikiState) -> tuple[str, ...]:
    """The rule texts the learner rejected or revoked and has not re-accepted since (the
    decisions in force, `rules.decisions.Decisions`, plus the old rejection texts). No automatic
    path may bring one back: callers check `near_duplicate(text, b) for b in
    blocked_rule_texts(state)` (S3's `land_preference`, Memory's consolidation, RSI's proposals)."""
    return tuple(dict.fromkeys(t for t, _, _ in Decisions.of(state).texts()))


def said_rules(state: WikiState) -> tuple[SpecItem, ...]:
    """The rules that are his own words (`authority=user`, origin user_ui / user_chat / import):
    what the profile's 「怎么学比较好」 lists. RSI's, default, seed and pending rules never read as 「你说过的」."""
    try:
        items = tspec.effective_spec(state).items
    except tspec.SpecInvalid:
        return ()
    return tuple(i for i in items if i.authority == "user" and i.origin in SAID_ORIGINS)
