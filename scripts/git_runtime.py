"""Locate the runtime shipped with the required Git for Windows installation."""

import shutil
import subprocess
from pathlib import Path


def git_runtime_dir() -> Path | None:
    """Ask Git for its distribution layout instead of assuming mingw64 or ucrt64."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run([git, "--exec-path"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if not result.stdout.strip():
        return None
    runtime = Path(result.stdout.strip()).parent.parent / "bin"
    return runtime if (runtime / "libwinpthread-1.dll").is_file() else None
