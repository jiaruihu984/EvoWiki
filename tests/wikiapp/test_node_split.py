"""wikiapp.node_split (spec 2026-10-29: graph.split merged into goals.enter): every 细分 entrance asks
the one shared one-level split (scripted, no real LLM) with its reason, the learner's words and the
node's evidence, under its own source; the answer is parts (new or reused nodes, each with the
model's atomic, recommended among them) committed as an undoable "split" change, or the node left
whole (`kept`: only the atomic mark). The parent's evidence stays on the parent."""

from datetime import timedelta

import pytest

from evowiki.goals.contracts import Aspect, Band, cell_id
from evowiki.goals.intake import IntakeError
from evowiki.nodes import UndoConflict, undo
from evowiki.wikiapp.llm import LLM_FAILURES, require
from evowiki.wikiapp.node_split import ask_split, split_events, split_node
from tests.wikiapp.graph_seed import DET, LA, MM, NOW, VEC, mixed_answers, replays_equal, seed
from tests.wikiapp.script import Script
from tests.wikiapp.split_answers import PURPOSE, answer, keep, parts

LATER = NOW + timedelta(days=1)


def run(repo, ids, *outs, node=MM, reason="confused", source="dont_get", words=""):
    s = Script().push(PURPOSE, *outs)
    done = split_node(repo, require(s.gateway()), "u1", node, reason=reason, source=source,
                      words=words, now=LATER, new_id=ids)
    return s, done


def test_a_split_writes_parts_atomic_recommended_and_k_in_one_undoable_change():
    repo, ids = seed()
    mixed_answers(repo, ids)
    before = repo.load("u1")
    n_wiki = len(repo.wiki_log("u1"))
    s, done = run(repo, ids, answer(), reason="requested", source="chat", words="拆细一点")
    assert s.calls(PURPOSE) == 1
    a, b, c = done.children
    assert all(x.startswith("n_") for x in done.children) and not done.kept
    g = done.snap.state.graph
    assert [g.node(x).name for x in done.children] == ["一行乘一列", "维度要对上", "不满足交换律"]
    assert g.node(a).definition == "结果的每个数是一行和一列的点积"
    assert set(g.children(MM)) == {a, b, c}
    assert all(r.source == "split" for r in g.relations if r.kind == "part_of" and r.dst == MM
               and r.src in done.children)
    # only recommended among the parts: no prerequisite / kind_of / related from a split
    assert {(r.kind, r.src, r.dst, r.phrase) for r in g.relations
            if r.source == "split" and r.kind != "part_of"} == {
        ("recommended", a, c, "先会算才看得出顺序有影响")}
    names = done.snap.state.names
    assert (names[MM].atomic, names[a].atomic, names[c].atomic) == (False, True, False)
    k = done.snap.k
    assert k.aspects(a) == (Aspect.PROCEDURE, Aspect.TERM)
    assert k.aspects(c) == (Aspect.INTERPRET,)                    # 心情: not an aspect, dropped
    view = done.snap.kview(LATER)
    assert all(view.cell(cell_id(x, asp)).band is Band.NONE
               for x in done.children for asp in k.aspects(x))
    for asp in before.k.aspects(MM):                               # the parent keeps its evidence
        assert view.cell(cell_id(MM, asp)) == before.kview(LATER).cell(cell_id(MM, asp))
    ch = done.change
    assert ch is not None and ch.kind == "split"
    assert ch.why == "你要求把它拆细" and ch.words == "拆细一点"
    assert repo.wiki_log("u1")[n_wiki:][-1].body.type == "concepts_named"
    assert replays_equal(repo, done.snap)
    back = undo(g, ch.change_id)
    assert back.nodes == before.state.graph.nodes and back.relations == before.state.graph.relations


def test_dont_get_prompt_has_why_words_evidence_keep_rule_and_the_graph_place():
    repo, ids = seed()
    mixed_answers(repo, ids)
    s, _ = run(repo, ids, answer(), words="AB 和 BA 到底为什么不一样")
    req = s.requests(PURPOSE)[0]
    assert req.prompt_version == "enter@v5" and req.params["source"] == "dont_get"
    p = req.prompt
    for want in ("学习者说这个点看不懂", "看不懂只是需要换个讲法", "AB 和 BA 到底为什么不一样",
                 "题目：AB 和 BA 一样吗？；选了「一样」，答错了", "这是他知识图谱里「线性代数」下面的「矩阵乘法」",
                 "「线性代数」包含这几部分", "「行列式」", "就不拆：\"parts\": []", "atomic",
                 "existing_id", "不拆时："):
        assert want in p, want
    # a node off any goal map: no goal line; only 拆成多页 asks for the row's question
    assert "学习目标：「" not in p and "question" not in p
    for gone in ("一页", "钻", "prerequisite", "kind_of", "\"after\"", "至少拆成 2 个"):
        assert gone not in p, gone


def test_inconsistent_answers_locate_the_weak_spot_without_forcing_right_and_wrong_apart():
    repo, ids = seed()
    mixed_answers(repo, ids)
    s, done = run(repo, ids, answer(), reason="inconsistent", source="chat")
    req = s.requests(PURPOSE)[0]
    assert req.params["source"] == "chat"
    assert "答题有对有错" in req.prompt and "不要求把答对的和答错的分到不同的部分" in req.prompt
    assert "答对了" in req.prompt and "答错了" in req.prompt
    # the answers stay the parent's: none is copied onto a part
    view = done.snap.kview(LATER)
    assert view.cell(cell_id(MM, Aspect.TERM)).direct > 0
    assert all(view.cell(cell_id(x, asp)).direct == 0 and view.cell(cell_id(x, asp)).band is Band.NONE
               for x in done.children for asp in done.snap.k.aspects(x))
    assert all(view.cell(cell_id(i, Aspect.TERM)).direct == 0 for i in (LA, DET, VEC))


def test_a_chat_request_passes_his_words_and_its_own_source():
    repo, ids = seed()
    s, done = run(repo, ids, parts("点积", "维度"), reason="requested", source="chat",
                  words="把矩阵乘法再拆细点")
    req = s.requests(PURPOSE)[0]
    assert req.params == {"source": "chat"}
    assert "学习者明确要求把这个点拆细" in req.prompt and "不要为了凑数" in req.prompt
    assert "把矩阵乘法再拆细点" in req.prompt
    assert done.snap.state.names[done.children[0]].atomic is None     # not judged: kept None


def test_an_existing_node_is_reused_by_id_only_its_part_of_is_added_and_undone():
    repo, ids = seed()
    before = repo.load("u1").state.graph
    _, done = run(repo, ids, {"parts": [
        {"name": "向量", "existing_id": VEC, "atomic": True},
        {"name": "一行乘一列", "summary": "点积", "atomic": True}]}, reason="requested",
        source="chat")
    v, new = done.children
    assert v == VEC and new.startswith("n_")
    g = done.snap.state.graph
    assert [n.id for n in g.nodes].count(VEC) == 1
    assert {r.src for r in g.relations if r.kind == "part_of" and r.dst == MM} == {VEC, new}
    ch = done.change
    assert ch is not None and [n.id for n in ch.added_nodes] == [new] and ch.node_ids == (MM, new, VEC) \
        or ch.node_ids == (MM, VEC, new)
    back = undo(g, ch.change_id)
    assert back.nodes == before.nodes and back.relations == before.relations


def test_a_node_that_should_not_be_split_is_kept_whole_as_a_normal_result():
    repo, ids = seed()
    n_aspect = len(repo.aspect_log("u1"))
    g0 = repo.load("u1").state.graph
    s, done = run(repo, ids, keep("「矩阵乘法」已经是一件事，换个例子讲更清楚"))
    assert s.calls(PURPOSE) == 1                       # a normal answer: not asked again
    assert done.kept == "「矩阵乘法」已经是一件事，换个例子讲更清楚"
    assert done.children == () and done.change is None
    assert done.snap.state.graph == g0
    assert done.snap.state.names[MM].atomic is True    # relative to now: 继续拆细 can still split
    assert len(repo.aspect_log("u1")) == n_aspect      # K untouched


def test_parts_overlapping_a_sibling_or_reusing_an_ancestor_are_dropped_then_asked_again():
    repo, ids = seed()
    bad = {"parts": [{"name": "行列式"}, {"name": "线代总览", "existing_id": LA},
                     {"name": "一行乘一列"}]}
    s, done = run(repo, ids, bad, parts("一行乘一列", "维度要对上"), reason="requested",
                  source="chat")
    assert s.calls(PURPOSE) == 2
    assert "上一次的拆法不能用" in s.requests(PURPOSE)[1].prompt
    names = [done.snap.state.graph.node(x).name for x in done.children]
    assert names == ["一行乘一列", "维度要对上"]
    assert LA not in done.children and DET not in done.children


def test_an_unusable_answer_twice_fails_closed_and_writes_nothing():
    repo, ids = seed()
    n = len(repo.wiki_log("u1")), len(repo.aspect_log("u1"))
    with pytest.raises(IntakeError):
        run(repo, ids, parts("一行乘一列"), parts("矩阵 乘法", "一行乘一列"))
    with pytest.raises(LLM_FAILURES):
        run(repo, ids, "这不是 JSON")
    with pytest.raises(LookupError):
        run(repo, ids, answer(), node="nd_nothing")
    assert (len(repo.wiki_log("u1")), len(repo.aspect_log("u1"))) == n


def test_only_the_path_row_split_asks_each_parts_question():
    repo, ids = seed()
    snap = repo.load("u1")
    s = Script().push(PURPOSE, {"parts": [
        {"name": "一行乘一列", "question": "结果里每个数怎么来"}, {"name": "维度要对上"}]})
    res = ask_split(require(s.gateway()), snap, MM, reason="requested", source="path_row",
                    now=LATER, question=True)
    p = s.requests(PURPOSE)[0].prompt
    assert "question：学习路径上这一页下面那行小字" in p
    ev = split_events(snap, MM, res, reason="requested", words="", now=LATER, new_id=ids)
    assert list(ev.questions.values()) == ["结果里每个数怎么来", ""]
    assert ev.added == ev.children


def test_a_later_split_on_a_part_blocks_undoing_the_first():
    repo, ids = seed()
    _, done = run(repo, ids, answer())
    _, deeper = run(repo, ids, parts("点积", "求和"), node=done.children[0])
    assert done.change is not None
    with pytest.raises(UndoConflict):
        undo(deeper.snap.state.graph, done.change.change_id)
