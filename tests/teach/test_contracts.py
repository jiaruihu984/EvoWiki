from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from evowiki.teach.contracts import (
    InferParams,
    PageFeedbackIn,
    PatchBody,
    PolicyVersion,
    RepParams,
    SpecItem,
    SpecPatchOp,
    TeachingSpec,
)

NOW = datetime(2026, 10, 9, tzinfo=UTC)


def _item(i):
    return SpecItem(item_id=f"i{i}", text="先讲人话再讲术语", origin="user_ui")


def test_spec_cap_unique_and_complexity():
    spec = TeachingSpec(version=1, items=(_item(1), _item(2)), params=RepParams(analogy_level=2))
    assert spec.complexity() == 3
    with pytest.raises(ValidationError):
        TeachingSpec(version=1, items=(_item(1), _item(1)))
    assert len(TeachingSpec(version=1, items=tuple(_item(i) for i in range(31))).items) == 31   # no count cap


def test_infer_params_clamped():
    InferParams()
    with pytest.raises(ValidationError):
        InferParams(evidence_weight={"self": 3.0})
    with pytest.raises(ValidationError):
        InferParams(forgetting_scale=5.0)


def test_page_feedback_needs_content():
    PageFeedbackIn(chips=("unclear",))
    PageFeedbackIn(text="开头太绕")
    with pytest.raises(ValidationError):
        PageFeedbackIn(text="   ")


def test_patch_bodies_discriminated_and_shaped():
    ad = TypeAdapter(PatchBody)
    assert ad.validate_python({"op": "set_param", "track": "A", "path": "slip", "value": 0.2}).op == "set_param"
    with pytest.raises(ValidationError):
        SpecPatchOp(op="edit_item", text="x")
    with pytest.raises(ValidationError):
        SpecPatchOp(op="add_item")


def test_policy_version_carries_its_track():
    PolicyVersion(track="A", version=1, at=NOW, infer=InferParams())
    with pytest.raises(ValidationError):
        PolicyVersion(track="B", version=1, at=NOW, infer=InferParams())
