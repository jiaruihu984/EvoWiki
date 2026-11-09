"""Switching an existing forged tool back on (harness 3b follow-up 1): a tool already in the user's
forge registry, switched off in the pack, is switched on by its H3 opt-in alone (no H12.tool op,
no promote, no new registry version). `diff.refuse` takes the registry's deep tool names
(`registered`); the improver passes the registry's names minus this run's builds, the round reads
them again at apply / approve time (no hooks, the switch off or the tool gone: refused, an approval
is PendingStale). The replay needs no overlay: the candidate pack's H3 value reaches
`forge.optin.opted_in`. Real forge registry rows on the test DB; fake replays and judge, a scripted
forge; nothing reaches a real provider."""

import re
from dataclasses import dataclass, field, replace
from datetime import timedelta

import psycopg
import pytest

from evowiki.forge import optin, registry, switch
from evowiki.harness import forge_entries
from evowiki.harness import round as round_mod
from evowiki.harness.change_source import HarnessChanges, candidate_ids, load_titles, summary
from evowiki.harness.diff import KeyOp, PackDiff, diff_to_json, refuse
from evowiki.harness.forging import harness_promotes
from evowiki.harness.improver import Composition, ImproverRun
from evowiki.harness.pack import EMPTY, HarnessPack
from evowiki.harness.replay import ReplayDeps, replay
from evowiki.harness.round import HarnessRounds
from evowiki.harness.tool_labels import FORGED_TOOL
from evowiki.store import forge as fstore
from evowiki.store import harness as pack_store
from evowiki.teach.approval import PendingStale
from evowiki.teach.contracts import TeachingSpec
from tests.harness.test_forging import CID, FORGE_ARGS, FakeForge, insert_candidate
from tests.harness.test_round import NOW, Env, U, case
from tests.harness.test_round_forge import ASK, gates
from tests.teach.fake_approvals import Effect, FakeApprovals

ON = {"name": "H3.deep.t1", "value": True}
TITLE = "等价检查"                      # insert_candidate's spec title


@pytest.fixture(autouse=True)
def _no_forge_env(monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE", raising=False)


MORE = re.compile(r"\n（还有 \d+ 字，用 offset=(\d+) 接着读）$")   # read_pack's paging line


@dataclass
class Composing:
    """Stands in for `run_improver`: an `ImproverRun` on the round's deps reads its H3 rows and
    composes `ops` (its `compose`: the forged-name check and `refuse` with the registry's names);
    `forge` first builds a candidate with `forge_tool`; `after(pool)` runs before the apply."""

    ops: list
    after: object = None
    forge: bool = False
    pack_rows: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def __call__(self, user_id, cases, *, deps):
        run = ImproverRun(user_id, cases, deps)
        if self.forge:
            run.forge_tool(FORGE_ARGS)
        out, offset = "", 0
        while True:                     # the whole H3 listing, page by page (CLIP per read)
            part = run.read_pack({"dim": "H3", "offset": offset})
            m = MORE.search(part)
            if m is None:
                break
            out, offset = out + part[:m.start()], int(m.group(1))
        self.pack_rows.append(out + part)
        try:
            run.compose({"decision": "merge", "ops": self.ops, "spec_ops": [],
                         "citations": ["signal:sig1"], "rationale": "这个工具其实有用"})
        except Exception as e:  # noqa: BLE001 - the tool error the model would see
            self.errors.append(str(e))
            return Composition(candidates=(), chosen=None, decision="none", citations=(),
                               rationale="不改", probes=(), comparisons=(),
                               run_id=f"harness:{deps.round_id}:improver", cost=0.0)
        if self.after is not None:
            self.after(deps.pool)
        decision, chosen, cites, why = run.composition
        return Composition(candidates=(), chosen=chosen, decision=decision, citations=cites,
                           rationale=why, probes=(), comparisons=(),
                           run_id=f"harness:{deps.round_id}:improver", cost=0.0)


def setup(pool, ops, *, forge=None, after=None, compose_forge=False, **kw):
    """The user's registry has t1 (CID, version 1), switched off in the pack by an earlier change."""
    forge = forge or FakeForge(pool)
    env = Env(pool, improver=Composing(ops, after, compose_forge), forge=forge.hooks(pool), **kw)
    with pool.connection() as conn:
        insert_candidate(conn, CID, name="t1")
        registry.promote(conn, CID, gate_ref="hg_x", by="user", why="好", gate_ok=lambda r: True,
                         now=NOW - timedelta(days=1))
        pack_store.set_value(conn, U, "H3.deep.t1", False, source="improver", why="没用",
                             now=NOW - timedelta(hours=2))
    return env, forge


def head(pool):
    with pool.connection() as conn:
        return fstore.registry_head(conn, U)


def switch_off(pool):
    with pool.connection() as conn:
        switch.set_enabled(conn, U, "tool", False, NOW)


def unregister(pool):
    with pool.connection() as conn:
        registry.rollback(conn, U, registry.change_id(U, 1), by="user", now=NOW)


# ---- refuse ------------------------------------------------------------------------------------

D: dict = {}


def test_refuse_allows_a_registered_tool_alone_and_refuses_an_unregistered_one():
    on = PackDiff((KeyOp("H3.deep.t1", True),))
    assert refuse(on, EMPTY, {}, defaults=D, registered=("t1",)) is None
    for reg in ((), ("t2",)):
        assert refuse(on, EMPTY, {}, defaults=D, registered=reg) == \
            "H3.deep.t1 要和它的 H12 候选一起改"
    assert refuse(on, EMPTY, {}, defaults=D) is not None               # default: nothing registered
    # an H12 op for a registered name is still a new candidate replacing it, as before
    pair = PackDiff((KeyOp("H3.deep.t1", True), KeyOp("H12.tool.deep.t1", {"candidate_id": "fc_n"})))
    assert refuse(pair, EMPTY, {}, defaults=D, registered=("t1",)) is None
    bad = PackDiff((KeyOp("H12.tool.deep.t1", {"candidate_id": "fc_n", "registry_version": 3}),))
    assert refuse(bad, EMPTY, {}, defaults=D, registered=("t1",)) is not None
    # switching it off needs no registry (as before)
    assert refuse(PackDiff((KeyOp("H3.deep.t1", False),)), EMPTY, {}, defaults=D) is None
    # a user-set opt-in stays the user's
    assert refuse(on, HarnessPack(1, {"H3.deep.t1": False}), {"H3.deep.t1": "user"}, defaults=D,
                  registered=("t1",)) is not None


# ---- the round ---------------------------------------------------------------------------------

def test_a_registered_tool_switched_back_on_applies_without_a_promote(pool, monkeypatch):
    env, forge = setup(pool, [ON])
    promoted = []
    monkeypatch.setattr(round_mod, "promote_for", lambda *a, **k: promoted.append(a))
    monkeypatch.setattr(registry, "promote", lambda *a, **k: promoted.append(a))
    r = env.run()
    assert r.status == "applied", r.reason
    assert env.improver.errors == []
    # the improver saw the tool and that it is off now
    rows = env.improver.pack_rows[0]
    assert "- H3.deep.t1｜" in rows and "实际：关着（已上线）" in rows
    assert promoted == [] and forge.proposed == []
    assert env.pack().values["H3.deep.t1"] is True
    assert "H12.tool.deep.t1" not in env.pack().values
    assert head(pool) == (1, {"t1": CID})                       # no new registry version
    with pool.connection() as conn:
        assert fstore.get_candidate(conn, CID).status == "promoted"
        assert harness_promotes(conn, U) == frozenset()
    (g,) = gates(pool)
    assert (g["subject"], g["verdict"]) == ("diff", "pass")
    (ch,) = env.changes()
    assert ch["diff"]["ops"] == [{"name": "H3.deep.t1", "value": True, "before": False}]


def test_the_change_line_names_the_title_and_undo_restores_the_key(pool):
    env, _ = setup(pool, [ON])
    r = env.run()
    assert r.status == "applied", r.reason
    changes = HarnessChanges(pool, env.registry)
    (row,) = changes.list(U)
    assert row.summary == f"写页面时开始用{TITLE}"
    assert "t1" not in row.summary and CID not in row.summary
    out = changes.undo(U, "h_" + r.change_id, now=NOW + timedelta(hours=1))
    assert out.status == "rolled_back"
    assert env.pack().values["H3.deep.t1"] is False             # back to what it was
    assert head(pool) == (1, {"t1": CID})                       # the registry is not touched


def test_the_change_line_without_the_tool_in_the_registry_says_a_forged_tool(pool):
    stored = diff_to_json(PackDiff((KeyOp("H3.deep.t1", True),)), {"H3.deep.t1": False})
    setup(pool, [ON])
    with pool.connection() as conn:
        ids = candidate_ids([stored])
        assert load_titles(conn, ids, U) == {"forged:deep:t1": TITLE}
        assert load_titles(conn, ids) == {}                    # no user: no registry lookup
    unregister(pool)
    with pool.connection() as conn:
        titles = load_titles(conn, ids, U)
    assert titles == {}
    assert summary(stored, None, None, titles) == f"写页面时开始用{FORGED_TOOL}"


@pytest.mark.parametrize("gone", [switch_off, unregister])
def test_the_tool_off_or_gone_at_apply_time_is_refused(pool, gone):
    env, _ = setup(pool, [ON], after=gone)
    r = env.run()
    assert (r.status, r.reason) == ("failed", round_mod.TOOL_GONE)
    assert env.pack().values["H3.deep.t1"] is False and env.changes() == []


def test_an_approved_switch_back_on_applies_without_a_promote(pool):
    env, _ = setup(pool, [ON, ASK], approvals=FakeApprovals(ask=False))
    r = env.run()
    assert r.status == "asked", r.reason
    effect = Effect()
    HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=effect)
    assert effect.n == 1
    values = env.pack().values
    assert values["H3.deep.t1"] is True and "H12.tool.deep.t1" not in values
    assert head(pool) == (1, {"t1": CID})


@pytest.mark.parametrize("gone", ["switch", "registry", "hooks"])
def test_an_approval_after_the_tool_went_off_or_away_is_stale(pool, gone):
    env, _ = setup(pool, [ON, ASK], approvals=FakeApprovals(ask=False))
    r = env.run()
    assert r.status == "asked", r.reason
    deps = env.deps
    if gone == "switch":
        switch_off(pool)
    elif gone == "registry":
        unregister(pool)
    else:
        deps = replace(deps, forge=None)
    effect = Effect()
    with pytest.raises(PendingStale, match=round_mod.TOOL_GONE):
        HarnessRounds(deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=effect)
    assert effect.n == 0
    (ch,) = env.changes()
    assert ch["status"] == "rejected" and env.pack().values["H3.deep.t1"] is False


# ---- the improver ------------------------------------------------------------------------------

def test_an_unregistered_tool_alone_is_refused_by_the_improver(pool):
    env, _ = setup(pool, [{"name": "H3.deep.t2", "value": True}])
    r = env.run()
    assert r.status == "no_change"
    assert "没有这个自己造的工具" in env.improver.errors[0]


def test_a_tool_listed_without_the_registry_is_refused_by_the_improver(pool):
    """deps.forged names a tool, but there are no forge hooks: it is not registered."""
    env, _ = setup(pool, [ON])
    imp = env.improver

    def improve(user_id, cases, *, deps):
        return imp(user_id, cases, deps=replace(deps, forge=None, forged={"deep": ("t1",)}))
    env.deps = replace(env.deps, improve=improve)
    r = env.run()
    assert r.status == "no_change"
    assert "要和它的 H12 候选一起改" in imp.errors[0]


def test_the_switch_off_at_improver_time_refuses_the_tool(pool):
    env, _ = setup(pool, [ON])
    switch_off(pool)
    r = env.run()
    assert r.status == "no_change"
    assert "没有这个自己造的工具" in env.improver.errors[0]


def test_a_name_built_this_run_needs_its_h12_op(pool):
    """A replacement built in this run is not in the registry yet: its opt-in alone is refused."""
    env, _ = setup(pool, [ON], forge=FakeForge(pool, cid="fc_t1b"), compose_forge=True)
    r = env.run()
    assert r.status == "no_change"
    assert "要和它的 H12 候选一起改" in env.improver.errors[0]
    assert "这一轮新造的版本" in env.improver.pack_rows[0]


# ---- the replay --------------------------------------------------------------------------------

@dataclass
class _Deep:
    repo: object = None
    seen: list = field(default_factory=list)

    def replay_section(self, user_id, **kw):
        self.seen.append(kw)
        from types import SimpleNamespace
        return SimpleNamespace(body="正文", cue=None, writer_provider="anthropic", run_id="r",
                               cost=0.0)


def test_the_replay_offers_the_registered_tool_from_the_candidate_pack(pool):
    setup(pool, [ON])
    deep = _Deep()
    base = HarnessPack(3, {"H3.deep.t1": False})
    replay(case(1), PackDiff((KeyOp("H3.deep.t1", True),)), base_pack=base,
           base_spec=TeachingSpec(version=1, items=()),
           deps=ReplayDeps(deep=deep, repo=None))  # type: ignore[arg-type]
    (kw,) = deep.seen
    assert "forge_candidate" not in kw                          # no overlay
    pack = kw["harness"](U)
    with pool.connection() as conn:
        assert optin.opted_in(conn, U, "deep", ("t1",), pack.values) == {"t1"}
        assert optin.opted_in(conn, U, "deep", ("t1",), base.values) == frozenset()


# ---- one registry entry at a time (MF1) / titles (MF2) -----------------------------------------

def corrupt(pool, cid=CID):
    """The candidate's files lose tool.py and schema.json: its registry entry no longer loads."""
    with pool.connection() as conn:
        conn.execute("update forge_candidate set files = '{}'::jsonb where id = %s", (cid,))


def add_tool(pool, cid, name, *, user=U, title=None):
    with pool.connection() as conn:
        insert_candidate(conn, cid, name=name, user=user)
        if title is not None:
            conn.execute("update forge_candidate set spec = spec || %s::jsonb where id = %s",
                         (f'{{"title": "{title}"}}', cid))
        registry.promote(conn, cid, gate_ref="hg_y", by="user", why="好",
                         gate_ok=lambda r: True, now=NOW - timedelta(hours=20))


def test_a_corrupt_unrelated_entry_does_not_stop_switching_another_tool_back_on(pool):
    env, _ = setup(pool, [ON])
    add_tool(pool, "fc_t9", "t9")
    corrupt(pool, "fc_t9")
    r = env.run()
    assert r.status == "applied", r.reason
    assert env.improver.errors == []
    assert env.pack().values["H3.deep.t1"] is True


def test_a_corrupt_entry_of_the_touched_tool_is_refused_with_a_plain_reason(pool):
    env, _ = setup(pool, [ON])
    corrupt(pool)
    r = env.run()
    assert r.status == "no_change"
    assert forge_entries.BROKEN in env.improver.errors[0]
    assert env.pack().values["H3.deep.t1"] is False


def test_an_approval_whose_tool_entry_broke_is_stale_with_the_reason(pool, caplog):
    env, _ = setup(pool, [ON, ASK], approvals=FakeApprovals(ask=False))
    add_tool(pool, "fc_t9", "t9")
    r = env.run()
    assert r.status == "asked", r.reason
    corrupt(pool, "fc_t9")                      # an unrelated entry: no effect on the approval
    effect = Effect()
    with caplog.at_level("WARNING", logger="evowiki.harness.forge_entries"):
        corrupt(pool)
        with pytest.raises(PendingStale, match=forge_entries.BROKEN):
            HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=effect)
    assert "'t1'" in caplog.text
    assert effect.n == 0
    (ch,) = env.changes()
    assert ch["status"] == "rejected" and env.pack().values["H3.deep.t1"] is False


def test_an_unrelated_corrupt_entry_leaves_an_approval_applicable(pool):
    env, _ = setup(pool, [ON, ASK], approvals=FakeApprovals(ask=False))
    add_tool(pool, "fc_t9", "t9")
    r = env.run()
    assert r.status == "asked", r.reason
    corrupt(pool, "fc_t9")
    effect = Effect()
    HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=effect)
    assert effect.n == 1 and env.pack().values["H3.deep.t1"] is True


def test_a_failed_registry_read_fails_every_approval(pool, monkeypatch):
    env, _ = setup(pool, [ON, ASK], approvals=FakeApprovals(ask=False))
    r = env.run()
    assert r.status == "asked", r.reason

    def down(conn, scope):
        raise psycopg.OperationalError("the forge registry cannot be read")
    monkeypatch.setattr(fstore, "registry_head", down)
    effect = Effect()
    with pytest.raises(psycopg.OperationalError):
        HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=effect)
    assert effect.n == 0
    (ch,) = env.changes()
    assert ch["status"] == "asked" and env.pack().values["H3.deep.t1"] is False


def test_titles_respect_the_users_masks(pool):
    setup(pool, [ON])
    with pool.connection() as conn:
        insert_candidate(conn, "fc_g1", name="g1", user=None)
        registry.promote(conn, "fc_g1", gate_ref="hg_g", by="operator", why="好",
                         gate_ok=lambda r: True, now=NOW - timedelta(hours=20))
        ref = {"forged:deep:g1"}
        assert load_titles(conn, ref, U) == {"forged:deep:g1": TITLE}
        fstore.set_setting(conn, U, registry.MASK + "g1", "fc_g1", NOW)
        assert load_titles(conn, ref, U) == {}


def test_the_before_note_names_the_forged_tool_by_its_title(pool):
    env, _ = setup(pool, [ON])
    r = env.run()
    assert r.status == "applied", r.reason
    (row,) = HarnessChanges(pool, env.registry).list(U)
    assert f"改之前：写页面时没用{TITLE}" in row.why
    assert FORGED_TOOL not in row.why
