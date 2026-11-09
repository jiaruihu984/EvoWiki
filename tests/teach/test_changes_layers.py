"""One change list for every layer (`RsiCycle.changes`): A patches (layer 0), A2 method changes
(layer 2) and external change sources, each with target, outcome series and effect line; stored
rows of the retired track B are left out; undo of a method change (`RsiCycle.undo_method`);
`layer2.outcomes.effect_line` wording."""

import logging
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.teach import infer_paths as ip
from evowiki.teach import registry
from evowiki.teach.contracts import (
    AnchorDelta,
    ChangeOut,
    FitModel,
    InferParams,
    Localization,
    MethodChange,
    OutcomeOut,
    ParamPatchOp,
    Patch,
    SpecPatchOp,
)
from evowiki.teach.cycle import RsiCycle, UserData
from evowiki.teach.layer2 import method_registry
from evowiki.teach.layer2.outcomes import (
    NOTE_AUTO_ROLLBACK,
    NOTE_ROLLBACK_BLOCKED,
    NOTE_USER_UNDO,
    UNIT_B,
    UNIT_METHOD,
    effect_line,
)
from evowiki.teach.layer2.store import MemoryLayer2Store
from evowiki.teach.rsi_store import MemoryRsiStore, PatchRecord
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1

U = "u1"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SELF = "evidence_weight.self"
DATA = UserData(events=[], domain_of={}, domain_label={}, signals_since=lambda t: 0)


def _h(n: int) -> datetime:
    return NOW + timedelta(hours=n)


def _delta(metric, before, after, n_after=24, lo=0.02, hi=0.1):
    return AnchorDelta(metric=metric, before=before, after=after, delta=None if hi is None else hi / 2, lo=lo, hi=hi,
                       n_before=30, n_after=n_after)


def _point(cid, verdict, at, *deltas):
    return OutcomeOut(change_id=cid, at=at, verdict=verdict, deltas=tuple(deltas))


class Source:
    prefix = "g_"

    def __init__(self):
        self.undone = []

    def _row(self, status="personal_accepted"):
        return ChangeOut(patch_id="g_1", track="G", status=status, summary="把「方差」拆成两页",
                         why="一页讲了两件事", signals=(), at=_h(5), can_undo=status == "personal_accepted")

    def list(self, user_id):
        return [self._row()]

    def undo(self, user_id, change_id, *, now):
        self.undone.append((user_id, change_id, now))
        return self._row("rolled_back")


class Broken:
    prefix = "x_"

    def list(self, user_id):
        raise RuntimeError("graph store down")

    def undo(self, user_id, change_id, *, now):
        raise AssertionError("not called")


DEFAULT = method_registry.DEFAULT_FIT_MODEL
FM2 = FitModel(version=1, paths=("slip", "guess_scale"))
FM3 = FitModel(version=1, paths=("slip",))


def _seed():
    store, l2 = MemoryRsiStore(), MemoryLayer2Store()
    # A: an accepted set_param patch (self 0.60 → 0.50) at NOW
    registry.commit(store, U, "A", infer=ip.set_(InferParams(), SELF, 0.5), patch_id="pa", now=NOW)
    loc = Localization(track="A", failure="overconfident", target="infer_param", ref=SELF,
                       signals=("x",), reason="主要依据「你的自述」的知识点")
    store.put_patch(PatchRecord(user_id=U, cutoff=NOW, updated_at=NOW, patch=Patch(
        patch_id="pa", track="A", localization=loc, body=ParamPatchOp(track="A", path=SELF, value=0.5),
        why="自述偏乐观", status="personal_accepted", from_version=1, to_version=2, created_at=NOW)))
    # B: a stored rule patch of the retired track B at +1h (never listed)
    bloc = Localization(track="B", failure="too_hard", target="spec_item", ref="i1",
                        signals=("s1",), reason="你说太难")
    store.put_patch(PatchRecord(user_id=U, cutoff=None, updated_at=_h(1), patch=Patch(
        patch_id="pb", track="B", localization=bloc, body=SpecPatchOp(op="add_item", text="先举例"),
        why="反馈说太难", status="personal_accepted", from_version=1, to_version=2, created_at=_h(1))))
    # A2: an applied method change at +2h (v1 → v2) and a gate-refused one at +3h
    method_registry.commit(l2, U, "A2", fit_model=FM2, change_id="mc_a2x", now=_h(2))
    l2.put_method_change(U, MethodChange(change_id="mc_a2x", kind="A2", status="applied",
                                         summary="估计时只看两项", why="猜得更准",
                                         from_version=1, to_version=2, created_at=_h(2)))
    l2.put_method_change(U, MethodChange(change_id="mc_a2", kind="A2", status="gate_failed",
                                         summary="估计时加上遗忘速度", why="复习记录多了",
                                         from_version=1, created_at=_h(3)))
    # the A patch's outcome series: pending, then helpful
    l2.add_outcome_point(U, _point("pa", "pending", _h(4), _delta("pred_logloss", 0.4, 0.38, n_after=7,
                                                                 lo=None, hi=None)))
    l2.add_outcome_point(U, _point("pa", "helpful", _h(6), _delta("pred_logloss", 0.41, 0.35)))
    return store, l2


def _cycle(store, l2, sources=()):
    return RsiCycle(store, ASPECT_CONFIG_V1, new_id=lambda p: f"{p}_x", layer2=l2, sources=sources)


def test_one_row_per_change_of_every_layer_newest_first():
    store, l2 = _seed()
    src = Source()
    c = _cycle(store, l2, (src,))
    rows = c.changes(U, DATA)
    assert [r.patch_id for r in rows] == ["g_1", "mc_a2", "mc_a2x", "pa"]     # pb: retired B
    by = {r.patch_id: r for r in rows}
    assert (by["pa"].layer, by["pa"].target, by["pa"].track) == (0, "学习状态评估", "A")
    assert (by["mc_a2x"].layer, by["mc_a2x"].target, by["mc_a2x"].track) == (2, "估计方法", "A")
    assert (by["mc_a2"].layer, by["mc_a2"].target, by["mc_a2"].track) == (2, "估计方法", "A")
    assert by["mc_a2x"].status == "personal_accepted" and by["mc_a2x"].can_undo
    assert by["mc_a2x"].summary == "估计时只看两项" and by["mc_a2x"].why == "猜得更准"
    assert by["mc_a2"].status == "gate_failed" and not by["mc_a2"].can_undo
    assert by["mc_a2"].summary == "估计时加上遗忘速度" and by["mc_a2"].effect_line == ""
    assert by["g_1"].track == "G"
    with pytest.raises(ValueError, match="retired track B"):
        c.undo(U, "pb", now=_h(7))


def test_effect_series_is_ordered_and_drives_outcome_and_line():
    store, l2 = _seed()
    by = {r.patch_id: r for r in _cycle(store, l2).changes(U, DATA)}
    pa = by["pa"]
    assert [p.verdict for p in pa.effect] == ["pending", "helpful"]
    assert pa.outcome == "helpful"
    assert pa.effect_line == "之后 24 道确认题：预测误差 0.41 → 0.35，有效"
    # applied without a point yet: data still being collected, with its cheap count (no fit
    # proposed under it yet); never applied: no line
    assert by["mc_a2x"].effect == () and by["mc_a2x"].outcome == "pending"
    assert by["mc_a2x"].effect_line == "数据还在收集：之后 0 个后续改动，满 10 个再判断"


def test_undo_of_a_method_change_rolls_the_registry_back_and_shows_undone():
    store, l2 = _seed()
    c = _cycle(store, l2)
    assert method_registry.current_model(l2, U).paths == FM2.paths
    out = c.undo_method(U, "mc_a2x", now=_h(7))
    assert out.status == "rolled_back" and out.updated_at == _h(7)
    v = method_registry.current(l2, U, "A2")
    assert v.version == 3 and v.change_id is None and v.fit_model.paths == DEFAULT.paths
    row = next(r for r in c.changes(U, DATA) if r.patch_id == "mc_a2x")
    assert row.status == "rolled_back" and not row.summary.startswith("已撤销") and not row.can_undo
    assert row.at == _h(7)
    with pytest.raises(ValueError):
        c.undo_method(U, "mc_a2x", now=_h(8))                # only applied can be undone
    with pytest.raises(ValueError):
        c.undo_method(U, "mc_a2", now=_h(8))                 # gate_failed
    with pytest.raises(LookupError):
        c.undo_method(U, "nope", now=_h(8))
    with pytest.raises(LookupError):
        c.undo_method("u2", "mc_a2x", now=_h(8))             # another user's


def test_a_failing_source_is_logged_and_skipped(caplog):
    store, l2 = _seed()
    with caplog.at_level(logging.ERROR, logger="evowiki.teach.cycle"):
        rows = _cycle(store, l2, (Broken(), Source())).changes(U, DATA)
    assert {r.patch_id for r in rows} == {"g_1", "mc_a2", "mc_a2x", "pa"}
    assert any("x_" in r.getMessage() for r in caplog.records)


# ---- effect_line ----

def test_effect_line_per_verdict():
    t = NOW
    assert effect_line(_point("c", "pending", t, _delta("check_correct", 0.5, 0.6, n_after=7,
                                                      lo=None, hi=None)))         == "数据还在收集：之后 7 道相关的题，满 10 道再判断"
    assert effect_line(_point("c", "pending", t)) == "数据还在收集：满 10 道相关的题再判断"
    assert effect_line(_point("c", "pending", t), n_now=3)         == "数据还在收集：之后 3 道相关的题，满 10 道再判断"
    assert effect_line(_point("c", "pending", t), n_now=4, unit=UNIT_METHOD)         == "数据还在收集：之后 4 个后续改动，满 10 个再判断"
    # enough after it but too little before it (a stored pending point): says so
    short = AnchorDelta(metric="check_correct", before=None, after=0.6, delta=None, lo=None,
                        hi=None, n_before=2, n_after=14)
    assert effect_line(_point("c", "pending", t, short), n_now=15, unit=UNIT_B)         == "数据还在收集：之后 15 条相关记录，改动前的记录还不够比较"
    assert effect_line(_point("c", "helpful", t, _delta("pred_logloss", 0.41, 0.35),
                              _delta("pred_auc", 0.6, 0.61, lo=-0.01, hi=0.03))) \
        == "之后 24 道确认题：预测误差 0.41 → 0.35，有效"
    assert effect_line(_point("c", "helpful", t, _delta("unclear_rate", 0.3, 0.12))) \
        == "之后看不懂的比例 30% → 12%，有效"
    harm = _point("c", "harmful", t, _delta("check_correct", 0.8, 0.6, lo=-0.3, hi=-0.1))
    assert effect_line(harm) == "之后确认题正确率 80% → 60%，变差，还在确认"
    auto = harm.model_copy(update={"note": NOTE_AUTO_ROLLBACK})
    assert effect_line(auto, rolled_back=True) == "之后确认题正确率 80% → 60%，变差，已自动撤回"
    blocked = harm.model_copy(update={"note": NOTE_ROLLBACK_BLOCKED})
    assert effect_line(blocked) == ("之后确认题正确率 80% → 60%，变差；"
                                    "之后的改动改了同样的地方，没有自动撤回")
    assert effect_line(_point("c", "no_effect", t, _delta("pred_logloss", 0.4, 0.4, lo=-0.1))) \
        == "看不出差别"


def test_effect_line_says_who_rolled_back_and_never_still_watching():
    t = NOW
    harm = _point("c", "harmful", t, _delta("check_correct", 0.8, 0.6, lo=-0.3, hi=-0.1))
    pending = _point("c", "pending", t, _delta("check_correct", 0.5, 0.6, n_after=7, lo=None,
                                                hi=None))
    for p in (harm, pending):
        undo = p.model_copy(update={"note": NOTE_USER_UNDO})
        assert effect_line(undo, rolled_back=True) == "你撤销了"
    # rolled back before the notes existed: the last measurement, 「已撤销」, never 「还在观察」
    assert effect_line(pending, rolled_back=True) == "已撤销，不再观察"
    assert effect_line(harm, rolled_back=True) == "之后确认题正确率 80% → 60%，变差，已撤销"


def test_no_model_names_in_any_line():
    store, l2 = _seed()
    for r in _cycle(store, l2).changes(U, DATA):
        text = f"{r.summary} {r.why} {r.effect_line} {r.target}"
        assert "模型" not in text and "DeepSeek" not in text and "Claude" not in text


def test_only_the_latest_method_change_of_its_kind_can_be_undone():
    store, l2 = _seed()                                      # mc_a2x: A2 v1 → v2 (applied)
    method_registry.commit(l2, U, "A2", fit_model=FM3, change_id="mc_a2x_2", now=_h(4))
    l2.put_method_change(U, MethodChange(change_id="mc_a2x_2", kind="A2", status="applied",
                                         summary="估计时只看一项", why="猜得更准",
                                         from_version=2, to_version=3, created_at=_h(4)))
    c = _cycle(store, l2)

    def undoable():
        return {r.patch_id: r.can_undo for r in c.changes(U, DATA) if r.layer == 2}
    assert undoable() == {"mc_a2x": False, "mc_a2x_2": True, "mc_a2": False}
    with pytest.raises(ValueError, match="not the latest"):
        c.undo_method(U, "mc_a2x", now=_h(7))
    assert method_registry.current(l2, U, "A2").version == 3          # nothing written
    c.undo_method(U, "mc_a2x_2", now=_h(7))
    assert method_registry.current_model(l2, U).paths == FM2.paths
    assert undoable() == {"mc_a2x": True, "mc_a2x_2": False, "mc_a2": False}  # the older is latest now
    c.undo_method(U, "mc_a2x", now=_h(8))
    v = method_registry.current(l2, U, "A2")
    assert v.version == 5 and v.fit_model.paths == DEFAULT.paths
    assert undoable() == {"mc_a2x": False, "mc_a2x_2": False, "mc_a2": False}


def test_a_never_applied_change_gets_no_effect_line_even_with_points():
    store, l2 = _seed()
    l2.add_outcome_point(U, _point("mc_a2", "helpful", _h(6), _delta("pred_logloss", 0.41, 0.35)))
    row = next(r for r in _cycle(store, l2).changes(U, DATA) if r.patch_id == "mc_a2")
    assert row.effect_line == "" and row.outcome == "pending"


def test_track_a_pending_line_counts_the_outcomes_after_acceptance():
    """Track A's cheap count = the predicted outcomes dated after the patch was accepted and not
    after `now` (one replay, as the cycle's `_measure_a`), never a stored point's number."""
    from evowiki.teach.predict import predict_outcomes
    from tests.teach import scenarios

    events, dom = scenarios.overconfident_self(seed=0)
    ts = sorted(o.ts for o in predict_outcomes(events, ASPECT_CONFIG_V1, InferParams(), dom))
    applied, now = ts[-5] - timedelta(seconds=1), ts[-2]         # 4 outcomes in (applied, now]
    store, l2 = MemoryRsiStore(), MemoryLayer2Store()
    registry.commit(store, U, "A", infer=ip.set_(InferParams(), SELF, 0.5), patch_id="pa",
                    now=applied)
    loc = Localization(track="A", failure="overconfident", target="infer_param", ref=SELF,
                       signals=("x",), reason="r")
    store.put_patch(PatchRecord(user_id=U, cutoff=applied, updated_at=applied, patch=Patch(
        patch_id="pa", track="A", localization=loc, body=ParamPatchOp(track="A", path=SELF, value=0.5),
        why="w", status="personal_accepted", from_version=1, to_version=2, created_at=applied)))
    data = UserData(events=events, domain_of=dom, domain_label={}, signals_since=lambda t: 0)
    (row,) = _cycle(store, l2).changes(U, data, now=now)
    assert row.effect_line == "数据还在收集：之后 4 道相关的题，满 10 道再判断"
    assert l2.outcome_points(U) == []


def test_a_failing_track_a_replay_or_a2_count_is_logged_and_the_rows_still_show(monkeypatch, caplog):
    """`_after_counts`: the track-A replay and the A2 count are wrapped — a failure is logged, that row's count is left out (its line says no number), every row shows."""
    import evowiki.teach.cycle as cycle_mod

    store, l2 = _seed()
    l2.put_method_change(U, MethodChange(change_id="mc_a2b", kind="A2", status="applied",
                                         summary="估计时加上遗忘速度", why="复习记录多了",
                                         from_version=1, to_version=2, created_at=_h(3)))
    l2.add_outcome_point(U, _point("pa", "pending", _h(7)))           # pa waits again: counted

    def boom(*a, **k):
        raise RuntimeError("replay broke")

    monkeypatch.setattr(cycle_mod, "predict_outcomes", boom)
    monkeypatch.setattr(RsiCycle, "_a2_children", boom)
    with caplog.at_level(logging.ERROR, logger="evowiki.teach.cycle"):
        by = {r.patch_id: r for r in _cycle(store, l2).changes(U, DATA, now=_h(9))}
    assert {"pa", "mc_a2x", "mc_a2", "mc_a2b"} <= set(by)
    assert by["pa"].effect_line == "数据还在收集：满 10 道相关的题再判断"
    assert by["mc_a2b"].effect_line == "数据还在收集：满 10 个后续改动再判断"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("track A after-side counts failed" in m for m in msgs)
    assert any("A2 after-side counts failed" in m for m in msgs)
