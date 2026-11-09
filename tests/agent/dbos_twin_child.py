"""Child process for the second-backend probe (never imported by the app). Usage:
    python -m tests.agent.dbos_twin_child <executor> <start|recover> <db_url> <log_path>
`start` enqueues a slow workflow and stays up until the file <log_path>.stop exists; `recover` only launches."""
import sys
import time
from pathlib import Path

from dbos import DBOS, SetWorkflowID

from evowiki.agent import dbos_app

EXECUTOR, MODE, DB, LOG = sys.argv[1:5]


def log(line: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


@DBOS.workflow()
def slow() -> int:
    log("slow-start")
    stop = Path(LOG + ".stop")
    while not stop.exists():
        time.sleep(0.1)
    log("slow-end")
    return 1


try:
    dbos_app.launch(DB, app_version="twin-probe", executor_id=EXECUTOR)
except dbos_app.ExecutorInUse as e:
    log(f"launch-refused {type(e).__name__}: {e}")
    sys.exit(3)
log("launched")
if MODE == "start":
    with SetWorkflowID("twin-slow"):
        DBOS.start_workflow(slow)
    while not Path(LOG + ".stop").exists():
        time.sleep(0.1)
    time.sleep(0.5)
else:
    time.sleep(2)
dbos_app.shutdown()
