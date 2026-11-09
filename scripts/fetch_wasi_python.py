"""Download the pinned CPython 3.13 WASI build used by the forge sandbox.

Source: https://github.com/brettcannon/cpython-wasi-build/releases (wasm32-wasip1).
The zip holds ``python.wasm`` and ``lib/python3.13/``. The sha256 is pinned; a
mismatch deletes the download and exits 1. The runtime is unpacked to
``--dest`` (default ``<main-checkout>/.forge/runtime/python-wasi``), never over tracked
files or a work tree, and is self-ignored by a ``.gitignore`` of ``*``.

Usage: uv run python scripts/fetch_wasi_python.py [--dest DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from evowiki.paths import WASI_ROOT

VERSION = "3.13.15"
URL = (
    "https://github.com/brettcannon/cpython-wasi-build/releases/download/"
    f"v{VERSION}/python-{VERSION}-wasi_sdk-24.zip"
)
SHA256 = "67e1c32a85d5e0600c0939f5fd4023ca0127f1a9e81f5499697467bda67af78d"
DEFAULT_DEST = WASI_ROOT
PY = "python3.13"


def _git(probe: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(probe), *args],
                             capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _would_touch_repo(dest: Path) -> str:
    """Why ``dest`` must not be (re)created, or "" when it is safe.

    The dest is wiped and replaced, so it may not hold tracked files, be a work
    tree root, or contain one. The runtime itself is kept out of git by the
    ``.gitignore`` (``*``) written into it, the way ``.venv`` does it.
    """
    probe = dest
    while not probe.exists():
        probe = probe.parent
    top = _git(probe, "rev-parse", "--show-toplevel")
    if top is not None:
        top_path = Path(top).resolve()
        resolved = dest.resolve()
        if resolved == top_path or resolved in top_path.parents:
            return f"{dest} is or contains the git work tree {top_path}"
        if dest.exists() and _git(dest, "ls-files", "."):
            return f"{dest} holds files tracked by git"
    if dest.exists() and any(p.name == ".git" for p in dest.rglob(".git")):
        return f"{dest} contains a git repository"
    return ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if root != target and root not in target.parents:
            raise RuntimeError(f"zip entry escapes destination: {member.filename}")
    zf.extractall(dest)


def precompile(dest: Path) -> None:
    """Byte-compile the stdlib once, inside WASI, with the runtime's own CPython.

    The sandbox mounts ``/lib`` read-only, so without .pyc files every call would
    recompile the imports from source (~1.3e9 fuel of the 2e9 L1 budget).
    ``unchecked-hash`` pycs do not depend on file mtimes.
    """
    import wasmtime

    engine = wasmtime.Engine()
    module = wasmtime.Module.from_file(engine, str(dest / "python.wasm"))
    cfg = wasmtime.WasiConfig()
    cfg.argv = ["python", "-I", "-m", "compileall", "-q", "--invalidation-mode",
                "unchecked-hash", "-x", r"/(test|idlelib|tkinter|turtledemo)/",
                f"/lib/{PY}"]
    cfg.inherit_stdout()
    cfg.inherit_stderr()
    cfg.preopen_dir(str(dest / "lib"), "/lib")
    store = wasmtime.Store(engine)
    store.set_wasi(cfg)
    linker = wasmtime.Linker(engine)
    linker.define_wasi()
    try:
        linker.instantiate(store, module).exports(store)["_start"](store)
    except wasmtime.ExitTrap as exc:
        if exc.code != 0:
            raise RuntimeError(f"compileall exited with {exc.code}") from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    ns = ap.parse_args(argv)
    dest: Path = ns.dest.absolute()
    why = _would_touch_repo(dest)
    if why:
        print(f"refusing: {why}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        zpath = Path(tmp) / "python-wasi.zip"
        print(f"downloading {URL}")
        with urllib.request.urlopen(URL, timeout=120) as r, zpath.open("wb") as f:
            shutil.copyfileobj(r, f)
        got = _sha256(zpath)
        if got != SHA256:
            zpath.unlink(missing_ok=True)
            print(f"sha256 mismatch: expected {SHA256}, got {got}", file=sys.stderr)
            return 1
        staging = Path(tmp) / "unpacked"
        with zipfile.ZipFile(zpath) as zf:
            _safe_extract(zf, staging)
        wasm = next(staging.rglob("python.wasm"), None)
        if wasm is None or not (wasm.parent / "lib" / PY / "os.py").is_file():
            print("archive does not contain python.wasm + lib/python3.13", file=sys.stderr)
            return 1
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(wasm.parent, dest)
        (dest / ".gitignore").write_text("*\n", encoding="utf-8")
    print("byte-compiling the stdlib inside WASI")
    precompile(dest)
    print(f"installed CPython {VERSION} WASI at {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
