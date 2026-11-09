"""A retained Bot schedule is safe after a real Bot-on -> off process restart."""

import os
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from dbos import DBOS

from evowiki.agent import dbos_app
from evowiki.service import rsi_feedback
from evowiki.service.deps import DurableRuntime
from evowiki.store.db import make_pool

MODE, DB = sys.argv[1:]
NOW = datetime(2026, 11, 8, tzinfo=UTC)


def main():
    pool = make_pool(DB, min_size=1, max_size=4)
    if MODE == "on":
        from evowiki.bot import durable as bot

        bot.install(bot.BotRuntime(pool=pool, clock=lambda: NOW, new_id=lambda p: p))
        dbos_app.launch(
            DB,
            app_version="d03-bot-switch",
            executor_id="d03-bot-on",
            queues=[rsi_feedback.QUEUE],
            schedules=False,
        )
        bot.register()
        bot.enqueue(bot.bot_check, "u1", "d03:retained-bot-check", "u1")
        assert DBOS.get_schedule("bot:tick") is not None
        os._exit(1)
    svc = SimpleNamespace(pool=pool, clock=lambda: NOW)
    rt = DurableRuntime(
        DB,
        executor_id="d03-bot-off",
        app_version="d03-bot-switch",
        queues=[rsi_feedback.QUEUE],
        schedules=True,
    )
    rt.rsi_services = svc
    rt.start(SimpleNamespace())
    try:
        # Resolve from the retained DB row; do not import the Bot in this process before start.
        assert DBOS.trigger_schedule("bot:tick").get_result() == []
        assert DBOS.get_workflow_status("d03:retained-bot-check").status == "ENQUEUED"
        assert DBOS.get_schedule(rsi_feedback.SCHEDULE) is not None
        with pool.connection() as conn:
            assert conn.execute("select count(*) as n from bot_run").fetchone()["n"] == 0
    finally:
        rt.stop()
        pool.close()


if __name__ == "__main__":
    main()
