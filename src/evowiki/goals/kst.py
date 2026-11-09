"""Knowledge Space Theory adaptive assessment: a Python port of the R package **mycaas**
(My Computerized Adaptive Assessment, version 0.0.1, CRAN 2024-10-11,
https://cran.r-project.org/web/packages/mycaas/index.html; authors Andrea Brancaccio and Umberto
Granziol), used by the goal flow's diagnosis (goals/probe.py). Design note:
the product specification

Ported from mycaas (R/ file → function → here):

- R/Questioning_rule.R `half_split(likelihood, states, beta, eta, Q_pool)` → `half_split`: the item
  whose marginal probability of being in the learner's state, L(q) = sum of the likelihood of the
  states containing q, is closest to 1/2 (min |2·L(q) − 1|, compared at 4 decimals as mycaas does),
  among the items still in the pool.
- R/Updating_rule.R `updating(likelihood, states, q, r_q, beta, eta)` → `updating`: the
  multiplicative rule (Falmagne & Doignon 2011, ch. 13 §10): a right answer (r_q = 1) multiplies
  states containing q by 1 − beta_q (no careless error) and the others by eta_q (lucky guess); a
  wrong answer multiplies them by beta_q and 1 − eta_q; then renormalise.
- R/Termination_rule.R `stopping_criterion(likelihood, states, termination, SC)` →
  `stopping_criterion`: "likelihood_maximization" (stop when some state's probability exceeds SC;
  Heller & Repitsch 2012) or "items_discrimination" (stop when every item's marginal lies outside
  [SC1, SC2]; Donadello et al. 2017).
- R/Assessment.R `assessment(...)` (its adaptive, no-repetition loop) → `run_assessment`: while
  not stopped and the pool is not empty, pick by half-split, take the item out of the pool, get a
  response, update. The uniform initial likelihood (`likelihood = NA` → rep(1/|K|)) is `uniform`.
  The goal flow runs the same loop one step per diagnosis round (goals/probe.py).

Faithful except, deliberately: (1) ties in half-split are broken by the first item in pool order
(mycaas draws one at random with `sample`) so the same evidence always picks the same item and a
stored diagnosis replays identically; (2) a response with zero total likelihood (only possible with
beta or eta at exactly 0 or 1) raises instead of dividing by zero (fail closed); (3) indices are
0-based; (4) the initial likelihood is the informative product prior below, not the uniform one
(mycaas accepts any initial `likelihood`; uniform is only its default).

Not in mycaas (it takes the state matrix as input, e.g. from `pks::delineate`): building the
knowledge structure from a prerequisite map. `downsets` builds the quasi-ordinal knowledge space of
a surmise relation (Doignon & Falmagne, Birkhoff's theorem): the states are exactly the sets closed
under prerequisites (q in K ⇒ every prerequisite of q in K). Cycles are allowed (items on a cycle
are mastered together).

State-space size and tractability. A prerequisite map with n items has between n + 1 (a chain) and
2^n (no prerequisites at all) states, so enumeration can explode. `Assessment` keeps it tractable
*exactly*, without truncation: the structure of a map whose prerequisite graph falls apart into
connected components is the product of the components' structures, a uniform (or product-form,
see prior) likelihood over the product is the product of the parts' likelihoods, an item's update touches only its component's
factor, an item's marginal is computed inside its component, and the most likely state's
probability is the product of the components' maxima — so each component is enumerated on its own
and the numbers are identical to enumerating the whole product. A component is limited to
`MAX_ITEMS` items (states are 64-bit masks) and `MAX_STATES` states (each state costs 16 bytes:
mask + likelihood; 2^20 states ≈ 16 MB, each update/half-split a few vectorised passes). A component
past either limit raises `StructureTooLarge` (no silent truncation); the probe then ends the
diagnosis explicitly (goals/probe.py `TOO_LARGE_WHY`).

Parameters (algorithm parameters, not caps; the probe passes them per observation):

- `BETA_QUIZ` = 0.1, `ETA_QUIZ` = 0.1: a quiz answered wrong although known (careless / a flawed
  generated item) and answered right although not known (a lucky guess: 2–8 options plus an
  explicit 「不知道」 that an honest learner takes instead of guessing). A right answer multiplies the
  odds of the states containing the item by 9, a wrong one by 1/9.
- `BETA_SELF` = 0.3, `ETA_SELF` = 0.3: a self-report (「学过线性代数吗？」) or what the learner's own
  description said: weak evidence, the odds move by 7/3 either way (a quiz moves them by 9).
- `BETA_DONT` = `ETA_DONT` = 0.1: a self-report of 「不会 / 没学过」 (means `dont`; user ruling
  2026-10-24: 「大块的那个都已经回答不会了就不用聚焦了」): the learner's own word that he never learned
  it counts as much as a wrong quiz answer, so it resolves the item *teach* on its own (1/2 → 0.1)
  and no focus question follows. It only ever enters the full-evidence likelihood (the strong one
  never sees a self-report), so it can only make an item *teach*, never *known*; 「熟悉」
  (`partly`) and 「学过但忘了」 (`forgot`) stay at BETA_SELF / ETA_SELF and still gets its question.
- `BETA_K` = `ETA_K` = 0.1: what K already had before the goal (bands built from earlier answers),
  as strong as a quiz.
- `STOP_P` = 0.5: "likelihood_maximization" with SC = 0.5, mycaas `assessment()`'s default (its
  `stopping_criterion()` default is 0.8), used by `run_assessment` / `Assessment.stop` (the
  faithful port of mycaas's loop). The goal flow's diagnosis no longer stops on it (probe@v6): the
  most likely *single state* of a whole map is a product over many items, so on a map of 26 items
  each about 95% decided it stays below 0.5 and half-split keeps asking near-certain items until
  the pool is empty (live run 4, 2026-10-11: rounds 9–13 asked items at 3%, 2%, 2%, 99%, 0%).
- `SC_TEACH` = 0.2, `SC_KNOWN` = 0.8: mycaas's other termination rule, "items_discrimination"
  (Donadello et al. 2017: stop when every item's marginal lies outside [SC1, SC2]), in the
  probe's asymmetric form (`discriminated`; goals/probe.py): an item is resolved *teach* when its
  full-evidence marginal is ≤ SC_TEACH, *known* when both its full-evidence and its strong-evidence
  marginals are ≥ SC_KNOWN. Why these two values: they are symmetric around 1/2 (mycaas's two-value
  SC) and sit where one quiz lands — a quiz moves the odds by 9, so an item at 1/2 goes to 0.9 or
  0.1 and one answer resolves it — while a self-report (odds × 7/3: 1/2 → 0.7 / 0.3) resolves
  nothing on its own; it takes a self-report that agrees with what the description already said
  (unlearned / medium 0.22 → 「没学过」 0.11) to resolve *teach*, and *known* needs strong evidence
  in any case. Narrower (0.1 / 0.9) puts a single right quiz from 1/2 exactly on the boundary and
  makes most items need two answers; wider (0.3 / 0.7) would let one self-report resolve an
  uninformed item. They are algorithm parameters, not caps: nothing counts questions.
- prior (probe@v5): not mycaas's uniform default but an informative one, the standard KST use of
  prior information (Heller & Repitsch 2012): each item i gets an initial mastery estimate p0_i
  and a state S gets P(S) ∝ Π_i p0_i^[i∈S] · (1 − p0_i)^[i∉S], restricted to the knowledge
  structure (states that are not prerequisite-closed get nothing) and normalised per connected
  part (`product_prior`, `Assessment.with_prior`). With every p0 = 1/2 it is exactly the uniform
  prior. An item without information has p0 = 1/2. Where p0 comes from (`level_p0`): the intake's
  gap identifier rates each knowledge point from the learner's own description
  (`IntakeResult.skill_gaps`: level + confidence):

  | level        | base p0 |
  |--------------|---------|
  | unknown / "" | none (1/2) |
  | unlearned    | 0.10    |
  | beginner     | 0.30    |
  | intermediate | 0.70    |
  | advanced     | 0.90    |

  and the confidence pulls it toward 1/2: p0 = 1/2 + w · (base − 1/2) with w = 1.0 (high), 0.7
  (medium, and a missing confidence), 0.4 (low). So advanced/high = 0.90 (prior odds 9, as
  strong as one quiz), advanced/medium = 0.78, advanced/low = 0.66, unlearned/low = 0.34. Drafts
  without levels fall back to the description's polarity (`DESCRIBED_P0`: "+" 0.7, "~" / "-" 0.3
  — exactly the weak update with BETA_SELF / ETA_SELF that probe@v4 applied), then to 1/2. The
  prior steers which item half-split picks and counts toward resolving an item *teach* (the
  full-evidence side); it never decides "known": the strong likelihood never sees it
  (goals/probe.py). The K evidence from before the goal then enters as ordinary `updating` calls
  before the first question.

There is no count of questions anywhere: an item is asked at most once (mycaas `repetition =
FALSE`), so the loop ends at the latest when the pool is empty.

----
mycaas is distributed under the MIT License (LICENSE: "YEAR: 2024 / COPYRIGHT HOLDER: mycaas
authors"):

MIT License

Copyright (c) 2024 mycaas authors

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES
OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
----

References: Doignon, J.-P., & Falmagne, J.-C. (1999). Knowledge Spaces. Springer. Falmagne, J.-C.,
& Doignon, J.-P. (2011). Learning Spaces. Springer. Heller, J., & Repitsch, C. (2012). Exploiting
prior information in stochastic knowledge assessment. Methodology. Donadello, I., et al. (2017).
ATS-PD. Educational and Psychological Measurement, 77(5). (ALEKS is the best-known product built on
this theory.)"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

BETA_QUIZ = 0.1
ETA_QUIZ = 0.1
BETA_SELF = 0.3
ETA_SELF = 0.3
BETA_DONT = 0.1
ETA_DONT = 0.1
BETA_K = 0.1
ETA_K = 0.1
STOP_P = 0.5
SC_TEACH = 0.2
SC_KNOWN = 0.8
LEVEL_BASE_P0 = {"unlearned": 0.10, "beginner": 0.30, "intermediate": 0.70, "advanced": 0.90}
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}
DEFAULT_CONFIDENCE_WEIGHT = 0.7          # no / an unrecognised confidence: as medium
DESCRIBED_P0 = {"+": 0.7, "~": 0.3, "-": 0.3}
TIE_REL_TOL = 1e-9                       # `Assessment.state(rel_tol=…)`: float-noise ties
MAX_ITEMS = 64
MAX_STATES = 2 ** 20

Termination = Literal["likelihood_maximization", "items_discrimination"]
Resolved = Literal["known", "teach"]


class StructureTooLarge(ValueError):
    """A connected part of the prerequisite map has more than MAX_ITEMS items or MAX_STATES
    knowledge states."""


# --- the knowledge structure (not in mycaas) ---------------------------------------------------

def components(items: Sequence[str], prereqs: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """The items split into connected parts of the (undirected) prerequisite graph, each in item
    order, ordered by their first item. Prerequisites outside `items` are ignored."""
    index = {q: i for i, q in enumerate(items)}
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for q in items:
        for p in prereqs.get(q, ()):
            if p in index:
                a, b = find(index[q]), find(index[p])
                if a != b:
                    parent[max(a, b)] = min(a, b)
    groups: dict[int, list[str]] = {}
    for i, q in enumerate(items):
        groups.setdefault(find(i), []).append(q)
    return list(groups.values())


def downsets(items: Sequence[str], prereqs: Mapping[str, Iterable[str]], *,
             limit: int = MAX_STATES) -> np.ndarray:
    """Every knowledge state of the surmise relation "p is a prerequisite of q" over `items`: the
    subsets closed under prerequisites, as bit masks (bit i = items[i]), ascending. Prerequisites
    outside `items` are ignored. StructureTooLarge past `limit` states or MAX_ITEMS items."""
    n = len(items)
    if n > MAX_ITEMS:
        raise StructureTooLarge(f"{n} items in one connected part (limit {MAX_ITEMS})")
    index = {q: i for i, q in enumerate(items)}
    direct = [0] * n
    for q in items:
        for p in prereqs.get(q, ()):
            if p in index and p != q:
                direct[index[q]] |= 1 << index[p]
    reach = [d | (1 << i) for i, d in enumerate(direct)]      # i and everything it needs
    changed = True
    while changed:                                             # transitive closure
        changed = False
        for i in range(n):
            r = reach[i]
            for j in range(n):
                if r >> j & 1 and reach[j] | r != r:
                    r |= reach[j]
            if r != reach[i]:
                reach[i], changed = r, True
    groups: dict[int, int] = {}                                # an item cycle is one group
    for i in range(n):
        bits = sum(1 << j for j in range(n) if reach[i] >> j & 1 and reach[j] >> i & 1)
        groups.setdefault(bits, i)
    order = sorted(groups.items(), key=lambda g: (reach[g[1]].bit_count(), g[1]))
    states = np.zeros(1, dtype=np.uint64)
    for bits, rep in order:                                    # prerequisites come first
        need = np.uint64(reach[rep] & ~bits)
        grown = states[(states & need) == need] | np.uint64(bits)
        if len(states) + len(grown) > limit:
            raise StructureTooLarge(f"more than {limit} knowledge states in one connected part "
                                    f"({n} items)")
        states = np.concatenate([states, grown])
    return np.sort(states)


def members(states: np.ndarray, i: int) -> np.ndarray:
    """mycaas's states[, i] == 1: which states contain item i."""
    return (states >> np.uint64(i)) & np.uint64(1) == np.uint64(1)


def uniform(states: np.ndarray) -> np.ndarray:
    """mycaas assessment(): `likelihood <- rep(1/nrow(states), nrow(states))`."""
    return np.full(len(states), 1.0 / len(states))


def level_p0(level: str, confidence: str = "") -> float | None:
    """The initial mastery estimate of a gap-identifier level (module doc table), pulled toward
    1/2 by the confidence; None for unknown / an unrecognised level (no information)."""
    base = LEVEL_BASE_P0.get(level.strip().casefold())
    if base is None:
        return None
    w = CONFIDENCE_WEIGHT.get(confidence.strip().casefold(), DEFAULT_CONFIDENCE_WEIGHT)
    return round(0.5 + w * (base - 0.5), 6)


def product_prior(states: np.ndarray, p0: Sequence[float]) -> np.ndarray:
    """P(S) ∝ Π_i p0_i^[i∈S] (1 − p0_i)^[i∉S] over the given states (one connected part; item i
    = bit i), normalised. Every p0 must be strictly between 0 and 1 (fail closed)."""
    if any(not 0 < p < 1 for p in p0):
        raise ValueError("a prior mastery estimate must be strictly between 0 and 1")
    log_w = np.zeros(len(states))
    for i, p in enumerate(p0):
        log_w += np.where(members(states, i), np.log(p), np.log1p(-p))
    w = np.exp(log_w - log_w.max())
    return w / w.sum()


# --- mycaas ports ------------------------------------------------------------------------------

def _check(likelihood: np.ndarray, states: np.ndarray, beta: float, eta: float) -> None:
    if not (0 <= beta <= 1 and 0 <= eta <= 1):
        raise ValueError("The error parameters eta and beta need to be between 0 and 1.")
    if len(states) != len(likelihood) or round(float(likelihood.sum())) != 1:
        raise ValueError(f"The cardinality of the knowledge structure is {len(states)} while the "
                         f"length of the likelihood is {len(likelihood)} which sums to "
                         f"{round(float(likelihood.sum()))}.")


def marginal(likelihood: np.ndarray, states: np.ndarray, i: int) -> float:
    """L_Kg in mycaas half_split: P(item i is in the learner's state)."""
    return float(likelihood[members(states, i)].sum())


def half_split(likelihood: np.ndarray, states: np.ndarray, n_items: int,
               pool: Sequence[int]) -> int:
    """mycaas half_split: the pool item minimising |2·L(q) − 1| (4 decimals); ties → the first in
    pool order (mycaas: a random one). mycaas zeroes L for items outside the pool, which makes
    them never minimal; restricting to the pool is the same."""
    if not pool:
        raise ValueError("empty pool")
    _check(likelihood, states, 0.0, 0.0)
    if any(not 0 <= q < n_items for q in pool):
        raise ValueError("pool item outside the structure")
    score = {q: round(abs(2 * marginal(likelihood, states, q) - 1), 4) for q in pool}
    best = min(score.values())
    return next(q for q in pool if score[q] == best)


def updating(likelihood: np.ndarray, states: np.ndarray, q: int, r_q: int, beta: float,
             eta: float) -> np.ndarray:
    """mycaas updating: the multiplicative rule after response r_q (1 right, 0 wrong) to item q
    with careless-error beta and lucky-guess eta (this item's)."""
    _check(likelihood, states, beta, eta)
    has = members(states, q)
    if r_q == 1:
        p = np.where(has, 1 - beta, eta)
    else:
        p = np.where(has, beta, 1 - eta)
    total = float((p * likelihood).sum())
    if total <= 0:
        raise ValueError("the response has zero likelihood under every state")
    return likelihood * p / total


def stopping_criterion(likelihood: np.ndarray, states: np.ndarray,
                       termination: Termination = "likelihood_maximization",
                       sc: Sequence[float] = (0.8,), *, n_items: int | None = None) -> bool:
    """mycaas stopping_criterion (its default SC = 0.8; `run_assessment` passes STOP_P; the probe
    uses `discriminated`). `n_items` (items_discrimination): the structure's item count (default:
    the highest bit set)."""
    if any(not 0 <= s <= 1 for s in sc):
        raise ValueError("The stopping criteria need to be between 0 and 1.")
    if termination == "likelihood_maximization":
        return bool((likelihood > sc[0]).any())
    if len(sc) != 2:
        raise ValueError("items_discrimination stopping criteria required exactly two values in SC.")
    n = n_items if n_items is not None else (int(states.max()).bit_length() if len(states) else 0)
    marg = [marginal(likelihood, states, i) for i in range(n)]
    return all(m > sc[1] or m < sc[0] for m in marg)


def discriminated(full: float, strong: float,
                  sc: tuple[float, float] = (SC_TEACH, SC_KNOWN)) -> Resolved | None:
    """items_discrimination for one item in the probe's asymmetric form (module doc): "teach" when
    the full-evidence marginal is ≤ sc[0], "known" when the full-evidence and the strong-evidence
    marginals are both ≥ sc[1], else None (unresolved). Teach is checked first."""
    lo, hi = sc
    if not 0 <= lo < hi <= 1:
        raise ValueError("items_discrimination needs 0 <= sc[0] < sc[1] <= 1")
    if full <= lo:
        return "teach"
    if full >= hi and strong >= hi:
        return "known"
    return None


# --- the assessment over a whole map (product of connected parts) -------------------------------

@dataclass(frozen=True)
class _Part:
    items: tuple[str, ...]
    states: np.ndarray
    likelihood: np.ndarray


@dataclass(frozen=True)
class Assessment:
    """The likelihood over the knowledge structure of `items` under `prereqs`, kept per connected
    part (module doc: exact). Immutable: `update` returns a new one."""

    items: tuple[str, ...]
    parts: tuple[_Part, ...]

    @classmethod
    def build(cls, items: Sequence[str], prereqs: Mapping[str, Iterable[str]], *,
              limit: int = MAX_STATES) -> "Assessment":
        items = tuple(dict.fromkeys(items))
        parts = []
        for comp in components(items, prereqs):
            states = downsets(comp, prereqs, limit=limit)
            parts.append(_Part(tuple(comp), states, uniform(states)))
        return cls(items, tuple(parts))

    def with_prior(self, p0: Mapping[str, float]) -> "Assessment":
        """The same structure with the product prior (module doc) as its likelihood: item → p0,
        items not in `p0` at 1/2. Replaces the current likelihood (call it before any update)."""
        parts = tuple(_Part(part.items, part.states,
                            product_prior(part.states, [p0.get(q, 0.5) for q in part.items]))
                      for part in self.parts)
        return Assessment(self.items, parts)

    def _where(self, item: str) -> tuple[int, int]:
        for k, part in enumerate(self.parts):
            if item in part.items:
                return k, part.items.index(item)
        raise KeyError(item)

    @property
    def n_states(self) -> int:
        """The size of the whole (product) structure."""
        return int(np.prod([len(p.states) for p in self.parts], dtype=object))

    def update(self, item: str, r: int, beta: float, eta: float) -> "Assessment":
        k, i = self._where(item)
        part = self.parts[k]
        new = _Part(part.items, part.states,
                    updating(part.likelihood, part.states, i, r, beta, eta))
        return Assessment(self.items, (*self.parts[:k], new, *self.parts[k + 1:]))

    def marginal(self, item: str) -> float:
        k, i = self._where(item)
        return marginal(self.parts[k].likelihood, self.parts[k].states, i)

    def prob_all(self, items: Iterable[str]) -> float:
        """P(every one of `items` is in the learner's state)."""
        want = set(items)
        p = 1.0
        for part in self.parts:
            mask = sum(1 << i for i, q in enumerate(part.items) if q in want)
            if mask:
                m = np.uint64(mask)
                p *= float(part.likelihood[(part.states & m) == m].sum())
        return p

    def pick(self, pool: Sequence[str]) -> str | None:
        """half_split over the whole map: the pool item whose marginal is closest to 1/2 (ties: the
        first in pool order); None when the pool is empty."""
        pool = [q for q in pool if q in self.items]
        if not pool:
            return None
        score = {q: round(abs(2 * self.marginal(q) - 1), 4) for q in pool}
        best = min(score.values())
        return next(q for q in pool if score[q] == best)

    def state(self, rel_tol: float = 0.0) -> tuple[frozenset[str], float]:
        """mycaas `kstate` / `P_kstate`: the most likely state (per part: the first maximum, as R's
        which.max) and its probability. `rel_tol` > 0: the first state within that relative
        distance of the maximum (states are ascending masks, so among tied states the first is
        the smallest: with only evidence of mastery on some items, exactly what they imply)."""
        out: set[str] = set()
        p = 1.0
        for part in self.parts:
            top = float(part.likelihood.max())
            j = (int(np.argmax(part.likelihood)) if rel_tol <= 0
                 else int(np.flatnonzero(part.likelihood >= top * (1 - rel_tol))[0]))
            p *= float(part.likelihood[j])
            mask = int(part.states[j])
            out |= {q for i, q in enumerate(part.items) if mask >> i & 1}
        return frozenset(out), p

    def stop(self, sc: float = STOP_P) -> bool:
        """stopping_criterion "likelihood_maximization" on the whole map: some state's probability
        (the most likely one's: the product of the parts' maxima) exceeds `sc`."""
        return self.state()[1] > sc


def run_assessment(a: Assessment, pool: Sequence[str],
                   respond: Callable[[str], tuple[int, float, float] | None], *,
                   sc: float = STOP_P) -> tuple[Assessment, list[tuple[str, int | None]]]:
    """mycaas assessment() with adaptive = TRUE, repetition = FALSE, likelihood_maximization:
    `respond(item)` → (r, beta, eta) or None (no evidence: the item is used up all the same).
    Returns the final assessment and the (item, r) sequence."""
    left = list(pool)
    asked: list[tuple[str, int | None]] = []
    while not a.stop(sc) and left:
        q = a.pick(left)
        assert q is not None
        left.remove(q)
        res = respond(q)
        asked.append((q, None if res is None else res[0]))
        if res is not None:
            a = a.update(q, *res)
    return a, asked
