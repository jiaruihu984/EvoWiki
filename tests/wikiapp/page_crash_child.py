"""Child process for the B1b crash tests (never imported by the app). Usage:
    python -m tests.wikiapp.page_crash_child <organize|idle> <crash|resume> <db_url> <log> <page_id>

`crash`: installs the page deps, launches DBOS, starts the page workflow for the seeded page (the
`idle` scenario: the Bot's idle write of page 2, on the user's page queue) and waits for it (a CRASH_AT point kills the process with os._exit). `resume`: the same installs and
launch (DBOS recovers the pending workflow of this executor and app version), then waits for it and
dumps the stream. The scripts are deterministic and indexed by the number of *returned* provider
calls in the log, so the process that recovers gets the answer the crashed call would have got.
CRASH_AT: `<purpose>:<n>` (inside the n-th provider call of that purpose) or `after_commit`
(the first wiki commit landed, its step is not checkpointed) or `after_llm_call:<purpose>:<n>`
(the n-th call's llm_call row is written, the gateway step is not checkpointed)."""
import dataclasses
import json
import os
import sys
import time
from datetime import UTC, datetime

from dbos import DBOS

from evowiki.agent import dbos_app
from evowiki.deep.store import PgAssetStore
from evowiki.deep.writer import PageWriter
from evowiki.store.db import make_pool
from evowiki.store.llm_logs import PgAttemptLog, PgCallLog
from evowiki.wikiapp import page_flows
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import Writing
from evowiki.wikiapp.page_flows import PageDeps
from evowiki.wikiapp.page_run_store import PgPageRunStore
from evowiki.wikiapp.page_runs import STREAM_KEY, DbosPageRuns, PageJob, register_queue
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import PgRepo
from tests.deep.fakes import (
    DeepScript,
    FakeChecker,
    call,
    library,
    plan_page,
    section,
    self_check,
    turn,
)
from tests.deep.test_evoui_parity import BASE
from tests.wikiapp.script import Script
from tests.wikiapp.test_k1_replay import PAGE1, PAGE2

SCENARIO, MODE, DB, LOG, PAGE_ID = sys.argv[1:6]
APP_VERSION = "crash"
EXECUTOR = "crash-exec"
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
CRASH_AT = os.environ.get("CRASH_AT", "") if MODE == "crash" else ""


def log(line: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def returned(purpose: str) -> int:
    try:
        with open(LOG, encoding="utf-8") as f:
            return sum(1 for ln in f if ln.startswith(f"returned {purpose} "))
    except FileNotFoundError:
        return 0


def serve(purpose: str, fetch):
    """One provider call: its number is the count of calls that returned before it, plus one."""
    n = returned(purpose) + 1
    log(f"provider {purpose} {n}")
    if CRASH_AT == f"{purpose}:{n}":
        log(f"crash {purpose}:{n}")
        os._exit(1)
    out = fetch(n)
    log(f"returned {purpose} {n}")
    return out


class ChildDeep(DeepScript):
    def __init__(self, scripts: dict[str, list]):
        super().__init__()
        self.scripts = scripts

    def _next(self, provider, purpose, subject):
        return serve(purpose, lambda n: self.scripts[purpose][n - 1])


class ChildScript(Script):
    def next_output(self, request):
        return serve(request.purpose, lambda n: super(ChildScript, self).next_output(request))


class CountingChecker(FakeChecker):
    def check_block(self, block_id, block, *args, **kw):
        log(f"check {block_id}")
        return super().check_block(block_id, block, *args, **kw)


class CrashRepo(PgRepo):
    """Commits, then dies before the workflow step that called it can be checkpointed."""

    def commit(self, snap, c, *, once=None):
        out = super().commit(snap, c, once=once)
        if CRASH_AT == "after_commit":
            log("crash after_commit")
            os._exit(1)
        return out


class CrashCallLog(PgCallLog):
    """Appends the llm_call row, then dies: the model answered and was recorded, the step was not."""

    def append(self, call) -> None:
        super().append(call)
        n = returned(call.purpose)
        if CRASH_AT == f"after_llm_call:{call.purpose}:{n}":
            log(f"crash {CRASH_AT}")
            os._exit(1)


class Ids:
    """Child ids start high: the seed's (test process) are 'x_0001'... They are numbered by the ids
    already handed out in this log (crash and resume children alike), as a unique generator would:
    the run commits before a crash (its outline and sections, stage 4 T3), so the recovering
    child must not hand out those ids again."""

    def __call__(self, prefix: str) -> str:
        n = 5001 + returned("id")
        log(f"returned id {prefix} {n}")
        return f"{prefix}_{n:04d}"


def block_turns(outline: dict, with_block: str) -> list[dict]:
    """plan_page with the outline, one write turn per section (the `with_block` section carries
    an EvoUI block), then finish."""
    turns = [turn(plan_page(outline=outline))]
    for i, sec in enumerate(outline["sections"], start=2):
        sid = sec["id"]
        if sid == with_block:
            turns.append(turn(
                section(sid, i, heading=sec["heading"], role="explain",
                        body=f"{sid} 精修正文。\n\n{{{{block:proj}}}}\n"),
                call("write_block", i, block_id="proj", section_id=sid, why="动手看", block=BASE)))
        else:
            turns.append(turn(section(sid, i, heading=sec["heading"], role="explain")))
    turns.append(turn(call("finish", 99, self_check=self_check())))
    return turns


def main() -> None:
    outline = PAGE1 if SCENARIO == "organize" else PAGE2
    pool = make_pool(DB, min_size=1, max_size=4)
    repo = CrashRepo(pool)
    script = ChildScript()
    deep = ChildDeep({"deep.write": block_turns(outline, "s2")})
    llm = dataclasses.replace(deep.llm(), log=CrashCallLog(pool), attempts=PgAttemptLog(pool))
    writing = Writing(writer=PageWriter(llm=llm, checker=CountingChecker(), library=library()),
                      specs=WikiSpecSource(repo), assets=PgAssetStore(pool))
    store = PgPageRunStore(pool)
    ids = Ids()
    deps = PageDeps(repo=repo, gateway=lambda _u: CountingGateway(script.gateway()),
                    writing=writing, store=store, clock=lambda: NOW, new_id=ids)
    page_flows.install(deps)                      # before launch: recovery runs at launch
    dbos_app.launch(DB, app_version=APP_VERSION, executor_id=EXECUTOR)
    register_queue()          # the user's page queue: a restart recovers what was still queued
    runs = DbosPageRuns(store)
    run_id = f"page:u1:{PAGE_ID}:1"
    if MODE == "crash":
        origin = "open" if SCENARIO == "organize" else "idle"
        started, _ = runs.start(PageJob(kind="organize", user_id="u1", page_id=PAGE_ID, now=NOW,
                                        origin=origin), deps, attach=True)
        assert started == run_id
        log(f"run {run_id}")
    result = DBOS.retrieve_workflow(run_id).get_result()
    log(f"result {result}")
    for raw in DBOS.read_stream(run_id, STREAM_KEY, polling_interval_sec=0.1):
        log("item " + json.dumps(raw, sort_keys=True, ensure_ascii=False))
    time.sleep(0.2)
    dbos_app.shutdown()
    pool.close()


if __name__ == "__main__":
    main()
