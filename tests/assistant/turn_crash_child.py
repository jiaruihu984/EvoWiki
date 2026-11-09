"""Child process for the B1c crash tests (never imported by the app). Usage:
    python -m tests.assistant.turn_crash_child <crash|resume> <db_url> <log>

`crash`: installs the assistant service (Postgres session store, scripted model, fake tools),
launches DBOS, sends one message (session `s1` of user `u1`, seeded by the test) and follows the
turn; a CRASH_AT point kills the process with os._exit. `resume`: the same installs and launch (DBOS
recovers the pending turn of this executor and app version), then waits for it and dumps its
stream. The model script is indexed by the number of *returned* provider calls in the log, so the
process that recovers gets the answer the crashed call would have got.
CRASH_AT: `provider:<n>` (inside the n-th provider call) or `after_append:<type>` (the session-log
row of that type landed, its step is not checkpointed)."""
import json
import os
import sys
import time
from datetime import UTC, datetime

from dbos import DBOS

from evowiki.agent import dbos_app
from evowiki.assistant import turn_runs
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps
from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.service import AssistantService
from evowiki.assistant.turn_runs import STREAM_KEY, DbosTurnRuns
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.store.db import make_pool
from evowiki.store.llm_logs import PgAttemptLog, PgCallLog
from tests.assistant.fakes import Data, FakeTool, tools

MODE, DB, LOG = sys.argv[1:4]
APP_VERSION = "crash"
EXECUTOR = "crash-exec"
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
CRASH_AT = os.environ.get("CRASH_AT", "") if MODE == "crash" else ""
REF = ModelRef(provider="scripted", model="m")

SCRIPT = [ChatTurn(tool_calls=(ToolCall(id="c1", name="web_search", args={"query": "z"}),)),
          ChatTurn(tool_calls=(ToolCall(id="c2", name="propose_page_edit", args={}),)),
          ChatTurn(text="我建议加一段。" + "答" * 100)]


def log(line: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def returned() -> int:
    try:
        with open(LOG, encoding="utf-8") as f:
            return sum(1 for ln in f if ln.startswith("returned "))
    except FileNotFoundError:
        return 0


def answer(_messages, _request):
    n = returned() + 1
    log(f"provider {n}")
    if CRASH_AT == f"provider:{n}":
        log(f"crash provider:{n}")
        os._exit(1)
    out = SCRIPT[n - 1]
    log(f"returned {n}")
    return out


class LoggedTool(FakeTool):
    def run(self, args, ctx):
        log(f"tool {self.name}")
        return super().run(args, ctx)


class CrashStore(PgSessionStore):
    """Appends the row, then dies before the workflow step that called it can be checkpointed."""

    def append(self, session_id, entry_id, type, payload, at):
        out = super().append(session_id, entry_id, type, payload, at)
        log(f"append {type}")
        if CRASH_AT == f"after_append:{type}":
            log(f"crash {CRASH_AT}")
            os._exit(1)
        return out


class Ids:
    """Child ids start high (the seed's, in the test process, are low), and differ per process like
    the service's uuids: a resumed process must not reuse an id the dead one handed out."""

    def __init__(self):
        self.n = 5000 if MODE == "crash" else 7000

    def __call__(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n:04d}"


class Applier:
    def apply(self, user_id, draft):
        return ("s9",)


def main() -> None:
    pool = make_pool(DB, min_size=1, max_size=4)
    gw = Gateway(GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True),
                 PgCallLog(pool), mode="record", attempts=PgAttemptLog(pool),
                 providers={"scripted": ScriptedChatProvider("scripted", answer)})
    ts = [LoggedTool(t.name, t.effect, t.result) for t in tools()]
    deps = TurnDeps(gateway=gw, tools=ts, store=CrashStore(pool), data=Data(),
                    new_id=Ids(), clock=lambda: NOW)
    svc = AssistantService(deps, Applier(), DbosTurnRuns())
    turn_runs.install(svc)                        # before launch: recovery runs at launch
    dbos_app.launch(DB, app_version=APP_VERSION, executor_id=EXECUTOR)
    if MODE == "crash":
        for _ in svc.send("u1", "s1", MessageIn(text="问")):
            pass
    [w] = DBOS.list_workflows(workflow_id_prefix="turn:s1:", load_input=False, load_output=False)
    result = DBOS.retrieve_workflow(w.workflow_id).get_result()
    log(f"result {result}")
    for raw in DBOS.read_stream(w.workflow_id, STREAM_KEY, polling_interval_sec=0.1):
        log("item " + json.dumps(raw, sort_keys=True, ensure_ascii=False))
    time.sleep(0.2)
    dbos_app.shutdown()
    pool.close()


if __name__ == "__main__":
    main()
