import json
from types import SimpleNamespace as NS

import pytest

from evowiki.llm.chat import ChatMessage, ChatRequest, ToolCall, ToolDef
from evowiki.llm.gateway import ProviderOutputError
from evowiki.llm.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    ProviderEmptyOutputError,
    ProviderRefusalError,
    anthropic_messages,
    deepseek_messages,
)

TOOL = ToolDef(name="web_search", description="搜", parameters={"type": "object",
               "properties": {"query": {"type": "string"}}})
HIST = (ChatMessage(role="user", text="问"),
        ChatMessage(role="assistant", text="我查一下",
                    tool_calls=(ToolCall(id="c1", name="web_search", args={"query": "z"}),
                                ToolCall(id="c2", name="web_search", args={"query": "y"})),
                    echo=None),
        ChatMessage(role="tool", tool_call_id="c1", text="结果1"),
        ChatMessage(role="tool", tool_call_id="c2", text="出错了", is_error=True))
REQ = ChatRequest(purpose="assistant.turn", prompt_version="assistant@v1", subject="s",
                  system="SYS", messages=HIST, tools=(TOOL,))


def test_deepseek_messages_shape():
    m = deepseek_messages(REQ)
    assert m[0] == {"role": "system", "content": "SYS"}
    assert m[2]["tool_calls"][0] == {"id": "c1", "type": "function", "function": {
        "name": "web_search", "arguments": json.dumps({"query": "z"}, ensure_ascii=False)}}
    assert m[3] == {"role": "tool", "tool_call_id": "c1", "content": "结果1"}


def test_deepseek_resends_reasoning_within_the_turn():
    req = REQ.model_copy(update={"messages": (HIST[0], HIST[1].model_copy(
        update={"echo": {"reasoning_content": "想"}}), *HIST[2:])})
    assert deepseek_messages(req)[2]["reasoning_content"] == "想"


class _DS:
    def __init__(self, message, finish="stop", usage=(5, 2)):
        self.kwargs = None
        self._resp = NS(model="deepseek-v4-pro", usage=NS(prompt_tokens=usage[0],
                        completion_tokens=usage[1]),
                        choices=[NS(message=message, finish_reason=finish)])
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.kwargs = kw
        return self._resp


def _ds_msg(content=None, calls=(), reasoning=None):
    return NS(content=content, reasoning_content=reasoning, tool_calls=[
        NS(id=i, function=NS(name=n, arguments=a)) for i, n, a in calls] or None)


def test_deepseek_chat_parses_tool_calls_and_usage():
    c = _DS(_ds_msg(calls=[("c9", "web_search", '{"query": "z"}')], reasoning="r"))
    out = DeepSeekProvider(client=c, max_tokens=100).chat("deepseek-v4-pro", REQ)
    assert out.turn.tool_calls == (ToolCall(id="c9", name="web_search", args={"query": "z"}),)
    assert out.turn.echo == {"reasoning_content": "r"}
    assert (out.turn.input_tokens, out.turn.output_tokens) == (5, 2)
    assert c.kwargs["tool_choice"] == "auto" and c.kwargs["tools"][0]["type"] == "function"
    assert "response_format" not in c.kwargs


def test_deepseek_bad_arguments_go_back_to_the_model():
    c = _DS(_ds_msg(calls=[("c9", "web_search", "{not json")]))
    call = DeepSeekProvider(client=c, max_tokens=100).chat("m", REQ).turn.tool_calls[0]
    assert (call.name, call.args, call.unparsed) == ("web_search", {}, "{not json")


def test_deepseek_truncated_and_no_tools_omits_tool_params():
    c = _DS(_ds_msg(content="半句"), finish="length")
    with pytest.raises(ProviderEmptyOutputError):
        DeepSeekProvider(client=c, max_tokens=100).chat("m", REQ)
    c = _DS(_ds_msg(content="答"))
    DeepSeekProvider(client=c, max_tokens=100).chat("m", REQ.model_copy(update={"tools": ()}))
    assert "tools" not in c.kwargs and "tool_choice" not in c.kwargs


def test_anthropic_merges_tool_results_into_one_user_message():
    m = anthropic_messages(REQ)
    assert [x["role"] for x in m] == ["user", "assistant", "user"]
    assert m[1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "web_search",
                                  "input": {"query": "z"}}
    assert m[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": "结果1", "is_error": False},
        {"type": "tool_result", "tool_use_id": "c2", "content": "出错了", "is_error": True}]


def test_anthropic_echo_is_resent_verbatim():
    blocks = [{"type": "thinking", "thinking": "", "signature": "sig"},
              {"type": "tool_use", "id": "c1", "name": "web_search", "input": {"query": "z"}}]
    req = REQ.model_copy(update={"messages": (HIST[0], HIST[1].model_copy(
        update={"echo": blocks}), *HIST[2:])})
    assert anthropic_messages(req)[1]["content"] == blocks


class _AN:
    def __init__(self, content, stop="end_turn"):
        self.kwargs = None
        self._resp = NS(model="claude-opus-5-5", stop_reason=stop, content=content,
                        usage=NS(input_tokens=1000, output_tokens=100))
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.kwargs = kw
        return self._resp


def test_anthropic_chat_reads_text_tools_echo_cost():
    c = _AN([NS(type="text", text="好"),
             NS(type="tool_use", id="c1", name="web_search", input={"query": "z"})],
            stop="tool_use")
    out = AnthropicProvider(client=c).chat("claude-opus-5-5", REQ)
    assert out.turn.text == "好" and out.turn.tool_calls[0].args == {"query": "z"}
    assert out.turn.echo[1]["type"] == "tool_use"
    assert out.cost == pytest.approx((1000 * 4.0 + 100 * 20.0) / 1_000_000)
    assert c.kwargs["tool_choice"] == {"type": "auto"}
    assert c.kwargs["tools"][0]["input_schema"] == TOOL.parameters


def test_anthropic_refusal_and_truncation():
    with pytest.raises(ProviderRefusalError):
        AnthropicProvider(client=_AN([], stop="refusal")).chat("m", REQ)
    with pytest.raises(ProviderEmptyOutputError):
        AnthropicProvider(client=_AN([], stop="max_tokens")).chat("m", REQ)


def test_paid_chat_failures_carry_their_tokens():
    # A response arrived, so the failure is paid: the gateway meters these tokens.
    with pytest.raises(ProviderEmptyOutputError) as e:
        DeepSeekProvider(client=_DS(_ds_msg(content="半"), finish="length"),
                         max_tokens=100).chat("m", REQ)
    assert (e.value.input_tokens, e.value.output_tokens) == (5, 2)
    for stop, err in (("refusal", ProviderRefusalError), ("max_tokens", ProviderEmptyOutputError)):
        with pytest.raises(err) as e:
            AnthropicProvider(client=_AN([], stop=stop)).chat("m", REQ)
        assert (e.value.input_tokens, e.value.output_tokens) == (1000, 100)
    with pytest.raises(ProviderOutputError) as e:
        AnthropicProvider(client=_AN([NS(type="tool_use", id="c1", name="x", input="s")],
                                     stop="tool_use")).chat("m", REQ)
    assert e.value.status == "parse_error"
    assert (e.value.input_tokens, e.value.output_tokens) == (1000, 100)


def test_chat_response_reports_provider_usage_or_none():
    out = DeepSeekProvider(client=_DS(_ds_msg(content="答")), max_tokens=100).chat("m", REQ)
    assert (out.input_tokens, out.output_tokens) == (5, 2)
    c = _DS(_ds_msg(content="答"))
    c._resp.usage = None                         # provider did not report usage
    out = DeepSeekProvider(client=c, max_tokens=100).chat("m", REQ)
    assert (out.input_tokens, out.output_tokens) == (None, None)
    assert (out.turn.input_tokens, out.turn.output_tokens) == (0, 0)   # turn stays int
    out = AnthropicProvider(client=_AN([NS(type="text", text="好")])).chat("m", REQ)
    assert (out.input_tokens, out.output_tokens) == (1000, 100)


def test_quick_level_chat_says_thinking_disabled_and_keeps_old_reasoning_echoes():
    """The assistant's 快速 (deepseek-nothink, chat_thinking=False) after 思考 turns in the same
    session: the switch says disabled and the earlier reasoning stays in the history (DeepSeek
    ignores it there; on a tool-calling thinking history it is required)."""
    req = REQ.model_copy(update={"messages": (HIST[0], HIST[1].model_copy(
        update={"echo": {"reasoning_content": "想"}}), *HIST[2:])})
    c = _DS(_ds_msg(content="好"))
    DeepSeekProvider(client=c, thinking=False, chat_thinking=False).chat("m", req)
    assert c.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert c.kwargs["messages"][2]["reasoning_content"] == "想"


def test_deepseek_drops_a_stray_quote_after_a_closed_list_in_tool_arguments():
    """The two plan_page failures of the S3 live run (llm_attempt.raw_output, recorded): a `"`
    right after the one_at_a_time list. The plan parses and is a valid plan_page plan."""
    from pathlib import Path

    from evowiki.deep.contracts import PlanAnswers
    raws = json.loads((Path(__file__).parent / "fixtures" / "deepseek_plan_page_stray_quote.json")
                      .read_text(encoding="utf-8"))["plan_page_arguments"]
    assert len(raws) == 2
    for raw in raws:
        with pytest.raises(ValueError):
            json.loads(raw)
        c = _DS(_ds_msg(calls=[("c1", "plan_page", raw)]))
        [call] = DeepSeekProvider(client=c, max_tokens=100).chat("m", REQ).turn.tool_calls
        plan = PlanAnswers.model_validate(call.args["plan"])
        assert plan.one_at_a_time[-1].endswith("）") and plan.practice


def test_deepseek_other_broken_arguments_still_fail():
    from evowiki.llm.providers import deepseek_tool_args
    assert deepseek_tool_args('{"a": [1, 2]"}') == {"a": [1, 2]}         # the one repair
    assert deepseek_tool_args('{"a": "x"}') == {"a": "x"}
    assert deepseek_tool_args('{"p": {"a": ["x}\\""]}') == {"p": {"a": ['x}"']}}   # a closer missing
    assert deepseek_tool_args('{"a": "cut') is None                               # inside a string
    for bad in ('{"a": [1, 2]] }', '{"a": 1 "b": 2}', '"{\\"a\\": 1}"', '{"a": [1]"""""}'):
        assert not isinstance(deepseek_tool_args(bad), dict), bad
