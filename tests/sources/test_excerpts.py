from dataclasses import replace

import pytest

from evowiki.sources import store
from evowiki.sources.excerpts import (
    MemorySourceExcerpts,
    PooledSourceExcerpts,
    excerpts_for,
    pages_of,
)
from evowiki.sources.models import LinkRow
from tests.runtime.helpers import make_user
from tests.sources.test_store import NOW, _sec, _src


def _two():
    a = replace(_sec("a1", 0, text="〔p.1〕甲的第一段。\n\n乙"), title="1 引言", tokens=100, summary=None)
    b = replace(_sec("a2", 1, text="长文"), title="2 方法", tokens=200, summary="方法概述")
    return _src("s1"), [a, b]


def _l(node, sec, role="teaches") -> LinkRow:
    return LinkRow(node, sec, "s1", role, "intake")


def test_full_text_within_budget():
    src, secs = _two()
    out = excerpts_for({"s1": (src, secs)}, [_l("n", "a1"), _l("n", "a2")], budget=1000)
    assert [e.text is not None for e in out] == [True, True]
    assert [e.label for e in out] == ["§1", "§2"]


def test_navigation_over_budget():
    src, secs = _two()
    out = excerpts_for({"s1": (src, secs)}, [_l("n", "a1"), _l("n", "a2")], budget=250)
    assert [e.text for e in out] == [None, None]
    assert [e.summary for e in out] == ["甲的第一段。", "方法概述"]


def test_dedupe_role_reads_wins():
    src, secs = _two()
    out = excerpts_for({"s1": (src, secs)}, [_l("n", "a1"), _l("n", "a1", "reads")], budget=1000)
    assert len(out) == 1 and out[0].role == "reads"


def test_memory_only_this_user():
    src, secs = _two()
    mem = MemorySourceExcerpts({"u1": [(src, secs, [_l("n", "a1")])]})
    assert len(mem.for_nodes("u1", ["n"])) == 1
    assert mem.for_nodes("u2", ["n"]) == []
    assert mem.for_nodes("u1", ["other"]) == []


@pytest.fixture
def seeded(pool):
    make_user(pool, "u1")
    src, secs = _two()
    with pool.connection() as conn:
        store.insert_source(conn, src, b"a", "x/y")
        store.replace_sections(conn, "u1", "s1", secs)
        store.replace_links(conn, "u1", ["n"], [_l("n", "a2")], by="intake", now=NOW)
    yield pool
    with pool.connection() as conn:
        conn.execute("delete from source where source_id = 's1'")


def test_pooled(seeded):
    out = PooledSourceExcerpts(seeded).for_nodes("u1", ["n"])
    assert len(out) == 1
    e = out[0]
    assert (e.label, e.title, e.page_from, e.page_to, e.source_title) == ("§2", "2 方法", 1, 2, "书")
    assert e.text == "长文"
    assert PooledSourceExcerpts(seeded).for_nodes("u2", ["n"]) == []


def test_pages_of_splits_at_page_marks_and_joins_a_page_across_sections():
    a = replace(_sec("a1", 0, text="开头\n〔p.2〕第二页上\n〔p.3〕第三页"), page_from=1, page_to=3)
    b = replace(_sec("a2", 1, text="〔p.3〕接着第三页\n〔p.4〕第四页"), page_from=3, page_to=4)
    none = replace(_sec("a3", 2, text="〔p.5〕看不清"), read_by="none")
    assert pages_of([b, a, none], [1, 3, 5]) == {1: "开头", 3: "第三页\n接着第三页"}


def test_page_texts_pooled_and_memory(seeded):
    pooled = PooledSourceExcerpts(seeded)
    # a2 has no page marks: all of it is on its page_from (1)
    assert pooled.page_texts("u1", "s1", [1]) == {1: "甲的第一段。\n\n乙\n长文"}
    with pytest.raises(LookupError):
        pooled.page_texts("u2", "s1", [1])                   # another user's material
    src, secs = _two()
    mem = MemorySourceExcerpts({"u1": [(src, secs, [])]})
    assert mem.page_texts("u1", "s1", [1]) == pooled.page_texts("u1", "s1", [1])
    with pytest.raises(LookupError):
        mem.page_texts("u1", "nope", [1])


def test_exercises_of_an_exam_source_left_out():
    src, secs = _two()
    links = [_l("n", "a1", "exercises"), _l("n", "a2", "teaches")]
    out = excerpts_for({"s1": (src, secs)}, links, budget=1000, exam_sources={"s1"})
    assert [e.section_id for e in out] == ["a2"]
    # a section that also teaches stays; without an exam source nothing is dropped
    both = [_l("n", "a1", "exercises"), _l("n", "a1", "teaches")]
    assert len(excerpts_for({"s1": (src, secs)}, both, budget=1000, exam_sources={"s1"})) == 1
    assert len(excerpts_for({"s1": (src, secs)}, links, budget=1000)) == 2
    mem = MemorySourceExcerpts({"u1": [(src, secs, links)]}, exam_sources={"s1"})
    assert [e.section_id for e in mem.for_nodes("u1", ["n"])] == ["a2"]


def test_pooled_drops_exercises_only_with_exam_use_and_items(pool):
    from evowiki.sources import exam_store
    from tests.sources.test_exam_store import _item
    make_user(pool, "u1")
    src, secs = _two()
    with pool.connection() as conn:
        store.insert_source(conn, src, b"a", "x/y")
        store.replace_sections(conn, "u1", "s1", secs)
        store.replace_links(conn, "u1", ["n"], [_l("n", "a1", "exercises"), _l("n", "a2")],
                            by="intake", now=NOW)
    ex = PooledSourceExcerpts(pool)
    assert len(ex.for_nodes("u1", ["n"])) == 2
    with pool.connection() as conn:
        store.add_use(conn, "u1", "s1", "exam", now=NOW)
    assert len(ex.for_nodes("u1", ["n"])) == 2                   # exam use, nothing extracted yet
    with pool.connection() as conn:
        exam_store.replace_items(conn, "u1", "s1", [_item("i1")], gen=1, now=NOW)
    assert [e.section_id for e in ex.for_nodes("u1", ["n"])] == ["a2"]
