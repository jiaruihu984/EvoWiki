"""Path edits 2 (assistant-actions task 2): `commit_path_edit` + `on_path_changed` hooks, add at an
index, 「拆成多页」 `split_row` (children rows in place, one commit, undo) on a seeded path with a
scripted call of the shared one-level split (goals.enter, source path_row) — no real LLM."""

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter

from evowiki.goals.intake import QUESTION_RULE, IntakeError
from evowiki.goals.path_edit import (
    AddEdit,
    MoveEdit,
    RemoveEdit,
    ReplaceEdit,
    ReplaceItem,
)
from evowiki.wikiapp.events import WikiEvent
from evowiki.wikiapp.llm import LLMUnavailable, require
from evowiki.wikiapp.node_state import atomic_of
from evowiki.wikiapp.path_edits import (
    ROWS_UNDO,
    commit_path_edit,
    edit_path,
    off_path_changed,
    on_path_changed,
    split_row,
    undo_split_row,
)
from evowiki.wikiapp.path_order import shown, startable_page_ids
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict, WikiInvalid
from evowiki.wikiapp.state import replay
from evowiki.wikiapp.wiki_schemas import PathAddIn
from tests.wikiapp.script import Script
from tests.wikiapp.split_answers import PURPOSE, keep
from tests.wikiapp.split_answers import parts as two
from tests.wikiapp.test_path_edits import NAMES, PATH_ID, Ids, seed_events

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
P1, P2, P3 = (f"{PATH_ID}-p{i}" for i in (1, 2, 3))

ANSWER = {"parts": [
    {"name": "抽样的随机性", "summary": "同一总体每次抽样结果不同", "atomic": True,
     "aspects": ["term"]},
    {"name": "样本均值的分布", "summary": "样本均值本身也是随机变量", "atomic": True,
     "aspects": ["term", "interpret"]}],
    "recommended": [{"from": "抽样的随机性", "to": "样本均值的分布", "why": "先弄清随机，再看均值的分布"}]}


@pytest.fixture
def env():
    repo, ids = MemoryRepo(["u1"]), Ids()
    aspect, wiki = seed_events(ids)
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    return repo, ids


def rec(repo):
    return repo.load("u1").state.paths[PATH_ID]


def split(env, key=P2, answer=ANSWER, *more):
    repo, ids = env
    gw = require(Script().push(PURPOSE, answer, *more).gateway())
    return split_row(repo, gw, "u1", PATH_ID, key, now=NOW, new_id=ids)


@pytest.fixture
def seen():
    calls = []

    def hook(user_id, path_id, before, after, source):
        calls.append((user_id, path_id, before, after, source))

    on_path_changed(hook)
    yield calls
    off_path_changed(hook)


def test_add_at_index_via_the_endpoint_function(env):
    repo, ids = env
    out = edit_path(repo, "u1", PATH_ID, PathAddIn(op="add", request_id="test-1", title="插在中间", to_index=1),
                    now=NOW, new_id=ids)
    assert [i.title for i in out.path][:3] == ["第 1 页", "插在中间", "第 2 页"]
    assert out.path[1].mine
    with pytest.raises(WikiInvalid):
        edit_path(repo, "u1", PATH_ID, PathAddIn(op="add", request_id="test-2", title="太远", to_index=9),
                  now=NOW, new_id=ids)


def test_a_title_only_page_the_learner_adds_first_is_written_first(env):
    """D34: 加一页 sent a title and no node; the row was shown but never stored as a page, so
    「下一页」 skipped it. It gets a node of its own (named like the title) in the same commit."""
    repo, ids = env
    out = edit_path(repo, "u1", PATH_ID, PathAddIn(op="add", request_id="test-3", title="我自己加的一页", to_index=0),
                    now=NOW, new_id=ids)
    assert out.path[0].title == "我自己加的一页" and out.path[0].mine
    st = repo.load("u1").state
    node = st.graph.node(shown(rec(repo))[0].node_ids[0])
    assert node is not None and node.name == "我自己加的一页"
    first = next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)
    page = repo.load("u1").state.pages[first.page_id]
    assert page.title == "我自己加的一页" and page.targets == (node.id,)


def test_every_page_removed_and_one_added_the_path_writes_it(env):
    """D34: delete every row, add one of your own: the path said 「这条路径的页都已经开始了」."""
    from evowiki.wikiapp.paths import path_out
    from evowiki.wikiapp.wiki_schemas import PathRemoveIn
    repo, ids = env
    for _ in range(len(shown(rec(repo)))):
        edit_path(repo, "u1", PATH_ID, PathRemoveIn(op="remove", page_key=shown(rec(repo))[0].key), now=NOW, new_id=ids)
    edit_path(repo, "u1", PATH_ID, PathAddIn(op="add", request_id="test-4", title="只剩我这一页"), now=NOW, new_id=ids)
    snap = repo.load("u1")
    view = path_out(snap.state, snap.k, PATH_ID, NOW)
    assert [p.title for p in view.pages] == ["只剩我这一页"] and view.next_index is not None
    got = next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)
    assert repo.load("u1").state.pages[got.page_id].title == "只剩我这一页"


def test_add_with_a_node_at_an_index_is_a_startable_page(env):
    repo, ids = env
    edit_path(repo, "u1", PATH_ID, PathAddIn(op="add", request_id="test-5", title="贝叶斯", node_id="n5", to_index=0),
              now=NOW, new_id=ids)
    r = rec(repo)
    assert shown(r)[0].node_ids == ("n5",)
    assert startable_page_ids(r)[0] is None and len(startable_page_ids(r)) == 4


def test_commit_path_edit_commits_once_and_runs_the_hooks_with_the_source(env, seen):
    repo, ids = env
    before = rec(repo)
    n = len(repo.wiki_log("u1"))
    new = commit_path_edit(repo, "u1", PATH_ID,
                           [MoveEdit(page_key=P3, to_index=0), RemoveEdit(page_key=P1)],
                           now=NOW, new_id=ids, source="assistant")
    assert len(repo.wiki_log("u1")) == n + 1 and rec(repo) == new
    [(uid, pid, b, a, src)] = seen
    assert (uid, pid, src) == ("u1", PATH_ID, "assistant") and b == before and a == new
    commit_path_edit(repo, "u1", PATH_ID, [], now=NOW, new_id=ids, source="bot")
    assert len(seen) == 1                                    # a no-op commits nothing, no hook


def test_commit_path_edit_refuses_a_row_the_path_does_not_show(env, seen):
    repo, ids = env
    with pytest.raises(WikiInvalid):
        commit_path_edit(repo, "u1", PATH_ID, [RemoveEdit(page_key="u-nope")], now=NOW,
                         new_id=ids, source="bot")
    with pytest.raises(LookupError):
        commit_path_edit(repo, "u1", "path-nope", [RemoveEdit(page_key=P1)], now=NOW,
                         new_id=ids, source="bot")
    assert seen == []


def test_a_failing_hook_is_logged_and_does_not_undo_the_edit(env, caplog):
    repo, ids = env

    def boom(*a):
        raise RuntimeError("boom")

    on_path_changed(boom)
    try:
        commit_path_edit(repo, "u1", PATH_ID, [RemoveEdit(page_key=P1)], now=NOW, new_id=ids,
                         source="user")
    finally:
        off_path_changed(boom)
    assert "boom" in caplog.text and [i.key for i in shown(rec(repo))] == [P2, P3]


def test_every_user_edit_calls_the_hooks_too(env, seen):
    repo, ids = env
    edit_path(repo, "u1", PATH_ID, PathAddIn(op="add", request_id="test-6", title="x"), now=NOW, new_id=ids)
    assert [c[4] for c in seen] == ["user"]


def test_split_row_puts_the_children_in_the_rows_place_titled_with_their_names(env, seen):
    repo, ids = env
    out = split(env)
    assert [i.title for i in out.path] == ["第 1 页", "抽样的随机性", "样本均值的分布", "第 3 页"]
    assert all("（" not in i.title for i in out.path)
    kids = out.path[1:3]
    assert all(k.mine and k.key.startswith("u-") and len(k.node_ids) == 1 for k in kids)
    g = repo.load("u1").state.graph
    assert {g.node(k.node_ids[0]).name for k in kids} == {"抽样的随机性", "样本均值的分布"}
    assert set(g.children("n2")) == {k.node_ids[0] for k in kids}
    assert out.undo == g.changes[-1].change_id
    a, b = (k.node_ids[0] for k in kids)
    rec_links = [r for r in g.relations if r.kind == "recommended" and {r.src, r.dst} == {a, b}]
    assert [(r.src, r.dst, r.source) for r in rec_links] == [(a, b, "split")]
    assert [c[4] for c in seen] == ["user"]
    # the stored path teaches the children, so 「下一页」 can start them in the shown order
    assert len(startable_page_ids(rec(repo))) == 4
    next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)           # the first row
    second = next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)
    assert repo.load("u1").state.pages[second.page_id].targets == (kids[0].node_ids[0],)


def test_split_row_is_one_commit_and_replays_equal(env):
    repo, _ = env
    n = len(repo.wiki_log("u1"))
    split(env)
    log = repo.wiki_log("u1")
    assert [e.body.action for e in log[n:] if hasattr(e.body, "action")] == ["split", "user_edit"]
    ta = TypeAdapter(list[WikiEvent])
    assert replay(ta.validate_json(ta.dump_json(log))) == repo.load("u1").state


def test_split_row_with_an_unusable_answer_writes_nothing(env, seen):
    repo, _ = env
    before = (len(repo.wiki_log("u1")), rec(repo))
    one = {"parts": [ANSWER["parts"][0]]}
    with pytest.raises(IntakeError):                         # asked once more, then refused
        split(env, P2, one, one)
    assert (len(repo.wiki_log("u1")), rec(repo)) == before and seen == []


def test_split_row_left_whole_keeps_the_path_and_marks_the_node_atomic(env, seen):
    repo, _ = env
    before = rec(repo)
    graph = repo.load("u1").state.graph
    out = split(env, answer=keep("它已经是一件事"))
    assert out.kept == "它已经是一件事" and out.undo in (None, "")
    assert rec(repo).path == before.path and [i.key for i in out.path] == [P1, P2, P3]
    st = repo.load("u1").state
    assert st.graph.nodes == graph.nodes and st.graph.relations == graph.relations
    assert atomic_of(st, "n2") is True and seen == []          # no path edit, no hook


def test_split_row_with_a_usable_retry_is_written(env):
    out = split(env, P2, {"parts": [ANSWER["parts"][0]]}, ANSWER)
    assert [i.title for i in out.path][1:3] == ["抽样的随机性", "样本均值的分布"]


def test_split_row_of_several_nodes_is_one_row_per_node_without_any_call(env, seen):
    repo, ids = env
    out = split_row(repo, None, "u1", PATH_ID, P3, now=NOW, new_id=ids)      # n3 and n4, no gateway
    assert [i.title for i in out.path] == ["第 1 页", "第 2 页", NAMES["n3"], NAMES["n4"]]
    assert [i.node_ids for i in out.path[2:]] == [["n3"], ["n4"]] and out.undo == ROWS_UNDO
    assert all(i.mine for i in out.path[2:]) and [c[4] for c in seen] == ["user"]
    assert len(startable_page_ids(rec(repo))) == 4
    g = repo.load("u1").state.graph
    assert g.children("n3") == ()                            # the graph did not change
    back = undo_split_row(repo, "u1", PATH_ID, P3, ROWS_UNDO, now=NOW, new_id=ids)
    assert [i.title for i in back.path] == ["第 1 页", "第 2 页", "第 3 页"]
    with pytest.raises(WikiInvalid):                         # nothing active any more
        undo_split_row(repo, "u1", PATH_ID, P3, ROWS_UNDO, now=NOW, new_id=ids)


def test_split_row_of_one_node_needs_a_gateway_and_a_shown_row(env):
    repo, ids = env
    with pytest.raises(LLMUnavailable):
        split_row(repo, None, "u1", PATH_ID, P2, now=NOW, new_id=ids)
    with pytest.raises(WikiInvalid):
        split(env, key="u-nope")


def test_split_row_works_on_a_row_the_user_added_and_on_a_child(env):
    repo, ids = env
    edit_path(repo, "u1", PATH_ID, PathAddIn(op="add", request_id="test-7", title="贝叶斯", node_id="n5"),
              now=NOW, new_id=ids)
    key = shown(rec(repo))[-1].key
    assert key.startswith("u-")
    out = split(env, key=key)
    assert [i.title for i in out.path][-2:] == ["抽样的随机性", "样本均值的分布"]
    again = split(env, key=out.path[-1].key, answer=two("均值的期望", "均值的方差"))
    assert [i.title for i in again.path][-3:] == ["抽样的随机性", "均值的期望", "均值的方差"]


def test_undo_split_row_brings_the_row_and_the_graph_back(env, seen):
    repo, ids = env
    before = repo.load("u1").state.graph
    out = split(env)
    back = undo_split_row(repo, "u1", PATH_ID, P2, out.undo, now=NOW, new_id=ids)
    assert [i.title for i in back.path] == ["第 1 页", "第 2 页", "第 3 页"]
    assert repo.load("u1").state.graph.children("n2") == before.children("n2")
    assert [c[4] for c in seen] == ["user", "user"]
    with pytest.raises(WikiInvalid):
        undo_split_row(repo, "u1", PATH_ID, P2, out.undo, now=NOW, new_id=ids)
    again = split(env)                                       # the restored row splits again
    assert [i.title for i in again.path][1:3] == ["抽样的随机性", "样本均值的分布"]


def test_undo_split_row_is_refused_once_a_child_was_split_further(env):
    repo, ids = env
    out = split(env)
    split(env, key=out.path[1].key, answer=two("子一", "子二"))
    with pytest.raises(WikiConflict):
        undo_split_row(repo, "u1", PATH_ID, P2, out.undo, now=NOW, new_id=ids)


def test_startable_page_ids_respects_a_replaced_row(env):
    repo, ids = env
    next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)          # p1 started
    split(env)                                                        # splits p2
    r = rec(repo)
    got = startable_page_ids(r)
    assert len(got) == 4 and got[0] is not None and got[1:] == [None, None, None]
    assert P2 not in [i.key for i in shown(r)]
    assert NAMES["n2"] not in [i.title for i in shown(r)]


def test_added_keys_are_unique(env):
    repo, ids = env
    commit_path_edit(repo, "u1", PATH_ID, [AddEdit(page_key="u-x", title="a")], now=NOW,
                     new_id=ids, source="bot")
    with pytest.raises(WikiInvalid):
        commit_path_edit(repo, "u1", PATH_ID, [AddEdit(page_key="u-x", title="b")], now=NOW,
                         new_id=ids, source="bot")


def test_undo_split_row_refuses_a_change_that_is_not_this_rows_split(env):
    repo, ids = env
    first = split(env)                                       # p2 -> two rows
    other = split(env, key=P1, answer=two("甲", "乙"))       # p1 -> two rows, its own change
    n = len(repo.wiki_log("u1"))
    for key, token in ((P2, other.undo), (P1, first.undo), (P2, ROWS_UNDO), (P2, "g_nope")):
        with pytest.raises((WikiInvalid, LookupError)):
            undo_split_row(repo, "u1", PATH_ID, key, token, now=NOW, new_id=ids)
    assert len(repo.wiki_log("u1")) == n                     # nothing written


def test_edits_on_hidden_rows_and_taken_keys_are_refused(env):
    repo, ids = env
    out = split(env)
    with pytest.raises(WikiInvalid):                         # p2 is hidden now
        commit_path_edit(repo, "u1", PATH_ID, [MoveEdit(page_key=P2, to_index=0)], now=NOW,
                         new_id=ids, source="bot")
    with pytest.raises(WikiInvalid):
        commit_path_edit(repo, "u1", PATH_ID, [RemoveEdit(page_key=P2)], now=NOW, new_id=ids,
                         source="bot")
    taken = out.path[1].key
    with pytest.raises(WikiInvalid):
        commit_path_edit(repo, "u1", PATH_ID,
                         [ReplaceEdit(page_key=P1, items=(ReplaceItem(page_key=taken, title="x"),))],
                         now=NOW, new_id=ids, source="bot")


def test_a_started_path_page_is_titled_with_its_row_not_a_position(env):
    repo, ids = env
    split(env)
    first = next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)
    second = next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)
    pages = repo.load("u1").state.pages
    assert pages[first.page_id].title == "第 1 页" and pages[second.page_id].title == "抽样的随机性"
    assert all("（" not in p.title for p in pages.values())


def test_split_rows_carry_the_question_each_child_answers_and_never_an_abstract(env):
    q = ["同样的总体为什么每次抽出来不一样", "多抽几次，平均数会怎么跳"]
    repo, ids = env
    sc = Script().push(PURPOSE, {**ANSWER, "parts": [{**c, "question": x}
                                                     for c, x in zip(ANSWER["parts"], q)]})
    out = split_row(repo, require(sc.gateway()), "u1", PATH_ID, P2, now=NOW, new_id=ids)
    kids = [i for i in out.path if i.title in ("抽样的随机性", "样本均值的分布")]
    assert [i.question for i in kids] == q
    # stored as path pages too, so the line survives the row being started
    stored = {p.title: p.question for p in rec(repo).path.pages}
    assert stored["抽样的随机性"] == q[0] and stored["样本均值的分布"] == q[1]
    # a split whose answer has no question leaves the line empty, not the definition
    plain = split(env, P1, two("甲", "乙"))
    assert [i.question for i in plain.path if i.title in ("甲", "乙")] == ["", ""]
    # only 拆成多页 asks for it, as a question; the call says where it came from
    [req] = sc.requests(PURPOSE)
    assert QUESTION_RULE in req.prompt and req.params.get("source") == "path_row"
