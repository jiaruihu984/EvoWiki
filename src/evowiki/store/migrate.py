import logging
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_KEY = 0x45766F57  # 'EvoW'


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations in file-name order, each in its own transaction. A migration's
    NOTICEs (its report, e.g. 0080's lines moved per user) are printed and logged."""
    def report(d: psycopg.errors.Diagnostic) -> None:
        if d.message_primary:
            print(f"[migrate] {d.message_primary}", flush=True)
            log.info("migration notice: %s", d.message_primary)
    conn.add_notice_handler(report)
    try:
        with conn.transaction():
            conn.execute("create table if not exists schema_migrations "
                         "(id text primary key, applied_at timestamptz not null default now())")
        applied: list[str] = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            with conn.transaction():
                conn.execute("select pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                done = conn.execute("select 1 from schema_migrations where id = %s",
                                    (path.stem,)).fetchone()
                if done:
                    continue
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("insert into schema_migrations (id) values (%s)", (path.stem,))
                applied.append(path.stem)
    finally:
        conn.remove_notice_handler(report)
    return applied
