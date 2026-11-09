"""The case format: one YAML list per category under `cases/` (cases/*.yaml), plus local files for what
may not enter the repo (a lecture's pages and their gold answers: `local: <name>` reads
the configured data directory's local/<name>.yaml and merges it over the case; missing file = the case is reported
as not run, never passed).

A case:
  id          unique
  kind        act | no_act | impossible | control | canary (see README)
  topic       the subject, what a held-out split keeps together
  source      where the failure or the utterance came from: {ref, failed?: verbatim bad output}
  world       a world (worlds.py), cloned per trial
  fault       {tool, type}: tool-failure injection (backend.py)
  steps       what the user does, in order (driver.py)
  grade       [{<grader>: {args}}], every one must hold on the trial's record (graders.py)

Hashes: `case_hash` covers the case files and the worlds they use, `grader_hash` the grading code; a
run records both, and a comparison across different hashes says so."""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from evowiki.harness.evalset_paths import data_dir, sealed_dir

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
DATA = data_dir()
LOCAL = DATA / "local"
SEALED = sealed_dir()
CATEGORIES = {"claims": ("说到做到", 3), "grounding": ("没看见不编造", 3),
              "integrity": ("不损坏内容", 2), "intent": ("该讲还是该动手", 3),
              "tutoring": ("讲解和辅导", 2), "evoui": ("EvoUI", 1), "memory": ("记忆", 2), "decompose": ("拆解路径", 2), "review": ("题目和复习", 2),
              }
FIXTURE_CATEGORIES = {"evoui"}     # cases with a `fixture`: code checks on stored output, no model call
KINDS = {"act", "no_act", "impossible", "control", "canary"}
SEALED_MIN = 8             # a category below this gets no sealed part (proposal §4 says 20, PROCTOR; tutoring has 8)
SEALED_SEED = "sealed-v1"
GRADER_FILES = ("graders.py", "judge.py", "spec.py")
TEST_PORT = int(os.environ.get("EVOWIKI_EVALSET_TEST_PORT", "15433"))
if TEST_PORT not in (15433, 25433):
    raise ValueError("evalset requires the isolated test cluster on port 15433 or 25433")


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    kind: str
    topic: str
    source: dict[str, Any]
    steps: tuple[dict[str, Any], ...]
    grade: tuple[dict[str, Any], ...]
    world: str | None = None
    fault: dict[str, str] | None = None
    missing: str | None = None    # a local file the case needs and this machine lacks
    fixture: dict[str, Any] | None = None    # stored output the graders read (run.py `fixtures`), no trial
    purposes: tuple[str, ...] = field(default=("assistant.turn",))
    language: str = "zh"
    translated_from: str | None = None


def _case(d: dict[str, Any], category: str, where: str, root: Path) -> Case:
    missing = None
    if local := d.pop("local", None):
        p = root / f"{local}.yaml"
        if p.exists():
            d = {**d, **yaml.safe_load(p.read_text(encoding="utf-8"))}
        else:
            missing = str(p)
    for k in ("id", "kind", "topic", "source"):
        if k not in d:
            raise ValueError(f"{where}: case {d.get('id')} has no {k}")
    if d["kind"] not in KINDS:
        raise ValueError(f"{where}: {d['id']}: unknown kind {d['kind']}")
    if d.get("language", "zh") not in ("zh", "en"):
        raise ValueError(f"{where}: unsupported language")
    if not d.get("steps") and not missing and not d.get("fixture"):
        raise ValueError(f"{where}: {d['id']} has no steps")
    return Case(id=d["id"], category=category, kind=d["kind"], topic=d["topic"], source=d["source"],
                steps=tuple(d.get("steps") or ()), grade=tuple(d.get("grade") or ()),
                world=d.get("world"), fault=d.get("fault"), missing=missing, fixture=d.get("fixture"),
                purposes=tuple(d.get("purposes") or ("assistant.turn",)),
                language=d.get("language", "zh"), translated_from=d.get("translated_from"))


def load(*, sealed: bool = False, language: str = "zh") -> list[Case]:
    """Every case. `sealed`: the ones under the configured sealed directory's cases instead; only the
    runner's `--sealed` path reads them, and what it prints is one bit per category (README)."""
    root = SEALED / "cases" if sealed else CASES
    if language not in ("zh", "en"):
        raise ValueError("unsupported case language")
    if language == "en":
        if sealed:
            raise ValueError("English cases are public development regressions, not a sealed set")
        root = root / "en"
    local = SEALED / "local" if sealed else LOCAL
    out: list[Case] = []
    for cat in CATEGORIES:
        p = root / f"{cat}.yaml"
        if p.exists():
            out += [_case(dict(d), cat, str(p), local)
                    for d in yaml.safe_load(p.read_text(encoding="utf-8")) or ()]
    ids = [c.id for c in out]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate case ids: {sorted({i for i in ids if ids.count(i) > 1})}")
    return out


def _sha(parts: list[bytes]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(hashlib.sha256(p).digest())
    return h.hexdigest()[:16]


def case_hash(cases: list[Case], world_hash) -> str:
    rows = sorted(json.dumps(c.__dict__, ensure_ascii=False, sort_keys=True, default=str) for c in cases)
    worlds = sorted({c.world for c in cases if c.world and not c.missing})
    return _sha([r.encode() for r in rows] + [f"{w}:{world_hash(w)}".encode() for w in worlds])


def grader_hash() -> str:
    return _sha([(HERE / f).read_bytes() for f in GRADER_FILES])


def unit_of(d: dict[str, Any]) -> str:
    """The sealing unit of a case (a raw case dict): the subject plus the failure it came from, so the
    variants of one failure on one subject never straddle the line (a dev case that is a sibling of a
    sealed one would leak it). `unit:` in the case overrides."""
    if d.get("unit"):
        return str(d["unit"])
    ref = str((d.get("source") or {}).get("ref", ""))
    head = re.split(r"[：:（(]| 变体", ref)[0].strip()
    return f"{d['topic']}|{head}"


def is_sealed_unit(unit: str) -> bool:
    """Where the unit goes when a category is cut: the same hash bucket as the offline split
    (`harness.offline.evalset._bucket`: a pure function of the unit and seed, so it never moves). About
    15% of the units go to sealed."""
    from evowiki.harness.offline.evalset import _bucket
    return _bucket(unit, SEALED_SEED) < 0.15
