"""scripts/rsi/fit_priors.py: the license gate and the at-a-bound refusal (synthetic results)."""

import importlib.util
from pathlib import Path

import numpy as np

from evowiki.teach import infer_paths as ip
from evowiki.teach.fit.priors import default_prior, from_logit

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "rsi" / "fit_priors.py"
_spec = importlib.util.spec_from_file_location("fit_priors", SCRIPT)
fp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fp)

GRID = 15
DEFAULTS = {p: default_prior(p) for p in ip.PATHS}


def _fits(z_slip, z_guess, se=0.4):
    """Students whose likelihood peaks at the given logits (flat MAP = the peak value)."""
    ax = np.array(fp.eb_axis(GRID))
    out = []
    for a, b in zip(z_slip, z_guess, strict=True):
        ll = -0.5 * ((ax[:, None] - a) / se) ** 2 - 0.5 * ((ax[None, :] - b) / se) ** 2
        out.append({"map": {"slip": from_logit("slip", a), "guess_scale": from_logit("guess_scale", b)},
                    "ll_grid": ll.tolist()})
    return out


def _result(fits, commit=True):
    return {"dataset": "d", "commit": commit, "paths": ["slip", "guess_scale"], "eb_grid": GRID,
            "fits": fits}


def test_identified_paths_are_written_with_source():
    rng = np.random.default_rng(0)
    fits = _fits(rng.normal(-2.0, 0.6, 200), rng.normal(0.5, 0.6, 200))
    pri, refused = fp.committed_priors([_result(fits)], DEFAULTS)
    assert refused == {}
    assert pri["slip"].source == f"d@{fp.DERIVATION}"
    assert abs(pri["slip"].mean - from_logit("slip", -2.0)) < 0.03
    assert pri["forgetting_scale"] == DEFAULTS["forgetting_scale"]


def test_path_at_a_bound_keeps_the_default_prior():
    rng = np.random.default_rng(1)
    zmax = fp.eb_axis(GRID)[-1]
    slip = np.where(rng.random(200) < 0.7, -zmax, rng.normal(-2.0, 0.6, 200))  # 70 % at a bound
    fits = _fits(slip, rng.normal(0.5, 0.6, 200))
    pri, refused = fp.committed_priors([_result(fits)], DEFAULTS)
    assert set(refused) == {"slip"}
    assert pri["slip"] == DEFAULTS["slip"]
    assert pri["guess_scale"].source == f"d@{fp.DERIVATION}"


def test_uncommittable_dataset_writes_nothing():
    rng = np.random.default_rng(2)
    fits = _fits(rng.normal(-2.0, 0.6, 50), rng.normal(0.5, 0.6, 50))
    pri, refused = fp.committed_priors([_result(fits, commit=False)], DEFAULTS)
    assert pri == DEFAULTS and refused == {}


def test_population_mean_at_a_bound_keeps_the_default_prior():
    """Flat MAPs mid-range (the share check passes) but every likelihood peaks at the slip edge."""
    rng = np.random.default_rng(3)
    zmax = fp.eb_axis(GRID)[-1]
    fits = _fits(np.full(100, -zmax), rng.normal(0.5, 0.6, 100))
    for f in fits:
        f["map"]["slip"] = 0.1
    pri, refused = fp.committed_priors([_result(fits)], DEFAULTS)
    assert refused == {"slip": "population mean within one grid step of a bound"}
    assert pri["slip"] == DEFAULTS["slip"]


def test_earlier_derived_entries_are_reset():
    stale = {**DEFAULTS, "forgetting_scale": fp.PathPrior(mean=1.2, sd_logit=0.7,
                                                          source="gone@fit-priors-v1")}
    pri, _ = fp.committed_priors([], stale)
    assert pri == DEFAULTS


def test_start_known_is_fitted_with_slip_and_guess():
    """fit-priors-v2: the K's starting mastery is fitted alongside slip / guess_scale (Task 20)."""
    assert fp.DERIVATION == "fit-priors-v2"
    assert fp.QUIZ_PATHS == ("slip", "guess_scale", "start_known")
    assert "start_known" in fp.RECALL_PATHS and set(fp.RECALL_PATHS) <= set(ip.PATHS)

    class A:
        eb_grid, eb_grid_3, eb_grid_4 = 15, 11, 5
    assert [fp.eb_grid_for(n, A) for n in (1, 2, 3, 4)] == [15, 15, 11, 5]


def test_entries_of_any_derivation_are_reset():
    for src in ("gone@fit-priors-v1", f"gone@{fp.DERIVATION}"):
        stale = {**DEFAULTS, "slip": fp.PathPrior(mean=0.2, sd_logit=0.7, source=src)}
        pri, _ = fp.committed_priors([], stale)
        assert pri == DEFAULTS
