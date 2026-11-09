import pytest
from pydantic import ValidationError

from evowiki.belief.theta import BackgroundPrior, ThetaInfer
from evowiki.content.models import PrereqKind
from evowiki.evidence.models import BEHAVIORAL_TYPES, EvidenceType


def test_default_covers_every_evidence_type():
    theta = ThetaInfer.default()
    assert set(theta.evidence_weights) == set(EvidenceType)


def test_behavioral_weights_start_at_zero():
    theta = ThetaInfer.default()
    assert all(theta.weight(t) == 0.0 for t in BEHAVIORAL_TYPES)


def test_out_of_bounds_weight_rejected():
    data = ThetaInfer.default().model_dump()
    data["evidence_weights"]["quiz_correct"] = 5.0
    with pytest.raises(ValidationError):
        ThetaInfer.model_validate(data)


def test_out_of_bounds_slip_rejected():
    data = ThetaInfer.default().model_dump()
    data["slip"] = 0.9
    with pytest.raises(ValidationError):
        ThetaInfer.model_validate(data)


def test_missing_evidence_type_rejected():
    data = ThetaInfer.default().model_dump()
    del data["evidence_weights"]["misconception"]
    with pytest.raises(ValidationError):
        ThetaInfer.model_validate(data)


def test_up_alpha_formal_exceeds_pedagogical_by_default():
    ua = ThetaInfer.default().up_alpha
    assert ua.for_kind(PrereqKind.FORMAL) > ua.for_kind(PrereqKind.PEDAGOGICAL)


def test_roundtrip_json():
    theta = ThetaInfer.default()
    assert ThetaInfer.model_validate_json(theta.model_dump_json()) == theta


def test_theta_is_frozen():
    theta = ThetaInfer.default()
    with pytest.raises(ValidationError):
        theta.slip = 0.9
    with pytest.raises(ValidationError):
        theta.up_alpha.formal = 0.5


def test_evidence_weights_mapping_is_read_only():
    theta = ThetaInfer.default()
    with pytest.raises(TypeError):
        theta.evidence_weights[EvidenceType.QUIZ_CORRECT] = 99.0  # type: ignore[index]


def test_s0_by_difficulty_mapping_is_read_only():
    theta = ThetaInfer.default()
    with pytest.raises(TypeError):
        theta.forgetting.S0_by_difficulty[1] = 99.0  # type: ignore[index]


def test_prior_by_background_mappings_are_read_only():
    theta = ThetaInfer.default()
    with pytest.raises(TypeError):
        theta.prior_by_background["none"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        theta.prior_by_background["university_math"]["la"] = BackgroundPrior(  # type: ignore[index]
            mean=0.5, strength=1.0
        )


def test_roundtrip_python_dump():
    theta = ThetaInfer.default()
    assert ThetaInfer.model_validate(theta.model_dump()) == theta


def test_roundtrip_json_read_only():
    theta = ThetaInfer.default()
    back = ThetaInfer.model_validate_json(theta.model_dump_json())
    assert back == theta
    with pytest.raises(TypeError):
        back.evidence_weights[EvidenceType.QUIZ_CORRECT] = 99.0  # type: ignore[index]


def test_default_factory_mappings_are_read_only():
    theta = ThetaInfer(evidence_weights=dict(ThetaInfer.default().evidence_weights))
    with pytest.raises(TypeError):
        theta.prior_by_background["x"] = {}  # type: ignore[index]
