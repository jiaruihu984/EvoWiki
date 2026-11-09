"""Gold-standard coverage of a goal decomposition (README.md in this folder).

A gold goal is a set of expert prerequisite concepts from a validated source (Metacademy,
LectureBank): the goal's anchor concepts plus their transitive prerequisite closure in the
source graph, trimmed by one rule (`core_of`). `coverage(names, gold)` scores a decomposition's
concept / region names against it: a gold concept is matched when one of its curated aliases,
normalized, is a substring of a normalized name, and that name contains none of the concept's
`exclude` strings (so 「梯度下降」 does not count as the concept 梯度 and 「支持向量机」 not as 向量).
Besides the overall `score`, the two parts are scored apart: `core_score` over the goal's own
topics (the anchors, role "target") and `prereq_score` over what must be known before it.
Pure: no LLM, no I/O except `load_goal` reading a fixture."""

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
# LectureBank has no license (README asks only for a citation): its excerpt is never committed
# (user ruling 2026-10-11); build_gold.py builds it from a local clone and its checks skip without it.
LECTUREBANK = "lecturebank_ml.json"
HAS_LECTUREBANK = (HERE / LECTUREBANK).exists()
# a prerequisite is core when at least this fraction of the goal's anchors needs it (transitively)
SUPPORT_FRACTION = 0.25
# The live ML-goal floor per fixture (README.md "Threshold"): the offline reference decomposition
# (tests/llm_cases/test_gold_coverage.py REFERENCE_ML) clears it with margin, a regions-only
# outline does not. Metacademy is finer-grained (42 core concepts, 12 of them course internals such
# as 「四个基本子空间」 that a goal-level decomposition need not name) than LectureBank (17).
MIN_ML_COVERAGE = {"metacademy_ml.json": 0.35, "lecturebank_ml.json": 0.5}
# The two parts are judged separately too (user ruling 2026-10-11, README.md "Threshold"): the
# goal's own core topics (its anchors, `core_score`) and what must be known before it
# (`prereq_score`). The reference clears both with margin; its math-only half fails the core floor
# and its core-only half fails the prereq floor, so listing one part well never hides the other.
MIN_ML_CORE = {"metacademy_ml.json": 0.5, "lecturebank_ml.json": 0.5}
MIN_ML_PREREQ = {"metacademy_ml.json": 0.25, "lecturebank_ml.json": 0.5}
_DROP = re.compile(r"[\s\-_·・,，、.。:：;；()（）\[\]【】「」『』'\"“”‘’/／+&]")


def normalize(text: str) -> str:
    """NFKC, casefold, no whitespace or punctuation: 「K-Means」 → 「kmeans」, 「（ＰＣＡ）」 → 「pca」."""
    return _DROP.sub("", unicodedata.normalize("NFKC", text).casefold())


def support_min(n_anchors: int) -> int:
    """How many anchors must need a prerequisite for it to be core (≥ 1)."""
    return max(1, round(n_anchors * SUPPORT_FRACTION))


def core_of(edges: Mapping[str, Sequence[str]], anchors: Sequence[str], k: int) -> list[str]:
    """The trimming rule: the anchors, then every concept in their prerequisite closure that at
    least `k` anchors reach (`edges`: concept → its direct prerequisites). Order: anchors as
    given, then prerequisites by support (desc) and id. Cycles in the source are tolerated."""
    support: dict[str, int] = {}
    for a in anchors:
        seen, stack = {a}, [a]
        while stack:
            for d in edges.get(stack.pop(), ()):
                if d not in seen:
                    seen.add(d)
                    stack.append(d)
        for d in seen - {a}:
            support[d] = support.get(d, 0) + 1
    prereqs = [d for d in support if d not in anchors and support[d] >= k]
    return list(anchors) + sorted(prereqs, key=lambda d: (-support[d], d))


@dataclass(frozen=True)
class GoldConcept:
    id: str
    name: str                       # English name in the source
    aliases: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    role: str = "prerequisite"      # "target" (an anchor) or "prerequisite"
    field: str | None = None        # the source's foundation course, when it has one


@dataclass(frozen=True)
class GoldGoal:
    source: str
    goal: str
    concepts: tuple[GoldConcept, ...]
    fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)   # field → aliases

    def concept(self, cid: str) -> GoldConcept:
        return next(c for c in self.concepts if c.id == cid)


@dataclass(frozen=True)
class Coverage:
    score: float                               # matched / gold core concepts
    core_score: float                          # the same over the goal's own topics (anchors)
    prereq_score: float                        # the same over the prerequisite (non-anchor) part
    matched: dict[str, tuple[str, ...]]        # gold id → the decomposition names that matched it
    missing: tuple[str, ...]                   # gold ids nobody matched
    missing_names: tuple[str, ...]             # their English names
    fields: dict[str, bool]                    # field → named itself or one of its concepts

    def as_report(self) -> dict:
        return {"score": round(self.score, 3), "core_score": round(self.core_score, 3),
                "prereq_score": round(self.prereq_score, 3),
                "matched": {k: list(v) for k, v in self.matched.items()},
                "missing": list(self.missing_names), "fields": self.fields}


def matches(name: str, concept: GoldConcept) -> bool:
    n = normalize(name)
    if any(normalize(x) in n for x in concept.exclude):
        return False
    return any(normalize(a) in n for a in concept.aliases if normalize(a))


def _share(concepts: Sequence[GoldConcept], matched: Mapping[str, object]) -> float:
    return sum(c.id in matched for c in concepts) / len(concepts) if concepts else 0.0


def node_names(result) -> list[str]:
    """The names a decomposition is scored on: every node of an intake result (intake@v14: areas
    and points are all nodes, `concepts`), plus the derived regions' names (the same nodes; an
    older result listed them apart), each once."""
    return list(dict.fromkeys([*(c.name for c in result.concepts),
                               *(r.name for r in result.regions)]))


def coverage(names: Iterable[str], gold: GoldGoal) -> Coverage:
    names = [x for x in dict.fromkeys(names) if x and normalize(x)]
    matched = {c.id: tuple(x for x in names if matches(x, c)) for c in gold.concepts}
    matched = {k: v for k, v in matched.items() if v}
    missing = tuple(c.id for c in gold.concepts if c.id not in matched)
    prereqs = [c for c in gold.concepts if c.role == "prerequisite"]
    targets = [c for c in gold.concepts if c.role == "target"]
    fields = {}
    for f, aliases in gold.fields.items():
        named = any(normalize(a) in normalize(x) for a in aliases for x in names)
        fields[f] = named or any(c.field == f and c.id in matched for c in gold.concepts)
    return Coverage(
        score=len(matched) / len(gold.concepts) if gold.concepts else 0.0,
        core_score=_share(targets, matched), prereq_score=_share(prereqs, matched),
        matched=matched, missing=missing,
        missing_names=tuple(gold.concept(m).name for m in missing), fields=fields)


def load_goal(fixture: str | Path, goal: str) -> GoldGoal:
    """One goal of a fixture (`metacademy_ml.json` / `lecturebank_ml.json`, or a path)."""
    path = Path(fixture) if Path(fixture).is_absolute() else HERE / fixture
    data = json.loads(path.read_text(encoding="utf-8"))
    g = next((g for g in data["goals"] if g["goal"] == goal), None)
    if g is None:
        raise KeyError(f"{path.name}: no goal {goal!r}")
    anchors = set(g["anchors"])
    concepts = []
    for cid in g["core"]:
        c = data["concepts"][cid]
        if not c.get("aliases"):
            raise ValueError(f"{path.name}: core concept {cid} has no aliases")
        concepts.append(GoldConcept(id=cid, name=c["name"], aliases=tuple(c["aliases"]),
                                    exclude=tuple(c.get("exclude", ())),
                                    role="target" if cid in anchors else "prerequisite",
                                    field=c.get("field")))
    fields = {f: tuple(v["aliases"]) for f, v in data.get("fields", {}).items()
              if f in g.get("fields", ())}                  # the fields this goal's core uses
    return GoldGoal(source=data["source"]["name"], goal=goal, concepts=tuple(concepts),
                    fields=fields)
