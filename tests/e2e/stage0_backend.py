"""Disposable /wiki backend for the Stage 0 real-Obsidian walkthrough (the Bot's LLM planner and the
guideline proposal deleted; driver obsidian-plugin/scripts/real-obsidian/stage0-walkthrough.mjs).
Test-only.

It is tests/e2e/audit_backend.py as is (build_services with no API key on a fresh database of the
test cluster, DBOS launched by the app lifespan, fail-closed scripted models), plus one hook:

POST /test/bot/seed: two rule-made Bot triggers of the user, dispatched through the real pipeline
(`bot.pipeline.dispatch`, no model): a `review_due` (2 cards) and a `goal_idle` (目标「黑壳虾」, 4
days). Each becomes a suggestion and an activity row with its trigger, so 今天 shows the goal card
and 参数 · Bot 最近做了什么 shows both rows whose "?" reads GET /wiki/bot/why. Returns the ids.

  uv run python -m tests.e2e.stage0_backend --port 8797 --user stage0-user
"""

from datetime import timedelta
from typing import Any

from evowiki.bot.pipeline import dispatch
from evowiki.store import bot as bot_store
from tests.e2e import audit_backend as ab

_create_app = ab.create_app


def _seed(svc, user: str) -> dict[str, Any]:
    now = svc.clock()
    rows = [
        bot_store.TriggerRow(
            trigger_id="t_stage0_review", user_id=user, kind="review_due", rule_id="review_due",
            rule_version=1, source_key="review_due:stage0", dedup_key="review_due", route="direct",
            evidence=[], payload={"count": 2, "cells": [], "page_id": None}, priority=50,
            reason="今天有 2 张卡片到期", created_at=now - timedelta(minutes=5),
            title="复习 2 张卡片"),
        bot_store.TriggerRow(
            trigger_id="t_stage0_idle", user_id=user, kind="goal_idle", rule_id="goal_idle",
            rule_version=1, source_key="goal_idle:p_stage0:stage0", dedup_key="goal_idle:p_stage0",
            route="direct", evidence=[],
            payload={"path_id": "p_stage0", "title": "黑壳虾", "page_id": None, "days": 4},
            priority=40, reason="目标「黑壳虾」已经 4 天没打开相关页面",
            created_at=now - timedelta(minutes=4), title="黑壳虾"),
    ]
    out = {}
    for r in rows:
        with svc.pool.connection() as conn:
            bot_store.insert_trigger(conn, r)
        d = dispatch(svc.pool, r.trigger_id, now=now, new_id=svc.new_id)
        out[r.trigger_id] = {"outcome": d.outcome, "reason": d.reason, "status": d.status,
                             "suggestion_id": d.suggestion_id}
    return out


def create_app(svc):
    app = _create_app(svc)
    user = ab_user[0]

    @app.post("/test/bot/seed")
    def _seed_route() -> dict[str, Any]:
        return _seed(svc, user)

    return app


ab_user = ["stage0-user"]


def main(argv: list[str] | None = None) -> None:
    import sys
    args = sys.argv[1:] if argv is None else argv
    if "--user" in args:
        ab_user[0] = args[args.index("--user") + 1]
    else:
        args = [*args, "--user", ab_user[0]]
    ab.create_app = create_app
    ab.main(args)


if __name__ == "__main__":
    main()
