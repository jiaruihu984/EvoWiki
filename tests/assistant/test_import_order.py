"""Import cycles surface only for some first imports; check the entry points in a fresh interpreter."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["evowiki.deep.agent", "evowiki.assistant.tools"])
def test_module_imports_cleanly_first(module: str) -> None:
    done = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
