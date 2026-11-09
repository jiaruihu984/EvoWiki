"""The merge hook's scripted 助手 turn calls merge_pages with arguments the tool accepts, ids taken
from the context pack. No LLM, no database."""

from evowiki.assistant.tools.actions import MergePagesArgs
from evowiki.llm.chat import ChatMessage, ChatRequest
from tests.e2e.merge_script import merge_turn

PACK = "他的 Wiki 里相关的页面：\n- [样本均值](#p:pg_0009)：概念 n1\n- [抽样分布](#p:pg_0012)：概念 n2"


def _req(*msgs: ChatMessage) -> ChatRequest:
    return ChatRequest(purpose="assistant.turn", prompt_version="t", subject="s", system="",
                       messages=msgs)


def test_the_merge_call_names_the_pages_by_id_and_is_valid():
    out = merge_turn(_req(ChatMessage(role="user", text=f"{PACK}\n\n把「样本均值」和「抽样分布」合成一页，叫「抽样」")))
    (call,) = out.tool_calls
    args = MergePagesArgs.model_validate(call.args)
    assert call.name == "merge_pages" and args.pages == ["pg_0009", "pg_0012"] and args.title == "抽样"


def test_after_the_tool_one_sentence_and_unknown_pages_are_not_scripted():
    said = ChatMessage(role="user", text=f"{PACK}\n\n把「样本均值」和「抽样分布」合成一页")
    assert merge_turn(_req(said, ChatMessage(role="tool", text="ok"))).text
    assert merge_turn(_req(ChatMessage(role="user", text="把「甲」和「乙」合成一页"))) is None
    assert merge_turn(_req(ChatMessage(role="user", text="你好"))) is None
