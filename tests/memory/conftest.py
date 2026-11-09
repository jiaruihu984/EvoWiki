"""Shared helpers for the memory tests."""

from datetime import UTC, datetime

from evowiki.content.models import Lang
from evowiki.store.users import UserRow, create_user

T = datetime(2026, 10, 27, 9, tzinfo=UTC)


def seed_user_and_session(pool, user_id: str, session_id: str) -> None:
    """An app_user (if new) and an assistant_session row, for the memory tables' foreign keys."""
    with pool.connection() as conn:
        if conn.execute("select 1 from app_user where user_id = %s", (user_id,)).fetchone() is None:
            create_user(conn, UserRow(user_id=user_id, background="none", condition="S2",
                                      lang=Lang.ZH, created_at=T))
        conn.execute("insert into assistant_session (session_id, user_id, page_id, "
                     "note_concept_id, created_at) values (%s, %s, 'p1', null, %s)",
                     (session_id, user_id, T))
