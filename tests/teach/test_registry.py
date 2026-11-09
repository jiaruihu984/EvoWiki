from datetime import UTC, datetime, timedelta

import pytest

from evowiki.teach import infer_paths
from evowiki.teach.contracts import (
    InferParams,
    Localization,
    ParamPatchOp,
    Patch,
    PolicyVersion,
    RsiSettings,
    SpecItem,
    TeachingSpec,
)
from evowiki.teach.registry import commit, current, infer_for, rollback
from evowiki.teach.rsi_store import CycleRecord, MemoryRsiStore, MemorySpecStore, PatchRecord

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _infer(w: float) -> InferParams:
    return infer_paths.set_(InferParams(), "evidence_weight.self", w)


def test_current_is_implicit_v1_and_not_written():
    s = MemoryRsiStore()
    a = current(s, "u", "A")
    assert a.version == 1 and a.infer == InferParams() and a.patch_id is None
    b = current(s, "u", "B")
    assert b.version == 1 and b.spec == TeachingSpec(version=1, items=())
    assert s.versions("u", "A") == [] and s.versions("u", "B") == []
    assert infer_for(s, "u") == InferParams()


def test_commit_chain_and_rollback():
    s = MemoryRsiStore()
    v2 = commit(s, "u", "A", infer=_infer(0.8), patch_id="p1", now=T0)
    v3 = commit(s, "u", "A", infer=_infer(1.0), patch_id="p2", now=T0 + timedelta(days=1))
    assert (v2.version, v3.version) == (2, 3)
    vs = s.versions("u", "A")
    assert [v.version for v in vs] == [1, 2, 3]
    assert vs[0].infer == InferParams() and vs[0].patch_id is None
    assert infer_for(s, "u") == _infer(1.0)
    v4 = rollback(s, "u", "A", 1, now=T0 + timedelta(days=2))
    assert v4.version == 4 and v4.patch_id is None and v4.infer == vs[0].infer
    assert current(s, "u", "A") == v4
    assert s.versions("u", "B") == [] and s.versions("other", "A") == []


def test_commit_argument_checks():
    s = MemoryRsiStore()
    with pytest.raises(ValueError):
        commit(s, "u", "A", patch_id="p", now=T0)
    with pytest.raises(ValueError):
        commit(s, "u", "A", infer=InferParams(), spec=TeachingSpec(version=1, items=()), patch_id="p", now=T0)
    with pytest.raises(ValueError):       # track B without a spec store fails closed
        commit(s, "u", "B", spec=TeachingSpec(version=1, items=()), patch_id="p", now=T0)
    with pytest.raises(ValueError):
        rollback(s, "u", "A", 7, now=T0)
    assert s.versions("u", "A") == []


def test_rollback_to_implicit_v1_on_empty_store_writes_v1_then_v2():
    s = MemoryRsiStore()
    v = rollback(s, "u", "A", 1, now=T0)
    assert v.version == 2 and v.infer == InferParams()


def test_track_b_writes_are_closed_here():
    """Rule provenance contract §7.1 #20: every teaching-spec write passes the rule checkpoint
    (`wikiapp.teach.commit_spec`); this registry no longer writes track B."""
    s, specs = MemoryRsiStore(), MemorySpecStore()
    spec = TeachingSpec(version=1, items=(SpecItem(item_id="i1", text="先讲人话", origin="rsi_b", authority="rsi"),))
    with pytest.raises(ValueError, match="commit_spec"):
        commit(s, "u", "B", spec=spec, patch_id="pb", now=T0, spec_store=specs)
    with pytest.raises(ValueError, match="commit_spec"):
        rollback(s, "u", "B", 1, now=T0, spec_store=specs)
    assert s.versions("u", "B") == [] and specs.get_spec("u") == TeachingSpec(version=1, items=())


def test_add_version_duplicate_or_gap_raises():
    s = MemoryRsiStore()
    v1 = PolicyVersion(track="A", version=1, at=T0, infer=InferParams())
    s.add_version("u", v1)
    with pytest.raises(ValueError):
        s.add_version("u", v1)
    with pytest.raises(ValueError):
        s.add_version("u", PolicyVersion(track="A", version=3, at=T0, infer=InferParams()))
    assert len(s.versions("u", "A")) == 1


def _patch(pid: str, at: datetime) -> Patch:
    loc = Localization(track="A", failure="overconfident", target="infer_param", ref="evidence_weight.self",
                       signals=("s1",), reason="r")
    return Patch(patch_id=pid, track="A", localization=loc,
                 body=ParamPatchOp(track="A", path="evidence_weight.self", value=0.4), why="w",
                 from_version=1, created_at=at)


def test_store_patches_cycles_settings():
    s = MemoryRsiStore()
    s.put_patch(PatchRecord(user_id="u", patch=_patch("p2", T0 + timedelta(hours=1)), cutoff=None))
    s.put_patch(PatchRecord(user_id="u", patch=_patch("p1", T0), cutoff=T0))
    s.put_patch(PatchRecord(user_id="u", patch=_patch("p2", T0 + timedelta(hours=1)), cutoff=None, gate_runs=1))
    ps = s.patches("u")
    assert [p.patch.patch_id for p in ps] == ["p1", "p2"] and ps[1].gate_runs == 1
    assert s.patches("other") == []
    s.add_cycle(CycleRecord(user_id="u", at=T0, trigger="manual", result={"A": "none"}, cost_usd=0.0))
    assert len(s.cycles("u")) == 1 and s.cycles("x") == []
    assert s.settings("u") == RsiSettings()
    s.put_settings("u", RsiSettings(cycle="manual"))
    assert s.settings("u").cycle == "manual"


def test_track_b_implicit_v1_is_the_users_existing_spec():
    s, specs = MemoryRsiStore(), MemorySpecStore()
    mine = (SpecItem(item_id="u1", text="先给例子", origin="user_ui"),
            SpecItem(item_id="u2", text="少用术语", origin="user_chat"))
    specs.put_spec("u", TeachingSpec(version=7, items=mine))
    assert current(s, "u", "B", specs).spec == TeachingSpec(version=1, items=mine)
    assert current(s, "u", "B").spec == TeachingSpec(version=1, items=())    # read without a store
    with pytest.raises(ValueError):                                          # writes closed here
        rollback(s, "u", "B", 1, now=T0, spec_store=specs)
    with pytest.raises(ValueError):
        rollback(s, "u", "B", 1, now=T0)                                     # no spec store: fail closed
