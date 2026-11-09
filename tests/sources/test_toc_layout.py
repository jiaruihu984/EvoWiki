import dataclasses

from evowiki.sources.document import Block, DocPage, SourceDocument
from evowiki.sources.toc import build_toc, candidate_lines, heading_lines
from evowiki.wikiapp.llm import LLMUnavailable
from tests.sources._fakes import id_gen as ids


def _layout_doc(heads_by_page, n_pages=6):
    blocks, pages = [], []
    for p in range(1, n_pages + 1):
        pages.append(DocPage(p))
        for i, (title, level) in enumerate(heads_by_page.get(p, [])):
            blocks.append(Block(f"p{p}-b{2*i}", p, "heading", title, level=level))
            blocks.append(Block(f"p{p}-b{2*i+1}", p, "paragraph", f"body of {title} " * 20))
    return SourceDocument("pdf", "mineru", "t", tuple(pages), tuple(blocks)).to_parsed("Doc")


def test_numbered_layout_heads_need_no_model():
    doc = _layout_doc({1: [("1 Intro", 1)], 2: [("1.1 Scope", 1), ("2 Method", 1)],
                       4: [("A. Appendix", 1)]})
    toc = build_toc(doc, "s", llm=None, new_id=ids())
    assert [(s.title, s.depth) for s in toc.sections if s.title != "前面的内容"][:3] == [
        ("1 Intro", 0), ("1.1 Scope", 1), ("2 Method", 0)]


def test_unnumbered_heads_go_under_the_last_numbered_one_without_a_model():
    doc = _layout_doc({1: [("3.1 线性回归", 1), ("线性模型", 1)],
                       2: [("损失函数", 1), ("3.2 实现", 1)], 3: [("小结", 1)]})
    toc = build_toc(doc, "s", llm=None, new_id=ids())
    assert [(s.title, s.depth) for s in toc.sections] == [
        ("3.1 线性回归", 0), ("线性模型", 1), ("损失函数", 1), ("3.2 实现", 0), ("小结", 1)]
    # 小结 is not STANDALONE (that set holds Abstract/References…), so it sits under 3.2


def test_model_sees_only_layout_head_lines():
    seen = {}

    def llm(role, step, prompt, schema):
        seen["prompt"] = prompt
        raise LLMUnavailable("x")

    doc = _layout_doc({1: [("Overview", 1)], 3: [("Details", 1)], 5: [("Wrap up", 1)]})
    toc = build_toc(doc, "s", llm=llm, new_id=ids())
    assert "Overview" in seen["prompt"] and "body of Overview" not in seen["prompt"]
    # the model gave nothing: the layout headings make the sections
    assert toc.mode == "headings" and toc.diagnostics == ("按版面识别的标题建目录",)
    assert [s.title for s in toc.sections] == ["Overview", "Details", "Wrap up"]


def test_standalone_names_are_top_level():
    doc = _layout_doc({1: [("Abstract", 1), ("1 Intro", 1)], 2: [("Background", 1)],
                       3: [("References", 1)]}, n_pages=3)
    toc = build_toc(doc, "s", llm=None, new_id=ids())
    assert [(s.title, s.depth) for s in toc.sections] == [
        ("Abstract", 0), ("1 Intro", 0), ("Background", 1), ("References", 0)]


def test_without_layout_heads_nothing_changes():
    doc = _layout_doc({1: [("Overview", 1)], 3: [("Details", 1)], 5: [("Wrap up", 1)]})
    plain = dataclasses.replace(doc, layout_heads=())
    assert heading_lines(plain) == []           # regex finds no heading here
    shown = candidate_lines(plain, [])
    assert shown[1] == [0, 1]                   # first lines are offered again
    assert candidate_lines(doc, heading_lines(doc))[1] == [0]


def test_prompt_v4_with_a_layout_parse_lists_only_heading_lines_and_says_so():
    from evowiki.sources.parsed import LayoutHead, PageText, ParsedDoc
    from evowiki.sources.toc import TocPlan, build_toc
    from evowiki.wikiapp.llm import JSON_PROMPT_VERSION
    from tests.sources._fakes import FakeLLM, id_gen
    pages = tuple(PageText(n, f"Heading {'ABCD'[n - 1]}\nbody line of page {n}.", "text")
                  for n in (1, 2, 3, 4))
    heads = (LayoutHead(1, 0, "Heading A", 1), LayoutHead(3, 0, "Heading C", 1))
    llm = FakeLLM(TocPlan(sections=[]))
    build_toc(ParsedDoc("pdf", "doc", pages, (), False, heads), "src", llm=llm, new_id=id_gen())
    prompt = llm.calls[0][2]
    assert JSON_PROMPT_VERSION["sources.toc"] == "sources-toc@v4"
    assert "版面分析" in prompt and "前几行" not in prompt
    assert "第1页：" in prompt and "第3页：" in prompt and "第2页" not in prompt
    assert "无文字" not in prompt and "body line" not in prompt
