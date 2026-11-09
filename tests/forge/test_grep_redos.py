import time

import pytest

from evowiki.forge.workspace import Workspace, WorkspaceError


def test_catastrophic_pattern_times_out_fast():
    ws = Workspace.of({"tool.py": "x = '" + "a" * 40 + "!'\n"})
    t = time.monotonic()
    with pytest.raises(WorkspaceError, match="太慢"):
        ws.grep(r"(a|aa)+$")
    assert time.monotonic() - t < 3.0


def test_normal_grep_unchanged():
    ws = Workspace.of({"tool.py": "def run(args):\n    return {}\n"})
    assert ws.grep(r"def \w+") == "tool.py:1: def run(args):"


def test_bad_regex_still_explained():
    with pytest.raises(WorkspaceError, match="正则不合法"):
        Workspace.of({"tool.py": ""}).grep("(")
