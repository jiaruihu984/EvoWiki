import subprocess
from pathlib import Path

import pytest


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A tiny repo shaped like EvoWiki: src/evowiki/{deep,store,simulator}, tests/."""
    root = tmp_path / "repo"
    files = {
        "src/evowiki/__init__.py": "",
        "src/evowiki/deep/__init__.py": "",
        "src/evowiki/deep/mathx.py": "def add(a, b):\n    return a - b   # planted bug\n",
        "src/evowiki/store/__init__.py": "",
        "src/evowiki/store/db.py": "X = 1\n",
        "src/evowiki/simulator/__init__.py": "",
        "src/evowiki/simulator/run.py": "from evowiki.content import shared\n",
        "src/evowiki/content/__init__.py": "",
        "src/evowiki/content/shared.py": "Y = 2\n",
        "tests/__init__.py": "",
        "tests/test_old.py": "def test_old():\n    assert True\n",
        "pyproject.toml": "[project]\nname='x'\ndependencies=['pydantic>=2']\n",
    }
    for p, t in files.items():
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(t, encoding="utf-8")
    _git(root.parent, "init", "-q", "-b", "master", str(root))
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root
