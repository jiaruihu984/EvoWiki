"""Re-embed sources whose stored vectors belong to another vector space than the current
embedder's (the embedding model changed: `model_version` is the model name + dimension).

While a source is stale, search compares nothing across spaces: `vector_hits` only looks at rows
tagged with the current embedder, so the stale chunks are found by BM25 alone (as chunks that are
not embedded yet are). Each batch becomes searchable the moment it is written. The sweep runs in a
daemon thread started with the app, one source at a time, so no request waits for it. A failure is
logged and the sweep stops; the rows stay as they were and the next start tries again."""

import logging
import threading

from psycopg_pool import ConnectionPool

from evowiki.retrieval.embed import Embedder
from evowiki.sources import store
from evowiki.sources.importer import _Deleted, embed_pending

log = logging.getLogger("evowiki.sources.reembed")


def reembed_stale(pool: ConnectionPool, embedder: Embedder,
                  stop: threading.Event | None = None) -> int:
    """Re-embed every stale source; returns the number of chunks re-embedded. Raises on the first
    failure (nothing is skipped silently)."""
    total = 0
    done: set[tuple[str, str]] = set()
    while stop is None or not stop.is_set():
        with pool.connection() as conn:
            todo = [k for k in store.sources_with_stale_vectors(conn, embedder) if k not in done]
            row = store.get_source(conn, *todo[0]) if todo else None
        if not todo:
            return total
        uid, sid = todo[0]
        done.add((uid, sid))   # a source that stays stale (deleted mid-way) is not retried here
        if row is None or row.deleted_at is not None or row.status == "reading":
            continue
        try:
            n = embed_pending(pool, uid, sid, embedder, gen=row.run_gen)
        except _Deleted:
            log.info("source %s/%s changed during re-embedding; its new import owns the vectors", uid, sid)
            continue
        log.info("re-embedded %d chunks of source %s/%s under %s", n, uid, sid,
                 embedder.model_version)
        total += n
    return total


def start_background(pool: ConnectionPool, embedder: Embedder) -> tuple[threading.Thread,
                                                                        threading.Event]:
    """Run `reembed_stale` in a daemon thread; set the returned event to make it stop between
    sources."""
    stop = threading.Event()

    def run() -> None:
        try:
            reembed_stale(pool, embedder, stop)
        except Exception:
            log.exception("re-embedding of sources stopped; it is retried at the next start")

    t = threading.Thread(target=run, name="reembed-sources", daemon=True)
    t.start()
    return t, stop
