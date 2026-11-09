"""A2: model selection by held-out likelihood (`teach.fit.select`). No LLM.

Synthetic logs from `conftest.ask_log`: per cell three questions the user asked, then one recall
test drawn from a known θ_infer. With `evidence_weight.ask` = 0 in the truth (the questions say
nothing about what the user knows) the default FitModel — which does not fit the ask weight —
reads them at the default 0.3 and mispredicts the recalls; adding the path fixes that. With the
default θ as the truth no neighbour should be better. 300 tests: a 180 / 120 split.
Measured while choosing the log size (not to pick the seed, which stays 7): with the
Bonferroni-adjusted rule (fix round 1), seeds 1, 3, 4 and 7 select the ask path and seeds 2 and 5
select nothing (power ≈ 4/6 at 120 held-out tests; before the adjustment it was 5/6, and on seed 2
even the true θ's held-out gain is inside its interval). The default-truth log gives None on
seeds 3, 5 and 7 (tested below). One select_model run takes ≈ 20–35 s alone, more on a loaded
machine."""

import math
from datetime import timedelta

import pytest

from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import DEFAULT_FIT_PATHS, FitModel, InferParams
from evowiki.teach.fit import select
from evowiki.teach.fit.priors import load_priors
from evowiki.teach.fit.select import compare, neighbours, select_model
from evowiki.teach.predict import predict_outcomes
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach.fit.conftest import ask_log

B = ASPECT_CONFIG_V1
D = InferParams()
CUR = FitModel(version=1, paths=DEFAULT_FIT_PATHS)
N = 300


def _cutoff(events, dom):
    outs = predict_outcomes(events, B, D, dom)
    return outs[int(len(outs) * 0.6)].ts


def _select(events, dom, current=CUR):
    return select_model(events, B, D, dom, current, load_priors(), cutoff=_cutoff(events, dom),
                        seed="test")


# ---- neighbours ----

def test_neighbours_of_the_default_model():
    labels = [label for label, _ in neighbours(CUR)]
    adds = [f"add {p}" for p in ip.PATHS if p not in DEFAULT_FIT_PATHS]
    drops = [f"drop {p}" for p in DEFAULT_FIT_PATHS]
    assert labels == [*adds, *drops, "prior ×2", "prior ÷2", "algorithm coord_map_recency@1"]
    got = dict(neighbours(CUR))
    assert got["add evidence_weight.ask"].paths == (*DEFAULT_FIT_PATHS, "evidence_weight.ask")
    assert got["drop slip"].paths == tuple(p for p in DEFAULT_FIT_PATHS if p != "slip")
    assert got["prior ×2"].prior_strength == 2.0 and got["prior ÷2"].prior_strength == 0.5
    rec = got["algorithm coord_map_recency@1"]
    assert rec.recency_half_life_days == 60.0 and rec.paths == CUR.paths
    back = dict(neighbours(rec))["algorithm coord_map@1"]
    assert back.recency_half_life_days is None
    assert all(m.version == CUR.version for m in got.values())


@pytest.mark.parametrize("model", [
    FitModel(version=3, paths=("slip",), prior_strength=8.0),
    FitModel(version=1, paths=tuple(ip.PATHS), prior_strength=0.125),
    FitModel(version=2, paths=DEFAULT_FIT_PATHS, prior_strength=0.25,
             algorithm="coord_map_recency@1", recency_half_life_days=30.0),
])
def test_neighbours_stay_inside_the_whitelist_and_bounds(model):
    out = neighbours(model)
    assert out == neighbours(model)                       # deterministic
    for label, m in out:
        assert set(m.paths) <= set(ip.PATHS) and len(m.paths) >= 1
        assert len(set(m.paths)) == len(m.paths)
        assert select.PRIOR_MIN <= m.prior_strength <= select.PRIOR_MAX
        assert FitModel.model_validate(m.model_dump()) == m
        assert m != model, label
    labels = [label for label, _ in out]
    if model.prior_strength == 8.0:
        assert "prior ×2" not in labels and "prior ÷2" in labels
    if model.prior_strength == 0.125:
        assert "prior ÷2" not in labels and not any(x.startswith("add ") for x in labels)
    if len(model.paths) == 1:
        assert not any(x.startswith("drop ") for x in labels)


# ---- the decision rule (held-out pairs scripted) ----

FITTED = InferParams(slip=0.27)       # what the scripted fit returns


def _scripted(monkeypatch, losses):
    """Replace the fit + score step: model → per-outcome (p, y) with y = 1, so log-loss = -ln p.
    `losses` maps a key (tuple of paths, prior strength) to the per-outcome log-loss list. Every
    fit returns FITTED; the calls (model, start) are recorded and returned."""
    calls = []

    def fake(events, base, start, domain_of, model, priors, cutoff):
        calls.append((model, start))
        key = (model.paths, model.prior_strength)
        return [(math.exp(-x), 1.0) for x in losses.get(key, losses["base"])], FITTED
    monkeypatch.setattr(select, "_held_out", fake)
    return calls


def _jitter(base, gain, n=80):
    return [base - gain + 0.001 * ((i * 7) % 5 - 2) for i in range(n)]


def test_rule_picks_the_largest_gain_and_penalises_added_paths(monkeypatch):
    m = FitModel(version=1, paths=("slip", "guess_scale"))
    drop = ("guess_scale",)
    add = ("slip", "guess_scale", "evidence_weight.ask")
    base = [0.5] * 80
    # an added path that gains less than ADD_MIN_GAIN loses to a smaller-model gain
    _scripted(monkeypatch, {"base": base, (add, 1.0): _jitter(0.5, 0.0015),
                            (drop, 1.0): _jitter(0.5, 0.001)})
    label, got, cmp = select_model([], B, D, {}, m, {}, cutoff=None, seed="s")
    assert label == "drop slip"
    # only the add, below the penalty → None
    _scripted(monkeypatch, {"base": base, (add, 1.0): _jitter(0.5, 0.0015)})
    assert select_model([], B, D, {}, m, {}, cutoff=None, seed="s") is None
    # above the penalty it wins
    _scripted(monkeypatch, {"base": base, (add, 1.0): _jitter(0.5, 0.01),
                            (drop, 1.0): _jitter(0.5, 0.004)})
    label, got, cmp = select_model([], B, D, {}, m, {}, cutoff=None, seed="s")
    assert label == "add evidence_weight.ask" and got.paths == add
    assert cmp["mean"] == pytest.approx(0.01, abs=1e-6) and cmp["ci_low"] > 0 and cmp["n"] == 80
    assert set(cmp) == {"ci_low", "ci_high", "mean", "n", "auc_a", "auc_b", "alpha", "k"}
    assert cmp["k"] == len(neighbours(m)) and cmp["alpha"] == pytest.approx(0.05 / cmp["k"])


def test_neighbours_are_fitted_from_the_current_models_train_window_fit(monkeypatch):
    m = FitModel(version=1, paths=("slip", "guess_scale"))
    calls = _scripted(monkeypatch, {"base": [0.5] * 80})
    start = InferParams(guess_scale=1.2)
    assert select_model([], B, start, {}, m, {}, cutoff=None, seed="s") is None
    (first_model, first_start), *rest = calls
    assert first_model == m and first_start == start           # the current model: from `start`
    assert len(rest) == len(neighbours(m))
    assert all(s == FITTED for _, s in rest)                    # neighbours: from its fit


def test_rule_is_bonferroni_over_the_neighbours(monkeypatch):
    """A gain whose 95 % interval clears 0 but whose 1 - 0.05/k interval does not is not picked."""
    m = FitModel(version=1, paths=("slip", "guess_scale"))
    k = len(neighbours(m))
    gains = [0.008 + (0.03 if i % 2 else -0.03) + 0.002 * ((i * 7) % 5 - 2) for i in range(80)]
    pairs_base = [(math.exp(-0.5), 1.0)] * 80
    pairs_new = [(math.exp(-(0.5 - g)), 1.0) for g in gains]
    loose = select._compare_pairs(pairs_base, pairs_new, 1, alpha=0.05)
    strict = select._compare_pairs(pairs_base, pairs_new, 1, alpha=0.05 / k, n_boot=4000)
    assert loose["ci_low"] > 0 >= strict["ci_low"], (loose, strict)
    _scripted(monkeypatch, {"base": [0.5] * 80, (("slip",), 1.0): [0.5 - g for g in gains]})
    assert select_model([], B, D, {}, m, {}, cutoff=None, seed="s") is None


def test_rule_ties_go_to_fewer_paths(monkeypatch):
    m = FitModel(version=1, paths=("slip", "guess_scale"))
    same = _jitter(0.5, 0.01)
    _scripted(monkeypatch, {"base": [0.5] * 80, (m.paths, 0.5): same, (("slip",), 1.0): same})
    label, got, _ = select_model([], B, D, {}, m, {}, cutoff=None, seed="s")
    assert label == "drop guess_scale" and got.paths == ("slip",)


def test_rule_needs_the_whole_interval_above_zero(monkeypatch):
    m = FitModel(version=1, paths=("slip", "guess_scale"))
    noisy = [0.5 - (0.3 if i % 2 else -0.29) for i in range(80)]       # mean gain 0.005, wide
    _scripted(monkeypatch, {"base": [0.5] * 80, (("slip",), 1.0): noisy})
    assert select_model([], B, D, {}, m, {}, cutoff=None, seed="s") is None


# ---- real fits on the synthetic logs ----

@pytest.fixture(scope="module")
def ask_world():
    return ask_log(N, true=ip.set_(D, "evidence_weight.ask", 0.0))


def test_selects_the_ask_weight_when_asks_carry_no_information(ask_world):
    events, dom = ask_world
    got = _select(events, dom)
    assert got is not None
    label, model, cmp = got
    assert label == "add evidence_weight.ask", got
    assert model.paths == (*DEFAULT_FIT_PATHS, "evidence_weight.ask") and model.version == 1
    assert cmp["ci_low"] > 0 and cmp["mean"] >= select.ADD_MIN_GAIN
    assert cmp["n"] == N - int(N * 0.6)


@pytest.mark.parametrize("seed", [3, 5, 7])
def test_no_neighbour_wins_when_the_default_model_is_true(seed):
    events, dom = ask_log(N, true=D, seed=seed)
    assert _select(events, dom) is None


def test_a_dropped_path_keeps_the_train_window_value(ask_world):
    events, dom = ask_world
    cut = _cutoff(events, dom)
    _, fitted = select._held_out(events, B, D, dom, CUR, load_priors(), cut)
    assert fitted.slip != D.slip                               # the fit moved it
    drop = dict(neighbours(CUR))["drop slip"]
    _, after = select._held_out(events, B, fitted, dom, drop, load_priors(), cut)
    assert after.slip == fitted.slip


def test_compare_is_deterministic_and_a_dropped_path_keeps_its_fitted_value(ask_world):
    events, dom = ask_world
    cut = _cutoff(events, dom)
    a = FitModel(version=1, paths=("slip",))
    b = FitModel(version=1, paths=("slip", "evidence_weight.ask"))
    ab = compare(events, B, D, dom, a, b, load_priors(), cutoff=cut, seed="x")
    assert ab == compare(events, B, D, dom, a, b, load_priors(), cutoff=cut, seed="x")
    assert ab["mean"] > 0 and ab["n"] == N - int(N * 0.6)
    # swapped: the slip-only model is fitted from the slip+ask fit, so it keeps that fit's ask
    # weight (a dropped path keeps its value) and refits slip to the same MAP: same predictions
    ba = compare(events, B, D, dom, b, a, load_priors(), cutoff=cut, seed="x")
    assert ba["mean"] == pytest.approx(0.0, abs=1e-12) and ba["n"] == ab["n"]


def test_compare_fails_closed_without_held_out_outcomes(ask_world):
    events, dom = ask_world
    after = max(e.ts for e in events) + timedelta(days=1)
    with pytest.raises(ValueError, match="no held-out outcomes"):
        compare(events, B, D, dom, CUR, CUR, load_priors(), cutoff=after, seed="x")
