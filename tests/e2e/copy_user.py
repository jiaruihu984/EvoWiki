"""Test-only: one user's rows of a source database (the dev DB) copied into a throwaway database,
so a real-Obsidian walkthrough runs over a COPY of that user's real data (chat tails, 2026-10-24).

The source is opened read-only (`default_transaction_read_only=on`: a write there fails); only the
destination is written, with foreign-key triggers off (`session_replication_role = replica`) so
the tables can come in any order. Copied: every table with a `user_id` column (rows of that user),
then, until nothing more comes, every table holding a foreign key to rows already copied (e.g. a
session's entries); only columns both databases have; rows already there are kept (ON CONFLICT DO
NOTHING). Tables the destination does not have are skipped (the source may be a migration
behind)."""

from collections.abc import Iterable

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

READ_ONLY = "-c default_transaction_read_only=on"


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "select column_name from information_schema.columns where table_schema='public' "
        "and table_name=%s order by ordinal_position", (table,))]


def _json_columns(conn: psycopg.Connection, table: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "select column_name from information_schema.columns where table_schema='public' "
        "and table_name=%s and data_type in ('json', 'jsonb')", (table,))}


def _tables(conn: psycopg.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "select table_name from information_schema.tables where table_schema='public' "
        "and table_type='BASE TABLE'")}


def _fks(conn: psycopg.Connection) -> list[tuple[str, str, str, str]]:
    """(child table, child column, parent table, parent column) of every single-column FK. Read from
    pg_catalog: information_schema shows only the constraints of tables the role owns, and the dev DB
    is read as evowiki_app since the 2026-10-28 cutover (no FK found: a session's entries were not copied)."""
    return [tuple(r) for r in conn.execute("""
        select c.relname, a.attname, p.relname, pa.attname
        from pg_constraint k
        join pg_class c on c.oid = k.conrelid
        join pg_namespace n on n.oid = c.relnamespace
        join pg_class p on p.oid = k.confrelid
        join pg_attribute a on a.attrelid = k.conrelid and a.attnum = k.conkey[1]
        join pg_attribute pa on pa.attrelid = k.confrelid and pa.attnum = k.confkey[1]
        where k.contype = 'f' and n.nspname = 'public' and array_length(k.conkey, 1) = 1""")]


def _copy(src: psycopg.Connection, dst: psycopg.Connection, table: str, where: sql.Composable,
          params: Iterable[object]) -> list[tuple]:
    generated = {r[0] for r in dst.execute(
        "select column_name from information_schema.columns where table_schema='public' "
        "and table_name=%s and is_generated='ALWAYS'", (table,))}
    cols = [c for c in _columns(src, table) if c in set(_columns(dst, table)) - generated]
    names = sql.SQL(", ").join(map(sql.Identifier, cols))
    rows = src.execute(sql.SQL("select {} from {} where ").format(names, sql.Identifier(table))
                       + where, list(params)).fetchall()
    if rows:
        ins = sql.SQL("insert into {} ({}) overriding system value values ({}) "
                      "on conflict do nothing").format(
            sql.Identifier(table), names, sql.SQL(", ").join(sql.Placeholder() * len(cols)))
        js = _json_columns(dst, table)
        put = [tuple(Jsonb(v) if c in js and v is not None else v for c, v in zip(cols, r, strict=True))
               for r in rows]
        with dst.cursor() as cur:
            cur.executemany(ins, put)
    return [dict(zip(cols, r, strict=True)) for r in rows]


def copy_user(source_dsn: str, dest_url: str, user_id: str) -> dict[str, int]:
    """Copies `user_id`'s rows; returns table → rows read from the source."""
    counts: dict[str, int] = {}
    with psycopg.connect(source_dsn, options=READ_ONLY) as src, psycopg.connect(dest_url) as dst:
        dst.execute("set session_replication_role = replica")
        both = _tables(src) & _tables(dst)
        copied: dict[str, list[dict]] = {}
        for t in sorted(both):
            if "user_id" in _columns(src, t):
                copied[t] = _copy(src, dst, t, sql.SQL("user_id = %s"), [user_id])
                counts[t] = len(copied[t])
        fks = [f for f in _fks(src) if f[0] in both and f[2] in both]
        grew = True
        while grew:
            grew = False
            for child, col, parent, pcol in fks:
                if child in copied or parent not in copied:
                    continue
                keys = sorted({str(r[pcol]) for r in copied[parent] if r.get(pcol) is not None})
                if not keys:
                    continue
                copied[child] = _copy(src, dst, child, sql.SQL("{}::text = any(%s)").format(
                    sql.Identifier(col)), [keys])
                counts[child] = len(copied[child])
                grew = True
        # the copied ids of serial columns (event seqs …): the sequences move past them, or the
        # next insert would collide with a copied row
        for t, col in dst.execute(
                "select table_name, column_name from information_schema.columns where "
                "table_schema='public' and (column_default like 'nextval(%%' or is_identity='YES')"
                ).fetchall():
            if copied.get(t):
                dst.execute(sql.SQL("select setval(pg_get_serial_sequence(%s, %s), "
                                    "greatest((select max({}) from {}), 1))").format(
                    sql.Identifier(col), sql.Identifier(t)), [t, col])
        dst.commit()
    return counts
