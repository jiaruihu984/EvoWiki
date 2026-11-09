"""The `ast` import graph of the repository (design §2, §5.1; plan P3 T3).

Used for two things: the files the simulator imports (denied to L2 patches, computed here at check
time, never hand-copied) and the tests that import a changed module (PASS_TO_PASS selection).
Fail closed: a `.py` file under `src/` or `tests/` that does not decode or parse raises
`GraphError`, because a graph with a hole could hide a simulator import.

Edges are static: `import a.b`, `from a.b import c` (adds `a.b` and `a.b.c` when that is a module),
relative imports resolved against the importing package, and `import_module("x")` /
`__import__("x")` with a constant string. Importing `a.b.c` also runs `a/__init__` and
`a/b/__init__`, so those package files are edges too. Imports that resolve to no repo file
(stdlib, third-party) are ignored."""

import ast
import os
from collections import deque
from collections.abc import Iterable, Mapping
from pathlib import Path

_ROOT_DIRS = ("src", "tests")
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache"}


class GraphError(ValueError):
    """The import graph cannot be computed (a file does not decode or parse). Fail closed."""


def _module_name(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def module_files(root: Path) -> dict[str, str]:
    """Module name -> repo-relative POSIX path, for every `.py` under `src/` and `tests/`.
    "evowiki.deep.agent" -> "src/evowiki/deep/agent.py"; "tests.deep.test_x" -> "tests/...".
    Symlinked directories are not followed."""
    out: dict[str, str] = {}
    root = Path(root)
    for top in _ROOT_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                rel = Path(dirpath, fn).relative_to(root).as_posix()
                name = _module_name(rel)
                if name:
                    out.setdefault(name, rel)
    return out


def _with_parents(name: str) -> set[str]:
    parts = name.split(".")
    return {".".join(parts[:i]) for i in range(1, len(parts) + 1)}


def imports(path: str, text: str, module: str) -> set[str]:
    """Module names `text` (the file at repo path `path`, module `module`) may import, including
    their parent packages. Raises GraphError when the text does not parse."""
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError) as e:
        raise GraphError(f"{path} 无法解析：{type(e).__name__}") from e
    is_pkg = path.endswith("__init__.py")
    package = module if is_pkg else module.rpartition(".")[0]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out |= _with_parents(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pkg_parts = package.split(".") if package else []
                up = node.level - 1
                if up > len(pkg_parts):
                    continue                       # beyond the top package: Python fails too
                anchor = pkg_parts[:len(pkg_parts) - up]
                base = ".".join(anchor + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if not base:
                continue
            out |= _with_parents(base)
            for a in node.names:
                if a.name != "*":
                    out.add(f"{base}.{a.name}")
        elif isinstance(node, ast.Call):
            f = node.func
            fname = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) \
                else ""
            if (fname in ("import_module", "__import__") and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str) and node.args[0].value):
                name = node.args[0].value
                if name.startswith("."):
                    if not package:
                        continue
                    name = package + name if name != "." else package
                out |= _with_parents(name)
    return out


def graph(root: Path, files: Mapping[str, str] | None = None) -> dict[str, set[str]]:
    """Repo file -> repo files it imports (both repo-relative POSIX). `files` = `module_files`."""
    root = Path(root)
    files = module_files(root) if files is None else files
    g: dict[str, set[str]] = {}
    for module, rel in files.items():
        try:
            text = (root / rel).read_bytes().decode("utf-8-sig")     # a BOM is valid Python
        except (OSError, UnicodeDecodeError) as e:
            raise GraphError(f"{rel} 无法读取：{type(e).__name__}") from e
        g[rel] = {files[m] for m in imports(rel, text, module) if m in files and files[m] != rel}
    return g


def closure(roots: Iterable[str], g: Mapping[str, set[str]]) -> frozenset[str]:
    """`roots` plus everything they import, transitively."""
    seen: set[str] = set()
    todo = deque(roots)
    while todo:
        f = todo.popleft()
        if f in seen:
            continue
        seen.add(f)
        todo.extend(g.get(f, ()))
    return frozenset(seen)


def importers(targets: Iterable[str], g: Mapping[str, set[str]]) -> frozenset[str]:
    """`targets` plus every file that imports one of them, transitively (reverse closure)."""
    rev: dict[str, set[str]] = {}
    for src, dsts in g.items():
        for d in dsts:
            rev.setdefault(d, set()).add(src)
    return closure(targets, rev)
