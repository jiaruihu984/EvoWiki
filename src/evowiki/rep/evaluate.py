"""θ_rep¹ evaluation: firing, candidate ViewOps, and the Stage I-B conflict order
(fire → coalesce identical → priority → specificity → incompatible tie = no-op)."""

from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass, replace
from typing import Literal

from evowiki.rep.dsl import (
    CreateHubAction,
    GroupSubject,
    RepRule,
    SetPerspectiveAction,
    SetRoleAction,
)
from evowiki.rep.theta import ThetaRep
from evowiki.view.features import (
    ConceptFeatures,
    FeatureContext,
    candidate_groups,
    cohesion,
    group_m,
)
from evowiki.view.perspective import select_default
from evowiki.view.state import ViewState

Subject = str | frozenset[str]


@dataclass(frozen=True)
class Firing:
    rule: RepRule
    subject: Subject


@dataclass(frozen=True)
class Candidate:
    slot: tuple[str, ...]
    value: Hashable
    rule_ids: tuple[str, ...]
    priority: int
    specificity: int
    propose: bool
    subject: Subject


@dataclass(frozen=True)
class Resolution:
    slot: tuple[str, ...]
    outcome: Literal["won", "conflict_noop"]
    winner_rule_ids: tuple[str, ...]
    loser_rule_ids: tuple[str, ...]


def _group_value(name: str, group: frozenset[str], ctx: FeatureContext) -> float:
    """Group features: cohesion comes from the group-level function (ConceptFeatures.cohesion is
    a per-concept placeholder and is never used for H1)."""
    if name == "cohesion":
        return cohesion(group, ctx)
    if name == "m":
        return group_m(group, ctx)
    raise KeyError(f"feature {name} is not defined for groups")


def evaluate(theta: ThetaRep, feats: dict[str, ConceptFeatures],
             ctx: FeatureContext) -> list[Firing]:
    out: list[Firing] = []
    for rule in theta.rules:
        if isinstance(rule.subject, GroupSubject):
            for grp in candidate_groups(ctx, rule.subject.min_size):
                if all(c.test(_group_value(c.feature.value, grp, ctx), theta.params)
                       for c in rule.when):
                    out.append(Firing(rule, grp))
            continue
        for cid in sorted(feats):
            if all(c.test(feats[cid].value(c.feature.value), theta.params) for c in rule.when):
                out.append(Firing(rule, cid))
    return out


def candidates(firings: list[Firing], feats: dict[str, ConceptFeatures],
                  ctx: FeatureContext, view: ViewState) -> list[Candidate]:
    out: list[Candidate] = []
    for fr in firings:
        base = {"rule_ids": (fr.rule.id,), "priority": fr.rule.priority,
                "specificity": fr.rule.specificity, "propose": fr.rule.propose,
                "subject": fr.subject}
        for a in fr.rule.then:
            if isinstance(a, SetRoleAction):
                anchor = getattr(feats[fr.subject], a.anchor) if a.anchor else None
                if a.anchor and anchor is None:
                    continue                          # no grounded anchor: nothing to set
                out.append(Candidate(slot=("SET_ROLE", fr.subject), value=(a.role, anchor),
                                     **base))
            elif isinstance(a, SetPerspectiveAction):
                ch = select_default(fr.subject, ctx,
                                    view.concept(fr.subject).perspective_segment_id)
                if ch.segment_id is not None:
                    out.append(Candidate(slot=("SET_PERSPECTIVE", fr.subject),
                                         value=ch.segment_id, **base))
            elif isinstance(a, CreateHubAction):
                members = tuple(sorted(fr.subject))
                out.append(Candidate(slot=("CREATE_HUB", *members), value=members, **base))
            # ATTACH / SURFACE_LINK: no θ_rep¹ initial rule uses them; RSI-B adds materializers.
    return out


def coalesce(cands: list[Candidate]) -> list[Candidate]:
    groups: dict[tuple, list[Candidate]] = defaultdict(list)
    for c in cands:
        groups[(c.slot, c.value)].append(c)
    out = []
    for grp in groups.values():
        top = max(grp, key=lambda c: (c.priority, c.specificity))
        out.append(replace(top, rule_ids=tuple(sorted({r for c in grp for r in c.rule_ids})),
                           propose=any(c.propose for c in grp)))
    return sorted(out, key=lambda c: (c.slot, str(c.value)))


def resolve(cands: list[Candidate]) -> tuple[list[Candidate], list[Resolution]]:
    """Coalescing already merged identical values, so any candidate still tied with the top at
    the same (priority, specificity) has a different value: an incompatible tie is a no-op."""
    by_slot: dict[tuple, list[Candidate]] = defaultdict(list)
    for c in cands:
        by_slot[c.slot].append(c)
    won: list[Candidate] = []
    res: list[Resolution] = []
    for slot in sorted(by_slot):
        ranked = sorted(by_slot[slot], key=lambda c: (-c.priority, -c.specificity, c.rule_ids))
        top = ranked[0]
        losers = tuple(r for c in ranked[1:] for r in c.rule_ids)
        if any((c.priority, c.specificity) == (top.priority, top.specificity)
               for c in ranked[1:]):
            res.append(Resolution(slot, "conflict_noop", (), top.rule_ids + losers))
            continue
        won.append(top)
        res.append(Resolution(slot, "won", top.rule_ids, losers))
    return won, res
