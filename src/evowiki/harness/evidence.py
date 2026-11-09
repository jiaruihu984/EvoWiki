"""The evidence a harness candidate must show before anyone is asked about it: the corrected rule of
report 2026-10-28-offline-eval-minimal (`offline.stats`), run on the round's own cases.

Order, cheapest veto first, each step stopping the round's spending when it decides:
1. the current pack writes every case k times (a case it cannot write is unusable);
2. canaries on its texts (`canaries`): the judge reads pairs whose answer is known. Picking a
   scrambled text over the intact one voids the round;
3. A/A: the current pack's own samples against each other, pooled with earlier rounds of the same
   writer and judge (`AA_MIN_PAIRS`: a handful of pairs cannot tell a biased pipeline from chance);
   a mean off 0.5 by more than `stats.AA_TOLERANCE` voids the round;
4. the candidate writes every case k times; a case only it cannot write fails it (deterministic);
5. dev cases (`stats.decide`, the case is the bootstrap unit) and exemplars on average;
6. the surviving candidate on the held-out cases the improver never saw: same direction, not worse.

Beside the score goes what the score does not optimise (`beside`), so 「写长了就赢」 shows: length,
cost, how often a text ends on a follow-up question or refuses, and how many wins went to the longer
text. These are reported, not gated, except the length rule the judge already applies.

A case whose two arms did different things (`Comparison.actions_differ`) leaves the judged cases: it
goes to `detail["action_cases"]`, and only a real regression can prove it (`gate.layers`). Without
any other case the content layer has nothing to say, and says so (`NOTHING_TO_JUDGE`, thin).

The texts compared are kept in the detail (`samples`): the human labels that would calibrate the judge
and the sampled reading before an approval both need them, and nothing else stores them.

Pure over `Write` / `Judge` callables, as `offline.run`; no database, no model of its own."""

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from evowiki.harness.anchors import AA_MIN_PAIRS, EVAL_SAMPLES
from evowiki.harness.diff import PackDiff
from evowiki.harness.judge import Comparison
from evowiki.harness.leak import sentences
from evowiki.harness.offline.run import BASE, Arm, sample
from evowiki.harness.offline.run import results as pair_results
from evowiki.harness.offline.stats import (
    _SCORE,
    AA_TOLERANCE,
    DEV_LOW,
    HELD_LOW,
    CaseResult,
    Verdict,
    decide,
)
from evowiki.harness.replay import Case, ReplayResult

Outcome = Literal["pass", "thin", "fail"]
MIXED = "参照和候选是不同的写作模型写的，没法比较"
NO_DEV = "现行设置一段都没写出来，没法比较"
NO_HELD = "没有留出的用例能用，不能确认换一段也不变差"
BROKEN = "新设置写不出 {n} 段：{why}"
CANARY_VOID = "裁判把打乱的文字选得比原文好，这一轮的比较不可信，作废"
AA_VOID = "现行设置和自己比，胜率 {m:.2f}（{n} 组），流水线偏向一边，这一轮作废"
NOTHING_TO_JUDGE = "每一段的动作都不同，裁判没有可比的内容"
CANARIES = 2
_FOLLOW_UP = re.compile(r"[？?]\s*$")
_REFUSAL = re.compile(r"没法|无法|做不到|不能帮|读不到|没读到|找不到|没有找到|不确定|\b(?:cannot|can['’]t|unable to|could not|couldn['’]t|not sure|uncertain|not found)\b", re.IGNORECASE)


@dataclass(frozen=True)
class EvalCase:
    """A round's case in the shape `offline.run` pairs: `id`, `exemplar`."""

    id: str
    exemplar: bool
    case: Case


def eval_cases(cases: Sequence[Case]) -> list[EvalCase]:
    return [EvalCase(c.case_id, c.anchor, c) for c in cases]


def held_out_split(cases: Sequence[Case]) -> tuple[tuple[Case, ...], tuple[Case, ...]]:
    """(what the improver sees, what it never sees): per kind of case, a third of them (at least one
    when the kind has two), the ones lowest by the hash of their id, so a case's side does not
    depend on what else is in the round."""
    held: list[str] = []
    for kind in dict.fromkeys(c.kind for c in cases):
        same = sorted((c.case_id for c in cases if c.kind == kind),
                      key=lambda i: hashlib.sha256(i.encode("utf-8")).hexdigest())
        n = max(1, len(same) // 3) if len(same) >= 2 else 0
        held += same[:n]
    return (tuple(c for c in cases if c.case_id not in held),
            tuple(c for c in cases if c.case_id in held))


Judge = Callable[[EvalCase, ReplayResult, ReplayResult], Comparison]
Write = Callable[[EvalCase, PackDiff, int], ReplayResult]


def _rate(arm: Arm, test: Callable[[str], bool]) -> float | None:
    texts = [r.text for rs in arm.samples.values() for r in rs]
    return sum(test(t) for t in texts) / len(texts) if texts else None


def beside(res: Sequence[CaseResult], cand: Arm, base: Arm) -> dict[str, Any]:
    """What the score does not optimise (module doc)."""
    cost_c = sum(r.cost_cand for r in res)
    cost_b = sum(r.cost_base for r in res)
    len_c = sum(r.len_cand for r in res)
    len_b = sum(r.len_base for r in res)
    wins = losses = ties = long_wins = 0
    for r in res:
        for i, w in enumerate(r.pairs):
            wins, losses, ties = wins + (w == "a"), losses + (w == "b"), ties + (w == "tie")
            a, b = cand.samples[r.case_id][i].text, base.samples[r.case_id][i].text
            long_wins += w != "tie" and len(a if w == "a" else b) > len(b if w == "a" else a)
    return {"wins": wins, "losses": losses, "ties": ties, "wins_by_longer": long_wins,
            "len_ratio": len_c / len_b if len_b else None,
            "cost_ratio": cost_c / cost_b if cost_b else None,
            "follow_up_cand": _rate(cand, lambda t: bool(_FOLLOW_UP.search(t))),
            "follow_up_base": _rate(base, lambda t: bool(_FOLLOW_UP.search(t))),
            "refusal_cand": _rate(cand, lambda t: bool(_REFUSAL.search(t))),
            "refusal_base": _rate(base, lambda t: bool(_REFUSAL.search(t)))}


def canaries(cases: Sequence[EvalCase], base: Arm, judge: Judge) -> list[dict[str, Any]]:
    """Up to CANARIES references, each judged against itself, against its sentences in reverse order
    and against itself written twice, reading the judge's raw choice in each order (the quote rule
    alone would tie all three). `raw`: what it named in the two orders, "a" being the altered copy."""
    out: list[dict[str, Any]] = []
    for c in [c for c in cases if base.samples.get(c.id)][:CANARIES]:
        r = base.samples[c.id][0]
        for name, text in (("same", r.text), ("scrambled", "".join(sentences(r.text)[::-1])),
                           ("padded", f"{r.text}\n\n{r.text}")):
            x = replace(r, text=text, run_id=f"{r.run_id}:canary:{name}")
            out.append({"case": c.id, "canary": name, "raw": list(judge(c, x, r).sides)})
    return out


def _scrambled_won(rows: Sequence[dict[str, Any]]) -> bool:
    return any(r["canary"] == "scrambled" and r["raw"] == ["a", "a"] for r in rows)


def aa_pairs(cases: Sequence[EvalCase], base: Arm, judge: Judge) -> list[str]:
    """The current pack's own samples against each other, one pair per problem case."""
    out = []
    for c in cases:
        rs = base.samples.get(c.id) or []
        if not c.exemplar and len(rs) >= 2:
            out.append(judge(c, rs[0], rs[1]).winner)
    return out


def aa_health(pairs: Sequence[str]) -> tuple[bool, float | None]:
    """(healthy, mean): always healthy below AA_MIN_PAIRS pairs, too few to tell."""
    if not pairs:
        return True, None
    m = sum(_SCORE[p] for p in pairs) / len(pairs)
    return len(pairs) < AA_MIN_PAIRS or abs(m - 0.5) <= AA_TOLERANCE, m


def _texts(cand: Arm, base: Arm) -> dict[str, dict[str, list[str]]]:
    return {i: {"candidate": [r.text for r in cand.samples.get(i, [])],
                "current": [r.text for r in rs]} for i, rs in base.samples.items()}


def _verdict_json(v: Verdict) -> dict[str, Any]:
    return {"passed": v.passed, "reasons": list(v.reasons), "win": list(v.win),
            "decisive": v.decisive, "exemplar_mean": v.exemplar_mean, "cases": v.cases,
            "len_ratio": v.len_ratio, "cost_ratio": v.cost_ratio, "notes": list(v.notes)}


def _underpowered(v: Verdict, low_bar: float) -> bool:
    """The judge saw no difference anywhere, or the candidate's mean is above even and only the
    interval's low bound is short: the cases are too few to say. Not a loss: `thin` (no lesson, its signals stay for the next round), never a
    pass. A round of 4 cases can only clear the bound with 3 clear wins; failing the rest wrote
    「无效」 lessons about changes that restored the regression cases (fault injection 2026-11-05)."""
    return (v.decisive == 0 or v.win[0] > 0.5) and v.win[1] <= low_bar and len(v.reasons) == 1


@dataclass(frozen=True)
class EvidenceResult:
    verdict: Outcome
    reasons: tuple[str, ...]
    wins: int
    losses: int
    ties: int
    anchor_losses: int
    detail: dict[str, Any]


def _end(verdict: Outcome, reason: str, detail: dict[str, Any],
         res: Sequence[CaseResult] = ()) -> EvidenceResult:
    pairs = [w for r in res if not r.exemplar for w in r.pairs]
    return EvidenceResult(verdict, (reason,), pairs.count("a"), pairs.count("b"), pairs.count("tie"),
                    sum(r.clear_loss for r in res if r.exemplar), detail)


def evaluate(dev: Sequence[Case], held_out: Sequence[Case], anchors: Sequence[Case],
             diff: PackDiff, *, write: Write, judge: Judge, raw_judge: Judge,
             prior_aa: Sequence[str] = (), k: int = EVAL_SAMPLES) -> EvidenceResult:
    """The rule in the module doc. `judge` records its comparisons on the round, `raw_judge` is the
    same judge for canaries and A/A, which are not comparisons of this candidate. `prior_aa`: A/A
    winners of earlier rounds with this writer and judge. A judge from the writer's family raises
    JudgeUnavailable, a judge's model error raises (fail closed)."""
    cases = eval_cases((*dev, *anchors))
    held = eval_cases(held_out)
    detail: dict[str, Any] = {"k": k, "dev": len(dev), "held_out": len(held),
                              "anchors": len(anchors)}
    acted: set[str] = set()

    def judged(c: EvalCase, a: ReplayResult, b: ReplayResult) -> Comparison:
        out = judge(c, a, b)
        if out.actions_differ:
            acted.add(c.id)
            detail["action_cases"] = sorted(acted)
        return out
    if not held:
        return _end("thin", NO_HELD, detail)
    base = sample(cases, BASE, write, k)
    detail["unusable"] = dict(base.failed)
    if not any(not c.exemplar and c.id in base.samples for c in cases):
        return _end("thin", NO_DEV, detail)
    try:
        rows = canaries(cases, base, raw_judge)
        detail["canaries"] = rows
        if _scrambled_won(rows):
            return _end("thin", CANARY_VOID, detail)
        aa = aa_pairs(cases, base, raw_judge)
        pooled = [*prior_aa, *aa]
        healthy, m = aa_health(pooled)
        detail["aa"] = {"round": aa, "pooled": len(pooled), "mean": m, "healthy": healthy}
        if not healthy:
            assert m is not None
            return _end("thin", AA_VOID.format(m=m, n=len(pooled)), detail)
        cand = sample(cases, diff, write, k)
        broken = {i: why for i, why in cand.failed.items() if i in base.samples}
        detail["unusable"].update({i: why for i, why in cand.failed.items() if i not in broken})
        if broken:
            detail["broken"] = broken
            return _end("fail", BROKEN.format(n=len(broken), why=next(iter(broken.values()))),
                        detail)
        res = pair_results(cases, cand, base, judged)
    except ValueError as e:           # `compare` refuses texts of two writers
        if "writers" in str(e):
            return _end("thin", MIXED, detail)
        raise
    detail["beside"] = beside(res, cand, base)
    detail["samples"] = _texts(cand, base)
    if not any(not r.exemplar for r in res):
        return _end("thin", NO_DEV, detail)
    detail["cases"] = [{"id": r.case_id, "exemplar": r.exemplar, "pairs": list(r.pairs)}
                       for r in res]
    res = [r for r in res if r.case_id not in acted]
    if not any(not r.exemplar for r in res):
        return _end("thin", NOTHING_TO_JUDGE, detail)
    v = decide(res)
    detail["dev_verdict"] = _verdict_json(v)
    if not v.passed:
        verdict: Outcome = "thin" if _underpowered(v, DEV_LOW) else "fail"
        return _end(verdict, "；".join(v.reasons), detail, res)
    h_base = sample(held, BASE, write, k)
    h_cand = sample(held, diff, write, k)
    detail["unusable"].update(h_base.failed)
    detail["unusable"].update({i: w for i, w in h_cand.failed.items() if i not in h_base.samples})
    h_broken = {i: why for i, why in h_cand.failed.items() if i in h_base.samples}
    if h_broken:
        detail["broken"] = h_broken
        return _end("fail", BROKEN.format(n=len(h_broken), why=next(iter(h_broken.values()))),
                    detail, res)
    h_res = [r for r in pair_results(held, h_cand, h_base, judged) if r.case_id not in acted]
    if not h_res:
        return _end("thin", NO_HELD, detail, res)
    hv = decide(h_res, low_bar=HELD_LOW, mean_bar=0.5)
    detail["held_verdict"] = _verdict_json(hv)
    detail["held_beside"] = beside(h_res, h_cand, h_base)
    detail["samples"].update(_texts(h_cand, h_base))
    detail["held_cases"] = [{"id": r.case_id, "pairs": list(r.pairs)} for r in h_res]
    if not hv.passed:
        return _end("thin" if _underpowered(hv, HELD_LOW) else "fail",
                    "留出的用例：" + "；".join(hv.reasons), detail, res)
    m, lo, hi = v.win
    return _end("pass", f"重放比较胜率 {m:.2f}（{lo:.2f}…{hi:.2f}），留出的用例 {hv.win[0]:.2f}",
                detail, res)
