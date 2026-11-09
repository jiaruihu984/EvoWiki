"""Cases that grade stored output instead of a live trial (categories EvoUI and 题目和复习): the case's
`fixture` holds the output (a template name from `deep/evoui/examples.json`, or a text), the grade
list names checks from here or from graders.py. No model is called; cost $0.

EvoUI, three layers (proposal §6): (1) code: the block validates and renders without error in the
product's own headless Chromium, (2) replay: every probe (a control set to two values) changes what
the page shows, the check the product runs when a block is written (`deep.render.PlaywrightChecker`),
(3) human labels: `files/evoui_labels.yaml`, empty until the user fills it, never graded by a model.

Needs Playwright with Chromium in the current environment:
  uv run --no-sync python -m tests.llm_cases.evalset.run fixtures"""

import json
import time
from pathlib import Path
from typing import Any

from tests.llm_cases.evalset import graders, spec
from tests.llm_cases.evalset.graders import Check

EXAMPLES = Path(__file__).resolve().parents[3] / "src" / "evowiki" / "deep" / "evoui" / "examples.json"
_checker: Any = None


def template(name: str) -> dict:
    for e in json.loads(EXAMPLES.read_text(encoding="utf-8")):
        if e["name"] == name:
            return e["block"]
    raise KeyError(f"no template {name!r}")


def block_renders(block: dict) -> Check:
    """Valid by the Python rules, renders, and every probe moves something."""
    global _checker
    from evowiki.deep.evoui.contracts import EvoUIBlock
    from evowiki.deep.evoui.validate import validate
    from evowiki.deep.render import PlaywrightChecker

    problems = validate(block)
    if problems:
        return Check("block_renders", False, f"校验不过：{problems[:2]}")
    _checker = _checker or PlaywrightChecker()
    r = _checker.check_block("fixture", EvoUIBlock.model_validate(block))
    ok = r.ok and r.probes_passed == r.probes_total
    return Check("block_renders", ok, f"渲染{'通过' if r.ok else '失败'}，探针 {r.probes_passed}/{r.probes_total}；"
                 f"{r.detail if not ok else ''}{list(r.page_errors)[:2] if r.page_errors else ''}")


def grade_fixture(case: spec.Case) -> list[Check]:
    fx = case.fixture or {}
    out: list[Check] = []
    for g in case.grade:
        (name, args), = g.items()
        if name == "block_renders":
            block = template(fx["template"]) if "template" in fx else fx["block"]
            out.append(block_renders(block))
        elif name == "question_consistent":
            out.append(graders.question_consistent({}, text=fx["text"], **(args or {})))
        else:
            raise ValueError(f"{case.id}: {name} is not a fixture check")
    return out


def run_all(*, sealed: bool = False) -> int:
    cases = [c for c in spec.load(sealed=sealed) if c.fixture]
    bad, t0 = 0, time.monotonic()
    for c in cases:
        checks = grade_fixture(c)
        ok = graders.passed(checks)
        bad += not ok
        print(f"{'通过' if ok else '不通过'}  {c.category}/{c.id}  " + "; ".join(x.basis for x in checks if not x.ok))
    print(f"{len(cases) - bad}/{len(cases)} 通过，{time.monotonic() - t0:.1f} 秒，模型调用 0，花费 $0")
    return 1 if bad else 0
