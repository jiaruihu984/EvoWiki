"""Anthropic prompt caching: cache_control breakpoints in AnthropicProvider.chat and cache-aware
cost. Offline only: a fake SDK client records the kwargs."""
import copy
from types import SimpleNamespace as NS

import pytest

from evowiki.llm import providers
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn, ToolCall, ToolDef, chat_input_hash
from evowiki.llm.gateway import ProviderOutputError
from evowiki.llm.models import ModelRef, ProviderResponse
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.providers import (
    AnthropicProvider,
    ProviderEmptyOutputError,
    ProviderRefusalError,
    anthropic_cost,
    anthropic_messages,
)

MARK = {"type": "ephemeral"}                    # 5 minutes: the moving tail
MARK_1H = {"type": "ephemeral", "ttl": "1h"}    # the prefixes shared across requests
T1 = ToolDef(name="read", description="Read.", parameters={"type": "object", "properties": {}})
T2 = ToolDef(name="write", description="Write.", parameters={"type": "object", "properties": {}})
PROMPT = "Write page X. " * 50
ECHO = [{"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "I read first."},
        {"type": "tool_use", "id": "c1", "name": "read", "input": {"k": 1}},
        {"type": "tool_use", "id": "c2", "name": "read", "input": {"k": 2}}]
ECHO2 = [{"type": "thinking", "thinking": "", "signature": "sig2"},
         {"type": "tool_use", "id": "c3", "name": "write", "input": {"md": "x"}}]


def _req(messages, tools=(T1, T2), system="SYS"):
    return ChatRequest(purpose="deep.write", prompt_version="v1", subject="s", system=system,
                       messages=tuple(messages), tools=tools)


TURN1 = [ChatMessage(role="user", text=PROMPT)]
TURN3 = [*TURN1,
         ChatMessage(role="assistant", tool_calls=(ToolCall(id="c1", name="read", args={"k": 1}),
                                                   ToolCall(id="c2", name="read", args={"k": 2})),
                     echo=ECHO),
         ChatMessage(role="tool", tool_call_id="c1", text="r1"),
         ChatMessage(role="tool", tool_call_id="c2", text="r2"),
         ChatMessage(role="assistant", tool_calls=(ToolCall(id="c3", name="write",
                                                            args={"md": "x"}),), echo=ECHO2),
         ChatMessage(role="tool", tool_call_id="c3", text="ok", is_error=True)]


class _AN:
    def __init__(self, content=None, stop="end_turn", usage=None, model="claude-sonnet-5-5"):
        self.kwargs = None
        self._resp = NS(model=model, stop_reason=stop,
                        content=content if content is not None else [NS(type="text", text="好")],
                        usage=usage if usage is not None else NS(
                            input_tokens=1000, output_tokens=500,
                            cache_read_input_tokens=50000, cache_creation_input_tokens=10000))
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.kwargs = kw
        return self._resp


def _call(req, **an):
    c = _AN(**an)
    out = AnthropicProvider(client=c).chat("claude-sonnet-5-5", req)
    return c.kwargs, out


def _markers(kw):
    """Paths of every block carrying cache_control."""
    found = []
    for i, t in enumerate(kw.get("tools", [])):
        if "cache_control" in t:
            found.append(("tools", i))
    if isinstance(kw["system"], list):
        found += [("system", i) for i, b in enumerate(kw["system"]) if "cache_control" in b]
    for i, m in enumerate(kw["messages"]):
        if isinstance(m["content"], list):
            found += [("messages", i, j) for j, b in enumerate(m["content"])
                      if isinstance(b, dict) and "cache_control" in b]
    return found


def _ttls(kw):
    """The TTL of every breakpoint in render order (tools → system → messages)."""
    blocks = list(kw.get("tools", []))
    blocks += kw["system"] if isinstance(kw["system"], list) else []
    for m in kw["messages"]:
        blocks += m["content"] if isinstance(m["content"], list) else []
    return [b["cache_control"].get("ttl", "5m") for b in blocks
            if isinstance(b, dict) and "cache_control" in b]


def _ttl_order_ok(kw):
    """At most 4 breakpoints, and every 1-hour one before every 5-minute one (an API rule)."""
    ttls = _ttls(kw)
    return len(ttls) <= 4 and ttls == sorted(ttls, key=lambda t: t != "1h")


def _strip(kw):
    """The kwargs with cache markers removed and marked block lists collapsed to strings (a
    first user message split at its stable prefix is joined back)."""
    kw = copy.deepcopy(kw)
    for t in kw.get("tools", []):
        t.pop("cache_control", None)
    if isinstance(kw["system"], list):
        assert len(kw["system"]) == 1
        kw["system"] = kw["system"][0]["text"]
    for m in kw["messages"]:
        if isinstance(m["content"], list):
            had = [isinstance(b, dict) and b.pop("cache_control", None) is not None
                   for b in m["content"]]
            if (any(had) and m["role"] == "user"
                    and all(b["type"] == "text" for b in m["content"])):
                m["content"] = "".join(b["text"] for b in m["content"])
    return kw


def _old_kwargs(req):
    """Exactly what chat() sent before prompt caching."""
    out = {"model": "claude-sonnet-5-5", "max_tokens": 16000, "system": req.system,
           "messages": anthropic_messages(req), "output_config": {"effort": "medium"},
           "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    if req.tools:
        out["tools"] = [{"name": t.name, "description": t.description,
                         "input_schema": t.parameters} for t in req.tools]
        out["tool_choice"] = {"type": req.tool_choice}
    return out


SHAPES = [pytest.param(TURN1, (T1, T2), "SYS", id="turn1"),
          pytest.param(TURN3, (T1, T2), "SYS", id="turn3-tool-results"),
          pytest.param(TURN1, (), "SYS", id="turn1-no-tools"),
          pytest.param(TURN3, (), "SYS", id="turn3-no-tools"),
          pytest.param(TURN1, (T1,), "", id="turn1-empty-system"),
          pytest.param(TURN3, (T1, T2), "", id="turn3-empty-system")]


@pytest.mark.parametrize("messages, tools, system", SHAPES)
def test_at_most_four_breakpoints_and_kwargs_otherwise_unchanged(messages, tools, system):
    req = _req(messages, tools, system)
    kw, _ = _call(req)
    assert _ttl_order_ok(kw)
    # the last tool is marked (1 hour) only when there is no system block to cache it with
    assert all(b.get("cache_control") == (None if system else MARK_1H)
               for b in (kw.get("tools") or [])[-1:])
    assert _strip(kw) == _old_kwargs(req)


def test_turn1_marks_system_1h_and_the_prompt_once():
    kw, _ = _call(_req(TURN1))
    assert _markers(kw) == [("system", 0), ("messages", 0, 0)]
    assert kw["system"] == [{"type": "text", "text": "SYS", "cache_control": MARK_1H}]
    assert kw["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": PROMPT, "cache_control": MARK}]}]
    assert kw["tools"] == [{"name": t.name, "description": t.description,
                            "input_schema": t.parameters} for t in (T1, T2)]   # no marker


def test_turn3_marks_the_first_prompt_and_the_last_tool_result():
    kw, _ = _call(_req(TURN3))
    msgs = kw["messages"]
    assert _markers(kw) == [("system", 0), ("messages", 0, 0),
                            ("messages", len(msgs) - 1, 0)]
    assert _ttls(kw) == ["1h", "5m", "5m"]
    assert msgs[-1]["content"] == [{"type": "tool_result", "tool_use_id": "c3", "content": "ok",
                                    "is_error": True, "cache_control": MARK}]
    assert msgs[1]["content"] == ECHO        # middle assistant echo resent verbatim


def test_tail_assistant_echo_marks_its_last_markable_block():
    kw, _ = _call(_req([*TURN1, ChatMessage(role="assistant", echo=ECHO)]))
    assert kw["messages"][-1]["content"][-1] == {**ECHO[-1], "cache_control": MARK}
    tail = [{"type": "text", "text": "t"}, {"type": "thinking", "thinking": "", "signature": "s"}]
    kw, _ = _call(_req([*TURN1, ChatMessage(role="assistant", echo=tail)]))
    assert kw["messages"][-1]["content"] == [{"type": "text", "text": "t", "cache_control": MARK},
                                             tail[1]]   # thinking blocks never carry a marker


def test_no_tools_and_empty_system_have_no_marker():
    kw, _ = _call(_req(TURN1, tools=(), system=""))
    assert "tools" not in kw and kw["system"] == ""
    assert _markers(kw) == [("messages", 0, 0)]


def test_empty_blocks_get_no_marker():
    kw, _ = _call(_req([ChatMessage(role="user", text="  ")], tools=(), system=""))
    assert kw["messages"] == [{"role": "user", "content": "  "}] and _markers(kw) == []
    empty_tail = [{"type": "text", "text": ""}]
    kw, _ = _call(_req([*TURN1, ChatMessage(role="assistant", echo=empty_tail)]))
    assert kw["messages"][-1]["content"] == empty_tail


def test_request_messages_and_echo_are_not_mutated():
    req = _req(TURN3)
    before = copy.deepcopy(req.model_dump())
    echo_before = copy.deepcopy(ECHO)
    _call(req)
    _call(req)
    assert req.model_dump() == before and ECHO == echo_before
    assert all("cache_control" not in b for b in ECHO + ECHO2)


def test_chat_input_hash_is_unchanged_from_master():
    req = ChatRequest(
        purpose="deep_write", prompt_version="v1", subject="s1", system="You write pages.",
        messages=(ChatMessage(role="user", text="Write page X."),
                  ChatMessage(role="assistant", text="",
                              tool_calls=(ToolCall(id="t1", name="read", args={"k": 1}),),
                              echo=[{"type": "tool_use", "id": "t1", "name": "read",
                                     "input": {"k": 1}}]),
                  ChatMessage(role="tool", text="result", tool_call_id="t1")),
        tools=(ToolDef(name="read", description="Read.",
                       parameters={"type": "object", "properties": {}}),),
        params={"max_tokens": 1000})
    digest = chat_input_hash(ModelRef(provider="anthropic", model="claude-sonnet-5-5"), req)
    assert digest == "6cc63fe06bf2fd126df95f049245baf615d317d68b6604f2d8074816c0532930"


def test_cost_includes_cache_tokens():
    expected = (1000 * 2.0 + 500 * 10.0 + 50000 * 0.20 + 10000 * 2.50) / 1_000_000
    assert anthropic_cost("claude-sonnet-5-5", 1000, 500, 50000, 10000) == pytest.approx(expected)
    _, out = _call(_req(TURN3))
    assert out.cost == pytest.approx(expected) == pytest.approx(0.042)
    assert (out.input_tokens, out.output_tokens) == (1000, 500)
    assert (out.cache_read_tokens, out.cache_write_tokens) == (50000, 10000)
    assert (out.turn.cache_read_tokens, out.turn.cache_write_tokens) == (50000, 10000)


def test_cost_without_cache_rates_charges_the_input_rate(monkeypatch):
    # anthropic_cost is the PriceTable (llm/prices.toml): a model with no cache rates charges
    # cache tokens at the input rate
    monkeypatch.setattr(providers, "_default_prices",
                        lambda: PriceTable({"anthropic:m-x": Price(3.0, 9.0)}))
    assert anthropic_cost("m-x", 10, 20, 30, 40) == pytest.approx(
        (10 * 3.0 + 20 * 9.0 + 30 * 3.0 + 40 * 3.0) / 1e6)
    assert anthropic_cost("unpriced", 1, 1, 1, 1) is None


def test_missing_or_none_cache_usage_reads_as_zero():
    for usage in (NS(input_tokens=1000, output_tokens=500),
                  NS(input_tokens=1000, output_tokens=500, cache_read_input_tokens=None,
                     cache_creation_input_tokens=None)):
        _, out = _call(_req(TURN1), usage=usage)
        assert (out.cache_read_tokens, out.cache_write_tokens) == (0, 0)
        assert (out.turn.cache_read_tokens, out.turn.cache_write_tokens) == (0, 0)
        assert out.cost == pytest.approx((1000 * 2.0 + 500 * 10.0) / 1e6)


@pytest.mark.parametrize("stop, content, err", [
    ("refusal", [], ProviderRefusalError),
    ("max_tokens", [], ProviderEmptyOutputError),
    ("tool_use", [NS(type="tool_use", id="c1", name="x", input="s")], ProviderOutputError)])
def test_paid_chat_failures_carry_cache_tokens(stop, content, err):
    with pytest.raises(err) as e:
        _call(_req(TURN1), stop=stop, content=content)
    assert (e.value.cache_read_tokens, e.value.cache_write_tokens) == (50000, 10000)
    assert e.value.cost == pytest.approx(0.042)


def _complete(purpose, system, model="claude-sonnet-5-5", **an):
    from evowiki.llm.models import LLMRequest

    c = _AN(content=[NS(type="text", text='{"v": 1}')], **an)
    r = AnthropicProvider(client=c).complete(
        model, LLMRequest(purpose=purpose, prompt_version="v", subject="s", system=system,
                          prompt="P"), {"type": "object"})
    return c.kwargs, r


def test_complete_marks_only_a_stable_cacheable_system_and_its_cost_counts_cache_tokens():
    from evowiki.deep.agent import WRITE_SYSTEM as LONG  # any long stable system prompt

    kw, r = _complete("p", "SYS")                           # not a stable purpose: unmarked
    assert kw["system"] == "SYS"
    assert kw["messages"] == [{"role": "user", "content": "P"}]   # the prompt is never marked
    assert (r.cache_read_tokens, r.cache_write_tokens) == (50000, 10000)
    assert r.cost == pytest.approx(0.042)
    kw, _ = _complete("goals.intake", LONG)                 # stable and long: 1-hour marker
    assert kw["system"] == [{"type": "text", "text": LONG, "cache_control": MARK_1H}]
    assert kw["messages"] == [{"role": "user", "content": "P"}]
    for purpose in ("goals.enter", "graph.group_naming", "rsi.h.improve"):
        assert _complete(purpose, LONG)[0]["system"][0]["cache_control"] == MARK_1H
    assert _complete("goals.intake", "SYS")[0]["system"] == "SYS"       # too short to cache
    assert _complete("goals.intakex", LONG)[0]["system"] == LONG        # not a prefix
    assert _complete("deep.plan", LONG)[0]["system"] == LONG            # gone (stage 4): not stable


def _usage_1h(write=10000, write_1h=6000):
    return NS(input_tokens=1000, output_tokens=500, cache_read_input_tokens=50000,
              cache_creation_input_tokens=write,
              cache_creation=NS(ephemeral_5m_input_tokens=write - write_1h,
                                ephemeral_1h_input_tokens=write_1h))


def test_one_hour_writes_are_priced_at_twice_the_input_rate():
    # Sonnet 5.5: input 2.0, read 0.20, 5-minute write 2.50, 1-hour write 4.00 (USD / MTok)
    expected = (1000 * 2.0 + 500 * 10.0 + 50000 * 0.20 + 4000 * 2.50 + 6000 * 4.00) / 1e6
    assert anthropic_cost("claude-sonnet-5-5", 1000, 500, 50000, 10000, 6000) == pytest.approx(
        expected)
    _, out = _call(_req(TURN1), usage=_usage_1h())
    assert out.cost == pytest.approx(expected)
    assert (out.cache_write_tokens, out.cache_write_1h_tokens) == (10000, 6000)
    _, r = _complete("goals.intake", "SYS", usage=_usage_1h())
    assert r.cost == pytest.approx(expected) and r.cache_write_1h_tokens == 6000
    with pytest.raises(ProviderRefusalError) as e:
        _call(_req(TURN1), stop="refusal", content=[], usage=_usage_1h())
    assert e.value.cache_write_1h_tokens == 6000 and e.value.cost == pytest.approx(expected)


def test_metered_usage_prices_one_hour_writes_from_the_price_table():
    from evowiki.llm.prices import load_prices
    from evowiki.llm.usage import NullMeter
    from evowiki.llm.usage_control import UsageControl

    u = UsageControl(meter=NullMeter(), prices=load_prices()).usage_of(
        "anthropic", "claude-sonnet-5-5", None, 1000, 500, 50000, 10000, 6000)
    assert u.cost_usd == pytest.approx(anthropic_cost("claude-sonnet-5-5", 1000, 500, 50000,
                                                      10000, 6000))
    assert u.cache_write_tokens == 10000                  # the total; the 1-hour part only prices


def test_stable_prefix_splits_the_first_prompt_and_is_not_hashed():
    stable = "SHARED " * 100
    msg = ChatMessage(role="user", text=stable + "PAGE A", stable_prefix=len(stable))
    req = _req([msg, *TURN3[1:]])
    kw, _ = _call(req)
    assert kw["messages"][0]["content"] == [
        {"type": "text", "text": stable, "cache_control": MARK_1H},
        {"type": "text", "text": "PAGE A", "cache_control": MARK}]
    assert _ttls(kw) == ["1h", "1h", "5m", "5m"] and _ttl_order_ok(kw)
    assert _strip(kw) == _old_kwargs(req)                  # the text the model reads is unchanged
    ref = ModelRef(provider="anthropic", model="claude-sonnet-5-5")
    plain = _req([ChatMessage(role="user", text=stable + "PAGE A"), *TURN3[1:]])
    assert chat_input_hash(ref, req) == chat_input_hash(ref, plain)
    # the whole message stable: one block, 1 hour
    kw, _ = _call(_req([ChatMessage(role="user", text=stable, stable_prefix=len(stable))]))
    assert kw["messages"][0]["content"] == [{"type": "text", "text": stable,
                                             "cache_control": MARK_1H}]


# ---- the real requests: the page writer and the reading assistant (fakes only) ----


def _deep_requests(tmp_path, *, script, language="zh"):
    """deep.write ChatRequests of two different pages of one user (same library) through the
    real write loop over a scripted gateway; `script(i)` = the write turns of page i."""
    from dataclasses import replace

    from evowiki.wikiapp.repo import MemoryRepo
    from tests.deep.fakes import DeepScript
    from tests.deep.test_writer import outline_input, writer
    from tests.wikiapp.script import Script
    from tests.wikiapp.test_goal import Ids
    from tests.wikiapp.test_page import organized

    env = (MemoryRepo(["u1"]), Script(), Ids())
    pid = organized(env)
    inp_a = replace(outline_input(env, pid), language=language)
    inp_b = replace(inp_a, page_id="pg_other", title="另一页", goal="学会另一件事")
    out = []
    for i, inp in enumerate((inp_a, inp_b), 1):
        ds = DeepScript().plan().write(*script(i))
        writer(ds).write(inp)
        out.append([r for r in ds.requests if r.purpose == "deep.write"])
    return out


def test_deep_write_shares_tools_system_and_stable_block_across_pages_and_turns(tmp_path):
    from evowiki.deep import skill
    from tests.deep.test_writer import write_all

    page_a, page_b = _deep_requests(tmp_path, script=write_all)
    kws = [_call(r)[0] for r in (page_a[0], page_a[2], page_b[0], page_b[2])]
    a1, a3, b1, b3 = kws
    for kw in kws:
        assert _ttl_order_ok(kw)
        assert all("cache_control" not in t for t in kw["tools"])
        assert kw["system"][0]["cache_control"] == MARK_1H
        first = kw["messages"][0]["content"]
        assert [b["cache_control"] for b in first] == [MARK_1H, MARK]
    # The final request-only language contract has its own moving-tail marker.
    from evowiki.deep.language import output_rule

    assert _ttls(a1) == _ttls(a3) == ["1h", "1h", "5m", "5m"]
    assert all(kw["messages"][-1]["content"][-1] == {
        "type": "text", "text": output_rule("zh"), "cache_control": MARK} for kw in kws)
    # byte-identical shared prefix: tools, system and the stable block
    for kw in (a3, b1, b3):
        assert kw["tools"] == a1["tools"] and kw["system"] == a1["system"]
        assert kw["messages"][0]["content"][0] == a1["messages"][0]["content"][0]
    stable = a1["messages"][0]["content"][0]["text"]
    # deep@v32: the preloaded references (teaching, the EvoUI index), then the catalog, which
    # follows the library, so it is last within the stable part
    assert skill.ref("teaching") in stable and skill.ref("evoui/index") in stable
    assert stable.index("可用的交互组件") > stable.index("# EvoUI 索引") > stable.index("# 讲解原则")
    for per_page in ("pg_other", "另一页", page_a[0].subject, "这个学习者的表达参数", "自查清单"):
        assert per_page not in stable, per_page
    # the page parts differ and come after it; the text read is the joined prompt
    assert a1["messages"][0]["content"][1] != b1["messages"][0]["content"][1]
    assert "".join(b["text"] for b in a1["messages"][0]["content"]) == page_a[0].messages[0].text


def test_a_truncated_write_retry_keeps_the_system_and_appends_the_note(tmp_path):
    from evowiki.deep.agent import TRUNCATED_NOTE
    from evowiki.llm.providers import ProviderEmptyOutputError
    from tests.deep.test_writer import write_all

    def script(i):
        cut = ProviderEmptyOutputError("cut", status="truncated", input_tokens=1, output_tokens=1)
        return [cut, *write_all(i)] if i == 1 else write_all(i)

    # requests: plan_page's turn, the cut one, its retry, the next
    (_plan, first, retry, after, *_), _ = _deep_requests(tmp_path, script=script)
    k1, k2, k3 = (_call(r)[0] for r in (first, retry, after))
    assert k1["system"] == k2["system"] == k3["system"] and k1["tools"] == k2["tools"]
    assert TRUNCATED_NOTE not in k1["system"][0]["text"]
    assert k2["messages"][0] == k1["messages"][0]                  # the first prompt unchanged
    # The persisted retry note follows the tool result; the request-only language
    # contract is reasserted after it and carries the moving-tail cache marker.
    assert k2["messages"][-2] == {"role": "user", "content": TRUNCATED_NOTE}
    assert k2["messages"][-1]["content"][-1] == k1["messages"][-1]["content"][-1]
    assert TRUNCATED_NOTE in str(k3["messages"])                    # stays in the history
    assert all(_ttl_order_ok(k) for k in (k1, k2, k3))


def test_assistant_turns_share_one_system_prompt(tmp_path):
    from evowiki.assistant.contracts import MessageIn, Selection
    from evowiki.assistant.loop import run_turn
    from tests.assistant.fakes import INFO
    from tests.assistant.test_loop import setup

    deps, prov, _ = setup(tmp_path, [ChatTurn(text="答一"), ChatTurn(text="答二")])
    sel = Selection(text="用均值和标准差做单位")
    list(run_turn(deps, INFO, MessageIn(text="QONE", selection=sel)))
    list(run_turn(deps, INFO, MessageIn(text="QTWO")))
    k1, k2 = (_call(r)[0] for r in prov.chat_calls)
    assert k1["system"] == k2["system"] and k1["system"][0]["cache_control"] == MARK_1H
    assert k1["tools"] == k2["tools"]
    system = k1["system"][0]["text"]
    assert "QONE" not in system and "用均值和标准差做单位" not in system
    # the history holds the plain question (and its quote), not the earlier context pack
    first = k2["messages"][0]["content"]
    assert "用均值和标准差做单位" in first[0]["text"] and first[1]["text"] == "QONE"
    assert k2["messages"][-1]["content"][-1]["text"].endswith("他问：\nQTWO")
    assert all(_ttl_order_ok(k) for k in (k1, k2))


def test_chat_turn_round_trips_and_old_rows_still_load():
    t = ChatTurn(text="a", input_tokens=1, output_tokens=2, cache_read_tokens=3,
                 cache_write_tokens=4)
    assert ChatTurn.model_validate(t.model_dump(mode="json")) == t
    old = {"text": "a", "tool_calls": [], "echo": None, "input_tokens": 1, "output_tokens": 2}
    loaded = ChatTurn.model_validate(old)
    assert (loaded.cache_read_tokens, loaded.cache_write_tokens) == (0, 0)
    r = ProviderResponse(raw_output="x")
    assert (r.cache_read_tokens, r.cache_write_tokens) == (None, None)
    e = ProviderOutputError("x")
    assert (e.cache_read_tokens, e.cache_write_tokens) == (None, None)


@pytest.mark.parametrize("messages,tools,system", SHAPES)
def test_a_request_with_cache_off_carries_no_breakpoint(messages, tools, system):
    """`ChatRequest.cache` False (the assistant's eyes hand-off): the payload is exactly the
    uncached one, no cache_control anywhere."""
    req = _req(messages, tools, system).model_copy(update={"cache": False})
    kw, _ = _call(req)
    assert _markers(kw) == [] and kw == _old_kwargs(req)


def test_cache_switch_is_out_of_the_hash_only_when_on():
    req = _req(TURN1)
    ref = ModelRef(provider="anthropic", model="claude-sonnet-5-5")
    assert "cache" not in req.model_dump(exclude_defaults=True)
    off = req.model_copy(update={"cache": False})
    assert chat_input_hash(ref, off) != chat_input_hash(ref, req)
    # a default (cached) request hashes as one built before the field existed
    import hashlib
    import json

    from evowiki.llm.chat import CHAT_SCHEMA
    dumped = req.model_dump(mode="json", exclude={"cache": True, "deferred": True, "messages": {
        "__all__": {"stable_prefix": True, "origin": True,       # origin came later too
                    "parts": {"__all__": {"data_b64"}}}}})
    for m in dumped["messages"]:
        del m["parts"]
    blob = json.dumps({"kind": CHAT_SCHEMA, "provider": "anthropic",
                       "model": "claude-sonnet-5-5", **dumped},
                      sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert chat_input_hash(ref, req) == hashlib.sha256(blob).hexdigest()


def test_deep_write_shares_stable_cache_prefix_across_output_languages(tmp_path):
    from evowiki.deep.language import output_rule
    from tests.deep.test_writer import write_all

    zh, _ = _deep_requests(tmp_path, script=write_all, language="zh")
    en, _ = _deep_requests(tmp_path, script=write_all, language="en")
    for left, right in zip(zh, en, strict=True):
        a, b = _call(left)[0], _call(right)[0]
        assert a["system"] == b["system"]
        assert a["system"][0]["cache_control"] == MARK_1H
        assert "All learner-facing text must be written" not in a["system"][0]["text"]
        assert a["tools"] == b["tools"]
        assert a["messages"][0]["content"][0] == b["messages"][0]["content"][0]
        assert a["messages"][-1]["content"][-1]["text"] == output_rule("zh")
        assert b["messages"][-1]["content"][-1]["text"] == output_rule("en")
        assert _ttl_order_ok(a) and _ttl_order_ok(b)
