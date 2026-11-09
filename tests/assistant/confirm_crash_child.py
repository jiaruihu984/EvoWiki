"""Child process for the stage 3 confirm-recovery test (never imported by the app). Usage:
    python -m tests.assistant.confirm_crash_child <crash|resume> <db_url> <log>

The session (seeded by the test) holds one pending call op on `drop_card`. The scripted model calls
`confirm` on it, then `confirm` again, then answers. `crash` kills the process inside the second
`confirm` (its step is not checkpointed); `resume` lets DBOS recover the turn: the first confirm's
step is replayed from its record (it does not run again), the second runs live and must find the op
closed from that recorded result. The log says how often the op's tool ran."""
import os
import sys
import time
from datetime import UTC, datetime

from dbos import DBOS

from evowiki.agent import dbos_app
from evowiki.agent.tools import ToolResult, ToolSpec
from evowiki.assistant import pending, turn_runs
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps
from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.service import AssistantService
from evowiki.assistant.tools.actions import Grounded
from evowiki.assistant.turn_runs import DbosTurnRuns
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.store.db import make_pool
from evowiki.store.llm_logs import PgAttemptLog, PgCallLog
from tests.assistant.fakes import Data

MODE, DB, LOG = sys.argv[1:4]
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
REF = ModelRef(provider="scripted", model="m")
PID = "po_m0_1"
CONFIRM = ChatTurn(tool_calls=(ToolCall(id="c1", name="confirm",
                                        args={"pending_id": PID, "asked": "好"}),))
SCRIPT = [CONFIRM, ChatTurn(tool_calls=(ToolCall(id="c2", name="confirm",
                                                 args={"pending_id": PID, "asked": "好"}),)),
          ChatTurn(text="删好了。")]


def log(line: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def count(prefix: str) -> int:
    try:
        with open(LOG, encoding="utf-8") as f:
            return sum(1 for ln in f if ln.startswith(prefix))
    except FileNotFoundError:
        return 0


def answer(_model, _request):
    n = count("returned ") + 1
    log(f"provider {n}")
    out = SCRIPT[n - 1]
    log(f"returned {n}")
    return out


_run = pending.ConfirmTool.run


def logged_confirm(self, args, ctx):
    log("confirm run")
    if MODE == "crash" and count("confirm run") == 2:
        log("crash confirm:2")
        os._exit(1)
    return _run(self, args, ctx)


pending.ConfirmTool.run = logged_confirm      # type: ignore[method-assign]


class DropCard:
    def __init__(self):
        self.spec = ToolSpec(name="drop_card", description="drop_card", effect="propose",
                             category="action", acts=True,
                             parameters={"type": "object",
                                         "properties": {"card_id": {"type": "string"}}})

    def run(self, args, ctx):
        log("tool drop_card")
        return ToolResult(content="已删")


class Ids:
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
    deps = TurnDeps(gateway=gw, tools=[Grounded(DropCard())], store=PgSessionStore(pool),
                    data=Data(), new_id=Ids(), clock=lambda: NOW)
    svc = AssistantService(deps, Applier(), DbosTurnRuns())
    turn_runs.install(svc)
    dbos_app.launch(DB, app_version="confirm-crash", executor_id="confirm-exec")
    if MODE == "crash":
        for _ in svc.send("u1", "s1", MessageIn(text="好")):
            pass
    [w] = DBOS.list_workflows(workflow_id_prefix="turn:s1:", load_input=False, load_output=False)
    log(f"result {DBOS.retrieve_workflow(w.workflow_id).get_result()}")
    time.sleep(0.2)
    dbos_app.shutdown()
    pool.close()


if __name__ == "__main__":
    main()
