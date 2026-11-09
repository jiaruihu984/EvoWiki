"""Harness changes in the change list and their undo (task 10). Real Postgres, in-memory spec registry."""

from datetime import UTC, datetime, timedelta

import pytest

from evowiki.content.models import Lang
from evowiki.harness.change_source import HarnessChanges, undo_change
from evowiki.harness.diff import KeyOp, PackDiff, diff_to_json
from evowiki.store import bot as bot_store
from evowiki.store import harness as store
from evowiki.store import harness_rsi as hr
from evowiki.store.users import UserRow, create_user
from evowiki.teach.contracts import PolicyVersion, SpecItem, SpecPatchOp, TeachingSpec

T0 = datetime(2026, 10, 21, 1, 0, tzinfo=UTC)
H4 = "H4.deep.max_tool_calls"
H1 = "H1.write"


@pytest.mark.parametrize("status", ["undone", "rolled_back"])
def test_native_undo_updates_existing_action_mirror(env, status):
    pool, rid = env
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1)
        bot_store.mirror_action(
            conn, user_id="u1", kind="rsi_method", scope="rsi", source="rsi",
            key=f"harness:u1:{cid}", status="executed", summary="s", why="w",
            content={"change_id": "h_" + cid, "kind": "H"},
            created_at=T0, decided_at=T0)
    with pool.connection() as conn:
        undo_change(conn, "u1", cid, registry=None, status=status,
                    now=T0 + timedelta(minutes=3))
    with pool.connection() as conn:
        assert conn.execute("select status from agent_action").fetchone()["status"] == "undone"


@pytest.mark.parametrize("read", ["list", "get", "changes"])
def test_historical_mirror_is_reconciled_without_another_undo(env, read):
    pool, rid = env
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1)
        undo_change(conn, "u1", cid, registry=None, now=T0 + timedelta(minutes=3))
        assert conn.execute("select count(*) as n from agent_action").fetchone()["n"] == 0
        bot_store.mirror_action(
            conn, user_id="u1", kind="rsi_method", scope="rsi", source="rsi",
            key=f"harness:u1:{cid}", status="executed", summary="s", why="w",
            content={"change_id": "h_" + cid, "kind": "H"},
            created_at=T0, decided_at=T0)
    with pool.connection() as conn:
        if read == "get":
            row = bot_store.get_action_by_key(conn, f"harness:u1:{cid}")
        elif read == "changes":
            (row,) = bot_store.list_change_rows(conn, "u1", sources=["rsi"],
                                                statuses=["undone"], limit=10)
        else:
            (row,) = bot_store.list_actions(conn, "u1", status="undone")
        assert row.status == "undone" and row.decided_at == T0 + timedelta(minutes=3)
        assert store.load_pack(conn, "u1").values[H4] == 80


def test_mirror_failure_rolls_back_native_undo(env, monkeypatch):
    pool, rid = env
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1)

    def fail(*args):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(bot_store, "sync_rsi_undone", fail)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        HarnessChanges(pool).undo("u1", cid, now=T0 + timedelta(minutes=3))
    with pool.connection() as conn:
        assert hr.get_change(conn, "u1", cid).status == "applied"
        assert store.load_pack(conn, "u1").values[H4] == 100


def test_refused_undo_leaves_mirror_executed(env):
    pool, rid = env
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1)
        bot_store.mirror_action(
            conn, user_id="u1", kind="rsi_method", scope="rsi", source="rsi",
            key=f"harness:u1:{cid}", status="executed", summary="s", why="w",
            content={"change_id": "h_" + cid, "kind": "H"},
            created_at=T0, decided_at=T0)
        store.set_value(conn, "u1", H4, 60, source="user", why="x",
                        now=T0 + timedelta(minutes=2))
    with pytest.raises(ValueError):
        HarnessChanges(pool).undo("u1", cid, now=T0 + timedelta(minutes=3))
    with pool.connection() as conn:
        assert hr.get_change(conn, "u1", cid).status == "applied"
        assert bot_store.get_action_by_key(conn, f"harness:u1:{cid}").status == "executed"
        assert store.load_pack(conn, "u1").values[H4] == 60


class Registry:
    def __init__(self):
        self.v = {1: TeachingSpec(version=1, items=(SpecItem(item_id="a", text="旧规则", origin="user_ui"),))}
        self.origins: list[str] = []
        self.actors: list[str] = []
        self.fail = False

    def _row(self, n):
        return PolicyVersion(track="B", version=n, at=T0, spec=self.v[n])

    def current(self, user_id):
        return self._row(max(self.v))

    def get(self, user_id, version):
        return self._row(version)

    def commit(self, user_id, spec, *, patch_id, at, undo=False, actor="rsi"):
        self.origins.append(patch_id)
        self.actors.append(actor)
        if self.fail:
            raise RuntimeError("registry down")
        n = max(self.v) + 1
        self.v[n] = spec.model_copy(update={"version": n})
        return self._row(n)


@pytest.fixture
def env(pool):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))
        rid = hr.start_round(conn, "u1", finding=None, now=T0)
    return pool, rid


def _apply(conn, rid, ops, before, minutes, *, spec_ops=(), spec_v=None, cost=(None, None), why="练习题太难"):
    now = T0 + timedelta(minutes=minutes)
    v = None
    for o in ops:
        v = store.set_value(conn, "u1", o.name, o.value, source="improver", why="r", now=now) or v
    return hr.record_change(conn, "u1", rid, diff_to_json(PackDiff(tuple(ops), tuple(spec_ops)), before, spec_v),
                            "applied", version=v, cost_before=cost[0], cost_after=cost[1],
                            reason=why, now=now)


def test_list_shape(env):
    pool, rid = env
    with pool.connection() as conn:
        _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1, cost=(0.10, 0.12))
        _apply(conn, rid, [KeyOp(H1, "新写法")], {H1: None}, 2)
    rows = HarnessChanges(pool).list("u1")
    assert len(rows) == 2 and all(r.track == "H" and r.patch_id.startswith("h_") for r in rows)
    text, num = rows[0], rows[1]
    assert text.summary == "改了『页面内容生成』的写法" and text.why == "练习题太难" and text.can_undo
    assert num.summary == "写页面：每轮最多调用几次工具改为 100（原来 80，重放一节平均约多花 $0.020）"
    assert num.status == "personal_accepted"
    assert num.outcome == "pending" and num.effect == () and num.effect_line.startswith("数据还在收集")


def test_the_cost_change_is_on_every_h4_op_not_the_first_op(env):
    pool, rid = env
    other = "H4.deep.max_rewrites"
    with pool.connection() as conn:
        _apply(conn, rid, [KeyOp(H1, "新写法"), KeyOp(H4, 100), KeyOp(other, 2)],
               {H1: None, H4: 80, other: 1}, 1, cost=(0.010, 0.014))
    (row,) = HarnessChanges(pool).list("u1")
    assert row.summary == ("改了『页面内容生成』的写法；"
                           "写页面：每轮最多调用几次工具改为 100（原来 80，重放一节平均约多花 $0.004）；"
                           "写页面：最多重写几轮改为 2（原来 1，重放一节平均约多花 $0.004）")


def test_undo_restores_value_and_records_a_version(env):
    pool, rid = env
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100), KeyOp(H1, "新")], {H4: 80, H1: None}, 1)
        store.set_value(conn, "u1", H4, 80, source="user", why="x", now=T0)   # earlier state irrelevant
    # reset to the state after the change
    with pool.connection() as conn:
        store.set_value(conn, "u1", H4, 100, source="improver", why="r", now=T0 + timedelta(minutes=2))
    out = HarnessChanges(pool).undo("u1", "h_" + cid, now=T0 + timedelta(minutes=5))
    assert out.status == "rolled_back" and not out.can_undo
    with pool.connection() as conn:
        pack = store.load_pack(conn, "u1")
        assert pack.values[H4] == 80 and H1 not in pack.values
        h = store.history(conn, "u1", limit=2)
        assert {x.source for x in h} == {"rollback"}
        assert hr.applied_changes(conn, "u1") == []
        (lesson,) = hr.experience(conn, "u1", query=None, limit=5, now=T0 + timedelta(minutes=6))
    assert lesson.verdict == "harmful" and lesson.evidence["by"] == "user"
    assert lesson.evidence["change_id"] == cid and lesson.evidence["round_id"] == rid


def test_newer_change_on_same_key_blocks_undo(env):
    pool, rid = env
    with pool.connection() as conn:
        a = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1)
        b = _apply(conn, rid, [KeyOp(H4, 110)], {H4: 100}, 2)
    src = HarnessChanges(pool)
    rows = {r.patch_id: r for r in src.list("u1")}
    assert not rows["h_" + a].can_undo and rows["h_" + b].can_undo
    with pytest.raises(ValueError, match="又被改过"):
        src.undo("u1", "h_" + a, now=T0 + timedelta(minutes=9))
    src.undo("u1", "h_" + b, now=T0 + timedelta(minutes=9))
    assert src.list("u1")[1].can_undo           # a is undoable again once b is undone


def test_value_changed_by_someone_else_blocks_undo(env):
    pool, rid = env
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1)
        store.set_value(conn, "u1", H4, 60, source="user", why="我改的", now=T0 + timedelta(minutes=2))
    (row,) = HarnessChanges(pool).list("u1")
    assert not row.can_undo                     # the list offers no 撤销 the undo would refuse
    with pytest.raises(ValueError):
        HarnessChanges(pool).undo("u1", cid, now=T0 + timedelta(minutes=3))


def test_observe_rollback_status_and_unknown_and_double(env):
    pool, rid = env
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1)
        undo_change(conn, "u1", cid, registry=None, status="rolled_back", now=T0 + timedelta(minutes=3))
        assert hr.last_round and hr.applied_changes(conn, "u1") == []
        with pytest.raises(ValueError):
            undo_change(conn, "u1", cid, registry=None, now=T0 + timedelta(minutes=4))
        with pytest.raises(LookupError):
            undo_change(conn, "u1", "nope", registry=None, now=T0 + timedelta(minutes=4))


def test_spec_ops_are_reverted_through_the_b_path(env):
    pool, rid = env
    reg = Registry()
    add = SpecPatchOp(op="add_item", item_id="n1", text="先给例子")
    reg.v[2] = TeachingSpec(version=2, items=(*reg.v[1].items, SpecItem(
        item_id="n1", text="先给例子", origin="rsi_b", authority="rsi", origin_ref="x")))
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1, spec_ops=[add], spec_v=1)
    out = HarnessChanges(pool, reg).undo("u1", cid, now=T0 + timedelta(minutes=5))
    assert out.status == "rolled_back"
    cur = reg.current("u1").spec
    assert cur.version == 3 and [i.item_id for i in cur.items] == ["a"]


def test_spec_changed_again_blocks_undo_and_writes_nothing(env):
    pool, rid = env
    reg = Registry()
    add = SpecPatchOp(op="add_item", item_id="n1", text="先给例子")
    reg.v[2] = TeachingSpec(version=2, items=(*reg.v[1].items, SpecItem(
        item_id="n1", text="改过的", origin="user_ui")))
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1, spec_ops=[add], spec_v=1)
    with pytest.raises(ValueError, match="讲解规则"):
        HarnessChanges(pool, reg).undo("u1", cid, now=T0 + timedelta(minutes=5))
    with pool.connection() as conn:
        assert store.load_pack(conn, "u1").values[H4] == 100


def test_the_undo_spec_commit_uses_the_apply_origin_and_comes_last(env):
    pool, rid = env
    reg = Registry()
    add = SpecPatchOp(op="add_item", item_id="n1", text="先给例子")
    reg.v[2] = TeachingSpec(version=2, items=(*reg.v[1].items, SpecItem(
        item_id="n1", text="先给例子", origin="rsi_b", authority="rsi", origin_ref="x")))
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1, spec_ops=[add], spec_v=1)
    # the spec commit fails: it is the last step, so the caller's rollback leaves nothing undone
    reg.fail = True
    with pytest.raises(RuntimeError, match="registry down"):
        HarnessChanges(pool, reg).undo("u1", cid, now=T0 + timedelta(minutes=5))
    with pool.connection() as conn:
        assert store.load_pack(conn, "u1").values[H4] == 100
        assert [r.change_id for r in hr.applied_changes(conn, "u1")] == [cid]
    assert max(reg.v) == 2
    reg.fail = False
    HarnessChanges(pool, reg).undo("u1", cid, now=T0 + timedelta(minutes=6))
    # the same origin (`harness:`) as the apply's spec commit
    assert reg.origins == [f"harness:{rid}", f"harness:{rid}"] and max(reg.v) == 3


def test_a_rolled_back_change_shows_its_observed_outcome(env):
    pool, rid = env
    harm = [{"metric": "unclear_rate", "before": 0.1, "after": 0.4, "delta": -0.3, "lo": -0.5,
             "hi": -0.1, "n_before": 12, "n_after": 12}]
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1)
        hr.add_observation(conn, "u1", cid, "harmful", harm, now=T0 + timedelta(days=1))
    (row,) = HarnessChanges(pool).list("u1")
    assert row.outcome == "harmful" and len(row.effect) == 1
    assert row.effect_line == "之后看不懂的比例 10% → 40%，变差，还在确认"
    with pool.connection() as conn:
        undo_change(conn, "u1", cid, registry=None, status="rolled_back", now=T0 + timedelta(days=2))
        hr.add_observation(conn, "u1", cid, "harmful", harm, note="auto_rollback",
                           now=T0 + timedelta(days=2))
    (row,) = HarnessChanges(pool).list("u1")
    assert row.status == "rolled_back" and row.outcome == "harmful"
    assert row.effect_line == "之后看不懂的比例 10% → 40%，变差，已自动撤回"
    assert [p.note for p in row.effect] == ["", "auto_rollback"]


def test_a_refused_rollback_reads_as_held_back(env):
    pool, rid = env
    harm = [{"metric": "unclear_rate", "before": 0.1, "after": 0.4, "delta": -0.3, "lo": -0.5,
             "hi": -0.1, "n_before": 12, "n_after": 12}]
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1)
        hr.add_observation(conn, "u1", cid, "harmful", harm, note="rollback_refused",
                           reason="又被改过", now=T0 + timedelta(days=1))
    (row,) = HarnessChanges(pool).list("u1")
    assert row.effect_line == "之后看不懂的比例 10% → 40%，变差；之后的改动改了同样的地方，没有自动撤回"


def test_an_observe_rollback_leaves_a_rule_the_user_took_over_but_their_undo_goes(env):
    """Rule provenance contract §1.4: an automatic rollback only reverts what is still RSI's (the
    rule the change added is the user's now: refused, nothing written); the user's own undo is
    always allowed (committed as the user)."""
    pool, rid = env
    reg = Registry()
    add = SpecPatchOp(op="add_item", item_id="n1", text="先给例子")
    reg.v[2] = TeachingSpec(version=2, items=(*reg.v[1].items, SpecItem(
        item_id="n1", text="先给例子", origin="rsi_h", authority="user")))
    with pool.connection() as conn:
        cid = _apply(conn, rid, [KeyOp(H4, 100)], {H4: 80}, 1, spec_ops=[add], spec_v=1)
    with pool.connection() as conn, pytest.raises(ValueError, match="你的"):
        undo_change(conn, "u1", cid, registry=reg, status="rolled_back",
                    now=T0 + timedelta(minutes=5))
    with pool.connection() as conn:
        assert store.load_pack(conn, "u1").values[H4] == 100
    assert max(reg.v) == 2
    with pool.connection() as conn:
        undo_change(conn, "u1", cid, registry=reg, status="undone", now=T0 + timedelta(minutes=6))
    assert reg.actors == ["user"] and [i.item_id for i in reg.current("u1").spec.items] == ["a"]
