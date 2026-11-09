"""What the wire shows of a 读懂 goal's reading (reading-mode spec §5, §8.1 「进度和展示」 bullet 1,
plan stage 4 Task 5): `ReadingOut` on the goal (map and graph), unit status from the page only
(读完 ≠ 掌握), progress without skipped units, locations, the 「在《T》的第 N 段里读」 labels and the
coverage of the sections the units read. Scripted LLM / memory repo only."""

from types import SimpleNamespace

import pytest

from evowiki.sources.labels import section_labels, uncovered
from evowiki.sources.models import LinkRow
from evowiki.wikiapp.graph_out import graph_out
from evowiki.wikiapp.kchange import answer_check
from evowiki.wikiapp.maps import map_out
from evowiki.wikiapp.page import mark_done
from evowiki.wikiapp.reading import start_unit_page
from evowiki.wikiapp.reading_view import reading_out
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.source_view import SourceInfo, _read_at, _read_sections, read_in
from tests.wikiapp.goal_script import auto_probe
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_reading_unit import (
    B_OUT,
    SECS,
    TITLE,
    _organized_reading_page,
    analyze,
    goal,
    rid,
    skip,
    st,
)

LABELS = section_labels(SECS)


@pytest.fixture
def env():
    s = Script()
    auto_probe(s)
    return MemoryRepo(["u1"]), s, Ids()


def out(env):
    snap = env[0].load("u1")
    return reading_out(snap.state, snap.kview(NOW), snap.state.readings[rid(env)], labels=LABELS)


def test_a_new_reading_is_all_not_started_with_locations(env):
    goal(env)
    o = out(env)
    assert [u.unit_id for u in o.units] == ["ru_a", "ru_b", "ru_c", "ru_d"]
    assert [u.status for u in o.units] == ["not_started"] * 4
    assert [u.location for u in o.units] == ["§1 p.1", "§2 p.2", "§2 p.2–3", "§3 p.4"]
    assert (o.progress.done, o.progress.total) == (0, 4) and o.path_id is None


def test_progress_follows_the_page_done_not_k(env):
    repo, _, ids = env
    pid, t, _ = _organized_reading_page(env)                     # unit b's page, with a body
    assert out(env).units[1].status == "reading"
    assert answer_check(repo, None, "u1", pid, "s3", 0, now=NOW, new_id=ids).correct
    o = out(env)                                                 # evidence on its point: still 在读
    assert o.units[1].status == "reading" and o.progress.done == 0
    mark_done(repo, "u1", pid, now=NOW, new_id=ids)
    o = out(env)
    assert o.units[1].status == "done" and (o.progress.done, o.progress.total) == (1, 4)
    assert o.path_id == f"path-{rid(env)}"
    assert o.units[1].teaches[0].id == t                          # K colour shown, whatever it is
    assert o.units[1].teaches[0].mix


def test_a_read_unit_whose_point_k_does_not_hold_is_still_read(env):
    repo, _, ids = env
    pid, _, _ = _organized_reading_page(env)
    mark_done(repo, "u1", pid, now=NOW, new_id=ids)
    u = out(env).units[1]
    assert u.status == "done" and not u.teaches[0].known


def test_skipped_units_are_not_counted(env):
    goal(env)
    skip(env, "ru_d")
    o = out(env)
    assert o.units[3].status == "skipped" and (o.progress.done, o.progress.total) == (0, 3)


def test_a_started_page_without_a_body_is_reading(env):
    repo, _, ids = env
    goal(env)
    analyze(env, "ru_b", B_OUT)
    start_unit_page(repo, "u1", rid(env), "ru_b", now=NOW, new_id=ids)
    assert out(env).units[1].status == "reading"


def test_the_goal_carries_its_reading_on_the_map_and_the_graph(env):
    repo, _, _ = env
    goal(env)
    snap = repo.load("u1")
    mid = snap.state.readings[rid(env)].map_id
    m = map_out(snap.state, snap.k, mid, NOW, labels=LABELS)
    assert m.reading is not None and m.reading.units[0].location == "§1 p.1"
    g = graph_out(snap.state, snap.k, NOW,
                  source_info=SourceInfo(section_labels={"p1": LABELS}))
    mine = next(x for x in g.goals if x.map_id == mid)
    assert mine.reading is not None and mine.reading.progress.total == 4
    assert mine.reading.units[2].location == "§2 p.2–3"


def test_sections_units_read_are_covered_and_labelled(env):
    goal(env)
    mid = st(env).readings[rid(env)].map_id
    assert uncovered(SECS, []) != []
    read = {sec for _, sec in _read_sections(st(env), mid)}
    assert uncovered(SECS, [], read) == []
    skip(env, "ru_d")
    read = {sec for _, sec in _read_sections(st(env), mid)}
    assert uncovered(SECS, [], read) == [("x2", False)]          # a skipped unit's section: 没讲
    analyze(env, "ru_c", {"teaches": [{"name": "多头注意力", "summary": "拆成几份", "atomic": True}],
                          "uses": [], "focus": []})
    t = next(u for u in st(env).readings[rid(env)].units if u.unit_id == "ru_c").teaches[0]
    assert _read_at(st(env))[(t, "p1", "x1")] == read_in(TITLE, 3) == f"在《{TITLE}》的第 3 段里读"
    assert uncovered(SECS, [LinkRow(node_id=t, section_id="x1", source_id="p1", role="teaches",
                                    by="reading")]) == [("x0", False), ("x2", False)]


def test_an_appendix_section_is_labelled_by_its_number():
    secs = [SimpleNamespace(section_id=str(i), ord=i, title=t)
            for i, t in enumerate(["A Model Architecture", "A.1.2 Alignment Model", "Appendix notes"])]
    assert list(section_labels(secs).values()) == ["§A", "§A.1.2", "§Appendix"]


def test_a_point_he_marked_familiar_is_not_said_to_be_unlearned():
    """Live 2026-11-04: 摸底 marked 熟悉, 读这一段 still said 你还没学 and listed 5 prerequisites."""
    from types import SimpleNamespace

    from evowiki.goals.contracts import Band
    from evowiki.goals.regions import GoalMap, MapNode
    from evowiki.wikiapp.reading_view import _met, _under_met

    bands = {"b.term": Band.SHAKY, "p.term": Band.GAP, "q.term": Band.GAP, "r.term": Band.LIKELY}
    no_cells = {"s"}
    kv = SimpleNamespace(aspects=lambda c: [] if c in no_cells else ["term"],
                         cell=lambda cell: SimpleNamespace(band=bands.get(cell, Band.GAP)))
    m = GoalMap.model_construct(nodes=())
    nodes = {"b": MapNode(id="b", kind="region", name="b"), "s": MapNode(id="s", kind="concept", name="s", parent="b"),
             "p": MapNode(id="p", kind="concept", name="p", parent="b"), "q": MapNode(id="q", kind="concept", name="q")}
    m = SimpleNamespace(node=nodes.get)
    assert _met(kv, "b") and _met(kv, "r") and not _met(kv, "q")
    assert _under_met(kv, m, "s")              # under the block he marked, no evidence of its own
    assert not _under_met(kv, m, "p")          # its own evidence says he does not know it
    assert not _under_met(kv, m, "q")          # on no marked block
