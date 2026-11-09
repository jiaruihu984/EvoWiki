"""One recorded-time boundary for evaluation inputs and their full source closure."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SnapshotBoundary:
    cutoff: datetime
    excluded_sources: frozenset[str] = frozenset()

    def allows(self, recorded_at: datetime | None, sources: Iterable[str]) -> bool:
        """Missing recording time cannot establish historical visibility.

        Callers supply the complete generation dependency closure, not just display citations.
        Current forgetting remains a separate mandatory check at the point of consumption.
        """
        return (recorded_at is not None and recorded_at <= self.cutoff
                and not self.excluded_sources.intersection(sources))


def recorded_at(conn, user_id: str, relation: str, record_id: str) -> datetime | None:
    """Database recording time of the current source version; unknown history stays unavailable."""
    from evowiki.store.db import fetch_one

    row = fetch_one(conn, "select recorded_at from evaluation_record_clock where user_id=%s "
                         "and relation=%s and record_id=%s", (user_id, relation, record_id))
    return row['recorded_at'] if row else None
