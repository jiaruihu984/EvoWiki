"""Task 10 step 6: a backfilled preference / fact goes to S3's `land_preference` (its planned
signature, faked here until S3's T6 lands) and comes back pending; a retry with the same
candidate key finds the same draft; the rule guard (`check_auto_rule`, actor system) keeps a rule
he rejected from coming back reworded; a pending rule is in no prompt until he confirms it.
In-memory wiki, fake land_preference and fake judges: no model call."""

from dataclasses import dataclass
from datetime import timedelta

import pytest

from evowiki.memory.consolidate import Candidate, retry_pending_landings
from evowiki.memory.contracts import EvidenceRef, MemoryScope
from evowiki.memory.lander import (
    AutoRuleGuard,
    Deferred,
    Landed,
    NullPreferenceLander,
    Refused,
    S3PreferenceLander,
)
from evowiki.profile.derive import derived
from evowiki.profile.note import HOW
from evowiki.teach.rule_judge import Verdict
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.teach import commit_spec, remove_item_flow, take_over_flow
from tests.memory.test_consolidate import DECISION, PREF, World, _session, cand, ev
from tests.wikiapp.test_goal import NOW, Ids

U = "u1"
IDS = Ids()             # one id source: every write of a test gets fresh ids
GLOBAL = MemoryScope(scope_type="global")


@dataclass(frozen=True)
class FakeLanded:
    status: str
    item_id: str | None = None
    pending_action_id: str | None = None
    reason: str | None = None


class FakeLand:
    """S3's land_preference as its plan promises it: explicit_history → a pending draft; the same
    idempotency_key again → that draft, no new one; `refuse` → refused."""

    def __init__(self, *, refuse: str | None = None, active: bool = False, boom: bool = False):
        self.refuse, self.active, self.boom = refuse, active, boom
        self.calls: list[dict] = []
        self.drafts: dict[str, str] = {}

    def __call__(self, repo, snap, **kw):
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("database down")
        if self.refuse:
            return FakeLanded("refused", reason=self.refuse)
        if self.active:
            return FakeLanded("active", item_id="it1")
        key = kw["idempotency_key"]
        pid = self.drafts.setdefault(key, f"act{len(self.drafts) + 1}")
        return FakeLanded("pending", pending_action_id=pid)


def _lander(land, repo=None):
    return S3PreferenceLander(land, repo or MemoryRepo([U]), lambda: NOW, IDS)


def _land_one(land, c, repo=None):
    return _lander(land, repo).land(U, c, session_id="s1", idempotency_key="k")


@pytest.fixture(autouse=True)
def _any_quote_backs(monkeypatch):
    """These are about landing: the candidates quote one fixture message, whatever they say."""
    monkeypatch.setattr("evowiki.memory.consolidate.BACKED_CHARS", 0.0)


def test_lander_calls_s3_with_the_planned_signature(tmp_path):
    w, land = World(tmp_path), FakeLand()
    fact = cand("about_me", "我是高二学生", ev("m4", quote="为什么除以标准差我还没懂"))
    r = w.run([PREF, fact], lander=_lander(land))
    pref_kw, fact_kw = land.calls
    assert (pref_kw["kind"], fact_kw["kind"]) == ("teaching", "about_me")
    assert pref_kw["provenance"] == "explicit_history" and pref_kw["session_id"] is None
    assert pref_kw["scope"] == GLOBAL and pref_kw["text"] == "数学题先解释符号"
    assert pref_kw["evidence"] == (EvidenceRef(message_id="m3", session_id="s1", role="user",
                                               quote="以后数学题都先解释符号"),)
    assert pref_kw["idempotency_key"] != fact_kw["idempotency_key"]
    by_kind = {i.kind: i for i in r.items_written}
    assert by_kind["preference"].pending_action_id == "act1"
    assert by_kind["about_me"].pending_action_id == "act2"
    assert all(i.status is None and i.spec_item_id is None for i in r.items_written)


def test_lander_maps_refused_active_and_failures():
    c = Candidate.model_validate(PREF)
    f = Candidate.model_validate(cand("about_me", "我是高二学生", ev("m3", quote="x")))
    assert _land_one(FakeLand(refuse="已经有相近的原则了"), c) == Refused("已经有相近的原则了")
    assert _land_one(FakeLand(active=True), c) == Landed(spec_item_id="it1")
    assert _land_one(FakeLand(active=True), f) == Landed(profile_ref={"key": "it1"})
    assert _land_one(FakeLand(boom=True), c) == Deferred("land_failed")
    assert _land_one(FakeLand(), c, MemoryRepo([])) == Deferred("land_failed")     # no wiki


def test_refused_at_consolidation_is_dropped(tmp_path):
    r = World(tmp_path).run([PREF], lander=_lander(FakeLand(refuse="dup")))
    assert [why for _, why in r.dropped] == ["refused:dup"] and r.items_written == []


def test_backlog_lands_at_once_and_retries_keep_one_pending(tmp_path):
    w = World(tmp_path)
    _session(w.sessions, "s2", "u1", [("n1", "以后例子都用生活里的", "b1", "好")],
             at=NOW + timedelta(days=1))
    w.run([PREF], lander=NullPreferenceLander())
    w.run([cand("preference", "例子都用生活里的", ev("n1", quote="以后例子都用生活里的"))],
          lander=NullPreferenceLander(), sid="s2", through=1)
    land = FakeLand()
    lander = _lander(land)
    assert retry_pending_landings(w.mem, w.sessions, lander, U, NOW, IDS) == 2
    for _ in range(3):
        assert retry_pending_landings(w.mem, w.sessions, lander, U, NOW, IDS) == 0
    assert len(land.drafts) == 2 and len(land.calls) == 2
    assert sorted(i.pending_action_id for i in w.mem.items(U, kinds=["preference"])) == [
        "act1", "act2"]


def test_retry_after_a_crash_between_landing_and_commit_finds_the_same_draft(tmp_path):
    w, land = World(tmp_path), FakeLand()
    w.run([PREF], lander=NullPreferenceLander())
    key = w.entries()[0]["pending_landing"][0]["candidate_key"]
    land(None, None, idempotency_key=key)                   # landed, nothing recorded yet
    assert retry_pending_landings(w.mem, w.sessions, _lander(land), U, NOW, IDS) == 1
    [it] = w.mem.items(U, kinds=["preference"])
    assert it.pending_action_id == "act1" and len(land.drafts) == 1


# ---- a pending rule is in no prompt (rule contract: authority pending is not in force) ----

def pending_rule(repo, text, item_id):
    """What a backfill puts in the spec: origin memory_backfill, authority pending, actor system."""
    commit_spec(repo, repo.load(U), lambda s: tspec.write(s, tspec.AddRule(
                    item_id, text, origin="memory_backfill", authority="pending",
                    origin_ref=f"memory:{item_id}"), actor="system"),
                actor="system", action="guideline", now=NOW, new_id=IDS)


def _in_prompts(repo, text) -> dict[str, bool]:
    snap = repo.load(U)
    spec = tspec.effective_spec(snap.state)
    return {"page": any(i.text == text for i in tspec.applicable(spec, "concept", None)),
            "wiki": text in tspec.wiki_texts(snap.state),
            "profile": text in derived(snap, NOW, {}).get(HOW, ())}


def test_a_pending_rule_is_in_no_prompt_until_confirmed():
    repo = MemoryRepo([U])
    pending_rule(repo, "数学题先解释符号", "mb1")
    assert _in_prompts(repo, "数学题先解释符号") == {"page": False, "wiki": False,
                                                    "profile": False}
    take_over_flow(repo, U, "mb1", now=NOW, new_id=IDS)            # he confirms (actor user)
    # in force now; 怎么学比较好 lists only user_ui / user_chat / import rules (contract §5)
    assert _in_prompts(repo, "数学题先解释符号") == {"page": True, "wiki": True, "profile": False}


# ---- the rule guard ----

class FakeJudge:
    def __init__(self, verdict):
        self.verdict, self.seen = verdict, []

    def conflict(self, candidate, scope, domain, against):
        self.seen.append(candidate)
        return Verdict(self.verdict, tuple(a.ref for a in against), "fake", "",
                       tuple(a.ref for a in against))


def _rejected_repo():
    repo = MemoryRepo([U])
    pending_rule(repo, "数学题先解释符号", "mb1")
    remove_item_flow(repo, U, "mb1", now=NOW, new_id=IDS)           # he rejects it
    [d] = repo.load(U).state.rule_decisions
    assert d.action == "rejected"
    return repo


@pytest.mark.parametrize("verdict, reason", [("conflict", "blocked_rule"),
                                             ("unsure", "needs_confirm")])
def test_reworded_rejected_rule_is_dropped_by_the_guard(tmp_path, verdict, reason):
    repo, judge, land = _rejected_repo(), FakeJudge(verdict), FakeLand()
    reworded = cand("preference", "做数学题前先讲清楚每个符号是什么意思",
                    ev("m3", quote="以后数学题都先解释符号"))
    r = World(tmp_path).run([reworded], lander=_lander(land, repo),
                            guard=AutoRuleGuard(repo, lambda uid: judge))
    assert [why for _, why in r.dropped] == [reason] and r.items_written == []
    assert judge.seen == ["做数学题前先讲清楚每个符号是什么意思"] and land.calls == []


def test_near_duplicate_of_a_rejected_rule_is_blocked_without_the_judge(tmp_path):
    repo, judge = _rejected_repo(), FakeJudge("no_conflict")
    r = World(tmp_path).run([cand("preference", "数学题先解释符号。",
                                  ev("m3", quote="以后数学题都先解释符号"))],
                            lander=_lander(FakeLand(), repo),
                            guard=AutoRuleGuard(repo, lambda uid: judge))
    assert [why for _, why in r.dropped] == ["blocked_rule"] and judge.seen == []


def test_guard_runs_on_preferences_only_and_passes_without_a_wiki(tmp_path):
    judge = FakeJudge("conflict")
    assert AutoRuleGuard(MemoryRepo([]), lambda uid: judge).check(U, "x", GLOBAL) is None
    blocking = AutoRuleGuard(_rejected_repo(), lambda uid: judge)
    assert len(World(tmp_path).run([DECISION], guard=blocking).items_written) == 1
    assert judge.seen == []


def test_retry_re_checks_the_guard(tmp_path):
    w = World(tmp_path)
    w.run([PREF], lander=NullPreferenceLander())          # waited while nothing was wired
    repo, land = _rejected_repo(), FakeLand()               # he rejected it meanwhile
    guard = AutoRuleGuard(repo, lambda uid: FakeJudge("no_conflict"))
    assert retry_pending_landings(w.mem, w.sessions, _lander(land, repo), U, NOW, IDS,
                                  guard=guard) == 0
    assert w.entries()[-1]["refused"][0][1] == "blocked_rule"
    assert w.mem.items(U, kinds=["preference"]) == [] and land.calls == []
