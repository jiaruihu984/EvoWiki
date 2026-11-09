from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from evowiki.simulator.frameworks import LatentOrganization
from evowiki.simulator.mathutil import jaccard
from evowiki.simulator.stimulus import PageStimulus

P_ACCEPT_MIN = 0.1
P_ACCEPT_MAX = 0.9
NAV_BRANCH_WEIGHT = 0.1     # λ_b: cost per option scanned at a decision point
NAV_BACKTRACK_WEIGHT = 1.0  # λ_r: cost per backtrack to the hub index
NAV_SEARCH_CLICKS = 4       # fallback search when no hub leads to the target


def page_alignment(page: PageStimulus, latent: LatentOrganization) -> float:
    """Jaccard fit between the page's organizing structure and the learner's active frame.

    The structure is the primary hub's members and/or the perspective's concepts; the best of
    the two counts. No active frame for the concept, or no structure on the page: 0.0.
    """
    frame = latent.frame_for(page.concept_id)
    if frame is None:
        return 0.0
    shown = [s for s in (page.hub_members, page.perspective_concepts) if s]
    return max((jaccard(s, frame.member_set) for s in shown), default=0.0)


def anchoring_factor(
    page: PageStimulus,
    latent: LatentOrganization,
    learned_fraction: Callable[[str], float],
    gain: float,
) -> float:
    """Learning-rate multiplier 1 + gain · alignment · (mean learned fraction of other frame members)."""
    frame = latent.frame_for(page.concept_id)
    if frame is None:
        return 1.0
    others = [m for m in frame.members if m != page.concept_id]
    support = sum(learned_fraction(m) for m in others) / len(others) if others else 0.0
    return 1.0 + gain * page_alignment(page, latent) * support


@dataclass(frozen=True)
class NavigationTrace:
    opened: tuple[str, ...]
    found_via: str | None
    clicks: int
    branching: int
    backtracks: int

    @property
    def cost(self) -> float:
        """C_nav = #clicks + λ_b·#options scanned + λ_r·#backtracks (design decision 13)."""
        return (self.clicks + NAV_BRANCH_WEIGHT * self.branching
                + NAV_BACKTRACK_WEIGHT * self.backtracks)


def navigate(target: str, hubs: Mapping[str, AbstractSet[str]],
             anchor: AbstractSet[str]) -> NavigationTrace:
    """Simulated browsing from the hub index (a simulator construct, spec §9.2 find cost).

    Hubs are opened in order of Jaccard relevance to the reader's anchor (ties: hub id). Each
    visit to the index scans |hubs| options and each opened hub scans its members. The target
    found: one more click. Not found: backtrack. No hub holds it: fall back to search.
    """
    order = sorted(hubs, key=lambda h: (-jaccard(set(hubs[h]), set(anchor)), h))
    clicks = branching = backtracks = 0
    opened: list[str] = []
    for h in order:
        branching += len(hubs)
        clicks += 1
        opened.append(h)
        branching += len(hubs[h])
        if target in hubs[h]:
            return NavigationTrace(tuple(opened), h, clicks + 1, branching, backtracks)
        backtracks += 1
    return NavigationTrace(tuple(opened), None, clicks + NAV_SEARCH_CLICKS, branching,
                           backtracks)


def hub_acceptance_probability(members: AbstractSet[str], latent: LatentOrganization) -> float:
    """P(accept) rises linearly with the best Jaccard overlap against any active frame."""
    return P_ACCEPT_MIN + (P_ACCEPT_MAX - P_ACCEPT_MIN) * latent.best_overlap(members)
