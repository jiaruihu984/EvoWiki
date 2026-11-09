"""Child process for the B0 crash probes (never imported by the app). Usage:
    python -m tests.agent.dbos_child <scenario> <crash|resume> <db_url> <log_path>"""
import os
import sys
import time

import psycopg
from dbos import DBOS, SetWorkflowID

from evowiki.agent import dbos_app
from evowiki.agent.effects import apply_once

SCENARIO, MODE, DB, LOG = sys.argv[1:5]
VERSION = "b0-probe"


def log(line: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def crash_here(point: str) -> None:
    if MODE == "crash" and os.environ.get("CRASH_AT") == point:
        log(f"crash {point}")
        os._exit(1)


@DBOS.step()
def step1() -> int:
    log("step1")
    return 1


@DBOS.step()
def step2(x: int) -> int:
    log("step2")
    return x + 1


@DBOS.step()
def write_effect() -> dict:
    with psycopg.connect(DB) as conn:
        def effect() -> dict:
            conn.execute("insert into probe_target values (1)")
            return {"ok": 1}

        out = apply_once(conn, "probe-effect", {"n": 1}, effect)
        conn.commit()
    log("effect")
    crash_here("after_effect_commit")          # committed, but the step has not returned
    return out


@DBOS.workflow()
def steps_wf() -> int:
    a = step1()
    crash_here("after_step1")
    return step2(a)


@DBOS.workflow()
def effect_wf() -> dict:
    return write_effect()


@DBOS.workflow()
def stream_wf() -> int:
    DBOS.write_stream("progress", "one")
    a = step1()
    crash_here("after_stream_one")
    DBOS.write_stream("progress", "two")
    DBOS.close_stream("progress")
    return a


WORKFLOWS = {"steps": steps_wf, "effect": effect_wf, "stream": stream_wf}

if __name__ == "__main__":
    dbos_app.launch(DB, app_version=VERSION)
    wid = f"probe-{SCENARIO}"
    if MODE == "crash":
        with SetWorkflowID(wid):
            WORKFLOWS[SCENARIO]()
    else:
        result = DBOS.retrieve_workflow(wid).get_result()
        log(f"result {result}")
        time.sleep(0.2)
    dbos_app.shutdown()
