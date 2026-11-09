"""Disposable /wiki backend for the Stage 4 real-Obsidian walkthrough (commit as you go: the writer
plans its outline with plan_page, then commits each section as it writes it; driver
obsidian-plugin/scripts/real-obsidian/stage4-walkthrough.mjs). Test-only.

It is tests/e2e/audit_backend.py (build_services with no API key on a fresh database of the test
cluster, DBOS launched by the app lifespan, fail-closed scripted models for intake / probe / path /
group naming / the chat), except the page writer: here it is the REAL `deep.writer.PageWriter` (its tool
loop, plan_page, write_section, finish, the commit-as-you-go stream), over a scripted chat model
for `deep.write` (one durable `llm.call:deep.write` step per turn, as with a real model):

- a turn with no accepted plan_page yet: plan_page (the plan; for a page without an outline, the
  outline `materials_backend.organize_reply` makes from the writer's first message);
- then one write_section per turn, s1 → s2 → s3, each after PACE seconds (POST /test/stage4/pace)
  and, when armed (POST /test/stage4/hold/{n}), held before section n until released
  (POST /test/stage4/release);
- then finish, with every self-check rule the first message lists, all ok.

Each section body names the write it belongs to (「第 N 次写」, one per plan_page), so a stopped
rewrite of a finished page can be told apart from the page it must keep. A tool error back from the
writer's tools is recorded and fails the turn (fail closed: the script never guesses a fix).

The writer's llm_call log never serves a recorded turn (`ST.cache` off; POST /test/stage4/cache?on=true
turns it on): with it, 重新写 after 停止 sends the stopped run's requests again and the gateway answers them
from the log without a model call (see the walkthrough README, finding 5).

GET /test/stage4: every deep.write turn (what it answered, when, its workflow), the hold, the
errors. GET /test/stage4/page/{page_id}: the stored record (writing mark, has_body, section bodies).
The audit backend's /test/audit, /test/runs and /test/wiki-script stay.

  uv run python -m tests.e2e.stage4_backend --port 8804 --user stage4-user
"""

import re
import threading
import time
from typing import Any

from evowiki.deep.library import MemoryLibrary
from evowiki.deep.llm import DeepLLM
from evowiki.deep.store import MemoryAssetStore
from evowiki.deep.writer import PageWriter
from evowiki.llm.chat import ChatRequest, ChatTurn, ToolCall
from evowiki.llm.prices import load_prices
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import NullMeter
from evowiki.wikiapp.organize import Writing
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import PgRepo
from tests.deep.fakes import DEEP_CONFIG, FakeChecker
from tests.e2e import audit_backend as ab
from tests.e2e.materials_backend import organize_reply
from tests.runtime.llm_helpers import MemoryCallLog

PLAN = {"observable": "能说出黑壳虾是什么、养它要备什么", "one_at_a_time": ["是什么", "怎么养"],
        "contrast": "黑壳虾 vs 观赏虾", "spoken_answer": "先说它是什么，再说怎么养",
        "practice": "不用当场练：这一页是入门介绍"}
ORDER = ("s1", "s2", "s3")


class Stage4State:
    def __init__(self) -> None:
        self.pace = 5.0
        self.hold_at: int | None = None           # hold before this section (1-based)
        self.holding = False
        self.ev = threading.Event()
        self.ev.set()
        self.writes = 0                           # plan_page turns answered (= writes started)
        self.turns: list[dict[str, Any]] = []
        self.errors: list[str] = []
        # the gateway serves a request whose hash is in its llm_call log from the log (record mode
        # too). Off here: a rerun of a stopped write sends the same requests as the stopped run and
        # would be answered from its recorded turns without calling the scripted model (seen in
        # this walkthrough; see the README). POST /test/stage4/cache?on=true turns it back on.
        self.cache = False

    def hold(self, n: int) -> None:
        self.hold_at = n
        self.ev.clear()

    def release(self) -> None:
        self.hold_at = None
        self.ev.set()

    def wait(self, n: int) -> None:
        if self.hold_at != n:
            return
        self.holding = True
        try:
            self.ev.wait(900)
        finally:
            self.holding = False


ST = Stage4State()


def _rules(first: str) -> list[str]:
    """The self-check rule ids the writer's first message lists (「自查清单…」 then `- id: text`)."""
    part = first.split("自查清单", 1)[1].split("\n\n", 1)[0]
    return re.findall(r"^- ([A-Za-z0-9_]+): ", part, re.MULTILINE)


def _done(request: ChatRequest) -> tuple[bool, list[str]]:
    """(plan_page accepted, the sections written) from the tool results so far; a tool error is
    recorded and raised (the script does not fix its own mistakes)."""
    names = {c.id: (c.name, c.args) for m in request.messages for c in m.tool_calls}
    planned, written = False, []
    for m in request.messages:
        if m.role != "tool" or m.tool_call_id not in names:
            continue
        name, args = names[m.tool_call_id]
        if m.is_error:
            msg = f"{name} refused: {m.text[:400]}"
            ST.errors.append(msg)
            raise RuntimeError(msg)
        if name == "plan_page":
            planned = True
        elif name == "write_section":
            written.append(args["section_id"])
    return planned, written


def deep_turn(request: ChatRequest) -> ChatTurn:
    if request.purpose != "deep.write":
        msg = f"unscripted deep call {request.purpose}"
        ST.errors.append(msg)
        raise RuntimeError(msg)
    first = request.messages[0].text
    planned, written = _done(request)
    outline = organize_reply(first)
    by_id = {s["id"]: s for s in outline["sections"]}
    rec: dict[str, Any] = {"at": time.time(), "workflow": ab._workflow(),
                           "seen": [c.name + (f":{c.args.get('section_id')}" if c.name ==
                                              "write_section" else "")
                                    for m in request.messages for c in m.tool_calls],
                           "first": first[:80], "thread": threading.current_thread().name,
                           "n_messages": len(request.messages)}
    if not planned:
        ST.writes += 1
        args: dict[str, Any] = {"plan": PLAN}
        if "这一页还没有大纲" in first:
            args["outline"] = outline
        call = ToolCall(id=f"plan-{ST.writes}-{len(request.messages)}", name="plan_page",
                        args=args)
        rec.update(tool="plan_page", outline="outline" in args, write=ST.writes)
    elif (todo := [s for s in ORDER if s not in written]):
        sid = todo[0]
        n = ORDER.index(sid) + 1
        time.sleep(ST.pace)
        ST.wait(n)
        s = by_id[sid]
        organizer = s["role"] == "organizer"
        body = (f"第 {ST.writes} 次写 · 第 {n} 段。"
                + ("黑壳虾是一种常见的淡水小虾，先把它和你认识的虾对上号。" if organizer else
                   f"这一段讲「{s['heading']}」：先用一个具体的例子讲清楚，再说它为什么这样。"))
        call = ToolCall(id=f"ws-{ST.writes}-{sid}-{len(request.messages)}", name="write_section",
                        args={"section_id": sid, "heading": s["heading"], "role": s["role"],
                              "depth": "quick" if organizer else "example",
                              "cue": f"第 {n} 段讲的是什么例子"[:60], "answer": "一句答案", "body": body})
        rec.update(tool="write_section", section=sid, write=ST.writes)
    else:
        checks = [{"rule": r, "ok": True, "section_id": "s1", "note": ""} for r in _rules(first)]
        call = ToolCall(id=f"fin-{ST.writes}-{len(request.messages)}", name="finish",
                        args={"self_check": checks})
        rec.update(tool="finish", write=ST.writes)
    rec["answered_at"] = time.time()
    ST.turns.append(rec)
    return ChatTurn(tool_calls=(call,), input_tokens=1000, output_tokens=500)


def _complete(_model: str, request) -> str:
    msg = f"unscripted deep completion {request.purpose}"
    ST.errors.append(msg)
    raise RuntimeError(msg)


class SwitchableLog(MemoryCallLog):
    """The writer's llm_call log; `get` (the gateway's cache read) only while ST.cache is on."""

    def get(self, h):
        return super().get(h) if ST.cache else None


def deep_llm() -> DeepLLM:
    providers = {p: ScriptedChatProvider(p, lambda _m, r: deep_turn(r), _complete)
                 for p in ("anthropic", "deepseek")}
    return DeepLLM(config=DEEP_CONFIG, providers=providers, log=SwitchableLog(),
                   prices=load_prices(), meter=NullMeter())


def writing(repo: PgRepo, _model: Any) -> Writing:
    """audit_backend's `writing(repo, model)` hook: the real PageWriter over the scripted chat."""
    return Writing(writer=PageWriter(llm=deep_llm(), checker=FakeChecker(),
                                     library=MemoryLibrary()),
                   specs=WikiSpecSource(repo), assets=MemoryAssetStore())


_create_app = ab.create_app


def create_app(svc):
    app = _create_app(svc)

    @app.get("/test/stage4")
    def _status() -> dict[str, Any]:
        return {"turns": ST.turns, "errors": ST.errors, "pace": ST.pace, "hold_at": ST.hold_at,
                "holding": ST.holding, "writes": ST.writes, "cache": ST.cache, "now": time.time()}

    @app.post("/test/stage4/pace", status_code=204)
    def _pace(s: float) -> None:
        ST.pace = s

    @app.post("/test/stage4/cache", status_code=204)
    def _cache(on: bool) -> None:
        ST.cache = on

    @app.post("/test/stage4/hold/{n}", status_code=204)
    def _hold(n: int) -> None:
        ST.hold(n)

    @app.post("/test/stage4/release", status_code=204)
    def _release() -> None:
        ST.release()

    @app.get("/test/stage4/page/{page_id}")
    def _page(page_id: str) -> dict[str, Any]:
        rec = PgRepo(svc.pool).load(_user[0]).state.pages.get(page_id)
        if rec is None:
            return {"exists": False}
        return {"exists": True, "writing": rec.writing.model_dump() if rec.writing else None,
                "has_body": rec.has_body, "done": rec.done,
                "written": rec.written.model_dump(mode="json") if rec.written else None,
                "outline": ([s.id for s in rec.page.outline.sections] if rec.page else None),
                "texts": ({k: v.body for k, v in rec.page.texts.items()} if rec.page else {})}

    return app


_user = ["stage4-user"]


def main(argv: list[str] | None = None) -> None:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if "--user" in args:
        _user[0] = args[args.index("--user") + 1]
    else:
        args += ["--user", _user[0]]
    ab.writing = writing
    ab.create_app = create_app
    ab.main(args)


if __name__ == "__main__":
    main()
