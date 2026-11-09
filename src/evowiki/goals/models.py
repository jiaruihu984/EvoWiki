"""The K band of a facet or concept, used by the organizer's deterministic fallback outline.
Pure data: no IO, no LLM."""

from collections.abc import Iterable
from enum import StrEnum


class KStatus(StrEnum):
    """K band of one facet, from (m, u, R) alone (rev 2.1 decision 1)."""

    MASTERED = "mastered"    # m high and certain → anchor
    LIKELY = "likely"        # m fairly high but uncertain (e.g. one right answer) → anchor_check
    PARTIAL = "partial"      # medium, or forgotten → refresher
    UNKNOWN = "unknown"      # low → expand


_WEAKEST_FIRST = (KStatus.UNKNOWN, KStatus.PARTIAL, KStatus.LIKELY)


def aggregate(statuses: Iterable[KStatus]) -> KStatus:
    """MASTERED only if every status is; otherwise the weakest (UNKNOWN < PARTIAL < LIKELY)."""
    seen = set(statuses)
    if seen == {KStatus.MASTERED}:
        return KStatus.MASTERED
    return next(st for st in _WEAKEST_FIRST if st in seen)
