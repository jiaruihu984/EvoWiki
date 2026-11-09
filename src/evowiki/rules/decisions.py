"""The user's rule decisions as the checkpoint and the guard read them (contract §1.2, §6 clause 1).

A decision is in force when it is the last one on its rule (`rule_id`), it is `rejected` or
`revoked`, and no later `reaccepted` / `taken_over` has a near-duplicate text. The old rejection
texts (`WikiState.rejected_guidelines`, written before `RuleDecided` existed and not migrated yet)
count as rejections of any scope. Delete ≠ reject: a decision only blocks reviving the same rule
(same id) or a near-duplicate text in an overlapping scope; anything merely similar goes to the
guard's semantic check (`rules.guard`). Pure."""

from collections.abc import Sequence
from dataclasses import dataclass

from evowiki.teach.contracts import SpecItem, SpecScope
from evowiki.teach.textsim import near_duplicate
from evowiki.wikiapp.events import RuleDecided

BLOCKING = ("rejected", "revoked")
CLEARING = ("reaccepted", "taken_over")


def scopes_overlap(a: SpecScope, a_domain: str | None, b: SpecScope, b_domain: str | None) -> bool:
    """Two rule scopes meet: either is `any` or they are equal, and either has no domain or the
    domains are the same (casefold, stripped)."""
    if a != "any" and b != "any" and a != b:
        return False
    if a_domain is None or b_domain is None:
        return True
    return a_domain.strip().casefold() == b_domain.strip().casefold()


@dataclass(frozen=True)
class Blocked:
    """What an automatic re-add would revive: a recorded decision, or an old rejection text."""

    text: str
    decision: RuleDecided | None = None


@dataclass(frozen=True)
class Decisions:
    records: tuple[RuleDecided, ...] = ()
    legacy_texts: tuple[str, ...] = ()

    @classmethod
    def of(cls, state) -> "Decisions":
        return cls(tuple(state.rule_decisions), tuple(state.rejected_guidelines))

    def active(self) -> tuple[RuleDecided, ...]:
        last: dict[str, int] = {}
        for n, d in enumerate(self.records):
            last[d.rule_id] = n
        out = []
        for n in sorted(last.values()):
            d = self.records[n]
            if d.action not in BLOCKING:
                continue
            if any(r.action in CLEARING and near_duplicate(r.text, d.text)
                   for r in self.records[n + 1:]):
                continue
            out.append(d)
        return tuple(out)

    def legacy_active(self) -> tuple[str, ...]:
        cleared = [r.text for r in self.records if r.action in CLEARING]
        return tuple(t for t in self.legacy_texts
                     if not any(near_duplicate(c, t) for c in cleared))

    def blocks(self, item: SpecItem) -> Blocked | None:
        """The decision an automatic (re-)add of `item` would go against: the same rule id, or a
        near-duplicate text whose scope overlaps."""
        for d in self.active():
            if d.rule_id == item.item_id or (
                    near_duplicate(d.text, item.text)
                    and scopes_overlap(d.scope, d.domain, item.scope, item.domain)):
                return Blocked(d.text, d)
        for t in self.legacy_active():
            if near_duplicate(t, item.text):
                return Blocked(t)
        return None

    def texts(self) -> Sequence[tuple[str, SpecScope, str | None]]:
        """(text, scope, domain) of every decision in force: what the guard compares against."""
        return ([(d.text, d.scope, d.domain) for d in self.active()]
                + [(t, "any", None) for t in self.legacy_active()])


NO_DECISIONS = Decisions()
