from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from evowiki.simulator.world import SimWorld

# Simulator construct (spec §9.2): how much detail a section kind carries for the reader.
# Written for the simulator, independently of the renderer's display table. Perspective and
# connection segments carry no depth.
DETAIL_RANK: Final[Mapping[str, int]] = MappingProxyType({
    "one_line": 0, "definition": 1, "intuition": 1, "example": 2,
    "misconception_fix": 2, "derivation": 3, "advanced": 4,
})


@dataclass(frozen=True)
class SlotStimulus:
    """One displayed slot as the learner sees it: type, segment ids and visibility only."""

    slot_type: str
    segment_ids: tuple[str, ...]
    visibility: str  # "shown" | "collapsed"


@dataclass(frozen=True)
class PageStimulus:
    """What the simulated learner sees on one page.

    Built by a page policy from observable/content information only, never from hidden state.
    """

    concept_id: str
    refreshers: frozenset[str] = frozenset()
    expandables: frozenset[str] = frozenset()
    misconception_fixes: frozenset[str] = frozenset()
    sections: tuple[str, ...] = ()
    refresher_segments: tuple[tuple[str, str], ...] = ()
    expandable_segments: tuple[tuple[str, str], ...] = ()
    hub_members: frozenset[str] | None = None
    perspective_concepts: frozenset[str] | None = None
    render_plan_id: str | None = None
    page_depth: int | None = None                        # 0..4; None = full canonical depth
    section_ranks: tuple[tuple[str, int], ...] = ()      # detail rank of shown sections
    section_expandables: tuple[tuple[str, int], ...] = ()  # collapsed sections (id, rank)
    probe_item_id: str | None = None                     # micro-quiz the page asks
    qa_enabled: bool = True                              # page Q&A available (False in S0)
    # Displayed slots in page order (Stage I-B attention model). None = legacy page without
    # slots (canonical_page and the Plan 2 tests), read by the legacy model.
    slots: tuple[SlotStimulus, ...] | None = None

    def shown_depth(self) -> int:
        return 4 if self.page_depth is None else self.page_depth

    def refresher_segment(self, concept_id: str) -> str:
        """Segment id of `concept_id`'s refresher; synthetic when the page names none."""
        return dict(self.refresher_segments).get(concept_id, f"{concept_id}:refresher")

    def expandable_segment(self, concept_id: str) -> str:
        """Segment id of `concept_id`'s expandable prerequisite block; synthetic if unnamed."""
        return dict(self.expandable_segments).get(concept_id, f"{concept_id}:prerequisite")

    def section_ids(self) -> tuple[str, ...]:
        """Section segments shown on the page; one synthetic id when the page names none."""
        return self.sections or (f"{self.concept_id}:page",)


def canonical_page(world: SimWorld, concept_id: str) -> PageStimulus:
    """S0-style static page: every direct prerequisite expandable, every misconception fix shown."""
    return PageStimulus(
        concept_id=concept_id,
        expandables=frozenset(p for p, _ in world.graph.direct_prerequisites(concept_id)),
        misconception_fixes=frozenset(m.id for m in world.misconceptions_of_concept(concept_id)),
    )
