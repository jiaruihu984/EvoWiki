"""Patch Memory (architecture spec §7.6): retrieval, decayed online counts and recursion metrics.

Counts decay with a 14-day half-life, so one noisy outcome never weighs on a patch forever (Stage 0
HB lesson). Track A no longer has a re-proposal check: its numbers come from the A1 fitter, not
from a proposer that could repeat itself, and a fit that changes nothing is never recorded."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime

from evowiki.teach.contracts import Track
from evowiki.teach.rsi_store import MemoryRecord

HALF_LIFE_DAYS = 14.0


def decayed(value: float, since: datetime, now: datetime) -> float:
    """`value` halved every HALF_LIFE_DAYS from `since` to `now`; a `now` before `since` never grows it."""
    days = max(0.0, (now - since).total_seconds() / 86400.0)
    return value * 0.5 ** (days / HALF_LIFE_DAYS)


def bump(rec: MemoryRecord, *, helpful: float = 0, harmful: float = 0, now: datetime) -> MemoryRecord:
    """Decay the record's online counts to `now`, then add the new evidence. A `now` before `counts_at`
    (clock skew) leaves `counts_at` where it is, so later decay is not over-counted."""
    if helpful < 0 or harmful < 0:
        raise ValueError("bump amounts must be ≥ 0")
    e = rec.entry
    entry = e.model_copy(update={
        "online_helpful": decayed(e.online_helpful, rec.counts_at, now) + helpful,
        "online_harmful": decayed(e.online_harmful, rec.counts_at, now) + harmful})
    return replace(rec, entry=entry, counts_at=max(now, rec.counts_at))


def signature(failure: str, path: str, domain: str | None) -> str:
    """"failure|path|domain"; domain None (every domain) is "*"."""
    return f"{failure}|{path}|{domain if domain is not None else '*'}"


def _failure_of(rec: MemoryRecord) -> str:
    return rec.entry.signature.split("|", 1)[0]


def similar(records: Iterable[MemoryRecord], *, track: Track, failure: str, path: str | None, now: datetime,
            k: int = 5) -> list[MemoryRecord]:
    """Up to k same-track records: same signature (failure|path, any domain — the caller has no domain
    here) first, then same path, then same failure; newest first within each tier. Records dated after
    `now` are skipped; records matching none of the three are left out."""
    prefix = f"{failure}|{path}|" if path is not None else None
    tiers: list[list[MemoryRecord]] = [[], [], []]
    for r in records:
        if r.entry.track != track or r.entry.at > now:
            continue
        if prefix is not None and r.entry.signature.startswith(prefix):
            tiers[0].append(r)
        elif path is not None and r.entry.path == path:
            tiers[1].append(r)
        elif _failure_of(r) == failure:
            tiers[2].append(r)
    out: list[MemoryRecord] = []
    for t in tiers:
        out.extend(sorted(t, key=lambda r: r.entry.at, reverse=True))
    return out[:k]


@dataclass(frozen=True)
class RecursionMetrics:
    proposals: int
    accepted: int
    proposal_efficiency: float | None            # ΣΔU(accepted) / proposals
    complexity_efficiency: float | None          # ΣΔU(accepted) / Σ patch complexity (accepted)
    repeat_rejected_rate: float | None           # repeat_of_rejected / proposals


def recursion_metrics(records: Iterable[MemoryRecord], track: Track | None = None) -> RecursionMetrics:
    """proposals = records (of `track`, or all). accepted counts personal_accepted plus rolled_back (only
    an accepted patch can be rolled back); ΔU and complexity sum over personal_accepted only, so a
    rolled-back patch's ΔU never counts. Complexity is the recorded `MemoryRecord.complexity`. Records are
    written when a patch closes, so every record is a finished proposal. Rates are None when their
    denominator is 0."""
    rs = [r for r in records if track is None or r.entry.track == track]
    live = [r for r in rs if r.entry.status == "personal_accepted"]
    accepted = len(live) + sum(1 for r in rs if r.entry.status == "rolled_back")
    du = sum(r.entry.delta_utility or 0.0 for r in live)
    cx = sum(r.complexity for r in live)
    n = len(rs)
    return RecursionMetrics(
        proposals=n, accepted=accepted,
        proposal_efficiency=du / n if n else None,
        complexity_efficiency=du / cx if cx else None,
        repeat_rejected_rate=sum(r.repeat_of_rejected for r in rs) / n if n else None)
