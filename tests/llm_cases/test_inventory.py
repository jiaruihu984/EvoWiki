"""Every product LLM purpose has offline cases; removed verifier calls stay removed."""

import re
from pathlib import Path

from evowiki.wikiapp.llm import JSON_PROMPT_VERSION, PURPOSE_GROUPS
from tests.llm_cases.inventory import INVENTORY, PRODUCT_DIRS, REMOVED_PURPOSES

ROOT = Path(__file__).resolve().parents[2]
# PURPOSE = "x" / X_PURPOSE = "x" / purpose="x" in the product code (digits allowed after the
# first character)
PURPOSE_RE = re.compile(r'''(?:\b[A-Z_]*PURPOSE\s*=\s*|purpose\s*=\s*)["']([a-z_][a-z0-9_.]*)["']''')


def _product_sources():
    for d in PRODUCT_DIRS:
        yield from (ROOT / d).rglob("*.py")


def product_purposes() -> set[str]:
    found = set()
    for f in _product_sources():
        found |= set(PURPOSE_RE.findall(f.read_text(encoding="utf-8")))
    return found - {"purpose"}           # the aspect enum member Aspect.PURPOSE = "purpose"


def test_every_product_purpose_is_inventoried():
    missing = product_purposes() - set(INVENTORY)
    assert not missing, f"LLM purposes without test cases: {sorted(missing)}"


def test_every_registered_purpose_is_inventoried():
    listed = (set(JSON_PROMPT_VERSION) | set(PURPOSE_GROUPS)) - set(INVENTORY)
    assert not listed, f"purposes in wikiapp/llm.py without test cases: {sorted(listed)}"


def test_inventory_has_no_stale_entries():
    stale = set(INVENTORY) - product_purposes() - set(JSON_PROMPT_VERSION) - set(PURPOSE_GROUPS)
    assert not stale, f"inventoried purposes no longer in the code: {sorted(stale)}"


def test_every_entry_has_offline_case_files_with_tests():
    for purpose, e in INVENTORY.items():
        assert (ROOT / e.site).exists(), (purpose, e.site)
        text = (ROOT / e.offline).read_text(encoding="utf-8")
        assert re.search(r"^def test_", text, re.MULTILINE), (purpose, e.offline)
        assert purpose in text, f"{e.offline} never names {purpose}"


def test_the_separate_verifier_call_stays_removed():
    for f in _product_sources():
        text = f.read_text(encoding="utf-8")
        for p in REMOVED_PURPOSES:             # as a purpose string (goals/probe.py stays: KST)
            assert f'"{p}"' not in text, (f, p)
        assert "def verify_items" not in text, f
    for p in REMOVED_PURPOSES:
        assert p not in JSON_PROMPT_VERSION and p not in PURPOSE_GROUPS


def test_goal_quiz_and_check_calls_use_only_the_generator_role():
    """No product question generator asks a second model to solve its quiz (NO_VERIFIER)."""
    for rel in ("src/evowiki/goals", "src/evowiki/wikiapp"):
        for f in (ROOT / rel).rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            assert not re.search(r'(?:llm|gateway|gw)\w*\s*\(\s*"verifier"', text), f
            assert not re.search(r'\.call\(\s*"verifier"', text), f
