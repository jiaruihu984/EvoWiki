"""The user's approval before an RSI change goes live (teach.approval; Bot level `ask` for
teaching_params / rsi): track A fit patches and the layer-2 method changes (A2) wait for the
user, approve applies once and only if nothing moved, reject closes them; `auto_undo` is
unchanged. Memory stores, fake approvals, scripted / stubbed fits only."""

from datetime import UTC, datetime, timedelta

import pytest

from evowiki.teach import infer_paths as ip
from evowiki.teach import registry
from evowiki.teach.approval import (
    METHOD_KIND,
    PATCH_KIND,
    RSI,
    STALE_A,
    STALE_METHOD,
    TEACHING_PARAMS,
    PendingGone,
    PendingStale,
    method_key,
    patch_key,
)
from evowiki.teach.contracts import InferParams
from evowiki.teach.cycle import USER_REJECTED, RsiCycle, UserData
from evowiki.teach.fit import select
from evowiki.teach.layer2 import method_registry as mr
from evowiki.teach.layer2.store import MemoryLayer2Store
from evowiki.teach.rsi_store import MemoryRsiStore
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach import scenarios
from tests.teach.fake_approvals import Effect, FakeApprovals
from tests.teach.fit.conftest import quiz_log
from tests.teach.test_cycle_a2 import ASK_MODEL, CMP, FakeSelect

U = "u1"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SELF = "evidence_weight.self"


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n}"


@pytest.fixture(scope="module")
def oc():
    events, dom = scenarios.overconfident_self(seed=0)
    return events, dom


def _data(events, dom):
    return UserData(events=events, domain_of=dom, domain_label={}, signals_since=lambda t: 0)


def _cycle(approvals=None, store=None, layer2=None):
    return RsiCycle(store or MemoryRsiStore(), ASPECT_CONFIG_V1, new_id=Ids(),
                    layer2=layer2 if layer2 is not None else MemoryLayer2Store(),
                    approvals=approvals)


def _proposed(oc, ap=None):
    events, dom = oc
    ap = ap or FakeApprovals()
    c = _cycle(ap)
    res = c.run(U, _data(events, dom), trigger="manual", now=NOW)
    return c, ap, res


# ---- track A ----

def test_ask_leaves_the_fit_waiting_with_a_ledger_proposal(oc):
    c, ap, res = _proposed(oc)
    assert res.a.startswith("awaiting_user "), res
    pid = res.a.split()[1]
    assert registry.current(c.store, U, "A").version == 1            # nothing went live
    (rec,) = c.store.patches(U)
    assert rec.patch.status == "awaiting_user" and rec.patch.from_version == 1
    assert rec.patch.to_version is None
    (m,) = c.store.memory(U)
    assert m.entry.status == "awaiting_user" and m.entry.online_helpful == 0
    (p,) = ap.proposals
    assert (p["kind"], p["scope"], p["key"]) == (PATCH_KIND, TEACHING_PARAMS, patch_key(U, pid))
    assert "你自己说的话" in p["summary"]
    assert p["why"] == rec.patch.why and p["content"]["patch_id"] == pid
    diff = p["content"]["diff"]
    assert diff[0] == {"kind": "ctx", "text": "学习状态评估"}
    assert {"kind": "del", "text": "自我评估权重 0.60"} in diff
    assert any(d["kind"] == "add" and d["text"].startswith("自我评估权重 ") for d in diff)
    (row,) = c.changes(U, _data(*oc))
    assert row.status == "awaiting_user" and "你自己说的话" in row.summary
    assert not row.can_undo and row.effect_line == ""
    # one track-A proposal at a time: the next run proposes nothing new
    again = c.run(U, _data(*oc), trigger="manual", now=NOW + timedelta(hours=1))
    assert again.a == f"awaiting_user {pid}" and len(c.store.patches(U)) == 1
    assert len(ap.proposals) == 1


def test_approve_applies_once(oc):
    c, _ap, res = _proposed(oc)
    pid = res.a.split()[1]
    eff = Effect()
    c.approve_pending(U, pid, now=NOW + timedelta(hours=2), on_accept=eff)
    assert eff.n == 1
    assert registry.current(c.store, U, "A").version == 2
    assert ip.get(registry.infer_for(c.store, U), SELF) < ip.get(InferParams(), SELF)
    (rec,) = c.store.patches(U)
    assert rec.patch.status == "personal_accepted" and rec.patch.to_version == 2
    assert rec.updated_at == NOW + timedelta(hours=2)
    (m,) = c.store.memory(U)
    assert m.entry.status == "personal_accepted" and m.entry.online_helpful == pytest.approx(1.0)
    (row,) = c.changes(U, _data(*oc))
    assert row.status == "personal_accepted" and row.can_undo
    # a second approval finds nothing waiting; nothing is applied twice
    with pytest.raises(PendingGone):
        c.approve_pending(U, pid, now=NOW + timedelta(hours=3), on_accept=eff)
    assert eff.n == 1 and registry.current(c.store, U, "A").version == 2
    # and it is undone like any accepted fit
    c.undo(U, pid, now=NOW + timedelta(hours=4))
    assert registry.infer_for(c.store, U) == InferParams()


def test_effect_failure_rolls_the_whole_approval_back(oc):
    c, _ap, res = _proposed(oc)
    pid = res.a.split()[1]

    def boom():
        raise RuntimeError("another approve won")
    with pytest.raises(RuntimeError):
        c.approve_pending(U, pid, now=NOW + timedelta(hours=2), on_accept=boom)
    # MemoryRsiStore has no transaction: the effect runs first, so nothing was written yet
    assert c.store.patches(U)[0].patch.status == "awaiting_user"
    assert registry.current(c.store, U, "A").version == 1


def test_reject_closes_it_and_remembers_it(oc):
    c, _ap, res = _proposed(oc)
    pid = res.a.split()[1]
    c.reject_pending(U, pid, now=NOW + timedelta(hours=2))
    (rec,) = c.store.patches(U)
    assert rec.patch.status == "rejected" and rec.patch.why.startswith(USER_REJECTED)
    (m,) = c.store.memory(U)
    assert m.entry.status == "rejected" and m.entry.online_harmful == pytest.approx(1.0)
    assert registry.current(c.store, U, "A").version == 1
    (row,) = c.changes(U, _data(*oc))
    assert row.status == "rejected" and "你自己说的话" in row.summary
    with pytest.raises(PendingGone):
        c.approve_pending(U, pid, now=NOW + timedelta(hours=3), on_accept=Effect())
    with pytest.raises(PendingGone):
        c.reject_pending(U, pid, now=NOW + timedelta(hours=3))
    # the track is free again: the next run may propose a new fit
    nxt = c.run(U, _data(*oc), trigger="manual", now=NOW + timedelta(hours=4))
    assert nxt.a.startswith("awaiting_user ") and nxt.a != f"awaiting_user {pid}"


def test_a_version_that_moved_since_the_proposal_fails_and_applies_nothing(oc):
    c, _ap, res = _proposed(oc)
    pid = res.a.split()[1]
    registry.commit(c.store, U, "A", infer=InferParams(), patch_id=None, now=NOW)  # moved: v2
    eff = Effect()
    with pytest.raises(PendingStale) as e:
        c.approve_pending(U, pid, now=NOW + timedelta(hours=2), on_accept=eff)
    assert str(e.value) == STALE_A and eff.n == 0
    assert registry.current(c.store, U, "A").version == 2
    (rec,) = c.store.patches(U)
    assert rec.patch.status == "gate_failed" and rec.patch.why.startswith(STALE_A)
    assert c.store.memory(U)[0].entry.status == "gate_failed"
    (row,) = c.changes(U, _data(*oc))
    assert "你自己说的话" in row.summary


def test_a_rejection_the_hook_missed_is_closed_by_the_next_run(oc):
    c, ap, res = _proposed(oc)
    pid = res.a.split()[1]
    ap.rejected_keys.add(patch_key(U, pid))
    c.run(U, _data(*oc), trigger="manual", now=NOW + timedelta(hours=1))
    assert next(r for r in c.store.patches(U) if r.patch.patch_id == pid).patch.status == "rejected"


def test_auto_undo_is_unchanged(oc):
    c, ap, res = _proposed(oc, FakeApprovals(ask=False))
    assert res.a.startswith("accepted ") and ap.proposals == []
    assert registry.current(c.store, U, "A").version == 2
    # an `ask` on rsi alone leaves track A applying at once
    _c2, ap2, res2 = _proposed(oc, FakeApprovals(scopes=(RSI,)))
    assert res2.a.startswith("accepted ") and ap2.proposals == []


def test_unknown_id_is_gone(oc):
    c, *_ = _proposed(oc)
    with pytest.raises(PendingGone):
        c.approve_pending(U, "nope", now=NOW, on_accept=Effect())
    with pytest.raises(PendingGone):
        c.reject_pending(U, "nope", now=NOW)


# ---- A2 ----

@pytest.fixture
def a2_on(monkeypatch):
    monkeypatch.setattr(select, "A2_ENABLED", True)

    def install(picks):
        fake = FakeSelect(picks)
        monkeypatch.setattr(select, "select_model", fake)
        return fake
    return install


def _a2_proposed(a2_on, picks=None):
    fake = a2_on(picks or [("add evidence_weight.ask", ASK_MODEL, dict(CMP))])
    events, dom = quiz_log(150)
    now = events[-1].ts + timedelta(minutes=1)
    ap = FakeApprovals(scopes=(RSI,))
    c = _cycle(ap)
    res = c.run(U, _data(events, dom), trigger="manual", now=now)
    return c, ap, res, fake, (events, dom), now


def test_a2_winner_waits_then_approval_commits_it(a2_on):
    c, ap, res, _fake, data, now = _a2_proposed(a2_on)
    assert res.a2.startswith("awaiting_user a2_"), res
    (mc,) = c.layer2.method_changes(U, "A2")
    assert mc.status == "awaiting_user" and mc.to_version is None and mc.from_version == 1
    assert mr.current(c.layer2, U, "A2").version == 1 and c.fit_model(U).version == 1
    (p,) = [p for p in ap.proposals if p["kind"] == METHOD_KIND]
    assert (p["scope"], p["key"]) == (RSI, method_key(U, mc.change_id))
    assert p["summary"] == "估计时多考虑了「提问行为权重」这一项"
    assert p["content"]["diff"] == [{"kind": "ctx", "text": "估计方法"},
                                    {"kind": "add", "text": "估计时多考虑了「提问行为权重」这一项"}]
    row = next(r for r in c.changes(U, _data(*data)) if r.patch_id == mc.change_id)
    assert row.status == "awaiting_user" and row.summary == "估计时多考虑了「提问行为权重」这一项" and not row.can_undo
    eff = Effect()
    c.approve_pending(U, mc.change_id, now=now + timedelta(hours=1), on_accept=eff)
    assert eff.n == 1
    (mc,) = c.layer2.method_changes(U, "A2")
    assert mc.status == "applied" and mc.to_version == 2 and mc.gate["to_model"]["version"] == 2
    assert c.fit_model(U).paths == ASK_MODEL.paths and c.fit_model(U).version == 2
    with pytest.raises(PendingGone):
        c.approve_pending(U, mc.change_id, now=now + timedelta(hours=2), on_accept=eff)
    assert eff.n == 1 and mr.current(c.layer2, U, "A2").version == 2


def test_a2_waits_one_at_a_time_and_a_rejected_structure_is_banned(a2_on):
    c, _ap, res, fake, _data2, now = _a2_proposed(a2_on, [("add evidence_weight.ask", ASK_MODEL,
                                                          dict(CMP)), None])
    cid = res.a2.split()[1]
    events, dom = quiz_log(200)                        # due again by outcome count
    again = c.run(U, _data(events, dom), trigger="manual", now=events[-1].ts + timedelta(minutes=1))
    assert again.a2 == f"awaiting_user {cid}" and len(fake.calls) == 1
    c.reject_pending(U, cid, now=now)
    (mc,) = c.layer2.method_changes(U, "A2")
    assert mc.status == "rejected" and mc.why.startswith(USER_REJECTED)
    assert mr.current(c.layer2, U, "A2").version == 1
    third = c.run(U, _data(events, dom), trigger="manual",
                  now=events[-1].ts + timedelta(minutes=2))
    assert third.a2 == "no_better" and len(fake.calls) == 2
    assert fake.calls[1]["banned"]                     # the rejected structure is not offered again


def test_a2_stale_when_the_registry_moved(a2_on):
    c, _ap, res, _fake, _d, now = _a2_proposed(a2_on)
    cid = res.a2.split()[1]
    mr.commit(c.layer2, U, "A2", fit_model=ASK_MODEL, change_id=None, now=now)   # moved: v2
    eff = Effect()
    with pytest.raises(PendingStale) as e:
        c.approve_pending(U, cid, now=now + timedelta(hours=1), on_accept=eff)
    assert str(e.value) == STALE_METHOD and eff.n == 0
    (mc,) = c.layer2.method_changes(U, "A2")
    assert mc.status == "gate_failed" and mc.why.startswith(STALE_METHOD)
    assert mr.current(c.layer2, U, "A2").version == 2


def test_without_approvals_nothing_is_ever_asked(oc):
    events, dom = oc
    c = _cycle(None)
    assert c.run(U, _data(events, dom), trigger="manual", now=NOW).a.startswith("accepted ")
