from datetime import timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from evowiki.belief.theta import ThetaInfer
from evowiki.belief.tracer import BeliefState, Tracer, replay
from evowiki.evidence.models import Evidence, EvidenceType, Polarity

CONCEPTS = ["vector", "matrix", "eigenvalue", "covariance", "pca", "svd"]


def _e(i, ts, cid, etype, pol):
    return Evidence(id=f"ev{i:04d}", source_event_id=f"e{i}", extractor_version="rule-v1",
                    concept_id=cid, evidence_type=etype, polarity=pol,
                    extraction_confidence=1.0, ts=ts)


def _stream(t0, specs):
    out = []
    for i, (day, cid, etype) in enumerate(specs):
        pol = Polarity.NEGATIVE if etype in (EvidenceType.QUIZ_WRONG,
                                            EvidenceType.SELF_REPORT_FORGETTING) else Polarity.POSITIVE
        out.append(_e(i, t0 + timedelta(days=day), cid, etype, pol))
    return out


evidence_specs = st.lists(
    st.tuples(st.integers(0, 60), st.sampled_from(CONCEPTS),
              st.sampled_from([EvidenceType.QUIZ_CORRECT, EvidenceType.QUIZ_WRONG,
                               EvidenceType.SELF_REPORT_POSITIVE,
                               EvidenceType.SELF_REPORT_FORGETTING])),
    min_size=1,
    max_size=40,
)


def test_ambiguity_set_and_cleared_by_recall(graph, t0):
    tr = Tracer.from_prior(graph, ThetaInfer.default(), "none")
    tr.apply(_e(0, t0, "pca", EvidenceType.QUIZ_WRONG, Polarity.NEGATIVE))
    assert tr.state.ambiguous == {"eigenvalue", "covariance"}
    tr.apply(_e(1, t0, "eigenvalue", EvidenceType.QUIZ_CORRECT, Polarity.POSITIVE))
    assert tr.state.ambiguous == {"covariance"}


def test_predict_item_uses_all_item_concepts(graph, t0):
    tr = Tracer.from_prior(graph, ThetaInfer.default(), "none")
    single = tr.predict_item("q_pca_1", t0)
    joint = tr.predict_item("q_pca_eig", t0)
    assert joint < single


def test_replay_orders_by_timestamp(graph, t0):
    theta = ThetaInfer.default()
    init = Tracer.from_prior(graph, theta, "none").state
    evs = _stream(t0, [(5, "pca", EvidenceType.QUIZ_CORRECT), (1, "pca", EvidenceType.QUIZ_WRONG)])
    assert replay(graph, theta, init, evs) == replay(graph, theta, init, list(reversed(evs)))


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])  # fixtures are immutable
@given(specs=evidence_specs, data=st.data())
def test_checkpoint_resume_equals_full_replay(graph, t0, specs, data):
    theta = ThetaInfer.default()
    init = Tracer.from_prior(graph, theta, "none").state
    evs = sorted(_stream(t0, specs), key=lambda e: (e.ts, e.id))
    cut = data.draw(st.integers(0, len(evs)), label="cut")
    full = replay(graph, theta, init, evs)
    mid = BeliefState.from_json(replay(graph, theta, init, evs[:cut]).to_json())
    resumed = replay(graph, theta, mid, evs[cut:])
    assert resumed == full


def test_checkpoint_resume_preserves_ambiguity(graph, t0):
    theta = ThetaInfer.default()
    init = Tracer.from_prior(graph, theta, "none").state
    evs = _stream(t0, [(0, "pca", EvidenceType.QUIZ_WRONG),
                       (2, "eigenvalue", EvidenceType.QUIZ_CORRECT),
                       (5, "pca", EvidenceType.QUIZ_CORRECT)])
    full = replay(graph, theta, init, evs)
    mid = BeliefState.from_json(replay(graph, theta, init, evs[:1]).to_json())
    assert mid.ambiguous == {"eigenvalue", "covariance"}
    resumed = replay(graph, theta, mid, evs[1:])
    assert resumed == full


def test_state_is_a_copy(graph, t0):
    tr = Tracer.from_prior(graph, ThetaInfer.default(), "none")
    tr.apply(_e(0, t0, "pca", EvidenceType.QUIZ_WRONG, Polarity.NEGATIVE))
    snap = tr.state
    snap.beliefs.clear()
    snap.ambiguous.add("lora")
    snap.applied = 99
    fresh = tr.state
    assert "pca" in fresh.beliefs
    assert fresh.ambiguous == {"eigenvalue", "covariance"}
    assert fresh.applied == 1
    assert fresh is not tr.state
