"""Deterministic page skeleton composition from segment metadata (spec §3.3 principle 5)."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from evowiki.content.models import Lang, SegmentKind
from evowiki.content.segments import SegmentIndex
from evowiki.render.plan import SECTION_KINDS, Expandable
from evowiki.render.theta import ThetaRender

# Minimum resolution level at which a section renders inline. One level below it the
# section is offered as an expandable block; further below it is omitted.
# `advanced` is never level-driven: it is always offered as an expandable block.
SHOW_LEVEL: Final[Mapping[SegmentKind, int | None]] = MappingProxyType({
    SegmentKind.ONE_LINE: 0,
    SegmentKind.DEFINITION: 1,
    SegmentKind.INTUITION: 2,
    SegmentKind.EXAMPLE: 2,
    SegmentKind.DERIVATION: 3,
    SegmentKind.MISCONCEPTION_FIX: 3,
    SegmentKind.ADVANCED: None,
})

LEVEL_DRIVEN: Final[tuple[SegmentKind, ...]] = tuple(
    k for k in SECTION_KINDS if SHOW_LEVEL[k] is not None
)


@dataclass(frozen=True)
class Skeleton:
    segments_selected: tuple[str, ...]
    expandables: tuple[Expandable, ...]


def compose_adaptive(
    concept_id: str,
    levels: Mapping[SegmentKind, int],
    segments: SegmentIndex,
    lang: Lang,
    theta: ThetaRender,
) -> Skeleton:
    """Sections in θ.order. A shown section contributes its first segment; the example section
    contributes up to θ.max_examples segments at level 4 and one otherwise."""
    selected: list[str] = []
    expandables: list[Expandable] = []
    for kind in theta.order:
        segs = segments.renderable(concept_id, kind, lang)
        if not segs:
            continue
        show = SHOW_LEVEL[kind]
        if show is None:
            expandables.append(Expandable(segment_id=segs[0].id, concept_id=concept_id,
                                          role="section"))
            continue
        level = levels[kind]
        if level >= show:
            n = theta.max_examples if kind is SegmentKind.EXAMPLE and level == 4 else 1
            selected.extend(s.id for s in segs[:n])
        elif level == show - 1:
            expandables.append(Expandable(segment_id=segs[0].id, concept_id=concept_id,
                                          role="section"))
    return Skeleton(tuple(selected), tuple(expandables))


def compose_static(
    concept_id: str, segments: SegmentIndex, lang: Lang, theta: ThetaRender
) -> Skeleton:
    """Canonical full page: every approved segment of every section, in θ.order."""
    selected = [
        s.id for kind in theta.order for s in segments.renderable(concept_id, kind, lang)
    ]
    return Skeleton(tuple(selected), ())
