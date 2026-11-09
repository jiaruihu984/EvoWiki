"""deep@v17: what the writer used to read first (spec items, the first exemplar chunk, the page's K)
is in the write prompt, so the first reply can write the first section (live run 2026-10-21: every
page began with 4–5 read calls, two turns before any section)."""

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.deep.agent import WRITE_SYSTEM, write_prompt
from evowiki.deep.tools import (
    READ_STUB,
    WRITE_STUB,
    ReadSpecTool,
    Workspace,
    WriteSectionTool,
    preread_k,
    preread_spec,
)
from evowiki.goals.contracts import split_cell
from evowiki.wikiapp.repo import MemoryRepo
from tests.deep.fakes import planned
from tests.deep.test_writer import outline_input
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_page import organized

CTX = ToolContext(user_id="u1", session_id="t")


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def test_the_write_prompt_carries_what_the_opening_reads_returned(env):
    inp = outline_input(env, organized(env))
    text = write_prompt(inp, (), planned(inp))
    tool = ReadSpecTool(inp.teaching)
    assert tool.run({"part": "items"}, CTX).content in text
    assert tool.run({"part": "exemplar"}, CTX).content in text
    k = preread_k(inp.kview, inp.names, inp.page_cells)
    assert k and k in text
    assert text.endswith("开始吧：直接写第一段。")
    assert "先 read_spec" not in text


def test_preread_k_reads_each_concept_of_the_page_once(env):
    inp = outline_input(env, organized(env))
    concepts = {split_cell(c)[0] for c in inp.page_cells}
    k = preread_k(inp.kview, inp.names, inp.page_cells + inp.page_cells)
    assert all(inp.names.get(c, c) in k for c in concepts)
    assert preread_k(inp.kview, inp.names, ()) == ""


def test_preread_spec_without_an_exemplar_is_the_items_alone(env):
    inp = outline_input(env, organized(env))
    bare = inp.teaching.model_copy(update={"exemplars": ()})
    assert preread_spec(bare) == ReadSpecTool(bare).run({"part": "items"}, CTX).content


def test_the_system_prompt_asks_for_the_first_section_first_and_one_whole_page_read():
    assert "第一次回复写第一段" in WRITE_SYSTEM
    assert "调用 read_page（不给 section_id）查看整页大纲" in WRITE_SYSTEM
    assert "不重复读取已经明确无误的内容" in WRITE_SYSTEM
    assert "动笔前调用 plan_page" in WRITE_SYSTEM and "规划完成后按规划写作" in WRITE_SYSTEM


@pytest.mark.parametrize("stub", [WRITE_STUB, READ_STUB])
def test_write_section_refuses_the_elision_stub_as_a_body(env, stub):
    # live run 2026-10-21: the writer copied the stub of its own elided write calls as section
    # text 19 times on one page; two sections were committed with it
    inp = outline_input(env, organized(env))
    ws = planned(inp, Workspace(inp.page, page_cells=inp.page_cells))
    sid = ws.outline_ids()[0]
    write = WriteSectionTool(ws)
    args = {"section_id": sid, "heading": "标题", "role": "explain", "depth": "quick",
            "cue": "一个具体的点？", "answer": "一句答案"}
    got = write.run({**args, "body": stub}, CTX)
    assert got.error is not None and "省略标记" in got.content and sid not in ws.sections
    assert write.run({**args, "body": "真正的正文。"}, CTX).error is None
    assert ws.sections[sid].body == "真正的正文。"
    # the section's own heading again as the body's first line would show twice in the note
    assert write.run({**args, "body": "## 标题\n正文。"}, CTX).error is None
    assert ws.sections[sid].body == "正文。"


def test_the_system_prompt_explains_elided_history():
    assert "不要把省略标记当正文照抄" in WRITE_SYSTEM
