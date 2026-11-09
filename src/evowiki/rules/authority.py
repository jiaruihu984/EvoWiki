"""The one write checkpoint of teaching rules (rule provenance contract §1.3 / §1.4).

`settle(base, new, actor=...)` compares the spec about to be saved with the spec it was derived
from, item by item, and either raises `RuleAuthorityError` (nothing may be written) or returns the
spec with its provenance stamped plus the user's `RuleDecided` events the change implies. Every
teaching-spec write goes through it: `wikiapp.teach.commit_spec` (mandatory `actor`) calls it on
every save, so a whole-spec writer (`RepoSpecRegistry.commit`, the import, a restore) cannot get
around it; `wikiapp.teachspec.write` and the in-memory registry run the same check.

Per item:
- `origin`, `origin_ref` and `protected` never change (copied from the base item).
- A content change (text, scope, domain) is `rev` + 1 with `rev_by` = the actor.
- actor `user`: anything goes. An edit makes the item `authority=user` (an RSI or default rule
  edited by the user becomes theirs; a pending one is thereby confirmed); `authority` rsi → user
  without an edit is a takeover (`taken_over`), pending → user a confirmation (`reaccepted`); any
  other authority change is refused. Removing an item writes `revoked` (`rejected` for a pending
  one). Adding / restoring an item (not a pending one) that a decision in force blocks writes
  `reaccepted`.
- actor `rsi`: may change or remove only `authority=rsi` items that are not `protected`, never
  change an authority, add only `authority=rsi` items, and never (re-)add an item a decision in
  force blocks
- actor `system`: adds only a seed pack item (origin seed, authority rsi; §7.1 #13) or Memory's
  backfill through S3's land_preference (origin memory_backfill, authority pending; §7.1 #23),
  and changes or removes nothing; the user's confirmation is their own write (actor user) (same rule id, or a near-duplicate text in an overlapping scope:
  `rules.decisions`). Approving an RSI deployment does not change who owns its rules."""

from collections.abc import Callable

from evowiki.rules.decisions import NO_DECISIONS, Decisions
from evowiki.teach.contracts import TeachingSpec
from evowiki.teach.rule_authority import Actor, RuleAuthorityError, settle_items
from evowiki.teach.textsim import normalize
from evowiki.wikiapp.events import DecisionVia, RuleDecided

__all__ = ["Actor", "RuleAuthorityError", "check", "settle"]


def settle(base: TeachingSpec, new: TeachingSpec, *, actor: Actor, decisions: Decisions,
           via: DecisionVia = "ui", ref: str | None = None, undo: bool = False,
           new_id: Callable[[str], str] | None = None) -> tuple[TeachingSpec, list[RuleDecided]]:
    """(`new` with its provenance stamped, the user's decision events), or RuleAuthorityError.
    Without `new_id` no events are built (a check only)."""
    spec, pairs = settle_items(base, new, actor=actor, undo=undo,
                               blocks=lambda i: decisions.blocks(i) is not None)
    if new_id is None:
        return spec, []
    return spec, [RuleDecided(decision_id=new_id("rd"), rule_id=i.item_id, rule_rev=i.rev,
                              text=i.text, text_key=normalize(i.text), scope=i.scope,
                              domain=i.domain, action=a, via=via, ref=ref) for i, a in pairs]


def check(base: TeachingSpec, new: TeachingSpec, *, actor: Actor,
          decisions: Decisions = NO_DECISIONS) -> TeachingSpec:
    """`settle` without events: the stamped spec, or RuleAuthorityError."""
    return settle(base, new, actor=actor, decisions=decisions)[0]
