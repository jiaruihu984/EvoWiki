"""A material's section text changes under a reading (reading-mode spec §2.1 last rule, §8.1
「节的文字变了」): a page read again goes into its section (`importer.swap_reads`) and the
material's readings follow (`wikiapp.reading.reanchor_readings`, what the import's and the exam
re-read's patch steps call): an anchor found moves the unit's paragraph range; one not found
reads what is left between its neighbours, marked 「原文位置可能不准」, no error and no overlap.
Postgres materials, a memory wiki; no model call."""

from dataclasses import replace

import pytest

from evowiki.goals.reading import ReadingPlan, ReadingUnit, check_units, span_of
from evowiki.sources import store
from evowiki.sources.importer import swap_reads
from evowiki.sources.labels import source_toc
from evowiki.wikiapp.events import ReadingSaved
from evowiki.wikiapp.reading import create_reading_goal, reanchor_readings
from evowiki.wikiapp.repo import MemoryRepo, PgRepo
from tests.runtime.helpers import make_user
from tests.sources.test_store import _sec, _src
from tests.wikiapp.test_goal import NOW, Ids

TEXT = ("〔p.1〕\n第一组实验用小球藻。\n\n标记十秒以后取样。\n\n"
        "〔p.2〕\n结果一：三碳化合物先出现。\n\n第二组实验改变光照。")


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    src = _src("rs1", status="ready", title="一篇论文")
    sec = replace(_sec("rx1", 0, source="rs1", text=TEXT), title="4 实验")
    with pool.connection() as conn:
        store.insert_source(conn, src, b"a", "x/y")
        store.replace_sections(conn, "u1", "rs1", [sec])
    toc = source_toc(src, [sec])
    paras = toc.sections[0].paras
    units = (ReadingUnit(unit_id="ru_a", order=1, title="第一组", spans=(span_of(paras, "rx1", 0, 1),)),
             ReadingUnit(unit_id="ru_b", order=2, title="结果和第二组",
                         spans=(span_of(paras, "rx1", 2, 3),)))
    repo, ids = MemoryRepo(["u1"]), Ids()
    res = create_reading_goal(repo, None, "u1", "读懂《一篇论文》", (),
                              ReadingPlan(units=units, source_id="rs1", title="一篇论文"),
                              now=NOW, new_id=ids, source=toc)
    return pool, repo, ids, res.reading_id


def _spans(repo, rid):
    return [u.spans[0] for u in repo.load("u1").state.readings[rid].units]


@pytest.mark.parametrize("postgres", [False, True])
def test_reanchor_cannot_publish_after_source_generation_changes(env, monkeypatch, postgres):
    from evowiki.sources.importer import _Deleted

    pool, repo, ids, rid = env
    if postgres:
        units = repo.load("u1").state.readings[rid].units
        repo = PgRepo(pool)
        with pool.connection() as conn:
            toc = source_toc(store.get_source(conn, "u1", "rs1"), store.sections(conn, "u1", "rs1"))
        result = create_reading_goal(repo, None, "u1", "读懂《一篇论文》", (),
            ReadingPlan(units=units, source_id="rs1", title="一篇论文"),
            now=NOW, new_id=ids, source=toc)
        rid = result.reading_id
    before = repo.load("u1").state.readings[rid]
    swap_reads(pool, "u1", "rs1", [(1, "新的一段。\n\n第一组实验用小球藻。\n\n标记十秒以后取样。")])
    commit = repo.commit

    def changed(snap, batch, **kwargs):
        with pool.connection() as conn:
            store.bump_run(conn, "u1", "rs1")
        return commit(snap, batch, **kwargs)

    monkeypatch.setattr(repo, "commit", changed)
    with pytest.raises(_Deleted):
        reanchor_readings(repo, pool, "u1", "rs1", ["rx1"], now=NOW, new_id=ids)
    assert repo.load("u1").state.readings[rid] == before


def test_an_anchor_found_moves_the_paragraph_range(env):
    pool, repo, ids, rid = env
    # page 1 read again: a figure caption before 「标记十秒」 — one more paragraph
    page1 = "第一组实验用小球藻。\n\n（图 1 的说明）\n\n标记十秒以后取样。"
    changed = swap_reads(pool, "u1", "rs1", [(1, page1)])
    assert changed == ["rx1"]
    assert reanchor_readings(repo, pool, "u1", "rs1", changed, now=NOW, new_id=ids) == [rid]
    a, b = _spans(repo, rid)
    assert (a.para_from, a.para_to, b.para_from, b.para_to) == (0, 2, 3, 4)
    assert not a.uncertain and not b.uncertain and (b.page_from, b.page_to) == (2, 2)
    assert [e.body.action for e in repo.wiki_log("u1")
            if isinstance(e.body, ReadingSaved)][-1] == "edited"


def test_an_anchor_not_found_is_marked_without_error_or_overlap(env):
    pool, repo, ids, rid = env
    # page 2 read again with other wording: unit b's first words are gone
    changed = swap_reads(pool, "u1", "rs1", [(2, "结论一：先出现三碳化合物。\n\n第二组实验改变光照。")])
    reanchor_readings(repo, pool, "u1", "rs1", changed, now=NOW, new_id=ids)
    a, b = _spans(repo, rid)
    assert not a.uncertain and (a.para_from, a.para_to) == (0, 1)
    assert b.uncertain and (b.para_from, b.para_to) == (2, 3)
    with pool.connection() as conn:
        secs = store.sections(conn, "u1", "rs1")
        toc = source_toc(store.get_source(conn, "u1", "rs1"), secs)
    reading = repo.load("u1").state.readings[rid]
    assert check_units(reading.units, toc.sections,
                       {s.id: len(s.paras) for s in toc.sections}) == []


def test_text_unchanged_for_the_readings_writes_nothing(env):
    pool, repo, ids, _ = env
    n = len(repo.wiki_log("u1"))
    assert reanchor_readings(repo, pool, "u1", "rs1", ["other"], now=NOW, new_id=ids) == []
    assert reanchor_readings(repo, pool, "u1", "rs1", ["rx1"], now=NOW, new_id=ids) == []
    assert len(repo.wiki_log("u1")) == n
