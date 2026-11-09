"""Offline pre-merge checks, including committed, staged, unstaged and untracked changes.

Run with the project's Python environment. The comparison defaults to origin/master;
no fetch, dependency installation, .env loading or application startup is performed.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = "src/evowiki/store/migrations/"
AREAS = ("assistant", "goals", "wikiapp", "deep", "forge", "harness")


def git(*args: str) -> list[str]:
    cmd = ["git", *args]
    print("+", shlex.join(cmd), flush=True)
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode:
        raise RuntimeError(f"git failed ({result.returncode}): {result.stderr}")
    return [p for p in result.stdout.split("\0") if p]


def simulator_closure() -> set[str]:
    """Follow local imports without executing code or importing the application."""
    pending = list((ROOT / "src/evowiki/simulator").rglob("*.py"))
    seen: set[str] = set()
    while pending:
        path = pending.pop()
        rel = path.relative_to(ROOT).as_posix()
        if rel in seen:
            continue
        seen.add(rel)
        module = path.relative_to(ROOT / "src").with_suffix("").parts
        package = module[:-1]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = list(package[:len(package) - node.level + 1]) if node.level else []
                base = ".".join(prefix + ([node.module] if node.module else []))
                names = [base, *(f"{base}.{a.name}" for a in node.names)]
            for name in names:
                if name != "evowiki" and not name.startswith("evowiki."):
                    continue
                parts = name.split(".")
                for i in range(1, len(parts) + 1):
                    stem = ROOT / "src" / Path(*parts[:i])
                    for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
                        if candidate.is_file():
                            pending.append(candidate)
    return seen


def migration_errors(changed: set[str], baseline: list[str]) -> list[str]:
    def number(p: str) -> int:
        match = re.fullmatch(r"(\d{4})_.+\.sql", Path(p).name)
        if not match:
            raise ValueError(f"invalid migration name: {p}")
        return int(match[1])

    old = {p for p in baseline if p.endswith(".sql")}
    added = sorted(number(p) for p in changed if p.startswith(MIGRATIONS)
                   and p.endswith(".sql") and p not in old)
    last = max((number(p) for p in old), default=0)
    expected = list(range(last + 1, last + 1 + len(added)))
    return [] if added == expected else [f"new migrations must be {expected}, got {added}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/master")
    args = parser.parse_args(argv)
    pytest_temp = None
    try:
        changed = set(git("diff", "--name-only", "--no-renames", "-z", args.base, "--"))
        changed.update(git("ls-files", "--others", "--exclude-standard", "-z"))
        baseline = git("ls-tree", "-r", "--name-only", "-z", args.base, "--", MIGRATIONS)
        protected = simulator_closure()
        errors = [f"protected file changed: {p}" for p in sorted(changed)
                  if p in protected or p.startswith(("src/evowiki/simulator/",
                      "src/evowiki/experiments/", "src/evowiki/rsib/", "rsib/"))]
        errors += migration_errors(changed, baseline)
        py = sys.executable
        commands = [([py, "-m", "ruff", "check", "src", "tests", "scripts"], ROOT)]
        tests = ["tests/simulator/test_import_boundary.py"]
        tests += [f"tests/{area}" for area in AREAS if any(
            p.startswith((f"src/evowiki/{area}/", f"tests/{area}/")) for p in changed)]
        if any(p in {"scripts/check_merge.py", "tests/scripts/test_check_merge.py"} for p in changed):
            tests.append("tests/scripts/test_check_merge.py")
        pytest_temp = tempfile.mkdtemp(prefix=".check-merge-pytest-", dir=ROOT)
        commands.append(([py, "-m", "pytest", *tests, f"--basetemp={pytest_temp}"], ROOT))
        # Schemas also live in route modules, models and tool definitions. Check all backend
        # Python changes rather than missing an inline API model or SSE union.
        if any((p.startswith("src/evowiki/") and p.endswith(".py")) or
               p.startswith("obsidian-plugin/src/generated/") or
               p == "scripts/gen_plugin_types.py" for p in changed):
            commands.append(([py, "scripts/gen_plugin_types.py", "--check"], ROOT))
        if any(p.startswith("obsidian-plugin/") for p in changed):
            npx = "npx.cmd" if os.name == "nt" else "npx"
            commands += [([npx, "--no-install", *c], ROOT / "obsidian-plugin")
                         for c in (["vitest", "run", "--configLoader", "native", "--cache=false"],
                                   ["tsc", "--noEmit"])]
        env = {**os.environ, "PYTHONPATH": "src", "PYTHONUTF8": "1", "npm_config_offline": "true"}
        for error in errors:
            print(error, file=sys.stderr)
        failed = bool(errors)
        for cmd, cwd in commands:
            print(f"+ ({cwd.relative_to(ROOT)}) {shlex.join(cmd)}", flush=True)
            try:
                result = subprocess.run(cmd, cwd=cwd, env=env, check=False)
                print(f"exit: {result.returncode}", flush=True)
                failed |= result.returncode != 0
            except OSError as exc:
                print(str(exc), file=sys.stderr)
                failed = True
        return int(failed)
    except (RuntimeError, ValueError, OSError, SyntaxError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if pytest_temp is not None:
            shutil.rmtree(pytest_temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
