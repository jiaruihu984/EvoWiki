"""Anti-circularity guard (spec §7.7, §9.4): the simulator never sees the system's model or L4 code."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from evowiki.simulator.render_adapter import PLAN_FIELDS_READ, SLOT_FIELDS_READ

SRC = Path(__file__).resolve().parents[2] / "src"
SIM = SRC / "evowiki" / "simulator"

# Internal modules the simulator may import (prefix match). Everything else under `evowiki` is
# out of bounds: the belief layer, evaluation code that wraps it, and every L4 / proposer module
# later plans add. An allowlist keeps working for module names that do not exist yet.
ALLOWED_INTERNAL = (
    "evowiki.simulator",
    "evowiki.content",
    "evowiki.evidence.models",
    "evowiki.evidence.payloads",
    "evowiki.evaluation.outcomes",
    "evowiki.timeutil",
)
# Package __init__ modules that importing an allowed module necessarily loads.
ALLOWED_PACKAGES = frozenset({"evowiki", "evowiki.evidence", "evowiki.evaluation"})
BANNED_ROOTS = frozenset({"importlib", "time", "secrets"})
BANNED_TEXT = ("__import__", "datetime.now(", ".utcnow(", "date.today(")


def _allowed(name: str) -> bool:
    return name in ALLOWED_PACKAGES or any(
        name == p or name.startswith(p + ".") for p in ALLOWED_INTERNAL
    )


def import_violations(source: str, package: str, filename: str) -> list[str]:
    """Every forbidden import in `source`, a module named `filename` inside `package`."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            names = [module] + [f"{module}.{a.name}" for a in node.names]
        else:
            continue
        for name in names:
            root = name.split(".")[0]
            if (
                (root == "evowiki" and not _allowed(name))
                or root in BANNED_ROOTS
                or (root == "random" and filename != "crn.py")
            ):
                found.append(f"{filename}: {name}")
    return found


def test_simulator_sources_import_only_allowlisted_modules():
    sources = sorted(SIM.rglob("*.py"))
    assert sources, "simulator package is missing"
    found: list[str] = []
    for path in sources:
        package = ".".join(path.relative_to(SRC).with_suffix("").parts[:-1])
        text = path.read_text(encoding="utf-8")
        found += import_violations(text, package, path.name)
        found += [f"{path.name}: {t}" for t in BANNED_TEXT if t in text]
    assert found == []


@pytest.mark.parametrize(
    "source",
    [
        "import evowiki.belief.tracer",
        "from evowiki.belief import theta",
        "from evowiki import belief",
        "from ..belief.tracer import Tracer",
        "from evowiki.render.theta import ThetaRender",
        "from evowiki.rsi.proposer import Proposer",
        "import importlib",
        "import random",
    ],
)
def test_checker_flags_forbidden_imports(source):
    assert import_violations(source, "evowiki.simulator", "learner.py")


def test_checker_accepts_allowed_imports():
    src = (
        "from evowiki.content.graph import ContentGraph\n"
        "from evowiki.evidence.models import Event\n"
        "from evowiki.evaluation.outcomes import QuizOutcome\n"
        "from . import crn\n"
        "import math\n"
    )
    assert import_violations(src, "evowiki.simulator", "learner.py") == []
    assert import_violations("import random", "evowiki.simulator", "crn.py") == []


_PROBE = """
import importlib, json, pkgutil, sys
import evowiki.simulator as sim
for m in pkgutil.walk_packages(sim.__path__, "evowiki.simulator."):
    importlib.import_module(m.name)
print(json.dumps(sorted(n for n in sys.modules if n == "evowiki" or n.startswith("evowiki."))))
"""


def test_transitive_imports_stay_inside_the_allowlist():
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True
    )
    loaded = json.loads(out.stdout)
    assert "evowiki.simulator" in loaded
    assert [m for m in loaded if not _allowed(m)] == []


# Fields that exist only on L3's RenderPlan (spec §4.5). Only the adapter may name them, so the
# simulator never grows a hidden dependency on RenderPlan's internal structure.
RENDER_PLAN_ONLY_FIELDS = ("primary_hub_id", "perspective_segment_id", "segments_selected",
                           "surfaced_links", "resolution_by_section", "outcome_labels",
                           "role_default", "role_requested", "role_effective", "role_source",
                           "role_anchor", "perspective_default", "perspective_effective",
                           "perspective_source", "collapse_blocked", "skip_reason",
                           "k_visibility")


def test_only_the_adapter_reads_render_plan_fields():
    offenders = [
        f"{path.name}: {field}"
        for path in sorted(SIM.rglob("*.py")) if path.name != "render_adapter.py"
        for field in RENDER_PLAN_ONLY_FIELDS
        if field in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


FORBIDDEN_PLAN_FIELDS = ("role_default", "role_requested", "role_effective", "role_source",
                         "role_anchor", "perspective_default", "perspective_effective",
                         "perspective_source", "collapse_blocked", "skip_reason",
                         "k_visibility", "segments_selected", "resolution_by_section",
                         "perspective_segment_id", "surfaced_links", "condition")


def test_adapter_reads_only_whitelisted_fields():
    tree = ast.parse((SIM / "render_adapter.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "stimulus_from_render_plan")
    plan_attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)
                  and isinstance(n.value, ast.Name) and n.value.id == "plan"}
    slot_attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)
                  and isinstance(n.value, ast.Name) and n.value.id == "slot"}
    assert plan_attrs <= PLAN_FIELDS_READ and slot_attrs <= SLOT_FIELDS_READ
    assert not ({*PLAN_FIELDS_READ, *SLOT_FIELDS_READ} & set(FORBIDDEN_PLAN_FIELDS))


def test_simulator_never_names_v_or_condition_fields():
    for path in SIM.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in FORBIDDEN_PLAN_FIELDS[:11]:
            assert name not in text, f"{path.name} mentions {name}"


def test_simulator_imports_no_view_rep_or_render():
    for banned in ("evowiki.view", "evowiki.rep", "evowiki.render"):
        assert not any(a == banned or a.startswith(banned + ".") for a in ALLOWED_INTERNAL)
