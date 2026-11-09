"""Chat attachments nobody kept (Q7): 30 days after its last use, a source whose every use is
`ask` and that no knowledge point links to loses its original, sections and chunks
(`soft_delete`; the row and its title stay, the chat shows it as cleaned up). Run at service start and after
every upload; the query is cheap, there is no scheduler."""

import logging
from datetime import datetime, timedelta

from psycopg_pool import ConnectionPool

from evowiki.sources import store
from evowiki.sources.runs import SourceRuns
from evowiki.store.locks import user_writer

log = logging.getLogger("evowiki.sources.cleanup")

KEEP_ASK_DAYS = 30
BATCH = 100


def sweep_ask_attachments(pool: ConnectionPool, runs: SourceRuns, now: datetime) -> int:
    """Deletes the stale ask-only sources (each run cancelled first, as the delete route does);
    returns how many. Raises on a database error (callers log it)."""
    before = now - timedelta(days=KEEP_ASK_DAYS)
    n = 0
    while True:
        with pool.connection() as conn:
            stale = store.stale_ask_only(conn, before=before, limit=BATCH)
        gone = 0
        for user_id, source_id in stale:
            with user_writer(pool, user_id) as tx:   # a use or link added meanwhile keeps it
                if not store.soft_delete_stale_ask(tx.conn, user_id, source_id, before=before,
                                                   now=now):
                    continue
                runs.cancel(user_id, source_id)
                gone += 1
        n += gone
        if len(stale) < BATCH or gone == 0:
            break
    if n:
        log.info("cleaned up %d chat attachments older than %d days", n, KEEP_ASK_DAYS)
    return n
