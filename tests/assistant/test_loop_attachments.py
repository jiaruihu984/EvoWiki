"""Chat attachments through a whole turn: routing by who can see, the Claude hand-off, budget,
history, the session log and the HTTP edges. Scripted providers only (no paid calls)."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from evowiki.assistant.attachments import (
    GONE_DETAIL,
    NEEDS_VISION,
    ONLY_FILES_ASK,
    STATEMENT_HEAD,
    AttachmentGone,
    AttachmentReading,
    MemoryAttachmentReader,
)
from evowiki.assistant.contracts import MAX_ATTACHMENTS, MessageIn
from evowiki.assistant.loop import SYSTEM, TurnDeps, run_turn
from evowiki.assistant.service import AssistantService
from evowiki.assistant.session import MemorySessionStore
from evowiki.assistant.turn_runs import AssistantUnavailable
from evowiki.llm.chat import ChatTurn
from evowiki.llm.gateway import Gateway, ProviderUnavailableError
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.providers import anthropic_messages, cache_marked_messages
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.service.app import create_app
from tests.assistant.attach_fakes import doc, image, png, reader
from tests.assistant.fakes import INFO, T0, Data, Ids, tools

DS = ModelRef(provider="deepseek", model="deepseek-chat")
CL = ModelRef(provider="anthropic", model="claude-sonnet-5-5")
FIGURE_PDF = Path(__file__).parents[1] / "fixtures" / "sources" / "figure_page.pdf"
PNG = png()


class Applier:
    def apply(self, user_id, draft):
        raise AssertionError("not used")


def gateway(tmp_path, ref, script, name):
    outs = iter(script)

    def answer(m, r):
        out = next(outs)
        if isinstance(out, BaseException):
            raise out
        return out
    prov = ScriptedChatProvider(ref.provider, answer)
    gw = Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
                 CallLog(tmp_path / name), mode="record", providers={ref.provider: prov})
    return gw, prov


def setup(tmp_path, items, *, route="deepseek", script=(), eyes=None,
          wired=True):
    gw, prov = gateway(tmp_path, CL if route == "anthropic" else DS, list(script), "c.jsonl")
    eyes_gw = eprov = None
    if eyes is not None:
        eyes_gw, eprov = gateway(tmp_path, CL, list(eyes), "e.jsonl")
    store = MemorySessionStore()
    store.create(INFO)
    extra = {"attachments": reader(*items), "sees_images": lambda acct: route == "anthropic",
             "eyes": eyes_gw} if wired else {}
    deps = TurnDeps(gateway=gw, tools=tools(), store=store, data=NoPageText(),
                    new_id=Ids(), clock=lambda: T0, **extra)
    return deps, prov, eprov


def turn(deps, text="这是什么？", ids=()):
    return list(run_turn(deps, INFO, MessageIn(text=text, attachments=ids)))


class NoPageText(Data):
    """These tests read the attachment parts: the 当前页's own part is test_loop's."""

    def page(self, user_id, page_id):
        p = super().page(user_id, page_id)
        return p and p.model_copy(update={"sections": ()})


def kinds(msg):
    return [p.kind for p in msg.parts]


def logged(deps) -> str:
    return json.dumps([e.payload for e in deps.store.entries("s1")], ensure_ascii=False)


def test_claude_route_sends_the_image_itself(tmp_path):
    deps, prov, _ = setup(tmp_path, [(image("pic"), PNG)], route="anthropic",
                          script=[ChatTurn(text="一个红色方块")])
    ev = turn(deps, ids=("pic",))
    last = prov.chat_calls[0].messages[-1]
    assert kinds(last) == ["image"] and last.parts[0].name == "pic.png"
    assert last.text.startswith(STATEMENT_HEAD) and "图片 · 已给出原图" in last.text
    done = ev[-1]
    assert done.type == "done" and "note" not in done.model_dump()
    assert done.attachments[0].source_id == "pic" and done.attachments[0].read.mode == "image"


def test_deepseek_route_gets_the_ocr_text_not_the_image(tmp_path):
    deps, prov, _ = setup(tmp_path, [(image("pic", ocr="三角形内角和是 180°"), PNG)],
                          script=[ChatTurn(text="讲的是内角和")])
    ev = turn(deps, ids=("pic",))
    last = prov.chat_calls[0].messages[-1]
    assert kinds(last) == ["doc"] and "三角形内角和是 180°" in last.parts[0].text
    assert "只有识别出的文字" in last.text and ev[-1].type == "done"


def test_deepseek_route_figure_goes_to_eyes_and_nothing_tells_the_user(tmp_path):
    deps, prov, eprov = setup(tmp_path, [(image("fig", figure=True, ocr="y"), PNG)],
                              eyes=[ChatTurn(text="这是一条抛物线")])
    ev = turn(deps, ids=("fig",))
    assert prov.chat_calls == [] and len(eprov.chat_calls) == 1
    assert kinds(eprov.chat_calls[0].messages[-1]) == ["image"]
    assert ev[-1].type == "done" and "note" not in ev[-1].model_dump()
    assert "".join(e.text for e in ev if e.type == "text_delta") == "这是一条抛物线"
    entries = deps.store.entries("s1")
    assert entries[0].payload["attachments"] == ["fig"]
    answer = next(e for e in entries if e.type == "assistant")
    assert answer.payload["reads"]["fig"]["mode"] == "image" and "note" not in answer.payload
    assert base64.b64encode(PNG).decode()[:40] not in logged(deps)


def test_no_eyes_is_needs_vision_without_any_model_call(tmp_path):
    deps, prov, _ = setup(tmp_path, [(image("fig", figure=True), PNG)])
    ev = turn(deps, ids=("fig",))
    assert prov.chat_calls == []
    err = ev[-1]
    assert (err.type, err.code, err.detail, err.retryable) == (
        "error", "needs_vision", NEEDS_VISION, False)
    te = next(e for e in deps.store.entries("s1") if e.type == "turn_error")
    assert te.payload["code"] == "needs_vision"


def test_eyes_failure_is_needs_vision(tmp_path):
    deps, prov, eprov = setup(tmp_path, [(image("fig", figure=True), PNG)],
                              eyes=[ProviderUnavailableError("claude down")])
    ev = turn(deps, ids=("fig",))
    assert prov.chat_calls == [] and len(eprov.chat_calls) == 1
    assert ev[-1].code == "needs_vision" and ev[-1].detail == NEEDS_VISION


def test_over_budget_the_larger_file_becomes_a_table_of_contents(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1500")
    small, big = doc("a", ["小" * 300]), doc("b", ["大" * 2000, "二\n后文"])
    deps, prov, _ = setup(tmp_path, [(small, None), (big, None)], script=[ChatTurn(text="答")])
    ev = turn(deps, "比较这两份", ("a", "b"))
    last = prov.chat_calls[0].messages[-1]
    assert kinds(last) == ["doc", "doc"]
    assert "小" * 300 in last.parts[0].text and "大" * 2000 not in last.parts[1].text
    assert "read_source" in last.parts[1].text
    assert [c.read.mode for c in ev[-1].attachments] == ["full", "toc"]
    assert "只给了目录（2 节）" in last.text
    assert "没用 read_source 读过就只能说只看了目录和摘要" in prov.chat_calls[0].system
    assert "没用 read_source 读过就只能说只看了目录和摘要" in SYSTEM


def test_only_attachments_asks_what_it_is(tmp_path):
    deps, prov, _ = setup(tmp_path, [(doc("a", ["线性代数讲义"]), None)],
                          script=[ChatTurn(text="这是一份线性代数讲义"), ChatTurn(text="好")])
    ev = turn(deps, "", ("a",))
    assert ONLY_FILES_ASK in prov.chat_calls[0].messages[-1].text
    assert "observed" not in [e.type for e in ev] and ev[-1].type == "done"
    turn(deps, "第一章讲什么")
    hist = prov.chat_calls[1].messages[0]
    assert hist.text == ONLY_FILES_ASK and kinds(hist) == ["doc"]


def test_second_turn_resends_the_same_parts_before_the_cache_breakpoint(tmp_path):
    items = [(doc("a", ["讲义正文"]), None), (image("pic"), PNG)]
    deps, prov, _ = setup(tmp_path, items, route="anthropic",
                          script=[ChatTurn(text="答一"), ChatTurn(text="答二")])
    turn(deps, "看看", ("a", "pic"))
    ev = turn(deps, "再问")
    first, second = prov.chat_calls
    assert second.messages[0].parts == first.messages[-1].parts
    assert second.messages[0].text == "看看" and second.messages[-1].parts == ()
    assert STATEMENT_HEAD in second.messages[-1].text and "source_id=pic" in second.messages[-1].text
    marked = cache_marked_messages(anthropic_messages(second))
    blocks = marked[0]["content"]
    assert [b["type"] for b in blocks] == ["text", "text", "image", "text"]
    assert "cache_control" in blocks[-1] and not any("cache_control" in b for b in blocks[:-1])
    assert ev[-1].attachments == ()                       # nothing attached to this message
    assert base64.b64encode(PNG).decode()[:40] not in logged(deps)


def test_claude_route_pdf_figure_pages_go_as_page_images(tmp_path):
    pdf = doc("d", ["第一页的字", "第二页的字"], figures=(2,))
    deps, prov, _ = setup(tmp_path, [(pdf, FIGURE_PDF.read_bytes())], route="anthropic",
                          script=[ChatTurn(text="答")])
    ev = turn(deps, ids=("d",))
    last = prov.chat_calls[0].messages[-1]
    assert kinds(last) == ["doc", "image"] and last.parts[1].name == "d.pdf 第 2 页"
    assert "第 2 页是图，已给出页面图片" in last.text
    assert ev[-1].attachments[0].read.mode == "full"


def test_unanswered_turns_attachment_moves_to_the_current_message(tmp_path):
    deps, prov, _ = setup(tmp_path, [(doc("a", ["正文"]), None)],
                          script=[ProviderUnavailableError("down"), ChatTurn(text="好")])
    assert turn(deps, "看看", ("a",))[-1].type == "error"
    turn(deps, "再试")
    last = prov.chat_calls[1].messages[-1]
    assert kinds(last) == ["doc"] and len(prov.chat_calls[1].messages) == 1


def test_a_picture_no_model_could_see_does_not_block_a_later_message_about_something_else(
        tmp_path):
    deps, prov, _ = setup(tmp_path, [(image("pic"), PNG)],
                          script=[ChatTurn(text="2"), ChatTurn(text="那张图我看不了")])
    assert turn(deps, "图里写了什么", ("pic",))[-1].code == "needs_vision"
    ev = turn(deps, "忽略那张图，1+1 等于几")
    assert ev[-1].type == "done" and len(prov.chat_calls) == 1
    last = prov.chat_calls[0].messages[-1]
    assert kinds(last) == [] and "这一轮没给出原图，也没有识别出的文字" in last.text
    assert "之前那一轮已经看过" not in last.text
    # asked about it without sending it again: the model is told it could not see it
    assert turn(deps, "那张图呢")[-1].type == "done"
    assert "这一轮没给出原图" in prov.chat_calls[1].messages[-1].text
    # sent again: it is that message's own, and still cannot be seen
    assert turn(deps, "再看看这张", ("pic",))[-1].code == "needs_vision"


# ---- service and HTTP ----

ANSWER = (ChatTurn(text="答"),)


def service(tmp_path, items, script=ANSWER, wired=True):
    deps, _, _ = setup(tmp_path, items, script=script, wired=wired)
    return AssistantService(deps, Applier()), deps


def test_send_refuses_reading_and_foreign_attachments(tmp_path):
    svc, deps = service(tmp_path, [(doc("r", ["x"], status="reading"), None)])
    with pytest.raises(AttachmentReading):
        svc.send("u1", "s1", MessageIn(text="看", attachments=("r",)))
    with pytest.raises(AttachmentGone):
        svc.send("u1", "s1", MessageIn(text="看", attachments=("someone_elses",)))
    assert deps.store.entries("s1") == []


def test_unwired_attachments_fail_closed(tmp_path):
    svc, _ = service(tmp_path, [], wired=False)
    with pytest.raises(AssistantUnavailable):
        svc.send("u1", "s1", MessageIn(text="看", attachments=("a",)))


def test_message_needs_text_or_an_attachment():
    with pytest.raises(ValidationError):
        MessageIn(text="  ", attachments=())
    with pytest.raises(ValidationError):
        MessageIn(text="x", attachments=tuple(f"s{i}" for i in range(MAX_ATTACHMENTS + 1)))
    assert MessageIn(attachments=("a",)).text == ""


def test_http_409_404_422(tmp_path):
    svc, deps = service(tmp_path, [(doc("r", ["x"], status="reading"), None),
                                   (doc("d", ["x"], deleted=True), None)])
    deps.attachments = MemoryAttachmentReader({
        "u1": {"r": (doc("r", ["x"], status="reading"), None),
               "d": (doc("d", ["x"], deleted=True), None)},
        "u2": {"theirs": (doc("theirs", ["x"], user="u2"), None)}})
    c = TestClient(create_app(SimpleNamespace(assistant=svc)))
    h = {"X-EvoWiki-User": "u1"}

    def send(*ids, session="s1"):
        return c.post(f"/assistant/sessions/{session}/messages",
                      json={"text": "看", "attachments": list(ids)}, headers=h)
    r = send("r")
    assert r.status_code == 409 and r.json()["detail"] == "附件还在读，读完再发"
    # deleted, unknown and someone else's: the same 409 (a 404 makes the client forget the
    # session; the same text never tells whether another user's id exists)
    for gone in ("d", "zz", "theirs"):
        r = send(gone)
        assert r.status_code == 409 and r.json()["detail"] == GONE_DETAIL, gone
    assert send("r", session="nope").status_code == 404           # an unknown session
    r = c.post("/assistant/sessions/s1/messages", json={"text": ""}, headers=h)
    assert r.status_code == 422
    assert deps.store.entries("s1") == []


def test_get_session_replays_attachment_cards(tmp_path):
    a = doc("a", ["正文"])
    svc, deps = service(tmp_path, [(a, None)])
    list(svc.send("u1", "s1", MessageIn(text="看看", attachments=("a",))))
    out = svc.get_session("u1", "s1")
    user, answer = out.messages
    card = user.attachments[0]
    assert (card.title, card.status, card.deleted, card.read.mode) == ("a.pdf", "ready", False,
                                                                       "full")
    assert card.expires_at is not None and answer.attachments == ()
    assert "note" not in answer.model_dump()
    deps.attachments = reader((doc("a", ["正文"], deleted=True), None))
    svc2 = AssistantService(deps, Applier())
    card = svc2.get_session("u1", "s1").messages[0].attachments[0]
    assert card.deleted and card.expires_at is None and card.read.mode == "full"
    assert svc2.list_sessions("u1").sessions[0].title == "看看"


# ---- fix round 1 ----

def test_an_older_figure_does_not_send_later_turns_to_eyes(tmp_path):
    deps, prov, eprov = setup(tmp_path, [(image("fig", figure=True), PNG)],
                              script=[ChatTurn(text="还是那条线")],
                              eyes=[ChatTurn(text="这是一条抛物线")])
    assert turn(deps, ids=("fig",))[-1].type == "done"
    ev = turn(deps, "它的顶点在哪？")
    assert ev[-1].type == "done" and len(eprov.chat_calls) == 1 and len(prov.chat_calls) == 1
    req = prov.chat_calls[0]
    assert all(p.kind != "image" for m in req.messages for p in m.parts)
    assert "图片 · 之前那一轮已经看过，见当时的回答" in req.messages[-1].text


def test_older_messages_keep_their_parts_when_new_pictures_arrive(tmp_path):
    pdf = doc("d", ["一", "二"], figures=(1, 2))
    items = [(pdf, FIGURE_PDF.read_bytes()), *((image(f"p{i}"), png()) for i in range(12))]
    deps, prov, _ = setup(tmp_path, items, route="anthropic",
                          script=[ChatTurn(text=f"答{i}") for i in range(3)])
    turn(deps, "看图", ("d",))
    turn(deps, "再看", tuple(f"p{i}" for i in range(5)))
    turn(deps, "还有", tuple(f"p{i}" for i in range(5, 10)))
    _, second, third = prov.chat_calls
    assert second.messages[0].parts == third.messages[0].parts
    assert kinds(second.messages[0]) == ["doc", "image", "image"]
    assert second.messages[-1].parts == third.messages[2].parts
    assert kinds(third.messages[-1]) == ["image"] * 5


def test_attachments_over_the_window_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOWIKI_CONTEXT_WINDOW", "1000")
    deps, prov, _ = setup(tmp_path, [(image("pic"), PNG)], route="anthropic")
    ev = turn(deps, ids=("pic",))
    assert prov.chat_calls == [] and (ev[-1].type, ev[-1].code) == ("error", "bad_request")


def test_attachment_deleted_before_the_turn_runs_says_so_in_chinese(tmp_path):
    deps, prov, _ = setup(tmp_path, [(doc("a", ["x"], deleted=True), None)])
    ev = turn(deps, ids=("a",))
    assert prov.chat_calls == [] and ev[-1].code == "bad_request"
    assert ev[-1].detail == "附件已经删除了，去掉它再发"


def test_a_vanished_attachment_shows_as_deleted_not_a_failed_view(tmp_path):
    svc, deps = service(tmp_path, [(doc("a", ["正文"]), None), (doc("b", ["正文"]), None)])
    list(svc.send("u1", "s1", MessageIn(text="看看", attachments=("a", "b"))))
    deps.attachments = reader((doc("b", ["正文"]), None))          # a's row is gone
    a, b = AssistantService(deps, Applier()).get_session("u1", "s1").messages[0].attachments
    assert a.deleted and a.title == "（已删除的附件）" and not b.deleted     # never the raw id


# ---- fix round 2 ----

def test_old_pictures_give_way_so_the_session_stays_usable(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOWIKI_CONTEXT_WINDOW", "20000")      # limit ≈ 16000 − history
    items = [(image(f"p{i}", ocr=None if i % 2 else f"图{i}的字"), png()) for i in range(20)]
    deps, prov, _ = setup(tmp_path, items, route="anthropic",
                          script=[ChatTurn(text=f"答{i}") for i in range(4)])
    for t in range(4):
        ev = turn(deps, f"第{t}张组", tuple(f"p{i}" for i in range(5 * t, 5 * t + 5)))
        assert ev[-1].type == "done"
    req = prov.chat_calls[3]
    assert kinds(req.messages[-1]) == ["image"] * 5
    assert [c.read.mode for c in ev[-1].attachments] == ["image"] * 5
    assert "image" not in kinds(req.messages[0])                  # the oldest gave way
    assert kinds(req.messages[0]) == ["doc"] * 3                  # p0, p2, p4: their OCR text
    assert "【p1.png · source_id=p1 · 图片 · 之前那一轮已经看过，见当时的回答】" in req.messages[-1].text


def test_current_attachments_alone_over_the_limit_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOWIKI_CONTEXT_WINDOW", "8000")       # limit ≈ 6400: 5 images = 8000
    items = [(image(f"p{i}"), png()) for i in range(5)]
    deps, prov, _ = setup(tmp_path, items, route="anthropic")
    ev = turn(deps, ids=tuple(f"p{i}" for i in range(5)))
    assert prov.chat_calls == [] and ev[-1].code == "bad_request"


def test_budget_follows_the_routed_model_not_the_configured_one(tmp_path, monkeypatch):
    """The account's tier decides the window: a DeepSeek-configured gateway whose route for this
    turn is Anthropic gets Anthropic's window (an 80k document stays whole)."""
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1000000")
    big = doc("big", ["字" * 80_000])
    deps, prov, _ = setup(tmp_path, [(big, None)], script=[ChatTurn(text="好")] * 2)
    turn(deps, ids=("big",))
    assert prov.chat_calls[0].messages[-1].parts[0].text.startswith("（只给了目录")
    routed = []

    def route(role, account=None, purpose=None):
        routed.append(purpose)
        return "anthropic"
    monkeypatch.setattr(deps.gateway, "route_provider", route, raising=False)
    turn(deps, "再看", ("big",))
    assert "整份已给出" in prov.chat_calls[1].messages[-1].text and "assistant.turn" in routed


# --- T15: chat-first intent ---------------------------------------------------------------------

class _ActTool:
    """An action tool by name only; `params`: its argument names (a fake ToolSpec)."""

    def __init__(self, name, params=()):
        from evowiki.agent.tools import ToolSpec
        self.spec = ToolSpec(name=name, description=f"{name} 工具", effect="propose", acts=True,
                             parameters={"type": "object",
                                         "properties": {p: {"type": "string"} for p in params}})

    def run(self, args, ctx):
        raise AssertionError("not called")


def _sources_rules() -> str:
    from evowiki.assistant.skills import library
    s = library().get("sources")
    assert s is not None
    return s.rules


def test_attachment_rules_only_in_turns_of_a_session_with_attachments(tmp_path):
    """Stage 3: the attachment rules are the sources Skill's, preloaded in a turn of a session
    with attachments; a turn without action tools has no Skills (ATTACHMENT_PLAIN is gone)."""
    from evowiki.assistant.loop import PROMPT_VERSION
    deps, prov, _ = setup(tmp_path, [(doc("a", ["讲义"]), None)],
                          script=[ChatTurn(text="答"), ChatTurn(text="答"), ChatTurn(text="答")])
    deps.tools = [*tools(), _ActTool("new_goal", ("goal", "source_ids")),
                  _ActTool("mark_source_use")]
    turn(deps, "随便问问")                                     # no attachment yet
    assert "用户放了附件时" not in prov.chat_calls[0].system
    turn(deps, "这份讲义讲什么", ("a",))
    assert _sources_rules() in prov.chat_calls[1].system
    assert "+sources@" in prov.chat_calls[1].prompt_version
    deps.tools = tools()                                      # no action tools: no Skills
    turn(deps, "再问问", ())
    from evowiki.assistant.loop import ATTACHMENT_PLAIN  # no Skills: the short rules
    assert ATTACHMENT_PLAIN in prov.chat_calls[2].system and "mark_source_use" not in prov.chat_calls[2].system
    assert prov.chat_calls[2].prompt_version == PROMPT_VERSION == "assistant@v80"


def test_action_turns_get_the_intent_rules_and_the_source_ids_line(tmp_path):
    deps, prov, _ = setup(tmp_path, [(doc("a", ["讲义"]), None)], script=[ChatTurn(text="答")])
    deps.tools = [*tools(), _ActTool("new_goal", ("goal", "source_ids")),
                  _ActTool("mark_source_use")]
    turn(deps, "下周考这个", ("a",))
    system = prov.chat_calls[0].system
    assert _sources_rules() in system
    assert "mark_source_use（use=exam）" in system and "不提 OCR、模型切换、导入步骤" in system
    assert "调 new_goal 时把这些附件的 source_id 放进 source_ids。" in system
    # the message's own attachments preload plan-goal too (a goal from the file)
    assert "+plan-goal@" in prov.chat_calls[0].prompt_version


def test_only_attachments_also_asks_what_he_wants_to_do_with_it():
    assert "说这份附件是什么" in ONLY_FILES_ASK and "再用一句话问他想拿它做什么" in ONLY_FILES_ASK


def test_done_shows_the_uses_an_action_of_the_turn_added(tmp_path):
    """mark_source_use (or a goal from the file) adds a use: done's card says it is kept (no expiry),
    so the plugin follows the import it started."""
    from dataclasses import replace

    from evowiki.agent.tools import ActionCard, ToolResult
    from evowiki.llm.chat import ToolCall
    att = doc("a", ["往年卷"])
    deps, _, _ = setup(tmp_path, [(att, None)], script=[
        ChatTurn(tool_calls=(ToolCall(id="c1", name="mark_source_use",
                                      args={"source_id": "a", "use": "exam"}),)),
        ChatTurn(text="已把《a.pdf》记为往年题。")])

    class Mark(_ActTool):
        def run(self, args, ctx):
            deps.attachments._data["u1"]["a"] = (replace(att, uses=("ask", "exam")), None)
            return ToolResult(content="已执行", action=ActionCard(
                action_id="act1", kind="source_use", title="把《a.pdf》记为往年题", status="executed"))
    deps.tools = [*tools(), Mark("mark_source_use", ("source_id", "use"))]
    ev = turn(deps, "这是往年题", ("a",))
    card = ev[-1].attachments[0]
    assert ev[-1].type == "done" and tuple(card.uses) == ("ask", "exam") and card.expires_at is None


def test_after_an_action_done_carries_the_sessions_earlier_attachments_as_they_are_now(tmp_path):
    """「刚才那份是往年题」: the use goes to an attachment of an earlier message; done's
    session_attachments has it (kept, importing), the current message has none."""
    from dataclasses import replace

    from evowiki.agent.tools import ActionCard, ToolResult
    from evowiki.llm.chat import ToolCall
    att = doc("a", ["往年卷"])
    deps, _, _ = setup(tmp_path, [(att, None)], script=[
        ChatTurn(text="这是一份卷子"),
        ChatTurn(tool_calls=(ToolCall(id="c1", name="mark_source_use",
                                      args={"source_id": "a", "use": "exam"}),)),
        ChatTurn(text="已把《a.pdf》记为往年题。"), ChatTurn(text="好")])

    class Mark(_ActTool):
        def run(self, args, ctx):
            running = replace(att.source, progress={"stage": "parse", "done": 0, "total": 1})
            deps.attachments._data["u1"]["a"] = (replace(att, source=running, uses=("ask", "exam")),
                                                 None)
            return ToolResult(content="已执行", action=ActionCard(
                action_id="act1", kind="source_use", title="把《a.pdf》记为往年题", status="executed"))
    deps.tools = [*tools(), Mark("mark_source_use", ("source_id", "use"))]
    first = turn(deps, "看看", ("a",))
    assert first[-1].session_attachments == () and first[-1].attachments[0].importing is False
    ev = turn(deps, "刚才那份是往年题")
    done = ev[-1]
    assert done.attachments == ()
    [earlier] = done.session_attachments
    assert earlier.source_id == "a" and tuple(earlier.uses) == ("ask", "exam")
    assert earlier.expires_at is None and earlier.importing is True
    plain = turn(deps, "第一题怎么做")                      # no action: nothing re-read
    assert plain[-1].session_attachments == ()


# ---- follow-up A: the eyes hand-off sends only what the picture turn needs ----

def test_eyes_request_has_the_last_two_turns_as_text_and_only_this_messages_pictures(tmp_path):
    from evowiki.llm.providers import AnthropicProvider
    items = [(doc("a", ["讲义正文"]), None), (image("old", ocr="旧图的字"), PNG),
             (image("fig", figure=True), PNG)]
    deps, prov, eprov = setup(tmp_path, items,
                              script=[ChatTurn(text=f"答{i}") for i in range(3)],
                              eyes=[ChatTurn(text="这是一条抛物线")])
    turn(deps, "这份讲义讲了什么", ("a",))
    turn(deps, "这张图呢", ("old",))
    turn(deps, "再问一句")
    ev = turn(deps, "这道题怎么做", ("fig",))
    assert ev[-1].type == "done" and len(prov.chat_calls) == 3 and len(eprov.chat_calls) == 1
    req = eprov.chat_calls[0]
    assert req.system == prov.chat_calls[-1].system                    # same system prompt
    assert [(m.role, m.text) for m in req.messages[:-1]] == [
        ("user", "这张图呢"), ("assistant", "答1"), ("user", "再问一句"), ("assistant", "答2")]
    assert all(m.parts == () for m in req.messages[:-1])               # no older attachment parts
    last = req.messages[-1]
    assert kinds(last) == ["image"] and last.parts[0].name == "fig.png"
    assert last.text.startswith(STATEMENT_HEAD)
    assert "【a.pdf · source_id=a · 在之前的消息里给过，这一轮没有带上" in last.text
    assert "【old.png · source_id=old · 在之前的消息里给过" in last.text
    assert "整份已给出" not in last.text
    assert req.cache is False and all(r.cache for r in prov.chat_calls)
    assert req.tools == prov.chat_calls[-1].tools                       # tools stay available

    class _Client:
        def __init__(self):
            self.kwargs = None
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

        def _create(self, **kw):
            self.kwargs = kw
            return SimpleNamespace(model="claude-sonnet-5-5", stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text="好")],
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    c = _Client()
    AnthropicProvider(client=c).chat("claude-sonnet-5-5", req)
    assert "cache_control" not in json.dumps(c.kwargs, default=str)
    c2 = _Client()
    AnthropicProvider(client=c2).chat("claude-sonnet-5-5", prov.chat_calls[-1])
    assert "cache_control" in json.dumps(c2.kwargs, default=str)       # normal turns: cached


def test_eyes_request_is_the_same_on_a_rerun_of_the_same_session(tmp_path):
    """The reduced history is a pure function of the logged session: building the same turn
    again over the same log gives the same request (what a durable replay relies on)."""
    from evowiki.llm.chat import chat_input_hash
    items = [(doc("a", ["讲义正文"]), None), (image("fig", figure=True), PNG)]
    reqs = []
    for k in range(2):
        (tmp_path / str(k)).mkdir()
        deps, _, eprov = setup(tmp_path / str(k), items, script=[ChatTurn(text="答0")],
                                  eyes=[ChatTurn(text="抛物线")])
        turn(deps, "这份讲义讲了什么", ("a",))
        turn(deps, "这道题怎么做", ("fig",))
        reqs.append(eprov.chat_calls[0])
    assert chat_input_hash(CL, reqs[0]) == chat_input_hash(CL, reqs[1])


# ---- follow-up B: which attachment 「这份 / 刚才那份」 means ----

def test_turn_statement_numbers_the_sessions_attachments_and_marks_the_latest(tmp_path):
    items = [(doc("lec", ["讲义"], title="Eigenvalues (lecture notes)"), None),
             (doc("big", ["书"], title="Understanding Linear Algebra"), None)]
    deps, prov, _ = setup(tmp_path, items, script=[ChatTurn(text=f"答{i}") for i in range(3)])
    # stage 3: the reference rule is the sources Skill's, so the turn has action tools
    deps.tools = [*tools(), _ActTool("new_goal", ("goal", "source_ids")),
                  _ActTool("mark_source_use")]
    turn(deps, "这份讲义讲了什么", ("lec",))
    turn(deps, "Example 4.2.4 呢", ("big",))
    turn(deps, "把这份讲义收进资料，按它学")
    text = prov.chat_calls[2].messages[-1].text
    assert "#1（随第 1 条消息，PDF）【Eigenvalues (lecture notes) · source_id=lec" in text
    assert "#2（随第 2 条消息，PDF，最近的）【Understanding Linear Algebra · source_id=big" in text
    system = prov.chat_calls[2].system
    for rule in ("用户说「这份 / 刚才那份 / 这个文件」时，指的是最近一次上传或最近一次提到的那份",
                 "标题或用途（讲义 / 论文 / 往年题）能对上的优先",
                 "先用一句话问是哪一份，不要猜", "只是提问时可以都看"):
        assert rule in system


def test_action_turns_carry_the_reference_rule_too(tmp_path):
    deps, prov, _ = setup(tmp_path, [(doc("a", ["讲义"]), None)], script=[ChatTurn(text="答")])
    deps.tools = [*tools(), _ActTool("new_goal", ("goal", "source_ids")),
                  _ActTool("mark_source_use")]
    turn(deps, "按这份学", ("a",))
    assert "最近一次上传或最近一次提到的那份" in prov.chat_calls[0].system
