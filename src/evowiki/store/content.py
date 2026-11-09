import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from evowiki.content.loaded import LoadedContent
from evowiki.store.db import fetch_one


def import_snapshot(conn: psycopg.Connection, doc: Mapping[str, Any], now: datetime) -> str:
    lc = LoadedContent.from_document(doc)  # validates the whole document first
    with conn.transaction():
        conn.execute("insert into content_snapshot (id, created_at, document, imported_at) "
                     "values (%s, %s, %s, %s) on conflict (id) do nothing",
                     (lc.version_id, doc["content_version"]["created_at"], Jsonb(dict(doc)), now))
        same = fetch_one(conn, "select document = %s::jsonb as same from content_snapshot "
                               "where id = %s", (Jsonb(dict(doc)), lc.version_id))
    if not same["same"]:
        raise ValueError(f"content_snapshot {lc.version_id} exists with a different document")
    return lc.version_id


def import_snapshot_file(conn: psycopg.Connection, path: Path, now: datetime) -> str:
    return import_snapshot(conn, json.loads(Path(path).read_text(encoding="utf-8")), now)


def activate(conn: psycopg.Connection, cv_id: str, now: datetime) -> None:
    with conn.transaction():
        conn.execute("insert into content_active (singleton, content_version, activated_at) "
                     "values (true, %s, %s) on conflict (singleton) do update set "
                     "content_version = excluded.content_version, "
                     "activated_at = excluded.activated_at", (cv_id, now))


def active_version(conn: psycopg.Connection) -> str | None:
    r = fetch_one(conn, "select content_version from content_active")
    return r["content_version"] if r else None


def initialize_content(conn: psycopg.Connection, now: datetime) -> LoadedContent:
    """A new personal wiki starts empty; never seed it with research/demo content."""
    with conn.transaction():
        # Serialize first starts so they cannot replace a content version activated by each other.
        conn.execute("select pg_advisory_xact_lock(184245731)")
        if active_version(conn) is None:
            cv = import_snapshot(conn, {
                "content_version": {"id": "cv_empty", "created_at": "2026-10-27T00:00:00Z",
                                    "snapshot_ref": "builtin:empty"},
                "concepts": [], "edges": [],
            }, now)
            activate(conn, cv, now)
        return load_content(conn)


def load_content(conn: psycopg.Connection, cv_id: str | None = None) -> LoadedContent:
    cv = cv_id or active_version(conn)
    if cv is None:
        raise RuntimeError("no active content version; run `python -m evowiki.service "
                           "import-content <graph.json> --activate`")
    r = fetch_one(conn, "select document from content_snapshot where id = %s", (cv,))
    if r is None:
        raise RuntimeError(f"content_snapshot {cv} not found")
    return LoadedContent.from_document(r["document"])
