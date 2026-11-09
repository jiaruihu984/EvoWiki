"""T15: the assistant's `mark_source_use` on real Postgres - a material's use recorded through the
ledger (`source_use`, level auto, no confirm card) and `sources.uses.apply_use`, which starts the
full import of a chat attachment. Fake source runs; nothing here calls a paid API."""

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.assistant.tools.actions import ActionDeps, build_action_tools
from evowiki.bot.actions import ActionLedger
from evowiki.bot.executors_assistant import SourceUseExecutor
from evowiki.sources import store, uses
from evowiki.sources.reader import PooledSourceReader
from evowiki.sources.uses import MSG_READ_FAILED
from evowiki.wikiapp.repo import PgRepo
from tests.assistant.fakes import ungrounded
from tests.runtime.helpers import make_user
from tests.sources.test_uses import NOW, Runs, add, recorded
from tests.wikiapp.test_path_edits import Ids

CTX = ToolContext(user_id="u1", session_id="s1")


class Env:
    def __init__(self, pool, runs):
        self.pool, self.runs = pool, runs
        self.ledger = ActionLedger(pool, lambda: NOW, Ids(), {})
        self.ledger.register("source_use", SourceUseExecutor(pool, lambda: self.runs, lambda: NOW))
        deps = ActionDeps(repo=PgRepo(pool), ledger=self.ledger, clock=lambda: NOW, new_id=Ids(),
                          sources=PooledSourceReader(pool, None))
        self.tool = next(t for t in ungrounded(build_action_tools(deps)) if t.spec.name == "mark_source_use")

    def run(self, **args):
        return self.tool.run(args, CTX)


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    add(pool, "att", "2023 年期末考试", ("一、选择题",))           # a chat attachment (ask only)
    add(pool, "theirs", "别人的讲义", ("第一章",), user="u2")
    return Env(pool, Runs())


def test_exam_is_recorded_at_once_and_starts_the_full_import(env):
    res = env.run(source_id="att", use="exam")
    assert res.error is None, res.content
    row = env.ledger.get("u1", res.action.action_id)
    assert (row.kind, row.scope, row.level, row.status) == ("source_use", "suggestion", "auto",
                                                            "executed")
    assert row.content == {"source_id": "att", "use": "exam", "session_id": "s1"}
    assert not res.action.confirm and res.action.can_undo
    assert res.action.title == "把《2023 年期末考试》记为往年题"
    assert "已执行" in res.content and "可以在卡片上撤销" in res.content
    assert recorded(env.pool, "att") == ["ask", "exam"]
    with env.pool.connection() as conn:
        use = next(u for u in store.uses_of(conn, "u1", "att") if u["use"] == "exam")
    assert use["session_id"] == "s1"
    assert [(j.source_id, j.mode) for j in env.runs.started] == [("att", "full")]


def test_a_foreign_or_unknown_source_is_refused_and_nothing_is_written(env):
    for sid in ("theirs", "nope"):
        res = env.run(source_id=sid, use="learn")
        assert res.error is not None and res.action is None
        assert f"没有这份资料：{sid}" in res.content
    assert env.ledger.list("u1") == [] and recorded(env.pool, "theirs", user="u2") == ["ask"]
    assert env.runs.started == []


def test_a_goal_use_is_not_this_tools(env):
    res = env.run(source_id="att", use="goal")
    assert res.error is not None and env.ledger.list("u1") == []


def test_read_failed_is_the_chinese_reason_and_no_success(env, monkeypatch):
    def failed(*a, **kw):
        raise uses.ReadFailed("source att could not be read")
    monkeypatch.setattr(uses, "apply_use", failed)
    res = env.run(source_id="att", use="learn")
    assert res.error is not None and res.action is None
    assert MSG_READ_FAILED in res.content and "已执行" not in res.content
    assert env.ledger.list("u1")[0].status == "failed"
    assert recorded(env.pool, "att") == ["ask"]


def test_a_run_that_cannot_start_keeps_the_use_and_says_so(env):
    env.runs = Runs(fail=True)
    res = env.run(source_id="att", use="learn")
    assert res.error is None
    assert "资料没能开始整理" in res.content and "照实" in res.content and "重试" not in res.content
    assert env.ledger.get("u1", res.action.action_id).status == "executed"
    assert recorded(env.pool, "att") == ["ask", "learn"]


def test_the_use_and_the_ledger_row_commit_together(env, monkeypatch):
    """A failure inside the ledger's effect rolls the use back too (one transaction)."""
    real = uses.apply_use

    def broken_effect(*a, effect, **kw):
        def boom(conn, use_id):
            effect(conn, use_id)
            raise RuntimeError("crash after the effect")
        return real(*a, effect=boom, **kw)
    monkeypatch.setattr(uses, "apply_use", broken_effect)
    res = env.run(source_id="att", use="exam")
    assert res.error is not None and res.action is None
    assert recorded(env.pool, "att") == ["ask"]
    assert [r.status for r in env.ledger.list("u1")] == ["approved"]


def test_undo_deletes_that_use_and_the_import_stays(env):
    res = env.run(source_id="att", use="exam")
    row = env.ledger.get("u1", res.action.action_id)
    assert row.undo and set(row.undo) == {"use_id"}
    done = env.ledger.undo("u1", row.action_id)
    assert done.status == "undone"
    assert recorded(env.pool, "att") == ["ask"]                      # only that use is gone
    assert [(j.source_id, j.mode) for j in env.runs.started] == [("att", "full")]  # not cancelled
    with env.pool.connection() as conn:
        assert store.get_source(conn, "u1", "att").run_gen == 1   # the full import's run stays


def test_undo_after_the_use_is_gone_is_refused_with_the_reason(env):
    from evowiki.bot.actions import NotDecidable
    res = env.run(source_id="att", use="learn")
    row = env.ledger.get("u1", res.action.action_id)
    with env.pool.connection() as conn, conn.transaction():
        store.delete_use(conn, "u1", row.undo["use_id"])
    with pytest.raises(NotDecidable, match="这个用处已经不在了"):
        env.ledger.undo("u1", row.action_id)
    assert env.ledger.get("u1", row.action_id).status == "executed"
