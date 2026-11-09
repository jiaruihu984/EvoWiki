"""Per-user prompt guidance overrides: the H1 keys of the harness pack (migration 0063, which
folded the 0058 prompt_override table in). Plain functions over a connection; the caller owns the
transaction. Rows live in `harness_value` under `H1.<step>`; every change is a harness_version row
too."""

from datetime import datetime

import psycopg

from evowiki.harness.keys import h1_key
from evowiki.store import harness
from evowiki.store.db import fetch_all, fetch_one

WHY = "你在 Prompt 设置里改的"     # same text as bot/prompts.WHY (the history row's why)
# The `why` of the removal row scripts/archive_h1_understand.py writes for an `H1.understand`
# wording in the old 读懂 format (reading-mode spec §7 item 1): undoing an older change of that
# step then says why it cannot (`retired`), not 「先撤销后来的那次」 — the cleanup is no ledger row.
FORMAT_RETIRED_WHY = "这一步的输出格式改了（按原文分段带读），按老格式写的写法已存档并清掉"


def lock_step(conn: psycopg.Connection, user_id: str, step: str) -> None:
    """Serialize changes of one user's pack until the transaction ends (a missing row cannot be
    locked with FOR UPDATE, so a transaction-scoped advisory lock)."""
    harness.lock_key(conn, user_id)


def get_override(conn: psycopg.Connection, user_id: str, step: str) -> str | None:
    r = fetch_one(conn, "select value from harness_value where user_id = %s and key = %s",
                  (user_id, h1_key(step)))
    return None if r is None else r["value"]


def list_overrides(conn: psycopg.Connection, user_id: str) -> dict[str, str]:
    rows = fetch_all(conn, "select key, value from harness_value where user_id = %s "
                           "and key like 'H1.%%'", (user_id,))
    return {r["key"][3:]: r["value"] for r in rows}


def set_override(conn: psycopg.Connection, user_id: str, step: str, text: str | None,
                 now: datetime, *, source: harness.Source = "user") -> None:
    """`text` None removes the override (back to the default wording). `source`: who wrote
    `text` (an undo puts back the source the text had, `source_before`)."""
    harness.set_value(conn, user_id, h1_key(step), text, source=source, why=WHY, now=now)


def source_before(conn: psycopg.Connection, user_id: str, step: str, text: str | None,
                  at: datetime) -> harness.Source:
    """Who wrote `text` as step `step`'s wording as of `at`: the source of the latest version row
    that set the key to it (an improver's text stays the improver's after an undo, so the harness
    loop may still change it). "user" for None (no override) or a text no row set."""
    if text is None:
        return "user"
    r = fetch_one(conn, "select source from harness_version where user_id = %s and key = %s "
                        "and after = to_jsonb(%s::text) and created_at <= %s "
                        "order by version desc limit 1", (user_id, h1_key(step), text, at))
    return "user" if r is None else r["source"]


def newer_change(conn: psycopg.Connection, user_id: str, step: str, created_at: datetime,
                 action_id: str) -> bool:
    """Whether an executed `prompt_override` ledger row of the same user and step is newer than
    the row (`created_at`, `action_id`) (ordered by created_at, then action_id)."""
    r = fetch_one(
        conn,
        "select 1 as x from agent_action where user_id = %s and kind = 'prompt_override' "
        "and status = 'executed' and content->>'step' = %s "
        "and (created_at, action_id) > (%s, %s) limit 1",
        (user_id, step, created_at, action_id))
    return r is not None


def superseded_ids(conn: psycopg.Connection, user_id: str) -> set[str]:
    """Action ids of the user's executed `prompt_override` rows that have a newer executed row of
    the same step (same ordering as `newer_change`): their undo is refused until the newer one is
    undone."""
    rows = fetch_all(
        conn,
        "select action_id from (select action_id, row_number() over (partition by content->>'step' "
        "order by created_at desc, action_id desc) as rn from agent_action "
        "where user_id = %s and kind = 'prompt_override' and status = 'executed') t where rn > 1",
        (user_id,))
    return {r["action_id"] for r in rows}


def retired(conn: psycopg.Connection, user_id: str, step: str) -> bool:
    """Whether the latest change of step `step` is the old-format cleanup (FORMAT_RETIRED_WHY)."""
    r = fetch_one(conn, "select why from harness_version where user_id = %s and key = %s "
                        "order by version desc limit 1", (user_id, h1_key(step)))
    return r is not None and r["why"] == FORMAT_RETIRED_WHY
