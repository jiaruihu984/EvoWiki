from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from evowiki.teach.contracts import DEFAULT_FIT_PATHS, FitModel, FitPatchOp, Localization, Patch
from evowiki.teach.infer_paths import PATHS


def test_default_paths_are_valid_infer_paths():
    assert set(DEFAULT_FIT_PATHS) <= set(PATHS)
    m = FitModel(version=1, paths=DEFAULT_FIT_PATHS)
    assert m.algorithm == "coord_map@1" and m.prior_strength == 1.0


@pytest.mark.parametrize("paths", [("slip", "slip"), ("evidence_weight.nope",)])
def test_bad_paths_rejected(paths):
    with pytest.raises(ValidationError):
        FitModel(version=1, paths=paths)


def test_recency_needs_half_life_and_only_then():
    with pytest.raises(ValidationError):
        FitModel(version=1, paths=("slip",), algorithm="coord_map_recency@1")
    with pytest.raises(ValidationError):
        FitModel(version=1, paths=("slip",), recency_half_life_days=30.0)
    FitModel(version=1, paths=("slip",), algorithm="coord_map_recency@1", recency_half_life_days=30.0)


def test_fit_op_validation():
    with pytest.raises(ValidationError):
        FitPatchOp(values={"nope": 0.1}, model_version=1)
    with pytest.raises(ValidationError):
        FitPatchOp(values={"slip": 0.3}, intervals={"slip": (0.1, 0.2)}, model_version=1)
    FitPatchOp(values={"slip": 0.12}, intervals={"slip": (0.05, 0.2)}, model_version=1)


def test_patch_round_trip():
    loc = Localization(track="A", failure="overconfident", target="infer_param", ref="slip",
                       signals=("x",), reason="r")
    patch = Patch(patch_id="p1", track="A", localization=loc,
                  body=FitPatchOp(values={"slip": 0.12}, model_version=1), why="fit",
                  from_version=1, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    back = Patch.model_validate(patch.model_dump(mode="json"))
    assert back == patch and isinstance(back.body, FitPatchOp)
