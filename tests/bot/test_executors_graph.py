"""Task 4 of assistant-actions: the ledger executors of the assistant's graph / review-card / rule /
settings actions (`bot.executors_graph`) on real Postgres with a seeded graph + path (and an
organized, learned page for the card kinds) and a SCRIPTED LLM - nothing here calls a model."""

from datetime import timedelta

import pytest

from evowiki.bot.actions import STALE_DETAIL, ActionLedger, NotDecidable, can_undo
from evowiki.bot.bot_schemas import ActionOut
from evowiki.bot.executors_assistant import SCOPES, action_level, register_assistant_executors
from evowiki.bot.executors_graph import GRAPH_SCOPES, MOVED, graph_fingerprint
from evowiki.bot.executors_profile import FORGET_KIND
from evowiki.bot.executors_profile import KIND as PROFILE_FACT
from evowiki.bot.settings import BotSettings
from evowiki.goals.contracts import CheckItem
from evowiki.goals.review import schedule
from evowiki.nodes import Relation
from evowiki.store import bot as store
from evowiki.wikiapp.events import PageSaved, WikiEvent
from evowiki.wikiapp.followup import PURPOSE as CONFIRM_PURPOSE
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.node_state import atomic_of
from evowiki.wikiapp.path_order import shown
from evowiki.wikiapp.repo import Commit, PgRepo, WikiConflict
from evowiki.wikiapp.review import card_id, goal_concepts, review_today, sources
from tests.runtime.helpers import make_user
from tests.wikiapp.partial_write_check import assert_partial_write
from tests.wikiapp.script import Script
from tests.wikiapp.split_answers import PURPOSE as SPLIT_PURPOSE
from tests.wikiapp.split_answers import answer as split_answer
from tests.wikiapp.split_answers import keep, parts
from tests.wikiapp.test_goal import Ids as GoalIds
from tests.wikiapp.test_path_edits import NOW, PATH_ID, Ids, seed_events
from tests.wikiapp.test_review_flow import LATER, organized_learned_page

ALL_ASK = BotSettings(levels={s: "ask" for s in ("page_body", "new_page", "delete_merge",
                                                 "structure", "teaching_params", "path")})
_n = iter(range(100_000))


def _build(pool, repo, ids, clock, scripts):
    ledger = ActionLedger(pool, clock, ids, {})

    def gateway(uid):
        return CountingGateway(scripts["s"].gateway())
    ex = register_assistant_executors(
        ledger, repo, clock, ids, request_write=lambda u, p: [], cancel_writes=lambda u, p: None,
        gateway=gateway, pool=pool)
    return ledger, ex


class Env:
    def __init__(self, pool, repo, ids, ledger, ex, scripts, clock_at):
        self.pool, self.repo, self.ids, self.ledger, self.ex = pool, repo, ids, ledger, ex
        self.scripts, self.at = scripts, clock_at


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    ids, repo, scripts = Ids(), PgRepo(pool), {"s": Script()}
    aspect, wiki = seed_events(ids)
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    with pool.connection() as conn:
        store.put_settings(conn, "u1", ALL_ASK, NOW)
    ledger, ex = _build(pool, repo, ids, lambda: NOW, scripts)
    return Env(pool, repo, ids, ledger, ex, scripts, NOW)


@pytest.fixture
def cards(pool):
    """An organized, learned page (s2 / s3 cue cards, s4 check card) at LATER."""
    make_user(pool, "u1")
    ids, repo, s = GoalIds(), PgRepo(pool), Script()
    scripts = {"s": s}
    pid = organized_learned_page((repo, s, ids))
    with pool.connection() as conn:
        store.put_settings(conn, "u1", ALL_ASK, LATER)
    ledger, ex = _build(pool, repo, ids, lambda: LATER, scripts)
    e = Env(pool, repo, ids, ledger, ex, scripts, LATER)
    e.pid = pid
    return e


def state(env):
    return env.repo.load("u1").state


def _ids_seen(kind, c, graph):
    if kind == "node_split":
        return [c["node_id"]]
    if kind == "node_relation":
        return [c["src"], c["dst"]]
    return [c["node_id"], c["parent"], *graph.parents(c["node_id"])]


def go(env, kind, content, *, draft=None, seen=True, **kw):
    """Proposes and approves. A graph action gets `graph_seen` (what Task 5's tools will put
    there) unless `seen=False`. An `auto` level (suggestion) stays `auto`: the row is recorded
    (`record_in`, status approved) and executed by `approve`, as the assistant's tools do."""
    if seen and "graph_seen" not in content and kind in (
            "node_split", "node_relation", "node_move"):
        g = state(env).graph
        content = {**content, "graph_seen": graph_fingerprint(g, _ids_seen(kind, content, g),
                                                              parts_only=kind == "node_split")}
    scope = GRAPH_SCOPES[kind][0]
    key, level = f"{kind}:{next(_n)}", env.ledger.level("u1", scope)
    if level == "auto":
        with env.pool.connection() as conn, conn.transaction():
            env.ledger.record_in(conn, "u1", kind=kind, scope=scope, source="assistant",
                                 summary=kind, why="", content=content, key=key,
                                 status="approved", page_id=kw.get("page_id"))
        row = env.ledger.get_by_key(key)
    else:
        extra = {"approved_now": True} if level == "auto_undo" else {}
        row = env.ledger.propose("u1", kind=kind, scope=scope, source="assistant", summary=kind,
                                 why="", content=content, key=key, **extra, **kw)
    return env.ledger.approve("u1", row.action_id, draft=draft)


# --- scopes, levels, can_undo -----------------------------------------------------------------

def test_every_kind_is_registered_with_the_scope_of_the_spec_table(env):
    # profile_fact's and profile_forget's executors need the profile store: service/deps registers them
    assert set(GRAPH_SCOPES) <= set(SCOPES) and set(SCOPES) - {PROFILE_FACT, FORGET_KIND, "memory_forget"} == set(env.ex)
    for kind in ("card_new", "card_fix", "review_postpone"):
        assert SCOPES[kind] == ("suggestion",)
    assert SCOPES["rule"] == ("teaching_params",) and SCOPES["settings"] == ("preference",)
    for kind in ("node_split", "node_relation", "node_known", "node_move"):
        assert SCOPES[kind] == ("structure",)
    assert action_level(env.ledger, "u1", "node_split") == ("ask", "structure")
    assert action_level(env.ledger, "u1", "card_new") == ("auto", "suggestion")
    assert action_level(env.ledger, "u1", "settings") == ("auto_undo", "preference")


# --- node_split ------------------------------------------------------------------------------

def test_split_asks_the_model_once_writes_children_and_undo_restores_the_graph(env):
    before = state(env).graph
    env.scripts["s"].push(SPLIT_PURPOSE, split_answer())
    out = go(env, "node_split", {"node_id": "n4"})
    assert out.status == "executed", out.detail
    assert env.scripts["s"].calls(SPLIT_PURPOSE) == 1 and out.result["kept"] == ""
    assert [p["title"] for p in ActionOut.of(out).content["pages"]] == ["一行乘一列", "维度要对上", "不满足交换律"]
    g = state(env).graph
    kids = out.result["children"]
    assert len(kids) == 3 and all(g.parents(k) == ("n4",) for k in kids)
    assert state(env).graph.node("n4") == before.node("n4")
    assert set(env.repo.load("u1").k.concept_ids()) >= set(kids)
    row = next(c for c in g.changes if c.kind == "split")
    assert row.why == "你要求把它拆细"                    # chat's default reason: requested
    assert can_undo(env.ledger.list("u1", status="executed")[0])
    undone = env.ledger.undo("u1", out.action_id)
    assert undone.status == "undone"
    after = state(env).graph
    assert [n.id for n in after.nodes] == [n.id for n in before.nodes]
    assert after.relations == before.relations


def test_split_with_a_reason_writes_it_and_old_reasons_are_refused(env):
    env.scripts["s"].push(SPLIT_PURPOSE, split_answer())
    out = go(env, "node_split", {"node_id": "n4", "reason": "confused"})
    assert out.status == "executed", out.detail
    assert env.scripts["s"].calls(SPLIT_PURPOSE) == 1
    row = next(c for c in state(env).graph.changes if c.kind == "split")
    assert row.why == "你点了看不懂"
    for old in ("user", "drill"):
        bad = go(env, "node_split", {"node_id": "n3", "reason": old})
        assert bad.status == "failed" and "拆分原因" in bad.detail
    assert env.scripts["s"].calls(SPLIT_PURPOSE) == 1


def test_split_left_whole_marks_the_node_atomic_and_undo_only_marks_the_row(env):
    before = state(env).graph
    env.scripts["s"].push(SPLIT_PURPOSE, keep("它已经是一件事"))
    out = go(env, "node_split", {"node_id": "n4", "words": "能再细一点吗"})
    assert out.status == "executed", out.detail
    assert out.result["kept"] == "它已经是一件事" and out.result["children"] == []
    assert ActionOut.of(out).detail == "它已经是一件事"
    assert out.result["change_id"] is None
    st = state(env)
    assert st.graph.nodes == before.nodes and st.graph.relations == before.relations
    assert atomic_of(st, "n4") is True
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    assert state(env).graph.nodes == before.nodes


def test_an_unusable_answer_twice_is_refused_and_writes_nothing(env):
    before = state(env).graph
    env.scripts["s"].push(SPLIT_PURPOSE, parts("只有一个"), parts("只有一个"))
    out = go(env, "node_split", {"node_id": "n4"})
    assert out.status == "failed" and "请切换模型重试" in out.detail
    assert env.scripts["s"].calls(SPLIT_PURPOSE) == 2
    assert state(env).graph.nodes == before.nodes


def test_split_is_stale_when_the_node_changed_and_refused_without_graph_seen(env):
    c = {"node_id": "n4",
         "graph_seen": graph_fingerprint(state(env).graph, ["n4"], parts_only=True)}
    env.scripts["s"].push(SPLIT_PURPOSE, split_answer())
    first = go(env, "node_split", {"node_id": "n4"})              # changes n4's edges
    assert first.status == "executed"
    stale = go(env, "node_split", c, seen=False)
    name = state(env).graph.node("n4").name
    assert stale.status == "failed" and stale.detail == f"「{name}」在提议之后改过了，这条修改已作废，图谱没有改动"
    assert "页面" not in stale.detail
    missing = go(env, "node_split", {"node_id": "n4"}, seen=False)
    assert missing.status == "failed" and "图谱" in missing.detail
    assert env.scripts["s"].calls(SPLIT_PURPOSE) == 1             # refused before any call


BLADE = {"key": "p1", "title": "刀片磨", "covers": "靠高速旋转的刀片打碎咖啡豆"}
BURR = {"key": "p2", "title": "刀盘磨", "covers": "靠两片刀盘挤压研磨咖啡豆"}


def _names(env, kids):
    return [state(env).graph.node(k).name for k in kids]


def test_named_parts_are_exactly_the_graphs_no_model_and_undo_restores(env):
    before = state(env).graph
    out = go(env, "node_split", {"node_id": "n4", "pages": [BLADE, BURR]})
    assert out.status == "executed", out.detail
    assert env.scripts["s"].calls(SPLIT_PURPOSE) == 0
    kids = out.result["children"]
    g = state(env).graph
    assert _names(env, kids) == ["刀片磨", "刀盘磨"] and g.children("n4") == tuple(kids)
    assert g.node(kids[0]).definition == BLADE["covers"]
    assert set(env.repo.load("u1").k.concept_ids()) >= set(kids)
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    after = state(env).graph
    assert after.nodes == before.nodes and after.relations == before.relations


def test_the_card_edit_is_what_lands(env):
    draft = {"pages": [{"key": "p2"}, {"title": "锥形磨", "isNew": True}]}       # 刀片磨 removed
    out = go(env, "node_split", {"node_id": "n4", "pages": [BLADE, BURR]}, draft=draft)
    assert out.status == "executed", out.detail
    assert _names(env, out.result["children"]) == ["刀盘磨", "锥形磨"]


@pytest.mark.parametrize("second", ["n4", "same"])
def test_a_named_part_that_is_the_node_or_a_repeat_is_refused_never_dropped(env, second):
    before = state(env).graph
    name = before.node("n4").name if second == "n4" else BLADE["title"]
    third = {"key": "p3", "title": "第三种", "covers": ""}
    out = go(env, "node_split", {"node_id": "n4", "pages": [
        BLADE, {"key": "p2", "title": name, "covers": ""}, third]})
    assert out.status == "failed" and name in out.detail and "拆不开" in out.detail
    assert state(env).graph.nodes == before.nodes


def test_a_named_part_of_an_existing_name_reuses_that_node(env):
    first = go(env, "node_split", {"node_id": "n4", "pages": [BLADE, BURR]})
    blade = first.result["children"][0]
    out = go(env, "node_split", {"node_id": "n3", "pages": [
        {"key": "p1", "title": BLADE["title"], "covers": ""},
        {"key": "p2", "title": "别的", "covers": ""}]})
    assert out.status == "executed", out.detail
    assert blade in out.result["children"] and set(state(env).graph.parents(blade)) == {"n4", "n3"}


def test_a_relation_landing_on_the_node_does_not_void_a_split(env):
    """Background work moved the graph, but not what a split depends on: the fingerprint holds."""
    g = state(env).graph
    seen = graph_fingerprint(g, ["n4"], parts_only=True)
    moved = g.model_copy(update={"relations": (
        *g.relations, Relation(kind="recommended", src="n1", dst="n4", source="split"))})
    assert graph_fingerprint(moved, ["n4"], parts_only=True) == seen
    assert graph_fingerprint(moved, ["n4"]) != graph_fingerprint(g, ["n4"])


def test_a_write_landing_during_the_commit_rebuilds_on_the_new_snapshot(env, monkeypatch):
    """K moved while the model thought (a page being written, an answer): the split still lands,
    with the model's one answer, and nobody is told a page changed."""
    env.scripts["s"].push(SPLIT_PURPOSE, split_answer())
    real, tries = env.repo.commit, []

    def commit(snap, c, **kw):
        if c.extra and not tries:
            tries.append(1)
            raise WikiConflict()
        return real(snap, c, **kw)
    monkeypatch.setattr(env.repo, "commit", commit)
    out = go(env, "node_split", {"node_id": "n4"})
    assert out.status == "executed", out.detail
    assert env.scripts["s"].calls(SPLIT_PURPOSE) == 1 and len(out.result["children"]) == 3


def test_a_write_that_keeps_landing_says_so_not_that_a_page_changed(env, monkeypatch):
    def commit(snap, c, **kw):
        raise WikiConflict()
    monkeypatch.setattr(env.repo, "commit", commit)
    out = go(env, "node_split", {"node_id": "n4", "pages": [BLADE, BURR]})
    assert out.status == "failed" and out.detail == MOVED and "页面" not in out.detail


def test_split_undo_is_refused_when_a_later_split_touched_a_child(env):
    env.scripts["s"].push(SPLIT_PURPOSE, split_answer())
    first = go(env, "node_split", {"node_id": "n4"})
    kid = first.result["children"][0]
    env.scripts["s"].push(SPLIT_PURPOSE, parts("点积是什么", "一行一列怎么配"))
    second = go(env, "node_split", {"node_id": kid})
    assert second.status == "executed", second.detail
    with pytest.raises(NotDecidable):
        env.ledger.undo("u1", first.action_id)


# --- node_relation / node_move ----------------------------------------------------------------

def _rel(g, kind, src, dst):
    return next((r for r in g.relations if r.key == (kind, src, dst)), None)


def test_remove_a_prerequisite_and_undo_restores_it(env):
    out = go(env, "node_relation", {"op": "remove", "src": "n1", "dst": "n2"})
    assert out.status == "executed", out.detail
    assert _rel(state(env).graph, "prerequisite", "n1", "n2") is None
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    assert _rel(state(env).graph, "prerequisite", "n1", "n2").phrase == "先有样本"


def test_an_added_prerequisite_is_the_users_unverified_soft_order(env):
    out = go(env, "node_relation", {"op": "add", "src": "n4", "dst": "n5", "phrase": "先懂 p 值"})
    assert out.status == "executed", out.detail
    r = _rel(state(env).graph, "prerequisite", "n4", "n5")
    assert (r.source, r.verified, r.phrase) == ("user", False, "先懂 p 值")
    assert "n4" not in env.repo.load("u1").k.prereqs("n5")       # unverified: not a K prereq
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    assert _rel(state(env).graph, "prerequisite", "n4", "n5") is None


def test_change_a_relation_and_refuse_a_cycle_or_a_stale_edit(env):
    out = go(env, "node_relation", {"op": "change", "src": "n1", "dst": "n2",
                                    "to": {"kind": "recommended", "phrase": "有帮助"}})
    assert out.status == "executed", out.detail
    g = state(env).graph
    assert _rel(g, "prerequisite", "n1", "n2") is None
    assert _rel(g, "recommended", "n1", "n2").source == "user"
    cyc = go(env, "node_relation", {"op": "add", "src": "n3", "dst": "n2"})   # n2 -> n3 exists
    assert cyc.status == "failed" and "cycle" in cyc.detail
    gone = go(env, "node_relation", {"op": "remove", "src": "n4", "dst": "n5"})
    assert gone.status == "failed"                            # no such relation: nothing written


def test_relation_undo_is_refused_when_the_relation_changed_since(env):
    out = go(env, "node_relation", {"op": "add", "src": "n4", "dst": "n5"})
    go(env, "node_relation", {"op": "change", "src": "n4", "dst": "n5",
                              "to": {"phrase": "后来改的"}})
    with pytest.raises(NotDecidable, match="之后又被改过"):
        env.ledger.undo("u1", out.action_id)


def test_move_a_node_under_another_parent_and_back(env):
    out = go(env, "node_move", {"node_id": "n4", "parent": "n5"})
    assert out.status == "executed", out.detail
    assert state(env).graph.parents("n4") == ("n5",)
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    assert state(env).graph.parents("n4") == ("r",)


def test_move_with_keep_old_adds_a_second_parent(env):
    out = go(env, "node_move", {"node_id": "n4", "parent": "n5", "keep_old": True})
    assert out.status == "executed"
    assert set(state(env).graph.parents("n4")) == {"r", "n5"}
    env.ledger.undo("u1", out.action_id)
    assert state(env).graph.parents("n4") == ("r",)
    cyc = go(env, "node_move", {"node_id": "r", "parent": "n1"})
    assert cyc.status == "failed" and "cycle" in cyc.detail


# --- node_known -------------------------------------------------------------------------------

def _confirm_item(**kw):
    return {"stem": "从总体里随机抽出的一部分叫什么？",
            "options": ["样本", "总体", "参数", "统计量"], "answer": 0, **kw}


def test_known_records_evidence_only_and_returns_one_confirm_item(env):
    # skill design A7 #8: K only; the stored path is not touched, the row shows 可以跳过 from K
    from evowiki.wikiapp.paths import path_items
    before = state(env).paths[PATH_ID]
    env.scripts["s"].push(CONFIRM_PURPOSE, _confirm_item())
    out = go(env, "node_known", {"node_id": "n1"})
    assert out.status == "executed", out.detail
    item = CheckItem.model_validate(out.result["item"])
    assert item.verified and item.options[item.answer] == "样本"
    assert env.scripts["s"].calls(CONFIRM_PURPOSE) == 1
    k = env.repo.load("u1").k
    assert all(k.belief(c, NOW).declared == "know" for c in out.result["cells"])
    assert state(env).paths[PATH_ID] == before                      # path data unchanged
    assert "paths" not in out.result
    snap = env.repo.load("u1")
    first = path_items(snap.state, snap.state.paths[PATH_ID], snap.k, NOW)[0]
    assert first.node_ids == ["n1"] and first.skippable and not first.skipped


def test_known_undo_retracts_the_evidence(env):
    env.scripts["s"].push(CONFIRM_PURPOSE, _confirm_item())
    out = go(env, "node_known", {"node_id": "n1"})
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    k = env.repo.load("u1").k
    assert all(k.belief(c, NOW).declared is None for c in out.result["cells"])
    assert not k.notes("n1")
    assert not shown(state(env).paths[PATH_ID])[0].skipped


def test_known_without_a_usable_model_records_nothing(env):
    env.scripts["s"].push(CONFIRM_PURPOSE, {"stem": "x", "options": ["a", "a", "b"], "answer": 0},
                          {"stem": "x", "options": ["a", "a", "b"], "answer": 0})
    out = go(env, "node_known", {"node_id": "n1"})
    assert out.status == "failed" and "请切换模型重试" in out.detail
    k = env.repo.load("u1").k
    assert all(k.belief(c, NOW).declared is None for c in ("n1.term", "n1.purpose"))
    assert not shown(state(env).paths[PATH_ID])[0].skipped


def test_known_confirm_right_keeps_it_and_answers_only_once(env):
    env.scripts["s"].push(CONFIRM_PURPOSE, _confirm_item())
    out = go(env, "node_known", {"node_id": "n1"})
    ex = env.ex["node_known"]
    res = ex.confirm("u1", out, out.result["item"]["answer"])
    assert res["correct"] and not res["undone"]
    assert env.ledger.get_by_key(out.idempotency_key).status == "executed"
    assert not shown(state(env).paths[PATH_ID])[0].skipped           # the path is never skipped
    from evowiki.bot.actions import ActionRefused
    with pytest.raises(ActionRefused, match="已经答过"):
        ex.confirm("u1", out, 0)


def test_known_confirm_wrong_takes_the_claim_back_with_a_message(env):
    env.scripts["s"].push(CONFIRM_PURPOSE, _confirm_item())
    out = go(env, "node_known", {"node_id": "n1"})
    item = out.result["item"]
    res = env.ex["node_known"].confirm("u1", out, (item["answer"] + 1) % len(item["options"]))
    assert not res["correct"] and res["undone"] and "撤回" in res["message"]
    row = env.ledger.get_by_key(out.idempotency_key)
    assert row.status == "undone"
    k = env.repo.load("u1").k
    assert all(k.belief(c, NOW).declared is None for c in out.result["cells"])
    assert not shown(state(env).paths[PATH_ID])[0].skipped
    # the wrong answer itself stays as evidence
    assert any(n.evidence_ref.startswith("known:") for n in k.notes("n1"))


# --- review cards -----------------------------------------------------------------------------

def _blank_cue(env, sid):
    snap = env.repo.load("u1")
    rec = snap.state.pages[env.pid]
    t = rec.page.texts[sid]
    page = rec.page.model_copy(update={"texts": {**rec.page.texts,
                                                 sid: t.model_copy(update={"cue": ""})}})
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.ids("wev"), ts=LATER,
                                                 body=PageSaved(record=rec.model_copy(
                                                     update={"page": page}), action="t"))]))


def _cue(env, sid):
    return state(env).pages[env.pid].page.texts[sid].cue


def test_card_new_adds_cues_and_undo_removes_them(cards):
    _blank_cue(cards, "s3")
    out = go(cards, "card_new", {"page_id": cards.pid,
                                 "cards": [{"section_id": "s3", "cue": "标准差的单位是什么", "answer": "一句答案"}]},
             page_id=cards.pid, page_version=None)
    assert out.status == "executed", out.detail
    assert _cue(cards, "s3") == "标准差的单位是什么"
    assert_partial_write(cards.repo, cards.pid, "card", {"s3"}, producer="edit")
    assert out.result["cards"] == [card_id(cards.pid, "s3", "cue")]
    assert any(i.section_id == "s3" for i in sources(state(cards)))
    assert cards.ledger.undo("u1", out.action_id).status == "undone"
    assert _cue(cards, "s3") == ""


def test_card_new_writes_a_card_over_a_cue_with_no_answer(cards):
    _blank_cue(cards, "s3")
    rec = state(cards).pages[cards.pid]
    page = rec.page.model_copy(update={"texts": {**rec.page.texts, "s3": rec.page.texts["s3"].model_copy(
        update={"cue": "只是折叠标题", "answer": ""})}})
    snap = cards.repo.load("u1")
    cards.repo.commit(snap, Commit(wiki=[WikiEvent(event_id="wev_title", ts=NOW, body=PageSaved(
        record=rec.model_copy(update={"page": page}), action="t"))]))
    out = go(cards, "card_new", {"page_id": cards.pid,
                                 "cards": [{"section_id": "s3", "cue": "标准差的单位是什么", "answer": "一句答案"}]},
             page_id=cards.pid, page_version=None)
    assert out.status == "executed", out.detail
    assert any(i.section_id == "s3" and i.answer == "一句答案" for i in sources(state(cards)))


def test_card_new_refuses_an_existing_or_summary_cue(cards):
    again = go(cards, "card_new", {"page_id": cards.pid,
                                   "cards": [{"section_id": "s2", "cue": "新的提示", "answer": "一句答案"}]})
    assert again.status == "failed" and "已经有回想卡" in again.detail
    _blank_cue(cards, "s3")
    summary = go(cards, "card_new", {"page_id": cards.pid,
                                     "cards": [{"section_id": "s3", "cue": "这一段讲的是什么", "answer": "一句答案"}]})
    assert summary.status == "failed" and "具体的点" in summary.detail
    assert _cue(cards, "s3") == ""


def test_card_drop_hides_the_card_and_undo_shows_it_again(cards):
    cid = card_id(cards.pid, "s2", "cue")
    out = go(cards, "card_fix", {"op": "drop", "card_id": cid})
    assert out.status == "executed", out.detail
    assert cid in state(cards).hidden_cards
    assert cid not in [i.card_id for i in review_today(cards.repo, "u1", now=LATER).items]
    assert cards.ledger.undo("u1", out.action_id).status == "undone"
    assert cid not in state(cards).hidden_cards
    assert cid in [i.card_id for i in review_today(cards.repo, "u1", now=LATER).items]


def test_card_fix_puts_the_rows_cue_with_no_call_and_undo_puts_it_back(cards):
    cid, old = card_id(cards.pid, "s2", "cue"), _cue(cards, "s2")
    out = go(cards, "card_fix", {"op": "fix", "card_id": cid, "opinion": "看不懂在问什么",
                                 "cue": "z 告诉你离均值几个标准差", "answer": "一句答案"})
    assert out.status == "executed", out.detail
    assert _cue(cards, "s2") == "z 告诉你离均值几个标准差"
    assert_partial_write(cards.repo, cards.pid, "card", {"s2"}, producer="edit")
    assert cards.ledger.undo("u1", out.action_id).status == "undone"
    assert _cue(cards, "s2") == old


def test_card_fix_check_card_resets_the_answer_and_undo_restores_both(cards):
    cid = card_id(cards.pid, "s4", "check")
    rec = state(cards).pages[cards.pid]
    old_item = next(s.check for s in rec.page.outline.sections if s.id == "s4")
    new_item = {"cell": old_item.cell, "stem": "z = 2 表示这个值在哪里？", "verified": True,
                "options": ["高于均值 2 个标准差", "高于均值 2 分", "占数据量的 2%"], "answer": 0}
    out = go(cards, "card_fix", {"op": "fix", "card_id": cid, "opinion": "题目没说清楚",
                                 "check": new_item})
    assert out.status == "executed", out.detail
    new = state(cards).pages[cards.pid]
    assert next(s.check for s in new.page.outline.sections if s.id == "s4").stem.startswith("z = 2")
    assert "s4" not in new.checks
    assert cards.ledger.undo("u1", out.action_id).status == "undone"
    back = state(cards).pages[cards.pid]
    assert next(s.check for s in back.page.outline.sections if s.id == "s4") == old_item


def test_card_fix_without_the_new_text_changes_nothing(cards):
    cid, old = card_id(cards.pid, "s2", "cue"), _cue(cards, "s2")
    out = go(cards, "card_fix", {"op": "fix", "card_id": cid, "opinion": "看不懂"})
    assert out.status == "failed" and _cue(cards, "s2") == old
    check = card_id(cards.pid, "s4", "check")
    other = {"cell": "zz.term", "stem": "x？", "options": ["a", "b"], "answer": 0}
    out = go(cards, "card_fix", {"op": "fix", "card_id": check, "opinion": "x", "check": other})
    assert out.status == "failed"


# --- review_postpone --------------------------------------------------------------------------

def _due(env):
    s = env.repo.load("u1")
    return schedule(s.k, LATER, sources(s.state), goal_concepts(s.state), s.state.review,
                    postponed=s.state.review_postponed)


def test_postpone_moves_the_due_dates_only_and_undo_restores_them(cards):
    k0 = cards.repo.load("u1").k
    mem0 = {c: k0.memory(c) for c in ("c1.procedure", "c1.interpret")}
    assert _due(cards).due >= 1
    out = go(cards, "review_postpone", {"scope": "all", "days": 3})
    assert out.status == "executed", out.detail
    assert _due(cards).due == 0 and not review_today(cards.repo, "u1", now=LATER).items
    k1 = cards.repo.load("u1").k
    assert {c: k1.memory(c) for c in mem0} == mem0          # the forgetting model is untouched
    assert cards.ledger.undo("u1", out.action_id).status == "undone"
    assert _due(cards).due >= 1 and state(cards).review_postponed == {}
    # after the days are over the reviews come back
    again = go(cards, "review_postpone", {"scope": "all", "days": 3})
    assert again.status == "executed"
    later = schedule(k1, LATER + timedelta(days=4), sources(state(cards)), frozenset(),
                     state(cards).review, postponed=state(cards).review_postponed)
    assert later.due >= 1


def test_postpone_refuses_bad_days_nothing_due_and_a_moved_postponement(cards):
    assert go(cards, "review_postpone", {"scope": "all", "days": 0}).status == "failed"
    out = go(cards, "review_postpone", {"scope": "all", "days": 2})
    assert go(cards, "review_postpone", {"scope": "all", "days": 2}).status == "failed"   # none due
    go(cards, "review_postpone", {"scope": "cells", "cells": ["c1.procedure"], "days": 9})
    with pytest.raises(NotDecidable, match="又推迟过"):
        cards.ledger.undo("u1", out.action_id)


# --- settings ---------------------------------------------------------------------------------

def _settings(env):
    with env.pool.connection() as conn:
        return store.get_settings(conn, "u1")


def test_settings_change_and_undo_restore_the_old_values(env):
    out = go(env, "settings", {"changes": {"today_cap": 2, "kinds": {"goal_idle": False}}})
    assert out.status == "executed", out.detail
    s = _settings(env)
    assert s.today_cap == 2 and s.kinds["goal_idle"] is False and s.kinds["review_due"] is True
    assert out.result["old"]["today_cap"] == 5
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    s = _settings(env)
    assert s.today_cap == 5 and s.kinds["goal_idle"] is True and s.levels == ALL_ASK.levels


def test_settings_refuse_levels_unknown_fields_bad_values_and_a_changed_field(env):
    for changes in ({"levels": {"structure": "auto_undo"}}, {"levels": {"nonsense": "ask"}},
                    {"nonsense": 1}, {"today_cap": 99}, {"today_cap": 5}):
        out = go(env, "settings", {"changes": changes})
        assert out.status == "failed", changes
    assert _settings(env).levels == ALL_ASK.levels
    out = go(env, "settings", {"changes": {"page_cap": 4}})
    with env.pool.connection() as conn:
        store.put_settings(conn, "u1", _settings(env).model_copy(update={"page_cap": 6}), NOW)
    with pytest.raises(NotDecidable, match="又改过"):
        env.ledger.undo("u1", out.action_id)


def test_settings_levels_may_only_get_stricter(env):
    with env.pool.connection() as conn:
        store.put_settings(conn, "u1", BotSettings(levels={"new_page": "auto_undo",
                                                            "structure": "ask"}), NOW)
    out = go(env, "settings", {"changes": {"levels": {"new_page": "ask"}}})   # 新建页面先问我
    assert out.status == "executed", out.detail
    assert _settings(env).levels["new_page"] == "ask"
    loose = go(env, "settings", {"changes": {"levels": {"structure": "auto_undo"}}})
    assert loose.status == "failed" and "放宽" in loose.detail
    assert _settings(env).levels["structure"] == "ask"
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    assert _settings(env).levels["new_page"] == "auto_undo"


def test_unknown_ids_are_reasons_not_staleness(env):
    for kind, c in (("node_split", {"node_id": "nope"}),
                    ("node_move", {"node_id": "n4", "parent": "nope"}),
                    ("node_relation", {"op": "remove", "src": "n4", "dst": "nope"})):
        out = go(env, kind, {**c, "graph_seen": "x"}, seen=False)
        assert out.status == "failed" and out.detail != STALE_DETAIL and "没有" in out.detail
    # a node with no cells in K: the real reason
    snap = env.repo.load("u1")
    from evowiki.nodes import NodeRec
    from evowiki.wikiapp.events import GraphSaved
    g = snap.state.graph.updated(nodes=[*snap.state.graph.nodes, NodeRec(id="lone", name="孤点")])
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.ids("wev"), ts=NOW,
                                                 body=GraphSaved(graph=g, action="user_edit"))]))
    env.scripts["s"].push(CONFIRM_PURPOSE, _confirm_item())
    out = go(env, "node_known", {"node_id": "lone"})
    assert out.status == "failed" and out.detail != STALE_DETAIL


CHECK = {"cell": "c2.term", "stem": "z = -2 表示这个值在哪里？", "verified": True,
         "options": ["低于均值 2 个标准差", "低于均值 2 分", "占数据量的 2%"], "answer": 0}


def test_card_new_puts_the_rows_check_card_and_undo_removes_it(cards):
    out = go(cards, "card_new", {"page_id": cards.pid,
                                 "cards": [{"section_id": "s3", "check": CHECK}]})
    assert out.status == "executed", out.detail
    assert out.level == "auto"
    page = state(cards).pages[cards.pid].page
    sec = next(s for s in page.outline.sections if s.id == "s3-q")
    assert sec.check.verified and sec.check.cell == "c2.term"
    ids = [s.id for s in page.outline.sections]
    assert ids.index("s3-q") == ids.index("s3") + 1
    assert out.result["cards"] == [card_id(cards.pid, "s3-q", "check")]
    assert card_id(cards.pid, "s3-q", "check") in [
        card_id(x.page_id, x.section_id, "check") for x in sources(state(cards)) if x.check]
    rec = state(cards).pages[cards.pid]
    changed = rec.page.model_copy(update={"outline": rec.page.outline.model_copy(update={
        "sections": tuple(
            s.model_copy(update={"check": s.check.model_copy(update={"stem": "改过的题干"})})
            if s.id == "s3-q" else s for s in rec.page.outline.sections)})})
    snap = cards.repo.load("u1")
    cards.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=cards.ids("wev"), ts=LATER,
                                                   body=PageSaved(record=rec.model_copy(
                                                       update={"page": changed}), action="t"))]))
    with pytest.raises(NotDecidable, match="之后被改过"):
        cards.ledger.undo("u1", out.action_id)
    cards.repo.commit(cards.repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=cards.ids("wev"), ts=LATER,
        body=PageSaved(record=state(cards).pages[cards.pid].model_copy(update={"page": rec.page}),
                       action="t"))]))
    assert cards.ledger.undo("u1", out.action_id).status == "undone"      # at level auto
    assert all(s.id != "s3-q" for s in state(cards).pages[cards.pid].page.outline.sections)
    dup = go(cards, "card_new", {"page_id": cards.pid,
                                 "cards": [{"section_id": "s4", "check": CHECK}]})
    assert dup.status == "failed"                                          # a check section
