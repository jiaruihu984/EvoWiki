"""Assistant-turn replays (harness 3b follow-up 3, R1–R3): a turn recorded by the real assistant
loop into the test database's trace, answered again under a candidate pack on a scripted gateway.
Fakes only: no paid model, no network."""

import http.client
import socket
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evowiki.agent.tools import (
    ActionCard,
    ActionItem,
    ProposalDraft,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from evowiki.agent.trace import REPLAY_RUN_PREFIX, REPLAY_TRIGGER
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import READ_STUB, REFUSED, SYSTEM, run_turn
from evowiki.deep.agent import SnapshotSeqs
from evowiki.harness import replay_assistant as ra
from evowiki.harness.diff import KeyOp, PackDiff
from evowiki.harness.pack import EMPTY, HarnessInvalid, HarnessPack
from evowiki.harness.replay import Case
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import USER_VERSION, Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.prompt_steps import apply_marks
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage_control import UsageControl
from evowiki.sources.config import fence
from evowiki.store import harness as store_harness
from evowiki.store.db import fetch_all, fetch_one
from evowiki.store.llm_logs import PgCallLog
from evowiki.store.trace import PgTraceSink
from evowiki.wikiapp.refine_service import ReplayFailed
from tests.assistant.fakes import INFO, Data, FakeTool, Ids
from tests.assistant.fakes import T0 as TURN_T0
from tests.runtime.helpers import make_user

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
OTHER = ModelRef(provider="scripted", model="other")
OTHER_CFG = GatewayConfig(generator=OTHER, verifier=OTHER, allow_single_provider=True)
FREE = PriceTable({})
PRICES = PriceTable({"scripted:m": Price(input_per_mtok=1.0, output_per_mtok=2.0)})
OLD_GUIDANCE = "旧的写法：先答，再问一个小问题。"
NEW_GUIDANCE = "新的写法：先给一个例子，再讲道理。"
LONG = "维基：z 分数是用标准差做单位量出的距离。" * 20        # a long read result (> 100 chars)
CALLS = {"propose_page_edit": {"page_id": "p1", "heading": "补充"},
         "split_page": {"page": "p1"},
         "web_search": {"query": "z 分数"},
         "fetch_url": {"url": "https://w/z"}}


class _Meter:
    def check(self, account):
        pass

    def record(self, account, purpose, usage, *, replayed):
        pass


def gateway(tmp_path, name, script, *, prices=FREE, log=None,
            tiers=None) -> tuple[Gateway, list]:
    outs = iter(script)
    sent: list = []

    def chat(model, req):
        sent.append(req)
        return next(outs)

    prov = ScriptedChatProvider("scripted", chat)
    gw = Gateway(CFG, log if log is not None else CallLog(tmp_path / f"{name}.jsonl"),
                 mode="record",
                 providers={"scripted": prov},
                 usage=UsageControl(meter=_Meter(), prices=prices, tier_configs=tiers))
    return gw, sent


class ActionFake:
    """A ledger action tool (acts): its result carries the card the ledger row would show."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.spec = ToolSpec(name="split_page", description="split_page 工具",
                             parameters={"type": "object", "properties": {}}, effect="propose",
                             category="action", acts=True)

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        self.calls.append(args)
        return ToolResult(content="已生成草稿，等用户确认", action=ActionCard(
            action_id="act1", kind="split_page", title="拆分「z 分数」",
            items=(ActionItem(key="p1a", title="z 分数怎么算"),), confirm=True))


def recording_tools():
    return [FakeTool("read_k", "read", ToolResult(content="K：z_score 不会")),
            FakeTool("web_search", "network", ToolResult(content=LONG)),
            FakeTool("fetch_url", "network", ToolResult(content="网页正文")),
            FakeTool("propose_page_edit", "propose", ToolResult(
                content="已生成", proposal=ProposalDraft(kind="add_section", page_id="p1",
                                                      heading="补充", text="补一个例子",
                                                      why="刚问过"))),
            ActionFake()]


def tc(name, i, **args):
    return ToolCall(id=f"c{i}", name=name, args=args or CALLS.get(name, {}))


def answer(text):
    return ChatTurn(text=text, input_tokens=1000, output_tokens=500)


def all_four(start=1):
    return ChatTurn(tool_calls=tuple(tc(n, start + i) for i, n in enumerate(CALLS)),
                    input_tokens=1000, output_tokens=500)


@pytest.fixture
def world(pool, tmp_path, every_skill):
    """User u1 with H1.assistant set (pack version 1) and one traced turn of the real loop that
    called the four side-effect tools, then read_k, then answered."""
    make_user(pool, "u1")
    with pool.connection() as conn:
        store_harness.set_value(conn, "u1", "H1.assistant", OLD_GUIDANCE, source="user",
                                why="t", now=TURN_T0)
        pack = store_harness.load_pack(conn, "u1")
    assert pack.version == 1
    gw, _ = gateway(tmp_path, "rec", [all_four(), ChatTurn(tool_calls=(tc("read_k", 5),)),
                                      answer("原来的回答")], log=PgCallLog(pool))
    from evowiki.assistant.loop import TurnDeps
    from evowiki.assistant.session import MemorySessionStore
    store = MemorySessionStore()
    store.create(INFO)
    deps = TurnDeps(gateway=gw.with_harness(lambda uid: pack), tools=recording_tools(),
                    store=store, data=Data(), new_id=Ids(),
                    clock=lambda: TURN_T0,
                    trace=PgTraceSink(pool, clock=lambda: TURN_T0))
    events = list(run_turn(deps, INFO, MessageIn(text="z 分数怎么理解？")))
    assert events[-1].type == "done"
    mid = next(e for e in store.entries("s1") if e.type == "user").payload["message_id"]
    return {"pool": pool, "run_id": f"assistant:s1:{mid}", "pack": pack, "tmp": tmp_path}


def case(world) -> Case:
    return Case(case_id="c1", run_id=world["run_id"], page_id="", section_id="", signal_ids=(),
                anchor=False, snapshot=SnapshotSeqs(0, 0, 0), mode="rewrite", instruction="",
                user_id="u1")


def replay(world, diff, script, *, prices=FREE, trace=None, pack_at=None, name="rp",
           tiers=None):
    gw, sent = gateway(world["tmp"], name, script, prices=prices, tiers=tiers)
    deps = ra.pg_deps(world["pool"], gw, trace=trace)
    if pack_at is not None:
        deps = ra.AssistantReplayDeps(gateway=gw, turn=deps.turn, pack_at=pack_at, trace=trace)
    out = ra.replay_turn(case(world), diff, base_pack=world["pack"], deps=deps)
    return out, sent


def traced(world) -> ra.TracedTurn:
    with world["pool"].connection() as conn:
        return ra.load_turn(conn, world["run_id"])


# ---- H1 ---------------------------------------------------------------------------------------

def test_the_traced_system_starts_with_system_under_the_recorded_guidance(world):
    t = traced(world)
    head, applied = apply_marks(SYSTEM, {"assistant": OLD_GUIDANCE})
    assert applied and t.request.system.startswith(head) and OLD_GUIDANCE in head
    assert t.pack_version == 1 and t.request.prompt_version.endswith(USER_VERSION)


def test_the_reference_replay_sends_the_traced_system_unchanged(world):
    _, sent = replay(world, PackDiff(), [answer("参考回答")])
    t = traced(world)
    assert sent[0].system == t.request.system
    assert sent[0].prompt_version == t.request.prompt_version
    assert sent[0].messages == t.request.messages


def test_a_candidate_h1_replaces_only_the_system_head(world):
    out, sent = replay(world, PackDiff((KeyOp("H1.assistant", NEW_GUIDANCE),)),
                       [answer("新回答")])
    t = traced(world)
    old_head, _ = apply_marks(SYSTEM, {"assistant": OLD_GUIDANCE})
    new_head, _ = apply_marks(SYSTEM, {"assistant": NEW_GUIDANCE})
    assert sent[0].system == new_head + t.request.system[len(old_head):]
    assert OLD_GUIDANCE not in sent[0].system and NEW_GUIDANCE in sent[0].system
    assert sent[0].prompt_version == t.request.prompt_version          # still a user override
    assert out.text == "新回答"


def test_a_candidate_back_to_the_default_guidance_drops_the_user_version(world):
    _, sent = replay(world, PackDiff((KeyOp("H1.assistant", None),)), [answer("默认")])
    assert sent[0].system.startswith(apply_marks(SYSTEM, {})[0])
    assert not sent[0].prompt_version.endswith(USER_VERSION)


def test_a_system_that_does_not_start_with_the_recorded_guidance_is_unusable(world):
    # the pack the run recorded is not the one the trace was sent with (here: the empty pack)
    with pytest.raises(ra.ReplaySourceMissing) as e:
        replay(world, PackDiff(), [answer("x")], pack_at=lambda uid, v: EMPTY)
    assert e.value.reason == "这次回答的系统提示对不上，不能重放"


def test_a_missing_run_is_unusable(world):
    world = {**world, "run_id": "assistant:s1:nope"}
    with pytest.raises(ra.ReplaySourceMissing):
        replay(world, PackDiff(), [answer("x")])


# ---- H2 / H3 ----------------------------------------------------------------------------------

def test_tool_texts_and_schemas_are_recorded_plus_the_candidate_h2(world):
    diff = PackDiff((KeyOp("H2.assistant.web_search", "新的搜索说明"),))
    _, sent = replay(world, diff, [answer("x")])
    tools = {d.name: d for d in sent[0].tools}
    recorded = {d.name: d for d in traced(world).request.tools}
    assert [d.name for d in sent[0].tools] == [d.name for d in traced(world).request.tools]
    assert tools["web_search"].description == "新的搜索说明"
    assert tools["web_search"].parameters == recorded["web_search"].parameters
    assert tools["read_k"].description == recorded["read_k"].description
    assert tools["fetch_url"].parameters == recorded["fetch_url"].parameters


def test_a_switched_off_tool_is_not_offered_and_cannot_run(world):
    diff = PackDiff((KeyOp("H3.assistant.web_search", False),))
    _, sent = replay(world, diff, [ChatTurn(tool_calls=(tc("web_search", 1),)), answer("答")])
    assert "web_search" not in {d.name for d in sent[0].tools}
    tool_msg = next(m for m in sent[1].messages if m.role == "tool")
    assert tool_msg.is_error and "没有这个工具" in tool_msg.text


# ---- H4 / H5 ----------------------------------------------------------------------------------

def test_the_candidate_tool_cap_applies(world):
    diff = PackDiff((KeyOp("H4.assistant.max_tool_calls", 1),))
    _, sent = replay(world, diff, [all_four(), answer("答")])
    tools = [m for m in sent[1].messages if m.role == "tool"]
    assert len(tools) == 4 and not tools[0].is_error
    assert all(m.text == REFUSED for m in tools[1:])
    assert sent[1].tool_choice == "none"


def test_the_reference_keeps_the_default_cap(world):
    _, sent = replay(world, PackDiff(), [all_four(), answer("答")])
    assert all(m.text != REFUSED for m in sent[1].messages if m.role == "tool")
    assert sent[1].tool_choice == "auto"


def _three_reads():
    return [ChatTurn(tool_calls=(tc("web_search", 1),)), ChatTurn(tool_calls=(tc("read_k", 2),)),
            ChatTurn(tool_calls=(tc("read_k", 3),)), answer("答")]


def test_the_candidate_elide_policy_applies(world):
    diff = PackDiff((KeyOp("H5.assistant.read_keep_calls", 1),
                     KeyOp("H5.assistant.read_stub_over", 100)))
    _, sent = replay(world, diff, _three_reads())
    search = [m for m in sent[2].messages if m.role == "tool" and m.tool_call_id == "c1"]
    assert search[0].text == READ_STUB


def test_the_reference_elide_policy_keeps_the_read(world):
    _, sent = replay(world, PackDiff(), _three_reads())
    search = [m for m in sent[2].messages if m.role == "tool" and m.tool_call_id == "c1"]
    assert search[0].text == fence(LONG)          # the assistant fences what a read brings back


# ---- unmeasurable candidates --------------------------------------------------------------------

@pytest.mark.parametrize("op", [KeyOp("H5.assistant.top_k_pages", 3), KeyOp("H6.keep_turns", 4),
                                KeyOp("H10.assistant.turn.tier", "good"),
                                KeyOp("H9.assistant.before_answer", None)])
def test_an_unmeasurable_candidate_is_never_replayed(world, op):
    with pytest.raises(HarnessInvalid, match="重放测不出"):
        replay(world, PackDiff((op,)), [answer("x")])


# ---- result -----------------------------------------------------------------------------------

def test_writer_provider_cost_run_id_and_text(world):
    out, _ = replay(world, PackDiff(), [all_four(), answer("新的回答")], prices=PRICES)
    assert out.case_id == "c1" and out.writer_provider == "scripted"
    assert out.cost == pytest.approx(2 * (1000 * 1.0 + 500 * 2.0) / 1e6)
    assert out.run_id.startswith(REPLAY_RUN_PREFIX) and out.run_id.endswith(world["run_id"])
    assert out.text.startswith("新的回答")
    assert "【模拟修改建议】补充（刚问过）\n补一个例子" in out.text
    assert "【模拟动作】拆分「z 分数」：z 分数怎么算" in out.text


def test_the_replay_is_traced_as_a_replay_run(world):
    out, _ = replay(world, PackDiff(), [answer("答")],
                    trace=PgTraceSink(world["pool"], clock=lambda: TURN_T0))
    with world["pool"].connection() as conn:
        row = fetch_one(conn, "select agent, input, pack_version, stop from agent_run "
                              "where run_id = %s", (out.run_id,))
    assert row["agent"] == "assistant.turn" and row["stop"] == "answered"
    assert row["input"]["job"]["trigger"] == REPLAY_TRIGGER
    assert row["input"]["source_run"] == world["run_id"]


def test_each_replay_draws_its_own_sample(world):
    a, _ = replay(world, PackDiff(), [answer("一")], name="same")
    b, _ = replay(world, PackDiff(), [answer("二")], name="same")      # same call log
    assert (a.text, b.text) == ("一", "二") and a.run_id != b.run_id


# ---- the sandbox (R2) -------------------------------------------------------------------------

def _snapshot(pool) -> dict[str, tuple[int, str | None]]:
    """Every public table's row count and a hash of its whole content (an UPDATE or a delete
    paired with an insert changes the hash, not the count)."""
    with pool.connection() as conn:
        tables = [r["table_name"] for r in fetch_all(
            conn, "select table_name from information_schema.tables "
                  "where table_schema = 'public' and table_type = 'BASE TABLE'")]
        out = {}
        for t in tables:
            r = fetch_one(conn, f'select count(*) as n, md5(string_agg(x::text, \'\' '
                                f'order by x::text)) as h from "{t}" x')
            out[t] = (r["n"], r["h"])
        return out


def test_a_replay_writes_nothing_and_reaches_no_network(world, monkeypatch):
    before = _snapshot(world["pool"])
    assert before["agent_run"][0] >= 1 and before["harness_version"][0] == 1
    assert before["llm_call"][0] == 3                    # the recorded turn's calls

    used: list[str] = []

    def no_network(what):
        def boom(*a: Any, **k: Any):
            used.append(what)
            raise AssertionError(f"a replay reached the network: {what}")
        return boom

    # every client path (urllib, http.client, httpx) opens its socket through these
    monkeypatch.setattr(socket, "create_connection", no_network("socket"))
    monkeypatch.setattr(socket, "getaddrinfo", no_network("dns"))
    monkeypatch.setattr(http.client.HTTPConnection, "connect", no_network("http"))
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", no_network("urllib"))
    # nothing of the recording turn's tools may run either
    for cls in (FakeTool, ActionFake):
        monkeypatch.setattr(cls, "run", no_network(f"{cls.__name__}.run"))

    # a pack that switches tools: read_k off, web_search on with new texts
    diff = PackDiff((KeyOp("H3.assistant.read_k", False),
                     KeyOp("H2.assistant.web_search", "联网搜一下"),
                     KeyOp("H4.assistant.max_tool_calls", 8)))
    out, sent = replay(world, diff, [all_four(), ChatTurn(tool_calls=(tc("read_k", 5),)),
                                     answer("沙盒里的回答")])
    assert "read_k" not in {d.name for d in sent[0].tools}
    results = {m.tool_call_id: m for m in sent[1].messages if m.role == "tool"}
    assert results["c1"].text.startswith("已向用户提出修改建议")     # the recorded result
    # the cassette holds what the model was given (stage 3 adds the offer line to a draft card)
    assert results["c2"].text.startswith("已生成草稿，等用户确认")
    assert results["c3"].text == fence(LONG) and results["c4"].text == fence("网页正文")
    assert "【模拟修改建议】" in out.text and "【模拟动作】" in out.text
    assert used == []
    assert _snapshot(world["pool"]) == before


# ---- pack history -----------------------------------------------------------------------------

def test_pack_at_rebuilds_each_past_version(pool):
    make_user(pool, "u2")
    t = datetime(2026, 10, 22, tzinfo=UTC)
    with pool.connection() as conn:
        store_harness.set_value(conn, "u2", "H1.assistant", "一", source="user", why="t", now=t)
        store_harness.set_value(conn, "u2", "H4.assistant.max_tool_calls", 4, source="improver",
                                why="t", now=t)
        store_harness.set_value(conn, "u2", "H1.assistant", None, source="rollback", why="t",
                                now=t)
        got = [store_harness.pack_at(conn, "u2", v) for v in range(4)]
        assert got[3] == store_harness.load_pack(conn, "u2")
        with pytest.raises(LookupError):
            store_harness.pack_at(conn, "u2", 4)
    assert [g.values for g in got] == [
        {}, {"H1.assistant": "一"}, {"H1.assistant": "一", "H4.assistant.max_tool_calls": 4},
        {"H4.assistant.max_tool_calls": 4}]
    assert got[0] == HarnessPack(0, {})


# ---- the model (spec §4.1) ---------------------------------------------------------------------

def test_the_traced_turn_names_its_model(world):
    assert traced(world).models == (REF,)


def test_a_replay_routed_to_another_model_is_unusable_before_any_call(world):
    tiers = {"fast": OTHER_CFG, "good": OTHER_CFG}
    with pytest.raises(ra.ReplaySourceMissing) as e:
        replay(world, PackDiff(), [], tiers=tiers)            # an empty script: no call allowed
    assert e.value.reason == "重放用的模型和那次回答不一样，不能比"
    assert "scripted" not in e.value.reason


def test_the_replay_pins_the_tier_that_reaches_the_traced_model(world):
    out, sent = replay(world, PackDiff(), [answer("答")], tiers={"fast": CFG, "good": OTHER_CFG})
    assert out.text == "答" and len(sent) == 1


def test_a_turn_without_a_known_model_is_unusable(world):
    t = traced(world)
    gw, _ = gateway(world["tmp"], "nm", [])
    deps = ra.AssistantReplayDeps(gateway=gw, turn=lambda rid: ra.TracedTurn(
        t.run_id, t.user_id, t.pack_version, t.request, t.cassette, ()),
        pack_at=ra.pg_deps(world["pool"], gw).pack_at)
    with pytest.raises(ra.ReplaySourceMissing, match="找不到这次回答用的模型"):
        ra.replay_turn(case(world), PackDiff(), base_pack=world["pack"], deps=deps)
    deps = ra.AssistantReplayDeps(gateway=gw, turn=lambda rid: ra.TracedTurn(
        t.run_id, t.user_id, t.pack_version, t.request, t.cassette, (REF, OTHER)),
        pack_at=deps.pack_at)
    with pytest.raises(ra.ReplaySourceMissing, match="中途换过模型"):
        ra.replay_turn(case(world), PackDiff(), base_pack=world["pack"], deps=deps)


def test_a_replay_call_answered_by_another_model_fails_with_its_spend(world, monkeypatch):
    real = Gateway.converse

    def fallback(self, role, request, **kw):
        turn, call = real(self, role, request, **kw)
        return turn, call.model_copy(update={"model": "other"})   # as if a fallback answered

    monkeypatch.setattr(Gateway, "converse", fallback)
    with pytest.raises(ReplayFailed) as e:
        replay(world, PackDiff(), [answer("答")], prices=PRICES)
    assert e.value.failure == "model_mismatch"
    assert e.value.cost == pytest.approx((1000 * 1.0 + 500 * 2.0) / 1e6)


# ---- Skill turns (stage 3; the user's ruling: each request rebuilt from its own tools) ----

def _skill_script():
    return [ChatTurn(tool_calls=(tc("read_ref", 1, path="edit-wiki/path"),)),
            ChatTurn(tool_calls=(tc("split_page", 2),)), answer("草稿在卡片上")]


@pytest.fixture
def skill_world(pool, tmp_path):
    """A traced turn that loads the edit-wiki/path reference mid-turn, then calls split_page."""
    make_user(pool, "u1")
    with pool.connection() as conn:
        store_harness.set_value(conn, "u1", "H1.assistant", OLD_GUIDANCE, source="user",
                                why="t", now=TURN_T0)
        pack = store_harness.load_pack(conn, "u1")
    gw, _ = gateway(tmp_path, "rec", _skill_script(), log=PgCallLog(pool))
    from evowiki.assistant.loop import TurnDeps
    from evowiki.assistant.session import MemorySessionStore
    store = MemorySessionStore()
    store.create(INFO)
    deps = TurnDeps(gateway=gw.with_harness(lambda uid: pack), tools=recording_tools(),
                    store=store, data=Data(), new_id=Ids(),
                    clock=lambda: TURN_T0, trace=PgTraceSink(pool, clock=lambda: TURN_T0))
    assert list(run_turn(deps, INFO, MessageIn(text="把这页拆开")))[-1].type == "done"
    mid = next(e for e in store.entries("s1") if e.type == "user").payload["message_id"]
    return {"pool": pool, "run_id": f"assistant:s1:{mid}", "pack": pack, "tmp": tmp_path}


def test_a_skill_turn_replays_with_each_requests_own_tools(skill_world):
    t = traced(skill_world)
    assert len(t.tool_lists) == 3 and "split_page" not in [d.name for d in t.tool_lists[0]]
    out, sent = replay(skill_world, PackDiff(), _skill_script())
    names = [[d.name for d in r.tools] for r in sent]
    assert names == [[d.name for d in ts] for ts in t.tool_lists]     # request k = traced k
    assert "split_page" not in names[0] and "split_page" in names[1]
    split = [m for m in sent[2].messages if m.role == "tool" and m.tool_call_id == "c2"]
    assert split and not split[0].is_error                 # loaded mid-turn: callable in replay
    assert "拆分「z 分数」" in out.text


def test_an_unrecorded_read_is_unavailable_without_reading_current_state(world):
    gw, sent = gateway(world["tmp"], "missing", [
        ChatTurn(tool_calls=(tc("read_k", 1, other="x"),)), answer("不能评分")])
    with pytest.raises(ReplayFailed) as error:
        ra.replay_turn(case(world), PackDiff(), base_pack=world["pack"],
                       deps=ra.pg_deps(world["pool"], gw))
    assert error.value.failure == "snapshot_unavailable"
    assert len(sent) == 1


def test_a_changed_skill_read_cannot_load_todays_skill(skill_world):
    script = [ChatTurn(tool_calls=(tc("load_skill", 1, skills=["plan-goal"]),)),
              answer("不能评分")]
    with pytest.raises(ReplayFailed) as error:
        replay(skill_world, PackDiff(), script)
    assert error.value.failure == "snapshot_unavailable"


def test_a_replay_ends_on_a_question_and_the_text_shows_it(pool, tmp_path):
    from evowiki.assistant.loop import TurnDeps
    from evowiki.assistant.session import MemorySessionStore
    from evowiki.assistant.tools.ask_user import AskUserTool
    make_user(pool, "u1")
    ask = [ChatTurn(tool_calls=(tc("ask_user", 1, question="你想用它做什么？"),))]
    gw, _ = gateway(tmp_path, "rec", ask, log=PgCallLog(pool))
    store = MemorySessionStore()
    store.create(INFO)
    deps = TurnDeps(gateway=gw.with_harness(lambda uid: EMPTY), tools=[*recording_tools(), AskUserTool()],
                    store=store, data=Data(), new_id=Ids(), clock=lambda: TURN_T0,
                    trace=PgTraceSink(pool, clock=lambda: TURN_T0))
    list(run_turn(deps, INFO, MessageIn(text="我想学 Rust")))
    mid = next(e for e in store.entries("s1") if e.type == "user").payload["message_id"]
    world = {"pool": pool, "run_id": f"assistant:s1:{mid}", "pack": EMPTY, "tmp": tmp_path}
    out, _ = replay(world, PackDiff(), ask)
    assert "【问他】你想用它做什么？" in out.text


def test_changed_write_arguments_cannot_reuse_a_different_recording(skill_world):
    script = [ChatTurn(tool_calls=(tc("read_ref", 1, path="edit-wiki/path"),)),
              ChatTurn(tool_calls=(tc("split_page", 2, page="别的页"),)), answer("不能评分")]
    with pytest.raises(ReplayFailed) as error:
        replay(skill_world, PackDiff(), script)
    assert error.value.failure == "snapshot_unavailable"


def test_the_s3_tools_are_built_in_so_an_h2_candidate_on_them_replays(skill_world):
    assert {"load_skill", "read_ref", "offer", "confirm"} <= set(ra.builtin_specs())
    diff = PackDiff((KeyOp("H2.assistant.load_skill", "载入一个技能，拿到它的做法"),))
    _, sent = replay(skill_world, diff, _skill_script())
    [load] = [d for d in sent[0].tools if d.name == "load_skill"]
    assert load.description == "载入一个技能，拿到它的做法"


def test_a_replay_rejects_future_and_heldout_sources_before_calling_the_model(world):
    gw, sent = gateway(world["tmp"], "boundary", [answer("must not be called")])
    deps = ra.pg_deps(world["pool"], gw)
    turn = traced(world)
    for restricted in (
        replace(deps, cutoff=turn.recorded_at - timedelta(microseconds=1)),
        replace(deps, excluded_sources=frozenset({"turn:s1/msg_001"})),
    ):
        with pytest.raises(ra.ReplaySourceMissing):
            ra.replay_turn(case(world), PackDiff(), base_pack=world["pack"], deps=restricted)
    assert not sent


def test_current_forgetting_vetoes_a_historical_snapshot(world, monkeypatch):
    from evowiki.store import bot
    monkeypatch.setattr(bot, "forgotten_words", lambda conn, uid: ("forgotten",))
    gw, sent = gateway(world["tmp"], "withdrawn", [answer("must not be called")])
    with pytest.raises(ra.ReplaySourceMissing, match=ra.SNAPSHOT_WITHDRAWN):
        ra.replay_turn(case(world), PackDiff(), base_pack=world["pack"],
                       deps=ra.pg_deps(world["pool"], gw))
    assert not sent
