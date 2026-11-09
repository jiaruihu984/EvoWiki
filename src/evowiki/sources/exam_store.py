"""Exam item store (migration 0071, on source_item). Plain functions over a connection; every
query carries user_id and reads skip deleted sources."""

from collections.abc import Mapping, Sequence
from datetime import date, datetime

import psycopg
from psycopg.types.json import Jsonb

from evowiki.sources.models import ExamItem
from evowiki.sources.store import _require_owner
from evowiki.store.db import fetch_all, fetch_one

EXAM_FREQUENT = 3

_COLS = ("i.item_id, i.source_id, i.section_id, i.page, i.ord, i.number, i.parent_id, i.kind, "
         "i.stem, i.options, i.answer, i.explanation, i.has_key, i.status, i.problem, i.node_id, "
         "i.cell, i.attach, i.attach_note, i.locator")
_FROM = ("from source_item i join source s on s.source_id = i.source_id "
         "where i.user_id = %s and s.user_id = %s and s.deleted_at is null")
# practice reads (the 真题练习 block, self-reports, 常考 / review / Bot facts): the source must
# still have an exam use, so undoing 「这是往年题」 takes the questions off the pages
_PRACTICE = (_FROM + " and exists (select 1 from source_use u where u.source_id = i.source_id "
             "and u.user_id = i.user_id and u.use = 'exam')")


def _item(r: dict) -> ExamItem:
    return ExamItem(**{**{k: r[k] for k in ExamItem.__dataclass_fields__},
                       "options": tuple(r["options"])})


def replace_items(conn: psycopg.Connection, user_id: str, source_id: str,
                  items: Sequence[ExamItem], *, gen: int, now: datetime) -> None:
    """Drop the source's old items and insert `items`. Groups are inserted first, so a
    sub-question's parent row exists whatever the item order (the parent FK is not deferrable)."""
    _require_owner(conn, user_id, source_id)
    conn.execute("delete from source_item where source_id = %s and user_id = %s",
                 (source_id, user_id))
    ordered = [it for it in items if it.kind == "group"] + [it for it in items if it.kind != "group"]
    for it in ordered:
        if it.source_id != source_id:
            raise ValueError(f"item {it.item_id} belongs to source {it.source_id}")
        conn.execute(
            "insert into source_item (item_id, user_id, source_id, section_id, page, kind, stem, "
            "options, answer, explanation, node_id, cell, has_key, ord, number, parent_id, status, "
            "problem, attach, attach_note, gen, created_at, locator) values "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s)",
            (it.item_id, user_id, source_id, it.section_id, it.page, it.kind, it.stem,
             Jsonb(list(it.options)), it.answer, it.explanation, it.node_id, it.cell, it.has_key,
             it.ord, it.number, it.parent_id, it.status, it.problem, it.attach, it.attach_note,
             gen, now, None if it.locator is None else Jsonb(it.locator)))


def items_of_source(conn: psycopg.Connection, user_id: str, source_id: str) -> list[ExamItem]:
    return [_item(r) for r in fetch_all(
        conn, f"select {_COLS} {_FROM} and i.source_id = %s order by i.ord",
        (user_id, user_id, source_id))]


def items_for_nodes(conn: psycopg.Connection, user_id: str,
                    node_ids: Sequence[str]) -> list[ExamItem]:
    if not node_ids:
        return []
    return [_item(r) for r in fetch_all(
        conn, f"select {_COLS} {_PRACTICE} and i.node_id = any(%s) and i.status = 'ok' "
              "and i.attach = 'attached' order by s.created_at, i.source_id, i.ord",
        (user_id, user_id, list(node_ids)))]


def items_by_id(conn: psycopg.Connection, user_id: str,
                item_ids: Sequence[str]) -> list[ExamItem]:
    """Practice read: only items of sources that still have an exam use."""
    if not item_ids:
        return []
    return [_item(r) for r in fetch_all(
        conn, f"select {_COLS} {_PRACTICE} and i.item_id = any(%s) order by i.source_id, i.ord",
        (user_id, user_id, list(item_ids)))]


def exam_counts(conn: psycopg.Connection, user_id: str) -> dict[str, int]:
    """node_id -> number of attached, ok, non-group items of sources with an exam use."""
    return {r["node_id"]: r["n"] for r in fetch_all(
        conn, "select i.node_id, count(*)::int as n " + _PRACTICE +
              " and i.node_id is not null and i.status = 'ok' and i.attach = 'attached' "
              "and i.kind <> 'group' group by i.node_id", (user_id, user_id))}


def exam_gen(conn: psycopg.Connection, user_id: str, source_id: str) -> int | None:
    """Import generation of the latest extraction; None when the source has no items."""
    r = fetch_one(conn, "select max(i.gen) as g " + _FROM + " and i.source_id = %s",
                  (user_id, user_id, source_id))
    return None if r is None else r["g"]


def frequent_nodes(counts: Mapping[str, int]) -> frozenset[str]:
    return frozenset(n for n, c in counts.items() if c >= EXAM_FREQUENT)


# ---- self-reports on exam items (source_item_mark, migration 0071) ----

def mark_key(item_id: str, day: date, result: str) -> str:
    """right / wrong share one key a day (one graded report per item per day); done has its own."""
    base = f"exam:{item_id}:{day.isoformat()}"
    return f"{base}:done" if result == "done" else base


def add_mark(conn: psycopg.Connection, user_id: str, item_id: str, result: str, *,
             day: date, at: datetime) -> bool:
    """Records one self-report; False when the same key is there already (a resend)."""
    cur = conn.execute(
        "insert into source_item_mark (user_id, item_id, mark_key, result, day, at) "
        "values (%s, %s, %s, %s, %s, %s) on conflict do nothing",
        (user_id, item_id, mark_key(item_id, day, result), result, day, at))
    return cur.rowcount == 1


def marks_on(conn: psycopg.Connection, user_id: str, item_ids: Sequence[str],
             day: date) -> dict[str, set[str]]:
    """item_id -> the results reported for it on `day`."""
    if not item_ids:
        return {}
    out: dict[str, set[str]] = {}
    for r in fetch_all(conn, "select item_id, result from source_item_mark where user_id = %s "
                             "and day = %s and item_id = any(%s)",
                       (user_id, day, list(item_ids))):
        out.setdefault(r["item_id"], set()).add(r["result"])
    return out
