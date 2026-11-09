"""The Bot's learning-path edits (B5b): the weekly review moves / inserts a page through the
action ledger (scope `path`), exactly once, version-checked, undoable. Real store, no LLM."""

from datetime import timedelta

import pytest

from evowiki.bot.actions import ActionLedger, NotDecidable
from evowiki.bot.diffs import Names, diff_for
from evowiki.bot.path_actions import STALE, UNDO_STALE, PathEditExecutor, path_version
from evowiki.bot.pipeline import dispatch, execute_path_edit, run_daily, stale_path_edits
from evowiki.bot.settings import DEFAULT_SETTINGS
from evowiki.goals.path_edit import MoveEdit
from evowiki.store import bot as store
from evowiki.wikiapp.path_edits import bot_edited, save_path
from evowiki.wikiapp.path_order import shown
from evowiki.wikiapp.repo import Commit, WikiConflict
from tests.bot.test_goal_week import NOW, make_repo, regraph, seed, wrong
from tests.bot.test_pipeline_weekly import MONDAY, slot

_n = iter(range(100_000))


def new_id(prefix):
    return f"{prefix}_pa{next(_n)}"


class Clock:
    def __call__(self):
        return NOW


@pytest.fixture
def repo(pool):
    return make_repo(pool)


@pytest.fixture
def ledger(pool, repo):
    led = ActionLedger(pool, Clock(), new_id, {})
    led.register("path_edit", PathEditExecutor(repo, Clock(), new_id))
    return led


def weak_c0(repo):
    repo.commit(repo.load("u1"), Commit(aspect=[
        wrong("c0.interpret", "review:pg_x:s1", NOW - timedelta(days=1))]))


def review(pool, ledger, level="auto_undo"):
    if level is not None:
        with pool.connection() as conn:
            store.put_settings(conn, "u1", DEFAULT_SETTINGS.model_copy(
                update={"levels": {**DEFAULT_SETTINGS.levels, "path": level}}), NOW)
    run_daily(pool, "u1", slot(MONDAY), now=NOW, new_id=new_id)
    with pool.connection() as conn:
        [t] = [t for t in store.triggers_since(conn, "u1", NOW - timedelta(days=1))
               if t.kind == "goal_review"]
    d = dispatch(pool, t.trigger_id, now=NOW, new_id=new_id, ledger=ledger)
    with pool.connection() as conn:
        [card] = store.suggestions_of_trigger(conn, t.trigger_id)
        acts = store.actions_of_trigger(conn, t.trigger_id)
    return d, card, acts


def order(repo):
    return [i.key for i in shown(repo.load("u1").state.paths["path-k"])]


BEFORE = ["path-k-p1", "path-k-p2", "path-k-p3", "path-k-p4"]
AFTER = ["path-k-p1", "path-k-p4", "path-k-p2", "path-k-p3"]


def test_auto_undo_moves_the_prerequisite_page_first_and_undo_restores(pool, repo, ledger):
    seed(repo)
    weak_c0(repo)
    _, card, [act] = review(pool, ledger)
    assert (act.kind, act.scope, act.level, act.status, act.source) == (
        "path_edit", "path", "auto_undo", "executed", "bot")
    assert order(repo) == AFTER
    rec = repo.load("u1").state.paths["path-k"]
    assert rec.edits[-1] == MoveEdit(page_key="path-k-p4", to_index=1, by="bot")
    assert card.why.endswith("；Bot 将把「第4页 c0」挪到前面，可在个性化里撤销")
    assert diff_for(act, Names(None)) == [
        {"kind": "del", "text": "之前：1. 页 c1　2. 第2页 c2　3. 第3页 c3　4. 第4页 c0"},
        {"kind": "add", "text": "之后：1. 页 c1　2. 第4页 c0　3. 第2页 c2　4. 第3页 c3"}]
    undone = ledger.undo("u1", act.action_id)
    assert undone.status == "undone" and order(repo) == BEFORE
    with pytest.raises(NotDecidable):
        ledger.undo("u1", act.action_id)


def test_undo_is_refused_once_the_path_changed_again(pool, repo, ledger):
    seed(repo)
    weak_c0(repo)
    _, _, [act] = review(pool, ledger)
    snap = repo.load("u1")
    rec = snap.state.paths["path-k"]
    save_path(repo, snap, rec.model_copy(update={"edits": (
        *rec.edits, MoveEdit(page_key="path-k-p3", to_index=0))}), now=NOW, new_id=new_id)
    with pytest.raises(NotDecidable, match=UNDO_STALE):
        ledger.undo("u1", act.action_id)


def test_ask_waits_with_its_diff_and_approving_twice_edits_once(pool, repo, ledger):
    seed(repo)
    weak_c0(repo)
    _, card, [act] = review(pool, ledger, level="ask")
    assert (act.status, act.level) == ("proposed", "ask") and order(repo) == BEFORE
    assert card.why.endswith("；把「第4页 c0」挪到前面，待你确认")
    assert ledger.approve("u1", act.action_id).status == "executed"
    assert ledger.approve("u1", act.action_id).status == "executed"
    rec = repo.load("u1").state.paths["path-k"]
    assert order(repo) == AFTER and sum(e.by == "bot" for e in rec.edits) == 1


def test_a_path_the_user_changed_before_approval_is_not_overwritten(pool, repo, ledger):
    seed(repo)
    weak_c0(repo)
    _, _, [act] = review(pool, ledger, level="ask")
    snap = repo.load("u1")
    rec = snap.state.paths["path-k"]
    mine = rec.model_copy(update={"edits": (MoveEdit(page_key="path-k-p3", to_index=1),)})
    save_path(repo, snap, mine, now=NOW, new_id=new_id)
    out = ledger.approve("u1", act.action_id)
    assert (out.status, out.detail) == ("failed", STALE)
    assert order(repo) == ["path-k-p1", "path-k-p3", "path-k-p2", "path-k-p4"]


def test_a_prerequisite_no_page_teaches_gets_a_page_before_the_next(pool, repo, ledger):
    seed(repo, nodes=("c1", "c2", "c3"))
    weak_c0(repo)
    _, card, [act] = review(pool, ledger)
    keys = order(repo)
    assert act.status == "executed" and len(keys) == 4 and keys[0] == "path-k-p1"
    added = repo.load("u1").state.paths["path-k"]
    new = next(i for i in shown(added) if i.key == keys[1])
    assert new.node_ids == ("c0",) and not new.mine and new.page is not None
    assert keys[2] == "path-k-p2"
    assert card.why.endswith("；Bot 将在前面加一页「概念0」，可在个性化里撤销")


def test_without_a_ledger_only_the_card(pool, repo):
    seed(repo)
    weak_c0(repo)
    _, card, acts = review(pool, None)
    assert acts == [] and order(repo) == BEFORE and card.title == "先补「概念0」再往下"


def test_the_bot_edit_and_its_undo_reach_the_path_hooks_and_go_through_the_router(pool, repo):
    """Merge of B5 with assistant-actions: the Bot's edit stores through `save_paths` (the one
    path-edit storage point), so `on_path_changed` sees it with source "bot"; the ledger's
    `path_edit` kind is a router that sends move / add rows to the Bot's executor."""
    from evowiki.bot.executors_assistant import PathEditRouter
    from evowiki.wikiapp.path_edits import off_path_changed, on_path_changed

    seen = []

    def hook(user_id, path_id, before, after, source):
        seen.append((user_id, path_id, source, before != after))

    led = ActionLedger(pool, Clock(), new_id, {})
    led.register("path_edit", PathEditRouter(assistant=None,
                                             bot=PathEditExecutor(repo, Clock(), new_id)))
    seed(repo)
    weak_c0(repo)
    on_path_changed(hook)
    try:
        _, _, [act] = review(pool, led)
        assert act.status == "executed" and order(repo) == AFTER
        led.undo("u1", act.action_id)
    finally:
        off_path_changed(hook)
    assert order(repo) == BEFORE
    assert seen == [("u1", "path-k", "bot", True), ("u1", "path-k", "bot", True)]
def test_bot_edited_refuses_a_move_that_is_not_forward(repo):
    """A page already before the one that needs it (or that very page) is never 'moved'."""
    seed(repo)
    snap = repo.load("u1")
    rec = snap.state.paths["path-k"]
    for key, before in (("path-k-p2", "path-k-p2"), ("path-k-p2", "path-k-p3")):
        with pytest.raises(WikiConflict):
            bot_edited(snap.state, rec, {"op": "move", "page_key": key, "before_key": before})


def test_the_teacher_being_the_next_page_makes_no_card_and_no_edit(pool, repo, ledger):
    seed(repo, nodes=("c1", "c0", "c2"))
    weak_c0(repo)
    run_daily(pool, "u1", slot(MONDAY), now=NOW, new_id=new_id)
    with pool.connection() as conn:
        [t] = [t for t in store.triggers_since(conn, "u1", NOW - timedelta(days=1))
               if t.kind == "goal_review"]
    assert t.payload["situation"] == "normal"
    assert order(repo) == ["path-k-p1", "path-k-p2", "path-k-p3"]


def test_an_edit_left_approved_is_executed_by_a_re_dispatch_or_the_sweep(pool, repo, ledger,
                                                                         monkeypatch):
    """Review fix 3: a raising approve (or a crash after the card's commit) leaves the row
    `approved`; re-dispatching the trigger, or the sweep's stale scan, executes it once."""
    seed(repo)
    weak_c0(repo)
    real = ledger.approve
    monkeypatch.setattr(ledger, "approve", lambda *a: (_ for _ in ()).throw(RuntimeError("db")))
    d, _, [act] = review(pool, ledger)
    assert act.status == "approved" and order(repo) == BEFORE
    assert [a.action_id for a in stale_path_edits(pool, now=NOW + timedelta(minutes=3))] == [
        act.action_id]
    monkeypatch.setattr(ledger, "approve", real)
    again = dispatch(pool, d.trigger_id, now=NOW, new_id=new_id, ledger=ledger)
    assert again.outcome is None and order(repo) == AFTER
    with pool.connection() as conn:
        assert store.get_action(conn, act.action_id).status == "executed"
    assert stale_path_edits(pool, now=NOW + timedelta(minutes=3)) == []


def test_the_sweep_executes_a_stale_approved_edit(pool, repo, ledger, monkeypatch):
    seed(repo)
    weak_c0(repo)
    real = ledger.approve
    monkeypatch.setattr(ledger, "approve", lambda *a: (_ for _ in ()).throw(RuntimeError("db")))
    review(pool, ledger)
    monkeypatch.setattr(ledger, "approve", real)
    for act in stale_path_edits(pool, now=NOW + timedelta(minutes=3)):
        execute_path_edit(pool, ledger, act)
    assert order(repo) == AFTER


MOVE_P4 = {"path_id": "path-k", "op": "move", "page_key": "path-k-p4", "before_key": "path-k-p2",
           "node_id": "c0", "title": "概念0"}


def test_bot_edited_refuses_moving_a_started_page(repo):
    seed(repo)
    snap = repo.load("u1")
    rec = snap.state.paths["path-k"]
    started = rec.model_copy(update={"page_ids": ("pg_1", None, None, "pg_4")})
    with pytest.raises(WikiConflict, match="已经开始学了"):
        bot_edited(snap.state, started, MOVE_P4)


def test_execution_refuses_a_move_past_a_page_it_depends_on(pool, repo, ledger):
    """Review: re-checked when the edit runs — p4 (c0) needs c3 (p3), so it may not jump p3."""
    regraph(repo, ("c0", "c2"), ("c3", "c0"))
    seed(repo)
    snap = repo.load("u1")
    rec = snap.state.paths["path-k"]
    with pytest.raises(WikiConflict, match="排到它要用到的「第3页 c3」前面"):
        bot_edited(snap.state, rec, MOVE_P4)
    act = ledger.propose("u1", kind="path_edit", scope="path", source="bot", summary="挪", why="w",
                         content=MOVE_P4, key="path:x", page_version=path_version(rec),
                         level="ask")
    out = ledger.approve("u1", act.action_id)
    assert out.status == "failed" and "前面，Bot 不挪" in out.detail
    assert order(repo) == BEFORE


def test_a_refused_edit_says_so_on_its_card_once(pool, repo, ledger):
    """Review: a sweep and a re-dispatch racing on the same refused edit append the reason once."""
    seed(repo)
    weak_c0(repo)
    _, card, [act] = review(pool, ledger, level="ask")
    snap = repo.load("u1")
    rec = snap.state.paths["path-k"]
    save_path(repo, snap, rec.model_copy(update={"edits": (MoveEdit(page_key="path-k-p3",
                                                                     to_index=1),)}),
              now=NOW, new_id=new_id)
    with pool.connection() as conn:
        store.set_action_status(conn, act.action_id, status="approved", at=NOW)
        act = store.get_action(conn, act.action_id)
    execute_path_edit(pool, ledger, act)

    class Racer:                       # the other caller: its approve also answers `failed`
        def approve(self, user_id, action_id):
            with pool.connection() as conn:
                return store.get_action(conn, action_id)
    execute_path_edit(pool, Racer(), act)
    with pool.connection() as conn:
        [card] = store.suggestions_of_trigger(conn, act.trigger_id)
    assert card.why.count("没能调整学习路径") == 1
