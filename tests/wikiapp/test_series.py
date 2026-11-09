import pytest
from pydantic import ValidationError

from evowiki.wikiapp.series import (
    SERIES_MAX_CHARS,
    SeriesNote,
    SeriesPage,
    SeriesTerm,
    SeriesUpdate,
    merge,
    render,
)


def _u(summary="讲了均值。", **kw):
    return SeriesUpdate(summary=summary, **kw)


def test_first_page_builds_note():
    n = merge(None, _u(example="班级考试成绩", terms=("均值", "方差"), notation=("x 表示分数",)),
              page_id="p1", title="均值")
    assert n.example == "班级考试成绩"
    assert n.terms == (SeriesTerm(term="均值", page_id="p1", page_title="均值"),
                       SeriesTerm(term="方差", page_id="p1", page_title="均值"))
    assert n.notation == ("x 表示分数",)
    assert n.pages == (SeriesPage(page_id="p1", title="均值", summary="讲了均值。"),)


def test_set_example_never_replaced_empty_takes_new():
    n = merge(None, _u(example="例子A"), page_id="p1", title="一")
    n2 = merge(n, _u(example="例子B"), page_id="p2", title="二")
    assert n2.example == "例子A"
    empty = merge(None, _u(), page_id="p1", title="一")
    assert empty.example == ""
    assert merge(empty, _u(example="例子B"), page_id="p2", title="二").example == "例子B"


def test_terms_and_notation_dedupe():
    n = merge(None, _u(terms=("均值",), notation=("x",)), page_id="p1", title="一")
    n2 = merge(n, _u(terms=(" 均值 ", "Mean", "mean"), notation=("x", "y")), page_id="p2", title="二")
    assert [(t.term, t.page_title) for t in n2.terms] == [("均值", "一"), ("Mean", "二"), ("mean", "二")]
    assert n2.notation == ("x", "y")


def test_same_page_id_replaced_in_place():
    n = merge(None, _u("旧"), page_id="p1", title="一")
    n = merge(n, _u("二页"), page_id="p2", title="二")
    n = merge(n, _u("新"), page_id="p1", title="一改")
    assert [(p.page_id, p.title, p.summary) for p in n.pages] == [("p1", "一改", "新"), ("p2", "二", "二页")]


def test_render_exact():
    n = merge(None, _u("讲了均值。", example="成绩", terms=("均值",), notation=("x 表示分数", "n 表示人数")),
              page_id="p1", title="均值")
    assert render(n) == (
        "贯穿的例子：成绩\n已经讲过的术语：均值：均值\n记号约定：x 表示分数；n 表示人数\n"
        "前面各页：\n- [均值](#p:p1)：讲了均值。"
    )
    n2 = merge(n, _u("讲了方差。", terms=("方差", "标准差")), page_id="p2", title="方差")
    lines = render(n2).splitlines()
    assert lines[1] == "已经讲过的术语：均值：均值；方差：方差、标准差"     # grouped by page
    assert lines[-1] == "- [方差](#p:p2)：讲了方差。"
    assert render(SeriesNote()) == (
        "贯穿的例子：（还没有）\n已经讲过的术语：（还没有）\n记号约定：（还没有）\n前面各页："
    )


def test_render_can_leave_out_one_page():
    n = merge(None, _u("一页。", example="成绩", terms=("均值",)), page_id="p1", title="一")
    n = merge(n, _u("二页。", terms=("方差",)), page_id="p2", title="二")
    out = render(n, exclude_page="p1")
    assert "均值" not in out and "一页" not in out
    assert "方差" in out and "二页" in out and "成绩" in out
    assert render(n, exclude_page=None) == render(n)


def _many(n_pages, summary):
    note = None
    for i in range(n_pages):
        note = merge(note, _u(summary, example="例", terms=(f"术语{i}",)), page_id=f"p{i}", title=f"页面{i}")
    return note


def test_cap_first_sentence_then_empty():
    long = "第一句话在这里。" + "后面很长" * 70
    note = _many(30, long)
    assert len(render(note)) <= SERIES_MAX_CHARS
    assert note.pages[-1].summary == long          # newest untouched
    assert note.pages[0].summary in ("", "第一句话在这里")
    assert note.example == "例"
    assert len(note.terms) == 30                   # terms untouched here


def test_cap_first_sentence_stage_is_enough_sometimes():
    long = "短句。" + "尾" * 290
    note = _many(10, long)
    assert len(render(note)) <= SERIES_MAX_CHARS
    assert note.pages[0].summary == "短句"
    assert note.pages[-1].summary == long


def test_cap_empties_oldest_first():
    long = "a" * 300
    note = _many(14, long)
    assert len(render(note)) <= SERIES_MAX_CHARS
    sums = [p.summary for p in note.pages]
    assert sums[-1] == long
    # once a summary is kept long, none after it was shortened more than the ones before it
    assert sums[0] == "" or len(sums[0]) <= 40
    k = sum(1 for x in sums[:-1] if x == "")
    assert all(x == "" for x in sums[:k])              # emptied ones form a prefix
    assert all(x != "" for x in sums[k:-1])


def test_cap_drops_oldest_terms_for_huge_note():
    note = SeriesNote(
        example="e",
        terms=tuple(SeriesTerm(term=f"术语{i:03d}", page_id=f"q{i}", page_title="某页标题")
                    for i in range(300)),
        pages=(SeriesPage(page_id="p0", title="旧", summary=""),),
    )
    out = merge(note, _u("新页"), page_id="p9", title="新")
    assert len(render(out)) <= SERIES_MAX_CHARS
    assert 0 < len(out.terms) <= 60
    assert out.terms[-1].term == "术语299"
    assert out.pages[-1].summary == "新页"


def test_update_over_count_rejected():
    with pytest.raises(ValidationError):
        SeriesUpdate(summary="x", terms=tuple(f"t{i}" for i in range(21)))
    with pytest.raises(ValidationError):
        SeriesUpdate(summary="x", notation=tuple(f"n{i}" for i in range(11)))


def test_older_flood_trimmed_own_survive():
    old = SeriesNote(
        example="e",
        terms=tuple(SeriesTerm(term=f"旧术语{i:03d}", page_id="p0", page_title="旧页") for i in range(60)),
        notation=tuple(f"旧约定{i}" + "字" * 100 for i in range(60)),
        pages=(SeriesPage(page_id="p0", title="旧", summary=""),),
    )
    upd = _u("新页", terms=("新术语A", "新术语B"), notation=("新约定X",))
    out = merge(old, upd, page_id="p9", title="新")
    assert len(render(out)) <= SERIES_MAX_CHARS
    assert {t.term for t in out.terms} >= {"新术语A", "新术语B"}
    assert "新约定X" in out.notation
    assert len(out.notation) < 61


def test_page_lines_dropped_oldest_first_reorganized_middle_protected():
    pages = tuple(
        SeriesPage(page_id=f"p{i}", title="标" * 190, summary="") for i in range(30)
    )
    old = SeriesNote(example="e", pages=pages)
    assert len(render(old)) > SERIES_MAX_CHARS
    out = merge(old, _u("改过的页" * 5), page_id="p10", title="改")
    assert len(render(out)) <= SERIES_MAX_CHARS
    ids = [p.page_id for p in out.pages]
    assert "p10" in ids
    assert next(p for p in out.pages if p.page_id == "p10").summary == "改过的页" * 5
    assert "p0" not in ids and ids[-1] == "p29"      # oldest dropped, newest kept


def test_models_frozen_and_bounded():
    with pytest.raises(ValidationError):
        SeriesUpdate(summary="")
    with pytest.raises(ValidationError):
        SeriesUpdate(summary="x", notation=("n" * 121,))
    with pytest.raises(ValidationError):
        SeriesUpdate(summary="x", terms=("t" * 81,))
    with pytest.raises(ValidationError):
        SeriesNote().example = "x"  # type: ignore[misc]


def test_long_title_is_clipped_not_rejected():
    title = "很长的目标" * 100 + "（1/3）"
    n = merge(None, _u(terms=("均值",)), page_id="p1", title=title)
    assert n.pages[0].title == title[:200] and n.terms[0].page_title == title[:200]
