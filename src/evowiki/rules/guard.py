"""The guard on automatic rule changes (rule provenance contract §2, §4.1, decision 4): one entry
point, `check_auto_rule`, shared by Memory's consolidation, H7 and the H1 / H2 / H8 text edits.
Only automatic changes are checked; a user's own action always passes.

In order:
1. deterministic: a near-duplicate (`teach.textsim.near_duplicate`) of a decision in force (rejected
   / revoked) or of one of the user's own / pending / protected rules, in an overlapping scope →
   Block;
2. a vector ranking of the user's rules and decisions in an overlapping scope keeps the TOP_K most
   similar (a filter only — it never decides: 「每页都要先举例」 and 「必要时先举例」 are close in
   meaning but differ in strength);
3. the judge (`teach.rule_judge`, through the user's model gateway) says conflict → Block,
   no_conflict → Pass, same (a restatement of one of the user's own / pending rules) → Pass with
   `same_as` (Memory lands it as that rule, not a second one; a restated rejection is a Block),
   unsure → NeedsConfirm;
4. no judge, a failed judge call, or anything unsure → NeedsConfirm: the change is not deployed
   automatically, it is asked.

Nothing to compare against → Pass without a call. Each verdict carries `GuardRecord`s (the model,
the rule versions shown, the verdict) for the caller to store with its round / patch.

The harness guard (`harness_guard`, `rsi_rules=True`) also compares with RSI's own rules and the
default rules RSI may tune (authority rsi, not protected; kind "rsi_rule"), leaving out the items
the diff itself edits or removes (`exclude`): a near-duplicate of one → Block (DUP_OWN), the judge's
conflict → Block, its same → Block as a duplicate (RSI-B's review did this before it was retired).
Those default rules tell the page writer how to write a page; the assistant does not write under
them, so an assistant key (H1.assistant, H2.assistant.*) is compared with his own rules and
decisions only. The harness passes only what a text value adds to the text in force
(`harness.leak.added_only`), never the unchanged rest of a prompt.
Memory's consolidation does not: a preference the user stated may well differ from a default rule,
and it lands pending for his confirmation anyway."""

import json
import logging
import math
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from evowiki.llm.run_context import traced_step
from evowiki.retrieval.embed import Embedder
from evowiki.rules.decisions import Decisions, scopes_overlap
from evowiki.teach.contracts import SpecScope, TeachingSpec
from evowiki.teach.rule_judge import Against, GatewayRuleJudge, RuleJudge
from evowiki.teach.textsim import jaccard, near_duplicate

log = logging.getLogger(__name__)

TOP_K = 8
NO_JUDGE = "没有可用的判断模型，要你确认"
JUDGE_FAILED = "判断模型这次没答上来，要你确认"
UNSURE = "拿不准会不会和你的规则冲突，要你确认"
CONFLICT = "和你的规则或你拒绝过的规则冲突"
DUP_DECISION = "和你删掉或拒绝过的规则几乎一样"
DUP_RULE = "和你自己的规则几乎一样"
DUP_OWN = "和已有的讲解规则几乎一样"
CONFLICT_OWN = "和已有的讲解规则冲突"


@dataclass(frozen=True)
class Candidate:
    """A rule text an automatic change would put in force. `label`: where it comes from
    ("add_item si12", "H1.write", ...)."""

    text: str
    scope: SpecScope = "any"
    domain: str | None = None
    label: str = ""


@dataclass(frozen=True)
class GuardRecord:
    label: str
    verdict: Literal["block", "pass", "confirm"]
    why: str
    model: str = ""
    shown: tuple[str, ...] = ()
    against: tuple[str, ...] = ()

    def dump(self) -> dict:
        return {"label": self.label, "verdict": self.verdict, "why": self.why,
                "model": self.model, "shown": list(self.shown), "against": list(self.against)}


@dataclass(frozen=True)
class GuardResult:
    outcome: Literal["pass", "block", "confirm"]
    reason: str = ""
    records: tuple[GuardRecord, ...] = field(default=())
    same_as: tuple[str, ...] = ()        # pass: the spec item ids the candidate only restates

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"


PASS = GuardResult("pass")


def against_of(spec: TeachingSpec, decisions: Decisions, *, rsi_rules: bool = False,
               exclude: Collection[str] = ()) -> list[Against]:
    """What an automatic change is compared with: the user's own and pending rules, the protected
    base rules (each as id@rev), and the decisions in force; with `rsi_rules`, also RSI's own and
    the default rules it may tune (kind rsi_rule), but not the items in `exclude` (the ones the
    change itself edits or removes)."""
    out = [Against(f"{i.item_id}@{i.rev}", i.text, "rule", i.scope, i.domain)
           for i in spec.items if i.authority != "rsi" or i.protected]
    if rsi_rules:
        out += [Against(f"{i.item_id}@{i.rev}", i.text, "rsi_rule", i.scope, i.domain)
                for i in spec.items
                if i.authority == "rsi" and not i.protected and i.item_id not in exclude]
    out += [Against(d.decision_id, d.text, d.action, d.scope, d.domain)  # type: ignore[arg-type]
            for d in decisions.active()]
    out += [Against(f"rejected#{n}", t, "rejected") for n, t in enumerate(decisions.legacy_active())]
    return out


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def nearest(text: str, pool: Sequence[Against], embedder: Embedder | None,
            k: int = TOP_K) -> list[Against]:
    """The k entries of `pool` most similar to `text` (all of them when there are no more): by the
    embedder's cosine, or, without one, by character-bigram Jaccard."""
    if len(pool) <= k:
        return list(pool)
    if embedder is None:
        return sorted(pool, key=lambda a: -jaccard(text, a.text))[:k]
    q = embedder.embed_query(text)
    docs = embedder.embed_documents([a.text for a in pool])
    ranked = sorted(zip(pool, docs), key=lambda p: -_cos(q, p[1]))
    return [a for a, _ in ranked[:k]]


def check_rule(spec: TeachingSpec, decisions: Decisions, cand: Candidate, *,
               judge: RuleJudge | None, embedder: Embedder | None = None,
               rsi_rules: bool = False, exclude: Collection[str] = ()) -> GuardResult:
    pool = [a for a in against_of(spec, decisions, rsi_rules=rsi_rules, exclude=exclude)
            if scopes_overlap(a.scope, a.domain, cand.scope, cand.domain)]  # type: ignore[arg-type]
    for a in pool:
        if near_duplicate(a.text, cand.text):
            # a duplicate of the user's own rule is no conflict: it only says it again
            if a.kind == "rule":
                continue
            why = DUP_OWN if a.kind == "rsi_rule" else DUP_DECISION
            return GuardResult("block", why, (GuardRecord(cand.label, "block", why,
                                                          against=(a.ref,)),))
    if not pool:
        return PASS
    shown = nearest(cand.text, pool, embedder)
    if judge is None:
        return GuardResult("confirm", NO_JUDGE, (GuardRecord(cand.label, "confirm", NO_JUDGE),))
    try:
        v = judge.conflict(cand.text, cand.scope, cand.domain, shown)
    except Exception:  # broad on purpose: an outage is "unsure", never a pass
        log.warning("rule guard: the judge failed on %s", cand.label, exc_info=True)
        return GuardResult("confirm", JUDGE_FAILED,
                           (GuardRecord(cand.label, "confirm", JUDGE_FAILED),))
    kinds = {a.ref: a.kind for a in shown}
    own = bool(v.against) and all(kinds.get(r) == "rsi_rule" for r in v.against)
    if v.verdict == "conflict":
        why = CONFLICT_OWN if own else CONFLICT
        rec = GuardRecord(cand.label, "block", v.why or why, v.model, v.shown, v.against)
        return GuardResult("block", why, (rec,))
    if v.verdict == "same":
        if any(kinds.get(r) != "rule" for r in v.against):
            # a rejection said again (a conflict), or one of RSI's own / the default rules said
            # again (a duplicate)
            decided = any(kinds.get(r) not in ("rule", "rsi_rule") for r in v.against)
            why = CONFLICT if decided else DUP_OWN
            rec = GuardRecord(cand.label, "block", v.why or why, v.model, v.shown, v.against)
            return GuardResult("block", why, (rec,))
        rec = GuardRecord(cand.label, "pass", v.why, v.model, v.shown, v.against)
        return GuardResult("pass", "", (rec,),
                           tuple(dict.fromkeys(r.split("@", 1)[0] for r in v.against)))
    if v.verdict == "unsure":
        rec = GuardRecord(cand.label, "confirm", v.why or UNSURE, v.model, v.shown, v.against)
        return GuardResult("confirm", UNSURE, (rec,))
    return GuardResult("pass", "", (GuardRecord(cand.label, "pass", v.why, v.model, v.shown),))


def check_many(spec: TeachingSpec, decisions: Decisions, cands: Sequence[Candidate], *,
               judge: RuleJudge | None, embedder: Embedder | None = None,
               rsi_rules: bool = False, exclude: Collection[str] = ()) -> GuardResult:
    """Every candidate checked; the worst outcome wins (block > confirm > pass), records kept."""
    records: list[GuardRecord] = []
    worst = PASS
    rank = {"pass": 0, "confirm": 1, "block": 2}
    for c in cands:
        r = check_rule(spec, decisions, c, judge=judge, embedder=embedder, rsi_rules=rsi_rules,
                       exclude=exclude)
        records += r.records
        if rank[r.outcome] > rank[worst.outcome]:
            worst = r
        if worst.outcome == "block":
            break
    return GuardResult(worst.outcome, worst.reason, tuple(records))


def check_auto_rule(state, text: str, scope: SpecScope = "any", domain: str | None = None, *,
                    actor: Literal["user", "rsi", "system"] = "rsi",
                    judge: RuleJudge | None = None,
                    embedder: Embedder | None = None) -> GuardResult:
    """The contract's entry point over a wiki state (its effective spec and decisions). A user's
    own action always passes."""
    if actor == "user":
        return PASS
    from evowiki.wikiapp.teachspec import effective_spec
    return check_rule(effective_spec(state), Decisions.of(state),
                      Candidate(text, scope, domain, "rule"), judge=judge, embedder=embedder)


def _traced(user_id: str, subject: str, check: Callable[[], GuardResult]) -> GuardResult:
    """A guard check as a traced step (plan 2026-10-29): its judge calls, and a veto (block or
    confirm) as a rejection with the records."""
    with traced_step(GUARD_STEP, subject, user_id) as step:
        res = check()
        if not res.passed:
            step.reject(f"rule guard {res.outcome}: {res.reason}"[:300],
                        records=[r.dump() for r in res.records])
        return res


GUARD_STEP = "rule_guard"       # the traced step of a guard check (llm/run_context.py)
GUARDED_DIMS = ("H1", "H2", "H8")       # text an agent reads as an instruction (§4.1)


def harness_candidates(diff: Any) -> list[Candidate]:
    """The rule texts a harness diff puts in force: H7 adds / edits, and every new H1 / H2 / H8
    value (JSON values as text). A set_param (a RepParams value) is no rule text."""
    out = [Candidate(op.text or "", op.scope, op.domain, f"H7 {op.op} {op.item_id or ''}".strip())
           for op in diff.spec_ops if op.op in ("add_item", "edit_item")]
    for op in diff.ops:
        if op.name.split(".", 1)[0] in GUARDED_DIMS and op.value is not None:
            text = op.value if isinstance(op.value, str) else json.dumps(op.value,
                                                                         ensure_ascii=False)
            out.append(Candidate(text[:4000], "any", None, op.name))
    return out


def edited_items(diff: Any) -> frozenset[str]:
    """The spec items a harness diff edits or removes: left out of the comparison (`exclude`)."""
    return frozenset(op.item_id for op in diff.spec_ops
                     if op.op in ("edit_item", "remove_item") and op.item_id)


def harness_guard(repo: Any, gateway: Any) -> Callable[[str, Any], GuardResult]:
    """The harness round's guard (`RoundDeps.guard`): the user's effective spec and decisions read
    now, RSI's own / the default rules included (`rsi_rules`, minus `edited_items`), the judge
    using EVOWIKI_IMPROVER_MODEL, metered to the user (`wikiapp.llm.CountingGateway`; no gateway → no judge, so anything to
    compare against is asked)."""
    from evowiki.runtime.usage import runtime_improver_ref
    from evowiki.wikiapp.llm import CountingGateway
    from evowiki.wikiapp.teachspec import effective_spec

    ref = runtime_improver_ref()
    if gateway is not None and ref is not None:
        gateway = gateway.with_model(ref)

    def guard(user_id: str, diff: Any) -> GuardResult:
        cands = harness_candidates(diff)
        if not cands:
            return PASS
        snap = repo.load(user_id)
        judge = (GatewayRuleJudge(CountingGateway(gateway, user_id), subject=user_id)
                 if gateway is not None else None)
        writer = [c for c in cands if not _assistant_key(c.label)]
        assistant = [c for c in cands if _assistant_key(c.label)]

        def check() -> GuardResult:
            spec, decisions = effective_spec(snap.state), Decisions.of(snap.state)
            a = check_many(spec, decisions, writer, judge=judge, rsi_rules=True,
                           exclude=edited_items(diff))
            if a.outcome == "block":
                return a
            b = check_many(spec, decisions, assistant, judge=judge)
            worst = b if b.outcome == "block" or a.outcome == "pass" else a
            return GuardResult(worst.outcome, worst.reason, (*a.records, *b.records))
        return _traced(user_id, user_id, check)
    return guard


def _assistant_key(label: str) -> bool:
    return label == "H1.assistant" or label.startswith(("H1.assistant.", "H2.assistant.",
                                                        "H8.assistant."))
