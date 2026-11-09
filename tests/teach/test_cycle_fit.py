"""RSI cycle track A with the A1 fitter (MemoryRsiStore, the synthetic quiz log of tests/teach/fit:
true slip 0.25, start θ the default with slip 0.10)."""

import inspect
from datetime import timedelta

import pytest

from evowiki.teach import infer_paths as ip
from evowiki.teach import registry
from evowiki.teach.contracts import DEFAULT_FIT_PATHS, FitModel, FitPatchOp, InferParams
from evowiki.teach.cycle import RsiCycle, UserData
from evowiki.teach.layer2.method_registry import DEFAULT_FIT_MODEL, FIT_DEFAULTS_ID
from evowiki.teach.rsi_store import MemoryRsiStore
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach.fit.conftest import TRUE_SLIP, quiz_log

U = "u1"
D = InferParams()
# 300 outcomes (180 training / 120 gate) fit slip ≈ 0.20 but the gate's 120 held-out outcomes
# cannot show the gain (no_ci_improvement); 600 gives the gate 240.
N = 600


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n}"


@pytest.fixture(scope="module")
def log():
    events, dom = quiz_log(N)
    return events, dom, max(e.ts for e in events) + timedelta(minutes=1)


def _data(events, dom):
    return UserData(events=events, domain_of=dom, domain_label={}, signals_since=lambda t: 0)


def _cycle(store, **kw):
    return RsiCycle(store, ASPECT_CONFIG_V1, new_id=Ids(), **kw)


def test_constructor_has_no_llm():
    params = inspect.signature(RsiCycle).parameters
    assert "llm" not in params and {"fit_model", "priors"} <= params.keys()
    assert DEFAULT_FIT_MODEL == FitModel(version=1, paths=DEFAULT_FIT_PATHS,
                                         defaults_id=FIT_DEFAULTS_ID)


def test_fit_accepts_then_no_change_then_undo(log):
    events, dom, now = log
    store = MemoryRsiStore()
    c = _cycle(store)
    res = c.run(U, _data(events, dom), trigger="manual", now=now)
    assert res.a.startswith("accepted "), res
    (pid,) = res.patch_ids
    infer = registry.infer_for(store, U)
    assert abs(infer.slip - TRUE_SLIP) <= 0.07, infer
    (rec,) = store.patches(U)
    body = rec.patch.body
    assert isinstance(body, FitPatchOp) and body.model_version == 1 and "slip" in body.values
    assert set(body.values) <= set(DEFAULT_FIT_PATHS) and set(body.intervals) == set(body.values)
    # the plain reason from the direction (cycle._a_why): the default slip was too low, so it over-rated the learner
    assert rec.patch.why == "之前总觉得你比实际更会，这样调一下更准"
    (m,) = store.memory(U)
    assert m.entry.path == "fit" and m.direction == 0 and m.from_value is None
    assert m.to_value is None and m.complexity == len(body.values)
    assert m.entry.status == "personal_accepted" and m.entry.online_helpful == pytest.approx(1.0)

    (ch,) = c.changes(U, _data(events, dom))
    assert ch.can_undo and "会做却失手的可能" in ch.summary and ch.before.startswith("确认题预测误差")

    res2 = c.run(U, _data(events, dom), trigger="manual", now=now + timedelta(minutes=5))
    assert res2.a == "fit_no_change" and res2.patch_ids == ()
    assert len(store.patches(U)) == 1 and len(store.memory(U)) == 1

    # a later, unrelated change survives the undo of the fit
    cur = registry.infer_for(store, U)
    registry.commit(store, U, "A", infer=ip.set_(cur, "inference_strength", 0.2), patch_id=None,
                    now=now + timedelta(minutes=10))
    out = c.undo(U, pid, now=now + timedelta(minutes=20))
    assert out.patch.status == "rolled_back"
    back = registry.infer_for(store, U)
    assert back.slip == pytest.approx(D.slip) and back.inference_strength == pytest.approx(0.2)
    for path in DEFAULT_FIT_PATHS:
        assert ip.get(back, path) == pytest.approx(ip.get(D, path)), path
    (m,) = store.memory(U)
    assert m.entry.status == "rolled_back" and m.entry.online_harmful == pytest.approx(1.0)
    (ch,) = c.changes(U, _data(events, dom))
    assert ch.status == "rolled_back" and not ch.can_undo and "会做却失手的可能" in ch.summary


def test_too_few_outcomes():
    events, dom = quiz_log(19)
    at = max(e.ts for e in events) + timedelta(minutes=1)
    res = _cycle(MemoryRsiStore()).run(U, _data(events, dom), trigger="manual", now=at)
    assert res.a == "too_few_outcomes" and res.patch_ids == ()


def test_fit_model_and_priors_are_injected(log):
    """The per-user FitModel decides which paths are fitted: a model of only inference_strength
    (which this log does not inform beyond its prior mean) changes nothing."""
    events, dom, now = log
    seen = []

    def model(uid):
        seen.append(uid)
        return FitModel(version=3, paths=("inference_strength",))
    res = _cycle(MemoryRsiStore(), fit_model=model).run(U, _data(events, dom), trigger="m", now=now)
    assert seen == [U] and res.a == "fit_no_change"
