from datetime import UTC, datetime, timedelta, timezone

import psycopg
import pytest

from evowiki.store.rsi import PgRsiStore, current_infer
from evowiki.teach import infer_paths, registry
from evowiki.teach.contracts import (
    FitPatchOp,
    GateCase,
    GateResult,
    InferParams,
    Localization,
    ParamPatchOp,
    Patch,
    PatchMemoryEntry,
    PolicyVersion,
    RsiSettings,
    SpecItem,
    SpecPatchOp,
    TeachingSpec,
)
from evowiki.teach.rsi_store import (
    CycleRecord,
    MemoryRecord,
    MemoryRsiStore,
    MemorySpecStore,
    PatchRecord,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
CST = timezone(timedelta(hours=8))


def _infer(w: float) -> InferParams:
    return infer_paths.set_(InferParams(), "evidence_weight.self", w)


def test_terminal_patch_without_mirror_does_not_create_action(pool):
    store = PgRsiStore(pool)
    store.put_patch(PatchRecord(user_id="u", patch=_patch("past", T0, status="rolled_back"),
                                cutoff=T0, updated_at=T0))
    with pool.connection() as conn:
        assert conn.execute("select count(*) as n from agent_action").fetchone()["n"] == 0


def _spec(n: int) -> TeachingSpec:
    return TeachingSpec(version=n, items=(SpecItem(item_id="i1", text="先讲例子", origin="user_ui"),))


def _patch(pid: str, at: datetime, *, track="A", status="candidate") -> Patch:
    loc = Localization(track=track, failure="overconfident" if track == "A" else "unclear",
                       target="infer_param" if track == "A" else "spec_item",
                       ref="evidence_weight.self" if track == "A" else "i1", signals=("s1",), reason="r")
    body = (ParamPatchOp(track="A", path="evidence_weight.self", value=0.4) if track == "A"
            else SpecPatchOp(op="edit_item", item_id="i1", text="更短"))
    return Patch(patch_id=pid, track=track, localization=loc, body=body, why="w", status=status,
                 from_version=1, created_at=at)


def _mem(pid: str, *, track="A", at=T0, user="u") -> MemoryRecord:
    gate = GateResult(patch_id=pid, channel=track, passed=False, vetoes=("anchor_worse",),
                      utility_before=0.31, utility_after=0.29, n_outcomes=41, ci_low=-0.01, ci_high=0.03,
                      cases=() if track == "A" else (GateCase(page_id="pg", section_id="s", anchor=True,
                                                              before="a", after="b", verdict="worse"),),
                      cost_usd=0.02)
    entry = PatchMemoryEntry(patch_id=pid, track=track, signature=f"overconfident|p|{pid}",
                             path="evidence_weight.self" if track == "A" else "i1", gate=gate,
                             status="rejected", delta_utility=-0.02, online_helpful=0.5,
                             online_harmful=1.25, at=at)
    if track == "A":
        return MemoryRecord(user_id=user, entry=entry, direction=-1, from_value=0.6, to_value=0.4,
                            counts_at=at, repeat_of_rejected=True, outcomes_at_close=40, complexity=1)
    return MemoryRecord(user_id=user, entry=entry, direction=0, from_value=None, to_value=None,
                        counts_at=at, repeat_of_rejected=False, outcomes_at_close=12, complexity=3)


@pytest.fixture(params=["memory", "pg"])
def store(request):
    if request.param == "memory":
        return MemoryRsiStore()
    return PgRsiStore(request.getfixturevalue("pool"))


# ---- contract: the same assertions against MemoryRsiStore and PgRsiStore ----

def test_versions_round_trip_and_order(store):
    assert store.versions("u", "A") == [] and store.versions("u", "B") == []
    v1 = PolicyVersion(track="A", version=1, at=T0, infer=InferParams())
    v2 = PolicyVersion(track="A", version=2, at=T0 + timedelta(hours=1), patch_id="p1", infer=_infer(0.4))
    b1 = PolicyVersion(track="B", version=1, at=T0.astimezone(CST), spec=_spec(1))
    for v in (v1, v2):
        store.add_version("u", v)
    store.add_version("u", b1)
    assert store.versions("u", "A") == [v1, v2]
    assert store.versions("u", "B") == [b1]
    assert store.versions("other", "A") == []


@pytest.mark.parametrize("bad", [1, 3])
def test_add_version_rejects_duplicate_and_gap(store, bad):
    store.add_version("u", PolicyVersion(track="A", version=1, at=T0, infer=InferParams()))
    with pytest.raises(ValueError, match="not the next"):
        store.add_version("u", PolicyVersion(track="A", version=bad, at=T0, infer=_infer(0.4)))
    assert [v.version for v in store.versions("u", "A")] == [1]


def test_add_version_first_must_be_1(store):
    with pytest.raises(ValueError, match="not the next"):
        store.add_version("u", PolicyVersion(track="A", version=2, at=T0, infer=InferParams()))
    assert store.versions("u", "A") == []


def test_patches_round_trip_upsert_and_order(store):
    assert store.patches("u") == []
    late = PatchRecord(user_id="u", patch=_patch("p2", T0 + timedelta(hours=1), track="B"), cutoff=None)
    early = PatchRecord(user_id="u", patch=_patch("p1", T0), cutoff=T0 - timedelta(days=1))
    store.put_patch(late)
    store.put_patch(early)
    assert store.patches("u") == [early, late]
    updated = PatchRecord(user_id="u", patch=_patch("p2", T0 + timedelta(hours=1), track="B",
                                                    status="awaiting_user"),
                          cutoff=None, gate_runs=2, last_gate_outcomes=37, updated_at=T0 + timedelta(days=2))
    store.put_patch(updated)
    assert store.patches("u") == [early, updated]
    assert store.patches("other") == []


def test_memory_round_trip_and_upsert(store):
    assert store.memory("u") == []
    a, b = _mem("p1"), _mem("p2", track="B", at=T0 + timedelta(hours=1))
    store.put_memory(a)
    store.put_memory(b)
    assert sorted(store.memory("u"), key=lambda r: r.entry.patch_id) == [a, b]
    bumped = MemoryRecord(user_id="u", entry=a.entry.model_copy(update={"online_harmful": 2.5}),
                          direction=-1, from_value=0.6, to_value=0.4, counts_at=T0 + timedelta(days=3),
                          repeat_of_rejected=True, outcomes_at_close=40, complexity=1)
    store.put_memory(bumped)
    assert sorted(store.memory("u"), key=lambda r: r.entry.patch_id) == [bumped, b]
    assert store.memory("other") == []


def test_fit_patch_and_its_memory_round_trip(store):
    """A track-A fit patch (A1) and its memory row (path "fit", direction 0, from/to None)."""
    loc = Localization(track="A", failure="underconfident", target="infer_param", ref="fit",
                       signals=("o1", "o2"), reason="r")
    body = FitPatchOp(values={"slip": 0.2, "evidence_weight.self": 0.35},
                      intervals={"slip": (0.15, 0.26), "evidence_weight.self": (0.1, 0.9)},
                      model_version=2)
    rec = PatchRecord(user_id="u", patch=Patch(patch_id="pf", track="A", localization=loc, body=body,
                                               why="w", from_version=1, created_at=T0),
                      cutoff=T0 - timedelta(days=1), updated_at=T0)
    store.put_patch(rec)
    assert store.patches("u") == [rec] and isinstance(store.patches("u")[0].patch.body, FitPatchOp)
    entry = PatchMemoryEntry(patch_id="pf", track="A", signature="underconfident|fit|*", path="fit",
                             gate=GateResult(patch_id="pf", channel="A", passed=True),
                             status="personal_accepted", at=T0)
    m = MemoryRecord(user_id="u", entry=entry, direction=0, from_value=None, to_value=None,
                     counts_at=T0, repeat_of_rejected=False, outcomes_at_close=84, complexity=2)
    store.put_memory(m)
    assert store.memory("u") == [m]


def test_cycles_round_trip_in_insert_order(store):
    assert store.cycles("u") == []
    c1 = CycleRecord(user_id="u", at=T0 + timedelta(hours=2), trigger="manual",
                     result={"A": {"patch_id": "p1", "passed": False}, "B": None}, cost_usd=0.05)
    c2 = CycleRecord(user_id="u", at=T0, trigger="weekly", result={}, cost_usd=0.0)
    store.add_cycle(c1)
    store.add_cycle(c2)
    assert store.cycles("u") == [c1, c2]
    assert store.cycles("other") == []


def test_settings_default_and_upsert(store):
    assert store.settings("u") == RsiSettings()
    s = RsiSettings(cycle="weekly", harness_month_limit_usd=1.5)
    store.put_settings("u", s)
    assert store.settings("u") == s
    store.put_settings("u", RsiSettings(cycle="manual"))
    assert store.settings("u") == RsiSettings(cycle="manual")
    assert store.settings("other") == RsiSettings()


def test_registry_commit_and_rollback_work_on_the_store(store):
    specs = MemorySpecStore()
    specs.put_spec("u", _spec(1))
    v2 = registry.commit(store, "u", "A", infer=_infer(0.4), patch_id="p1", now=T0)
    assert [v.version for v in store.versions("u", "A")] == [1, 2]       # implicit v1 written first
    assert registry.infer_for(store, "u") == v2.infer
    with pytest.raises(ValueError):          # track B goes through commit_spec only
        registry.commit(store, "u", "B", spec=_spec(1), patch_id="p2", now=T0, spec_store=specs)
    back = registry.rollback(store, "u", "A", 1, now=T0 + timedelta(days=1))
    assert back.version == 3 and back.infer == InferParams()
    assert store.versions("u", "B") == []


# ---- Pg only ----

def test_timestamps_come_back_utc(pool):
    s = PgRsiStore(pool)
    s.put_patch(PatchRecord(user_id="u", patch=_patch("p1", T0.astimezone(CST)), cutoff=T0.astimezone(CST),
                            updated_at=T0.astimezone(CST)))
    s.put_memory(_mem("m1", at=T0.astimezone(CST)))
    s.add_cycle(CycleRecord(user_id="u", at=T0.astimezone(CST), trigger="manual", result={}, cost_usd=0.0))
    rec = s.patches("u")[0]
    assert rec.cutoff == T0 and rec.cutoff.utcoffset() == timedelta(0)
    assert rec.updated_at.utcoffset() == timedelta(0)
    assert s.memory("u")[0].counts_at.utcoffset() == timedelta(0)
    assert s.cycles("u")[0].at.utcoffset() == timedelta(0)


def test_policy_version_rows_are_append_only(pool):
    s = PgRsiStore(pool)
    s.add_version("u", PolicyVersion(track="A", version=1, at=T0, infer=InferParams()))
    s.add_cycle(CycleRecord(user_id="u", at=T0, trigger="manual", result={}, cost_usd=0.0))
    with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("update rsi_policy_version set patch_id = 'x'")
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("delete from rsi_policy_version")
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("update rsi_cycle set cost_usd = 1")
        conn.rollback()


def test_cycle_trigger_and_cost_are_checked(pool):
    s = PgRsiStore(pool)
    with pytest.raises(psycopg.errors.CheckViolation):
        s.add_cycle(CycleRecord(user_id="u", at=T0, trigger="hourly", result={}, cost_usd=0.0))
    with pytest.raises(psycopg.errors.CheckViolation):
        s.add_cycle(CycleRecord(user_id="u", at=T0, trigger="manual", result={}, cost_usd=-1.0))
    assert s.cycles("u") == []


@pytest.mark.parametrize("sql", [
    "insert into rsi_cycle (user_id, at, trigger, result, cost_usd) values ('', now(), 'manual', '{}', 0)",
    ("insert into rsi_memory (patch_id, user_id, track, signature, path, direction, entry, counts_at, "
     "repeat_of_rejected, outcomes_at_close, complexity, at) "
     "values ('p', '', 'A', 's', 'x', 1, '{}', now(), false, 0, 1, now())"),
    ("insert into rsi_patch (patch_id, user_id, track, status, created_at, body) "
     "values ('p', '', 'A', 'candidate', now(), '{}')"),
])
def test_empty_user_id_is_rejected(pool, sql):
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(sql)


def test_patch_and_memory_ids_cannot_move_between_users(pool):
    s = PgRsiStore(pool)
    s.put_patch(PatchRecord(user_id="u", patch=_patch("p1", T0), cutoff=None))
    with pytest.raises(ValueError, match="belongs to another user"):
        s.put_patch(PatchRecord(user_id="v", patch=_patch("p1", T0), cutoff=None))
    s.put_memory(_mem("m1"))
    with pytest.raises(ValueError, match="belongs to another user"):
        s.put_memory(_mem("m1", user="v"))
    assert s.patches("v") == [] and s.memory("v") == [] and len(s.patches("u")) == 1


def test_transaction_rolls_back_both_registry_writes(pool):
    s = PgRsiStore(pool)
    with pytest.raises(RuntimeError, match="boom"), s.transaction():
        registry.commit(s, "u", "A", infer=_infer(0.4), patch_id="p1", now=T0)
        assert [v.version for v in s.versions("u", "A")] == [1, 2]       # same connection sees its writes
        raise RuntimeError("boom")
    assert s.versions("u", "A") == []
    with s.transaction():
        registry.commit(s, "u", "A", infer=_infer(0.4), patch_id="p1", now=T0)
    assert [v.version for v in s.versions("u", "A")] == [1, 2]


def test_current_infer_takes_a_connection(pool):
    s = PgRsiStore(pool)
    with pool.connection() as conn:
        assert current_infer(conn, "u") == InferParams()
    registry.commit(s, "u", "A", infer=_infer(0.4), patch_id="p1", now=T0)
    registry.commit(s, "u", "A", infer=_infer(0.8), patch_id="p2", now=T0)
    s.add_version("u", PolicyVersion(track="B", version=1, at=T0, spec=_spec(1)))
    with pool.connection() as conn, conn.transaction():
        conn.execute("set transaction isolation level repeatable read")
        assert current_infer(conn, "u") == _infer(0.8)
        assert current_infer(conn, "other") == InferParams()


def test_a_stored_infer_without_start_known_loads_with_the_default(pool):
    """θ_infer rows stored before Task 20 have no start_known: through the Pg store path they load
    and take the default 0.3 (contracts.InferParams.start_known), nothing else changes."""
    from psycopg.types.json import Jsonb
    old = _infer(0.4)
    body = PolicyVersion(track="A", version=1, at=T0, infer=old).model_dump(mode="json")
    del body["infer"]["start_known"]
    with pool.connection() as conn:
        conn.execute("insert into rsi_policy_version (user_id, track, version, at, patch_id, body) "
                     "values ('u', 'A', 1, %s, null, %s)", (T0, Jsonb(body)))
    [v] = PgRsiStore(pool).versions("u", "A")
    assert v.infer is not None and v.infer.start_known == 0.3 and v.infer == old
    with pool.connection() as conn:
        assert current_infer(conn, "u") == old
