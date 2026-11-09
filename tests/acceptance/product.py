"""The running product the acceptance checks drive: the real service (`build_services` + `create_app`,
DBOS page runs and assistant turns, the real gateway with usage metering) on a fresh Postgres database
(tests/pg.py), served by uvicorn on 127.0.0.1 and spoken to over HTTP / SSE the way the Obsidian
plugin speaks to it. Only the models are fake:

- every model call goes through the runtime gateway to scripted DeepSeek providers (the two DeepSeek
  tiers of DeepSeek mode): the assistant's chat turn and the goal / path / outline calls are answered
  by the 黑壳虾 fakes of tests/e2e/blocks_backend.py (the approved demo's six blocks); the diagnosis
  is the assistant's, in the chat (stage 3 T11): `diagnosis_turn` is the scripted assistant of a
  turn with intent diagnose (the turn calls ask_rough; then ask_check per pick, a question per turn);
- per-answer background consolidation and anchor maintenance receive explicit scripted replies;
- pages are written by the scripted page writer (tests/wikiapp/fake_writer.ScriptWriter), one call per
  section, optionally slowed down (`write_delay`) so a run can be watched and stopped mid-way.

Fail closed: a call nothing is scripted for raises blocks_backend's `Unscripted` and is recorded in
`Product.unscripted`; every check asserts that list is empty, so a check never passes or fails on a
call the harness did not plan for.

Not a product default, set here only: the test user's RSI cycle is manual (a due cycle would make model
calls nobody scripted), as in tests/e2e/wiki_backend.py."""

import contextlib
import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import uvicorn

from evowiki.assistant.tools.check import NO_PICK
from evowiki.deep.store import MemoryAssetStore
from evowiki.llm.chat import ChatRequest, ChatTurn, ToolCall
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.runtime.llm import DEEPSEEK_NOTHINK
from evowiki.service import deps
from evowiki.service.app import create_app
from evowiki.service.deps import build_services
from evowiki.store.rsi import PgRsiStore
from evowiki.teach.contracts import RsiSettings
from evowiki.wikiapp.organize import Writing
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import CONFLICT_DETAIL, PgRepo
from tests.e2e import blocks_backend
from tests.e2e.wiki_backend import FailClosedScript
from tests.wikiapp.fake_writer import WRITE, ScriptWriter

USER = "learner"
SURVEY = {"math": "university", "ml": "beginner", "dl": "none"}
SLOW_TURN = "慢慢讲"                 # a chat message whose answer streams slowly (the reload check)
SLOW_ANSWER = "黑壳虾其实就是新米虾属的一种小型淡水虾。" * 8
SLOW_PIECE_S = 0.15                 # one streamed piece of the slow answer every 0.15 s (~4 s in all)


# ---- the fake models ---------------------------------------------------------------------------

DIAGNOSE = "开始诊断"                # the plugin's diagnose message (ASSISTANT_COPY.diagnoseStart)
SELF_OPTIONS = ["学过，还会", "学过一点", "没学过"]
SELF_MEANS = ["know", "partly", "dont"]


def diagnosis_turn(request: ChatRequest) -> ChatTurn | None:
    """The scripted assistant of the diagnosis in the chat (the assess Skill's order; the turn
    itself called ask_rough): ask_check on the pick the last result names (a self-report, or a
    two-option quiz); when none is named, one sentence. None: not a diagnosis turn (his last message
    is not the diagnose message)."""
    msgs = request.messages
    users = [i for i, m in enumerate(msgs) if m.role == "user"]
    if not users or DIAGNOSE not in msgs[users[-1]].text:
        return None
    n = len([m for m in msgs[users[-1] + 1:] if m.role == "tool"])
    last = msgs[-1]
    if NO_PICK not in last.text and "pick_id：" in last.text:
        pick = re.findall(r"pick_id：(\S+)", last.text)[-1:]
        names = re.findall(r"知识点 / 块：「([^」]+)」", last.text)[-1:]
        assert pick and names, last.text
        pick, name = pick[0], names[0]
        item = ({"self_report": {"stem": f"学过「{name}」吗？", "options": SELF_OPTIONS,
                                 "means": SELF_MEANS}}
                if "自评" in last.text else
                {"quiz": {"stem": f"「{name}」这一块讲的是黑壳虾的哪方面？",
                          "options": [f"{name}本身", "和黑壳虾无关的事"], "answer": 0}})
        return ChatTurn(tool_calls=(ToolCall(id=f"dg-{n}-ask", name="ask_check",
                                             args={"pick_id": pick, **item}),))
    return ChatTurn(text="好，摸底做完了，从第一块开始。")


def anchor_reply(prompt: str) -> dict:
    """A cited background sentence from the actual bounded raw input, with no invented learning."""
    evidence = json.loads(prompt)
    raw = evidence['raw']
    if not raw:
        return {'statements': [], 'learning': []}
    node, turn = next(iter(raw.items()))
    return {'statements': [{'text': turn['question'][:600], 'sources': [node],
                            'state': 'background'}], 'learning': []}


class Models:
    """The scripted DeepSeek providers: blocks_backend's 黑壳虾 fakes, plus one slow chat answer."""

    def __init__(self) -> None:
        self.script = blocks_backend.build_script()
        # Per-answer maintenance runs through the real background workflow. These goal
        # operations do not assert durable preferences, decisions, or personal facts.
        self.script.always("assistant.consolidate", lambda _prompt: {
            "items": [], "corrected": [], "withdrawn": []})
        self.script.always("memory_anchor", anchor_reply)
        self.script.always("goals.rough_typed", lambda _prompt: {"marks": []})   # his typed words state no block
        # the scripted writer's sections (one per write call): enough for every page a check writes,
        # including the pages the idle Bot writes ahead
        self.script.push(WRITE, *[{"text": f"回想：第 {i} 段\n\n这一段先用一个具体的例子讲清楚，再说它为什么这样。"}
                                  for i in range(1, 401)])
        self._slow = threading.local()

    def _chat(self, script: FailClosedScript, request: ChatRequest) -> ChatTurn:
        users = [m for m in request.messages if m.role == "user"]
        said = users[-1].text if users else ""
        self._slow.on = SLOW_TURN in said
        if self._slow.on:
            return ChatTurn(text=SLOW_ANSWER)
        diag = diagnosis_turn(request)
        if diag is not None:
            return diag
        return blocks_backend.assistant_turn(script, request)

    def _piece(self, _i: int) -> None:
        if getattr(self._slow, "on", False):
            time.sleep(SLOW_PIECE_S)

    def providers(self) -> dict[str, ScriptedChatProvider]:
        return {p: ScriptedChatProvider(p, lambda _m, r: self._chat(self.script, r),
                                        complete=blocks_backend.assistant_json(self.script, p),
                                        stream_chunk=8,
                                        on_chunk=self._piece)
                for p in ("deepseek", DEEPSEEK_NOTHINK)}

    @property
    def unscripted(self) -> list[str]:
        return self.script.unscripted

    def served(self, purpose: str | None = None) -> list[str]:
        return [s for s in self.script.served() if purpose is None or s.startswith(f"{purpose}/")]


class SlowWriter(ScriptWriter):
    """The scripted page writer (one model call per section), `delay` seconds per call; `calls` logs
    every section call as (monotonic time when it returned, page id)."""

    def __init__(self, script: FailClosedScript, delay: float):
        super().__init__(self)              # ScriptWriter takes its answers from self.take
        self._script, self.delay = script, delay
        self.calls: list[tuple[float, str]] = []
        self._page = threading.local()

    def write(self, inp, **kw):
        self._page.id = inp.page_id
        return super().write(inp, **kw)

    def take(self, purpose: str, prompt: str = "take") -> str:
        if self.delay:
            time.sleep(self.delay)
        out = self._script.take(purpose, prompt)
        self.calls.append((time.monotonic(), self._page.id))
        return out

    def calls_for(self, page_id: str, after: float = 0.0) -> int:
        return sum(1 for t, pid in self.calls if pid == page_id and t > after)


# ---- talking to it -----------------------------------------------------------------------------

@dataclass
class Event:
    name: str
    data: Any
    id: str | None = None


def sse(lines: Iterator[str]) -> Iterator[Event]:
    """The events of an SSE response, as they arrive (comment frames skipped)."""
    name, data, eid = "message", [], None
    for line in lines:
        if line == "":
            if data:
                yield Event(name, json.loads("\n".join(data)), eid)
            name, data, eid = "message", [], None
        elif line.startswith(":"):
            continue
        else:
            key, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if key == "event":
                name = value
            elif key == "data":
                data.append(value)
            elif key == "id":
                eid = value


@dataclass
class Product:
    url: str
    svc: Any
    models: Models
    writer: SlowWriter
    user: str = USER
    http: httpx.Client = field(init=False)
    conflicts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.http = httpx.Client(base_url=self.url, headers={"X-EvoWiki-User": self.user},
                                 timeout=httpx.Timeout(60.0))

    def get(self, path: str, **params) -> Any:
        r = self.http.get(path, params=params)
        assert r.status_code == 200, f"GET {path}: {r.status_code} {r.text}"
        return r.json()

    def post(self, path: str, body: Any = None, *, ok: tuple[int, ...] = (200, 201)) -> Any:
        r = self.http.post(path, json=body if body is not None else {})
        assert r.status_code in ok, f"POST {path}: {r.status_code} {r.text}"
        return r.json() if r.content else None

    def click(self, path: str, body: Any = None) -> Any:
        """A learner's button that may meet 「数据刚被另一个请求改过，请重试」 (409: the idle Bot
        committed a page it wrote ahead at the same moment): the learner does what it says and
        clicks again. Each such retry is counted in `conflicts`."""
        for _ in range(5):
            r = self.http.post(path, json=body if body is not None else {})
            if r.status_code == 409 and r.json().get("detail") == CONFLICT_DETAIL:
                self.conflicts.append(path)
                continue
            assert r.status_code in (200, 201), f"POST {path}: {r.status_code} {r.text}"
            return r.json()
        raise AssertionError(f"POST {path}: still 409 after 5 tries")

    def idle(self, timeout: float = 30.0) -> None:
        """Waits until no page of the learner is being written (the idle Bot writes ahead)."""
        deadline = time.monotonic() + timeout
        while self.get("/wiki/writes") and time.monotonic() < deadline:
            time.sleep(0.2)

    @contextlib.contextmanager
    def stream(self, method: str, path: str, body: Any = None) -> Iterator[Iterator[Event]]:
        """An SSE request; leaving the block closes the connection (what a client that stops
        reading - the plugin's 停止, a reload - does to the server)."""
        kw = {"json": body} if body is not None else {}
        with self.http.stream(method, path, **kw) as r:
            assert r.status_code == 200, f"{method} {path}: {r.status_code} {r.read()!r}"
            yield sse(r.iter_lines())

    def events(self, method: str, path: str, body: Any = None) -> list[Event]:
        with self.stream(method, path, body) as evs:
            return list(evs)

    # what the learner does, as the plugin sends it ----------------------------------------------

    def new_chat(self) -> str:
        return self.post("/assistant/sessions", {})["session_id"]

    def say(self, session_id: str, text: str, **extra: Any) -> list[Event]:
        """The learner sends a chat message (`extra`: the message's other fields, as the plugin
        sends them: intent, reply_data) and reads the answer to the end."""
        evs = self.events("POST", f"/assistant/sessions/{session_id}/messages",
                          {"text": text, **extra})
        assert evs and evs[-1].name == "done", [(e.name, e.data) for e in evs]
        return evs

    def ask_for_goal(self, text: str) -> dict:
        """A new chat, the learner's message; returns the goal card it shows (its goal_new row)."""
        self.say(self.new_chat(), text)
        rows = [a for a in self.get("/wiki/bot/actions")
                if a["kind"] == "goal_new" and a["status"] == "proposed"]
        assert len(rows) == 1, rows
        return rows[0]

    def confirm(self, card: dict) -> dict:
        """确认 on the goal card, then what the chat shows under it (goal-run.ts start():
        LearnFlow.openGoal reads the draft's round, or the map)."""
        row = self.post(f"/wiki/bot/actions/{card['action_id']}/approve", {})
        result = row["result"]
        if result.get("draft_id"):
            return self.get(f"/wiki/goals/{result['draft_id']}")
        assert result.get("map_id"), row
        return {"draft_id": None, "map_id": result["map_id"], "path_id": None, "page_id": None,
                "questions": [], "map": self.get(f"/wiki/maps/{result['map_id']}")}

    def answer(self, out: dict, how: str, rounds: list[dict]) -> dict:
        """The diagnosis of the draft `out` shows: "skip" (跳过，直接开始: POST …/answers skip) or
        "idk" (the diagnosis in the chat, `diagnose_in_chat`: nothing marked on the 摸底 grid, 不知道
        to every question). Each question asked is one entry of `rounds`."""
        if how == "skip":
            rounds.append(out)
            return self.post(f"/wiki/goals/{out['draft_id']}/answers", {"skip": True})
        return self.diagnose_in_chat(out["draft_id"], rounds)

    def diagnose_in_chat(self, draft_id: str, rounds: list[dict]) -> dict:
        """What the plugin's GoalRun does when the open round has no question (stage 3 T11): one
        message with intent diagnose in a chat; the turn ends on a card — the 摸底 grid (ask_rough),
        answered with nothing marked, or a question (ask_check), answered 不知道 (a self-report:
        没学过; the option he clicks is sent as his message) — and his message resumes it, until a turn ends on no card.
        Returns the draft as the chat then reads it (GET /wiki/goals/{id}: finished)."""
        sid = self.new_chat()
        evs = self.say(sid, DIAGNOSE, intent="diagnose", draft_id=draft_id)
        for _ in range(60):
            ask = next((e for e in reversed(evs) if e.name == "ask"), None)
            if ask is None:
                break
            if ask.data["kind"] == "ask_rough":
                evs = self.say(sid, "先不标", reply_data={"marks": {}})
                continue
            assert ask.data["kind"] == "ask_check", ask.data
            rounds.append({"draft_id": draft_id, "questions": [ask.data["payload"]]})
            options = ask.data["payload"]["options"]
            # 不知道 on a quiz; a self-report has no 不知道: 没学过
            evs = self.say(sid, "不知道" if "不知道" in options else "没学过")
        else:
            raise AssertionError("the diagnosis went on for 60 messages")
        out = self.get(f"/wiki/goals/{draft_id}")
        assert out["draft_id"] is None, f"the diagnosis is still open: {out}"
        return out

    def until_first_path(self, out: dict, how: str, rounds: list[dict] | None = None) -> dict:
        """What the chat and the learner do after 确认 (goal-run.ts advance(); on-demand
        decomposition, spec 2026-10-28 §2.2): each diagnosis round is answered `how`; a finished
        opening diagnosis enters the next block (`nextRegion`); entering shows its parts and the
        learner picks one (the first one not done yet — the product never picks it); opening a
        point plans its path. Returns that path (GET /wiki/paths/{id}). `rounds` collects every
        round shown."""
        rounds = [] if rounds is None else rounds
        map_id = out.get("map_id") or (out.get("map") or {}).get("map_id")
        entered: str | None = None
        for _ in range(20):
            if out.get("draft_id"):
                out = self.answer(out, how, rounds)
                map_id = map_id or out.get("map_id") or (out.get("map") or {}).get("map_id")
                continue
            if out.get("path_id"):
                return self.get(f"/wiki/paths/{out['path_id']}")
            assert map_id, out
            m = self.get(f"/wiki/maps/{map_id}")
            part = pick_part(m, entered) if entered is not None else None
            node = part or next_region(m)
            assert node is not None, "the map has no block to enter"
            entered = node
            out = self.post(f"/wiki/maps/{map_id}/regions/{node}/enter")
        raise AssertionError("no path after 20 steps")

    @contextlib.contextmanager
    def open_page(self, path_id: str) -> Iterator[tuple[str, Iterator[Event]]]:
        """生成第一页 / 下一页 (wiki controller): POST /wiki/paths/{id}/next, then the page's organize
        stream (it starts the page's write run, or follows the one already running). When every
        row has its page already (the idle Bot started writing it: a point's path is often one
        page), the plugin opens that row's page instead (StepOut start_page with a page_id)."""
        r = self.http.post(f"/wiki/paths/{path_id}/next", json={})
        if r.status_code == 409 and r.json().get("detail") == ALL_STARTED:
            path = self.get(f"/wiki/paths/{path_id}")      # the Bot started it meanwhile
            page_id = next(x["page_id"] for x in path["pages"]
                           if x["page_id"] is not None and x["status"] != "done")
        else:
            assert r.status_code in (200, 201), f"POST next: {r.status_code} {r.text}"
            page_id = r.json()["page_id"]
        with self.stream("POST", f"/wiki/pages/{page_id}/organize") as evs:
            yield page_id, evs

    def read_through(self, path_id: str) -> str:
        """Opens the path's next page and reads its writing to the end; returns the page id."""
        with self.open_page(path_id) as (page_id, evs):
            names = [e.name for e in evs]
        assert names and names[-1] == "done", names
        return page_id

    def finish(self, page_id: str) -> dict:
        """学完 on a page (the 「读完了」 block's main button: POST /wiki/pages/{id}/done, as the plugin
        sends it)."""
        return self.click(f"/wiki/pages/{page_id}/done")


ALL_STARTED = "这条路径的页都已经开始了"


def pick_part(m: dict, node: str) -> str | None:
    """The part of an entered node the learner picks: the first one not done yet (no path, no
    page, not known); None when the node shows no parts."""
    return next((n["id"] for n in m["nodes"] if n["parent"] == node and n["path_id"] is None
                 and n["page_id"] is None and n["state"] != "known"), None)


def next_region(m: dict) -> str | None:
    """The node a finished opening diagnosis enters (goal-run.ts nextRegion): the map's
    next_region (any node: a block, a part, an entered block waiting for a pick), else the first
    top-level block not known yet."""
    regions = [n for n in m["nodes"] if n["kind"] == "region"]
    named = m.get("next_region")
    if named is not None and any(n["id"] == named and n["state"] != "known" for n in m["nodes"]):
        return named
    ids = {r["id"] for r in regions}
    return next((r["id"] for r in regions
                 if (r["parent"] is None or r["parent"] not in ids) and r["state"] != "known"), None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def running_product(db_url: str, monkeypatch, *, write_delay: float = 0.0) -> Iterator[Product]:
    """The service on `db_url` (a fresh database), up on a local port until the block ends."""
    models = Models()
    providers = models.providers()
    monkeypatch.setattr(deps, "runtime_providers", lambda _env: providers)
    monkeypatch.setattr(deps, "runtime_deep_providers", lambda _env: {})
    env = {"EVOWIKI_DATABASE_URL": db_url, "EVOWIKI_EMBEDDER": "hash",
           "EVOWIKI_RUNTIME_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "fake-acceptance-key"}
    svc = build_services(env)
    writer = SlowWriter(models.script, write_delay)
    svc.page_writing = Writing(writer=writer, specs=WikiSpecSource(PgRepo(svc.pool)),
                               assets=MemoryAssetStore())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(svc), host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, name="acceptance-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 120
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the service did not start")
        time.sleep(0.05)
    product = Product(f"http://127.0.0.1:{port}", svc, models, writer)
    try:
        product.post("/users", {"user_id": USER, "survey": SURVEY, "condition": "S2", "lang": "zh"})
        # The learner confirms the displayed defaults before the first model call, exactly
        # through the product API; acceptance never disables the central spending gate.
        budget = product.get("/usage/settings")
        assert (budget["daily_limit_usd"], budget["monthly_limit_usd"], budget["budget_confirmed"]) == (5, 30, False)
        confirmed = product.http.patch("/usage/settings", json={"confirm_budget": True})
        assert confirmed.status_code == 200 and confirmed.json()["budget_confirmed"] is True
        PgRsiStore(svc.pool).put_settings(USER, RsiSettings(cycle="manual"))   # see module doc
        yield product
        product.idle()              # no write left running into the shutdown
    finally:
        product.http.close()
        server.should_exit = True
        thread.join(timeout=60)
        svc.pool.close()
