import numpy as np
import pytest

from evowiki.teach.fit.hier import fit_population

AX = np.linspace(-6.9, 6.9, 29)


def _grids(mu, sd, se, n, seed):
    """Per student: true z ~ N(mu, sd) per path, observed through Gaussian noise of sd `se`
    (the likelihood grid is that noise model's log-density)."""
    rng = np.random.default_rng(seed)
    z = rng.normal(mu, sd, size=(n, len(mu)))
    zhat = z + rng.normal(0.0, se, size=z.shape)
    g0, g1 = np.meshgrid(AX, AX, indexing="ij")
    ll = (-0.5 * ((g0[None] - zhat[:, 0, None, None]) / se) ** 2
          - 0.5 * ((g1[None] - zhat[:, 1, None, None]) / se) ** 2)
    return ll, zhat


def test_recovers_population_beneath_noisy_students():
    ll, zhat = _grids(mu=(-1.0, 0.5), sd=(0.8, 1.2), se=1.5, n=600, seed=1)
    mu, sd, total = fit_population(ll, [AX, AX])
    assert mu == pytest.approx([-1.0, 0.5], abs=0.2)
    assert sd == pytest.approx([0.8, 1.2], abs=0.25)
    # the naive spread of per-student estimates includes the noise
    assert float(np.std(zhat[:, 0])) > 1.4
    assert np.isfinite(total)


def test_sd_floor_and_one_path():
    rng = np.random.default_rng(2)
    zhat = rng.normal(2.0, 0.3, size=300)              # nearly no population spread
    ll = -0.5 * ((AX[None] - zhat[:, None]) / 0.3) ** 2
    mu, sd, _ = fit_population(ll, [AX], sd_floor=0.5)
    assert mu[0] == pytest.approx(2.0, abs=0.15)
    assert sd == [0.5]


def test_fails_closed():
    with pytest.raises(ValueError):
        fit_population(np.zeros((3, 4)), [AX])
    with pytest.raises(ValueError):
        fit_population(np.zeros((1, len(AX))), [AX])
    bad = np.zeros((3, len(AX)))
    bad[0, 0] = np.nan
    with pytest.raises(ValueError):
        fit_population(bad, [AX])
