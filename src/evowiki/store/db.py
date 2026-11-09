import os
from collections.abc import Mapping
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row, tuple_row
from psycopg_pool import ConnectionPool

ENV_VAR = "EVOWIKI_DATABASE_URL"
# The role that owns the tables and runs migrations (0082: the service connects as evowiki_app, which
# owns nothing in public). Unset: migrations run on EVOWIKI_DATABASE_URL.
ADMIN_ENV_VAR = "EVOWIKI_ADMIN_DATABASE_URL"
APP_ROLE = "evowiki_app"


def database_url(env: Mapping[str, str] = os.environ) -> str:
    url = env.get(ENV_VAR)
    if not url:
        raise RuntimeError(f"{ENV_VAR} is not set; point it at a Postgres with pgvector")
    return url


def admin_database_url(env: Mapping[str, str] = os.environ) -> str:
    return env.get(ADMIN_ENV_VAR) or database_url(env)


def role_problem(conn: psycopg.Connection) -> str | None:
    """Why this connection's role may not serve (0082's role split would not hold), or None: a
    superuser, a member of evowiki_forget_owner, or the owner of a table in public."""
    with conn.cursor(row_factory=tuple_row) as cur:     # pooled connections read dict rows
        superuser, forget_owner, owned = cur.execute(
            "select r.rolsuper, pg_has_role(current_user, 'evowiki_forget_owner', 'MEMBER'), "
            "  (select count(*) from pg_tables where schemaname = 'public' and tableowner = current_user) "
            "from pg_roles r where r.rolname = current_user").fetchone()
    if superuser or forget_owner or owned:
        return (f"database role {conn.info.user} may not serve: it is "
                f"{'a superuser' if superuser else 'evowiki_forget_owner' if forget_owner else 'a table owner'}; "
                f"connect as {APP_ROLE} (EVOWIKI_DATABASE_URL) and keep the owner in {ADMIN_ENV_VAR}")
    return None


def _configure(conn: psycopg.Connection) -> None:
    conn.row_factory = dict_row
    register_vector(conn)
    conn.execute("set timezone to 'UTC'")  # timestamptz reads back as UTC everywhere
    conn.commit()  # register_vector reads pg_type; end that implicit transaction


def make_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> ConnectionPool:
    pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, configure=_configure,
                          kwargs={"autocommit": False}, open=False)
    pool.open(wait=True)
    return pool


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = ()) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = ()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()
