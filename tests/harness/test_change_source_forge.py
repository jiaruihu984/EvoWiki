"""Undoing a harness change that promoted a forged tool (harness 3b, Task 7): the user's undo and
observe's automatic rollback both roll the forge registry back with the pack keys; the forge's own
change-list row of a harness promote is hidden and its undo refused (`HarnessForgeFilter`), while
other forge rows stay. Real Postgres and forge registry; the round is the fake round of
tests/harness/test_round_forge.py."""

from datetime import timedelta

import pytest

from evowiki.forge import registry
from evowiki.forge.change_source import ForgeChangeSource, ForgePatchChangeSource
from evowiki.harness.change_source import FORGE_ROW, HarnessChanges, HarnessForgeFilter
from evowiki.harness.observe import ObserveDeps, observe_changes
from evowiki.harness.round import _why
from evowiki.store import forge as fstore
from evowiki.store import harness as pack_store
from evowiki.store import harness_rsi
from tests.harness.test_forging import CID, insert_candidate
from tests.harness.test_observe import Signals
from tests.harness.test_round import NOW, U
from tests.harness.test_round_forge import ASK, PAIR, env_with
from tests.teach.fake_approvals import Effect, FakeApprovals


@pytest.fixture(autouse=True)
def _no_forge_env(monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE", raising=False)


def applied(pool):
    env, _ = env_with(pool, PAIR)
    r = env.run()
    assert r.status == "applied", r.reason
    return env, r.change_id


def test_at_an_ask_level_a_forged_tool_still_goes_live_and_its_row_says_what_it_does(pool):
    ap = FakeApprovals(ask=True)
    env, _ = env_with(pool, PAIR, approvals=ap)
    r = env.run()
    assert r.status == "applied", r.reason
    assert ap.proposals == [] and "t1" in _tools(pool)
    source = HarnessChanges(pool, env.registry)
    (row,) = source.list(U)
    assert row.tool_live and row.can_undo and "改之前" not in row.why
    assert row.summary == "新增了工具「等价检查」：写页面时检查两个式子是不是同一个意思"
    out = source.undo(U, row.patch_id, now=NOW + timedelta(hours=1))
    assert not out.tool_live and "t1" not in _tools(pool)


def test_at_an_ask_level_a_forged_tool_with_another_change_waits_with_it(pool):
    ap = FakeApprovals(ask=True)
    env, _ = env_with(pool, [*PAIR, {"name": "H1.write", "value": "先举例"}], approvals=ap)
    r = env.run()
    assert r.status == "asked" and len(ap.proposals) == 1 and "t1" not in _tools(pool)


def test_the_reason_is_whole_and_names_no_setting():
    long = "三条被点踩的回答都是同一毛病：" + "问题" * 150
    out = _why(f"{long}改 H2.assistant.resolve_memory_conflict 的触发条件；同时改 `H1.write`、H2.deep.zz\n技术细节")
    assert out.startswith(long) and "H1" not in out and "H2" not in out and "技术细节" not in out
    assert "助手「处理一组待确认的记忆」这个工具的说明的触发条件" in out and "『" in out and out.endswith("工具说明")


def _tools(pool):
    with pool.connection() as conn:
        return dict(registry.view(conn, U, enabled=True).tools)


def test_undo_rolls_registry_back(pool):
    env, cid = applied(pool)
    assert "t1" in _tools(pool)
    out = HarnessChanges(pool, env.registry).undo(U, "h_" + cid, now=NOW + timedelta(hours=1))
    assert out.status == "rolled_back" and not out.can_undo
    with pool.connection() as conn:
        assert fstore.get_candidate(conn, CID).status == "rolled_back"
        assert fstore.registry_history(conn, U)[0].change["by"] == "harness"
    assert env.pack().values == {}                                 # both keys back to before
    assert "t1" not in _tools(pool)


def test_undo_of_an_approved_tool_reads_the_version_from_the_pack(pool):
    env, _ = env_with(pool, [*PAIR, ASK], approvals=FakeApprovals(ask=False))
    r = env.run()
    from evowiki.harness.round import HarnessRounds
    HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=Effect())
    assert "t1" in _tools(pool)
    HarnessChanges(pool, env.registry).undo(U, "h_" + r.change_id, now=NOW + timedelta(hours=1))
    assert "t1" not in _tools(pool) and env.pack().values == {}


def test_an_undo_whose_promote_is_gone_changes_nothing(pool):
    env, cid = applied(pool)
    with pool.connection() as conn:                # the pack points at a version that is no promote
        pack_store.set_value(conn, U, "H12.tool.deep.t1",
                             {"candidate_id": CID, "registry_version": 7}, source="improver",
                             why="x", now=NOW)
    with pytest.raises(ValueError):
        HarnessChanges(pool, env.registry).undo(U, "h_" + cid, now=NOW + timedelta(hours=1))
    assert "t1" in _tools(pool)
    with pool.connection() as conn:
        assert harness_rsi.get_change(conn, U, cid).status == "applied"


def test_forge_rows_hidden(pool):
    applied(pool)
    def clock():
        return NOW
    inner = ForgeChangeSource(pool, clock)
    (row,) = inner.list(U)
    assert row.patch_id == f"forge:{U}:1"
    wrapped = HarnessForgeFilter(inner, pool)
    assert wrapped.prefix == inner.prefix and wrapped.list(U) == []
    with pytest.raises(ValueError, match=FORGE_ROW):
        wrapped.undo(U, row.patch_id, now=NOW)
    with pool.connection() as conn:
        assert fstore.get_candidate(conn, CID).status == "promoted"
    # a promote nobody's harness round made still shows and undoes through the forge
    with pool.connection() as conn:
        insert_candidate(conn, "fc_mine", name="mine")
        registry.promote(conn, "fc_mine", gate_ref="hg_x", by="user", why="好",
                         gate_ok=lambda ref: True, now=NOW)
    (mine,) = wrapped.list(U)
    assert mine.patch_id == f"forge:{U}:2"
    assert wrapped.undo(U, mine.patch_id, now=NOW).status == "rolled_back"
    # the patch source's review verbs pass through; the tool source has none
    assert getattr(HarnessForgeFilter(ForgePatchChangeSource(pool, clock), pool), "approve", None)
    assert getattr(wrapped, "approve", None) is None


def test_observe_rollback_undoes_tool(pool):
    env, cid = applied(pool)
    sig = Signals()
    sig.add("check_answer", 20, NOW - timedelta(days=3), outcome=1.0)   # no 看不懂 before
    sig.add("check_unclear", 10, NOW + timedelta(hours=1))              # all 看不懂 after
    deps = ObserveDeps(pool=pool, registry=env.registry, signals=sig)
    (res,) = observe_changes(U, deps, now=NOW + timedelta(days=2))
    assert res.verdict == "harmful" and not res.rolled_back
    assert "t1" in _tools(pool)
    sig.add("check_unclear", 10, NOW + timedelta(days=1))
    (res,) = observe_changes(U, deps, now=NOW + timedelta(days=3))
    assert res.verdict == "harmful" and res.rolled_back
    with pool.connection() as conn:
        assert harness_rsi.get_change(conn, U, cid).status == "rolled_back"
        assert fstore.get_candidate(conn, CID).status == "rolled_back"
    assert "t1" not in _tools(pool) and env.pack().values == {}


def test_an_undo_whose_tool_value_has_no_registry_version_is_refused(pool):
    from evowiki.harness.forging import UNDO_GONE
    env, cid = applied(pool)
    with pool.connection() as conn:                # the pack's value lost its registry version
        pack_store.set_value(conn, U, "H12.tool.deep.t1", {"candidate_id": CID},
                             source="improver", why="x", now=NOW)
    before = env.pack().values
    with pytest.raises(ValueError, match=UNDO_GONE):
        HarnessChanges(pool, env.registry).undo(U, "h_" + cid, now=NOW + timedelta(hours=1))
    assert "t1" in _tools(pool) and env.pack().values == before     # nothing written
    with pool.connection() as conn:
        assert harness_rsi.get_change(conn, U, cid).status == "applied"
        assert fstore.get_candidate(conn, CID).status == "promoted"


def test_undo_takes_the_pack_lock_before_the_pack_and_the_registry(pool, monkeypatch):
    """The lock order of an apply / approval (pack lock, then the forge registry's), so an undo
    and an approval cannot deadlock; the checks read the pack under the lock."""
    from evowiki.harness import change_source, forging
    env, cid = applied(pool)
    calls: list[str] = []
    real_lock, real_load, real_undo = (change_source.store.lock_key, change_source.store.load_pack,
                                       forging.undo_tool)

    def lock(conn, user_id):
        calls.append("lock")
        return real_lock(conn, user_id)

    def load(conn, user_id):
        calls.append("load")
        return real_load(conn, user_id)

    def undo_tool(conn, user_id, value):
        calls.append("registry")
        return real_undo(conn, user_id, value)
    monkeypatch.setattr(change_source.store, "lock_key", lock)
    monkeypatch.setattr(change_source.store, "load_pack", load)
    monkeypatch.setattr(forging, "undo_tool", undo_tool)
    HarnessChanges(pool, env.registry).undo(U, "h_" + cid, now=NOW + timedelta(hours=1))
    assert calls[0] == "lock" and calls.index("load") < calls.index("registry")
    assert "t1" not in _tools(pool)


def test_load_titles_reads_forge_titles_and_component_names(pool):
    from psycopg.types.json import Jsonb

    from evowiki.harness.change_source import candidate_ids, load_titles
    from evowiki.harness.diff import KeyOp, PackDiff, diff_to_json
    applied(pool)
    with pool.connection() as conn:
        conn.execute("insert into component_library (component_id, body) values (%s, %s)",
                     ("quiz-card", Jsonb({"name": "选择题卡片", "check": {"ok": True}})))
        diff = diff_to_json(PackDiff((KeyOp("H8.component.quiz-card.presets", [{"name": "a"}]),
                                      KeyOp("H8.component.gone-one.note", "x"),
                                      KeyOp("H12.tool.deep.t1", {"candidate_id": CID}))), {})
        ids = candidate_ids([diff])
        assert ids == {"component:quiz-card", "component:gone-one", CID}
        assert load_titles(conn, ids) == {"component:quiz-card": "选择题卡片", CID: "等价检查",
                                         f"does:{CID}": "写页面时检查两个式子是不是同一个意思"}
