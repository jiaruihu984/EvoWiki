"""A forged tool through a harness round (harness 3b, Task 7): the improver forges a tool (the real
`forge_tool` on an `ImproverRun`, the forge's `propose` faked with a real built candidate row) and
composes its H3 opt-in + H12.tool record; the round's final probe and gate decide (fake replays,
the round's fake judge); the gate row is written before anything applies; a pass promotes the
candidate in the real forge registry inside the pack transaction (auto) or on approval (with an
ask key). Uses the round fakes of tests/harness/test_round.py; nothing reaches a real provider."""

from dataclasses import dataclass, field

import pytest

from evowiki.harness import round as round_mod
from evowiki.harness.improver import Composition, ImproverRun
from evowiki.harness.round import HarnessRounds
from evowiki.store import forge as fstore
from evowiki.store import harness_rsi
from evowiki.teach.approval import PendingStale
from tests.harness.test_forging import CID, FORGE_ARGS, FakeForge
from tests.harness.test_round import NOW, Env, FakeReplay, U
from tests.teach.fake_approvals import Effect, FakeApprovals

PAIR = [{"name": "H3.deep.t1", "value": True},
        {"name": "H12.tool.deep.t1", "value": {"candidate_id": CID}}]
ASK = {"name": "H3.deep.read_page", "value": False}


@pytest.fixture(autouse=True)
def _scripted_forge_env(monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE", raising=False)
    # RSI forge calls otherwise select the runtime improver instead of this scripted gateway.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@dataclass
class ForgingImprover:
    """Stands in for `run_improver`: an `ImproverRun` on the round's deps forges the tool (its
    `forge_tool`) and composes `ops` (its `compose`, with the forged-name check); `after(pool)`
    runs between the compose and the round's apply."""

    ops: list
    after: object = None
    seen: list = field(default_factory=list)
    outputs: list = field(default_factory=list)

    def __call__(self, user_id, cases, *, deps):
        self.seen.append(deps)
        run = ImproverRun(user_id, cases, deps)
        self.outputs.append(run.forge_tool(FORGE_ARGS))
        assert run.compose({"decision": "merge", "ops": self.ops, "spec_ops": [],
                            "citations": ["signal:sig1"], "rationale": "用新工具逐项检查等价"}) \
            == "已交卷。"
        if self.after is not None:
            self.after(deps.pool)
        decision, chosen, cites, why = run.composition
        return Composition(candidates=(), chosen=chosen, decision=decision, citations=cites,
                           rationale=why, probes=(), comparisons=(),
                           run_id=f"harness:{deps.round_id}:improver", cost=run.cost([]))


def env_with(pool, ops, *, forge=None, after=None, **kw):
    forge = forge or FakeForge(pool, converse=True)
    env = Env(pool, improver=ForgingImprover(ops, after), forge=forge.hooks(pool), **kw)
    return env, forge


def _cand(pool):
    with pool.connection() as conn:
        return fstore.get_candidate(conn, CID)


def gates(pool):
    with pool.connection() as conn:
        return conn.execute("select * from harness_gate where user_id = %s order by at",
                            (U,)).fetchall()


def _round_decision(env):
    (row,) = env.rounds()
    return row.decision


def test_forged_tool_auto_promoted(pool):
    env, forge = env_with(pool, PAIR)
    r = env.run()
    assert r.status == "applied", r.reason
    assert "要用它" in env.improver.outputs[0]
    assert _cand(pool).status == "promoted"
    (g,) = gates(pool)
    assert (g["verdict"], g["subject"], g["round_id"]) == ("pass", f"tool:{CID}", r.round_id)
    assert _round_decision(env)["gate"]["ref"] == g["gate_id"]
    value = env.pack().values["H12.tool.deep.t1"]
    assert value == {"candidate_id": CID, "registry_version": 1}
    assert env.pack().values["H3.deep.t1"] is True
    (ch,) = env.changes()
    stored = next(o for o in ch["diff"]["ops"] if o["name"] == "H12.tool.deep.t1")
    assert stored["value"] == value and stored["before"] is None
    assert _round_decision(env) is not None
    (row,) = env.rounds()
    assert next(o for o in row.diff["ops"] if o["name"] == "H12.tool.deep.t1")["value"] == value
    with pool.connection() as conn:
        change = fstore.registry_history(conn, U)[0].change
    assert change["by"] == f"harness:{r.round_id}" and change["gate_ref"] == g["gate_id"]
    # the forge's calls ran on the round's recording gateway (their cost is the round's), on the
    # good tier, account group rsi, with the round's base pack
    gw, account, pack = forge.bound[0]
    assert isinstance(gw._log, round_mod._RecordingLog) and gw._log.made
    assert (account.group, account.tier_pin) == ("rsi", "good") and pack.version == 0


def test_forged_tool_with_ask_key_waits(pool):
    ap = FakeApprovals(ask=False)
    env, _ = env_with(pool, [*PAIR, ASK], approvals=ap)
    r = env.run()
    assert r.status == "asked"
    assert _cand(pool).status == "built" and env.pack().values == {}
    (g,) = gates(pool)
    assert g["verdict"] == "pass" and g["subject"] == f"tool:{CID}"
    (p,) = ap.proposals
    assert any("t1" not in str(ln) and "新增了工具「" in str(ln) for ln in p["content"]["diff"]), p["content"]["diff"]
    effect = Effect()
    HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=effect)
    assert effect.n == 1 and _cand(pool).status == "promoted"
    values = env.pack().values
    assert values["H12.tool.deep.t1"] == {"candidate_id": CID, "registry_version": 1}
    assert values["H3.deep.read_page"] is False
    (ch,) = env.changes()
    assert ch["status"] == "applied"          # its stored diff stays as asked (immutable); undo
                                              # reads the registry version from the pack


def test_an_approved_tool_no_longer_built_is_stale(pool):
    env, _ = env_with(pool, [*PAIR, ASK], approvals=FakeApprovals(ask=False))
    r = env.run()
    with pool.connection() as conn:
        fstore.set_status(conn, CID, "rolled_back", NOW)
    with pytest.raises(PendingStale, match=round_mod.TOOL_GONE):
        HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=Effect())
    (ch,) = env.changes()
    assert ch["status"] == "rejected" and env.pack().values == {}
    with pool.connection() as conn:
        assert fstore.registry_head(conn, U)[0] == 0


def _switch_off(p):
    from evowiki.forge import switch
    with p.connection() as conn:
        switch.set_enabled(conn, U, "tool", False, NOW)


def test_the_tool_switch_turned_off_before_the_apply_fails_closed(pool):
    env, _ = env_with(pool, PAIR, after=_switch_off)
    r = env.run()
    assert (r.status, r.reason) == ("failed", round_mod.TOOL_NOT_PROMOTED)
    assert _cand(pool).status == "built" and env.pack().values == {} and env.changes() == []
    with pool.connection() as conn:
        assert fstore.registry_head(conn, U)[0] == 0


def test_an_approval_after_the_tool_switch_went_off_is_stale(pool):
    env, _ = env_with(pool, [*PAIR, ASK], approvals=FakeApprovals(ask=False))
    r = env.run()
    assert r.status == "asked"
    _switch_off(pool)
    effect = Effect()
    with pytest.raises(PendingStale, match=round_mod.TOOL_GONE):
        HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=effect)
    assert effect.n == 0 and _cand(pool).status == "built"
    (ch,) = env.changes()
    assert ch["status"] == "rejected" and env.pack().values == {}
    with pool.connection() as conn:
        assert fstore.registry_head(conn, U)[0] == 0


def test_an_approval_without_forge_hooks_is_stale(pool):
    from dataclasses import replace
    env, _ = env_with(pool, [*PAIR, ASK], approvals=FakeApprovals(ask=False))
    r = env.run()
    with pytest.raises(PendingStale, match=round_mod.TOOL_GONE):
        HarnessRounds(replace(env.deps, forge=None)).approve(U, "h_" + r.change_id, now=NOW,
                                                             on_accept=Effect())
    assert _cand(pool).status == "built" and env.pack().values == {}


def test_gate_fail_never_promotes(pool):
    env, _ = env_with(pool, PAIR, replay=FakeReplay(broken={"case:run-1:s2"}))
    r = env.run()
    assert r.status == "failed"
    (g,) = gates(pool)
    assert (g["verdict"], g["subject"]) == ("fail", f"tool:{CID}")
    assert _cand(pool).status == "built" and env.pack().values == {} and env.changes() == []


def test_a_candidate_gone_before_the_apply_fails_closed(pool):
    def gone(p):
        with p.connection() as conn:
            fstore.set_status(conn, CID, "build_failed", NOW)
    env, _ = env_with(pool, PAIR, after=gone)
    r = env.run()
    assert (r.status, r.reason) == ("failed", round_mod.TOOL_NOT_PROMOTED)
    assert env.pack().values == {} and env.changes() == []
    with pool.connection() as conn:
        assert fstore.registry_head(conn, U)[0] == 0
    (g,) = gates(pool)
    assert g["verdict"] == "pass"                               # the gate row stays (append-only)


def test_a_diff_without_a_tool_records_a_diff_gate(pool):
    from tests.harness.test_round import H4, FakeImprover
    env = Env(pool, improver=FakeImprover(H4))
    r = env.run()
    assert r.status == "applied"
    (g,) = gates(pool)
    assert (g["subject"], g["verdict"]) == ("diff", "pass")
    assert _round_decision(env)["gate"]["ref"] == g["gate_id"]
    with pool.connection() as conn:
        assert harness_rsi.get_gate(conn, U, g["gate_id"]).verdict == "pass"


def test_the_forge_tier_deep_uses_the_deep_writer(pool):
    env, forge = env_with(pool, PAIR, forge=FakeForge(pool))
    with pool.connection() as conn:
        from evowiki.store import harness as pack_store
        pack_store.set_value(conn, U, "H10.forge.code.tier", "deep", source="user", why="试",
                             now=NOW)
    r = env.run()
    assert r.status == "applied", r.reason
    gw, account, _ = forge.bound[0]
    assert account.tier_pin is None and account.group == "rsi"
    assert isinstance(gw._log, round_mod._RecordingLog)
