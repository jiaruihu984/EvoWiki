import json
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path

from evowiki.content.graph import ContentGraph
from evowiki.simulator.mathutil import jaccard

DEFAULT_LIBRARY_PATH = Path(__file__).parent / "data" / "frameworks.json"
ACTIVATION_FRACTION = 0.6


@dataclass(frozen=True)
class Frame:
    """One organizing framework: a named group of concepts a learner may think in."""

    id: str
    label: str
    members: tuple[str, ...]

    @property
    def member_set(self) -> frozenset[str]:
        return frozenset(self.members)


@dataclass(frozen=True)
class FrameworkLibrary:
    version: str
    frames: tuple[Frame, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(f.id for f in self.frames)

    def frame(self, frame_id: str) -> Frame:
        for f in self.frames:
            if f.id == frame_id:
                return f
        raise KeyError(frame_id)


def load_framework_library(graph: ContentGraph, path: Path | None = None) -> FrameworkLibrary:
    """Load a framework library, keeping only members present in `graph`.

    Frames left with fewer than two members are dropped. Duplicate frame ids raise ValueError.
    """
    data = json.loads(Path(path or DEFAULT_LIBRARY_PATH).read_text(encoding="utf-8"))
    present = set(graph.concept_ids)
    frames: list[Frame] = []
    seen: set[str] = set()
    for raw in data["frames"]:
        if raw["id"] in seen:
            raise ValueError(f"duplicate frame id: {raw['id']}")
        seen.add(raw["id"])
        members = tuple(sorted(set(raw["members"]) & present))
        if len(members) >= 2:
            frames.append(Frame(raw["id"], raw["label"], members))
    return FrameworkLibrary(data["library_version"], tuple(frames))


class LatentOrganization:
    """Hidden organization state of one simulated learner (spec §9.2, simulator construct).

    Frames activate in two ways. A scheduled frame activates only when the learner enters its
    curriculum phase. Any other frame activates once the mean learned fraction of its members
    reaches `activation_fraction`. Scheduled ids missing from the library are ignored.
    """

    def __init__(
        self,
        library: FrameworkLibrary,
        scheduled: Mapping[str, str],
        activation_fraction: float = ACTIVATION_FRACTION,
    ):
        self._library = library
        self._scheduled = {f: p for f, p in scheduled.items() if f in library.ids}
        self._fraction = activation_fraction
        self._active: list[str] = []

    @property
    def active_frames(self) -> tuple[str, ...]:
        return tuple(self._active)

    def on_phase(self, phase: str) -> list[str]:
        new = [f for f, p in sorted(self._scheduled.items()) if p == phase and f not in self._active]
        self._active.extend(new)
        return new

    def on_progress(self, learned_fraction: Callable[[str], float]) -> list[str]:
        new: list[str] = []
        for frame in self._library.frames:
            if frame.id in self._active or frame.id in self._scheduled:
                continue
            mean = sum(learned_fraction(m) for m in frame.members) / len(frame.members)
            if mean >= self._fraction:
                self._active.append(frame.id)
                new.append(frame.id)
        return new

    def frame_for(self, concept_id: str) -> Frame | None:
        """The most recently activated frame containing `concept_id`, if any."""
        for fid in reversed(self._active):
            frame = self._library.frame(fid)
            if concept_id in frame.member_set:
                return frame
        return None

    def best_overlap(self, members: AbstractSet[str]) -> float:
        """Max Jaccard overlap between `members` and any active frame (0.0 if none active)."""
        return max(
            (jaccard(members, self._library.frame(f).member_set) for f in self._active),
            default=0.0,
        )
