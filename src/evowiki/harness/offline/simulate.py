"""Monte Carlo of the decision rules (spec §5.3) under a simple writer / judge noise model, to see
what each step of the offline round buys before any paid run. Numbers here are only as good as the
model's assumptions (written in `Model`); the calibration run measures the real judge.

Model: every written sample of a case has quality `effect + N(0, 1)` (effect = the pack's true
effect on that case, in units of the writer's own sample-to-sample spread). The judge, shown X first
and Y second, sees `q_X − q_Y + length_bias·(len_X − len_Y) + position + N(0, judge_noise)` and says
"first" above `tie_band`, "second" below −tie_band, else "same". A both-order comparison counts only
when both orders agree (flip = tie), and a candidate win with a text over `LENGTH_RATIO` × longer is
a tie (the harness judge's length rule)."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from math import comb

import numpy as np

from evowiki.harness.anchors import LENGTH_RATIO
from evowiki.harness.offline.stats import AA_TOLERANCE, DEV_LOW, EXEMPLAR_MIN, HELD_LOW


@dataclass(frozen=True)
class Model:
    judge_noise: float = 0.5
    tie_band: float = 0.3
    position: float = 0.2          # pull towards the text shown first
    length_bias: float = 1.0       # judge score per +100 % relative length
    rel_length: float = 0.0        # candidate's text is this much longer (0.2 = +20 %)
    length_cut: float = 0.0        # share of candidate wins the length rule voids because the
                                   # same pack's own texts vary in length (A/A run: 3 of 5)


@dataclass(frozen=True)
class Scenario:
    name: str
    dev_effect: float              # true effect on dev topics (incl. fitting to them)
    held_effect: float             # true effect on unseen topics
    model: Model = Model()
    exemplar_effect: float | None = None   # true effect on the exemplars; None = held_effect
    candidates: int = 1            # several candidates tried, the best on dev goes on


@dataclass(frozen=True)
class Rule:
    name: str
    dev_cases: int = 60
    held_cases: int = 25
    exemplars: int = 15
    k: int = 2
    swap: bool = True
    aa: str = "sanity"             # "floor": candidate low > A/A high; "sanity": an A/A mean off 0.5
                                   # by more than AA_TOLERANCE voids the round (biased pipeline); "off"
    held_low: float | None = HELD_LOW  # held-out passes when its mean > 0.5 and its low > this;
                                   # None = no held-out step
    exemplar: str = "mean"         # "clear": any exemplar lost on every sample fails; "mean": the
                                   # exemplars' mean score below EXEMPLAR_MIN fails; "off"
    old_gate: bool = False         # the per-user harness gate: few cases, wins ≥ 2, no loss

    def writes(self, candidates: int) -> int:
        if self.old_gate:
            return (self.dev_cases + self.exemplars) * self.k * (1 + candidates)
        dev = (self.dev_cases + self.exemplars) * self.k * (1 + int(self.aa != "off") + candidates)
        return dev + (self.held_cases * self.k * 2 if self.held_low is not None else 0)


OLD = Rule("旧关卡（4 段问题 + 1 段范例，每段 1 次）", dev_cases=4, exemplars=1, k=1, aa="off",
           held_low=None, exemplar="clear", old_gate=True)
FIRST = Rule("方案初稿（A/A 当下限、留出集要显著、范例一段都不能输）", aa="floor", held_low=0.5,
             exemplar="clear")
FULL = Rule("修正后（A/A 只体检、留出集不变差、范例看平均）")
RULES = (OLD, FIRST, FULL,
         replace(FULL, name="修正后 k=3", k=3),
         replace(FULL, name="去掉：两种顺序都比", swap=False),
         replace(FULL, name="去掉：每段多次取样（k=1）", k=1),
         replace(FULL, name="去掉：A/A 体检", aa="off"),
         replace(FULL, name="去掉：留出集", held_low=None),
         replace(FULL, name="去掉：范例检查", exemplar="off"),
         replace(FULL, name="开发集只有 15 段", dev_cases=15, held_cases=8))


def _winners(rng, effect: np.ndarray, m: Model, swap: bool) -> np.ndarray:
    """effect: (..., pairs) true effects; returns +1 candidate win, 0 tie, −1 loss."""
    diff = effect + rng.standard_normal(effect.shape) - rng.standard_normal(effect.shape)
    lb = m.length_bias * m.rel_length

    def order(z):
        return np.where(z > m.tie_band, 1, np.where(z < -m.tie_band, -1, 0))
    first = order(diff + lb + m.position + m.judge_noise * rng.standard_normal(effect.shape))
    if not swap:
        w = first
    else:   # candidate shown second: "first" names the reference
        second = -order(-diff - lb + m.position + m.judge_noise * rng.standard_normal(effect.shape))
        w = np.where(first == second, first, 0)
    if 1 + m.rel_length > LENGTH_RATIO:
        w = np.where(w == 1, 0, w)
    if m.length_cut:
        w = np.where((w == 1) & (rng.random(w.shape) < m.length_cut), 0, w)
    return w


# the judge measured on the 2026-10-28 A/A run (DeepSeek writer, Haiku 4.5 judge, 18 pairs):
# never "same", the text shown second picked 72 %, `fit` → no tie band and a pull of −0.9 to the
# first; the length rule voided 3 of the 5 candidate wins
MEASURED = Model(judge_noise=0.5, tie_band=0.0, position=-0.9)
MEASURED_CUT = replace(MEASURED, length_cut=0.6)


def _case_scores(rng, sims: int, cases: int, k: int, effect: float, m: Model,
                 swap: bool) -> np.ndarray:
    w = _winners(rng, np.full((sims, cases, k), effect), m, swap)
    return (w + 1).mean(axis=2) / 2           # (sims, cases): 1 win … 0.5 tie … 0 loss


def _low_high(rng, scores: np.ndarray, boot: int) -> tuple[np.ndarray, np.ndarray]:
    n = scores.shape[1]
    idx = rng.integers(0, n, size=(boot, n))
    means = scores[:, idx].mean(axis=2)       # (sims, boot)
    return np.quantile(means, 0.025, axis=1), np.quantile(means, 0.975, axis=1)


def _clear_loss(rng, sims: int, n: int, k: int, effect: float, m: Model, swap: bool) -> np.ndarray:
    w = _winners(rng, np.full((sims, n, k), effect), m, swap)
    return (w == -1).all(axis=2).any(axis=1)  # any exemplar lost on every sample


def accept_rate(rule: Rule, sc: Scenario, *, sims: int = 1000, boot: int = 300,
                seed: int = 0) -> float:
    rng = np.random.default_rng(seed)
    m = sc.model
    if rule.old_gate:
        ok = np.zeros(sims, dtype=bool)
        for _ in range(sc.candidates):       # the old round adopts the first that passes
            w = _winners(rng, np.full((sims, rule.dev_cases), sc.dev_effect), m, rule.swap)
            wins, losses = (w == 1).sum(axis=1), (w == -1).sum(axis=1)
            ex = sc.held_effect if sc.exemplar_effect is None else sc.exemplar_effect
            anchor = _winners(rng, np.full((sims, rule.exemplars), ex), m, rule.swap)
            ok |= (wins >= 2) & (losses == 0) & ~(anchor == -1).any(axis=1)
        return float(ok.mean())
    ex_effect = sc.held_effect if sc.exemplar_effect is None else sc.exemplar_effect
    floor = np.full(sims, DEV_LOW)
    valid = np.ones(sims, dtype=bool)
    if rule.aa != "off":
        aa = _case_scores(rng, sims, rule.dev_cases, rule.k, 0.0, replace(m, rel_length=0.0),
                          rule.swap)
        if rule.aa == "floor":
            floor = np.maximum(floor, _low_high(rng, aa, boot)[1])
        else:
            valid = np.abs(aa.mean(axis=1) - 0.5) <= AA_TOLERANCE
    passed_any = np.zeros(sims, dtype=bool)
    for _ in range(sc.candidates):
        s = _case_scores(rng, sims, rule.dev_cases, rule.k, sc.dev_effect, m, rule.swap)
        lo, _ = _low_high(rng, s, boot)
        ok = lo > floor
        if rule.exemplar == "clear":
            ok &= ~_clear_loss(rng, sims, rule.exemplars, rule.k, ex_effect, m, rule.swap)
        elif rule.exemplar == "mean":
            ex = _case_scores(rng, sims, rule.exemplars, rule.k, ex_effect, m, rule.swap)
            ok &= ex.mean(axis=1) >= EXEMPLAR_MIN
        passed_any |= ok
    passed_any &= valid
    if rule.held_low is None:
        return float(passed_any.mean())
    h = _case_scores(rng, sims, rule.held_cases, rule.k, sc.held_effect, m, rule.swap)
    lo, _ = _low_high(rng, h, boot)
    held_floor = 0.5 if rule.aa == "floor" else rule.held_low
    held_ok = (lo > np.maximum(floor, held_floor)) if rule.aa == "floor" else (
        (h.mean(axis=1) > 0.5) & (lo > held_floor))
    return float((passed_any & held_ok).mean())


SCENARIOS = (
    Scenario("没改好（真实效果 0）", 0.0, 0.0),
    Scenario("改坏了（−0.2）", -0.2, -0.2),
    Scenario("小改进（+0.2）", 0.2, 0.2),
    Scenario("中等改进（+0.4）", 0.4, 0.4),
    Scenario("明显改进（+0.6）", 0.6, 0.6),
    Scenario("大改进（+0.8）", 0.8, 0.8),
    Scenario("只在开发集主题上有效（过拟合 +0.4 / 0）", 0.4, 0.0),
    Scenario("开发集 +0.6，换主题反而变差（−0.2）", 0.6, -0.2),
    Scenario("问题段 +0.6，范例页不受影响（0）", 0.6, 0.6, exemplar_effect=0.0),
    Scenario("问题段 +0.6，但把写得好的范例页改差（−0.4）", 0.6, 0.6, exemplar_effect=-0.4),
    Scenario("只是写长了 20%（裁判偏爱长文）", 0.0, 0.0, Model(rel_length=0.2)),
    Scenario("试 3 个都没用的候选，挑最好的", 0.0, 0.0, candidates=3),
    Scenario("裁判噪声加倍，中等改进（+0.4）", 0.4, 0.4, Model(judge_noise=1.0)),
    Scenario("裁判有位置偏好 0.6，没改好", 0.0, 0.0, Model(position=0.6)),
    Scenario("裁判有位置偏好 0.6，中等改进（+0.4）", 0.4, 0.4, Model(position=0.6)),
    Scenario("实测裁判，没改好", 0.0, 0.0, MEASURED),
    Scenario("实测裁判，明显改进（+0.6）", 0.6, 0.6, MEASURED),
    Scenario("实测裁判 + 长度规则作废 60% 的胜，明显改进（+0.6）", 0.6, 0.6, MEASURED_CUT),
    Scenario("实测裁判 + 长度规则作废 60% 的胜，大改进（+0.8）", 0.8, 0.8, MEASURED_CUT),
)


def table(rules=RULES, scenarios=SCENARIOS, *, sims: int = 1000,
          progress: Callable[[str], None] | None = None) -> list[list[str]]:
    """Rows: scenario × rule acceptance rates (%), plus the writes each rule costs."""
    rows = [["情况", *[r.name for r in rules]]]
    for i, sc in enumerate(scenarios):
        if progress:
            progress(sc.name)
        rows.append([sc.name, *[f"{100 * accept_rate(r, sc, sims=sims, seed=i):.1f}%"
                                for r in rules]])
    rows.append(["每个候选要写的段数（1 个候选）", *[str(r.writes(1)) for r in rules]])
    return rows


def calibration_pass(agreement: float, pairs: int = 40, bar: float = 0.75) -> float:
    """P(the measured agreement on `pairs` decisive labels ≥ `bar`) for a judge whose true agreement
    with the person is `agreement` (binomial)."""
    need = int(np.ceil(bar * pairs))
    return sum(comb(pairs, x) * agreement**x * (1 - agreement)**(pairs - x)
               for x in range(need, pairs + 1))


def aa_shares(m: Model, *, n: int = 20000, seed: int = 0) -> tuple[float, float, float]:
    """What an A/A comparison looks like under `m` (true effect 0): each order's share of "first" and
    of "second", and the share of pairs whose two orders name the same text."""
    rng = np.random.default_rng(seed)
    d = rng.standard_normal(n) - rng.standard_normal(n)
    z1 = d + m.position + m.judge_noise * rng.standard_normal(n)       # X shown first
    z2 = -d + m.position + m.judge_noise * rng.standard_normal(n)      # Y shown first
    first = ((z1 > m.tie_band).mean() + (z2 > m.tie_band).mean()) / 2
    second = ((z1 < -m.tie_band).mean() + (z2 < -m.tie_band).mean()) / 2
    same = ((z1 > m.tie_band) & (z2 < -m.tie_band)) | ((z1 < -m.tie_band) & (z2 > m.tie_band))
    return float(first), float(second), float(same.mean())


def fit(first: float, second: float, same_side: float) -> Model:
    """The judge noise model (noise, tie band, position) whose A/A shares (`aa_shares`) are nearest
    the measured ones: a coarse grid, least squares. Three numbers fit three parameters, so a small
    run fixes them only roughly."""
    best: tuple[float, Model] | None = None
    for noise in np.arange(0.0, 3.01, 0.1):
        for band in np.arange(0.0, 3.01, 0.1):
            for pos in np.arange(-1.0, 1.01, 0.1):
                m = Model(judge_noise=round(float(noise), 2), tie_band=round(float(band), 2),
                          position=round(float(pos), 2))
                f, s, a = aa_shares(m, n=4000)
                err = (f - first) ** 2 + (s - second) ** 2 + (a - same_side) ** 2
                if best is None or err < best[0]:
                    best = (err, m)
    assert best is not None
    return best[1]
