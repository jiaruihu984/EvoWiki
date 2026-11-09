import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "envfile.py"
_spec = importlib.util.spec_from_file_location("envfile_under_test", _PATH)
envfile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(envfile)


def test_reads_comments_export_quotes_and_literals(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# a comment\n"
        "PLAIN=abc\n"
        "export EXPORTED=1\n"
        "INLINE=value # trailing comment\n"
        'DQ="double quoted"\n'
        "SQ='single quoted'\n"
        'HASH="keeps # inside"\n'
        "EMPTY=\n"
        "NOVALUE\n"
        "URL=postgresql://u:p@localhost:5433/db\n"
        "REF=${PLAIN}x\n"
        "  SPACED = padded  \n", encoding="utf-8")
    got = envfile.read_env_file(p)
    assert got["PLAIN"] == "abc" and got["EXPORTED"] == "1"
    assert got["INLINE"] == "value"
    assert got["DQ"] == "double quoted" and got["SQ"] == "single quoted"
    assert got["HASH"] == "keeps # inside"
    assert got["EMPTY"] == ""
    assert "NOVALUE" not in got
    assert got["URL"] == "postgresql://u:p@localhost:5433/db"
    assert got["REF"] == "${PLAIN}x"          # literal: no interpolation
    assert got["SPACED"] == "padded"


def test_missing_file_raises_unless_allowed(tmp_path):
    with pytest.raises(OSError):
        envfile.read_env_file(tmp_path / "nope.env")
    assert envfile.read_env_file(tmp_path / "nope.env", missing_ok=True) == {}
