"""RSI persistence surface (architecture spec §7.5–7.6): Policy Registry rows, patches, Patch Memory,
cycle log and per-user settings. `RsiStore` is the protocol; `MemoryRsiStore` is the dict-backed
implementation used by tests and dev mode. `SpecStore` is R1's teaching-spec store protocol;
`MemorySpecStore` stands in for it until R1 merges."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from evowiki.teach.contracts import (
    Patch,
    PatchMemoryEntry,
    PolicyVersion,
    RsiSettings,
    TeachingSpec,
    Track,
)


@dataclass(frozen=True)
class PatchRecord:
    user_id: str
    patch: Patch
    cutoff: datetime | None                     # channel A: outcomes after this are the held-out split
    gate_runs: int = 0
    last_gate_outcomes: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True)
class MemoryRecord:
    """A Patch Memory row. Track B and track-A fit rows use direction 0 and from/to None."""

    user_id: str
    entry: PatchMemoryEntry
    direction: int                               # +1 / -1 / 0 (track B)
    from_value: float | None
    to_value: float | None
    counts_at: datetime                          # when entry.online_* were last decayed
    repeat_of_rejected: bool
    outcomes_at_close: int                       # this user's outcome count when the patch closed
    complexity: int = 1                          # complexity the patch adds (see below)

    # complexity — track A: number of InferParams paths the patch changes (1 for a set_param);
    # track B: |TeachingSpec.complexity() after − before|, floored at 1. Filled by whoever closes the patch.

    def __post_init__(self) -> None:
        if self.direction not in (-1, 0, 1):
            raise ValueError(f"direction must be -1, 0 or +1, got {self.direction!r}")
        if self.complexity < 0:
            raise ValueError(f"complexity must be ≥ 0, got {self.complexity!r}")


@dataclass(frozen=True)
class CycleRecord:
    user_id: str
    at: datetime
    trigger: str
    result: dict
    cost_usd: float


class RsiStore(Protocol):
    def versions(self, user_id: str, track: Track) -> list[PolicyVersion]: ...
    def add_version(self, user_id: str, v: PolicyVersion) -> None: ...
    def patches(self, user_id: str) -> list[PatchRecord]: ...
    def put_patch(self, rec: PatchRecord) -> None: ...
    def memory(self, user_id: str) -> list[MemoryRecord]: ...
    def put_memory(self, rec: MemoryRecord) -> None: ...
    def cycles(self, user_id: str) -> list[CycleRecord]: ...
    def add_cycle(self, rec: CycleRecord) -> None: ...
    def settings(self, user_id: str) -> RsiSettings: ...
    def put_settings(self, user_id: str, s: RsiSettings) -> None: ...


class MemoryRsiStore:
    """Dict-backed `RsiStore`. Versions are append-only and gap-free per (user, track): the next row
    must be exactly last + 1 (the first must be 1), so a duplicate or a skipped number raises."""

    def __init__(self) -> None:
        self._versions: dict[tuple[str, Track], list[PolicyVersion]] = {}
        self._patches: dict[str, dict[str, PatchRecord]] = {}
        self._memory: dict[str, dict[str, MemoryRecord]] = {}
        self._cycles: dict[str, list[CycleRecord]] = {}
        self._settings: dict[str, RsiSettings] = {}

    def versions(self, user_id: str, track: Track) -> list[PolicyVersion]:
        return list(self._versions.get((user_id, track), []))

    def add_version(self, user_id: str, v: PolicyVersion) -> None:
        rows = self._versions.setdefault((user_id, v.track), [])
        expected = rows[-1].version + 1 if rows else 1
        if v.version != expected:
            raise ValueError(f"{user_id}/{v.track}: version {v.version} is not the next ({expected})")
        rows.append(v)

    def patches(self, user_id: str) -> list[PatchRecord]:
        return sorted(self._patches.get(user_id, {}).values(), key=lambda r: r.patch.created_at)

    def put_patch(self, rec: PatchRecord) -> None:
        self._patches.setdefault(rec.user_id, {})[rec.patch.patch_id] = rec

    def memory(self, user_id: str) -> list[MemoryRecord]:
        return list(self._memory.get(user_id, {}).values())

    def put_memory(self, rec: MemoryRecord) -> None:
        self._memory.setdefault(rec.user_id, {})[rec.entry.patch_id] = rec

    def cycles(self, user_id: str) -> list[CycleRecord]:
        return list(self._cycles.get(user_id, []))

    def add_cycle(self, rec: CycleRecord) -> None:
        self._cycles.setdefault(rec.user_id, []).append(rec)

    def settings(self, user_id: str) -> RsiSettings:
        return self._settings.get(user_id, RsiSettings())

    def put_settings(self, user_id: str, s: RsiSettings) -> None:
        self._settings[user_id] = s


class SpecStore(Protocol):
    def get_spec(self, user_id: str) -> TeachingSpec: ...
    def put_spec(self, user_id: str, spec: TeachingSpec) -> None: ...


class MemorySpecStore:
    """Stand-in for R1's spec store; a user with nothing stored gets `TeachingSpec(version=1, items=())`."""

    def __init__(self) -> None:
        self._specs: dict[str, TeachingSpec] = {}

    def get_spec(self, user_id: str) -> TeachingSpec:
        return self._specs.get(user_id, TeachingSpec(version=1, items=()))

    def put_spec(self, user_id: str, spec: TeachingSpec) -> None:
        self._specs[user_id] = spec
