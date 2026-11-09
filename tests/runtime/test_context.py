import pytest

from evowiki.belief.theta import ThetaInfer
from evowiki.render.theta import ThetaRender
from evowiki.runtime.context import prepare_runtime
from evowiki.runtime.policy import PolicyBundle
from evowiki.store.policies import get_policy_document
from tests.runtime.helpers import T0, load_fixture_content, seed_runtime


def test_policy_version_is_a_content_hash():
    a = PolicyBundle.default()
    assert a.version_id.startswith("pv_") and len(a.version_id) == 15
    assert PolicyBundle.of(ThetaInfer.default(), ThetaRender.default()).version_id == a.version_id
    d = ThetaInfer.default().model_dump()
    d["slip"] = 0.2
    assert PolicyBundle.of(ThetaInfer.model_validate(d), ThetaRender.default()).version_id \
        != a.version_id


def test_prepare_runtime_registers_policy(pool):
    ctx = seed_runtime(pool)
    with pool.connection() as conn:
        assert get_policy_document(conn, ctx.policy.version_id) == ctx.policy.document()
    prepare_runtime(pool, ctx.content, ctx.policy, T0)  # idempotent


def test_prepare_runtime_requires_imported_content(pool):
    with pytest.raises(RuntimeError, match="import"):
        prepare_runtime(pool, load_fixture_content(), PolicyBundle.default(), T0)
