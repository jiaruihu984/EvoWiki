"""Disposable backend for the page-chat walkthrough in REAL Obsidian (driver
obsidian-plugin/scripts/real-obsidian/page-chat-walkthrough.mjs). Test-only, no paid call.

The audit backend (build_services on a fresh Postgres, DBOS, every model a scripted fake) whose chat
provider streams slowly, with two more scripted turns on the page's end chips:
HARD (理解有困难) → answers at once, in Markdown with a formula and a quote; SHALLOW (内容太简单) →
read_page with a made-up id (fails), read_page (works), then answers; WRITE → write_page, for the
page to chat about. Anything else goes to the audit backend's own script.

  uv run python -m tests.e2e.page_chat_backend --port 8817 --user page-chat-user
"""

import time

from evowiki.llm.chat import ChatRequest, ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.scripted import ScriptedChatProvider
from tests.e2e import audit_backend as ab

HARD = "这页理解有困难"
SHALLOW = "这页内容太简单"
WRITE = "写一页度与度分布"
ANSWER = ("卡在这里很正常。**度**就是一个点连着几条边。\n\n"
          "> 第 $i$ 个点的度写成 $k_i$，它是一个**整数**。\n\n"
          "比如一个点连着三条边，那 $k_i = 3$。再看一个例子：两个点之间只连一条边，"
          "它们的度都是 $1$。这样数下来，**每一条边**都会被数两次。")
_inner = ab.assistant_turn


def assistant_turn(script, request: ChatRequest) -> ChatTurn:
    said, tools = ab._said(request)
    q = said.rsplit("他问：\n", 1)[-1].split("\n\n已载入：", 1)[0].strip()
    if q == WRITE:
        if "write_page" not in [t.name for t in request.tools]:
            return ChatTurn(tool_calls=(ToolCall(id="r", name="read_ref", args={"path": "edit-wiki/path"}),))
        if not any("草稿" in t for t in tools):
            return ChatTurn(tool_calls=(ToolCall(id="w", name="write_page", args={"title": "度与度分布"}),))
        return ChatTurn(text="草稿在卡片上。")
    if q not in (HARD, SHALLOW):
        return _inner(script, request)
    ab.CHAT.append({"page_chat": q, "tools_so_far": tools,
                    "parts": [p.text for p in request.messages[-1].parts] if not tools else []})
    if q == HARD or len(tools) == 2:
        return ChatTurn(text=ANSWER)
    args = {"page_id": "pg_?关于这一页"} if not tools else {}
    return ChatTurn(tool_calls=(ToolCall(id=f"r{len(tools)}", name="read_page", args=args),))


def assistant_gateway(script) -> Gateway:
    providers = {p: ScriptedChatProvider(p, lambda _m, r: assistant_turn(script, r),
                                         complete=script._responder(p), stream_chunk=6,
                                         on_chunk=lambda _i: time.sleep(0.12))
                 for p in ab.PROVIDERS}
    return Gateway(ab.GW_CONFIG, script.log, mode="record", clock=lambda: ab.T0,
                   providers=providers, record_max_attempts=1)


def build_script():
    # a new conversation closes the last one, which consolidates its memory: nothing to keep here
    s = _build_script()
    s.always("assistant.consolidate", lambda _prompt: {"items": []})
    s.always("assistant.compact", lambda _prompt: {
        "handoff": {"summary": "聊了这一页", "decisions": [], "active_requests": [], "pending": []},
        "learning": {"unclear": [], "understood": []}})
    return s


_build_script = ab.build_script
ab.build_script = build_script
ab.assistant_turn = assistant_turn
ab.assistant_gateway = assistant_gateway

if __name__ == "__main__":
    ab.main()
