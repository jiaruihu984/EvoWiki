"""Which cases of the evaluation set speak to a round's feedback, from the data, with no keyword table: a
case whose grade or steps name a tool the change touches or the complained-about turn called, and the cases
nearest to what he said (character bigrams against the case's own utterances). A few cases of no_act and
control kinds go along, picked by hash, so a change that makes the assistant act where it must not shows.

No related case means the set has nothing to say: the caller reports too little evidence, never a pass.
The sealed set is not read here (`tests/llm_cases/evalset/spec.py`, its own folder)."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from evowiki.teach.textsim import jaccard

CASES = Path("tests/llm_cases/evalset/cases")
RELATED = 12
CONTROLS = 6
NEAR = 0.1          # bigrams shared with an utterance of the case, below which it is about something else
CONTROL_KINDS = ("no_act", "control")


@dataclass(frozen=True)
class Feedback:
    """What a round knows of the turns he complained about: his words and the tools involved."""

    said: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


def _load(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted((root / CASES).glob("*.yaml")):
        out += [c for c in yaml.safe_load(f.read_text(encoding="utf-8")) or () if c.get("steps") and not c.get("local")]
    return out


def _says(case: Mapping[str, Any]) -> list[str]:
    return [str(s["say"]) for s in case["steps"] if s.get("say")]


def _names_a_tool(case: Mapping[str, Any], tools: Sequence[str]) -> bool:
    words = json.dumps([case.get("grade"), case["steps"]], ensure_ascii=False)
    return any(t in words for t in tools)


def relevant(root: Path, fb: Feedback) -> tuple[str, ...]:
    """The case ids for `fb` (module doc); () when no case is related. A tool many cases name (read_page) says little:
    it counts by how rare it is, and his words count as they are."""
    cases = _load(root)
    near = {c["id"]: max((jaccard(x, y) for x in fb.said for y in _says(c)), default=0.0) for c in cases}
    naming = {t: [c["id"] for c in cases if _names_a_tool(c, (t,))] for t in fb.tools}
    score = {i: s if s >= NEAR else 0.0 for i, s in near.items()}
    for ids in naming.values():
        for i in ids:
            score[i] += 1 / len(ids)
    related = sorted((i for i, s in score.items() if s > 0), key=lambda i: (-score[i], i))[:RELATED]
    if not related:
        return ()
    rest = [c["id"] for c in cases if c["id"] not in related and c.get("kind") in CONTROL_KINDS]
    rest.sort(key=lambda i: hashlib.sha256(i.encode("utf-8")).hexdigest())
    return (*related, *rest[:CONTROLS])
