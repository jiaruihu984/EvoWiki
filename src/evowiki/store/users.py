from datetime import datetime

import psycopg
from pydantic import AwareDatetime, BaseModel, ConfigDict

from evowiki.schema import Lang
from evowiki.store.db import fetch_all, fetch_one
from evowiki.store.locks import UserTx


class UserExists(ValueError):
    pass


class UnknownUser(LookupError):
    """No such registered user (404)."""


class SessionError(ValueError):
    pass


class UserRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    background: str
    condition: str
    lang: Lang
    created_at: AwareDatetime


class SessionRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    user_id: str
    started_at: AwareDatetime
    ended_at: AwareDatetime | None


def create_user(conn: psycopg.Connection, row: UserRow) -> None:
    try:
        with conn.transaction():
            conn.execute(
                "insert into app_user (user_id, background, condition, lang, created_at) "
                "values (%s, %s, %s, %s, %s)",
                (row.user_id, row.background, row.condition, row.lang.value, row.created_at))
    except psycopg.errors.UniqueViolation as exc:
        raise UserExists(row.user_id) from exc


def get_user(conn: psycopg.Connection, user_id: str) -> UserRow | None:
    r = fetch_one(conn, "select * from app_user where user_id = %s", (user_id,))
    return UserRow.model_validate(r) if r else None


def list_user_ids(conn: psycopg.Connection) -> list[str]:
    return [r["user_id"] for r in fetch_all(conn, "select user_id from app_user order by 1")]


def start_session(tx: UserTx, session_id: str, now: datetime) -> None:
    tx.conn.execute("insert into user_session (session_id, user_id, started_at) "
                    "values (%s, %s, %s)", (session_id, tx.user_id, now))


def get_session(conn: psycopg.Connection, session_id: str) -> SessionRow | None:
    r = fetch_one(conn, "select * from user_session where session_id = %s", (session_id,))
    return SessionRow.model_validate(r) if r else None


def close_session(tx: UserTx, session_id: str, now: datetime) -> None:
    s = get_session(tx.conn, session_id)
    if s is None or s.user_id != tx.user_id:
        raise SessionError(f"unknown session {session_id} for user {tx.user_id}")
    if s.ended_at is not None:
        raise SessionError(f"session {session_id} already ended")
    tx.conn.execute("update user_session set ended_at = %s where session_id = %s",
                    (now, session_id))
