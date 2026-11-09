"""pixeltable-pgserver is a dev/test dependency only; the runtime speaks plain Postgres URLs."""

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "evowiki"


def test_runtime_never_imports_pgserver():
    assert SRC.is_dir()
    offenders = [p for p in SRC.rglob("*.py") if "pgserver" in p.read_text(encoding="utf-8")]
    assert offenders == []
