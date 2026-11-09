"""RSI cycle step 7: A2 (layer 2 of track A). `select_model` itself is tested in
tests/teach/fit/test_select.py; here it is replaced by a scripted fake (the cycle's wiring,
due rule, registry commit, change list, monitoring and automatic rollback)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evowiki.teach import registry
from evowiki.teach.contracts import (
    DEFAULT_FIT_PATHS,
    FitModel,
    FitPatchOp,
    InferParams,
    Localization,
    MethodChange,
    OutcomeOut,
    Patch,
)
from evowiki.teach.cycle import A2_ROLLBACK_REASON, RsiCycle, UserData, _a2_summary
from evowiki.teach.fit import select
from evowiki.teach.layer2 import method_registry as mr
from evowiki.teach.layer2.method_registry import DEFAULT_FIT_MODEL
from evowiki.teach.layer2.store import MemoryLayer2Store
from evowiki.teach.rsi_store import MemoryRsiStore, PatchRecord
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach.fit.conftest import quiz_log

U = "u1"
T = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
ASK_MODEL = FitModel(version=1, paths=(*DEFAULT_FIT_PATHS, "evidence_weight.ask"))
CMP = {"ci_low": 0.004, "ci_high": 0.05, "mean": 0.02, "n": 60, "auc_a": 0.6, "auc_b": 0.65}


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n}"


def _data(events=(), dom=None):
    return UserData(events=events, domain_of=dom or {}, domain_label={}, signals_since=lambda t: 0)


def _cycle(store=None, layer2=None):
    return RsiCycle(store or MemoryRsiStore(), ASPECT_CONFIG_V1, new_id=Ids(),
                    layer2=layer2 if layer2 is not None else MemoryLayer2Store())


class FakeSelect:
    def __init__(self, picks):
        self.picks, self.calls = list(picks), []

    def __call__(self, events, base, start, domain_of, current, priors, *, cutoff, seed,
                 banned=()):
        self.calls.append({"current": current, "cutoff": cutoff, "start": start, "seed": seed,
                           "n_events": len(events), "banned": set(banned)})
        pick = self.picks.pop(0)
        if isinstance(pick, Exception):
            raise pick
        return pick


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(select, "A2_ENABLED", True)

    def install(picks):
        fake = FakeSelect(picks)
        monkeypatch.setattr(select, "select_model", fake)
        return fake
    return install


def test_production_default_is_on_after_offline_validation():
    import ast  # the module's own value, not the tests' autouse patch

    tree = ast.parse(Path(select.__file__).read_text(encoding="utf-8"))
    (value,) = [n.value.value for n in tree.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "A2_ENABLED" for t in n.targets)]
    assert value is True


def test_off_runs_no_selection(monkeypatch):
    monkeypatch.setattr(select, "A2_ENABLED", False)
    fake = FakeSelect([])
    monkeypatch.setattr(select, "select_model", fake)
    events, dom = quiz_log(120)
    c = _cycle()
    res = c.run(U, _data(events, dom), trigger="manual", now=events[-1].ts + timedelta(minutes=1))
    assert res.a2 == "off" and fake.calls == []
    assert c.layer2.method_changes(U) == [] and c.store.cycles(U)[-1].result["a2_n"] is None


def test_winner_is_committed_shown_and_used_then_waits_for_new_outcomes(enabled):
    fake = enabled([("add evidence_weight.ask", ASK_MODEL, dict(CMP)), None])
    events, dom = quiz_log(150)
    first = events[:-30]                       # the same log with 30 fewer quiz outcomes
    now = events[-1].ts + timedelta(minutes=1)
    c = _cycle()
    res = c.run(U, _data(first, dom), trigger="manual", now=now)
    assert res.a2.startswith("applied a2_"), res
    (call,) = fake.calls
    assert call["current"] == DEFAULT_FIT_MODEL and call["start"] == InferParams()
    (mc,) = c.layer2.method_changes(U, "A2")
    assert res.a2 == f"applied {mc.change_id}"
    assert mc.status == "applied" and mc.from_version == 1 and mc.to_version == 2
    assert mc.summary == "估计时多考虑了「提问行为权重」这一项"
    assert "模型" not in mc.summary and "0.020" not in mc.why
    assert mc.gate["label"] == "add evidence_weight.ask" and mc.gate["ci_low"] == CMP["ci_low"]
    assert mc.gate["n_outcomes"] == 120
    # used by the next A1 fit
    assert c.fit_model(U).paths == ASK_MODEL.paths and c.fit_model(U).version == 2
    # the change list: a layer-2 row on 估计方法 with 撤销
    (row,) = [r for r in c.changes(U, _data(first, dom)) if r.layer == 2]
    assert row.target == "估计方法" and row.can_undo
    assert row.effect_line == "数据还在收集：之后 0 个后续改动，满 10 个再判断"
    # 29 / 30 new outcomes: not due; 30: due again (no better neighbour this time)
    again = c.run(U, _data(first, dom), trigger="manual", now=now + timedelta(minutes=5))
    assert again.a2 == "not_due" and len(fake.calls) == 1
    more = c.run(U, _data(events, dom), trigger="manual", now=now + timedelta(minutes=10))
    assert more.a2 == "no_better" and len(fake.calls) == 2
    assert fake.calls[1]["current"].version == 2
    assert c.store.cycles(U)[-1].result["a2_n"] == 150
    assert len(c.layer2.method_changes(U, "A2")) == 1


def test_too_few_outcomes_and_errors_write_nothing(enabled):
    fake = enabled([ValueError("boom")])
    events, dom = quiz_log(60)
    now = events[-1].ts + timedelta(minutes=1)
    c = _cycle()
    assert c.run(U, _data(events, dom), trigger="manual", now=now).a2 == "too_few_outcomes"
    events, dom = quiz_log(120)
    res = c.run(U, _data(events, dom), trigger="manual", now=now + timedelta(days=30))
    assert res.a2.startswith("error ValueError") and len(fake.calls) == 1
    assert c.layer2.method_changes(U) == [] and c.layer2.method_versions(U, "A2") == []
    # a failed selection counts as a run: no retry until A2_EVERY new outcomes
    assert c.store.cycles(U)[-1].result["a2_n"] == 120
    again = c.run(U, _data(events, dom), trigger="manual", now=now + timedelta(days=31))
    assert again.a2 == "not_due" and len(fake.calls) == 1


def test_a1_fit_patches_carry_the_registry_model_version():
    """The production cycle (no injected fit_model) fits with the A2 registry's current model and
    stamps its version on the patch: what `_monitor_a2` groups the children by."""
    events, dom = quiz_log(120)
    l2 = MemoryLayer2Store()
    mr.commit(l2, U, "A2", fit_model=ASK_MODEL, change_id="a2_x", now=T)
    c = _cycle(layer2=l2)
    assert c.fit_model(U).version == 2
    c.run(U, _data(events, dom), trigger="manual", now=events[-1].ts + timedelta(minutes=1))
    (rec,) = c.store.patches(U)
    assert isinstance(rec.patch.body, FitPatchOp) and rec.patch.body.model_version == 2


# ---- monitoring of applied A2 changes ----

def _fit_patch(store, pid, version, status, at, from_v=1, to_v=None):
    loc = Localization(track="A", failure="overconfident", target="infer_param", ref="fit",
                       signals=("e1",), reason="r")
    p = Patch(patch_id=pid, track="A", localization=loc, why="w", status=status,
              body=FitPatchOp(values={"slip": 0.2}, model_version=version),
              from_version=from_v, to_version=to_v, created_at=at)
    store.put_patch(PatchRecord(user_id=U, patch=p, cutoff=at, updated_at=at))


def _world():
    """A2 v2 applied at T. Under v1: 12 accepted fits judged helpful; under v2: 12 gate failures."""
    store, l2 = MemoryRsiStore(), MemoryLayer2Store()
    registry.commit(store, U, "A", infer=InferParams(slip=0.2), patch_id="v1p0", now=T)
    for i in range(12):
        pid = f"v1p{i}"
        _fit_patch(store, pid, 1, "personal_accepted", T - timedelta(days=10 - i * 0.1), 1, 2)
        l2.add_outcome_point(U, OutcomeOut(change_id=pid, at=T - timedelta(days=1),
                                           verdict="helpful"))
    for i in range(12):
        _fit_patch(store, f"v2p{i}", 2, "gate_failed", T + timedelta(hours=i + 1))
    v = mr.commit(l2, U, "A2", fit_model=ASK_MODEL, change_id="a2_x", now=T)
    l2.put_method_change(U, MethodChange(change_id="a2_x", kind="A2", status="applied",
                                         summary="估计时多考虑了「提问行为权重」这一项", why="对比结果",
                                         from_version=1, to_version=v.version, created_at=T))
    return store, l2


def test_harmful_a2_change_is_rolled_back_after_confirmation(enabled):
    store, l2 = _world()
    c = _cycle(store, l2)
    now = T + timedelta(days=1)
    (row,) = c._monitor_a2(U, now)
    assert row["change_id"] == "a2_x" and row["verdict"] == "harmful"
    assert not row["rolled_back"] and row["point"]
    (ch,) = [r for r in c.changes(U, _data()) if r.layer == 2]
    assert ch.outcome == "harmful" and ch.effect_line.endswith("变差，还在确认") and ch.can_undo
    # a new A2 selection waits for the confirming / denying point
    events, dom = quiz_log(120)
    assert c._run_a2(U, _data(events, dom), now) == ("waiting_harmful", None)
    # the same evidence again: not recomputed, no rollback
    assert c._monitor_a2(U, now + timedelta(minutes=30)) == []
    # MIN_EVENTS more failed fits under v2: the second harmful point has more evidence → rollback
    # (fewer are not even recomputed: bounded work per run)
    for i in range(12, 21):
        _fit_patch(store, f"v2p{i}", 2, "gate_failed", T + timedelta(hours=20, minutes=i))
    assert c._monitor_a2(U, now + timedelta(minutes=45)) == []
    _fit_patch(store, "v2p21", 2, "gate_failed", T + timedelta(hours=21))
    (row,) = c._monitor_a2(U, now + timedelta(hours=1))
    assert row["rolled_back"] and row["note"] == A2_ROLLBACK_REASON
    (mc,) = l2.method_changes(U, "A2")
    assert mc.status == "rolled_back" and mc.why.startswith(A2_ROLLBACK_REASON)
    assert mc.gate["rollback_reason"] == A2_ROLLBACK_REASON
    assert mr.current(l2, U, "A2").version == 3
    assert mr.current_model(l2, U).paths == DEFAULT_FIT_PATHS
    (ch,) = [r for r in c.changes(U, _data()) if r.layer == 2]
    assert ch.status == "rolled_back" and not ch.can_undo
    assert ch.effect_line.endswith("变差，已自动撤回")
    assert c._monitor_a2(U, now + timedelta(hours=2)) == []


def test_cycle_monitors_applied_a2_changes_even_when_a2_is_off(monkeypatch):
    monkeypatch.setattr(select, "A2_ENABLED", False)
    store, l2 = _world()
    c = _cycle(store, l2)
    res = c.run(U, _data(), trigger="manual", now=T + timedelta(days=1))
    assert res.a2 == "off"
    rows = [o for o in c.store.cycles(U)[-1].result["outcomes"] if o["change_id"] == "a2_x"]
    assert len(rows) == 1 and set(rows[0]) == {"change_id", "verdict", "n", "point",
                                               "rolled_back", "note"}


def test_a2_summaries_are_plain_chinese():
    assert _a2_summary("add evidence_weight.ask") == "估计时多考虑了「提问行为权重」这一项"
    assert _a2_summary("drop slip").startswith("估计时不再考虑「已掌握时的答错概率」这一项")
    assert _a2_summary("prior ÷2") == "估计时更相信你自己的作答"
    assert _a2_summary("prior ×2") == "估计时更相信一般人的规律"
    assert _a2_summary("algorithm coord_map_recency@1") == "换成更看重最近表现的估计"
    for m in (DEFAULT_FIT_MODEL, ASK_MODEL.model_copy(update={"algorithm": "coord_map_recency@1",
                                                              "recency_half_life_days": 60.0})):
        for label, _ in select.neighbours(m):
            s = _a2_summary(label)
            assert "@" not in s and "evidence_weight" not in s and "模型" not in s


def test_a_run_without_select_a2_defers_the_selection(enabled):
    """The manual POST /wiki/rsi/run passes select_a2=False: no fits on the request, a2 =
    "deferred", nothing counted as an A2 run; the next run with selection (the tick) does it."""
    fake = enabled([None])
    events, dom = quiz_log(120)
    now = events[-1].ts + timedelta(minutes=1)
    c = _cycle()
    res = c.run(U, _data(events, dom), trigger="manual", now=now, select_a2=False)
    assert res.a2 == "deferred" and fake.calls == []
    assert c.store.cycles(U)[-1].result["a2_n"] is None
    assert c.run(U, _data(events, dom), trigger="every_5_feedback",
                 now=now + timedelta(minutes=1)).a2 == "no_better"
    assert len(fake.calls) == 1


def test_an_undone_a2_structure_is_never_selected_again(enabled):
    """The user undoes an A2 change: the next selection gets its structure as banned (and so
    does an automatically rolled-back one), so the same move is not re-applied."""
    fake = enabled([("add evidence_weight.ask", ASK_MODEL, dict(CMP)), None])
    events, dom = quiz_log(150)
    now = events[-1].ts + timedelta(minutes=1)
    c = _cycle()
    res = c.run(U, _data(events[:-30], dom), trigger="manual", now=now)
    cid = res.a2.split()[1]
    c.undo_method(U, cid, now=now + timedelta(minutes=1))
    assert c.fit_model(U).paths == DEFAULT_FIT_PATHS
    c.run(U, _data(events, dom), trigger="manual", now=now + timedelta(minutes=10))
    assert fake.calls[1]["banned"] == {select.structure_key(ASK_MODEL)}


def test_select_model_skips_banned_neighbours(monkeypatch):
    seen = []

    def held_out(events, base, start, domain_of, model, priors, cutoff):
        seen.append(model)
        return [(0.5, 1.0)] * 3, InferParams()
    monkeypatch.setattr(select, "_held_out", held_out)
    ask = select.structure_key(ASK_MODEL)
    select.select_model((), None, InferParams(), {}, DEFAULT_FIT_MODEL, {}, cutoff=T, seed="s",
                        banned={ask})
    assert ask not in {select.structure_key(m) for m in seen[1:]}
    assert len(seen) == len(select.neighbours(DEFAULT_FIT_MODEL))     # current + all but one


def test_a2_why_states_the_bonferroni_level():
    from evowiki.teach.cycle import _a2_interval

    text = _a2_interval({**CMP, "alpha": 0.05 / 15, "k": 15})
    assert text == "同时比较了 15 种改法，按多重比较校正后的 99.7 % 区间 0.004–0.050"
    assert "95" not in text and "模型" not in text
    assert _a2_interval(CMP) == "区间 0.004–0.050"           # no level recorded: no number
