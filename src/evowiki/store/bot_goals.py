"""Weekly goal reviews, parked goals and reading time (migration 0054, B5 spec §1 / §1.6). Plain
functions over a connection; the caller owns the transaction."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from evowiki.store.db import fetch_all, fetch_one

# --- weekly review snapshots --------------------------------------------------------------


@dataclass(frozen=True)
class ReviewRow:
    path_id: str
    week_start: date
    situation: str
    facts: dict[str, Any]
    trigger_id: str | None
    at: datetime


def put_review(conn: psycopg.Connection, user_id: str, *, week_start: date, path_id: str,
               situation: str, facts: dict[str, Any], trigger_id: str | None,
               at: datetime) -> bool:
    """False when the path was already reviewed that week (nothing is written)."""
    r = fetch_one(
        conn,
        "insert into bot_goal_review (user_id, week_start, path_id, situation, facts, trigger_id, "
        "at) values (%s,%s,%s,%s,%s,%s,%s) on conflict do nothing returning path_id",
        (user_id, week_start, path_id, situation, Jsonb(facts), trigger_id, at))
    return r is not None


def week_reviewed(conn: psycopg.Connection, user_id: str, week_start: date) -> bool:
    return fetch_one(conn, "select 1 as x from bot_goal_review where user_id = %s "
                           "and week_start = %s limit 1", (user_id, week_start)) is not None


def _review(r: dict) -> ReviewRow:
    return ReviewRow(path_id=r["path_id"], week_start=r["week_start"], situation=r["situation"],
                     facts=r["facts"], trigger_id=r["trigger_id"], at=r["at"])


def review_before(conn: psycopg.Connection, user_id: str, path_id: str,
                  week_start: date) -> ReviewRow | None:
    """The path's newest review of a week before `week_start` (the "比上周" snapshot)."""
    r = fetch_one(conn, "select * from bot_goal_review where user_id = %s and path_id = %s "
                        "and week_start < %s order by week_start desc limit 1",
                  (user_id, path_id, week_start))
    return None if r is None else _review(r)


def latest_reviews(conn: psycopg.Connection, user_id: str) -> dict[str, ReviewRow]:
    """path id -> its newest review."""
    rows = fetch_all(conn, "select distinct on (path_id) * from bot_goal_review "
                           "where user_id = %s order by path_id, week_start desc", (user_id,))
    return {r["path_id"]: _review(r) for r in rows}


# --- parked goals -------------------------------------------------------------------------

def park(conn: psycopg.Connection, user_id: str, path_id: str, at: datetime) -> None:
    conn.execute(
        "insert into bot_goal_state (user_id, path_id, parked_at, resumed_at) "
        "values (%s, %s, %s, null) on conflict (user_id, path_id) do update set "
        "parked_at = case when bot_goal_state.resumed_at is null then bot_goal_state.parked_at "
        "else excluded.parked_at end, resumed_at = null", (user_id, path_id, at))


def resume(conn: psycopg.Connection, user_id: str, path_id: str, at: datetime) -> None:
    conn.execute("update bot_goal_state set resumed_at = %s where user_id = %s and path_id = %s "
                 "and resumed_at is null", (at, user_id, path_id))


def parked(conn: psycopg.Connection, user_id: str) -> dict[str, datetime]:
    """path id -> when it was parked, for the goals parked now."""
    rows = fetch_all(conn, "select path_id, parked_at from bot_goal_state where user_id = %s "
                           "and resumed_at is null", (user_id,))
    return {r["path_id"]: r["parked_at"] for r in rows}


# --- reading time -------------------------------------------------------------------------

def add_reading(conn: psycopg.Connection, user_id: str, batch_id: str,
                rows: Sequence[tuple[str, date, int]], at: datetime) -> bool:
    """Add one batch's (page, local day, seconds) rows. False (nothing added) when the batch was
    already counted."""
    r = fetch_one(conn, "insert into reading_batch (user_id, batch_id, at) values (%s, %s, %s) "
                        "on conflict do nothing returning batch_id", (user_id, batch_id, at))
    if r is None:
        return False
    for page_id, day, seconds in rows:
        conn.execute(
            "insert into reading_time (user_id, page_id, day, seconds, updated_at) "
            "values (%s, %s, %s, %s, %s) on conflict (user_id, page_id, day) do update set "
            "seconds = reading_time.seconds + excluded.seconds, updated_at = excluded.updated_at",
            (user_id, page_id, day, seconds, at))
    return True


def reading_by_page(conn: psycopg.Connection, user_id: str, page_ids: Iterable[str],
                    first_day: date, last_day: date) -> dict[str, int]:
    """page id -> seconds read on local days first_day..last_day (inclusive); pages with none
    are left out."""
    ids = sorted(set(page_ids))
    if not ids:
        return {}
    rows = fetch_all(conn, "select page_id, sum(seconds) as s from reading_time "
                           "where user_id = %s and page_id = any(%s) and day >= %s and day <= %s "
                           "group by page_id", (user_id, ids, first_day, last_day))
    return {r["page_id"]: int(r["s"]) for r in rows}


def first_reading_day(conn: psycopg.Connection, user_id: str) -> date | None:
    """The first local day the plugin reported reading time for the user (None: never)."""
    r = fetch_one(conn, "select min(day) as d from reading_time where user_id = %s", (user_id,))
    return r["d"]
