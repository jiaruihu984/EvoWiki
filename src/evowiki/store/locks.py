from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class UserTx:
    """A transaction holding the user's single-writer lock (spec §5.7). Only user_writer makes one."""

    conn: psycopg.Connection
    user_id: str


def xact_lock(conn: psycopg.Connection, key: str) -> None:
    """Take the transaction-scoped advisory lock named `key` (released at commit or rollback).
    Different key strings are different locks: use one prefix per purpose (`bot:`, `undo:`...)."""
    conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


@contextmanager
def session_lock(pool: ConnectionPool, key: str) -> Iterator[bool]:
    """A nonblocking lock across model calls, with no open transaction while work runs."""
    with pool.connection() as conn:
        got = conn.execute("select pg_try_advisory_lock(hashtextextended(%s, 0))", (key,)).fetchone()
        conn.commit()
        acquired = bool(got["pg_try_advisory_lock"])
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
                conn.commit()


@contextmanager
def user_writer(pool: ConnectionPool, user_id: str) -> Iterator[UserTx]:
    with pool.connection() as conn, conn.transaction():
        xact_lock(conn, f"evowiki.user:{user_id}")
        yield UserTx(conn, user_id)
