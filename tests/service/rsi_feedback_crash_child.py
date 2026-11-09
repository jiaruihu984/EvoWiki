"""D03: restart an actual DBOS correction delivery with a scripted triage provider."""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from dbos import DBOS

from evowiki.agent import dbos_app
from evowiki.harness.triage import TriageOut
from evowiki.service import rsi_feedback, rsi_runs
from evowiki.store.db import make_pool

MODE, DB, LOG = sys.argv[1:]
NOW = datetime(2026, 11, 8, tzinfo=UTC)


def log(text):
    with Path(LOG).open("a", encoding="utf-8") as file:
        file.write(text + "\n")


class Gateway:
    def call(self, role, req, schema, *, account):
        assert req.purpose == "rsi.h.triage"
        log("triage")
        return TriageOut(largest=2), None


def main():
    pool = make_pool(DB, min_size=1, max_size=4)
    svc = SimpleNamespace(
        pool=pool,
        clock=lambda: NOW,
        actions=None,
        gateway=Gateway(),
        new_id=lambda prefix: prefix + str(uuid4()),
    )
    rsi_feedback.install(svc)
    if MODE == "crash":

        def crash(*a):
            log("crash before triage")
            os._exit(1)

        rsi_runs.rsi_tick = crash
    dbos_app.launch(
        DB,
        app_version="d03-feedback",
        executor_id="d03-feedback",
        queues=[rsi_feedback.QUEUE],
        schedules=False,
    )
    try:
        rsi_feedback.register()
        DBOS.retrieve_workflow("rsi-feedback:u1:202611080000").get_result()
        assert not rsi_feedback.pending_ids(svc, "u1")
        rsi_feedback.wake_pending()
        log("delivered")
    finally:
        rsi_feedback.uninstall()
        dbos_app.shutdown()
        pool.close()


if __name__ == "__main__":
    main()
