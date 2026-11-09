"""DeepSeek strict function calling for `complete` (EVOWIKI_DEEPSEEK_STRICT, default off). No paid
API: fake OpenAI clients record the request body, and the real SDK talks to a local server for the
beta base URL and the streamed (thinking) path. Docs: https://api-docs.deepseek.com/guides/tool_calls
(strict mode, Beta) and /api/create-chat-completion (tool_choice in thinking mode)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict, Field

from evowiki.deep.evoui.typed import QuizSpec
from evowiki.goals.guardrails import DraftOutline
from evowiki.llm.gateway import ProviderOutputError, _parse
from evowiki.llm.models import LLMRequest
from evowiki.llm.providers import (
    DEEPSEEK_BETA_BASE_URL,
    DEEPSEEK_STRICT_FORMATS,
    DeepSeekProvider,
    ProviderEmptyOutputError,
    ProviderRejectedError,
    StrictSchemaError,
    deepseek_strict,
    deepseek_strict_schema,
)
from evowiki.view.naming import HubName
from tests.llm.test_anthropic_output_schemas import output_schemas


class QuestionOut(BaseModel):
    """A quiz block on a section (a typed EvoUI template with nested definitions)."""

    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(description="放在哪一段（页面里已有的段 id）")
    why: str = Field(description="它的用处（看懂 / 练熟 / 辨析），一句话")
    block: QuizSpec


REQ = LLMRequest(purpose="p", prompt_version="v1", subject="s", system="SYS", prompt="PROMPT")
FORCED = {"type": "function", "function": {"name": "respond"}}


class FakeClient:
    """An OpenAI client stand-in: `with_options(base_url=...)` returns a child that records which
    base URL each request went to; `reply` is (content, tool_calls, finish_reason) or an
    exception to raise."""

    def __init__(self, reply, base_url="https://api.deepseek.com", log=None):
        self.reply, self.base_url = reply, base_url
        self.log = [] if log is None else log
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def with_options(self, **kw):
        return FakeClient(self.reply, kw.get("base_url", self.base_url), self.log)

    def _create(self, **kw):
        self.log.append((self.base_url, kw))
        if isinstance(self.reply, Exception):
            raise self.reply
        content, calls, finish = self.reply
        tool_calls = [SimpleNamespace(id=f"call_{i}", type="function",
                                      function=SimpleNamespace(name=n, arguments=a))
                      for i, (n, a) in enumerate(calls)]
        usage = SimpleNamespace(prompt_tokens=60, completion_tokens=7,
                                prompt_cache_hit_tokens=50, prompt_cache_miss_tokens=10)
        return SimpleNamespace(model="deepseek-v4-flash", usage=usage, choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=tool_calls or None),
            finish_reason=finish)])


def _answer(args: str, finish="tool_calls"):
    return (None, [("respond", args)], finish)


def _strict(reply, **kw):
    client = FakeClient(reply)
    return DeepSeekProvider(client=client, env={}, strict=True, **kw), client.log


# ---- the switch ----

@pytest.mark.parametrize("raw,want", [(None, False), ("", False), ("0", False), ("off", False),
                                      ("1", True), ("true", True), (" ON ", True)])
def test_the_flag_reads_on_and_off(raw, want):
    assert deepseek_strict({} if raw is None else {"EVOWIKI_DEEPSEEK_STRICT": raw}) is want


def test_an_unreadable_flag_fails_closed():
    with pytest.raises(ValueError, match="EVOWIKI_DEEPSEEK_STRICT"):
        deepseek_strict({"EVOWIKI_DEEPSEEK_STRICT": "maybe"})
    with pytest.raises(ValueError, match="EVOWIKI_DEEPSEEK_STRICT"):
        DeepSeekProvider(client=FakeClient(_answer("{}")),
                         env={"EVOWIKI_DEEPSEEK_STRICT": "maybe"})


def test_off_by_default_keeps_the_json_object_request():
    client = FakeClient(('{"label_en": "a", "label_zh": "b"}', [], "stop"))
    DeepSeekProvider(client=client, env={}).complete("deepseek-v4-flash", REQ,
                                                     HubName.model_json_schema())
    (url, body), = client.log
    assert url == "https://api.deepseek.com"
    assert body["response_format"] == {"type": "json_object"} and "tools" not in body
    assert "JSON Schema" in body["messages"][1]["content"]


def test_the_env_flag_turns_it_on_and_needs_an_sdk_client():
    client = FakeClient(_answer('{"label_en": "a", "label_zh": "b"}'))
    DeepSeekProvider(client=client, env={"EVOWIKI_DEEPSEEK_STRICT": "1"}).complete(
        "deepseek-v4-flash", REQ, HubName.model_json_schema())
    assert client.log[0][0] == DEEPSEEK_BETA_BASE_URL
    with pytest.raises(ValueError, match="strict mode needs an OpenAI client"):
        DeepSeekProvider(client=SimpleNamespace(), env={}, strict=True)


# ---- exact request bodies for real output schemas ----

def test_hub_name_request_body_is_exact_with_thinking_off():
    provider, log = _strict(_answer('{"label_en": "Linear maps", "label_zh": "线性映射"}'),
                            thinking=False, max_tokens=64)
    resp = provider.complete("deepseek-v4-flash", REQ, HubName.model_json_schema())
    (url, body), = log
    assert url == DEEPSEEK_BETA_BASE_URL
    assert body == {
        "model": "deepseek-v4-flash", "max_tokens": 64,
        "messages": [{"role": "system", "content": "SYS"},
                     {"role": "user", "content": "PROMPT\n\nGive your answer by calling the "
                                                 "`respond` tool exactly once: its arguments "
                                                 "are the whole answer."}],
        "tools": [{"type": "function", "function": {
            "name": "respond", "strict": True,
            "description": "Return the answer as this tool's arguments.",
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": {"label_en": {"type": "string"},
                                          "label_zh": {"type": "string"}},
                           "required": ["label_en", "label_zh"]}}}],
        "tool_choice": FORCED,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    # the tool arguments are the raw output the gateway parses; usage as for json_object
    assert _parse(HubName, resp.raw_output) == HubName(label_en="Linear maps", label_zh="线性映射")
    assert (resp.input_tokens, resp.output_tokens, resp.cache_read_tokens) == (10, 7, 50)


def test_question_block_nested_defs_are_inlined_and_const_becomes_enum():
    provider, log = _strict(_answer("{}"), thinking=False)
    provider.complete("deepseek-v4-flash", REQ, QuestionOut.model_json_schema())
    params = log[0][1]["tools"][0]["function"]["parameters"]
    block = params["properties"]["block"]
    question = block["properties"]["questions"]["items"]
    assert params["required"] == ["section_id", "why", "block"]
    assert block["properties"]["kind"] == {"type": "string", "enum": ["quiz"]}
    assert block["properties"]["layout"]["enum"] == ["list", "paged"]
    assert question["additionalProperties"] is False
    assert question["required"] == ["q", "options", "correct", "why", "hint", "cell"]
    assert question["properties"]["correct"] == {"type": "integer",
                                                 "description": "正确选项的序号，从 0 开始"}
    text = json.dumps(params)
    assert "$ref" not in text and "$defs" not in text
    assert '"title": "' not in text            # schema titles gone; the property `title` stays
    assert block["properties"]["title"]["type"] == "string"


def test_outline_optional_nested_check_keeps_null_and_drops_defaults():
    provider, log = _strict(_answer('{"sections": []}'), thinking=False)
    resp = provider.complete("deepseek-v4-flash", REQ, DraftOutline.model_json_schema())
    params = log[0][1]["tools"][0]["function"]["parameters"]
    section = params["properties"]["sections"]["items"]
    check = section["properties"]["check"]
    assert params["required"] == ["profile", "assumed", "sections"]
    assert section["required"] == list(section["properties"])
    assert check["anyOf"][1] == {"type": "null"}
    assert check["anyOf"][0]["required"] == ["cell", "stem", "options", "answer"]
    assert "default" not in json.dumps(params)
    assert _parse(DraftOutline, resp.raw_output) == DraftOutline()


def test_unsupported_keywords_are_stripped_or_converted():
    schema = {"type": "object", "title": "T",
              "properties": {
                  "name": {"type": "string", "minLength": 1, "maxLength": 9, "pattern": "^a",
                           "format": "date-time", "default": "x"},
                  "mail": {"type": "string", "format": "email"},
                  "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                           "maxItems": 3, "uniqueItems": True},
                  "n": {"type": "integer", "minimum": 0, "maximum": 5, "const": 3},
                  "kind": {"oneOf": [{"type": "string"}, {"type": "integer"}],
                           "discriminator": {"propertyName": "x"}},
                  "anything": {"title": "Anything"},
                  "wrapped": {"allOf": [{"$ref": "#/$defs/In"}], "description": "d"},
                  "title": {"type": "string"}},    # a property NAMED title is kept
              "$defs": {"In": {"type": "object", "title": "In",
                               "properties": {"x": {"type": "number", "multipleOf": 2}}}}}
    out = deepseek_strict_schema(schema)
    p = out["properties"]
    assert p["name"] == {"type": "string", "pattern": "^a"}
    assert p["mail"] == {"type": "string", "format": "email"}
    assert p["tags"] == {"type": "array", "items": {"type": "string"}}
    assert p["n"] == {"type": "integer", "minimum": 0, "maximum": 5, "const": 3}
    assert p["kind"] == {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    assert p["anything"] == {"anyOf": [{"type": t} for t in ("string", "number", "boolean",
                                                              "null")]}
    assert p["wrapped"] == {"type": "object", "description": "d", "additionalProperties": False,
                            "properties": {"x": {"type": "number", "multipleOf": 2}},
                            "required": ["x"]}
    assert p["title"] == {"type": "string"}
    assert out["required"] == list(p) and "$defs" not in out and "title" not in out
    assert "minLength" in schema["properties"]["name"]      # input untouched


@pytest.mark.parametrize("schema,match", [
    ({"type": "object", "properties": {"m": {"type": "object",
                                             "additionalProperties": {"type": "integer"}}}},
     "open map"),
    ({"type": "object", "properties": {"kids": {"type": "array",
                                                "items": {"$ref": "#/$defs/N"}}},
      "$defs": {"N": {"type": "object", "properties": {"kids": {
          "type": "array", "items": {"$ref": "#/$defs/N"}}}}}}, "recursive"),
])
def test_an_inexpressible_schema_is_refused_before_any_request(schema, match):
    with pytest.raises(StrictSchemaError, match=match):
        deepseek_strict_schema(schema)
    provider, log = _strict(_answer("{}"))
    with pytest.raises(ProviderRejectedError, match="cannot express"):
        provider.complete("deepseek-v4-flash", REQ, schema)
    assert log == []      # nothing sent, nothing paid


# ---- thinking ----

@pytest.mark.parametrize("thinking,switch", [(True, {"thinking": {"type": "enabled"}}),
                                             (None, None)])
def test_thinking_mode_never_forces_the_tool(thinking, switch):
    """DeepSeek: required / named tool_choice are a 400 in thinking mode (also the default)."""
    provider, log = _strict(_answer('{"label_en": "a", "label_zh": "b"}'), thinking=thinking)
    provider.complete("deepseek-v4-pro", REQ, HubName.model_json_schema())
    body = log[0][1]
    assert "tool_choice" not in body and body["tools"][0]["function"]["strict"] is True
    assert body.get("extra_body", {}).get("thinking") == (switch or {}).get("thinking")


def test_a_per_request_thinking_off_forces_the_tool():
    provider, log = _strict(_answer("{}"), thinking=True)
    provider.complete("deepseek-v4-pro", REQ.model_copy(update={"params": {"thinking": False}}),
                      HubName.model_json_schema())
    assert log[0][1]["tool_choice"] == FORCED


# ---- what comes back ----

@pytest.mark.parametrize("reply", [('{"label_en": "a"}', [], "stop"),
                                   (None, [("other", "{}")], "tool_calls"),
                                   (None, [("respond", "{}"), ("respond", "{}")], "tool_calls")],
                         ids=["text-only", "wrong-tool", "two-calls"])
def test_anything_but_one_respond_call_is_a_parse_error(reply):
    provider, _ = _strict(reply)
    with pytest.raises(ProviderOutputError, match="did not answer with one `respond`") as e:
        provider.complete("deepseek-v4-pro", REQ, HubName.model_json_schema())
    assert e.value.status == "parse_error"           # retried by the gateway in record mode
    assert e.value.input_tokens == 10                # paid failure keeps its tokens


def test_truncated_and_empty_arguments():
    provider, _ = _strict(_answer('{"label_en": "a', finish="length"))
    with pytest.raises(ProviderEmptyOutputError) as e:
        provider.complete("deepseek-v4-pro", REQ, HubName.model_json_schema())
    assert e.value.status == "truncated" and e.value.raw_output == '{"label_en": "a'
    provider, _ = _strict(_answer("  "))
    with pytest.raises(ProviderEmptyOutputError) as e:
        provider.complete("deepseek-v4-pro", REQ, HubName.model_json_schema())
    assert e.value.status == "empty_output"


class _BadRequest(Exception):
    status_code = 400

    def __init__(self):
        super().__init__("bad request")
        self.body = {"error": {"message": "Invalid schema for function 'respond'"}}


def test_a_400_in_strict_mode_is_rejected_and_never_falls_back():
    provider, log = _strict(_BadRequest())
    with pytest.raises(ProviderRejectedError, match="strict mode rejected.*Invalid schema") as e:
        provider.complete("deepseek-v4-pro", REQ, HubName.model_json_schema())
    assert e.value.status_code == 400 and e.value.status == "provider_error"
    assert len(log) == 1                              # no json_object retry


def test_a_400_without_strict_is_a_refused_request():
    from evowiki.llm.providers import ProviderBadRequestError
    client = FakeClient(_BadRequest())
    with pytest.raises(ProviderBadRequestError) as e:
        DeepSeekProvider(client=client, env={}).complete("deepseek-v4-pro", REQ, {})
    assert e.value.status_code == 400 and e.value.status == "provider_error"


def test_every_product_output_schema_converts_to_the_documented_subset():
    allowed_string = {"type", "description", "enum", "pattern", "format"}
    allowed_number = {"type", "description", "enum", "const", "minimum", "maximum",
                      "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}

    def check(node, path, bad):
        if isinstance(node, list):
            for i, v in enumerate(node):
                check(v, f"{path}[{i}]", bad)
            return
        if not isinstance(node, dict):
            return
        t = node.get("type")
        keys = set(node)
        if "$ref" in node or "$defs" in node:
            bad.append(f"{path}: ref")
        if t == "object":
            if node.get("additionalProperties") is not False or node.get("required") != list(
                    node.get("properties", {})):
                bad.append(f"{path}: open object")
            for k, v in node["properties"].items():
                check(v, f"{path}/{k}", bad)
            return
        if t == "string" and keys - allowed_string:
            bad.append(f"{path}: {keys - allowed_string}")
        if t == "string" and node.get("format") not in (None, *DEEPSEEK_STRICT_FORMATS):
            bad.append(f"{path}: format")
        if t in ("number", "integer") and keys - allowed_number:
            bad.append(f"{path}: {keys - allowed_number}")
        if t == "array" and keys - {"type", "description", "items"}:
            bad.append(f"{path}: {keys - {'type', 'description', 'items'}}")
        if "oneOf" in node or "allOf" in node:
            bad.append(f"{path}: oneOf/allOf")
        for k in ("items", "anyOf"):
            if k in node:
                check(node[k], f"{path}/{k}", bad)

    problems = {}
    for name, model in output_schemas().items():
        bad: list[str] = []
        check(deepseek_strict_schema(model.model_json_schema()), "", bad)
        if bad:
            problems[name] = bad
    assert not problems, problems


# ---- the real SDK against a local server ----

def _serve(response: bytes, content_type: str):
    seen = SimpleNamespace(paths=[], bodies=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.paths.append(self.path)
            seen.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, seen


def test_the_sdk_client_posts_to_the_beta_path(monkeypatch):
    openai = pytest.importorskip("openai")
    reply = json.dumps({"id": "c", "object": "chat.completion", "created": 0,
                        "model": "deepseek-v4-flash",
                        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                            "role": "assistant", "content": None, "tool_calls": [{
                                "id": "call_0", "type": "function", "function": {
                                    "name": "respond",
                                    "arguments": '{"label_en": "a", "label_zh": "b"}'}}]}}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                                  "total_tokens": 8}}).encode()
    httpd, seen = _serve(reply, "application/json")
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        monkeypatch.setattr("evowiki.llm.providers.DEEPSEEK_BETA_BASE_URL", url + "/beta")
        client = openai.OpenAI(api_key="local-test", base_url=url, max_retries=0)
        resp = DeepSeekProvider(client=client, env={}, strict=True, thinking=False).complete(
            "deepseek-v4-flash", REQ, HubName.model_json_schema())
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert seen.paths == ["/beta/chat/completions"]
    assert seen.bodies[0]["tools"][0]["function"]["strict"] is True
    assert _parse(HubName, resp.raw_output) == HubName(label_en="a", label_zh="b")


def test_a_streamed_thinking_call_folds_the_tool_arguments():
    openai = pytest.importorskip("openai")
    from evowiki.runtime.llm import _openai_http
    from tests.llm.test_providers_stream import ds_frames, sse_server

    frames = ds_frames([], reasoning=["想"], finish="tool_calls",
                       calls=[("call_0", "respond", ['{"label_en": "a", ', '"label_zh": "b"}'])])
    with sse_server(frames) as srv:
        client = openai.OpenAI(api_key="local-test", base_url=srv.url, max_retries=0)
        provider = DeepSeekProvider(client=client, env={}, thinking=True, max_tokens=64,
                                    timeout_for=lambda p: 10.0, http_client_factory=_openai_http,
                                    strict=True, strict_client=client)
        resp = provider.complete("deepseek-v4-pro", REQ, HubName.model_json_schema())
        body = srv.bodies[0]
    assert body["stream"] is True and "tool_choice" not in body
    assert body["thinking"] == {"type": "enabled"}
    assert _parse(HubName, resp.raw_output) == HubName(label_en="a", label_zh="b")
