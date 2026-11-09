from collections.abc import Mapping
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from evowiki.store.db import fetch_one


def register_policy(conn: psycopg.Connection, policy_id: str, document: Mapping[str, Any],
                    schema_version: str, now: datetime) -> None:
    with conn.transaction():
        conn.execute("insert into policy_version (id, scope, theta_or_delta, schema_version, "
                     "created_at) values (%s, 'global', %s, %s, %s) on conflict (id) do nothing",
                     (policy_id, Jsonb(dict(document)), schema_version, now))
        r = fetch_one(conn, "select theta_or_delta = %s::jsonb as same from policy_version "
                            "where id = %s", (Jsonb(dict(document)), policy_id))
    if not r["same"]:
        raise ValueError(f"policy_version {policy_id} exists with a different document")


def get_policy_document(conn: psycopg.Connection, policy_id: str) -> dict | None:
    r = fetch_one(conn, "select theta_or_delta from policy_version where id = %s", (policy_id,))
    return r["theta_or_delta"] if r else None
