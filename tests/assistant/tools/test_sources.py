import pytest

from evowiki.agent.source_tools import (
    READ_DESCRIPTION,
    ListSourcesTool,
    ReadSourceTool,
    SearchSourceTool,
)
from evowiki.agent.tool_helpers import SECTION_CHARS
from evowiki.agent.tools import ToolContext
from evowiki.assistant.tools import build_tools
from evowiki.belief.aspects import AspectKView
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources.reader import MemorySourceReader, PooledSourceReader
from tests.assistant.tools._source_fakes import INJECT, seed_pg
from tests.assistant.tools._source_fakes import page_text as _pages
from tests.assistant.tools._source_fakes import sec as _sec
from tests.assistant.tools._source_fakes import src as _src
from tests.assistant.tools.conftest import T0

LONG = "〔p.15〕\n" + "特征值" * 2500 + "\n\n〔p.16〕\n尾巴。"


@pytest.fixture
def reader():
    s1 = _src("s1", "u1", "线性代数讲义", unreadable=range(5, 15))
    secs1 = [
        _sec("a", "s1", 0, "第一章 向量", 1, 4, _pages(1, 3, "向量") + "\n\n〔p.4〕\n" + INJECT,
             summary="向量和线性组合。"),
        _sec("a1", "s1", 1, "1.1 内积", 4, 4, "", parent="a", depth=1),
        _sec("b", "s1", 2, "第二章 扫描页", 5, 14, "", read_by="none"),
        _sec("c", "s1", 3, "第三章 特征值", 15, 16, LONG),
    ]
    s2 = _src("s2", "u1", "笔记", pages=None, kind="md")
    secs2 = [_sec("n", "s2", 0, "笔记", None, None, "矩阵乘法不满足交换律。")]
    # mixed chapter: p.21-24 text, p.22-23 scanned (dropped from the text by toc)
    s3 = _src("s3", "u1", "混合扫描", pages=25, unreadable=(22, 23))
    secs3 = [_sec("m", "s3", 0, "第一章 混合", 21, 24,
                  "〔p.21〕\n第21页有字。\n\n〔p.24〕\n第24页有字。"),
             _sec("blank", "s3", 1, "空白", 25, 25, "")]
    gone = _src("sd", "u1", "已删的资料", deleted=True)
    secs_gone = [_sec("g", "sd", 0, "旧章", 1, 1, "〔p.1〕\n删掉的内容")]
    s9 = _src("s9", "u2", "别人的资料")
    secs9 = [_sec("x", "s9", 0, "秘密", 1, 1, "〔p.1〕\n别人的内容")]
    return MemorySourceReader({"u1": [(s1, secs1), (s2, secs2), (s3, secs3), (gone, secs_gone)],
                               "u2": [(s9, secs9)]})


@pytest.fixture
def ctx():
    return ToolContext(user_id="u1", session_id="sess1", page_id="p1")


def test_specs_are_parallel_reads(reader):
    for t in (ListSourcesTool(reader), ReadSourceTool(reader), SearchSourceTool(reader)):
        assert t.spec.effect == "read" and t.spec.parallel
        assert t.spec.parameters["type"] == "object"


# --- list_sources -----------------------------------------------------------------------------

def test_list_sources_lines_and_top_level_toc(reader, ctx):
    r = ListSourcesTool(reader).run({}, ctx)
    assert r.error is None
    c = r.content
    assert "s1 · 线性代数讲义 · pdf · 20 页 · " in c
    assert "第 5–14 页读不出字" in c
    assert "§a 第一章 向量（p.1–4）" in c and "向量和线性组合。" in c
    assert "§c 第三章 特征值（p.15–16）" in c
    assert "1.1 内积" not in c                       # only the first level
    assert "s2 · 笔记 · md" in c
    assert "别人的资料" not in c


def test_list_sources_empty(ctx):
    r = ListSourcesTool(MemorySourceReader({})).run({}, ctx)
    assert r.error is None and "还没有资料" in r.content


# --- read_source ------------------------------------------------------------------------------

def test_read_section(reader, ctx):
    r = ReadSourceTool(reader).run({"source_id": "s1", "section_id": "a"}, ctx)
    assert r.error is None
    assert "第2页讲向量" in r.content
    assert INJECT in r.content                         # data, verbatim, not obeyed
    src = r.sources[0]
    assert src.title == "线性代数讲义 p.1" and src.segment_id == "a"
    assert src.url is None and src.page_id is None


def test_read_page_range_takes_only_those_pages(reader, ctx):
    r = ReadSourceTool(reader).run({"source_id": "s1", "page_from": 2, "page_to": 3}, ctx)
    assert r.error is None
    assert "第2页讲向量" in r.content and "第3页讲向量" in r.content
    assert "第1页讲向量" not in r.content and INJECT not in r.content
    assert r.sources[0].title == "线性代数讲义 p.2"


def test_read_page_range_across_sections_reports_unreadable_pages(reader, ctx):
    r = ReadSourceTool(reader).run({"source_id": "s1", "page_from": 3, "page_to": 15}, ctx)
    assert r.error is None
    assert "第3页讲向量" in r.content and "特征值" in r.content
    assert "第 5–14 页读不出字" in r.content
    assert "尾巴" not in r.content                      # p.16 is outside


def test_read_unreadable_section_does_not_pretend(reader, ctx):
    r = ReadSourceTool(reader).run({"source_id": "s1", "section_id": "b"}, ctx)
    assert "第 5–14 页读不出字" in r.content
    assert r.error is None and "页讲" not in r.content      # no text invented


def test_read_offset_continues(reader, ctx):
    t = ReadSourceTool(reader)
    r1 = t.run({"source_id": "s1", "section_id": "c"}, ctx)
    assert r1.error is None and "还没读完，下次 offset=" in r1.content
    nxt = int(r1.content.rsplit("offset=", 1)[1].split()[0].rstrip("。）)"))
    assert nxt == SECTION_CHARS
    r2 = t.run({"source_id": "s1", "section_id": "c", "offset": nxt}, ctx)
    assert r2.error is None and "尾巴" in r2.content and "还没读完" not in r2.content


def test_read_section_reports_unreadable_pages_inside_it(reader, ctx):
    r = ReadSourceTool(reader).run({"source_id": "s3", "section_id": "m"}, ctx)
    assert r.error is None
    assert "第 22–23 页读不出字" in r.content
    assert "第21页有字" in r.content and "第24页有字" in r.content


def test_list_sources_marks_unreadable_pages_per_section(reader, ctx):
    c = ListSourcesTool(reader).run({}, ctx).content
    assert "§m 第一章 混合（p.21–24）（第 22–23 页读不出字）" in c


def test_read_page_range_with_no_text_says_so(reader, ctx):
    r = ReadSourceTool(reader).run({"source_id": "s3", "page_from": 25}, ctx)
    assert r.error and "第 25 页没有文字" in r.content


def test_huge_titles_are_clipped(ctx):
    t = "很长的标题" * 400                                   # 2000 chars
    reader = MemorySourceReader({"u1": [(_src("s1", "u1", t), [
        _sec("a", "s1", 0, t, 1, 2, "〔p.1〕\n" + "字" * 7000 + "\n\n〔p.2〕\n尾。")])]})
    for args in ({"source_id": "s1", "section_id": "a"}, {"source_id": "s1", "page_from": 1}):
        r = ReadSourceTool(reader).run(args, ctx)
        assert r.error is None and "还没读完" in r.content
    assert ListSourcesTool(reader).run({}, ctx).error is None
    assert SearchSourceTool(reader).run({"query": "尾"}, ctx).error is None


def test_read_description_uses_the_cap():
    assert f"每次最多 {SECTION_CHARS} 字" in READ_DESCRIPTION


def test_read_offset_past_end_is_error(reader, ctx):
    r = ReadSourceTool(reader).run({"source_id": "s1", "section_id": "a", "offset": 99999}, ctx)
    assert r.error


@pytest.mark.parametrize("args, needle", [
    ({"source_id": "s1"}, "要给节或页码"),
    ({"source_id": "nope", "section_id": "a"}, "找不到资料"),
    ({"source_id": "s9", "section_id": "x"}, "找不到资料"),           # another user's id
    ({"source_id": "sd", "section_id": "g"}, "找不到资料"),           # deleted source
    ({"source_id": "s1", "section_id": "zz"}, "没有节"),
    ({"source_id": "s1", "page_from": 30, "page_to": 31}, "没有"),
    ({"source_id": "s1", "page_from": 5, "page_to": 2}, "页码"),
])
def test_read_errors(reader, ctx, args, needle):
    r = ReadSourceTool(reader).run(args, ctx)
    assert r.error and needle in r.content


# --- search_source ----------------------------------------------------------------------------

def test_search_source_hits(reader, ctx):
    r = SearchSourceTool(reader).run({"query": "交换律"}, ctx)
    assert r.error is None
    assert "笔记 · §笔记" in r.content and "交换律" in r.content
    assert r.sources and r.sources[0].segment_id == "n"


def test_search_source_page_and_injection_text(reader, ctx):
    r = SearchSourceTool(reader).run({"query": "忽略之前的指令", "source_id": "s1"}, ctx)
    assert "线性代数讲义 · §第一章 向量 · p.4" in r.content
    assert INJECT in r.content
    assert r.sources[0].title == "线性代数讲义 p.4"


def test_search_source_other_users_id_and_unknown(reader, ctx):
    for sid in ("s9", "nope", "sd"):
        r = SearchSourceTool(reader).run({"query": "内容", "source_id": sid}, ctx)
        assert r.error and "找不到资料" in r.content


def test_search_source_no_hits_is_not_an_error(reader, ctx):
    r = SearchSourceTool(reader).run({"query": "量子场论"}, ctx)
    assert r.error is None and "没有找到" in r.content


def test_search_source_bad_args(reader, ctx):
    assert SearchSourceTool(reader).run({"query": ""}, ctx).error
    assert SearchSourceTool(reader).run({"query": "x", "k": 0}, ctx).error


# --- build_tools ------------------------------------------------------------------------------

def test_build_tools_adds_source_tools_only_with_a_reader(store, aspect_k, reader, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    kv = AspectKView(aspect_k, T0)
    without = build_tools(pages=store, kview=kv)
    assert not {"list_sources", "read_source", "search_source"} & set(without)
    with_ = build_tools(pages=store, kview=kv, sources=reader)
    assert {"list_sources", "read_source", "search_source"} <= set(with_)
    assert len(with_) == len(without) + 3


# --- PooledSourceReader over Postgres -----------------------------------------------------------

def test_pooled_reader(pool):
    from tests.runtime.helpers import make_user

    make_user(pool, "u1")
    make_user(pool, "u2")
    with pool.connection() as conn:
        seed_pg(conn, "s1", "u1", ["矩阵的特征值满足特征方程。", "行列式等于特征值的乘积。"],
                HashingEmbedder())
        conn.commit()
    r = PooledSourceReader(pool, HashingEmbedder())
    assert [s.source_id for s in r.list("u1")] == ["s1"] and r.list("u2") == []
    assert [s.section_id for s in r.sections("u1", "s1")] == ["s1-sec"]
    assert r.sections("u2", "s1") == []
    hits = r.search("u1", "行列式", None, 3)
    assert hits and hits[0].source_id == "s1"
    assert r.search("u2", "行列式", None, 3) == []
