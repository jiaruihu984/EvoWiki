"""One offline round (spec §5.2–5.4): the current pack's samples, an optional A/A health check
(an unbalanced A/A voids the round before any candidate is written), every candidate on the dev
cases, the best passing one once on the held-out cases.

`Write` and `Judge` are plain callables so the same round runs on the harness's real replay and
judge (`harness_write`, `harness_judge`) and on the fakes of the walkthrough and the simulation.
A sample whose write fails is dropped with its case and listed in the report (`unusable`), never
silently replaced; a judge error stops the round (fail closed)."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from evowiki.harness.diff import PackDiff
from evowiki.harness.judge import Comparison, compare
from evowiki.harness.offline.evalset import OfflineCase, signals_text
from evowiki.harness.offline.stats import HELD_LOW, CaseResult, Verdict, aa_check, decide
from evowiki.harness.pack import HarnessPack
from evowiki.harness.replay import Case, ReplayDeps, ReplayResult, replay, run_all
from evowiki.llm.gateway import Gateway
from evowiki.llm.usage import Account
from evowiki.teach.contracts import TeachingSpec

Write = Callable[[OfflineCase, PackDiff, int], ReplayResult]          # (case, diff, sample no.)
Judge = Callable[[OfflineCase, ReplayResult, ReplayResult], Comparison]  # candidate a, current b
Score = Callable[[OfflineCase, ReplayResult], float | None]          # rubric share, None = n/a
BASE = PackDiff()


@dataclass
class Arm:
    """The samples of one pack on a set of cases; `failed` = case id → why a write failed."""
    samples: dict[str, list[ReplayResult]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


def sample(cases: Sequence[OfflineCase], diff: PackDiff, write: Write, k: int,
           *, first: int = 0) -> Arm:
    """Every case k times, at once (`replay.run_all`); a failed write drops its case with the reason."""
    jobs = [(c, i) for c in cases for i in range(k)]
    out = run_all([lambda c=c, i=i: write(c, diff, first + i) for c, i in jobs])
    arm = Arm()
    for c in cases:
        done = [r for (cc, _), r in zip(jobs, out, strict=True) if cc is c]
        bad = next((r for r in done if isinstance(r, Exception)), None)
        if bad is not None:
            arm.failed[c.id] = f"{type(bad).__name__}: {bad}"[:300]
        else:
            arm.samples[c.id] = done
    return arm


def _mean(xs: Sequence[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def results(cases: Sequence[OfflineCase], cand: Arm, base: Arm, judge: Judge,
            score: Score | None = None) -> list[CaseResult]:
    """Pair the i-th candidate sample with the i-th reference sample of each case both arms wrote."""
    out = []
    for c in cases:
        a, b = cand.samples.get(c.id), base.samples.get(c.id)
        if not a or not b:
            continue
        got = run_all([lambda x=x, y=y, c=c: judge(c, x, y) for x, y in zip(a, b, strict=True)])
        bad = next((g for g in got if isinstance(g, Exception)), None)
        if bad is not None:
            raise bad                # a judge error stops the round (fail closed)
        pairs = tuple(g.winner for g in got)
        out.append(CaseResult(
            c.id, c.exemplar, pairs,
            rubric_cand=_mean([score(c, x) for x in a]) if score else None,
            rubric_base=_mean([score(c, y) for y in b]) if score else None,
            cost_cand=sum(x.cost for x in a), cost_base=sum(y.cost for y in b),
            len_cand=sum(len(x.text) for x in a) / len(a),
            len_base=sum(len(y.text) for y in b) / len(b)))
    return out


@dataclass(frozen=True)
class CandidateOutcome:
    name: str
    dev: Verdict | None              # None: no usable case
    held_out: Verdict | None = None  # only the one candidate that went to held-out


@dataclass(frozen=True)
class OfflineReport:
    aa: tuple[bool, float] | None    # (healthy, mean) of the A/A run; None = not run
    candidates: tuple[CandidateOutcome, ...]
    chosen: str | None               # the candidate that passed held-out, None = keep the pack
    unusable: Mapping[str, str]      # case id → why it was dropped
    dev_cases: int
    held_out_cases: int


def run_offline(dev: Sequence[OfflineCase], held_out: Sequence[OfflineCase],
                candidates: Mapping[str, PackDiff], *, write: Write, judge: Judge, k: int = 2,
                aa: bool = True, score: Score | None = None) -> OfflineReport:
    if not candidates:
        raise ValueError("no candidates")
    base = sample(dev, BASE, write, k)
    unusable = dict(base.failed)
    aa_v = None
    if aa:
        base2 = sample(dev, BASE, write, k, first=k)
        unusable.update(base2.failed)
        aa_v = aa_check(results(dev, base2, base, judge, score))
        if not aa_v[0]:      # the pipeline favours one side: no candidate result could be trusted
            return OfflineReport(aa_v, (), None, unusable, len(dev), len(held_out))
    outcomes: list[CandidateOutcome] = []
    best: tuple[float, str] | None = None
    for name, diff in candidates.items():
        arm = sample(dev, diff, write, k)
        unusable.update({cid: f"{name}: {why}" for cid, why in arm.failed.items()})
        res = results(dev, arm, base, judge, score)
        v = decide(res) if any(not r.exemplar for r in res) else None
        outcomes.append(CandidateOutcome(name, v))
        if v is not None and v.passed and (best is None or v.win[0] > best[0]):
            best = (v.win[0], name)
    chosen = None
    if best is not None and held_out:
        name = best[1]
        hb = sample(held_out, BASE, write, k)
        hc = sample(held_out, candidates[name], write, k)
        unusable.update(hb.failed)
        unusable.update({cid: f"{name}: {why}" for cid, why in hc.failed.items()})
        res = results(held_out, hc, hb, judge, score)
        hv = (decide(res, low_bar=HELD_LOW, mean_bar=0.5)
              if any(not r.exemplar for r in res) else None)
        outcomes = [CandidateOutcome(o.name, o.dev, hv) if o.name == name else o
                    for o in outcomes]
        chosen = name if hv is not None and hv.passed else None
    return OfflineReport(aa_v, tuple(outcomes), chosen, unusable, len(dev), len(held_out))


# --- the harness's real replay and judge ---

def harness_write(hcases: Mapping[str, Case], *, base_pack: HarnessPack, base_spec: TeachingSpec,
                  deps: ReplayDeps) -> Write:
    """Each call is one fresh replay (the replay salts its calls, so no sample is a cache hit)."""
    def write(c: OfflineCase, diff: PackDiff, i: int) -> ReplayResult:
        return replay(hcases[c.id], diff, base_pack=base_pack, base_spec=base_spec, deps=deps)
    return write


def harness_judge(hcases: Mapping[str, Case], *, gw: Gateway, account: Account) -> Judge:
    """`harness.judge.compare` as is: blind, both orders, flip = tie, the quote check, the length
    rule; a judge of the writer's family raises JudgeUnavailable."""
    def judge(c: OfflineCase, a: ReplayResult, b: ReplayResult) -> Comparison:
        return compare(hcases[c.id], a, b, signals_text=signals_text(c), asked_for_more=False,
                       gw=gw, account=account)
    return judge


# --- the report ---

def _v(v: Verdict | None) -> str:
    if v is None:
        return "（没有能用的用例）"
    m, lo, hi = v.win
    rub = "—" if v.rubric_delta is None else f"{v.rubric_delta[0]:+.3f}（{v.rubric_delta[1]:+.3f}…{v.rubric_delta[2]:+.3f}）"
    ratio = "—" if v.len_ratio is None else f"{v.len_ratio:.2f}"
    ex = "—" if v.exemplar_mean is None else f"{v.exemplar_mean:.2f}"
    return (f"{'通过' if v.passed else '不通过'}｜{v.cases} 段｜胜率 {m:.2f}（{lo:.2f}…{hi:.2f}）｜"
            f"分出胜负 {v.decisive:.0%}｜rubric {rub}｜长度比 {ratio}｜范例得分 {ex}"
            f"｜{'；'.join(v.reasons + v.notes)}")


def markdown(r: OfflineReport) -> str:
    lines = [f"开发集 {r.dev_cases} 段，留出集 {r.held_out_cases} 段。", "",
             "- A/A（现行手册对自己）：" + ("没跑" if r.aa is None else
                 f"胜率 {r.aa[1]:.2f}，{'正常' if r.aa[0] else '偏向一边，这一轮作废：先查流水线'}")]
    for o in r.candidates:
        lines.append(f"- 候选 {o.name}，开发集：{_v(o.dev)}")
        if o.held_out is not None:
            lines.append(f"  - 留出集：{_v(o.held_out)}")
    lines.append(f"- 结论：{'采用候选 ' + r.chosen + '（等你批准）' if r.chosen else '保持现行手册'}")
    if r.unusable:
        lines.append(f"- 没能用的用例 {len(r.unusable)} 段：" + "；".join(
            f"{k}（{v}）" for k, v in sorted(r.unusable.items())))
    return "\n".join(lines)
