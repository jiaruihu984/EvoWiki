from datetime import UTC, datetime

import pytest

from evowiki.content.models import Lang
from evowiki.store.locks import user_writer
from evowiki.store.users import (
    SessionError,
    UserExists,
    UserRow,
    close_session,
    create_user,
    get_session,
    get_user,
    list_user_ids,
    start_session,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def test_user_and_session_lifecycle(pool):
    row = UserRow(user_id="u1", background="none", condition="S2", lang=Lang.EN, created_at=T0)
    with pool.connection() as conn:
        create_user(conn, row)
        with pytest.raises(UserExists):
            create_user(conn, row)
        assert get_user(conn, "u1") == row
        assert get_user(conn, "u1").model_dump_json() == row.model_dump_json()
        assert get_user(conn, "nobody") is None
        assert list_user_ids(conn) == ["u1"]
    with user_writer(pool, "u1") as tx:
        start_session(tx, "s1", T0)
        close_session(tx, "s1", T0)
    with pool.connection() as conn:
        assert get_session(conn, "s1").ended_at == T0
        assert get_session(conn, "s1").model_dump_json() == (
            '{"session_id":"s1","user_id":"u1","started_at":"2026-10-01T00:00:00Z",'
            '"ended_at":"2026-10-01T00:00:00Z"}')
    with pytest.raises(SessionError), user_writer(pool, "u1") as tx:
        close_session(tx, "s1", T0)
