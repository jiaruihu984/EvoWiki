"""Architecture audit Q8: product packages reach the frozen kernel types only through evowiki.schema.

content.models / content.graph / content.relations, evidence.* and timeutil are in the simulator's
frozen closure; a product module importing them directly would tie product changes to frozen
files again."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "evowiki"
PRODUCT = ("agent", "assistant", "bot", "deep", "goals", "llm", "nodes", "retrieval", "runtime",
           "service", "store", "teach", "wiki", "wikiapp")
FROZEN = ("evowiki.content.models", "evowiki.content.graph", "evowiki.content.relations",
          "evowiki.evidence", "evowiki.timeutil")


def _imports(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out += [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
    return out


def test_product_imports_frozen_types_only_through_schema():
    bad = sorted(f"{p.relative_to(SRC)}: {m}"
                 for pkg in PRODUCT for p in (SRC / pkg).rglob("*.py")
                 for m in _imports(p)
                 if m == "evowiki.content" or any(m == f or m.startswith(f + ".") for f in FROZEN))
    assert bad == []


def test_schema_names_are_the_kernel_objects():
    from evowiki import schema
    from evowiki.content.graph import ContentGraph
    from evowiki.content.models import PUBLISHABLE, Lang, Segment
    from evowiki.timeutil import days_between

    assert (schema.ContentGraph, schema.Lang, schema.Segment, schema.PUBLISHABLE,
            schema.days_between) == (ContentGraph, Lang, Segment, PUBLISHABLE, days_between)
