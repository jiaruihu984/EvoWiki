from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from evowiki.teach.contracts import DEFAULT_FIT_PATHS, FitModel, MethodChange, MethodVersion

T = datetime(2026, 10, 1, tzinfo=UTC)


def test_method_version_carries_one_matching_model():
    fm = FitModel(version=2, paths=DEFAULT_FIT_PATHS)
    MethodVersion(kind="A2", version=2, at=T, fit_model=fm)
    with pytest.raises(ValidationError):
        MethodVersion(kind="B2", version=2, at=T, fit_model=fm)      # B2 retired (2026-10-29)
    with pytest.raises(ValidationError):
        MethodVersion(kind="A2", version=3, at=T, fit_model=fm)       # version mismatch
    with pytest.raises(ValidationError):
        MethodVersion(kind="A2", version=1, at=T)                     # no model


def test_method_change_round_trips():
    c = MethodChange(change_id="m1", kind="A2", status="applied", summary="估计时少考虑一项",
                     why="猜得更准", from_version=1, to_version=2,
                     gate={"checks": ["sim"]}, created_at=T)
    assert MethodChange.model_validate(c.model_dump(mode="json")) == c
