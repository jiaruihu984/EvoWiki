from dataclasses import replace

from evowiki.sources.labels import (
    LEAD_CHARS,
    body_chars,
    lead,
    section_labels,
    source_toc,
    uncovered,
)
from evowiki.sources.models import LinkRow
from tests.sources.test_store import _sec, _src


def _s(sid, ord_, title, parent=None, **kw):
    return replace(_sec(sid, ord_, parent=parent), title=title, **kw)


def test_numbered_labels():
    secs = [_s("a", 0, "1 引言"), _s("b", 1, "2 方法"), _s("c", 2, "2.1 数据"), _s("d", 3, "第3章 结果")]
    assert section_labels(secs) == {"a": "§1", "b": "§2", "c": "§2.1", "d": "§3"}


def test_title_labels_and_duplicates():
    secs = [_s("a", 0, "前言"), _s("b", 1, "附录 B 证明细节很长很长很长"),
            _s("c", 2, "习题"), _s("d", 3, "习题")]
    assert section_labels(secs) == {"a": "§前言", "b": "§附录 B 证明细节很长很",
                                    "c": "§习题", "d": "§习题（2）"}


def test_lead():
    assert lead("〔p.3〕第一段第一句。\n第一段第二句。\n\n第二段") == "第一段第一句。\n第一段第二句。"
    assert lead("字" * (LEAD_CHARS + 5)) == "字" * LEAD_CHARS + "…"


def test_source_toc():
    secs = [_s("a", 0, "1 引言", page_from=2, page_to=5, text="〔p.2〕开头。"),
            _s("b", 1, "前言", page_from=7, page_to=7, text="x"),
            _s("c", 2, "后记", page_from=None, page_to=None, text="y")]
    toc = source_toc(_src("s1"), secs, prefix="1·")
    assert [t.label for t in toc.sections] == ["1·§1", "1·§前言", "1·§后记"]
    assert [t.pages for t in toc.sections] == ["p.2–5", "p.7", ""]
    assert toc.sections[0].lead == "开头。" and toc.title == "书"
    assert [t.chars for t in toc.sections] == [3, 1, 1]


def test_body_chars_counts_past_the_heading():
    assert body_chars("2. CheXNet", "〔p.2〕2. CheXNet") == 0          # only a heading
    assert body_chars("2. CheXNet", "〔p.2〕\n2.  CheXNet\n\n〔p.3〕") == 0
    assert body_chars("2.1 Problem", "2.1 Problem\nThe task  is\nbinary.") == len("The task is binary.")
    assert body_chars("前言", "没有标题行的正文") == len("没有标题行的正文")


def test_uncovered():
    secs = [_s("P", 0, "前言"), _s("A", 1, "A"), _s("A1", 2, "A1", parent="A"),
            _s("A2", 3, "A2", parent="A"), _s("B", 4, "B")]
    links = [LinkRow("n1", "A1", "s1", "teaches", "intake"),
             LinkRow("n2", "B", "s1", "exercises", "intake")]
    assert uncovered(secs, links) == [("P", False), ("B", True)]


def test_a_cut_title_leaves_no_trailing_space_a_label_differs_by():
    """Live 2026-11-04: 「§Definitions 」 and 「§Definitions」 were two labels; the model's stripped one named the empty one."""
    secs = [_s("a", 0, "Definitions - Graph theory"), _s("b", 1, "Definitions")]
    assert section_labels(secs) == {"a": "§Definitions", "b": "§Definitions（2）"}
