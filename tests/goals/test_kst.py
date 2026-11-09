"""goals/kst.py: the mycaas port (half_split, updating, stopping_criterion, the assessment loop)
and the knowledge structure built from a prerequisite map. Pure numbers, no LLM."""

import numpy as np
import pytest

from evowiki.goals import kst
from evowiki.goals.kst import Assessment, StructureTooLarge

# mycaas's own example (Brancaccio, de Chiusole, Stefanutti 2023, Example 1), rows as bit masks
# (bit i = item i + 1 in R)
EXAMPLE = [[0, 0, 0, 0, 0], [0, 0, 0, 0, 1], [0, 0, 1, 0, 1], [0, 0, 0, 1, 1], [0, 0, 1, 1, 1],
           [1, 0, 1, 0, 1], [0, 1, 0, 1, 1], [1, 0, 1, 1, 1], [0, 1, 1, 1, 1], [1, 1, 0, 1, 1],
           [1, 1, 1, 1, 1]]
BETA = [.004, .03, .02, .01, .007]
ETA = [5e-06, 5e-05, 4e-05, .007, .08]


def masks(rows) -> np.ndarray:
    return np.array([sum(b << i for i, b in enumerate(r)) for r in rows], dtype=np.uint64)


def sets(states, items) -> set[frozenset[str]]:
    return {frozenset(q for i, q in enumerate(items) if int(s) >> i & 1) for s in states}


# --- the structure from a prerequisite map ---------------------------------------------------------

def test_downsets_of_a_small_map_match_hand_enumeration():
    # a ← b ← d, a ← c ← d (a diamond) plus a lone e
    items = ["a", "b", "c", "d"]
    pre = {"b": ["a"], "c": ["a"], "d": ["b", "c"]}
    got = sets(kst.downsets(items, pre), items)
    want = {frozenset(), frozenset("a"), frozenset("ab"), frozenset("ac"), frozenset("abc"),
            frozenset("abcd")}
    assert got == want


def test_a_region_above_its_members_and_a_prerequisite_region():
    # region R needs members m1, m2; region S needs R and its member s1
    items = ["R", "S", "m1", "m2", "s1"]
    pre = {"R": ["m1", "m2"], "S": ["R", "s1"]}
    got = sets(kst.downsets(items, pre), items)
    free = [frozenset(x) for x in ([], ["m1"], ["m2"], ["m1", "m2"])]
    want = {f | s for f in free for s in (frozenset(), frozenset(["s1"]))}
    want |= {frozenset(["R", "m1", "m2"]), frozenset(["R", "m1", "m2", "s1"]),
             frozenset(["R", "S", "m1", "m2", "s1"])}
    assert got == want and len(want) == 11


def test_a_cycle_means_mastered_together_and_unknown_prereqs_are_ignored():
    items = ["a", "b", "c"]
    got = sets(kst.downsets(items, {"a": ["b"], "b": ["a"], "c": ["a", "zz"]}), items)
    assert got == {frozenset(), frozenset("ab"), frozenset("abc")}


def test_too_many_states_fails_closed():
    items = [f"x{i}" for i in range(11)]                 # no prerequisites: 2^11 states
    with pytest.raises(StructureTooLarge):
        kst.downsets(items, {}, limit=2 ** 10)
    assert len(kst.downsets(items, {}, limit=2 ** 11)) == 2 ** 11
    with pytest.raises(StructureTooLarge):
        kst.downsets([f"y{i}" for i in range(kst.MAX_ITEMS + 1)], {})


def test_components_factor_the_structure_exactly():
    items = ["a", "b", "c", "d"]
    pre = {"b": ["a"], "d": ["c"]}
    assert kst.components(items, pre) == [["a", "b"], ["c", "d"]]
    a = Assessment.build(items, pre)
    assert a.n_states == 9 and [len(p.states) for p in a.parts] == [3, 3]
    whole_states = kst.downsets(items, pre)              # one enumeration of the same map
    whole = kst.uniform(whole_states)
    for q, r in (("b", 1), ("c", 0), ("a", 1)):
        a = a.update(q, r, 0.1, 0.2)
        whole = kst.updating(whole, whole_states, items.index(q), r, 0.1, 0.2)
    for i, q in enumerate(items):
        assert a.marginal(q) == pytest.approx(kst.marginal(whole, whole_states, i))
    assert a.state()[1] == pytest.approx(float(whole.max()))


# --- the mycaas functions ----------------------------------------------------------------------

def test_half_split_as_in_mycaas_example():
    states = masks(EXAMPLE)
    lik = np.zeros(11)
    lik[[5, 7, 10]] = 1 / 3
    # Q_pool c(2,4,5): L = 1/3, 2/3, 1 → items 2 and 4 tie; ties take the first (mycaas: random)
    assert kst.half_split(lik, states, 5, [1, 3, 4]) == 1
    assert kst.half_split(lik, states, 5, [3, 4]) == 3
    with pytest.raises(ValueError):
        kst.half_split(np.full(3, 0.5), states, 5, [1])     # likelihood does not fit


def test_updating_matches_a_hand_calculation():
    states = masks(EXAMPLE)
    lik = kst.uniform(states)
    out = kst.updating(lik, states, 2, 1, BETA[2], ETA[2])   # mycaas example: q = 3, r_q = 1
    has = [bool(r[2]) for r in EXAMPLE]                  # 6 of the 11 states contain item 3
    num = np.array([(1 - BETA[2]) if h else ETA[2] for h in has]) / 11
    assert out == pytest.approx(num / num.sum())
    assert out[has.index(True)] == pytest.approx(0.98 / (6 * 0.98 + 5 * 4e-05))
    wrong = kst.updating(lik, states, 2, 0, 0.1, 0.2)
    assert wrong[has.index(True)] == pytest.approx(0.1 / (6 * 0.1 + 5 * 0.8))
    with pytest.raises(ValueError):
        kst.updating(lik, states, 2, 1, 1.4, 0.1)        # eta / beta outside [0, 1]
    with pytest.raises(ValueError):
        kst.updating(np.array([1.0] + [0.0] * 10), states, 2, 1, 0.0, 0.0)  # impossible answer


def test_stopping_criterion_both_rules():
    states = masks(EXAMPLE)
    lik = np.zeros(11)
    lik[7], lik[10] = .49, .51
    assert kst.stopping_criterion(lik, states, "likelihood_maximization", (0.5,))
    assert not kst.stopping_criterion(lik, states, "likelihood_maximization", (0.8,))
    assert not kst.stopping_criterion(lik, states, "items_discrimination", (0.2, 0.8), n_items=5)
    lik2 = np.zeros(11)
    lik2[10] = 1.0
    assert kst.stopping_criterion(lik2, states, "items_discrimination", (0.2, 0.8), n_items=5)
    with pytest.raises(ValueError):
        kst.stopping_criterion(lik, states, "items_discrimination", (0.5,))


# --- the loop ----------------------------------------------------------------------------------

ML_ITEMS = ["LA", "PR", "OPT", "SL", "c1", "c2", "c3", "c4", "c5", "c6", "c7"]
ML_PRE = {"c2": ["c1"], "c4": ["c3"], "c6": ["c2", "c5"], "LA": ["c1", "c2"], "PR": ["c3", "c4"],
          "OPT": ["c5", "LA"], "SL": ["c6", "c7", "LA", "PR", "OPT"]}


def run(true: set[str]):
    a = Assessment.build(ML_ITEMS, ML_PRE)
    return kst.run_assessment(a, ML_ITEMS, lambda q: (int(q in true), kst.BETA_QUIZ,
                                                      kst.ETA_QUIZ))


def test_half_split_picks_the_item_closest_to_one_half():
    a = Assessment.build(["a", "b", "c"], {"b": ["a"], "c": ["b"]})    # a chain: 4 states
    assert [round(a.marginal(q), 2) for q in "abc"] == [0.75, 0.5, 0.25]
    assert a.pick(["a", "b", "c"]) == "b" and a.pick(["a", "c"]) == "a"   # tie a / c → first
    assert a.pick([]) is None


def test_all_right_and_all_wrong_stop_before_asking_everything_and_find_the_state():
    for true in (set(ML_ITEMS), set()):
        a, asked = run(true)
        assert a.stop() and len(asked) < len(ML_ITEMS)
        assert a.state()[0] == frozenset(true)
        assert len({q for q, _ in asked}) == len(asked)      # never the same item twice


def test_mixed_answers_end_when_the_pool_is_empty_at_the_latest():
    true = {"c1", "c2", "c3", "c5", "c7"}
    a, asked = run(true)
    assert len(asked) <= len(ML_ITEMS) and a.state()[0] == frozenset(true)


def test_a_self_report_moves_less_than_a_quiz():
    a = Assessment.build(["a", "b"], {"b": ["a"]})
    by_self = a.update("a", 1, kst.BETA_SELF, kst.ETA_SELF).marginal("a")
    by_quiz = a.update("a", 1, kst.BETA_QUIZ, kst.ETA_QUIZ).marginal("a")
    assert a.marginal("a") < by_self < by_quiz
    prob_all = a.update("b", 1, 0.1, 0.1).prob_all(["a", "b"])
    assert prob_all == pytest.approx(a.update("b", 1, 0.1, 0.1).marginal("b"))


# --- the informative prior (probe@v5) --------------------------------------------------------------

def test_level_p0_table_and_confidence_pull_toward_one_half():
    assert kst.level_p0("advanced", "high") == 0.9 and kst.level_p0("unlearned", "high") == 0.1
    assert kst.level_p0("beginner", "high") == 0.3 and kst.level_p0("intermediate", "high") == 0.7
    assert kst.level_p0("advanced", "medium") == pytest.approx(0.78)
    assert kst.level_p0("advanced", "low") == pytest.approx(0.66)
    assert kst.level_p0("advanced", "") == kst.level_p0("advanced", "medium")   # missing: medium
    assert kst.level_p0("Advanced ", "HIGH") == 0.9
    assert kst.level_p0("unknown", "high") is None and kst.level_p0("", "") is None
    assert kst.level_p0("expert", "high") is None


def test_product_prior_matches_a_hand_calculation():
    # region R needs m1, m2; m2 needs m1: states {}, {m1}, {m1,m2}, {m1,m2,R}
    items = ["R", "m1", "m2"]
    a = Assessment.build(items, {"R": ["m1", "m2"], "m2": ["m1"]})
    p = {"R": 0.9, "m1": 0.9, "m2": 0.6}
    got = a.with_prior(p)
    w = {frozenset(): .1 * .1 * .4, frozenset({"m1"}): .1 * .9 * .4,
         frozenset({"m1", "m2"}): .1 * .9 * .6, frozenset({"R", "m1", "m2"}): .9 * .9 * .6}
    total = sum(w.values())
    part = got.parts[0]
    for mask, lik in zip(part.states, part.likelihood, strict=True):
        s = frozenset(q for i, q in enumerate(part.items) if int(mask) >> i & 1)
        assert lik == pytest.approx(w[s] / total)
    assert got.marginal("R") == pytest.approx(w[frozenset({"R", "m1", "m2"})] / total)
    assert got.marginal("m1") == pytest.approx(1 - w[frozenset()] / total)


def test_product_prior_of_one_half_is_uniform_and_parts_stay_exact():
    items = ["a", "b", "c", "d"]
    pre = {"b": ["a"], "d": ["c"]}                           # two parts
    a = Assessment.build(items, pre)
    half = a.with_prior({})
    assert all(np.allclose(p.likelihood, kst.uniform(p.states)) for p in half.parts)
    p0 = {"a": 0.8, "b": 0.3, "c": 0.2, "d": 0.6}
    whole = Assessment(tuple(items), (kst._Part(tuple(items), kst.downsets(items, pre),
                                                kst.uniform(kst.downsets(items, pre))),))
    whole = whole.with_prior(p0)
    split = a.with_prior(p0)
    for q in items:
        assert split.marginal(q) == pytest.approx(whole.marginal(q))
    with pytest.raises(ValueError):
        a.with_prior({"a": 1.0})


def test_state_with_a_tolerance_takes_the_smallest_of_tied_states():
    a = Assessment.build(["R", "m1", "m2"], {"R": ["m1", "m2"], "m2": ["m1"]})
    assert a.state(rel_tol=kst.TIE_REL_TOL)[0] == frozenset()   # no evidence: nothing
    right_m2 = a.update("m2", 1, kst.BETA_QUIZ, kst.ETA_QUIZ)
    assert right_m2.state(rel_tol=kst.TIE_REL_TOL)[0] == {"m1", "m2"}   # what m2 implies


# --- items_discrimination in the probe's asymmetric form (probe@v6) ------------------------------

def test_discriminated_is_asymmetric():
    """teach: the full marginal <= SC_TEACH (whatever the strong one says); known: both >= SC_KNOWN;
    otherwise unresolved. Bounds inclusive."""
    assert (kst.SC_TEACH, kst.SC_KNOWN) == (0.2, 0.8)
    assert kst.discriminated(0.2, 0.99) == "teach" and kst.discriminated(0.05, 0.5) == "teach"
    assert kst.discriminated(0.8, 0.8) == "known" and kst.discriminated(0.99, 0.95) == "known"
    assert kst.discriminated(0.95, 0.79) is None            # full only: a prior / self-report
    assert kst.discriminated(0.5, 0.9) is None and kst.discriminated(0.21, 0.0) is None
    with pytest.raises(ValueError):
        kst.discriminated(0.5, 0.5, (0.8, 0.2))


def test_one_quiz_resolves_an_item_at_one_half_one_self_report_does_not():
    """Hand-checked marginals on one free item (states {}, {x}, uniform): a right quiz → 0.9,
    a wrong one → 0.1 (resolved either way); a self-report → 0.7 / 0.3 (unresolved)."""
    a = Assessment.build(["x"], {})
    right = a.update("x", 1, kst.BETA_QUIZ, kst.ETA_QUIZ).marginal("x")
    wrong = a.update("x", 0, kst.BETA_QUIZ, kst.ETA_QUIZ).marginal("x")
    assert right == pytest.approx(0.9) and wrong == pytest.approx(0.1)
    assert kst.discriminated(right, right) == "known" and kst.discriminated(wrong, wrong) == "teach"
    know = a.update("x", 1, kst.BETA_SELF, kst.ETA_SELF).marginal("x")
    dont = a.update("x", 0, kst.BETA_SELF, kst.ETA_SELF).marginal("x")
    assert know == pytest.approx(0.7) and dont == pytest.approx(0.3)
    assert kst.discriminated(know, 0.5) is None and kst.discriminated(dont, 0.5) is None
    # a self-report agreeing with the prior resolves teach: unlearned / medium 0.22 → 「没学过」
    p0 = kst.level_p0("unlearned", "medium")
    low = a.with_prior({"x": p0}).update("x", 0, kst.BETA_SELF, kst.ETA_SELF).marginal("x")
    assert low == pytest.approx(1 / (1 + (1 - p0) / p0 * 7 / 3)) and kst.discriminated(low, 0.5) \
        == "teach"


def test_a_chain_top_answered_once_stays_below_sc_known_on_the_strong_side():
    """The uniform start over a chain m1 ← m2 ← s (states {}, {m1}, {m1,m2}, {m1,m2,s}) is not
    neutral per item: one right quiz on s gives s 0.9 / (0.3 + 0.9) = 0.75 < 0.8, its
    prerequisites 1.0 / 1.2 and 1.1 / 1.2."""
    a = Assessment.build(["m1", "m2", "s"], {"m2": ["m1"], "s": ["m2"]})
    assert a.marginal("m1") == pytest.approx(0.75) and a.marginal("s") == pytest.approx(0.25)
    r = a.update("s", 1, kst.BETA_QUIZ, kst.ETA_QUIZ)
    assert r.marginal("s") == pytest.approx(0.75)
    assert r.marginal("m2") == pytest.approx(1.0 / 1.2) and r.marginal("m1") == pytest.approx(1.1 / 1.2)
