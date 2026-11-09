"""The forge replay child's entries `deep.refine` / `assistant.turn` (harness 3b, Task 8): a case
encoded in the parent replays in the child to the same text as the in-process replay, with every
model call through the child's `converse` and no database. Scripted converses only."""

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from evowiki.deep.agent import PlanAnswers, SnapshotSeqs
from evowiki.deep.store import MemoryAssetStore, MemoryJobStore
from evowiki.forge import policy, replay_child, replay_entries
from evowiki.forge.binding import accounted
from evowiki.forge.patch import replay as forge_replay
from evowiki.harness import replay_cases
from evowiki.harness.diff import PackDiff
from evowiki.harness.pack import HarnessPack
from evowiki.harness.replay import Case, ReplayDeps, replay
from evowiki.harness.replay_cases import (
    MISSING_RECORD,
    SCHEMA_PARAM,
    CaseNotEncodable,
    assistant_turn,
    deep_refine,
    encode_assistant_case,
    encode_deep_case,
    parent_converse,
    restore,
    strip_h10,
)
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn, ToolDef
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import LLMCall
from evowiki.llm.prices import load_prices
from evowiki.llm.usage import Account, NullMeter
from evowiki.llm.usage_control import UsageControl
from evowiki.wikiapp.events import PageSaved
from evowiki.wikiapp.refine_service import DeepService
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import MemoryRepo
from tests.deep.fakes import (
    DEEP_CONFIG,
    DeepScript,
    FakeChecker,
    library,
    plan_page,
    simple_round,
    turn,
)
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import commit_wiki, organized

ROOT = Path(replay_child.__file__).resolve().parents[3]
H1 = "候选：每个符号第一次出现时先用一句话说它是什么。"
PACK = HarnessPack(4, {"H1.write": H1, "H10.deep.write.thinking": True})


def llm_call(purpose: str, provider: str) -> LLMCall:
    return LLMCall(id=f"c_{purpose}", input_hash="h", purpose=purpose, prompt_version="v",
                   subject="s", provider=provider, model="m", model_version=None, params={},
                   raw_output="", parsed_output=None, cost=None, latency=0.0, ts=NOW)


PLAN_TURN = turn(plan_page())                 # the writer's first turn (deep@v28)


class ScriptConverse:
    """The parent's converse, scripted per purpose (the child's purpose; under `serve` it is in the
    relabelled subject `<cid>:<purpose>:<subject>`): a plan is answered as JSON text, a write as
    the scripted turn. `provider`: the family every LLMCall reports."""

    def __init__(self, provider: str = "deepseek", **queues: list) -> None:
        self.provider = provider
        self.queues = {k if "." in k else k.replace("_", "."): list(v) for k, v in queues.items()}
        self.requests: list[ChatRequest] = []

    def __call__(self, req: ChatRequest) -> tuple[ChatTurn, LLMCall]:
        self.requests.append(req)
        purpose = (req.subject.split(":")[1] if req.purpose == forge_replay.REPLAY_PURPOSE
                   else req.purpose)
        q = self.queues.get(purpose)
        if not q:
            raise AssertionError(f"unscripted {purpose}")
        out = q.pop(0)
        if isinstance(out, Exception):
            raise out
        t = (ChatTurn(text=json.dumps(out, ensure_ascii=False)) if purpose == "deep.plan"
             else ChatTurn.model_validate(out))
        return t, llm_call(purpose, self.provider)

    def of(self, purpose: str) -> list[ChatRequest]:
        return [r for r in self.requests if r.purpose == purpose]


class World:
    """A MemoryRepo-backed deep service with an organized page (its s2 is replayed)."""

    def __init__(self) -> None:
        self.repo, self.ids = MemoryRepo(["u1"]), Ids()
        self.pid = organized((self.repo, Script(), self.ids))
        now = self.repo.load("u1")
        self.case = Case(case_id="c1", run_id="r1", page_id=self.pid, section_id="s2",
                         signal_ids=("sig1",), anchor=False,
                         snapshot=SnapshotSeqs(now.wiki_seq, now.aspect_seq, now.infer_version),
                         mode="rewrite", instruction="讲得更直观", user_id="u1")
        self.spec = WikiSpecSource(self.repo).spec(now)

    def service(self, ds: DeepScript) -> DeepService:
        return DeepService(repo=self.repo, jobs=MemoryJobStore(), assets=MemoryAssetStore(),
                           specs=WikiSpecSource(self.repo), checker=FakeChecker(), llm=ds.llm(),
                           clock=lambda: NOW, new_id=self.ids, library=library())

    def in_process(self, ds: DeepScript):
        return replay(self.case, PackDiff(), base_pack=strip_h10(PACK), base_spec=self.spec,
                      deps=ReplayDeps(deep=self.service(ds), repo=self.repo))

    def encoded(self) -> dict:
        return encode_deep_case(None, self.repo, self.case, PACK, self.spec, excerpts=None,
                                library=library(),
                                now=NOW)


@pytest.fixture
def world() -> World:
    return World()


def writes(ds: DeepScript) -> list[ChatRequest]:
    return [r for r in ds.requests if isinstance(r, ChatRequest) and r.purpose == "deep.write"]


# ---- deep.refine --------------------------------------------------------------------------


def test_the_child_entry_replays_what_the_in_process_replay_gives(world):
    ds = DeepScript().plan().write(*simple_round())
    ref = world.in_process(ds)
    case = world.encoded()
    json.dumps(case)                                            # it crosses the pipe as one line
    conv = ScriptConverse(deep_write=[PLAN_TURN, *simple_round()])
    out = deep_refine(case, conv, checker=FakeChecker())
    assert out.keys() == {"case_id", "text", "writer_provider", "cost", "run_id"}, out
    assert out["text"] == ref.text and "s2 精修正文" in out["text"]
    assert out["writer_provider"] == "deepseek" and ref.writer_provider == "anthropic"
    assert out["cost"] is None and out["case_id"] == "c1"
    assert out["run_id"].startswith("harness-replay:")
    # the writer saw exactly the in-process request: same snapshot, spec, pack, library and "now"
    (mine,), (theirs,) = conv.of("deep.write")[:1], writes(ds)[:1]
    assert mine.system == theirs.system and mine.messages == theirs.messages
    assert mine.tools == theirs.tools and mine.params == theirs.params
    assert H1 in mine.system


def test_the_case_carries_the_pack_without_h10_and_the_logs_up_to_the_snapshot(world):
    case = world.encoded()
    assert case["pack"] == {"version": 4, "values": {"H1.write": H1}}
    assert len(case["wiki_events"]) == world.case.snapshot.wiki_seq
    assert len(case["aspect_events"]) == world.case.snapshot.aspect_seq
    assert case["infer"] and case["spec"]["version"] == world.spec.version
    assert [c["component_id"] for c in case["library"]] == ["pair-demo"]
    assert "domain" not in case                                 # KEEP_DOMAIN: the stored one
    assert strip_h10(PACK).values == {"H1.write": H1} and strip_h10(PACK).version == 4


def test_later_events_are_not_shipped(world):
    case = world.encoded()
    rec = world.repo.load("u1").state.pages[world.pid]          # the page emptied after it
    commit_wiki((world.repo, Script(), world.ids),
                PageSaved(record=rec.model_copy(update={"page": None}), action="rewritten"))
    assert world.repo.load("u1").wiki_seq > world.case.snapshot.wiki_seq
    assert world.encoded() == case


def test_the_entry_opens_no_database_connection(world, monkeypatch):
    import psycopg
    import psycopg_pool

    def refuse(*a, **k):
        raise AssertionError("the replay child opened a database connection")

    monkeypatch.setattr(psycopg, "connect", refuse)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(refuse))
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", refuse)
    case = world.encoded()
    out = deep_refine(case, ScriptConverse(deep_write=[PLAN_TURN, *simple_round()]),
                      checker=FakeChecker())
    assert "text" in out, out


def test_a_refused_call_is_an_error_output_not_a_crash(world):
    out = deep_refine(world.encoded(),
                      ScriptConverse(deep_write=[replay_child.ReplayCallError("refused")]),
                      checker=FakeChecker())
    assert out["case_id"] == "c1" and "refused" in out["error"] and "text" not in out


def test_a_refused_write_is_an_error_output_too(world):
    conv = ScriptConverse(deep_write=[PLAN_TURN,
                                      replay_child.ReplayCallError("ProviderUnavailableError")])
    out = deep_refine(world.encoded(), conv, checker=FakeChecker())
    assert out["case_id"] == "c1" and "ProviderUnavailableError" in out["error"]


def test_a_bad_case_is_an_error_output(world):
    out = deep_refine({"case_id": "c9"}, ScriptConverse(), checker=FakeChecker())
    assert out == {"case_id": "c9", "error": "KeyError: 'user_id'"}
    case = {**world.encoded(), "section_id": "s9"}
    out = deep_refine(case, ScriptConverse(), checker=FakeChecker())
    assert out["error"].startswith("LookupError")


def test_a_page_with_materials_is_not_encodable(world):
    class Excerpts:
        def for_nodes(self, user_id, node_ids, parents=None):
            return ["某一节资料"]

        def page_texts(self, user_id, source_id, pages):
            return {}

    with pytest.raises(CaseNotEncodable):
        encode_deep_case(None, world.repo, world.case, PACK, world.spec, excerpts=Excerpts())


def test_hook_steps_on_any_tier_go_to_the_parent_with_their_own_purpose(world):
    """An H9 delegate step on the fast tier: the child has one route (P10), so it asks the
    parent, purpose `hook.deep.before_write` (one of the entry's allowed purposes)."""
    step = {"at": "deep.before_write", "kind": "delegate", "tier": "fast", "tools": [],
            "instruction": "先读一下这一页的格子", "output_to": "context.notes",
            "max_tool_calls": 0}
    pack = HarnessPack(1, {"H9.deep.before_write": [step]})
    case = encode_deep_case(None, world.repo, world.case, pack, world.spec, excerpts=None,
                            now=NOW)
    conv = ScriptConverse(deep_write=[PLAN_TURN, *simple_round()],
                          **{"hook.deep.before_write": [{"text": "格子都还没学过。"}]})
    out = deep_refine(case, conv, checker=FakeChecker())
    assert "text" in out, out
    assert {r.purpose for r in conv.requests} <= replay_entries.PURPOSES["deep.refine"]
    assert "格子都还没学过。" in conv.of("deep.write")[0].messages[0].text


# ---- the tables -------------------------------------------------------------------------


def test_the_entries_are_registered_with_their_real_purposes():
    assert replay_entries.ENTRIES["deep.refine"] == "evowiki.harness.replay_cases:deep_refine"
    assert replay_entries.ENTRIES["assistant.turn"] == "evowiki.harness.replay_cases:assistant_turn"
    assert replay_entries.PURPOSES["deep.refine"] == replay_cases.DEEP_PURPOSES
    assert replay_entries.PURPOSES["assistant.turn"] == replay_cases.ASSISTANT_PURPOSES


def test_a_patch_can_never_change_the_case_codec():
    assert "禁止" in policy.violation("src/evowiki/harness/replay_cases.py", exists=True)


# ---- assistant.turn -----------------------------------------------------------------------


TOOLS = (ToolDef(name="read_page", description="读页面", parameters={"type": "object"}),
         ToolDef(name="propose_page_edit", description="提议改页面",
                 parameters={"type": "object"}))
PROPOSE = {"page_id": "p1", "kind": "add_section", "text": "加一段", "why": "例子不够"}
PROPOSED = "已向用户提出修改建议（prop_1），用户确认前不会改动任何内容。"


def assistant_case() -> dict:
    req = ChatRequest(purpose="assistant.turn", prompt_version="assistant@v13", subject="ses1",
                      system="你是陪读助手。", tools=TOOLS,
                      messages=(ChatMessage(role="user", text="z 分数怎么读？"),))
    return {"kind": "assistant.turn", "case_id": "assistant:ses1:msg1",
            "request": req.model_dump(mode="json"),
            "cassette": [
                {"name": "read_page", "args": {"page_id": "p1"},
                 "result": {"content": "页面 p1 的内容", "summary": "z 分数"}},
                {"name": "propose_page_edit", "args": PROPOSE, "result": {"content": PROPOSED}}]}


def tool_turn(name: str, i: int, **args) -> dict:
    return {"text": "", "tool_calls": [{"id": f"{name}-{i}", "name": name, "args": args}]}


def tool_texts(req: ChatRequest) -> list[str]:
    return [m.text for m in req.messages if m.role == "tool"]


def test_the_assistant_entry_answers_tools_from_the_cassette():
    conv = ScriptConverse(assistant_turn=[
        tool_turn("read_page", 1, page_id="p1"),
        {"text": "先看一个例子。", "tool_calls": [
            {"id": "propose_page_edit-2", "name": "propose_page_edit", "args": PROPOSE}]},
        {"text": "z=1 就是比平均高一个标准差。"}])
    out = assistant_turn(assistant_case(), conv)
    assert out == {"case_id": "assistant:ses1:msg1",
                   "text": "先看一个例子。\n\nz=1 就是比平均高一个标准差。"}
    reqs = conv.of("assistant.turn")
    assert tool_texts(reqs[1]) == ["页面 p1 的内容"]
    assert tool_texts(reqs[2])[-1] == PROPOSED                 # recorded; nothing applied
    assert reqs[0].system == "你是陪读助手。" and reqs[0].tools == TOOLS


def test_the_assistant_entry_does_not_score_an_unrecorded_read():
    conv = ScriptConverse(assistant_turn=[tool_turn("read_page", 1, page_id="p9"),
                                         {"text": "must not be judged"}])
    out = assistant_turn(assistant_case(), conv)
    assert "text" not in out and MISSING_RECORD in out["error"]
    assert len(conv.of("assistant.turn")) == 1


def test_the_assistant_entry_fails_closed():
    out = assistant_turn(assistant_case(),
                         ScriptConverse(assistant_turn=[replay_child.ReplayCallError("refused")]))
    assert out["case_id"] == "assistant:ses1:msg1" and "refused" in out["error"]
    bad = {**assistant_case(), "request": {**assistant_case()["request"], "purpose": "deep.write"}}
    assert "not an assistant turn" in assistant_turn(bad, ScriptConverse())["error"]


def test_encode_assistant_case_reads_the_first_request_and_the_tool_steps(monkeypatch):
    from evowiki.agent.tools import ToolResult
    from evowiki.agent.trace import model_payload, resolve_model, tool_payload
    from evowiki.store import trace as store_trace

    case = assistant_case()
    req = ChatRequest.model_validate(case["request"])
    payload, texts = model_payload(req, ChatTurn(text="x"), None)
    rows = [{"seq": 1, "kind": "model", "call_id": "c1", "ts": NOW,
             **resolve_model(payload, texts)},
            {"seq": 2, "kind": "tool", "call_id": None, "ts": NOW,
             **tool_payload("t1", "read_page", {"page_id": "p1"},
                            ToolResult(content="页面 p1 的内容"))}]
    monkeypatch.setattr(store_trace, "steps", lambda conn, run_id: rows)
    got = encode_assistant_case(object(), "assistant:ses1:msg1")
    assert ChatRequest.model_validate(got["request"]) == req
    assert got["cassette"] == [{"name": "read_page", "args": {"page_id": "p1"},
                                "result": ToolResult(content="页面 p1 的内容")
                                .model_dump(mode="json")}]
    monkeypatch.setattr(store_trace, "steps", lambda conn, run_id: rows[1:])
    with pytest.raises(LookupError):
        encode_assistant_case(object(), "r")


# ---- the parent's converse --------------------------------------------------------------


ACC = Account(user_id="u1", group="page")


def gateway(ds: DeepScript) -> Gateway:
    """The parent's deep tier over the script's providers (the plan via `complete`, writes via
    `chat`), metered and priced like the service's."""
    return Gateway(DEEP_CONFIG, ds.log, mode="record", providers=ds.providers(),
                   usage=UsageControl(NullMeter(), load_prices()), clock=lambda: NOW)


def relabelled(conv):
    """What `serve` does to each child request before the parent's converse sees it."""
    def send(req: ChatRequest):
        return conv(req.model_copy(update={
            "purpose": forge_replay.REPLAY_PURPOSE,
            "subject": f"fc_1:{req.purpose}:{req.subject}"[:forge_replay.SUBJECT_MAX]}))
    return send


def test_the_writes_reach_the_provider_through_the_chat_path(world):
    """Stage 4: no deep.plan JSON call any more — plan_page is the first write turn."""
    ds = DeepScript().plan().write(*simple_round())
    ref = world.in_process(ds)
    n = len(ds.requests)
    ds.plan().write(*simple_round())
    conv = parent_converse(gateway(ds), ACC)
    assert accounted(conv)                                      # run_replay accepts it
    out = deep_refine(world.encoded(), relabelled(conv), checker=FakeChecker())
    assert out.get("text") == ref.text and out["writer_provider"] == "anthropic", out
    mine, theirs = ds.requests[n:], ds.requests[:n]
    assert [r.purpose for r in mine] == ["deep.write"] * 3     # purpose restored
    assert mine[0] == theirs[0] and mine[0].subject == world.pid
    assert all(SCHEMA_PARAM not in r.params for r in mine)


def plain(subject: str, purpose: str = forge_replay.REPLAY_PURPOSE, **params) -> ChatRequest:
    return ChatRequest(purpose=purpose, prompt_version="v", subject=subject, system="s",
                       messages=(ChatMessage(role="user", text="写吧"),), params=params)


@pytest.mark.parametrize("req", [
    plain("garbage"), plain("fc_1:deep.write"), plain(":deep.write:x"), plain("fc_1::x"),
    plain("fc_1:deep.write:pg_1", purpose="deep.write"),       # not relabelled at all
    plain("fc_1:deep.write:" + "x" * 200),                     # cut at the cap: not restorable
])
def test_a_request_serve_did_not_relabel_is_refused(req):
    ds = DeepScript()
    with pytest.raises(ValueError):
        parent_converse(gateway(ds), ACC)(req)
    assert ds.requests == []


def test_restore_keeps_colons_in_the_subject():
    got = restore(plain("fc_1:hook.deep.before_write:a:b"))
    assert (got.purpose, got.subject) == ("hook.deep.before_write", "a:b")


def test_a_json_call_with_another_schema_or_purpose_is_refused():
    ds = DeepScript()
    conv = parent_converse(gateway(ds), ACC)
    with pytest.raises(ValueError):
        conv(plain("fc_1:deep.plan:pg_1", json_schema={"type": "object"}))
    with pytest.raises(ValueError):
        conv(plain("fc_1:deep.write:pg_1", json_schema=PlanAnswers.model_json_schema()))
    assert ds.requests == []


# ---- through the real child process -------------------------------------------------------


class SubprocessPipe:
    """`forge.sandbox.jobs.PipeProc` over a plain subprocess (no Job Object): enough to drive the
    real `replay_child` with `serve`."""

    def __init__(self, argv: list[str], *, cwd: Path, env: dict[str, str], err: Path) -> None:
        self._err = err.open("w", encoding="utf-8")
        self.p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self._err, text=True,
                                  encoding="utf-8")
        self.lines: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            for line in self.p.stdout:      # type: ignore[union-attr]
                self.lines.put(line)
            self.lines.put(None)
        threading.Thread(target=pump, daemon=True).start()

    def write_line(self, line: str, timeout: float) -> None:
        self.p.stdin.write(line + "\n")     # type: ignore[union-attr]
        self.p.stdin.flush()                # type: ignore[union-attr]

    def read_line(self, timeout: float, max_bytes: int) -> str | None:
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        return None if line is None else line.rstrip("\r\n")

    def wait(self, timeout: float) -> int | None:
        try:
            return self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait()
        self._err.close()


def test_one_deep_case_through_the_real_child(world, tmp_path):
    """The script writes no interactive, so the child's render checker (headless Chromium, as the
    service's) is never started: no browser needed."""
    ds = DeepScript().plan().write(*simple_round())
    ref = world.in_process(ds)
    env = {k: v for k, v in os.environ.items()
           if "DATABASE" not in k and not k.endswith(("_API_KEY", "_KEY"))}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    env["EVOWIKI_DATABASE_URL"] = "postgresql://127.0.0.1:1/nothing"
    proc = SubprocessPipe([sys.executable, "-m", "evowiki.forge.replay_child"], cwd=ROOT,
                          env=env, err=tmp_path / "child.err")
    n = len(ds.requests)
    ds.plan().write(*simple_round())
    try:
        r = forge_replay.serve(proc, entry="deep.refine", cases=[world.encoded()],
                               converse=parent_converse(gateway(ds), ACC), candidate_id="fc_1",
                               max_calls=10, wall_s=180.0)
    finally:
        proc.kill()
    assert r.ok, (r.reason, (tmp_path / "child.err").read_text(encoding="utf-8")[-3000:])
    (out,) = r.outputs
    assert out["text"] == ref.text and out["writer_provider"] == "anthropic", out
    assert out["cost"] is None and r.calls == 3                # plan_page + two write turns
    assert r.cost_usd and r.cost_usd > 0                       # priced by the parent's gateway
    mine, theirs = ds.requests[n:], ds.requests[:n]
    assert [(type(x).__name__, x.purpose) for x in mine] == [("ChatRequest", "deep.write")] * 3
    assert mine[0] == theirs[0] and mine[1] == theirs[1]       # the very requests sent in process


# ---- the parent side on Postgres ----------------------------------------------------------


def test_a_postgres_case_replays_like_the_in_process_replay(pool):
    """`encode_deep_case` on a PgRepo reads the stored logs up to the snapshot seqs on the
    caller's connection; the child folds them in a MemoryRepo to the same
    request and text as the in-process replay on Postgres."""
    from evowiki.deep.store import PgAssetStore, PgJobStore
    from evowiki.wikiapp.repo import PgRepo
    from tests.runtime.helpers import make_user

    make_user(pool, "u1")
    repo, ids = PgRepo(pool), Ids()
    pid = organized((repo, Script(), ids))
    now = repo.load("u1")
    case = Case(case_id="c1", run_id="r1", page_id=pid, section_id="s2", signal_ids=(),
                anchor=False, snapshot=SnapshotSeqs(now.wiki_seq, now.aspect_seq,
                                                    now.infer_version),
                mode="rewrite", instruction="", user_id="u1")
    spec = WikiSpecSource(repo).spec(now)
    ds = DeepScript().plan().write(*simple_round())
    svc = DeepService(repo=repo, jobs=PgJobStore(pool), assets=PgAssetStore(pool),
                      specs=WikiSpecSource(repo), checker=FakeChecker(), llm=ds.llm(),
                      clock=lambda: NOW, new_id=ids, library=library())
    ref = replay(case, PackDiff(), base_pack=strip_h10(PACK), base_spec=spec,
                 deps=ReplayDeps(deep=svc, repo=repo))
    with pool.connection() as conn:
        encoded = encode_deep_case(conn, repo, case, PACK, spec, excerpts=None,
                                   library=library(), now=NOW)
    assert encoded == json.loads(json.dumps(encoded))
    conv = ScriptConverse(deep_write=[PLAN_TURN, *simple_round()])
    out = deep_refine(encoded, conv, checker=FakeChecker())
    assert out.get("text") == ref.text, out
    (mine,), (theirs,) = conv.of("deep.write")[:1], writes(ds)[:1]
    assert mine.system == theirs.system and mine.messages == theirs.messages
