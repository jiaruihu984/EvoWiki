"""Checkout-local evaluation storage, shared by the runner and runtime gates."""

import os
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def data_dir(env: Mapping[str, str] = os.environ) -> Path:
    return _path(env.get("EVOWIKI_EVALSET_DATA_DIR"), ROOT / ".evowiki-run/evalset")


def sealed_dir(env: Mapping[str, str] = os.environ) -> Path:
    return _path(env.get("EVOWIKI_SEALED_ROOT"), ROOT / ".evowiki-run/evalset-sealed")


def _path(value: str | None, default: Path) -> Path:
    path = Path(value).expanduser() if value else default
    return (path if path.is_absolute() else ROOT / path).resolve()
