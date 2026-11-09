"""Multi-write RSI operations are atomic over Postgres: the RSI stores on one pool share the pinned
connection of a `transaction()` (`store.pinned`), so a failure on the second write rolls the first
one back (A2 commit + change row, method undo + change row)."""

from datetime import UTC, datetime, timedelta

import pytest

from evowiki.store.rsi import PgRsiStore
from evowiki.store.rsi_layer2 import PgLayer2Store
from evowiki.teach import registry
from evowiki.teach.contracts import (
    DEFAULT_FIT_PATHS,
    AnchorDelta,
    FitModel,
    InferParams,
    MethodChange,
    OutcomeOut,
)
from evowiki.teach.cycle import RsiCycle, UserData
from evowiki.teach.fit import select
from evowiki.teach.layer2 import method_registry as mr
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.runtime.helpers import make_user
from tests.teach.fit.conftest import quiz_log

U = "u1"
T = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
ASK = FitModel(version=1, paths=(*DEFAULT_FIT_PATHS, "evidence_weight.ask"))
CMP = {"ci_low": 0.004, "ci_high": 0.05, "mean": 0.02, "n": 60, "auc_a": 0.6, "auc_b": 0.65,
       "alpha": 0.05 / 15, "k": 15}


def _point(cid: str) -> OutcomeOut:
    return OutcomeOut(change_id=cid, at=T, verdict="pending",
                      deltas=(AnchorDelta(metric="undo_rate", n_after=3),))


def test_stores_on_one_pool_share_the_transaction(pool):
    rsi, l2 = PgRsiStore(pool), PgLayer2Store(pool)
    with pytest.raises(RuntimeError), rsi.transaction():
        registry.commit(rsi, U, "A", infer=InferParams(slip=0.2), patch_id=None, now=T)
        l2.add_outcome_point(U, _point("c1"))
        mr.commit(l2, U, "A2", fit_model=ASK, change_id="a2_x", now=T)
        raise RuntimeError("second write failed")
    assert rsi.versions(U, "A") == [] and l2.outcome_points(U) == []
    assert l2.method_versions(U, "A2") == []
    with rsi.transaction():                                 # and commits together
        l2.add_outcome_point(U, _point("c1"))
        registry.commit(rsi, U, "A", infer=InferParams(slip=0.2), patch_id=None, now=T)
    assert len(l2.outcome_points(U)) == 1 and len(rsi.versions(U, "A")) == 2


class _Ids:
    n = 0

    def __call__(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n}"


def test_a2_commit_and_change_row_are_one_write(pool, monkeypatch):
    monkeypatch.setattr(select, "A2_ENABLED", True)
    monkeypatch.setattr(select, "select_model", lambda *a, **k: ("add evidence_weight.ask", ASK,
                                                                  dict(CMP)))
    l2 = PgLayer2Store(pool)
    c = RsiCycle(PgRsiStore(pool), ASPECT_CONFIG_V1, new_id=_Ids(), layer2=l2)

    def boom(*a, **k):
        raise RuntimeError("change row write failed")
    monkeypatch.setattr(PgLayer2Store, "put_method_change", boom)
    events, dom = quiz_log(120)
    data = UserData(events=events, domain_of=dom, domain_label={}, signals_since=lambda t: 0)
    status, _ = c._a2(U, data, events[-1].ts + timedelta(minutes=1))[:2]
    assert status.startswith("error RuntimeError")
    assert l2.method_versions(U, "A2") == [] and l2.method_changes(U) == []


def test_undo_method_is_one_write(pool, monkeypatch):
    make_user(pool, U)          # the action ledger mirror of the change references app_user
    l2 = PgLayer2Store(pool)
    v = mr.commit(l2, U, "A2", fit_model=ASK, change_id="a2_x", now=T)
    l2.put_method_change(U, MethodChange(change_id="a2_x", kind="A2", status="applied",
                                         summary="s", why="w", from_version=1,
                                         to_version=v.version, created_at=T))
    c = RsiCycle(PgRsiStore(pool), ASPECT_CONFIG_V1, new_id=_Ids(), layer2=l2)

    def boom(*a, **k):
        raise RuntimeError("change row write failed")
    monkeypatch.setattr(PgLayer2Store, "put_method_change", boom)
    with pytest.raises(RuntimeError):
        c.undo_method(U, "a2_x", now=T + timedelta(hours=1))
    monkeypatch.undo()
    assert [x.version for x in l2.method_versions(U, "A2")] == [1, 2]
    assert l2.method_changes(U)[0].status == "applied"
    c.undo_method(U, "a2_x", now=T + timedelta(hours=2))
    assert [x.version for x in l2.method_versions(U, "A2")] == [1, 2, 3]
    assert l2.method_changes(U)[0].status == "rolled_back"
