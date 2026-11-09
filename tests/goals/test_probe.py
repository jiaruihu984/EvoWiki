"""goals/probe.py: the KST diagnosis picks every question (the node and the cell), updates on
every answer and decides when to stop; the scripted assistant only writes the picked question
(stage 3 T11: ask_check). Offline, no model."""

import json

from evowiki.goals import kst
from evowiki.goals.contracts import Aspect
from evowiki.goals.diagnostic import IDK
from evowiki.goals.probe import (
    ALL_EVIDENCED_WHY,
    RESOLVED_WHY,
    TOO_LARGE_WHY,
    ProbeNode,
    ProbeQuestion,
    ProbeRound,
    _State,
    check_answers,
    next_round,
    probe_observations,
    structure,
)
from evowiki.goals.selfreport import SELF_CLAIM
from tests.wikiapp.goal_script import asked

from .fakes_intake import FakeK

A = Aspect
# regions are topics (no cells of their own); their members are concept items too
CONCEPTS = {"m1": (A.TERM,), "m2": (A.PROCEDURE,), "p1": (A.TERM,), "s1": (A.PURPOSE,)}
LA = ProbeNode(id="hub-la", name="线性代数", cells=("m1.term", "m2.procedure"), region=True,
               members=(("m1", "矩阵"), ("m2", "矩阵乘法")))
PR = ProbeNode(id="hub-pr", name="概率论", cells=("p1.term",), region=True,
               members=(("p1", "条件概率"),))
SL = ProbeNode(id="hub-sl", name="监督学习", cells=("s1.purpose",), region=True, target=True,
               members=(("s1", "线性回归"),), prereqs=("hub-la", "hub-pr"))
MEMBERS = [ProbeNode(id="m1", name="矩阵", cells=("m1.term",)),
           ProbeNode(id="m2", name="矩阵乘法", cells=("m2.procedure",), prereqs=("m1",)),
           ProbeNode(id="p1", name="条件概率", cells=("p1.term",)),
           ProbeNode(id="s1", name="线性回归", cells=("s1.purpose",), prereqs=("m2",))]
NODES = [LA, PR, SL, *MEMBERS]
SELF_LA = {"kind": "self_report", "target": "hub-la", "stem": "学过线性代数吗？",
           "options": ["学过，还会算", "学过，忘得差不多了", "没学过", "说不清"],
           "means": ["know", "forgot", "dont", "unsure"]}


class FakeLLM:
    """The scripted assistant (stage 3 T11: the assistant writes the picked question with
    ask_check): queued outputs in the old probe model's shape (`{"questions": [...]}`; the item for
    the picked node is used), then the default question (goal_script.build_question). It is never
    a model call: `prompts` stays empty."""

    def __init__(self, *outs, auto: bool = True):
        self.outs = [o for o in outs if isinstance(o, dict)]
        self.prompts: list[str] = []

    def write(self, r):
        return asked(r, self.outs.pop(0) if self.outs else None)


def asked_round(llm, rounds, **kw) -> ProbeRound:
    """The next round, asked by the scripted assistant. Consecutive self-report picks (one block
    a pick in the chat) are gathered into one round here, as the old coarse round 1 asked them,
    so the KST cases below keep their rounds; every quiz is a round of its own."""
    r = next_round(rounds=rounds, pick_id=f"d:{len(rounds) + 1}", **kw)
    if r.done or r.pick.intent != "self_report":
        return llm.write(r)
    qs = []
    while not r.done and r.pick.intent == "self_report":
        qs.append(llm.write(r).questions[0])
        r = next_round(rounds=[*rounds, ProbeRound(questions=tuple(qs), done=False)],
                       pick_id=f"d:{len(rounds) + 1}", **kw)
    first = next_round(rounds=rounds, pick_id=f"d:{len(rounds) + 1}", **kw)
    return first.model_copy(update={"questions": tuple(qs)})


def _round(llm, rounds=(), nodes=NODES, described=()):
    return asked_round(llm, list(rounds), nodes=nodes, k=FakeK(CONCEPTS), described=described)


def answered(r: ProbeRound, pick) -> ProbeRound:
    return r.model_copy(update={"answers": tuple(pick(q) for q in r.questions)})


def right(q):
    return q.answer if q.kind == "quiz" else 0


def test_structure_has_prereq_regions_and_only_the_presupposed_members_of_answered_mastery():
    """probe@v6: a region does not sit above its members by default; an answered mastery
    question puts it above the members it presupposes (an unanswered one does not count)."""
    items, pre = structure(NODES)
    assert items == ("hub-la", "hub-pr", "hub-sl", "m1", "m2", "p1", "s1")
    assert pre["hub-la"] == set() and pre["hub-sl"] == {"hub-la", "hub-pr"}
    assert pre["m2"] == {"m1"} and pre["s1"] == {"m2"} and pre["m1"] == set()
    q = ProbeQuestion(kind="quiz", target="hub-la", stem="?", options=("a", "b", "c"), answer=0,
                      intent="mastery", node="hub-la", presupposes=("m1", "p1"))
    asked = ProbeRound(questions=(q,), done=False)
    assert structure(NODES, [asked])[1]["hub-la"] == set()                # not answered yet
    pre = structure(NODES, [asked.model_copy(update={"answers": (1,)})])[1]
    assert pre["hub-la"] == {"m1"} and pre["hub-sl"] == {"hub-la", "hub-pr"}   # p1: not a member


def test_round_one_is_one_self_report_per_region_and_the_prompt_only_asks_for_them():
    llm = FakeLLM()
    r = _round(llm)
    assert [(q.kind, q.intent, q.node) for q in r.questions] == [
        ("self_report", "self_report", n) for n in ("hub-la", "hub-pr", "hub-sl")]
    assert r.title == "了解你的基础" and not r.done
    assert [d.decision for d in r.decisions] == ["teach"] * len(NODES)   # nothing known yet


def test_then_one_quiz_per_round_on_the_half_split_item_until_it_stops():
    llm = FakeLLM()
    rounds = [answered(_round(llm), right)]
    asked = []
    while True:
        r = _round(llm, rounds)
        if r.done:
            break
        assert len(r.questions) == 1 and r.questions[0].kind == "quiz"
        q = r.questions[0]
        asked.append((q.intent, q.node))
        assert r.pick.node == q.node and (r.pick.cell == q.cell or not r.pick.cell)
        rounds.append(answered(r, right))
    assert len({n for _, n in asked}) == len(asked) < len(NODES)
    assert all(i == ("mastery" if n.startswith("hub-") else "goal_use") for i, n in asked)
    assert r.questions == () and r.why == RESOLVED_WHY.format(teach=0.2, known=0.8)
    assert {d.node: d.decision for d in r.decisions} == dict.fromkeys(
        [n.id for n in NODES], "known")


def test_the_first_quiz_is_the_item_closest_to_one_half():
    llm = FakeLLM()
    r1 = answered(_round(llm), lambda q: 3)                 # every self-report 说不清: no evidence
    r2 = _round(llm, [r1])
    a = kst.Assessment.build(*structure(NODES))
    expect = a.pick([n.id for n in NODES])
    assert r2.questions[0].node == expect
    assert f"{a.marginal(expect):.0%}" in r2.why


def test_a_bank_of_items_keeps_only_the_picked_one_and_logs_the_rest():
    r1 = answered(_round(FakeLLM()), lambda q: 3)
    pick = _round(FakeLLM(), [r1]).questions[0]
    bank = {"questions": [
        {"kind": "quiz", "target": c, "stem": f"{c}？", "options": ["对", "错一", "错二"],
         "answer": 0} for n in NODES for c in n.cells]}
    r2 = _round(FakeLLM(bank), [r1])
    assert len(r2.questions) == 1 and r2.questions[0].node == pick.node
    assert {d.reason for d in r2.dropped} <= {"not_picked", "repeated_in_round"}


def test_nothing_with_k_evidence_is_asked_and_it_enters_as_prior_evidence():
    nodes = [LA.model_copy(update={"evidenced": ("m1",)}), PR, SL,
             MEMBERS[0].model_copy(update={"evidenced": ("m1",), "k_prior": "known"}),
             *MEMBERS[1:]]
    llm = FakeLLM()
    r = _round(llm, nodes=nodes)
    assert [q.node for q in r.questions] == ["hub-pr", "hub-sl"]   # 线性代数 has evidence
    assert {d.node: d.source for d in r.decisions}["m1"] == "k"
    rounds = [answered(r, lambda q: 3)]
    while not (r := _round(llm, rounds, nodes=nodes)).done:
        assert r.questions[0].node != "m1"
        rounds.append(answered(r, right))


def test_every_node_with_k_evidence_means_no_call():
    nodes = [n.model_copy(update={"k_prior": "known"}) for n in NODES]
    llm = FakeLLM()
    r = _round(llm, nodes=nodes)
    assert r.done and r.questions == () and r.why == ALL_EVIDENCED_WHY
    assert all(d.decision == "known" and d.source == "k" for d in r.decisions)


def test_the_description_is_weak_evidence_and_blocks_the_self_report():
    r = _round(FakeLLM(), described={"m1": "~", "m2": "~"})
    assert [q.node for q in r.questions] == ["hub-pr", "hub-sl"]
    src = {d.node: d.source for d in r.decisions}
    assert src["m1"] == src["m2"] == "described"
    plain = kst.Assessment.build(*structure(NODES))
    by_desc = plain.update("m1", 0, kst.BETA_SELF, kst.ETA_SELF)
    p = {d.node: d.p for d in r.decisions}
    assert p["m1"] < plain.marginal("m1") and p["m1"] > by_desc.update(
        "m1", 0, kst.BETA_QUIZ, kst.ETA_QUIZ).marginal("m1")
    old_ids_only = _round(FakeLLM(), described=("m1",))      # a pre-v4 draft: ids only
    assert [q.node for q in old_ids_only.questions] == ["hub-pr", "hub-sl"]


def test_a_map_too_large_to_enumerate_ends_without_a_call():
    many = [ProbeNode(id=f"x{i}", name=f"x{i}", cells=(f"x{i}.term",)) for i in range(21)]
    llm = FakeLLM()
    assert len(_round(llm, nodes=many).questions) == 1      # 21 unrelated items: 21 small parts
    many.append(ProbeNode(id="R", name="R", cells=("R.term",),     # one part: 2^21 + 1
                          prereqs=tuple(n.id for n in many)))
    llm = FakeLLM()
    r = _round(llm, nodes=many)
    assert r.done and r.questions == () and r.decisions == ()
    assert r.why == TOO_LARGE_WHY                            # no internals in the shown reason


def test_a_map_too_large_to_enumerate_is_diagnosed_over_its_regions():
    many = [ProbeNode(id=f"x{i}", name=f"x{i}", cells=(f"x{i}.term",)) for i in range(21)]
    many.append(ProbeNode(id="y", name="y", cells=("y.term",),     # one part: 2^21 + 1
                          prereqs=tuple(n.id for n in many)))
    region = ProbeNode(id="R", name="R", region=True, cells=tuple(c for n in many for c in n.cells),
                       members=tuple((n.id, n.name) for n in many))
    llm = FakeLLM()
    r = _round(llm, nodes=[region, *many])
    assert not r.done and r.questions and {q.node for q in r.questions} == {"R"}
    assert [d.node for d in r.decisions] == ["R"]


def test_region_self_report_is_weak_evidence_on_its_members_quiz_is_strong():
    r = _round(FakeLLM())
    cells = {n.id: n.cells for n in NODES}
    obs = probe_observations(r, (1, IDK, 3), draft_id="dr1", round_index=1, cells=cells)
    assert {o.cell for o in obs} == {"m1.term", "m2.procedure"}
    assert all(o.polarity == "+" and o.strength == SELF_CLAIM["studied"][1] and o.kind == "self"
               and o.quote == "学过，忘得差不多了" and o.evidence_ref == "probeself:dr1:1:0"
               for o in obs)
    rounds = [answered(r, lambda q: 3)]
    while not (q := _round(FakeLLM(), rounds)).done and q.questions[0].intent != "goal_use":
        rounds.append(answered(q, right))
    quiz = q.questions[0]
    obs = probe_observations(q, (quiz.answer,), draft_id="dr1", round_index=2, cells=cells)
    assert [(o.cell, o.polarity, o.kind, o.evidence_ref) for o in obs] == [
        (quiz.cell, "+", "quiz", "probe:dr1:2:0")]


def test_a_mastery_quiz_on_a_cell_less_region_writes_no_observation_and_decides_it():
    nodes = [LA, *MEMBERS[:2]]
    llm = FakeLLM()
    rounds = [answered(_round(llm, nodes=nodes), right)]     # 「学过，还会算」
    r = _round(llm, rounds, nodes=nodes)
    q = r.questions[0]
    assert (q.intent, q.node, q.target, q.cell) == ("mastery", "hub-la", "hub-la", "")
    assert r.title == "确认掌握程度"
    assert probe_observations(r, (q.answer,), draft_id="d", round_index=2,
                              cells={n.id: n.cells for n in nodes}) == ()
    end = _round(llm, [*rounds, answered(r, right)], nodes=nodes)
    assert end.done and [(d.node, d.decision, d.source) for d in end.decisions] == [
        ("hub-la", "known", "quiz"), ("m1", "known", "inferred"), ("m2", "known", "inferred")]


def test_check_answers():
    r = _round(FakeLLM())
    assert check_answers(r, [0, 0, 0]) is None and check_answers(r, [IDK, IDK, IDK]) is None
    assert "数量" in check_answers(r, [0]) and "第 1 题" in check_answers(r, [4, 0, 0])


def test_rounds_round_trip_as_json_and_v3_rounds_still_load_and_count():
    r = _round(FakeLLM())
    assert type(r).model_validate(json.loads(r.model_dump_json())) == r
    v3 = {"questions": [{"kind": "quiz", "target": "m2.procedure", "stem": "2×3 乘 3×4 得到？",
                         "options": ["2×4", "3×3", "2×3", "不能相乘"], "answer": 0,
                         "means": [], "verified": True},
                        {"kind": "self_report", "target": "hub-pr", "stem": "学过概率论吗？",
                         "options": ["学过", "没学过"], "means": ["know", "dont"],
                         "answer": -1, "verified": False}],
          "done": False, "why": "", "title": "了解你的基础",
          "known": [{"target": "hub-la", "why": "x"}], "answers": [0, 1], "dropped": []}
    old = ProbeRound.model_validate(v3)
    assert old.questions[0].intent == "" and old.questions[0].cell == "m2.procedure"
    nxt = _round(FakeLLM(), [old])
    assert nxt.questions and nxt.questions[0].kind == "quiz"       # no second self-report round
    src = {d.node: d.source for d in nxt.decisions}
    # 概率论 「没学过」 is a weak wrong on its member p1 (the cells the self-report is written to)
    assert src["m2"] == "quiz" and src["p1"] == "self_report" and src["hub-pr"] == "self_report"
    assert nxt.questions[0].node != "m2"                          # quizzed in v3: not again


# --- probe@v5: plain text, known only from answers, the opening's decisions -------------------

def test_self_report_and_prior_alone_never_make_a_node_known():
    """「学过，还会算」 on 线性代数 and an advanced / high rating put it in the most likely state of
    the full likelihood, but nothing was answered: the diagnosis ends (model unusable) with
    everything to teach (the open round's decisions: nothing is known before an answer)."""
    nodes = [LA.model_copy(update={"p0": kst.level_p0("advanced", "high")}),
             *(m.model_copy(update={"p0": kst.level_p0("advanced", "high")})
               for m in MEMBERS[:2])]
    r = _round(FakeLLM(), nodes=nodes)
    assert all(d.decision == "teach" and d.source == "described" for d in r.decisions)
    plain = [LA, *MEMBERS[:2]]
    r1 = answered(_round(FakeLLM(), nodes=plain), right)            # self-report: know
    end = _round(FakeLLM(), [r1], nodes=plain)
    assert [(d.node, d.decision, d.source) for d in end.decisions] == [   # on every member too
        ("hub-la", "teach", "self_report"), ("m1", "teach", "self_report"),
        ("m2", "teach", "self_report")]
    assert {d.node: d.p for d in end.decisions}["m1"] > 0.5      # likely, but not verified
    described = _round(FakeLLM({"questions": []}, {"questions": []}, auto=False), nodes=plain,
                       described={"m1": "+", "m2": "+"})
    assert all(d.decision == "teach" for d in described.decisions)


def test_the_openings_decisions_are_kept_and_only_unevidenced_members_are_asked():
    """In an entered region (goal.py), members the opening decided are fixed: never asked, the
    decision kept as it is even against later evidence; the others may be asked."""
    nodes = [MEMBERS[0].model_copy(update={"decided": "teach", "evidenced": ("m1",)}),
             MEMBERS[1].model_copy(update={"decided": "known", "evidenced": ("m2",)}),
             MEMBERS[2], MEMBERS[3]]
    llm = FakeLLM()
    rounds, asked = [], []
    while not (r := _round(llm, rounds, nodes=nodes)).done:
        asked += [q.node for q in r.questions]
        rounds.append(answered(r, right))
    assert set(asked) <= {"p1", "s1"} and asked
    d = {x.node: (x.decision, x.source) for x in r.decisions}
    assert d["m1"] == ("teach", "opening") and d["m2"] == ("known", "opening")
    fixed = [n.model_copy(update={"decided": "known"}) for n in MEMBERS]
    llm = FakeLLM()
    r = _round(llm, nodes=fixed)
    assert r.done and r.why == ALL_EVIDENCED_WHY
    assert all(x.source == "opening" for x in r.decisions)


# --- probe@v6: items_discrimination stop, presupposed members ----------------------------------

X, Y, Z = (ProbeNode(id=i, name=i, cells=(f"{i}.term",)) for i in ("x", "y", "z"))


def wrong(q):
    return (q.answer + 1) % len(q.options)


def test_the_stop_is_asymmetric_items_discrimination():
    """x no estimate (1/2), y unlearned / high (0.1), z advanced / high (0.9); three independent
    items, so every marginal is checked by hand. y: full 0.1 <= 0.2 -> resolved teach from the
    prior alone, never asked. z: full 0.9 but strong 1/2 -> unresolved (a prior never resolves
    known), a claim, asked first; right -> full odds 9 * 9 = 81 -> 81/82, strong 0.9 -> known.
    x: 1/2, asked; wrong -> 0.1 -> teach. Then everything is resolved and the diagnosis ends."""
    nodes = [X, Y.model_copy(update={"p0": kst.level_p0("unlearned", "high")}),
             Z.model_copy(update={"p0": kst.level_p0("advanced", "high")})]
    st = _State(nodes, (), {})
    assert [st.status(i) for i in ("x", "y", "z")] == [None, "teach", None]
    assert abs(st.a.marginal("y") - 0.1) < 1e-9 and abs(st.strong.marginal("z") - 0.5) < 1e-9
    llm = FakeLLM()
    r1 = _round(llm, nodes=nodes)
    assert [q.node for q in r1.questions] == ["z"] and "确认" in r1.why and "90%" in r1.why
    r1 = answered(r1, right)
    st = _State(nodes, [r1], {})
    assert abs(st.a.marginal("z") - 81 / 82) < 1e-9 and abs(st.strong.marginal("z") - 0.9) < 1e-9
    assert st.status("z") == "known" and st.candidates() == ["x"]
    r2 = _round(llm, [r1], nodes=nodes)
    assert [q.node for q in r2.questions] == ["x"] and "最拿不准（会的概率 50%）" in r2.why
    end = _round(llm, [r1, answered(r2, wrong)], nodes=nodes)
    assert end.done and end.why == RESOLVED_WHY.format(teach=kst.SC_TEACH, known=kst.SC_KNOWN)
    assert [(d.node, d.decision, d.source) for d in end.decisions] == [
        ("x", "teach", "quiz"), ("y", "teach", "described"), ("z", "known", "quiz")]


def test_many_near_certain_items_end_the_diagnosis_without_a_likely_single_state():
    """Live run 4: 26 items each about 95% decided, so no single state passed 0.5 and probe@v5's
    likelihood_maximization kept asking items at 3%, 2%, 2%, 99%, 0%. Here: 20 items rated at
    0.05 (resolved teach) and one at 1/2 — only that one is asked, then it ends although neither
    likelihood's most likely state passes 0.5."""
    near = [ProbeNode(id=f"n{i}", name=f"n{i}", cells=(f"n{i}.term",), p0=0.05)
            for i in range(20)]
    nodes = [*near, X]
    llm = FakeLLM()
    r = _round(llm, nodes=nodes)
    assert [q.node for q in r.questions] == ["x"]                 # never a near-certain item
    rounds = [answered(r, right)]
    end = _round(llm, rounds, nodes=nodes)
    st = _State(nodes, rounds, {})
    assert st.a.state()[1] < kst.STOP_P and st.strong.state()[1] < kst.STOP_P
    assert end.done and end.why == RESOLVED_WHY.format(teach=0.2, known=0.8)


def test_a_near_certain_item_is_never_picked_while_an_unresolved_one_is_left():
    """n0 (p0 0.15) is resolved teach; k (p0 0.97) is near-certain but only by the prior, so it is
    unresolved: a claim, asked first; then x. n0 is never asked."""
    nodes = [ProbeNode(id="n0", name="n0", cells=("n0.term",), p0=0.15),
             ProbeNode(id="k", name="k", cells=("k.term",), p0=0.97), X]
    st = _State(nodes, (), {})
    assert st.status("n0") == "teach" and st.status("k") is None
    r = _round(FakeLLM(), nodes=nodes)
    assert [q.node for q in r.questions] == ["k"] and "确认" in r.why
    r2 = _round(FakeLLM(), [answered(r, right)], nodes=nodes)
    assert [q.node for q in r2.questions] == ["x"]
    end = _round(FakeLLM(), [answered(r, right), answered(r2, right)], nodes=nodes)
    assert end.done and {d.node: d.decision for d in end.decisions} == {
        "n0": "teach", "k": "known", "x": "known"}


M1 = ProbeNode(id="m1", name="矩阵", cells=("m1.term",))
M2 = ProbeNode(id="m2", name="矩阵乘法", cells=("m2.procedure",))
ADV = kst.level_p0("advanced", "high")


def _la_nodes():
    return [LA.model_copy(update={"p0": ADV}), M1.model_copy(update={"p0": ADV}),
            M2.model_copy(update={"p0": ADV})]


def test_a_right_mastery_answer_decides_only_the_presupposed_members():
    """线性代数 rated advanced / high (region and members): its mastery question comes first; the
    model says it presupposes m1 (and names a foreign id and the region itself: dropped, logged).
    A right answer makes m1 inferred-known; m2 stays uncertain and is asked next; the region is
    known only once m2 is."""
    nodes = _la_nodes()
    item = {"kind": "mastery", "target": "hub-la", "stem": "奇异值分解题",
            "options": ["对", "错一", "错二", "错三"], "answer": 0,
            "presupposes": ["m1", "zz", "hub-la", "m1"]}
    llm = FakeLLM({"questions": [item]})
    r1 = _round(llm, nodes=nodes)
    q = r1.questions[0]
    assert (q.intent, q.node, q.presupposes) == ("mastery", "hub-la", ("m1",))
    rounds = [answered(r1, right)]
    st = _State(nodes, rounds, {})
    assert st.status("m1") == "known" and st.status("m2") is None
    r2 = _round(FakeLLM(), rounds, nodes=nodes)
    d = {x.node: (x.decision, x.source) for x in r2.decisions}
    assert d["m1"] == ("known", "inferred") and d["m2"][0] == "teach" and d["hub-la"][0] == "teach"
    assert [x.node for x in r2.questions] == ["m2"]
    end = _round(FakeLLM(), [*rounds, answered(r2, right)], nodes=nodes)
    assert end.done and {x.node: x.decision for x in end.decisions}["hub-la"] == "known"


def test_an_empty_or_odd_presupposes_is_allowed():
    nodes = _la_nodes()
    base = {"kind": "mastery", "target": "hub-la", "stem": "题", "options": ["a", "b", "c"],
            "answer": 0}
    for pre, want in (([], ()), (None, ()), ("m2", ("m2",)), ({"x": 1}, ())):
        r = _round(FakeLLM({"questions": [{**base, "presupposes": pre}]}), nodes=nodes)
        assert r.questions[0].presupposes == want and r.dropped == (), pre
    r = _round(FakeLLM({"questions": [base]}), nodes=nodes)          # no field at all
    assert r.questions[0].presupposes == ()
    st = _State(nodes, [answered(r, right)], {})                     # right, nothing presupposed
    assert st.status("m1") is None and st.status("m2") is None


# --- 不会 / 没学过 ends a block, a failure prunes what builds on it (user rulings 2026-10-24) ---

# the user's case (复杂网络中的渗流): blocks without members, no edges between them
BLOCKS = [ProbeNode(id=f"c{i}", name=n, cells=(f"c{i}.term",), region=True)
          for i, n in enumerate(["网络基础量", "ER 随机图与配置模型", "渗流", "幂律与 Molloy-Reed"], 1)]
DONT, PARTLY = 2, 1          # the self-report options: 会 / 学过但记不清 / 不会/没学过 / 说不清


def test_every_block_answered_dont_ends_the_diagnosis_without_a_focus_round():
    """「我都说了我不会了」: 不会/没学过 on every block → no 聚焦 question, every block teach."""
    llm = FakeLLM()
    r1 = _round(llm, nodes=BLOCKS)
    assert [q.kind for q in r1.questions] == ["self_report"] * 4
    r = _round(llm, [answered(r1, lambda q: DONT)], nodes=BLOCKS)
    assert r.done and r.questions == ()
    assert {d.node: (d.decision, d.source) for d in r.decisions} == {
        b.id: ("teach", "self_report") for b in BLOCKS}


def test_a_partly_block_still_gets_its_focus_item_a_dont_block_none():
    r1 = _round(FakeLLM(), nodes=BLOCKS)
    picks = {"c1": PARTLY, "c2": DONT, "c3": DONT, "c4": DONT}
    r = _round(FakeLLM(), [answered(r1, lambda q: picks[q.node])], nodes=BLOCKS)
    assert [q.node for q in r.questions] == ["c1"]


def test_dont_on_a_block_with_members_makes_them_all_teach_and_asks_none_of_them():
    llm = FakeLLM()
    r1 = _round(llm)                                   # hub-la, hub-pr, hub-sl
    picks = {"hub-la": DONT, "hub-pr": 0, "hub-sl": 0}
    st = _State(NODES, [answered(r1, lambda q: picks[q.node])], {})
    assert {x: st.status(x) for x in ("m1", "m2")} == {"m1": "teach", "m2": "teach"}
    assert "m1" not in st.candidates() and "m2" not in st.candidates()


def test_idk_on_a_basic_point_drops_the_question_built_on_it_and_ends_the_round():
    """「我上面都说了我不会了，还在问什么」: 度 answered 不知道 → the ER question built on it is not
    asked (recommended / unverified edges, downward only) and the diagnosis ends, both teach."""
    deg = ProbeNode(id="deg", name="度", cells=("deg.term",))
    er = ProbeNode(id="er", name="ER 图 c<1 时的最大连通分量", cells=("er.term",), builds_on=("deg",))
    llm = FakeLLM()
    r = _round(llm, nodes=[deg, er])
    assert [q.node for q in r.questions] == ["deg"]          # the basic point first (half-split tie)
    end = _round(llm, [answered(r, lambda q: IDK)], nodes=[deg, er])
    assert end.done and end.questions == ()
    assert {d.node: (d.decision, d.source) for d in end.decisions} == {
        "deg": ("teach", "quiz"), "er": ("teach", "inferred")}
    # a right answer on the basic point infers nothing through the unverified edge: er is asked;
    # and a right answer on er does not make deg known either
    nxt = _round(llm, [answered(r, right)], nodes=[deg, er])
    assert [q.node for q in nxt.questions] == ["er"]
    st = _State([deg, er], [answered(_round(llm, nodes=[er, deg]), right)], {})
    assert st.source_of("deg") == "none" and not st.decision("deg")
