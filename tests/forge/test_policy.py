from pathlib import Path

import pytest

from evowiki.forge import policy
from evowiki.forge.patch.paths import Change, check_change, simulator_files


@pytest.mark.parametrize("path", [
    "src/evowiki/deep/writer.py", "src/evowiki/assistant/tools/x.py", "src/evowiki/nodes/a.py"])
def test_allowed(path):
    assert policy.violation(path, exists=True) == ""


@pytest.mark.parametrize("path", [
    "src/evowiki/forge/policy.py", "src/evowiki/forge/limits.py", "src/evowiki/harness/gate.py",
    "src/evowiki/teach/anchors.py", "src/evowiki/store/migrations/0001.sql",
    "src/evowiki/llm/gateway.py", "src/evowiki/agent/durable.py", "src/evowiki/experiments/a.py",
    "src/evowiki/rsib/a.py", "src/evowiki/simulator/a.py", "CLAUDE.md", ".env", ".env.local",
    ".claude/settings.json", "pyproject.toml", "uv.lock", "scripts/x.py",
    "obsidian-plugin/main.ts", "src/evowiki/agent/loop.py", "README.md",
    # not plain repo-relative paths: refused, never normalised into something allowed
    "/src/evowiki/deep/a.py", "src/evowiki/deep/../forge/x.py", "src\\evowiki\\deep\\a.py",
    "D:/Example/src/evowiki/deep/a.py", "./src/evowiki/deep/a.py", "", "SRC/evowiki/deep/a.py"])
def test_denied_or_outside(path):
    assert policy.violation(path, exists=False)


def test_tests_are_add_only():
    assert policy.violation("tests/deep/test_new.py", exists=False) == ""
    assert "已有测试" in policy.violation("tests/deep/test_writer.py", exists=True)


def test_policy_and_limits_are_denied_anchors():
    for anchor in ("src/evowiki/forge/policy.py", "src/evowiki/forge/limits.py"):
        assert "禁止" in policy.violation(anchor, exists=True)


@pytest.mark.parametrize("exists", [False, True])
def test_wikiapp_regular_files_are_allowed(exists):
    assert policy.violation("src/evowiki/wikiapp/repo.py", exists=exists) == ""


def test_wikiapp_is_outside_simulator_closure():
    protected = simulator_files(Path(__file__).resolve().parents[2])
    assert not any(p.startswith("src/evowiki/wikiapp/") for p in protected)


def test_wikiapp_still_obeys_dynamic_simulator_protection():
    path = "src/evowiki/wikiapp/repo.py"
    assert check_change([Change("M", path)], base_files=frozenset({path}),
                        sim_files=frozenset({path}))


@pytest.mark.parametrize("path", ["migrations/0001.sql", "src/evowiki/store/migrations/0001.sql",
                                  "src/evowiki/harness/judge.py", "src/evowiki/forge/policy.py"])
def test_protected_paths_stay_denied(path):
    assert policy.violation(path, exists=True)
