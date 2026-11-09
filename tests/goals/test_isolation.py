"""Anti-circularity (personal-wiki plan §5): goals/ reads G, K and the facet catalog only —
never the simulator, experiments, storage or V persistence. Plan rev 3 makes page organisation
an LLM job: only the LLM-calling modules may import the LLM layer; guardrails,
the outline checks and fold stay deterministic and LLM-free."""

import ast
import json
import subprocess
import sys
from pathlib import Path

GOALS = Path(__file__).resolve().parents[2] / "src" / "evowiki" / "goals"
ALLOWED = ("evowiki.goals", "evowiki.content", "evowiki.schema", "evowiki.belief", "evowiki.view.roles",
           "evowiki.view.constraints", "evowiki.teach.standard", "evowiki.tokens",
           "evowiki.plain_text", "evowiki.teach.textsim")
BANNED_AT_RUNTIME = ("evowiki.llm", "evowiki.simulator", "evowiki.experiments", "evowiki.store",
                     "evowiki.service", "evowiki.runtime", "evowiki.pipeline", "evowiki.rsib",
                     "evowiki.render")
LLM_LANE = frozenset({"writer.py", "patch.py"})


def _evowiki_imports(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append("evowiki.goals." + (node.module or "") if node.level else node.module or "")
    return [m for m in out if m.startswith("evowiki")]


def test_goals_imports_only_g_k_and_role_types():
    files = sorted(GOALS.glob("*.py"))
    assert {f.name for f in files} >= {"models.py", "order.py", "outline_context.py"}
    for f in files:
        allowed = ALLOWED + (("evowiki.llm",) if f.name in LLM_LANE else ())
        for mod in _evowiki_imports(f):
            assert mod.startswith(allowed), f"{f.name} imports {mod}"


def test_importing_goals_loads_no_llm_simulator_or_store():
    code = ("import json, sys, evowiki.goals.guardrails, evowiki.goals.outline_context, "
            "evowiki.goals.fold; "
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('evowiki'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout
    assert not [m for m in json.loads(out) if m.startswith(BANNED_AT_RUNTIME)]
