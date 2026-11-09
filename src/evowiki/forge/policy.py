"""Repository path policy for generated code patches.

Edit tools, gates, and landing enforce this policy independently. Paths must be plain
repository-relative POSIX paths under an allowed root and outside all denied roots.
The gates also deny the simulator import closure, computed from the import graph.
This policy is itself protected from generated edits.
"""

from fnmatch import fnmatchcase
from pathlib import PurePosixPath

# Allowed roots (whitelist). `tests/**` only takes NEW files: existing tests are read-only.
ALLOWED: tuple[str, ...] = tuple(
    f"src/evowiki/{p}/**"
    for p in ("deep", "assistant", "bot", "render", "content", "sources", "wiki", "wikiapp", "goals", "nodes"))
TESTS_NEW_ONLY = "tests/**"

# Denied (blacklist, wins over the whitelist): evaluation stays outside the loop, the forge itself,
# the store / service / gateway (cost reports must stay trustworthy), durable plumbing, frozen
# research code, and repo configuration.
DENIED: tuple[str, ...] = (
    "src/evowiki/harness/**",
    "src/evowiki/teach/**",
    "src/evowiki/belief/**",
    "src/evowiki/evaluation/**",
    "src/evowiki/forge/**",
    "src/evowiki/store/**",
    "src/evowiki/service/**",
    "src/evowiki/llm/**",
    "src/evowiki/agent/durable.py",
    "src/evowiki/agent/dbos_app.py",
    "src/evowiki/agent/effects.py",
    "src/evowiki/experiments/**",
    "src/evowiki/rsib/**",
    "src/evowiki/simulator/**",
    "simulator/**",
    "CLAUDE.md",
    ".claude/**",
    ".env*",
    "pyproject.toml",
    "uv.lock",
    "scripts/**",
    "obsidian-plugin/**",
)


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return fnmatchcase(path, pattern)


def violation(path: str, *, exists: bool) -> str:
    """Why an L2 patch may not touch `path` (repo-relative, POSIX), or "" if it may.
    `exists` = the file is already in the base commit (old tests are read-only)."""
    p = PurePosixPath(path)
    if (not path or "\\" in path or p.is_absolute() or ".." in p.parts or ":" in path
            or str(p) != path):
        return f"{path!r} 不是仓库内的相对路径"
    if any(_matches(path, d) for d in DENIED):
        return f"{path} 在禁止修改的范围内"
    if _matches(path, TESTS_NEW_ONLY):
        return f"{path} 是已有测试，只能新增测试文件" if exists else ""
    if any(_matches(path, a) for a in ALLOWED):
        return ""
    return f"{path} 不在允许修改的范围内"
