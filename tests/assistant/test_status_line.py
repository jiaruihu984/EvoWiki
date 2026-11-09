"""The assistant's status line: one plain line per tool call, streamed as the call starts."""

import re

from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import run_turn
from evowiki.assistant.status_line import (
    FALLBACK,
    LINES,
    NO_NAMES,
    StatusNames,
    attachments_line,
    status_line,
)
from evowiki.harness.tool_names import TOOLS
from evowiki.llm.chat import ChatTurn, ToolCall
from tests.assistant.fakes import FakeTool
from tests.assistant.test_loop import INFO, setup

NAMES = StatusNames(pages={"p1": "基因配对", "p2": "均值"}, current_page="p1",
                    sections={"s1": 1, "s2": 2}, sources={"src1": "线性代数讲义.pdf"})


def test_every_assistant_tool_has_its_own_plain_line():
    assert set(LINES) == set(TOOLS["assistant"])
    for name in TOOLS["assistant"]:
        for names in (NO_NAMES, NAMES):
            line = status_line(name, {}, names)
            assert line.startswith("正在") and line != FALLBACK, name
            # no internals: never the tool's identifier, never an ascii identifier at all
            assert name not in line and not re.search(r"[a-z]+_[a-z]+", line), (name, line)


def test_arguments_fill_in_titles_sections_and_queries():
    assert status_line("read_page", {"section_id": "s2"}, NAMES) == "正在读《基因配对》第 2 节"
    assert status_line("read_page", {}, NAMES) == "正在读《基因配对》"
    assert status_line("read_page", {"page_id": "p2"}, NAMES) == "正在读《均值》"
    # a section number is only known for the current page; an unknown page is never shown by id
    assert status_line("read_page", {"page_id": "p2", "section_id": "s2"}, NAMES) == "正在读《均值》"
    assert status_line("read_page", {"page_id": "pg_9f3"}, NAMES) == "正在读页面"
    assert status_line("read_page", {}, NO_NAMES) == "正在读页面"
    assert status_line("search_wiki", {"query": "碱基配对"}, NAMES) == "正在搜索：碱基配对"
    assert status_line("search_wiki", {}, NAMES) == "正在你的 Wiki 里查找"
    assert status_line("web_search", {"query": "z 分数"}, NAMES) == "正在网上搜索：z 分数"
    assert status_line("fetch_url", {"url": "https://zh.wikipedia.org/wiki/x"}, NAMES) == \
        "正在打开网页：zh.wikipedia.org"
    assert status_line("fetch_url", {"url": 3}, NAMES) == "正在打开网页"
    assert status_line("read_source", {"source_id": "src1", "page_from": 3, "page_to": 5},
                       NAMES) == "正在读《线性代数讲义.pdf》第 3–5 页"
    assert status_line("read_source", {"source_id": "nope"}, NAMES) == "正在读资料"
    assert status_line("read_source", {"source_id": "nope", "page_from": 3, "precise": True},
                       NAMES) == "正在精确读资料第 3 页"
    assert status_line("search_source", {"query": "特征值", "source_id": "src1"}, NAMES) == \
        "正在《线性代数讲义.pdf》里搜索：特征值"
    assert status_line("search_source", {"query": "特征值"}, NAMES) == "正在资料里搜索：特征值"
    assert status_line("propose_page_edit", {"page_id": "p1"}, NAMES) == "正在写草稿"
    assert status_line("split_page", {"page": "p2"}, NAMES) == "正在准备拆分《均值》"
    assert status_line("split_page", {"page": "n3"}, NAMES) == "正在准备拆分页面"
    assert status_line("write_page", {"title": "协方差"}, NAMES) == "正在准备写新的一页《协方差》"
    assert status_line("pause_goal", {"paused": False}, NAMES) == "正在准备继续学习目标"
    assert status_line("edit_goal_blocks", {"ops": []}, NAMES) == "正在调整学习目标的分块"


def test_long_or_odd_arguments_stay_one_short_line():
    line = status_line("search_wiki", {"query": "很长的\n问题" * 20}, NAMES)
    assert "\n" not in line and len(line) <= len("正在搜索：") + 25 and line.endswith("…")
    assert status_line("search_wiki", {"query": ["x"]}, NAMES) == "正在你的 Wiki 里查找"
    assert status_line("read_page", "not a dict", NAMES) == "正在读《基因配对》"   # type: ignore[arg-type]


def test_unknown_and_forged_tools_say_the_generic_line():
    for name in ("call_tool", "find_tool", "request_tool", "my_forged_tool", ""):
        assert status_line(name, {"x": 1}, NAMES) == FALLBACK == "正在处理…"


def test_attachments_line():
    assert attachments_line(["线性代数讲义.pdf"]) == "正在读《线性代数讲义.pdf》"
    assert attachments_line(["a", "b", "c"]) == "正在读你发的 3 份资料"
    assert attachments_line([""]) == "正在读你发的资料"


def test_the_turn_streams_a_status_line_as_each_call_starts(tmp_path):
    ts = [FakeTool("read_page", "read"), FakeTool("search_wiki", "read"),
          FakeTool("web_search", "network"), FakeTool("propose_page_edit", "propose"),
          FakeTool("my_forged_tool", "read")]
    deps, _, _ = setup(tmp_path, [
        ChatTurn(text="先看看。", tool_calls=(ToolCall(id="c1", name="read_page",
                                                     args={"section_id": "s2"}),)),
        ChatTurn(tool_calls=(ToolCall(id="c2", name="search_wiki", args={"query": "碱基配对"}),
                             ToolCall(id="c3", name="web_search", args={"query": "z"}))),
        ChatTurn(tool_calls=(ToolCall(id="c4", name="read_page", args={"page_id": "p2"}),
                             ToolCall(id="c5", name="my_forged_tool", args={}))),
        ChatTurn(text="答")], ts=ts)
    ev = list(run_turn(deps, INFO, MessageIn(text="问")))
    # the current page's title and section number (fakes.PAGE: 「z 分数」, s2 = 2nd section),
    # another page by the context pack's digest (p2 = 「均值」), in the order the calls start
    # a read / network call's running line is its row's label (tool_start); the drafting step's is a
    # status line (its card follows)
    assert [e.label if e.type == "tool_start" else e.text for e in ev
            if e.type in ("status", "tool_start")] == [
        "正在读《z 分数》第 2 节", "正在搜索：碱基配对", "正在网上搜索：z", "正在读《均值》",
        "正在处理…"]
    kinds = [e.type for e in ev]
    # each row ends with what was done (the asking call's own text is narration: not shown)
    assert kinds[:2] == ["tool_start", "tool_end"] and kinds.count("text_delta") == 1
    assert ev[1].label == "读了《z 分数》第 2 节" and ev[1].group == "read_page"
    assert kinds[-1:] == ["done"]                       # no observer step any more (stage 3: record_evidence)
    assert not any(e.type == "status" for e in deps.store.entries(INFO.session_id))
