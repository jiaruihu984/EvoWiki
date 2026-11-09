"""No code path in the forge runs forged code outside the WASI sandbox (plan T9 Step 3): an AST scan
of `src/evowiki/forge/**` (the guest runner, which only ever runs inside WASI, is the one exception)
plus a check that nothing in `src/` imports the in-process test fake."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "evowiki"
FORGE = SRC / "forge"
GUEST = FORGE / "sandbox" / "guest_runner.py"
# Modules that must spawn processes (plan P3 T3/T4/T10): only the `subprocess` import is allowed
# there; every other banned name stays banned.
BAD_CALLS = {"exec", "eval", "compile", "__import__", "execfile", "import_module"}
BAD_ATTRS = {"import_module", "system", "popen", "spawnv", "spawnl", "startfile", "exec_module",
             "run_path", "run_module", "load_module", "execv", "execl", "Popen"}
BAD_MODULES = {"runpy", "subprocess", "ctypes", "multiprocessing", "code", "codeop", "pty",
               "importlib.util", "importlib.machinery", "importlib.import_module", "tests",
               "win32process", "win32job"}
# Modules that must spawn processes (plan P3 T3/T4/T10), each with exactly the hits it may have.
SPAWN_ALLOWED = {
    "forge/sandbox/jobs.py": {"imports subprocess", "imports win32process", "imports win32job"},
    "forge/patch/git.py": {"imports subprocess"},
    # the replay child's one dynamic import: a fixed `module:function` from replay_entries.ENTRIES
    "forge/replay_child.py": {"calls .import_module()"},
}


def _hits(text: str, label: str) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(text)):
        where = f"{label}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in BAD_CALLS:
                out.append(f"{where} calls {f.id}()")
            if isinstance(f, ast.Attribute) and f.attr in BAD_ATTRS:
                out.append(f"{where} calls .{f.attr}()")
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for m in mods:
            if any(m == b or m.startswith(b + ".") for b in BAD_MODULES):
                out.append(f"{where} imports {m}")
    return out


def test_forge_never_executes_code_on_the_host():
    files = sorted(p for p in FORGE.rglob("*.py") if p != GUEST)
    assert len(files) >= 10, files                     # the scan really sees the package
    hits = []
    for p in files:
        label = p.relative_to(SRC).as_posix()
        allowed = SPAWN_ALLOWED.get(label, set())
        hits += [h for h in _hits(p.read_text(encoding="utf-8"), label)
                 if h.split(" ", 1)[1] not in allowed]
    assert hits == [], hits


def test_spawn_allowlist_points_at_real_files():
    assert all((SRC / f).is_file() for f in SPAWN_ALLOWED)


def test_nothing_in_src_imports_the_test_fakes():
    hits = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
            if any(s in p.read_text(encoding="utf-8") for s in ("tests.forge", "InProcessSandbox"))]
    assert hits == [], hits


@pytest.mark.parametrize("snippet,expect", [
    ("exec('1')", "calls exec()"),
    ("eval('1')", "calls eval()"),
    ("import subprocess", "imports subprocess"),
    ("import runpy", "imports runpy"),
    ("from importlib import import_module\nimport_module('x')", "import_module"),
    ("import importlib\nimportlib.import_module('x')", "calls .import_module()"),
    ("from tests.forge.fakes import InProcessSandbox", "imports tests.forge.fakes"),
    ("import os\nos.system('x')", "calls .system()"),
])
def test_the_scan_catches_what_it_should(snippet, expect):
    hits = _hits(snippet, "probe")
    assert any(expect in h for h in hits), hits
