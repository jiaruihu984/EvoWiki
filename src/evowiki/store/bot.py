"""Bot state (migration 0050): settings, event cursor, intents, daily slots, check log,
suggestions, learned suppression and runs. Plain functions over a connection; the caller owns
the transaction. Bot state never goes into the wiki event log."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from evowiki.agent.effects import payload_hash
from evowiki.bot.settings import DEFAULT_SETTINGS, BotSettings
from evowiki.bot.suppression import Counters
from evowiki.store.db import fetch_all, fetch_one

MEMORY_FORGET_KIND = "memory_forget"    # memory/forget_fact.py


def forgotten_words(conn: psycopg.Connection, user_id: str) -> list[str]:
    """What his confirmed 「忘掉…」 rows name; an undone row no longer counts."""
    rows = fetch_all(conn, "select distinct w from agent_action, "
                           "jsonb_array_elements_text(content -> 'words') as w "
                           "where user_id = %s and kind = %s and status = 'executed'",
                     (user_id, MEMORY_FORGET_KIND))
    return [r["w"] for r in rows]


@dataclass(frozen=True)
class TriggerRow:
    trigger_id: str
    user_id: str
    kind: str
    rule_id: str
    rule_version: int
    source_key: str
    dedup_key: str
    route: str
    evidence: list
    payload: dict
    priority: int
    reason: str
    created_at: datetime
    placement: str = "today"
    page_id: str | None = None
    status: str = "new"
    gate_outcome: str | None = None
    gate_reason: str | None = None
    run_id: str | None = None
    gated_at: datetime | None = None
    title: str = ""                  # what the intent is about (the 最近 list)
    gate_detail: str = ""            # the gate's plain-Chinese explanation


@dataclass(frozen=True)
class SuggestionRow:
    suggestion_id: str
    user_id: str
    kind: str
    title: str
    why: str
    action: dict
    placement: str
    priority: int
    dedup_key: str
    expires_at: datetime
    created_at: datetime
    trigger_id: str | None = None
    run_id: str | None = None
    page_id: str | None = None
    status: str = "pending"
    cost_usd: float = 0.0
    reacted_at: datetime | None = None


def _row(cls: type, r: dict) -> Any:
    return cls(**{k: r[k] for k in cls.__dataclass_fields__})


# --- settings -----------------------------------------------------------------------------

def get_settings(conn: psycopg.Connection, user_id: str) -> BotSettings:
    r = fetch_one(conn, "select settings from bot_settings where user_id = %s", (user_id,))
    return DEFAULT_SETTINGS if r is None else BotSettings.model_validate(r["settings"])


def put_settings(conn: psycopg.Connection, user_id: str, s: BotSettings, now: datetime) -> None:
    conn.execute(
        "insert into bot_settings (user_id, settings, updated_at) values (%s, %s, %s) "
        "on conflict (user_id) do update set settings = excluded.settings, "
        "updated_at = excluded.updated_at",
        (user_id, Jsonb(s.model_dump(mode="json")), now))


# --- cursor -------------------------------------------------------------------------------

def lock_cursor(conn: psycopg.Connection, user_id: str, now: datetime) -> tuple[int, int]:
    conn.execute("insert into trigger_cursor (user_id, updated_at) values (%s, %s) "
                 "on conflict do nothing", (user_id, now))
    r = fetch_one(conn, "select wiki_seq, aspect_seq from trigger_cursor "
                        "where user_id = %s for update", (user_id,))
    return r["wiki_seq"], r["aspect_seq"]


def set_cursor(conn: psycopg.Connection, user_id: str, wiki_seq: int, aspect_seq: int,
               now: datetime) -> None:
    conn.execute("update trigger_cursor set wiki_seq = %s, aspect_seq = %s, updated_at = %s "
                 "where user_id = %s", (wiki_seq, aspect_seq, now, user_id))


# --- triggers -----------------------------------------------------------------------------

def insert_trigger(conn: psycopg.Connection, row: TriggerRow) -> str | None:
    r = fetch_one(
        conn,
        "insert into bot_trigger (trigger_id, user_id, kind, rule_id, rule_version, source_key, "
        "dedup_key, route, evidence, payload, priority, reason, placement, page_id, status, "
        "gate_outcome, gate_reason, run_id, created_at, gated_at, title, gate_detail) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "on conflict (user_id, source_key) do nothing returning trigger_id",
        (row.trigger_id, row.user_id, row.kind, row.rule_id, row.rule_version, row.source_key,
         row.dedup_key, row.route, Jsonb(row.evidence), Jsonb(row.payload), row.priority,
         row.reason, row.placement, row.page_id, row.status, row.gate_outcome, row.gate_reason,
         row.run_id, row.created_at, row.gated_at, row.title, row.gate_detail))
    return None if r is None else r["trigger_id"]


def trigger_exists(conn: psycopg.Connection, user_id: str, source_key: str) -> bool:
    return fetch_one(conn, "select 1 as x from bot_trigger where user_id = %s and source_key = %s",
                     (user_id, source_key)) is not None


def _trigger(conn: psycopg.Connection, trigger_id: str, suffix: str) -> TriggerRow | None:
    r = fetch_one(conn, "select * from bot_trigger where trigger_id = %s" + suffix, (trigger_id,))
    return None if r is None else _row(TriggerRow, r)


def lock_trigger(conn: psycopg.Connection, trigger_id: str) -> TriggerRow | None:
    return _trigger(conn, trigger_id, " for update")


def get_trigger(conn: psycopg.Connection, trigger_id: str) -> TriggerRow | None:
    return _trigger(conn, trigger_id, "")


def set_trigger_gate(conn: psycopg.Connection, trigger_id: str, *, status: str,
                     outcome: str | None, reason: str | None, at: datetime,
                     run_id: str | None = None, detail: str = "") -> None:
    conn.execute(
        "update bot_trigger set status = %s, gate_outcome = %s, gate_reason = %s, "
        "gated_at = %s, run_id = coalesce(%s, run_id), "
        "gate_detail = coalesce(nullif(%s, ''), gate_detail) where trigger_id = %s",
        (status, outcome, reason, at, run_id, detail, trigger_id))


def new_triggers(conn: psycopg.Connection, older_than: datetime,
                 limit: int = 200) -> list[TriggerRow]:
    rows = fetch_all(conn, "select * from bot_trigger where status = 'new' and created_at < %s "
                           "order by created_at limit %s", (older_than, limit))
    return [_row(TriggerRow, r) for r in rows]


# --- slots / checks -----------------------------------------------------------------------

def record_slot(conn: psycopg.Connection, user_id: str, slot_date: date, status: str,
                reason: str, slot_at: datetime, at: datetime) -> bool:
    r = fetch_one(
        conn,
        "insert into bot_slot (user_id, slot_date, status, reason, slot_at, at) "
        "values (%s,%s,%s,%s,%s,%s) on conflict do nothing returning user_id",
        (user_id, slot_date, status, reason, slot_at, at))
    return r is not None


def record_check(conn: psycopg.Connection, user_id: str, at: datetime, dropped: bool) -> None:
    conn.execute("insert into bot_check (user_id, at, dropped) values (%s, %s, %s)",
                 (user_id, at, dropped))


def recent_checks(conn: psycopg.Connection, user_id: str, since: datetime) -> list[datetime]:
    rows = fetch_all(conn, "select at from bot_check where user_id = %s and at >= %s "
                           "and not dropped order by at", (user_id, since))
    return [r["at"] for r in rows]


# --- suggestions --------------------------------------------------------------------------

def insert_suggestion(conn: psycopg.Connection, row: SuggestionRow) -> None:
    conn.execute(
        "insert into bot_suggestion (suggestion_id, user_id, trigger_id, run_id, kind, title, "
        "why, action, placement, page_id, priority, status, dedup_key, "
        "expires_at, cost_usd, created_at, reacted_at) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (row.suggestion_id, row.user_id, row.trigger_id, row.run_id, row.kind, row.title,
         row.why, Jsonb(row.action), row.placement, row.page_id, row.priority, row.status,
         row.dedup_key, row.expires_at, row.cost_usd, row.created_at,
         row.reacted_at))


def suggestions(conn: psycopg.Connection, user_id: str, *, since: datetime | None = None,
                placement: str | None = None, page_id: str | None = None) -> list[SuggestionRow]:
    where, params = ["user_id = %s"], [user_id]
    if since is not None:
        where.append("created_at >= %s")
        params.append(since)
    if placement is not None:
        where.append("placement = %s")
        params.append(placement)
    if page_id is not None:
        where.append("page_id = %s")
        params.append(page_id)
    rows = fetch_all(conn, f"select * from bot_suggestion where {' and '.join(where)} "
                           "order by created_at desc, suggestion_id", params)
    return [_row(SuggestionRow, r) for r in rows]


def open_suggestions(conn: psycopg.Connection, user_id: str, *, placement: str,
                     page_id: str | None, now: datetime) -> list[SuggestionRow]:
    """Listable cards of a placement: pending / shown, and started (`accepted`) ones until they
    are marked done; not past `expires_at`. Priority desc, then oldest first."""
    sql = ("select * from bot_suggestion where user_id = %s and placement = %s "
           "and expires_at > %s and status in ('pending', 'shown', 'accepted')")
    params: list = [user_id, placement, now]
    if page_id is not None:
        sql += " and page_id = %s"
        params.append(page_id)
    rows = fetch_all(conn, sql + " order by priority desc, created_at, suggestion_id", params)
    return [_row(SuggestionRow, r) for r in rows]


def mark_shown(conn: psycopg.Connection, suggestion_id: str) -> bool:
    """pending -> shown, only if it is still pending (a concurrent reaction wins)."""
    r = fetch_one(conn, "update bot_suggestion set status = 'shown' where suggestion_id = %s "
                        "and status = 'pending' returning suggestion_id", (suggestion_id,))
    return r is not None


def lock_suggestion(conn: psycopg.Connection, suggestion_id: str) -> SuggestionRow | None:
    r = fetch_one(conn, "select * from bot_suggestion where suggestion_id = %s for update",
                  (suggestion_id,))
    return None if r is None else _row(SuggestionRow, r)


REACTION_STATUSES = ("accepted", "not_interested")


def set_suggestion(conn: psycopg.Connection, suggestion_id: str, *, status: str,
                   at: datetime) -> None:
    """Change the status. `reacted_at` is stamped only by a user reaction (shown / expired / done
    are not)."""
    sets, params = ["status = %s"], [status]
    if status in REACTION_STATUSES:
        sets.append("reacted_at = %s")
        params.append(at)
    conn.execute(f"update bot_suggestion set {', '.join(sets)} where suggestion_id = %s",
                 (*params, suggestion_id))


def append_suggestion_why(conn: psycopg.Connection, suggestion_id: str, tail: str) -> None:
    """Append `tail` once: a second caller (a racing sweep / re-dispatch) adds nothing."""
    conn.execute("update bot_suggestion set why = why || %s where suggestion_id = %s "
                 "and strpos(why, %s) = 0", (tail, suggestion_id, tail))


def restore_shown(conn: psycopg.Connection, suggestion_id: str, now: datetime) -> bool:
    """Undo of a 不感兴趣 click: the card is `shown` again (only from `not_interested`, and only
    while it has not expired); the reaction time is cleared."""
    r = fetch_one(conn, "update bot_suggestion set status = 'shown', reacted_at = null "
                        "where suggestion_id = %s and status = 'not_interested' "
                        "and expires_at > %s returning suggestion_id", (suggestion_id, now))
    return r is not None


def preference_action_ids(conn: psycopg.Connection, user_id: str,
                          suggestion_ids: Sequence[str]) -> dict[str, str]:
    """suggestion id -> action id of its (still executed) 偏好 ledger row."""
    if not suggestion_ids:
        return {}
    rows = fetch_all(
        conn, "select idempotency_key, action_id from agent_action where user_id = %s "
              "and kind = 'preference' and status = 'executed' and idempotency_key = any(%s)",
        (user_id, [f"pref:{i}" for i in suggestion_ids]))
    return {r["idempotency_key"][len("pref:"):]: r["action_id"] for r in rows}


def preference_rows(conn: psycopg.Connection, user_id: str,
                    since: datetime) -> list[tuple[datetime, str, str]]:
    """(when, summary, why) of the 偏好 clicks in force since `since`, newest first."""
    rows = fetch_all(
        conn, "select created_at, summary, why from agent_action where user_id = %s "
              "and kind = 'preference' and status = 'executed' and created_at >= %s "
              "order by created_at desc", (user_id, since))
    return [(r["created_at"], r["summary"], r["why"]) for r in rows]


def last_suggested_at(conn: psycopg.Connection, user_id: str, dedup_key: str) -> datetime | None:
    r = fetch_one(conn, "select max(created_at) as t from bot_suggestion "
                        "where user_id = %s and dedup_key = %s", (user_id, dedup_key))
    return r["t"]


def count_suggestions(conn: psycopg.Connection, user_id: str, *, since: datetime,
                      placement: str, page_id: str | None = None) -> int:
    sql = ("select count(*) as n from bot_suggestion where user_id = %s and created_at >= %s "
           "and placement = %s")
    params: list = [user_id, since, placement]
    if page_id is not None:
        sql += " and page_id = %s"
        params.append(page_id)
    return fetch_one(conn, sql, params)["n"]


# --- learned suppression ------------------------------------------------------------------

def get_counters(conn: psycopg.Connection, user_id: str, dedup_key: str) -> Counters:
    r = fetch_one(conn, "select accepted, not_interested, later, last_not_interested_at "
                        "from bot_suppression where user_id = %s and dedup_key = %s",
                  (user_id, dedup_key))
    return Counters() if r is None else Counters(**r)


def put_counters(conn: psycopg.Connection, user_id: str, dedup_key: str, c: Counters) -> None:
    conn.execute(
        "insert into bot_suppression (user_id, dedup_key, accepted, not_interested, later, "
        "last_not_interested_at) values (%s,%s,%s,%s,%s,%s) "
        "on conflict (user_id, dedup_key) do update set accepted = excluded.accepted, "
        "not_interested = excluded.not_interested, later = excluded.later, "
        "last_not_interested_at = excluded.last_not_interested_at",
        (user_id, dedup_key, c.accepted, c.not_interested, c.later, c.last_not_interested_at))


def all_user_ids(conn: psycopg.Connection) -> list[str]:
    return [r["user_id"] for r in fetch_all(conn, "select user_id from app_user order by user_id")]


def last_seen(conn: psycopg.Connection) -> dict[str, datetime | None]:
    """Every user with when his client was last in front of him (`bot.settings.bot_serves`)."""
    return {r["user_id"]: r["last_seen_at"]
            for r in fetch_all(conn, "select user_id, last_seen_at from app_user order by user_id")}


def last_seen_of(conn: psycopg.Connection, user_id: str) -> datetime | None:
    row = fetch_one(conn, "select last_seen_at from app_user where user_id = %s", (user_id,))
    return row["last_seen_at"] if row is not None else None


def mark_seen(conn: psycopg.Connection, user_id: str, at: datetime) -> bool:
    """False: no such user."""
    return conn.execute("update app_user set last_seen_at = %s where user_id = %s",
                        (at, user_id)).rowcount > 0

# --- action ledger (migration 0051) -------------------------------------------------------

@dataclass(frozen=True)
class ActionRow:
    action_id: str
    user_id: str
    kind: str
    scope: str
    level: str
    status: str
    source: str
    summary: str
    content: dict
    content_hash: str
    idempotency_key: str
    created_at: datetime
    why: str = ""
    page_id: str | None = None
    page_version: str | None = None
    result: dict | None = None
    wiki_event_ids: list = field(default_factory=list)
    undo: dict | None = None
    detail: str = ""
    run_id: str | None = None
    step: str | None = None
    trigger_id: str | None = None
    decided_at: datetime | None = None
    expires_at: datetime | None = None


_ACTION_COLS = ("action_id, user_id, kind, scope, level, status, source, summary, why, content, "
                "content_hash, idempotency_key, page_id, page_version, result, wiki_event_ids, "
                "undo, detail, run_id, step, trigger_id, created_at, decided_at, expires_at")
_ACTION_INSERT = (f"insert into agent_action ({_ACTION_COLS}) values "
                  f"({', '.join(['%s'] * 24)})")


def _action_params(r: ActionRow) -> tuple:
    return (r.action_id, r.user_id, r.kind, r.scope, r.level, r.status, r.source, r.summary,
            r.why, Jsonb(r.content), r.content_hash, r.idempotency_key, r.page_id,
            r.page_version, None if r.result is None else Jsonb(r.result),
            Jsonb(list(r.wiki_event_ids)), None if r.undo is None else Jsonb(r.undo), r.detail,
            r.run_id, r.step, r.trigger_id, r.created_at, r.decided_at, r.expires_at)


def insert_action(conn: psycopg.Connection, row: ActionRow) -> bool:
    """False when the idempotency key already exists (nothing is written)."""
    r = fetch_one(conn, _ACTION_INSERT + " on conflict (idempotency_key) do nothing "
                                         "returning action_id", _action_params(row))
    return r is not None


def upsert_action_row(conn: psycopg.Connection, row: ActionRow) -> None:
    """Mirror of a change that already happened: a repeat of the key updates the outcome. A key
    that belongs to another user is never touched (raises)."""
    r = fetch_one(
        conn,
        _ACTION_INSERT + " on conflict (idempotency_key) do update set status = excluded.status, "
        "result = excluded.result, undo = excluded.undo, "
        "wiki_event_ids = excluded.wiki_event_ids, detail = excluded.detail, "
        "decided_at = excluded.decided_at, summary = excluded.summary "
        "where agent_action.user_id = excluded.user_id "
        "returning action_id", _action_params(row))
    if r is None:
        raise ValueError(f"idempotency key {row.idempotency_key!r} belongs to another user")


def _action(conn: psycopg.Connection, where: str, param: str, suffix: str) -> ActionRow | None:
    r = fetch_one(conn, f"select * from agent_action where {where} = %s{suffix}", (param,))
    if r is not None and r["source"] == "rsi" and r["status"] == "executed":
        sync_rsi_undone(conn, r["user_id"])
        r = fetch_one(conn, f"select * from agent_action where {where} = %s{suffix}", (param,))
    return None if r is None else _row(ActionRow, r)


def get_action(conn: psycopg.Connection, action_id: str) -> ActionRow | None:
    return _action(conn, "action_id", action_id, "")


def lock_action(conn: psycopg.Connection, action_id: str) -> ActionRow | None:
    return _action(conn, "action_id", action_id, " for update")


def get_action_by_key(conn: psycopg.Connection, key: str) -> ActionRow | None:
    return _action(conn, "idempotency_key", key, "")


def set_action_status(conn: psycopg.Connection, action_id: str, *, status: str, at: datetime,
                      detail: str = "", result: dict | None = None,
                      wiki_event_ids: list | None = None,
                      from_statuses: Sequence[str] | None = None) -> bool:
    """Result / event ids are written only when given; `decided_at` is set by every change.
    With `from_statuses` the row moves only if it is currently in one of them (a mirror must
    never fight the ledger); the return value says whether a row changed."""
    sets, params = ["status = %s", "detail = %s", "decided_at = %s"], [status, detail, at]
    if result is not None:
        sets.append("result = %s")
        params.append(Jsonb(result))
    if wiki_event_ids is not None:
        sets.append("wiki_event_ids = %s")
        params.append(Jsonb(list(wiki_event_ids)))
    where, tail = "action_id = %s", [action_id]
    if from_statuses is not None:
        where += " and status = any(%s)"
        tail.append(list(from_statuses))
    cur = conn.execute(f"update agent_action set {', '.join(sets)} where {where}",
                       (*params, *tail))
    return cur.rowcount > 0


def new_action_id() -> str:
    return f"act_{uuid.uuid4().hex[:20]}"


def sync_rsi_undone(conn: psycopg.Connection, user_id: str) -> None:
    """Existing RSI mirrors follow terminal native facts, in the caller's transaction.

    Also repairs historical mismatches on reads; never re-executes an undo or creates a row.
    """
    conn.execute(
        "update agent_action a set status = 'undone', decided_at = n.at, detail = '' "
        "from ("
        "select 'rsi:' || user_id || ':' || patch_id as key, updated_at as at "
        "from rsi_patch where user_id = %(uid)s and status = 'rolled_back' "
        "union all select 'rsi-method:' || user_id || ':' || change_id, updated_at "
        "from rsi_method_change where user_id = %(uid)s and status = 'rolled_back' "
        "union all select 'harness:' || user_id || ':' || change_id, settled_at "
        "from harness_change where user_id = %(uid)s and status in ('undone', 'rolled_back')"
        ") n where a.user_id = %(uid)s and a.source = 'rsi' "
        "and a.status = 'executed' and a.idempotency_key = n.key", {"uid": user_id})


def mirror_action(conn: psycopg.Connection, *, user_id: str, kind: str, scope: str, source: str,
                  key: str, status: str, summary: str, why: str, content: dict,
                  created_at: datetime, decided_at: datetime | None,
                  action_id: str | None = None) -> None:
    """For store modules: the ledger row of a change an existing mechanism already made (level:
    the user's). A repeat of the key updates the outcome."""
    upsert_action_row(conn, ActionRow(
        action_id=action_id or new_action_id(), user_id=user_id, kind=kind, scope=scope,
        level=get_settings(conn, user_id).levels[scope], status=status, source=source, summary=summary, why=why,
        content=content, content_hash=payload_hash(content), idempotency_key=key,
        created_at=created_at, decided_at=decided_at))


# an `approved` row is normally executed within moments; one older than this was left behind
# (429 / crash mid-approve) and waits for the user again
APPROVED_STALE = timedelta(minutes=2)


# the per-page write records (`user_write`, and `bot_write` without `picks`) belong to 「Bot 最近做了什么」
# only: not in the ledger window, not in the change list. The 「要写 N 页」 batch (`picks`) is a real approval.
NOT_PAGE_WRITE = ("not (kind = 'user_write' or (kind = 'bot_write' and not (content ? 'picks')))")


def list_actions(conn: psycopg.Connection, user_id: str, *, status: str | None = None,
                 limit: int = 100, now: datetime | None = None,
                 writes: bool = False) -> list[ActionRow]:
    """With `status="proposed"` and `now`, stale `approved` rows are listed too (see count_pending).
    The per-page write records are left out unless `writes`."""
    sync_rsi_undone(conn, user_id)
    sql, params = "select * from agent_action where user_id = %s", [user_id]
    if not writes:
        sql += " and " + NOT_PAGE_WRITE
    if status == "proposed" and now is not None:
        sql += " and (status = 'proposed' or (status = 'approved' and decided_at < %s))"
        params.append(now - APPROVED_STALE)
        status = None
    if status is not None:
        sql += " and status = %s"
        params.append(status)
    rows = fetch_all(conn, sql + " order by created_at desc, action_id limit %s",
                     (*params, limit))
    return [_row(ActionRow, r) for r in rows]


def renamed_in_drafts(conn: psycopg.Connection, user_id: str, source_id: str, old: str,
                      new: str) -> None:
    """A material got its real title: goal drafts made from it that still wait for a yes say it
    too. A goal already confirmed keeps its name. Only the title's own places (《old》 or the whole
    field) change, not the words of the plan, which may hold the old name for their own reasons."""
    def swap(text: str) -> str:
        return new if text == old else text.replace(f"《{old}》", f"《{new}》")

    for row in list_actions(conn, user_id, status="proposed"):
        if row.kind != "goal_new" or source_id not in row.content.get("source_ids", ()):
            continue
        content = {**row.content}
        for key in ("title", "goal"):
            if isinstance(content.get(key), str):
                content[key] = swap(content[key])
        if "source_titles" in content:
            content["source_titles"] = [swap(t) for t in content["source_titles"]]
        if isinstance(content.get("reading"), dict) and "title" in content["reading"]:
            content["reading"] = {**content["reading"], "title": swap(content["reading"]["title"])}
        conn.execute("update agent_action set summary = %s, why = %s, content = %s, "
                     "content_hash = %s where action_id = %s",
                     (swap(row.summary), swap(row.why), Jsonb(content), payload_hash(content),
                      row.action_id))


def open_page_revise(conn: psycopg.Connection, user_id: str, page_id: str) -> ActionRow | None:
    """The page's `page_revise` row whose draft is still open (being written or waiting for the
    learner), newest first."""
    rows = fetch_all(
        conn, "select * from agent_action where user_id = %s and kind = 'page_revise' "
              "and page_id = %s and status = 'proposed' "
              "and content->>'state' in ('writing', 'ready') "
              "order by created_at desc, action_id limit 1", (user_id, page_id))
    return _row(ActionRow, rows[0]) if rows else None


def write_rows(conn: psycopg.Connection, user_id: str, run_ids: list[str]) -> list[ActionRow]:
    """The per-page write rows (`bot_write` / `user_write`) of these page runs, no limit."""
    if not run_ids:
        return []
    rows = fetch_all(
        conn, "select * from agent_action where user_id = %s and kind in ('bot_write', 'user_write') "
              "and run_id = any(%s)", (user_id, run_ids))
    return [_row(ActionRow, r) for r in rows]


def merge_making(conn: psycopg.Connection, user_id: str, page_id: str) -> ActionRow | None:
    """The executed `page_merge` whose merged page is this one."""
    r = fetch_one(
        conn, "select * from agent_action where user_id = %s and kind = 'page_merge' "
              "and status = 'executed' and result -> 'made' @> %s order by created_at desc limit 1",
        (user_id, Jsonb([{"page_id": page_id}])))
    return None if r is None else _row(ActionRow, r)


def assistant_row_making(conn: psycopg.Connection, user_id: str, page_id: str,
                         before: datetime) -> ActionRow | None:
    """The newest executed assistant action (not after `before`) that made this page."""
    r = fetch_one(
        conn, "select * from agent_action where user_id = %s and source = 'assistant' "
              "and status in ('executed', 'undone') and created_at <= %s "
              "and result -> 'made' @> %s order by created_at desc limit 1",
        (user_id, before, Jsonb([{"page_id": page_id}])))
    return None if r is None else _row(ActionRow, r)


def batch_rows(conn: psycopg.Connection, user_id: str) -> list[ActionRow]:
    """Every `bot_write` batch proposal (「要写 N 页」, content has `picks`) of the user in any
    status, newest first, no limit (the ledger's own listing is windowed)."""
    rows = fetch_all(
        conn,
        "select * from agent_action where user_id = %s and kind = 'bot_write' "
        "and content ? 'picks' order by created_at desc, action_id", (user_id,))
    return [_row(ActionRow, r) for r in rows]


def unsettled_merges(conn: psycopg.Connection, user_id: str, new_page: str) -> list[ActionRow]:
    """Executed `page_merge` rows whose merged page is `new_page` and whose originals are not in the
    trash yet (`undo.settled` false)."""
    rows = fetch_all(
        conn,
        "select * from agent_action where user_id = %s and kind = 'page_merge' "
        "and status = 'executed' and undo->>'new_page' = %s "
        "and coalesce((undo->>'settled')::boolean, true) = false", (user_id, new_page))
    return [_row(ActionRow, r) for r in rows]


def list_change_rows(conn: psycopg.Connection, user_id: str, *, sources: Sequence[str],
                     statuses: Sequence[str], limit: int) -> list[ActionRow]:
    """Decided rows of `sources` for the change list, newest first."""
    sync_rsi_undone(conn, user_id)
    rows = fetch_all(
        conn,
        "select * from agent_action where user_id = %s and source = any(%s) and status = any(%s) "
        "and " + NOT_PAGE_WRITE + " order by created_at desc, action_id limit %s",
        (user_id, list(sources), list(statuses), limit))
    return [_row(ActionRow, r) for r in rows]


def count_pending(conn: psycopg.Connection, user_id: str, now: datetime | None = None) -> int:
    """Actions waiting for the user (a pending item never expires): `proposed`, and with `now`
    also `approved` rows whose execution did not finish within APPROVED_STALE (the user can
    approve them again; a click-approved row being executed right now is not pending)."""
    if now is not None:
        return fetch_one(conn, "select count(*) as n from agent_action where user_id = %s and "
                               "(status = 'proposed' or (status = 'approved' and decided_at < %s))",
                         (user_id, now - APPROVED_STALE))["n"]
    return fetch_one(conn, "select count(*) as n from agent_action where user_id = %s "
                           "and status = 'proposed'", (user_id,))["n"]


def set_action_content(conn: psycopg.Connection, action_id: str, content: dict,
                       content_hash: str) -> None:
    """The user's edited draft replaces the proposal's content (re-hashed by the caller)."""
    conn.execute("update agent_action set content = %s, content_hash = %s where action_id = %s",
                 (Jsonb(content), content_hash, action_id))


def set_action_redrafted(conn: psycopg.Connection, action_id: str, content: dict,
                         content_hash: str, page_version: str | None) -> None:
    """A draft composed again against the page as it is now (重试): new content and page version,
    and the row waits for the learner again (a stale-failed row back to `proposed`)."""
    conn.execute("update agent_action set content = %s, content_hash = %s, page_version = %s, "
                 "status = 'proposed', detail = '', decided_at = null where action_id = %s",
                 (Jsonb(content), content_hash, page_version, action_id))


def set_action_undo(conn: psycopg.Connection, action_id: str, undo: dict) -> None:
    """The data an executor needs to take the action back (written in the executing commit)."""
    conn.execute("update agent_action set undo = %s where action_id = %s",
                 (Jsonb(undo), action_id))


# --- runs ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class RunRow:
    run_id: str
    user_id: str
    kind: str
    status: str
    started_at: datetime
    trigger_id: str | None = None
    calls: int = 0
    cost_usd: float = 0.0
    reads: list = field(default_factory=list)
    detail: str = ""
    finished_at: datetime | None = None


def insert_run(conn: psycopg.Connection, *, run_id: str, user_id: str, kind: str,
               trigger_id: str | None, now: datetime) -> None:
    conn.execute(
        "insert into bot_run (run_id, user_id, kind, trigger_id, status, started_at) "
        "values (%s,%s,%s,%s,'running',%s) on conflict (run_id) do nothing",
        (run_id, user_id, kind, trigger_id, now))


def update_run(conn: psycopg.Connection, run_id: str, *, status: str, now: datetime, calls: int,
               cost_usd: float, reads: Sequence, detail: str) -> bool:
    cur = conn.execute(
        "update bot_run set status = %s, finished_at = %s, calls = %s, cost_usd = %s, "
        "reads = %s, detail = %s where run_id = %s",
        (status, now, calls, cost_usd, Jsonb(list(reads)), detail, run_id))
    return cur.rowcount > 0


def get_run(conn: psycopg.Connection, run_id: str) -> RunRow | None:
    r = fetch_one(conn, "select * from bot_run where run_id = %s", (run_id,))
    return None if r is None else _row(RunRow, r)


def last_runs(conn: psycopg.Connection, user_id: str, kind: str, limit: int) -> list[RunRow]:
    """The newest `limit` runs of a kind (by started_at, newest first)."""
    rows = fetch_all(conn, "select * from bot_run where user_id = %s and kind = %s "
                           "order by started_at desc, run_id desc limit %s",
                     (user_id, kind, limit))
    return [_row(RunRow, r) for r in rows]


def put_kind_pause(conn: psycopg.Connection, user_id: str, kind: str, until: datetime) -> None:
    """Pause a run kind until `until` (a later existing pause is kept)."""
    conn.execute(
        "insert into bot_kind_pause (user_id, kind, until) values (%s, %s, %s) "
        "on conflict (user_id, kind) do update set until = greatest(bot_kind_pause.until, "
        "excluded.until)", (user_id, kind, until))


def kind_paused_until(conn: psycopg.Connection, user_id: str, kind: str,
                      now: datetime) -> datetime | None:
    """When the pause of this run kind ends, or None when it is not paused at `now`."""
    r = fetch_one(conn, "select until from bot_kind_pause where user_id = %s and kind = %s",
                  (user_id, kind))
    return r["until"] if r is not None and r["until"] > now else None


def bot_cycle_changes(conn: psycopg.Connection, user_id: str,
                      trigger_id: str) -> list[dict[str, Any]]:
    """The RSI patches the Bot-started cycle(s) of a trigger made (B5c): patch id, track, status
    and its why, oldest cycle first."""
    rows = fetch_all(
        conn,
        "select p.patch_id, p.track, p.status, coalesce(p.body ->> 'why', '') as why "
        "from rsi_cycle c cross join lateral jsonb_array_elements_text(c.result -> 'patch_ids') "
        "as pid(patch_id) join rsi_patch p on p.patch_id = pid.patch_id and p.user_id = c.user_id "
        "where c.user_id = %s and c.trigger = 'bot' and c.result ->> 'bot_trigger_id' = %s "
        "order by c.id, p.patch_id", (user_id, trigger_id))
    return [dict(r) for r in rows]


def count_runs(conn: psycopg.Connection, trigger_id: str, kind: str) -> int:
    """How many runs of `kind` a trigger had (an RSI trigger's attempt number)."""
    return fetch_one(conn, "select count(*) as n from bot_run where trigger_id = %s and kind = %s",
                     (trigger_id, kind))["n"]


def awaiting_rsi_triggers(conn: psycopg.Connection, older_than: datetime,
                          limit: int = 200) -> list[TriggerRow]:
    """Triggers waiting for their RSI run since before `older_than`. Whether a run is still alive
    is DBOS's to say (durable._rsi_live: a queued / pending workflow), not a `running` bot_run row,
    which can be left behind by a cancelled or crashed run."""
    rows = fetch_all(conn, "select * from bot_trigger where status = 'awaiting_rsi' "
                           "and coalesce(gated_at, created_at) < %s "
                           "order by created_at limit %s", (older_than, limit))
    return [_row(TriggerRow, r) for r in rows]


def triggers_since(conn: psycopg.Connection, user_id: str, since: datetime) -> list[TriggerRow]:
    rows = fetch_all(conn, "select * from bot_trigger where user_id = %s and created_at >= %s "
                           "order by created_at desc, trigger_id", (user_id, since))
    return [_row(TriggerRow, r) for r in rows]


def suggestions_of_trigger(conn: psycopg.Connection, trigger_id: str) -> list[SuggestionRow]:
    rows = fetch_all(conn, "select * from bot_suggestion where trigger_id = %s "
                           "order by created_at, suggestion_id", (trigger_id,))
    return [_row(SuggestionRow, r) for r in rows]


def actions_of_trigger(conn: psycopg.Connection, trigger_id: str) -> list[ActionRow]:
    rows = fetch_all(conn, "select * from agent_action where trigger_id = %s "
                           "order by created_at, action_id", (trigger_id,))
    return [_row(ActionRow, r) for r in rows]


def get_suggestion(conn: psycopg.Connection, suggestion_id: str) -> SuggestionRow | None:
    r = fetch_one(conn, "select * from bot_suggestion where suggestion_id = %s", (suggestion_id,))
    return None if r is None else _row(SuggestionRow, r)


def expire_suggestions(conn: psycopg.Connection, user_id: str, now: datetime) -> None:
    """The user's pending / shown suggestions whose time is up become `expired`."""
    conn.execute("update bot_suggestion set status = 'expired' where user_id = %s "
                 "and status in ('pending', 'shown') and expires_at <= %s",
                 (user_id, now))


def count_open_today(conn: psycopg.Connection, user_id: str, *, start: datetime, end: datetime,
                     now: datetime) -> int:
    """Today cards created in [start, end) that are still pending / shown and not expired."""
    return fetch_one(conn, "select count(*) as n from bot_suggestion where user_id = %s "
                           "and placement = 'today' and status in ('pending', 'shown') "
                           "and created_at >= %s and created_at < %s and expires_at > %s",
                     (user_id, start, end, now))["n"]


def count_declined(conn: psycopg.Connection, user_id: str, *, kind: str, dedup_key: str,
                   since: datetime) -> int:
    """Suggestions of this kind and dedup key the user marked not interested since `since`."""
    return fetch_one(conn, "select count(*) as n from bot_suggestion where user_id = %s "
                           "and kind = %s and dedup_key = %s and status = 'not_interested' "
                           "and created_at >= %s", (user_id, kind, dedup_key, since))["n"]


def slot_status(conn: psycopg.Connection, user_id: str, slot_date: date) -> str | None:
    """The status of the user's Bot slot on that day, or None when there is none."""
    r = fetch_one(conn, "select status from bot_slot where user_id = %s and slot_date = %s",
                  (user_id, slot_date))
    return None if r is None else r["status"]
