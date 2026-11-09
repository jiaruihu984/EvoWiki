"""One pinned connection per (thread, pool), shared by every store on that pool.

`transaction(pool)` opens one connection and one transaction and pins it to the current thread for
that pool; every `connection(pool)` call on the same thread and pool inside the block gets the
pinned connection, so writes of different store classes (`PgRsiStore` /
`PgLayer2Store`) commit or roll back together. Nested `transaction(pool)` blocks become
savepoints. Outside a block, `connection(pool)` is a plain pooled connection (committed on clean
exit, rolled back on error)."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool

_local = threading.local()


def _held(pool: ConnectionPool) -> psycopg.Connection | None:
    pinned: dict[int, psycopg.Connection] = getattr(_local, "pinned", None) or {}
    return pinned.get(id(pool))


@contextmanager
def transaction(pool: ConnectionPool) -> Iterator[None]:
    held = _held(pool)
    if held is not None:
        with held.transaction():
            yield
        return
    if getattr(_local, "pinned", None) is None:
        _local.pinned = {}
    with pool.connection() as conn, conn.transaction():
        _local.pinned[id(pool)] = conn
        try:
            yield
        finally:
            _local.pinned.pop(id(pool), None)


@contextmanager
def connection(pool: ConnectionPool) -> Iterator[psycopg.Connection]:
    held = _held(pool)
    if held is not None:
        yield held
        return
    with pool.connection() as conn:
        yield conn


def is_pinned(pool: ConnectionPool) -> bool:
    """Whether this thread is inside a `transaction(pool)` block (so `connection(pool)` joins it)."""
    return _held(pool) is not None
