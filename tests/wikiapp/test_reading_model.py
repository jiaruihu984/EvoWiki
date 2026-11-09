"""Reading mode's objects (spec 2026-10-28-reading-mode-design §2.1, §2.2, §4): the reading
snapshot event replays into the state, older page / path records still parse, paragraphs and
spans locate a unit by section + paragraphs (+ pages when the material has them), the structure
check refuses each invalid shape, and the stability ops keep ids and frozen locations. Pure."""

from datetime import UTC, datetime

import pytest

from evowiki.goals.intake import TocSection
from evowiki.goals.path import PathPage
from evowiki.goals.reading import (
    Reading,
    ReadingPlan,
    ReadingRefused,
    ReadingUnit,
    UnitSpan,
    check_units,
    merge_units,
    new_unit_id,
    reanchor,
    skip_unit,
    span_of,
    unskip_unit,
)
from evowiki.sources.labels import paragraphs
from evowiki.wikiapp.events import PageRecord, ReadingSaved, WikiEvent
from evowiki.wikiapp.state import replay

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)

# §4 with page marks: six paragraphs over pages 7–9 (paragraph 2 runs from page 7 into page 8)
S4 = ("〔p.7〕\n4 实验\n\n第一组实验用小球藻。\n\n标记十秒以后取样，\n〔p.8〕\n立刻杀死细胞。\n\n"
      "结果一：三碳化合物先出现。\n\n〔p.9〕\n第二组实验改变光照。\n\n结果二：五碳糖积累。")
# the same section without page numbers
PLAIN = "4 实验\n\n第一组实验用小球藻。\n\n第二组实验改变光照。"


def _units(*spans_per_unit, ids=None):
    return tuple(ReadingUnit(unit_id=(ids[n] if ids else f"ru_{n}"), order=n + 1,
                             title=f"单元{n + 1}", spans=tuple(spans))
                 for n, spans in enumerate(spans_per_unit))


def _reading(units, **kw):
    return Reading(reading_id="rd_1", map_id="map-x", source_id="s1", title="论文",
                   units=units, created_at=NOW, **kw)


# --- event, state, older records ---------------------------------------------------------------

def test_reading_saved_round_trips_and_replays_into_the_state():
    r = _reading(_units([UnitSpan(section_id="x1")]), left_out=("x9",))
    ev = WikiEvent(event_id="e1", ts=NOW, body=ReadingSaved(record=r, action="planned"))
    back = WikiEvent.model_validate_json(ev.model_dump_json())
    assert back == ev
    later = r.model_copy(update={"units": (r.units[0].model_copy(update={"skipped": True}),)})
    s = replay([back, WikiEvent(event_id="e2", ts=NOW,
                                body=ReadingSaved(record=later, action="edited"))])
    assert s.readings == {"rd_1": later}                 # the last snapshot wins
    assert s.reading_of("map-x") == later and s.reading_of("map-y") is None
    assert replay([back]).to_json() != replay([]).to_json()


def test_older_page_and_path_records_parse_as_learn_pages():
    page = PageRecord.model_validate({"page_id": "p1", "title": "t", "goal": "g",
                                      "concepts": ["c1"], "targets": ["c1"],
                                      "created_at": NOW.isoformat()})
    assert (page.kind, page.unit_id, page.reading_id) == ("learn", None, None)
    pp = PathPage.model_validate({"page_id": "p1", "targets": ["c1"], "concepts": ["c1"],
                                  "objective": "o", "new_cells": 1})
    assert (pp.kind, pp.unit_id) == ("learn", None)
    reading = page.model_copy(update={"kind": "reading", "unit_id": "ru_1", "reading_id": "rd_1"})
    assert PageRecord.model_validate_json(reading.model_dump_json()) == reading


# --- paragraphs and spans ------------------------------------------------------------------------

def test_paragraphs_with_page_marks():
    ps = paragraphs(S4)
    assert [p.index for p in ps] == [0, 1, 2, 3, 4, 5]
    assert [(p.page, p.page_to) for p in ps] == [(7, 7), (7, 7), (7, 8), (8, 8), (9, 9), (9, 9)]
    assert ps[2].head == "标记十秒以后取样， 立刻杀死细胞。" and "〔p.8〕" in ps[2].text
    # a mark alone between blank lines starts its page and is no paragraph
    assert [(p.head, p.page) for p in paragraphs("甲\n\n〔p.3〕\n\n乙", page=2)] == [("甲", 2),
                                                                                  ("乙", 3)]


def test_paragraphs_without_page_marks_have_no_pages():
    ps = paragraphs(PLAIN)
    assert [(p.head, p.page, p.page_to) for p in ps] == [
        ("4 实验", None, None), ("第一组实验用小球藻。", None, None), ("第二组实验改变光照。", None, None)]
    sp = span_of(ps, "x4", 1, 2)
    assert (sp.para_from, sp.para_to, sp.page_from, sp.page_to) == (1, 2, None, None)
    assert sp.anchor == "第一组实验用小球藻。"


def test_one_unit_across_two_pages_and_two_units_on_one_page():
    ps = paragraphs(S4)
    across = span_of(ps, "x4", 1, 3)                      # starts on p.7, ends on p.8
    assert (across.page_from, across.page_to) == (7, 8)
    assert across.anchor == "第一组实验用小球藻。"[:15]
    a, b = span_of(ps, "x4", 4, 4), span_of(ps, "x4", 5, 5)   # both on p.9
    assert (a.page_from, a.page_to, b.page_from, b.page_to) == (9, 9, 9, 9)
    whole = span_of(ps, "x4")
    assert (whole.para_from, whole.page_from, whole.page_to, whole.anchor) == (None, 7, 9, "4 实验")
    with pytest.raises(IndexError):
        span_of(ps, "x4", 3, 9)


def test_new_unit_id_uses_the_injected_source():
    assert new_unit_id(lambda p: f"{p}_7") == "ru_7"


# --- the structure check (§2.2) ---------------------------------------------------------------

SECTIONS = (TocSection(id="x0", label="§摘要", title="摘要", chars=200),
            TocSection(id="x1", label="§1", title="1 引言", chars=800),
            TocSection(id="x2", label="§2", title="2 方法", chars=0),          # only a heading
            TocSection(id="x21", label="§2.1", title="2.1 材料", chars=500),
            TocSection(id="x4", label="§4", title="4 实验", chars=3000),
            TocSection(id="x9", label="§参考文献", title="参考文献", chars=900))
COUNTS = {"x0": 1, "x1": 3, "x2": 1, "x21": 2, "x4": 6, "x9": 4}


def W(sid):                     # a whole section
    return UnitSpan(section_id=sid)


def P(sid, a, b):               # paragraphs a–b
    return UnitSpan(section_id=sid, para_from=a, para_to=b)


def test_a_good_plan_has_no_problem():
    units = _units([W("x0"), W("x1")],                  # cross-section merge
                   [W("x2"), W("x21")],                 # a heading with its subsection
                   [P("x4", 0, 2)], [P("x4", 3, 5)])    # one section split in two
    assert check_units(units, SECTIONS, COUNTS) == []
    # a heading-only section between two spans does not break them apart
    assert check_units(_units([P("x1", 1, 2), W("x21")]), SECTIONS, COUNTS) == []


@pytest.mark.parametrize("units,word", [
    ((), "一个单元都没有"),
    (_units([W("nope")]), "目录里没有的节"),
    (_units([P("x4", 0, 6)]), "超出范围"),
    (_units([P("x4", 3, 2)]), "超出范围"),
    (_units([UnitSpan(section_id="x4", para_from=1)]), "段落范围不完整"),
    (_units([W("x0"), W("x4")]), "原文不连续"),          # §1 and §2.1 skipped in between
    (_units([P("x4", 0, 2), P("x4", 4, 5)]), "原文不连续"),
    (_units([P("x1", 0, 1), W("x21")]), "原文不连续"),    # §1's last paragraph left behind
    (_units([P("x4", 0, 3)], [P("x4", 3, 5)]), "同时在"),
    (_units([W("x4")], [P("x4", 4, 5)]), "整节排进别的单元"),
    (_units([P("x4", 0, 1)], [W("x4")]), "又在第 2 个单元「单元2」里整节出现"),
    (_units([W("x2")]), "只有标题"),
    (_units([W("x4")], [W("x1")]), "按原文顺序"),
    (_units([W("x1"), W("x0")]), "原文不连续"),
])
def test_each_invalid_shape_is_a_problem(units, word):
    problems = check_units(units, SECTIONS, COUNTS)
    assert any(word in p for p in problems), problems


def test_orders_must_run_from_one_and_a_section_without_a_count_is_whole_only():
    bad = (ReadingUnit(unit_id="ru_a", order=2, title="甲", spans=(W("x0"),)),)
    assert any("序号应该是 1" in p for p in check_units(bad, SECTIONS, COUNTS))
    assert any("没有段落列表" in p
               for p in check_units(_units([P("x4", 0, 1)]), SECTIONS, {}))


# --- stability ops (§2.1) ----------------------------------------------------------------------

def test_merge_keeps_the_first_id_and_moves_the_rest_up():
    r = _reading(_units([P("x4", 0, 2)], [P("x4", 3, 5)], [W("x9")], ids=["ru_a", "ru_b", "ru_c"]))
    out = merge_units(r, "ru_a", "ru_b")
    assert [(u.unit_id, u.order) for u in out.units] == [("ru_a", 1), ("ru_c", 2)]
    assert out.units[0].spans == (P("x4", 0, 5),)        # neighbouring ranges joined
    assert out.units[0].title == "单元1、单元2"
    assert "ru_b" not in {u.unit_id for u in out.units}
    # a draft's plan merges the same way
    plan = ReadingPlan(source_id="s1", title="论文", units=r.units)
    assert merge_units(plan, "ru_b", "ru_c", title="后半").units[1].title == "后半"


@pytest.mark.parametrize("first,second,reason", [
    ("ru_a", "ru_c", "not_adjacent"), ("ru_a", "ru_a", "same_unit"),
    ("ru_a", "ru_z", "unknown_unit"), ("ru_c", "ru_d", "started")])
def test_merge_refuses(first, second, reason):
    units = _units([W("x0")], [W("x1")], [W("x21")], [P("x4", 0, 5)],
                   ids=["ru_a", "ru_b", "ru_c", "ru_d"])
    units = (*units[:3], units[3].model_copy(update={"page_id": "p1"}))
    with pytest.raises(ReadingRefused) as e:
        merge_units(_reading(units), first, second)
    assert e.value.reason == reason


def test_skip_and_unskip():
    r = _reading(_units([W("x0")], [W("x1")], ids=["ru_a", "ru_b"]))
    skipped = skip_unit(r, "ru_b")
    assert [u.skipped for u in skipped.units] == [False, True]
    with pytest.raises(ReadingRefused) as e:
        merge_units(skipped, "ru_a", "ru_b")
    assert e.value.reason == "skipped"
    assert unskip_unit(skipped, "ru_b") == r
    with pytest.raises(ReadingRefused):
        unskip_unit(r, "ru_b")
    started = _reading((r.units[0].model_copy(update={"page_id": "p1"}), r.units[1]))
    with pytest.raises(ReadingRefused) as e:
        skip_unit(started, "ru_a")
    assert e.value.reason == "started"


def test_reanchor_finds_the_spans_again():
    old = paragraphs(S4)
    r = _reading(_units([span_of(old, "x4", 0, 2)], [span_of(old, "x4", 3, 5)]))
    # re-OCR: a new paragraph before the second unit's first one, page 8 split out
    new = paragraphs(S4.replace("结果一：", "（插图说明）\n\n结果一："))
    out = reanchor(r, "x4", new)
    a, b = out.units[0].spans[0], out.units[1].spans[0]
    assert (a.para_from, a.para_to, b.para_from, b.para_to) == (0, 3, 4, 6)
    assert not a.uncertain and not b.uncertain
    assert (b.page_from, b.page_to) == (8, 9)


def test_reanchor_not_found_reads_the_whole_section_and_says_so():
    old = paragraphs(S4)
    r = _reading(_units([span_of(old, "x4", 0, 2)], [span_of(old, "x4", 3, 5)]))
    new = paragraphs(S4.replace("结果一：三碳化合物先出现。", "结论一：先出现三碳化合物。"))
    out = reanchor(r, "x4", new)
    a, b = out.units[0].spans[0], out.units[1].spans[0]
    assert not a.uncertain and (a.para_from, a.para_to) == (0, 2)
    # the paragraphs after a: marked, never overlapping a (M8)
    assert b.uncertain and (b.para_from, b.para_to) == (3, 5) and (b.page_from, b.page_to) == (8, 9)
    assert check_units(out.units, [SECTIONS[4]], {"x4": len(new)}) == []
    # the section's only span not found: the section whole, marked
    lone = _reading(_units([span_of(old, "x4", 3, 5)]))
    (sp,) = reanchor(lone, "x4", new).units[0].spans
    assert sp.uncertain and sp.whole and (sp.page_from, sp.page_to) == (7, 9)
    # other sections' spans are untouched
    r2 = _reading(_units([W("x1")]))
    assert reanchor(r2, "x4", new) == r2
