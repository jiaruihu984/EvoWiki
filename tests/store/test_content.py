import json
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from evowiki.store.content import (
    activate,
    active_version,
    import_snapshot,
    import_snapshot_file,
    initialize_content,
    load_content,
)

SNAP = Path(__file__).resolve().parents[1] / "fixtures" / "runtime" / "content_snapshot.json"
NOW = datetime(2026, 10, 2, tzinfo=UTC)


def test_first_start_is_empty_and_idempotent(pool):
    with pool.connection() as conn:
        content = initialize_content(conn, NOW)
        assert content.version_id == "cv_empty" and not content.graph.concept_ids
        assert initialize_content(conn, NOW).version_id == "cv_empty"
        assert conn.execute("select count(*) as n from content_snapshot").fetchone()["n"] == 1
        cv = import_snapshot_file(conn, SNAP, NOW)
        activate(conn, cv, NOW)
        assert initialize_content(conn, NOW).version_id == cv
        assert conn.execute("select count(*) as n from content_snapshot").fetchone()["n"] == 2


def test_import_activate_load(pool):
    with pool.connection() as conn:
        assert active_version(conn) is None
        with pytest.raises(RuntimeError):
            load_content(conn)
        cv = import_snapshot_file(conn, SNAP, NOW)
        assert cv == "cv_runtimefixture"
        assert import_snapshot_file(conn, SNAP, NOW) == cv  # idempotent
        activate(conn, cv, NOW)
        lc = load_content(conn)
    assert lc.version_id == cv and "pca" in lc.graph.concept_ids


def test_conflicting_snapshot_rejected_and_immutable(pool):
    doc = json.loads(SNAP.read_text(encoding="utf-8"))
    with pool.connection() as conn:
        import_snapshot(conn, doc, NOW)
        doc["concepts"][0]["name_en"] = "Changed"
        with pytest.raises(ValueError, match="different"):
            import_snapshot(conn, doc, NOW)
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("delete from content_snapshot")
