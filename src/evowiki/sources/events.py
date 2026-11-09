"""What happens when a source is put to a use (design §8): one place for the upload route,
`POST /wiki/sources/{id}/uses` and, later, the Bot (materials D reuses it as is)."""

import logging
from datetime import datetime

import psycopg

from evowiki.sources import store
from evowiki.sources.models import Use

log = logging.getLogger("evowiki.sources.events")


def source_added(conn: psycopg.Connection, user_id: str, source_id: str, use: Use,
                 now: datetime, *, goal_id: str | None = None, page_id: str | None = None,
                 session_id: str | None = None) -> str:
    """Records the use (a `source_use` row, in the caller's transaction) and returns its id.
    A deleted or foreign source raises LookupError. A `supplement` use starts the materials-D
    match once this transaction has committed and the import has ended (`match_flow`; a
    material still reading is matched by the import's own hook); an `exam` use starts the
    materials-F extraction the same way (`exam_flow`)."""
    use_id = store.add_use(conn, user_id, source_id, use, goal_id=goal_id, page_id=page_id,
                           session_id=session_id, now=now)
    log.info("source %s/%s added for %s (goal %s, page %s, session %s): use %s", user_id,
             source_id, use, goal_id, page_id, session_id, use_id)
    if use == "supplement":      # materials D: find the pages it covers (after the commit)
        from evowiki.sources.match_flow import start_match_after_use
        start_match_after_use(user_id, source_id, use_id)
    elif use == "exam":          # materials F: extract and attach its questions (after the commit)
        from evowiki.sources.exam_flow import start_exam_after_use
        start_exam_after_use(user_id, source_id, use_id)
    return use_id
