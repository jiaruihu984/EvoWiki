"""Shared AST checks for deployment-only imports and hidden-state isolation."""

import ast
from pathlib import Path

HIDDEN_NAMES = ("rel_known", "rel_traces", "rho_rel", "true_level", "truth", "HiddenFinalState",
                "GroundTruth", "RelationTruth", "RelProbeTruth", "latent", "Trajectory",
                "SimLearner", "relprobe_log", "final_state")


def _imports(path: Path, package: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                out.append(package + ("." + node.module if node.module else ""))
            else:
                out.append(node.module or "")
    return out


def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add(node.name.rsplit(".", 1)[-1])
    return out
