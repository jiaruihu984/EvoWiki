"""Disposable /wiki backend for the Obsidian wiki-sidebar contract test
(obsidian-plugin/test/contract/wiki.contract.test.ts). Test-only.

Fresh Postgres database (bundled pgserver or EVOWIKI_TEST_DATABASE_URL), the runtime fixture
content, NO real LLM: the Gateway is scripted. The script is E1's
tests/fixtures/wiki_e2e/script.json (the z-score → PCA scenario, loaded with
tests/wikiapp/e2e_driver.load_script; synthetic, non-reportable) with this file's EXTRA entries
appended to each purpose's queue for the steps the contract test runs after that scenario
(deeper, ask, reorganize with a check). Each purpose answers
from its own queue in order, so the backend is single-use: restart it for every contract run.

Ids come from E1's deterministic per-prefix `Ids` (pg_0001 / pg_0002), the clock is the system
clock.

Fail closed: an unscripted LLM call raises `Unscripted`, a BaseException, so no `except
Exception` in the product (a swallowed verifier failure, for instance) can turn it
into a quiet answer; the request it happened in fails (HTTP 500 / a stream cut short). The call
is also recorded: GET /test/wiki-script returns {"served", "unscripted", "pending"} and the
contract test asserts `unscripted == []`.

/wiki identifies the caller by the X-EvoWiki-User header and 404s an unknown user, so the backend
creates the user at start (default `e2e-user`, E1's; printed as JSON on the first stdout line).

The test user's RSI cycle is set to "manual" at start (a setting of this test backend only, not a
product default): check answers, review grades and section feedback run the background RSI tick,
and a due cycle would make LLM calls this script has no answers for (`Unscripted` is a
BaseException, so the tick's `except Exception` cannot catch it). Manual is never due.

POST /test/flat-goal makes E1's goal (tests/wikiapp/flat_goal.py, TEST ONLY), as E1's driver does:
goals are made only in the 助手 now (S1's blocks), and this script and the contract walk are E1's
goal → one block → its path scenario. Its diagnosis is the chat's now (next_check / ask_check: the
assistant writes each round's question), so POST /test/answer-check does what the scripted assistant
does in E1's driver: the round's question (tests/wikiapp/goal_script.question_of) answered with its
key (answer_check), one round per call.

Sources (/wiki/sources) import inline with the hashing embedder and no model: the scripted gateway
is never asked (the toc falls back to the outline / page groups, summaries are skipped). Only files
and pasted text are uploaded, so no URL fetcher is set.

No deep service is configured (`Services.deep` is None): /wiki/assets/* and the refine routes
answer 503 here; interactives are tested with the plugin's fake data only. Pages are written by the
scripted page writer (`tests.wikiapp.fake_writer`), which answers each section from the script's
`goals.write` queue (single writer, 2026-10-10).

  uv run python -m tests.e2e.wiki_backend --port 8767 [--user e2e-user]
"""

import argparse
import dataclasses
import json
import os
import sys
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from evowiki.clock import SystemClock
from evowiki.llm.models import LLMRequest
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.service.app import create_app
from evowiki.service.deps import Services, import_deps
from evowiki.sources.runs import InlineSourceRuns
from evowiki.store.db import make_pool
from evowiki.store.rsi import PgRsiStore
from evowiki.teach.contracts import RsiSettings
from evowiki.wikiapp.page_run_store import PgPageRunStore
from evowiki.wikiapp.page_runs import InlinePageRuns, spawn_thread
from evowiki.wikiapp.repo import PgRepo
from tests.pg import build_template, fresh_database, start_admin_url
from tests.runtime.helpers import make_user, seed_runtime
from tests.wikiapp.e2e_driver import PCA_VERIFIED, PROBE, USER, Z_VERIFIED, Ids, load_script
from tests.wikiapp.fake_writer import writing
from tests.wikiapp.script import Script
from tests.wikiapp.verify import verify_prereqs

# The default embedder (retrieval.embed) reads the environment: like tests/conftest.py, the
# deterministic hashing embedder, never a model download.
os.environ.setdefault("EVOWIKI_EMBEDDER", "hash")

# Appended after E1's queues, in the order the contract test makes the calls (see its header).
# 讲解方式 从文字导入 (step 18) answers once: a second import on this backend is unscripted.
EXTRA: dict[str, list[dict[str, Any]]] = {
    "goals.write": [
        # 按当前状态重新组织 pg_0001: the single writer writes every prose section again (s1, then s2..s4), no per-section cache
        {"text": "回想：z 分数 = 用平均数和标准差当尺子\n\n先回到你会的平均数，再看 z 怎么读。"},
        {"text": "回想：均值 μ = Σx / N\n\n先把所有数加起来，再除以个数。你已经会了，这里只是提醒一下。"},
        {"text": "回想：方差是各数到均值距离平方的平均\n\nσ² = Σ(x − μ)² / N，你已经答对了这个公式。"},
        {"text": "回想：z = (x − μ) / σ，先减均值再除以标准差\n\n分子是离均值多远，分母把这个距离换成“几个标准差”。z = -1.5 就是比均值低 1.5 个标准差。"},
    ],
    "plan_page": [
        # 按当前状态重新组织 pg_0001: the first outline plus an embedded check
        {"profile": "你会算平均数，方差公式已经答对；z 分数会算一点，还不会解读。",
         "assumed": [],
         "sections": [
             {"id": "s1", "heading": "从你会的平均数出发", "role": "organizer", "cells": [],
              "why": "背景里说会算平均数"},
             {"id": "s2", "heading": "均值提醒", "role": "recap", "cells": ["c2.procedure"],
              "why": "均值你说懂了，留一张提醒卡"},
             {"id": "s3", "heading": "方差提醒", "role": "recap", "cells": ["c3.notation"],
              "why": "方差公式答对了，留一张提醒卡"},
             {"id": "s4", "heading": "z 分数怎么算、怎么读", "role": "explain",
              "cells": ["c1.procedure", "c1.interpret"], "why": "目标；解读还不会"},
             {"id": "s5", "heading": "确认一下", "role": "check", "cells": ["c1.interpret"],
              "why": "答案决定要不要再讲解读",
              "check": {"cell": "c1.interpret", "stem": "z = 2 表示什么？",
                        "options": ["比均值高 2 个标准差", "比均值高 2 分", "高于 2% 的人",
                                    "无法判断"],
                        "answer": 0}}]},
    ],
    "wiki.feedback_understand": [
        # step 18b: 读完觉得 太浅了 / 有点难 and the 「⋯」 menu's 太浅了 (no words). (The feedback dialog with a sentence left the
        # sidebar in b9262d04, chat-first: its reply went with it.)
        # The plans add nothing to the page (empty sections), so the note and K stay as the later steps expect.
        {"reading": "你觉得这一页讲得太浅", "missing": [],
         "page": {"summary": "暂时不改这一页", "sections": [], "component": None, "component_after": None},
         "style": {"kind": "讲得太浅", "before": "按默认的讲法写", "after": "讲得更深一点"}},
        {"reading": "你觉得这一页有点难", "missing": [],
         "page": {"summary": "暂时不改这一页", "sections": [], "component": None, "component_after": None},
         "style": {"kind": "讲得太难", "before": "按默认的讲法写", "after": "讲得更浅一点"}},
        {"reading": "你觉得这一节讲得太浅", "missing": [],
         "page": {"summary": "暂时不改这一页", "sections": [], "component": None, "component_after": None},
         "style": {"kind": "讲得太浅", "before": "按默认的讲法写", "after": "这一节讲得更深一点"}},
    ],
}


class Unscripted(BaseException):  # a BaseException on purpose (see the module doc)
    """An LLM call the script has no output for."""


class FailClosedScript(Script):
    """E1's Script, but an unscripted call raises `Unscripted` (not AssertionError, which the
    product may catch as a failed call) and is recorded for GET /test/wiki-script."""

    def __init__(self) -> None:
        super().__init__()
        self.unscripted: list[str] = []

    def _responder(self, provider: str):
        def answer(model: str, request: LLMRequest) -> str:
            out = self.next_output(request)
            if out is None:
                what = f"{request.purpose}/{request.subject} via {provider}"
                self.unscripted.append(what)
                print(f"[wiki_backend] UNSCRIPTED LLM CALL: {what}", file=sys.stderr, flush=True)
                raise Unscripted(what)
            self._served.append((provider, request))
            return out
        return answer

    def served(self) -> list[str]:
        return [f"{r.purpose}/{r.subject}" for _, r in self._served]


def build_script() -> FailClosedScript:
    base = load_script()
    s = FailClosedScript()
    for purpose, queue in base._queues.items():
        s.push(purpose, *queue)
    # goals.probe is a bank, not a queue (e2e_driver.bank_writer): the KST diagnosis picks the node
    # each round, so its model is an `always` responder that load_script installed on `base`.
    s.always(PROBE, base._always[PROBE])
    # every graph change enqueues the naming of its unnamed groups (wikiapp.group_naming): no names, so
    # the groups keep their fallback names (a goal's loose points: the goal's title), as in tests/e2e/materials_backend.py
    s.always("graph.group_naming", lambda _prompt: {"groups": []})
    for purpose, outputs in EXTRA.items():
        s.push(purpose, *outputs)
    return s


def mount_script_status(app: FastAPI, script: FailClosedScript) -> None:
    """TEST ONLY: what the scripted LLM served, refused and still holds."""

    @app.get("/test/wiki-script")
    def _status() -> dict[str, Any]:
        return {"served": script.served(), "unscripted": list(script.unscripted),
                "pending": script.pending()}


def mount_verify(app: FastAPI, svc: Services, user: str) -> None:
    """TEST ONLY: verify a goal's prerequisites as tests/wikiapp/e2e_driver does right after creating
    that goal (`goal=z`: Z_VERIFIED, 均值, 方差 → z 分数; `goal=pca`: PCA_VERIFIED, z 分数, 方差 → PCA).
    Relation layers (9b305f25, user ruling 2026-10-14): intake writes no verified prerequisite and
    nothing in the product verifies one yet, so without this the diagnosis and K infer nothing
    along them and the scenario loses its meaning."""
    sets = {"z": Z_VERIFIED, "pca": PCA_VERIFIED}

    @app.post("/test/verify-prereqs", status_code=204)
    def _verify(goal: Literal["z", "pca"] = "z") -> None:
        verify_prereqs(PgRepo(svc.pool), user, sets[goal], now=svc.clock(), new_id=svc.new_id)


def mount_flat_goal(app: FastAPI, svc: Services, user: str) -> None:
    """TEST ONLY (module doc): E1's goal for the contract walk — `goal` and his `words`, no
    materials — and its first round, as GoalDraftOut (201)."""
    from evowiki.service.routes_wiki import _draft_out
    from evowiki.wikiapp.llm import require
    from tests.wikiapp.flat_goal import flat_goal

    class FlatGoalIn(BaseModel):
        goal: str
        words: list[str] = []

    @app.post("/test/flat-goal", status_code=201)
    def _flat_goal(body: FlatGoalIn):
        res = flat_goal(PgRepo(svc.pool), require(svc.gateway, user), user, body.goal,
                        " ".join(body.words), now=svc.clock(), new_id=svc.new_id)
        return _draft_out(svc, user, res)


def mount_answer_check(app: FastAPI, svc: Services, user: str) -> None:
    """TEST ONLY (module doc): the open round of `draft_id` answered as E1's driver answers it — the
    scripted assistant's question on its pick, its key — and {"stem", "draft": GoalDraftOut}."""
    from evowiki.service.routes_wiki import _draft_out
    from evowiki.wikiapp.llm import require
    from tests.wikiapp.goal_script import answer_round, question_of

    @app.post("/test/answer-check", status_code=201)
    def _answer_check(draft_id: str):
        repo = PgRepo(svc.pool)
        d = repo.load(user).state.drafts[draft_id]
        q = question_of(d.rounds[-1])
        res = answer_round(repo, require(svc.gateway, user), user, draft_id, [q.answer],
                           now=svc.clock(), new_id=svc.new_id)
        return {"stem": q.stem, "draft": _draft_out(svc, user, res).model_dump(mode="json")}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--user", default=USER, help="the test user to create")
    a = ap.parse_args(argv)
    admin, _keep = start_admin_url()
    build_template(admin)
    url, _ = fresh_database(admin)
    pool = make_pool(url)
    ctx = seed_runtime(pool)
    make_user(pool, a.user)
    # TEST ONLY (see the module doc): no background RSI cycle can start during the contract run.
    PgRsiStore(pool).put_settings(a.user, RsiSettings(cycle="manual"))
    script = build_script()
    svc = Services(pool=pool, ctx=ctx, clock=SystemClock(), embedder=HashingEmbedder(),
                   gateway=script.gateway(), new_id=Ids(),
                   runs=InlinePageRuns(spawn=spawn_thread, store=PgPageRunStore(pool)),
                   page_writing=writing(PgRepo(pool), script))
    # TEST ONLY: source imports (资料, step 24) run inline with the hashing embedder and NO model (the toc comes from
    # the PDF outline or page groups, summaries are skipped with a note), so they never reach the scripted gateway.
    svc.sources = InlineSourceRuns(dataclasses.replace(import_deps(svc), llm_for=lambda _uid: None))
    app = create_app(svc)
    mount_script_status(app, script)
    mount_verify(app, svc, a.user)
    mount_flat_goal(app, svc, a.user)
    mount_answer_check(app, svc, a.user)
    print(json.dumps({"wiki_backend": f"http://127.0.0.1:{a.port}", "user": a.user}),
          flush=True)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
