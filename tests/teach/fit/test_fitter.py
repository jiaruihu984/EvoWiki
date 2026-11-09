import time
from datetime import timedelta

import pytest

from evowiki.goals.contracts import cell_id
from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import DEFAULT_FIT_PATHS, FitModel, InferParams
from evowiki.teach.fit.fitter import fit_infer
from evowiki.teach.fit.priors import default_prior, load_priors
from evowiki.teach.predict import predict_outcomes
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach.fit.conftest import ASPECTS, TRUE_SLIP, fresh_log, quiz_log
from tests.teach.scenarios import _obs

D = InferParams()
SLIP = FitModel(version=1, paths=("slip",))


def _fit(events, dom, model=SLIP, cutoff=None, priors=None, **kw):
    return fit_infer(events, ASPECT_CONFIG_V1, D, dom, model=model,
                     priors=load_priors() if priors is None else priors, cutoff=cutoff, **kw)


def test_recovers_slip_with_interval():
    events, dom = quiz_log(300)
    r = _fit(events, dom)
    (post,) = r.posteriors
    assert post.path == "slip" and post.prior_mean == D.slip
    assert abs(r.params.slip - TRUE_SLIP) <= 0.07, r
    assert post.value == r.params.slip
    assert post.lo < TRUE_SLIP < post.hi
    assert r.n_train == 300
    assert r.loglik_train >= r.loglik_start
    # paths outside the model keep their start value
    assert r.params.model_dump(exclude={"slip"}) == D.model_dump(exclude={"slip"})


def _every_fourth_wrong(events):
    """The same log with quiz outcomes fixed: every fourth `-`, the rest `+` (25 % wrong, the
    true slip, on well-known cells; the likelihood alone reads it as a slip well above 0.1)."""
    out, k = [], 0
    for ev in events:
        if ev.event_id.startswith("fq-q"):
            pol = "-" if k % 4 == 0 else "+"
            ev = ev.model_copy(update={"observation": ev.observation.model_copy(
                update={"polarity": pol})})
            k += 1
        out.append(ev)
    return out


def test_strong_prior_shrinks_small_sample():
    events, dom = quiz_log(20)
    events = _every_fourth_wrong(events)
    mean = load_priors()["slip"].mean
    weak = _fit(events, dom, FitModel(version=1, paths=("slip",), prior_strength=0.125))
    assert weak.params.slip - mean > 0.04, weak         # the data alone pull slip up (a grid step is 0.025)
    r = _fit(events, dom, FitModel(version=1, paths=("slip",), prior_strength=8.0))
    assert r.n_train == 20
    assert abs(r.params.slip - mean) <= 0.03, r
    assert r.loglik_train >= r.loglik_start


def test_outcomes_after_cutoff_never_influence_the_fit():
    events, dom = quiz_log(200)
    cutoff = events[-1].ts + timedelta(seconds=1)
    cells = [cell_id(c, a) for c in sorted(dom) for a in ASPECTS]
    ts = cutoff
    adversarial = []
    for n in range(50):          # all wrong: would pull slip to its upper bound
        ts += timedelta(hours=1)
        adversarial.append(_obs(f"adv{n}", ts, cells[n % len(cells)], "quiz", "-"))
    base = _fit(events, dom, cutoff=cutoff)
    more = _fit(events + adversarial, dom, cutoff=cutoff)
    assert more == base
    assert more.n_train == 200
    # without the cutoff the same outcomes do move the fit (the check above is not vacuous)
    assert _fit(events + adversarial, dom).params.slip > base.params.slip


def test_deterministic():
    events, dom = quiz_log(120)
    assert _fit(events, dom) == _fit(events, dom)


def test_recency_weighting_runs_deterministically():
    events, dom = quiz_log(150)
    m = FitModel(version=1, paths=("slip",), algorithm="coord_map_recency@1",
                 recency_half_life_days=30.0)
    r = _fit(events, dom, m)
    assert r.n_train == 150
    assert 0.0 <= r.params.slip <= 0.3
    assert r == _fit(events, dom, m)


def test_no_training_outcomes_fails_closed():
    events, dom = quiz_log(30)
    with pytest.raises(ValueError, match="no training outcomes"):
        _fit(events, dom, cutoff=events[0].ts)


def test_default_paths_on_300_outcomes_within_budget():
    events, dom = quiz_log(300)
    assert len(predict_outcomes(events, ASPECT_CONFIG_V1, D, dom)) == 300
    t = time.perf_counter()
    r = _fit(events, dom, FitModel(version=1, paths=DEFAULT_FIT_PATHS))
    assert time.perf_counter() - t < 20.0
    assert r.loglik_train >= r.loglik_start
    assert [p.path for p in r.posteriors] == list(DEFAULT_FIT_PATHS)


def test_start_known_takes_the_start_off_slip_and_guess():
    """Why start_known is in DEFAULT_FIT_PATHS (Task 20). A user who starts out knowing more than
    the K's prior: without start_known the fit can only raise P(correct) on fresh cells by pushing
    slip down and guess_scale up; with it, the start takes that share and the fit is better."""
    events, dom = fresh_log(true=InferParams(start_known=0.72, slip=0.15))
    qg = ("slip", "guess_scale")
    without = _fit(events, dom, FitModel(version=1, paths=qg))
    with_sk = _fit(events, dom, FitModel(version=1, paths=(*qg, "start_known")))
    assert with_sk.params.start_known > D.start_known
    assert with_sk.loglik_train > without.loglik_train
    assert with_sk.params.slip >= without.params.slip
    assert with_sk.params.guess_scale <= without.params.guess_scale


def test_start_known_stays_put_when_the_start_is_the_default():
    """When the user starts where the K's prior does, fitting start_known changes nothing (default
    priors: the committed start_known prior, from ASSISTments, would pull it towards its mean)."""
    flat = {p: default_prior(p) for p in ip.PATHS}
    qg = ("slip", "guess_scale")

    def fits(events, dom):
        return (_fit(events, dom, FitModel(version=1, paths=qg), priors=flat),
                _fit(events, dom, FitModel(version=1, paths=(*qg, "start_known")), priors=flat))

    without, with_sk = fits(*fresh_log(true=InferParams(slip=0.25)))
    assert with_sk.params.start_known == D.start_known
    assert with_sk.params.slip == without.params.slip
    assert with_sk.loglik_train == pytest.approx(without.loglik_train)
    # every quiz cell has evidence before its first answer: the start hardly shows, one grid step at most
    without, with_sk = fits(*quiz_log(300))
    assert 0.2 < with_sk.params.start_known <= D.start_known
    assert with_sk.params.slip == without.params.slip
    assert with_sk.loglik_train - without.loglik_train < 0.1
