"""Locate the pinned CPython WASI runtime (``scripts/fetch_wasi_python.py``)."""
import os
from pathlib import Path

from evowiki.paths import WASI_ROOT

ENV = "EVOWIKI_WASI_PYTHON"
DEFAULT = WASI_ROOT
PY = "python3.13"


def root() -> Path:
    env = os.environ.get(ENV)
    return Path(env) if env else DEFAULT


def wasm_path() -> Path:
    return root() / "python.wasm"


def lib_path() -> Path:
    return root() / "lib"


def available() -> bool:
    return wasm_path().is_file() and (lib_path() / PY / "os.py").is_file()


def why_unavailable() -> str:
    if available():
        return ""
    return (f"CPython WASI runtime not found at {root()} (need python.wasm and lib/{PY}/os.py); "
            f"run `uv run python scripts/fetch_wasi_python.py` or set {ENV}")
