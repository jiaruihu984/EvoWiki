import json
import math

import pytest

from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import InferParams
from evowiki.teach.fit.priors import PathPrior, default_prior, load_priors, log_prior


def test_committed_priors_cover_every_path_with_defaults():
    """Every path has a prior; only start_known comes from public data (ASSISTments, fit-priors-v2:
    slip / guess_scale fit at a bound and were not written), the rest are the defaults."""
    pri = load_priors()
    assert set(pri) == set(ip.PATHS)
    d = InferParams()
    for path, p in pri.items():
        lo, hi = ip.bounds(path)
        assert lo <= p.mean <= hi
        if path == "start_known":
            assert p.source == "assistments2009@fit-priors-v2" and 0.0 < p.sd_logit
            continue
        assert p.mean == ip.get(d, path)
        assert p.sd_logit == 1.5 and p.source == "default"


def test_missing_path_gets_default_prior(tmp_path):
    f = tmp_path / "priors.json"
    f.write_text(json.dumps({"slip": {"mean": 0.2, "sd_logit": 0.5, "source": "x@1"}}),
                 encoding="utf-8")
    pri = load_priors(f)
    assert pri["slip"] == PathPrior(mean=0.2, sd_logit=0.5, source="x@1")
    assert pri["guess_scale"] == default_prior("guess_scale")
    assert set(pri) == set(ip.PATHS)


def test_unknown_path_or_bad_entry_fails_closed(tmp_path):
    f = tmp_path / "priors.json"
    f.write_text(json.dumps({"nope": {"mean": 0.2, "sd_logit": 1.0, "source": "default"}}),
                 encoding="utf-8")
    with pytest.raises(ValueError):
        load_priors(f)
    f.write_text(json.dumps({"slip": {"mean": 0.9, "sd_logit": 1.0, "source": "default"}}),
                 encoding="utf-8")
    with pytest.raises(ValueError):
        load_priors(f)
    f.write_text(json.dumps({"slip": {"mean": 0.1, "sd_logit": 0.0, "source": "default"}}),
                 encoding="utf-8")
    with pytest.raises(ValueError):
        load_priors(f)


def test_log_prior_peaks_at_mean_and_scales_with_strength():
    p = PathPrior(mean=0.1, sd_logit=1.5, source="default")
    assert log_prior("slip", 0.1, p, 1.0) == pytest.approx(0.0, abs=1e-12)
    a = log_prior("slip", 0.2, p, 1.0)
    b = log_prior("slip", 0.29, p, 1.0)
    assert 0.0 > a > b
    assert log_prior("slip", 0.2, p, 8.0) == pytest.approx(8.0 * a)
    # logit-scale distance: (0.2 - 0) / 0.3 = 2/3 vs 1/3
    z = math.log(2.0) - math.log(0.5)
    assert a == pytest.approx(-0.5 * (z / 1.5) ** 2)
    assert math.isfinite(log_prior("slip", 0.0, p, 1.0))     # bound endpoint stays finite
