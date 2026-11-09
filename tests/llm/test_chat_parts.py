import pytest

from evowiki.llm.chat import (
    ChatMessage,
    ChatRequest,
    ChatTurn,
    DocPart,
    ImagePart,
    ImagesNotSupported,
    TextPart,
    ToolCall,
    chat_input_hash,
    has_images,
)
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.providers import (
    anthropic_messages,
    cache_marked_messages,
    deepseek_messages,
)
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import Account

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
ACC = Account(user_id="u1", group="assistant")
SHA_A, SHA_B = "a" * 64, "b" * 64
B64 = "QUJD" * 100


def img(sha=SHA_A, data=B64, name="a.png") -> ImagePart:
    return ImagePart(media_type="image/png", sha256=sha, data_b64=data, name=name)


def doc() -> DocPart:
    return DocPart(source_id="s1", title="讲义", text="<fence>内容</fence>")


def req(*parts, text="问题", stable_prefix=0) -> ChatRequest:
    return ChatRequest(purpose="assistant.turn", prompt_version="assistant@v1", subject="s1",
                       system="SYS", messages=(ChatMessage(
                           role="user", text=text, parts=tuple(parts),
                           stable_prefix=stable_prefix),))


def test_hash_unchanged_without_parts():
    r = ChatRequest(
        purpose="assistant.turn", prompt_version="assistant@v1", subject="s1", system="SYS",
        messages=(ChatMessage(role="user", text="z-score 是什么？", stable_prefix=3),
                  ChatMessage(role="assistant", text="a",
                              tool_calls=(ToolCall(id="c1", name="t", args={"x": 1}),))),
        params={"k": 1})
    # computed with the code before `parts` existed
    assert chat_input_hash(REF, r) == (
        "4c519eac6eade753445759645b09e0cbb4330d5867e333e8850aa0865f7e77b6")


def test_hash_uses_sha_not_bytes():
    h = lambda p: chat_input_hash(REF, req(p))
    assert h(img(data="QUJD")) == h(img(data="WFla"))
    assert h(img(sha=SHA_A)) != h(img(sha=SHA_B))
    assert chat_input_hash(REF, req(img())) != chat_input_hash(REF, req())


def test_anthropic_image_and_doc_blocks():
    msgs = anthropic_messages(req(doc(), img()))
    assert msgs == [{"role": "user", "content": [
        {"type": "text", "text": "【附件：讲义（source_id=s1）】\n<fence>内容</fence>"},
        {"type": "text", "text": "【图片：a.png】"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": B64}},
        {"type": "text", "text": "问题"}]}]


def test_anthropic_cache_marks_with_parts():
    r = req(doc(), img(), text="稳定部分|变化部分", stable_prefix=5)
    marked = cache_marked_messages(anthropic_messages(r), stable=5)
    blocks = marked[0]["content"]
    assert [b.get("cache_control") for b in blocks] == [
        None, None, None, {"type": "ephemeral", "ttl": "1h"}, {"type": "ephemeral"}]
    assert [b["text"] for b in blocks[3:]] == ["稳定部分|", "变化部分"]
    # without a stable prefix: one marker on the last block, nothing raises
    plain = cache_marked_messages(anthropic_messages(req(doc())))
    assert plain[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_deepseek_doc_is_text():
    msgs = deepseek_messages(req(TextPart(text="前言"), doc()))
    assert msgs[1] == {"role": "user", "content": (
        "前言\n\n【附件：讲义（source_id=s1）】\n<fence>内容</fence>\n\n问题")}


def test_deepseek_rejects_images():
    r = req(doc(), img())
    assert has_images(r) and not has_images(req(doc()))
    with pytest.raises(ImagesNotSupported):
        deepseek_messages(r)


def test_gateway_never_persists_image_bytes(tmp_path):
    attempts = AttemptLog(tmp_path / "att.jsonl")
    calls = CallLog(tmp_path / "calls.jsonl")
    prov = ScriptedChatProvider("scripted", lambda m, r: ChatTurn(text="答"))
    g = Gateway(CFG, calls, mode="record", providers={"scripted": prov}, attempts=attempts)
    g.converse("generator", req(img()), account=None)

    def boom(m, r):
        raise ImagesNotSupported("deepseek 不能看图片")
    attempts2 = AttemptLog(tmp_path / "att2.jsonl")
    calls2 = CallLog(tmp_path / "calls2.jsonl")
    g2 = Gateway(CFG, calls2, mode="record",
                 providers={"scripted": ScriptedChatProvider("scripted", boom)},
                 attempts=attempts2)
    with pytest.raises(ImagesNotSupported):
        g2.converse("generator", req(img()), account=None)
    rows = [*calls.calls(), *attempts.attempts(), *calls2.calls(), *attempts2.attempts()]
    assert attempts2.attempts() and attempts.attempts()
    assert all("QUJDQUJD" not in x.model_dump_json() for x in rows)


def test_trace_never_keeps_image_bytes_or_material_text():
    """Agent traces (llm_text / agent_step, migration 0064) keep the parts' ids, hashes and
    names, never the image bytes or the material's full text; such a step cannot be replayed."""
    import json

    from evowiki.agent.trace import NotReplayable, model_payload, request_from, resolve_model
    full = "资" * 5000
    big = DocPart(source_id="s9", title="讲义", text=full)
    r = req(TextPart(text="附言"), img(), big)
    payload, texts = model_payload(r, ChatTurn(text="答"), None)
    stored = json.dumps([payload, texts], ensure_ascii=False)
    assert "QUJDQUJD" not in stored and full[:1000] not in stored
    step = resolve_model(payload, texts)
    parts = step["messages"][0]["parts"]
    assert parts[0] == {"kind": "text", "text": "附言"}
    assert parts[1] == {"kind": "image", "media_type": "image/png", "sha256": SHA_A,
                        "data_b64": "", "name": "a.png"}
    assert parts[2]["source_id"] == "s9" and parts[2]["title"] == "讲义"
    assert parts[2]["text"].startswith("资" * 200) and "source_id=s9" in parts[2]["text"]
    assert "共 5000 字" in parts[2]["text"] and len(parts[2]["text"]) < 300
    with pytest.raises(NotReplayable):
        request_from(step)
    # a request without parts stores and replays exactly as before
    plain = req()
    p2, t2 = model_payload(plain, None, None)
    assert request_from(resolve_model(p2, t2)) == plain
