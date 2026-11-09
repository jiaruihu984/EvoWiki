"""GraphChanges (plan T6): the graph's splits as RSI change rows (track G, prefix g_),
newest first, plain Chinese without ids, and undo through the repo in one commit."""

from datetime import timedelta

import pytest

from evowiki.nodes import GraphChange
from evowiki.teach.change_source import ChangeSource, source_for
from evowiki.wikiapp.events import GraphSaved, WikiEvent
from evowiki.wikiapp.graph_changes import GraphChanges, GraphUndoRefused
from evowiki.wikiapp.llm import require
from evowiki.wikiapp.node_split import split_node
from evowiki.wikiapp.repo import Commit
from tests.wikiapp.graph_seed import IDS, MM, NOW, replays_equal, seed
from tests.wikiapp.script import Script
from tests.wikiapp.split_answers import PURPOSE as SPLIT
from tests.wikiapp.split_answers import answer, parts

T1, T2, T3 = (NOW + timedelta(days=d) for d in (1, 2, 3))


def env():
    repo, ids = seed()
    s = Script().push(SPLIT, answer())
    sp = split_node(repo, require(s.gateway()), "u1", MM, reason="confused", source="dont_get",
                    words="完全看不懂", now=T1, new_id=ids)
    s.push(SPLIT, parts("左边的列数", "右边的行数"))
    again = split_node(repo, require(s.gateway()), "u1", sp.children[1], reason="requested",
                       source="chat", now=T2, new_id=ids)
    return repo, ids, sp, again


def test_rows_are_newest_first_track_g_and_say_what_happened_in_words():
    repo, ids, sp, again = env()
    src: ChangeSource = GraphChanges(repo, ids)          # satisfies the RSI hook (typed)
    assert src.prefix == "g_"
    assert source_for([src], sp.change.change_id) is src
    rows = src.list("u1")
    assert [r.patch_id for r in rows] == [again.change.change_id, sp.change.change_id]
    m, s = rows
    assert all(r.track == "G" and r.layer == 0 and r.target == "知识图谱" for r in rows)
    assert all(r.status == "personal_accepted" for r in rows)
    assert s.summary == "把「矩阵乘法」拆成了几个小点"
    assert s.why == "你点了看不懂" and s.signals == ("完全看不懂",)
    assert s.after == ""                                # a split carries no note
    assert m.can_undo and not s.can_undo              # the later split touched a split child
    assert not any(i in r.summary + r.why + r.after for r in rows for i in (*IDS, *sp.children))


def test_undo_runs_through_the_repo_and_unblocks_the_earlier_change():
    repo, ids, sp, again = env()
    src = GraphChanges(repo, ids)
    with pytest.raises(GraphUndoRefused, match="先撤销后面那次"):
        src.undo("u1", sp.change.change_id, now=T3)
    row = src.undo("u1", again.change.change_id, now=T3)
    assert row.status == "rolled_back" and not row.can_undo and not row.summary.startswith("已撤销")
    snap = repo.load("u1")
    assert snap.state.graph.node(sp.children[1]) is not None
    assert repo.wiki_log("u1")[-1].body.action == "undone"        # type: ignore[union-attr]
    assert replays_equal(repo, snap)
    row = src.undo("u1", sp.change.change_id, now=T3)
    assert row.status == "rolled_back"
    g = repo.load("u1").state.graph
    assert g.children(MM) == () and all(g.node(c) is None for c in sp.children)
    with pytest.raises(GraphUndoRefused, match="已经撤销过"):
        src.undo("u1", sp.change.change_id, now=T3)
    with pytest.raises(LookupError):
        src.undo("u1", "g_nothing", now=T3)


def test_migrate_and_reset_records_are_not_listed_but_still_refuse_undo():
    repo, ids = seed()
    snap = repo.load("u1")
    mig = GraphChange(change_id="g_migrate_1", at=NOW, kind="migrate", summary="转换了 4 个节点",
                      why="旧的两层结构转成统一节点图")
    rst = GraphChange(change_id="g_reset_1", at=NOW, kind="reset", summary="清空节点图（没有备份）",
                      why="节点图清空（旧结构仍可重放）")
    g = snap.state.graph.updated(changes=[mig, rst])
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id="wev_x", ts=NOW,
                                             body=GraphSaved(graph=g, action="migrated"))]))
    src = GraphChanges(repo, ids)
    assert src.list("u1") == []
    assert [c.change_id for c in repo.load("u1").state.graph.changes] == ["g_migrate_1",
                                                                           "g_reset_1"]
    for cid in ("g_migrate_1", "g_reset_1"):
        with pytest.raises(GraphUndoRefused, match="不能撤销"):
            src.undo("u1", cid, now=T1)
    # a later split is listed; the migration still is not
    s = Script().push(SPLIT, answer())
    sp = split_node(repo, require(s.gateway()), "u1", MM, reason="confused", source="dont_get",
                    words="", now=T1,
                    new_id=ids)
    assert [r.patch_id for r in src.list("u1")] == [sp.change.change_id]
