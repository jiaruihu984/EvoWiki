"""The publication barrier for interactive evidence and progress."""
from typing import Any

from evowiki.store import dreaming
from evowiki.store.db import fetch_one
from evowiki.wikiapp.lineage import asset_artifact
from evowiki.wikiapp.repo import WikiConflict


def guard_asset_write(conn: Any, user_id: str, asset_id: str) -> None:
    if conn is None:  # MemoryRepo has no SQL forget protocol.
        return
    # Wiki commits already hold the user's writer lock. Never wait on the opposite
    # lock order used by forgetting materials: retry the durable event instead.
    locked = fetch_one(conn, "select pg_try_advisory_xact_lock(hashtext(%s)) as locked",
                       ("memory:user:" + user_id,))["locked"]
    if not locked or dreaming.paused(conn, user_id):
        raise WikiConflict("正在处理遗忘请求，请稍后重试交互结果")
    if not dreaming.sources_visible(conn, user_id, [asset_artifact(user_id, asset_id)]):
        raise LookupError(f"这个交互的来源已删除：{asset_id}")
    row = fetch_one(conn, "select a.page_id,s.state from wiki_asset a left join wiki_snapshot s "
                         "using(user_id) where a.user_id=%s and a.asset_id=%s", (user_id, asset_id))
    if row is None or (row["state"] is not None and (
            row["page_id"] in row["state"].get("trashed_pages", [])
            or row["page_id"] not in row["state"].get("pages", {}))):
        raise LookupError(f"没有这个交互：{asset_id}")
