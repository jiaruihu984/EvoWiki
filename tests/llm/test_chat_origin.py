import pytest
from pydantic import ValidationError

from evowiki.llm.chat import MEMORY_FENCE_HEAD, ChatMessage, ChatRequest, chat_input_hash
from evowiki.llm.models import ModelRef
from evowiki.llm.providers import anthropic_messages, deepseek_messages

REF = ModelRef(provider="deepseek", model="deepseek-chat")


def req(*msgs):
    return ChatRequest(purpose="assistant.turn", prompt_version="v", subject="s", system="sys",
                       messages=msgs, tools=())


def test_user_origin_hashes_exactly_as_before():
    old = req(ChatMessage(role="user", text="hi"))
    # the hash a request without the field had (the dump minus origin), pinned
    assert chat_input_hash(REF, old) == chat_input_hash(REF, req(ChatMessage(role="user", text="hi",
                                                                            origin="user")))
    assert chat_input_hash(REF, old) == (
        "c73d450cdaaf8d20c87c9439a3a5179d4117e33e04052cea1bd7f470c73148fd")


def test_memory_origin_changes_the_hash():
    a = req(ChatMessage(role="user", text="x"))
    b = req(ChatMessage(role="user", text="x", origin="memory"))
    assert chat_input_hash(REF, a) != chat_input_hash(REF, b)


def test_origin_only_on_user_messages():
    with pytest.raises(ValidationError):
        ChatMessage(role="assistant", text="x", origin="memory")


def test_deepseek_joins_memory_fence_first_into_the_next_user_message():
    out = deepseek_messages(req(ChatMessage(role="user", text="- [2026-10-27 当时的决定] 甲",
                                            origin="memory"),
                                ChatMessage(role="user", text="问题")))
    assert [m["role"] for m in out] == ["system", "user"]
    assert out[1]["content"].startswith(MEMORY_FENCE_HEAD)
    assert out[1]["content"].endswith("<历史资料>\n- [2026-10-27 当时的决定] 甲\n</历史资料>\n\n问题")


def test_anthropic_merges_memory_into_the_next_user_message_fence_first():
    out = anthropic_messages(req(ChatMessage(role="user", text="早先"),
                                 ChatMessage(role="assistant", text="答"),
                                 ChatMessage(role="user", text="甲", origin="memory"),
                                 ChatMessage(role="user", text="问题")))
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    blocks = out[2]["content"]
    assert len(blocks) == 2 and blocks[0]["text"].startswith(MEMORY_FENCE_HEAD)
    assert "<历史资料>\n甲\n</历史资料>" in blocks[0]["text"]
    assert blocks[1] == {"type": "text", "text": "问题"}
