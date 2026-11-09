"""Architecture audit stage 3: store/ is the persistence layer.

It may use domain contracts and the foundation (teach, belief, llm, agent, harness's pack / keys,
content, schema, retrieval) and the Bot's two contract modules (bot.settings, bot.suppression,
which import nothing of evowiki), never the application or HTTP layers. And the Bot's tables are
read and written only through store/ (one writer per table)."""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "evowiki"
UPPER = ("evowiki.wikiapp", "evowiki.service", "evowiki.assistant", "evowiki.deep",
         "evowiki.goals", "evowiki.wiki", "evowiki.nodes", "evowiki.runtime")
BOT_CONTRACTS = ("evowiki.bot.settings", "evowiki.bot.suppression")
BOT_SQL = re.compile(r"\b(from|into|update|join)\s+bot_[a-z_]+", re.IGNORECASE)


def _imports(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append(node.module)
    return [m for m in out if m.startswith("evowiki")]


def test_store_never_imports_the_application_layers():
    bad = sorted(f"{p.relative_to(SRC)}: {m}" for p in (SRC / "store").rglob("*.py")
                 for m in _imports(p)
                 if m.startswith(UPPER) or (m.startswith("evowiki.bot") and m not in BOT_CONTRACTS))
    assert bad == []


def test_the_bot_contract_modules_import_nothing_of_evowiki():
    for mod in BOT_CONTRACTS:
        path = SRC / (mod.removeprefix("evowiki.").replace(".", "/") + ".py")
        assert _imports(path) == [], mod


def test_bot_tables_are_only_touched_from_store():
    bad = sorted(str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if p.relative_to(SRC).parts[0] != "store"
                 and BOT_SQL.search(p.read_text(encoding="utf-8")))
    assert bad == []
