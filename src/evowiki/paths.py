"""Repository-owned data paths, shared by normal and live worktrees."""

import subprocess
from pathlib import Path

ROOT = Path(subprocess.check_output(
    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
    cwd=Path(__file__).resolve().parent, text=True, encoding="utf-8").strip()).parent
LIVE_ROOT = ROOT.with_name(ROOT.name + "-live")
FORGE_ROOT = ROOT.with_name(ROOT.name + "-forge")
WASI_ROOT = ROOT / ".forge" / "runtime" / "python-wasi"
