"""/wiki/rsi/*: manual run, change list, undo, settings; the background tick after signal routes.
Track A fits θ_infer,u statistically (no LLM call); the harness round is off by default."""

import logging

import pytest

from evowiki.service import routes_wiki, rsi_runs
from evowiki.service.rsi_runs import BUSY_DETAIL, cycle_lock, rsi_tick
from evowiki.store.rsi import PgRsiStore
from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import ChangeOut, InferParams, RsiSettings
from evowiki.teach.cycle import RsiCycle
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1, JSON_PROMPT_VERSION, PURPOSE_GROUPS
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.service.conftest import H, signup
from tests.service.test_wiki_api import organized_page
from tests.teach import scenarios
from tests.wikiapp.script import Script

SELF = "evidence_weight.self"


@pytest.fixture
def wiki(services, client):
    script = Script()
    services.gateway = script.gateway()
    return client, script


def seed(services, client, uid: str = "u1") -> None:
    """A signed-up user whose aspect log is the overconfident-self scenario (self-reports `+`,
    later checks mostly fuzzy): enough outcomes for a retrospective split and one cluster."""
    signup(client, uid)
    events, _ = scenarios.overconfident_self(seed=0)
    repo = PgRepo(services.pool)
    events = [e.model_copy(update={"event_id": f"{uid}-{e.event_id}"}) for e in events]
    repo.commit(repo.load(uid), Commit(aspect=events))   # event ids are unique across users


def run(client, uid: str = "u1") -> dict:
    r = client.post("/wiki/rsi/run", headers=H(uid))
    assert r.status_code == 200, r.text
    return r.json()


def changes(client, uid: str = "u1") -> list[ChangeOut]:
    r = client.get("/wiki/rsi/changes", headers=H(uid))
    assert r.status_code == 200, r.text
    return [ChangeOut.model_validate(c) for c in r.json()]


def self_weight(services, uid: str = "u1") -> float:
    return ip.get(PgRepo(services.pool).load(uid).k.config.infer, SELF)


def test_track_a_has_no_llm_purpose():
    assert "rsi.propose_a" not in PURPOSE_GROUPS and "rsi.propose_a" not in JSON_PROMPT_VERSION


def test_the_switch_writes_all_three_values_and_off_stops_every_round(client, services, monkeypatch):
    # Prerequisite rejection is exercised in test_setup_projection; isolate the settings
    # transaction here, without creating real provider clients or invoking a model.
    monkeypatch.setattr("evowiki.service.routes_setup.rsi_blockers", lambda _svc: [])
    signup(client, "u1")
    store = PgRsiStore(services.pool)
    assert client.get("/wiki/rsi/switch", headers=H("u1")).json() == {"on": False}
    store.put_settings("u1", RsiSettings(cycle="manual", harness=False, harness_month_limit_usd=0.0))
    assert client.put("/wiki/rsi/switch", headers=H("u1"), json={"on": True}).json() == {"on": True}
    assert store.settings("u1") == RsiSettings(cycle="weekly", harness=True, harness_month_limit_usd=None)
    assert client.get("/wiki/rsi/switch", headers=H("u1")).json() == {"on": True}
    assert client.put("/wiki/rsi/switch", headers=H("u1"), json={"on": False}).json() == {"on": False}
    assert store.settings("u1") == RsiSettings(cycle="manual", harness=False, harness_month_limit_usd=0.0)
    assert rsi_runs._due_now(services, "u1") is False


def test_settings_roundtrip(client):
    signup(client, "u1")
    r = client.get("/wiki/rsi/settings", headers=H("u1"))
    assert r.status_code == 200 and RsiSettings.model_validate(r.json()) == RsiSettings()
    r = client.put("/wiki/rsi/settings", headers=H("u1"),
                   json={"cycle": "manual", "harness_month_limit_usd": 0.5})
    assert r.status_code == 200, r.text
    assert RsiSettings.model_validate(r.json()) == RsiSettings(cycle="manual",
                                                               harness_month_limit_usd=0.5)
    assert client.get("/wiki/rsi/settings", headers=H("u1")).json()["cycle"] == "manual"
    assert client.put("/wiki/rsi/settings", headers=H("u1"),
                      json={"cycle": "daily"}).status_code == 422
    # RSI-B's keys retired (2026-10-29): refused on write …
    assert client.put("/wiki/rsi/settings", headers=H("u1"),
                      json={"gate_limit_usd": 0.5}).status_code == 422
    assert client.get("/wiki/rsi/settings", headers=H("ghost")).status_code == 404
    assert client.put("/wiki/rsi/settings", headers=H("ghost"), json={}).status_code == 404


def test_stored_settings_with_retired_keys_still_load(client, services):
    """… and dropped on read while a database has not run 0099 yet."""
    from psycopg.types.json import Jsonb
    signup(client, "u1")
    with services.pool.connection() as conn:
        conn.execute("insert into rsi_settings (user_id, settings, updated_at) values (%s, %s, now())",
                     ("u1", Jsonb({"cycle": "manual", "gate_limit_usd": 0.0, "strict_gate": False,
                                   "layer2": False, "harness": True})))
    got = PgRsiStore(services.pool).settings("u1")
    assert got == RsiSettings(cycle="manual", harness=True)


def test_unknown_user_is_404(client):
    assert client.post("/wiki/rsi/run", headers=H("ghost")).status_code == 404
    assert client.get("/wiki/rsi/changes", headers=H("ghost")).status_code == 404
    assert client.post("/wiki/rsi/patches/p/undo", headers=H("ghost")).status_code == 404


def test_run_without_data_and_without_gateway(client, services):
    signup(client, "u1")
    out = run(client)
    assert out == {"ran": True, "trigger": "manual", "a": "too_few_outcomes", "b": "off",
                   "patch_ids": [], "cost_usd": 0.0, "a2": "off"}
    assert [c.trigger for c in PgRsiStore(services.pool).cycles("u1")] == ["manual"]
    assert changes(client) == []


def test_track_a_fits_without_gateway(client, services):
    seed(services, client)
    assert run(client)["a"].startswith("accepted ")
    assert self_weight(services) < ip.get(InferParams(), SELF)


def test_run_accepts_then_undo(wiki, services):
    client, s = wiki
    seed(services, client)
    seed(services, client, "u2")
    out = run(client)
    assert out["a"].startswith("accepted ") and len(out["patch_ids"]) == 1, out
    assert s.calls() == 0                                  # track A makes no LLM call
    pid = out["patch_ids"][0]
    # the accepted θ_infer,u is what K is built with now — for u1 only
    assert self_weight(services) < ip.get(InferParams(), SELF)
    assert PgRepo(services.pool).load("u2").k.config is ASPECT_CONFIG_V1

    (ch,) = changes(client)
    assert ch.patch_id == pid and ch.status == "personal_accepted" and ch.can_undo
    # plain learner language, no numbers; a fit moves many paths and at most three are named
    assert not any(c.isdigit() for c in ch.summary) and ch.summary.endswith("，还有几项也调了")
    assert "你比实际" in ch.why
    assert ch.signals[1].endswith("道题")
    assert changes(client, "u2") == []

    assert client.post(f"/wiki/rsi/patches/{pid}/undo", headers=H("u2")).status_code == 404
    r = client.post(f"/wiki/rsi/patches/{pid}/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    undone = ChangeOut.model_validate(r.json())
    assert undone.patch_id == pid and undone.status == "rolled_back" and not undone.can_undo
    assert PgRepo(services.pool).load("u1").k.config is ASPECT_CONFIG_V1
    again = client.post(f"/wiki/rsi/patches/{pid}/undo", headers=H("u1"))
    assert again.status_code == 409 and again.json()["detail"]
    assert client.post("/wiki/rsi/patches/nope/undo", headers=H("u1")).status_code == 404


def test_undo_of_a_fit_a_newer_fit_overrides_is_409(client, services):
    from evowiki.teach import registry
    from evowiki.teach.contracts import FitPatchOp, Localization, Patch
    from evowiki.teach.rsi_store import PatchRecord

    signup(client, "u1")
    store = PgRsiStore(services.pool)
    now = services.clock()
    loc = Localization(track="A", failure="overconfident", target="infer_param", ref="fit",
                       signals=("e1",), reason="r")
    for pid, slip, frm in (("p1", 0.2, 1), ("p2", 0.24, 2)):
        v = registry.commit(store, "u1", "A", infer=InferParams(slip=slip), patch_id=pid, now=now)
        store.put_patch(PatchRecord(user_id="u1", updated_at=now, cutoff=now, patch=Patch(
            patch_id=pid, track="A", localization=loc, why="w", status="personal_accepted",
            body=FitPatchOp(values={"slip": slip}, model_version=1), from_version=frm,
            to_version=v.version, created_at=now)))
    rows = {c.patch_id: c for c in changes(client)}
    assert not rows["p1"].can_undo and rows["p2"].can_undo
    r = client.post("/wiki/rsi/patches/p1/undo", headers=H("u1"))
    assert r.status_code == 409 and r.json()["detail"] == "这项更改现在不能撤销"   # the raw reason (newer fit p2) is logged
    assert client.post("/wiki/rsi/patches/p2/undo", headers=H("u1")).status_code == 200
    assert client.post("/wiki/rsi/patches/p1/undo", headers=H("u1")).status_code == 200


def test_manual_run_defers_a2_selection_to_the_background_tick(client, services, monkeypatch):
    from evowiki.teach.fit import select

    monkeypatch.setattr(select, "A2_ENABLED", True)

    def never(*a, **k):
        raise AssertionError("A2 selection must not run inside POST /wiki/rsi/run")
    monkeypatch.setattr(select, "select_model", never)
    seed(services, client)
    out = run(client)
    assert out["a2"] == "deferred"


# ---- background tick ----

def test_tick_runs_when_due_and_respects_manual(client, services):
    seed(services, client)
    store = PgRsiStore(services.pool)
    store.put_settings("u1", RsiSettings(cycle="manual"))
    rsi_tick(services, "u1")
    assert store.cycles("u1") == []
    store.put_settings("u1", RsiSettings(cycle="every_5_feedback"))   # many signals
    rsi_tick(services, "u1")
    (c,) = store.cycles("u1")
    assert c.trigger == "every_5_feedback" and c.result["a"].startswith("accepted ")
    rsi_tick(services, "u1")                                 # no new signal since: not due
    assert len(store.cycles("u1")) == 1


def test_tick_never_raises(client, services, monkeypatch, caplog):
    signup(client, "u1")

    def boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(rsi_runs, "_due_now", lambda svc, uid: True)   # past the cheap pre-check
    monkeypatch.setattr(RsiCycle, "maybe_run", boom)
    with caplog.at_level(logging.ERROR, logger=rsi_runs.log.name):
        rsi_tick(services, "u1")
        rsi_tick(services, "ghost")
    assert sum("rsi tick failed" in r.message for r in caplog.records) == 2


def test_not_due_tick_never_builds_user_data(client, services, monkeypatch):
    """A tick that is not due decides from settings + last cycle + an Observe count: no snapshot
    load, no replay (the UserData builder is never called)."""
    seed(services, client)
    store = PgRsiStore(services.pool)
    built = []
    real = rsi_runs._data

    def spy(svc, uid):
        built.append(uid)
        return real(svc, uid)
    monkeypatch.setattr(rsi_runs, "_data", spy)
    monkeypatch.setattr(PgRepo, "load", lambda *a, **kw: pytest.fail("snapshot loaded"))
    signup(client, "u2")
    rsi_tick(services, "u2")                                 # no signals at all
    store.put_settings("u1", RsiSettings(cycle="manual"))
    rsi_tick(services, "u1")
    store.put_settings("u1", RsiSettings(cycle="weekly"))
    rsi_tick(services, "ghost")                              # unknown user: zero signals, not due
    assert built == [] and store.cycles("u1") == []
    monkeypatch.undo()
    monkeypatch.setattr(rsi_runs, "_data", spy)
    store.put_settings("u1", RsiSettings(cycle="every_5_feedback"))
    rsi_tick(services, "u1")                                 # due: built once, one cycle
    assert built == ["u1"] and len(store.cycles("u1")) == 1
    rsi_tick(services, "u1")                                 # no new Observe since: not built
    store.put_settings("u1", RsiSettings(cycle="weekly"))
    rsi_tick(services, "u1")                                 # weekly, last cycle just now
    assert built == ["u1"] and len(store.cycles("u1")) == 1


@pytest.fixture
def ticks(monkeypatch):
    seen = []
    monkeypatch.setattr(routes_wiki, "rsi_tick", lambda svc, uid: seen.append(uid))
    return seen


def test_signal_routes_schedule_the_tick(wiki, services, ticks):
    client, _ = wiki
    pid = organized_page(wiki)
    assert ticks == []
    r = client.post(f"/wiki/pages/{pid}/checks/s4", headers=H("u1"), json={"choice": 0})
    assert r.status_code == 200, r.text
    assert ticks == ["u1"]
    r = client.post(f"/wiki/pages/{pid}/sections/s2/feedback", headers=H("u1"),
                    json={"kind": "know"})
    assert r.status_code == 200, r.text
    assert ticks == ["u1", "u1"]
    r = client.post("/wiki/review/grade", headers=H("u1"),
                    json={"cell": "c1.interpret", "grade": "fuzzy"})
    assert r.status_code == 200, r.text
    assert ticks == ["u1", "u1", "u1"]
    # a refused request schedules nothing
    assert client.post(f"/wiki/pages/{pid}/checks/s4", headers=H("u1"),
                       json={"choice": 0}).status_code == 409
    assert ticks == ["u1", "u1", "u1"]


def test_a_failing_tick_does_not_break_the_request(wiki, services, monkeypatch):
    client, _ = wiki
    pid = organized_page(wiki)

    def boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(RsiCycle, "maybe_run", boom)
    r = client.post(f"/wiki/pages/{pid}/checks/s4", headers=H("u1"), json={"choice": 0})
    assert r.status_code == 200, r.text


def test_during_a_round_only_what_it_changes_waits_for_the_lock(wiki, services, monkeypatch):
    client, _ = wiki
    seed(services, client)

    class Source:
        def __init__(self, prefix, rsi_related):
            self.prefix, self.rsi_related = prefix, rsi_related

        def list(self, user_id):
            return []

        def undo(self, user_id, change_id, *, now):
            return ChangeOut(patch_id=change_id, track="G", status="rolled_back", summary="", why="",
                             signals=(), at=now, can_undo=False, target="", kind="preference")

    monkeypatch.setattr(services, "change_sources", (Source("act_", False), Source("h_", True)))
    with cycle_lock(services, "u1") as got:
        assert got
        assert client.post("/wiki/rsi/patches/act_1/undo", headers=H("u1")).status_code == 200
        assert client.post("/wiki/rsi/patches/h_1/undo", headers=H("u1")).status_code == 409
    assert client.post("/wiki/rsi/patches/h_1/undo", headers=H("u1")).status_code == 200


def test_one_cycle_at_a_time_per_user(wiki, services):
    client, s = wiki
    seed(services, client)
    signup(client, "u2")
    store = PgRsiStore(services.pool)
    store.put_settings("u1", RsiSettings(cycle="weekly"))
    with cycle_lock(services, "u1") as got:              # a cycle "in flight" for u1
        assert got
        with cycle_lock(services, "u1") as again:
            assert not again
        rsi_tick(services, "u1")                          # due, but busy: skipped
        r = client.post("/wiki/rsi/run", headers=H("u1"))
        assert r.status_code == 409 and r.json() == {"detail": BUSY_DETAIL, "code": "rsi_busy", "params": {}}
        assert client.post("/wiki/rsi/patches/p/undo", headers=H("u1")).status_code == 409
        assert run(client, "u2")["a"] == "too_few_outcomes"   # other users are not blocked
        assert store.cycles("u1") == []
        assert s.calls() == 0
    rsi_tick(services, "u1")                              # released: the due cycle runs once
    (c,) = store.cycles("u1")
    assert c.trigger == "weekly"                          # the default (10 signals, first run)
    rsi_tick(services, "u1")                              # no new signal: not due
    assert len(store.cycles("u1")) == 1
    with cycle_lock(services, "u1") as got:              # the lock was released after the tick
        assert got


def test_tick_rechecks_due_under_the_lock(client, services, monkeypatch):
    """The cheap pre-check may be stale by the time the lock is held: maybe_run re-reads `due`."""
    seed(services, client)
    store = PgRsiStore(services.pool)
    store.put_settings("u1", RsiSettings(cycle="weekly"))
    rsi_tick(services, "u1")
    assert len(store.cycles("u1")) == 1
    monkeypatch.setattr(rsi_runs, "_due_now", lambda svc, uid: True)   # a stale "due"
    rsi_tick(services, "u1")
    assert len(store.cycles("u1")) == 1


def test_due_now_with_the_harness_counts_assistant_feedback(client, services, monkeypatch):
    """With the harness on, assistant rejections and 👎 make the tick due like page signals do
    (as the round's own `_due_count`); with it off they do not count."""
    from evowiki.harness import round as hround
    signup(client, "u1")
    store = PgRsiStore(services.pool)
    seen = []
    monkeypatch.setattr(hround, "assistant_feedback_count",
                        lambda sessions, ledger, uid, since: seen.append(uid) or 11)
    store.put_settings("u1", RsiSettings(harness=True, cycle="weekly"))
    assert rsi_runs._due_now(services, "u1") is True and seen == ["u1"]
    store.put_settings("u1", RsiSettings())
    assert rsi_runs._due_now(services, "u1") is False and seen == ["u1"]
