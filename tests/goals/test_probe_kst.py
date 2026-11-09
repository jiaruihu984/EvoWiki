"""goals.probe (probe@v4) offline cases. The diagnosis is Knowledge Space Theory (goals/kst.py, a
port of mycaas): the algorithm picks every question (half-split), updates on every answer
(multiplicative rule, a self-report weaker than a quiz) and stops when one knowledge state is
likely enough; the model only writes the item it is told to. Information flow (the product specification
specs/2026-10-11-llm-information-flow.md, rule ASK): the prompt carries the goal, the map, K, the
learner's description, the answered questions and exactly the picked item — never a choice of
targets. Fakes only."""


from evowiki.goals import kst
from evowiki.goals.contracts import Aspect
from evowiki.goals.diagnostic import IDK
from evowiki.goals.probe import (
    RESOLVED_WHY,
    ProbeNode,
    ProbeRound,
    _State,
    next_round,
    structure,
)
from tests.goals.fakes_intake import FakeK
from tests.wikiapp.flat_goal import region_goal
from tests.wikiapp.goal_script import asked

A = Aspect
GOAL = "我要学机器学习"
BACKGROUND = "会写 Python，大学学过线性代数和一点概率，都忘得差不多了。"
CONCEPTS = {"m1": (A.TERM,), "m2": (A.PROCEDURE,), "p1": (A.TERM,), "p2": (A.PROCEDURE,),
            "s1": (A.TERM, A.PURPOSE), "s2": (A.TERM,), "s3": (A.PURPOSE,)}
REGIONS = [ProbeNode(id="hub-la", name="线性代数", cells=("m1.term", "m2.procedure"), region=True,
                     members=(("m1", "矩阵"), ("m2", "矩阵乘法"))),
           ProbeNode(id="hub-pr", name="概率论", cells=("p1.term", "p2.procedure"), region=True,
                     members=(("p1", "条件概率"), ("p2", "贝叶斯公式"))),
           ProbeNode(id="hub-sl", name="监督学习", region=True, target=True,
                     cells=("s1.term", "s1.purpose", "s2.term", "s3.purpose"),
                     members=(("s1", "损失函数"), ("s2", "梯度下降"), ("s3", "线性回归")),
                     prereqs=("hub-la", "hub-pr"))]
MEMBERS = [ProbeNode(id="m1", name="矩阵", cells=("m1.term",)),
           ProbeNode(id="m2", name="矩阵乘法", cells=("m2.procedure",), prereqs=("m1",)),
           ProbeNode(id="p1", name="条件概率", cells=("p1.term",)),
           ProbeNode(id="p2", name="贝叶斯公式", cells=("p2.procedure",), prereqs=("p1",)),
           ProbeNode(id="s1", name="损失函数", cells=("s1.term", "s1.purpose")),
           ProbeNode(id="s2", name="梯度下降", cells=("s2.term",), prereqs=("s1",)),
           ProbeNode(id="s3", name="线性回归", cells=("s3.purpose",), prereqs=("m2", "s2"))]
NODES = [*REGIONS, *MEMBERS]


def known_regions(r) -> list[str]:
    """The regions the diagnosis decided known (a region's known-ness lives in K afterwards)."""
    return [d.node for d in r.decisions if d.decision == "known" and d.node.startswith("hub-")]


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


def run(llm, rounds=(), nodes=NODES, described=()) -> ProbeRound:
    return asked_round(llm, list(rounds), nodes=nodes, k=FakeK(CONCEPTS), described=described)


def diagnose(pick, nodes=NODES, described=()) -> tuple[list[ProbeRound], ProbeRound, FakeLLM]:
    """Every round answered by `pick(question)` until the diagnosis ends."""
    llm, rounds = FakeLLM(), []
    while not (r := run(llm, rounds, nodes=nodes, described=described)).done:
        rounds.append(r.model_copy(update={"answers": tuple(pick(q) for q in r.questions)}))
        assert len(rounds) <= 2 * len(nodes), "the diagnosis does not end"
    return rounds, r, llm


def quizzes(rounds) -> list[str]:
    return [q.node for r in rounds for q in r.questions if q.kind == "quiz"]


def right(q):
    return q.answer if q.kind == "quiz" else q.means.index("know")


def wrong(q):
    """Every quiz wrong, every self-report 「学过，忘得差不多了」 (the trial's answers)."""
    return (q.answer + 1) % len(q.options) if q.kind == "quiz" else q.means.index("forgot")


def task(prompt: str) -> str:
    return prompt.split("这一轮要弄清的：")[1].split("⟦g:probe⟧")[0]


# --- (a) what the prompt must and must not contain ---------------------------------------------

def test_described_nodes_get_no_self_report():
    llm = FakeLLM()
    r = run(llm, described={"m1": "~", "m2": "~", "p1": "~"})
    assert [q.node for q in r.questions] == ["hub-sl"]
    assert {d.node: d.source for d in r.decisions}["m1"] == "described"


# --- (a) harness: outputs ---------------------------------------------------------------------

# --- (b) behaviour over whole diagnoses -------------------------------------------------------

def test_all_answers_right_needs_few_questions():
    rounds, end, _ = diagnose(right)
    assert len(quizzes(rounds)) < len(NODES) // 2
    assert end.why == RESOLVED_WHY.format(teach=kst.SC_TEACH, known=kst.SC_KNOWN)
    assert all(d.decision == "known" for d in end.decisions)
    assert set(known_regions(end)) == {"hub-la", "hub-pr", "hub-sl"}


def test_all_wrong_descends_to_the_prerequisites_then_stops():
    rounds, end, _ = diagnose(wrong)
    asked = quizzes(rounds)
    assert len(asked) == len(set(asked)) and asked
    assert set(asked) >= {"m1", "p1", "s1"}                   # the bottoms get asked
    assert all(d.decision == "teach" for d in end.decisions) and known_regions(end) == []


def test_mixed_answers_find_the_learners_state():
    knows = {"hub-la", "m1", "m2", "p1"}
    rounds, end, _ = diagnose(lambda q: right(q) if q.node in knows else wrong(q))
    asked = quizzes(rounds)
    assert len(asked) == len(set(asked)) <= len(NODES)
    assert {d.node for d in end.decisions if d.decision == "known"} == knows
    assert known_regions(end) == ["hub-la"]


def test_a_self_report_is_weak_evidence():
    """「学过，还会」 on every region (it counts on the region item and each member) moves a
    member less than one quiz would and never ends the diagnosis: known needs strong evidence."""
    r1 = run(FakeLLM())
    after_self = run(FakeLLM(), [r1.model_copy(update={"answers": (0, 0, 0)})])
    assert not after_self.done and after_self.questions[0].kind == "quiz"
    plain = kst.Assessment.build(*structure(NODES))
    p = {d.node: d.p for d in after_self.decisions}
    by_quiz = plain.update("m2", 1, kst.BETA_QUIZ, kst.ETA_QUIZ).marginal("m2")
    assert plain.marginal("m2") < p["m2"] < by_quiz


def test_trial_1011_offline_replay_terminates():
    """The 2026-10-11 trial through the goal flow (ML map, 「…都忘得差不多了」): the learner
    always picks option 0 after the shuffle. It ran 45 rounds on probe@v3's loop; here every
    node is asked at most once and the diagnosis ends."""
    from evowiki.wikiapp.llm import CountingGateway
    from evowiki.wikiapp.repo import MemoryRepo
    from tests.wikiapp.goal_script import answer_round, auto_probe, question_of
    from tests.wikiapp.script import Script
    from tests.wikiapp.test_big_goal import ML
    from tests.wikiapp.test_goal import NOW, Ids

    repo, s, ids = MemoryRepo(["u1"]), Script(), Ids()
    s.push("goals.intake", ML)
    auto_probe(s)
    res = region_goal(repo, CountingGateway(s.gateway()), "u1", GOAL, BACKGROUND,
                      s.pop_mapper(), now=NOW, new_id=ids)
    rounds = []
    while res.open:
        r = res.draft.rounds[-1]
        rounds.append(r.model_copy(update={"questions": (question_of(r),)}))
        assert len(rounds) <= 30, "the diagnosis does not end"
        res = answer_round(repo, CountingGateway(s.gateway()), "u1", res.draft.draft_id,
                           [0], now=NOW, new_id=ids)
    asked = [(q.kind, q.node) for r in rounds for q in r.questions]
    assert len(asked) == len(set(asked))
    m = repo.load("u1").state.maps[res.map_id]
    nodes = [n for n in m.nodes if n.kind == "concept" or n.parent is None]
    assert len(rounds) <= 2 * len(nodes)                      # a block a self-report, a quiz each
    assert asked[0][0] == "self_report"
    final = repo.load("u1").state.drafts[res.draft.draft_id].rounds[-1]
    assert final.done and len(final.decisions) == len(nodes)
    assert any(line.startswith("摸底结论 ") for line in m.log)


# --- (b) through the goal flow ----------------------------------------------------------------

def test_flow_background_observations_become_described_and_reach_the_probe():
    """create_goal: the description's observations (「都忘得差不多了」 → ~ on the 线性代数
    members) are `Draft.described` with their polarity; the probe prompt carries the description
    and marks them; 线性代数 gets no self-report."""
    from evowiki.wikiapp.llm import CountingGateway
    from evowiki.wikiapp.repo import MemoryRepo
    from tests.wikiapp.goal_script import auto_probe
    from tests.wikiapp.script import Script
    from tests.wikiapp.test_big_goal import LA, ML
    from tests.wikiapp.test_goal import NOW, Ids

    forgot = "都忘得差不多了"
    obs = [{"concept": c, "aspect": a, "polarity": "~", "strength": 0.4, "quote": forgot}
           for c, a in (("n1", "term"), ("n2", "procedure"))]
    repo, s, ids = MemoryRepo(["u1"]), Script(), Ids()
    s.push("goals.intake", {**ML, "observations": obs})
    auto_probe(s)
    res = region_goal(repo, CountingGateway(s.gateway()), "u1", GOAL, BACKGROUND,
                      s.pop_mapper(), now=NOW, new_id=ids)
    assert res.draft.described == ("c2", "c3") and res.draft.described_as == ("+", "+")
    r = res.draft.rounds[0]
    assert r.pick is not None and r.pick.node != LA            # 线性代数 gets no self-report
    src = {d.node: d.source for d in r.decisions}
    assert src["c1"] == src["c2"] == "described"
    assert IDK == -1


# --- (b) probe@v5: the prior from the intake's levels, known only from answers ------------------

ADVANCED = kst.level_p0("advanced", "high")
LA_SIDE = {"hub-la", "m1", "m2"}


def with_p0(nodes, p0):
    return [n.model_copy(update={"p0": p0.get(n.id)}) for n in nodes]


def test_a_claimed_region_gets_its_mastery_question_first_and_a_right_answer_decides_it():
    """线性代数 rated advanced / high (the region and its members), the rest settled by K: the
    region's mastery item is the most uncertain one (the members are near-certain under the
    prior), so it is asked first; the key decides the region and its members known (members by
    inference, never asked)."""
    def settled(n):
        if n.id in LA_SIDE:
            return n
        if n.region:                                   # K has evidence on its members
            return n.model_copy(update={"evidenced": tuple(m for m, _ in n.members)})
        return n.model_copy(update={"k_prior": "teach", "evidenced": (n.id,)})

    nodes = [settled(n) for n in with_p0(NODES, dict.fromkeys(LA_SIDE, ADVANCED))]
    llm = FakeLLM()
    r = run(llm, nodes=nodes)
    assert [(q.kind, q.intent, q.node) for q in r.questions] == [("quiz", "mastery", "hub-la")]
    nxt = run(FakeLLM(), [r.model_copy(update={"answers": (r.questions[0].answer,)})],
              nodes=nodes)
    d = {x.node: (x.decision, x.source) for x in nxt.decisions}
    assert d["hub-la"] == ("known", "quiz")
    assert d["m1"] == d["m2"] == ("known", "inferred")
    assert nxt.done and known_regions(nxt) == ["hub-la"]


def test_a_claimed_region_among_open_ones_is_asked_before_its_members():
    """Only 线性代数 rated (advanced / high): round 1 self-reports the regions the description
    says nothing about; 线性代数 gets none; its mastery item comes before any of its members and
    its key decides it; the members are never asked."""
    nodes = with_p0(NODES, dict.fromkeys(LA_SIDE, ADVANCED))
    rounds, end, _ = diagnose(lambda q: right(q) if q.node in LA_SIDE else wrong(q), nodes=nodes)
    assert [q.node for q in rounds[0].questions] == ["hub-pr", "hub-sl"]
    asked = quizzes(rounds)
    assert "hub-la" in asked and not {"m1", "m2"} & set(asked)
    d = {x.node: x.decision for x in end.decisions}
    assert d["hub-la"] == d["m1"] == d["m2"] == "known" and "hub-la" in set(known_regions(end))


def test_without_levels_no_region_item_is_forced():
    """No levels, every self-report 说不清: the first quiz is the half-split item of the uniform
    prior — a member, not a region (P(a whole region) is small there)."""
    r1 = run(FakeLLM()).model_copy(update={"answers": (3, 3, 3)})
    r2 = run(FakeLLM(), [r1])
    assert r2.questions[0].intent == "goal_use"


def test_a_self_report_of_know_alone_never_decides_known():
    """「学过，还会」 on every region: before any answer to a quiz everything is still to teach (a
    self-report only steers what is asked)."""
    r1 = run(FakeLLM()).model_copy(update={"answers": (0, 0, 0)})
    end = run(FakeLLM(), [r1])
    assert not end.done and known_regions(end) == []
    assert all(d.decision == "teach" for d in end.decisions)
    assert {d.source for d in end.decisions} <= {"self_report", "inferred"}


# --- live run 3 findings: a claim is confirmed first; an item about the picked node is used --------

def test_a_claim_is_confirmed_before_the_most_uncertain_item():
    """Live run 3: 线性代数 rated advanced / medium (p0 0.78 on the region and its members), the
    other regions open. Under the prior 线性代数 is near-certain, so plain half-split never picks
    it; but nothing has confirmed it, so its mastery question comes first (one answer confirms
    the region and its members), then half-split as before."""
    p0 = kst.level_p0("advanced", "medium")
    nodes = with_p0(NODES, dict.fromkeys(LA_SIDE, p0))
    r1 = run(FakeLLM(), nodes=nodes).model_copy(update={"answers": (3, 3)})   # 说不清
    r2 = run(FakeLLM(), [r1], nodes=nodes)
    q = r2.questions[0]
    assert (q.intent, q.node) == ("mastery", "hub-la") and "确认" in r2.why
    r3 = run(FakeLLM(), [r1, r2.model_copy(update={"answers": (q.answer,)})], nodes=nodes)
    assert all(x.node not in LA_SIDE for x in r3.questions)


def _only(node_id: str, **item) -> dict:
    base = {"stem": "哪一个对？", "options": ["对的", "错一", "错二", "错三"], "answer": 0}
    return {"questions": [{**base, **item}]}


# --- probe@v6: live run 4 offline, presupposed members ----------------------------------------


def test_a_mastery_answer_presupposing_one_member_leaves_the_other_open():
    """A claimed 线性代数 (advanced / high), the model's mastery question presupposes only m1: the
    key makes m1 inferred-known, m2 stays uncertain and is asked; the region is known only after
    m2's own answer."""
    nodes = with_p0(NODES, dict.fromkeys(LA_SIDE, ADVANCED))
    item = {"kind": "mastery", "target": "hub-la", "stem": "奇异值分解题",
            "options": ["对", "错一", "错二", "错三"], "answer": 0, "presupposes": ["m1"]}
    rounds = [run(FakeLLM(), nodes=nodes).model_copy(update={"answers": (3, 3)})]   # 说不清
    r2 = run(FakeLLM({"questions": [item]}), rounds, nodes=nodes)
    assert (r2.questions[0].node, r2.questions[0].presupposes) == ("hub-la", ("m1",))
    rounds.append(r2.model_copy(update={"answers": (r2.questions[0].answer,)}))
    r3 = run(FakeLLM(), rounds, nodes=nodes)
    d = {x.node: (x.decision, x.source) for x in r3.decisions}
    assert d["m1"] == ("known", "inferred") and d["m2"][0] == d["hub-la"][0] == "teach"
    assert [q.node for q in r3.questions] == ["m2"]


# --- node model (unified node graph, plan T8): items are the goal's nodes ---------------------

MATH = ProbeNode(id="math", name="数学", cells=("math.term",), region=True,
                 members=(("hub-la", "线性代数"), ("p1", "条件概率")))
LA, M1, M2, P1 = REGIONS[0], MEMBERS[0], MEMBERS[1], MEMBERS[2]


def _q(node, target, answer_given, *, intent="goal_use", presupposes=()):
    from evowiki.goals.probe import ProbeQuestion
    q = ProbeQuestion(kind="quiz", target=target, stem=f"{node}？", options=("对", "错一", "错二"),
                      answer=0, verified=True, intent=intent, node=node, presupposes=presupposes)
    return q, answer_given


def _answered(*qa) -> ProbeRound:
    return ProbeRound(questions=tuple(q for q, _ in qa), done=False,
                      answers=tuple(a for _, a in qa))


def test_a_node_with_parts_gets_a_mastery_item_and_a_leaf_a_goal_use_item():
    """Mastery iff the node has parts among the items; a region-like node whose parts are not in
    the goal's subset is a leaf here (goal-use on its own cells); a cell-less one stays mastery."""
    old = ProbeNode(id="c9", name="旧区域", cells=("c9.term",), region=True)
    topic = ProbeNode(id="hub-x", name="主题", cells=(), region=True)
    st = _State([LA, M1, M2, old, topic], (), {})
    assert [st.mastery(n) for n in (LA, M1, old, topic)] == [True, False, False, True]
    llm = FakeLLM()
    r1 = run(llm, nodes=[old]).model_copy(update={"answers": (3,)})        # 说不清
    r2 = run(llm, [r1], nodes=[old])
    assert [(q.intent, q.node, q.target) for q in r2.questions] == [("goal_use", "c9", "c9.term")]


def test_a_nested_node_is_decided_from_its_leaves_and_its_self_report_reaches_every_part():
    """数学 ⊃ 线性代数 ⊃ {矩阵, 矩阵乘法}, and 条件概率: 数学's aggregate is its leaves (m1, m2,
    p1); a self-report on 数学 counts (weakly) on every node under it."""
    nodes = [MATH, LA, M1, M2, P1]
    st = _State(nodes, (), {})
    assert st.leaves(MATH) == ["p1", "m1", "m2"] and st.member_items(MATH) == ["hub-la", "p1"]
    from evowiki.goals.probe import ProbeQuestion
    sr = ProbeQuestion(kind="self_report", target="math", stem="学过数学吗？",
                       options=("会", "不会"), means=("know", "dont"), intent="self_report",
                       node="math")
    self_math = ProbeRound(questions=(sr,), done=False, answers=(0,))
    st = _State(nodes, [self_math], {})
    assert {x: st.source.get(x) for x in ("math", "hub-la", "m1", "m2", "p1")} == dict.fromkeys(
        ("math", "hub-la", "m1", "m2", "p1"), "self_report")
    right = _answered(_q("m1", "m1.term", 0), _q("m2", "m2.procedure", 0), _q("p1", "p1.term", 0))
    d = {x.node: x.decision for x in _State(nodes, [right], {}).decisions()}
    assert d["math"] == d["hub-la"] == "known"
    half = _answered(_q("m1", "m1.term", 0), _q("m2", "m2.procedure", 0), _q("p1", "p1.term", 1))
    d = {x.node: x.decision for x in _State(nodes, [half], {}).decisions()}
    assert d["hub-la"] == "known" and d["math"] == "teach"     # 条件概率 wrong: not the whole field


def test_a_quizzed_node_with_parts_keeps_its_own_evidence_next_to_its_parts():
    """Direct and aggregate evidence are both kept (spec §2): the parts all right but the node's
    own mastery question wrong → still to teach; without its own question → known."""
    nodes = [LA, M1, M2]
    parts = (_q("m1", "m1.term", 0), _q("m2", "m2.procedure", 0))
    d = {x.node: x.decision for x in _State(nodes, [_answered(*parts)], {}).decisions()}
    assert d["hub-la"] == "known"
    wrong_own = _answered(_q("hub-la", "hub-la", 1, intent="mastery"), *parts)
    st = _State(nodes, [wrong_own], {})
    d = {x.node: (x.decision, x.source) for x in st.decisions()}
    assert d["m1"][0] == d["m2"][0] == "known" and d["hub-la"] == ("teach", "quiz")


def test_a_right_answer_on_a_big_node_reaches_its_prerequisites_never_all_its_parts():
    """线性代数 ← 监督学习 (prerequisite); a right mastery answer on 监督学习 presupposing only s1
    (损失函数): its prerequisite 线性代数's item and s1 are implied; s2 / s3, parts it did not
    presuppose, are not (s2 needs s1, not the other way round)."""
    sl = REGIONS[2]
    nodes = [LA, sl, *MEMBERS[4:]]
    _, pre = structure(nodes, [_answered(_q("hub-sl", "hub-sl", 0, intent="mastery",
                                            presupposes=("s1",)))])
    assert pre["hub-sl"] == {"hub-la", "s1"}
    st = _State(nodes, [_answered(_q("hub-sl", "hub-sl", 0, intent="mastery",
                                     presupposes=("s1",)))], {})
    srcs = {x: st.source_of(x) for x in ("hub-la", "s1", "s2", "s3")}
    assert srcs["hub-la"] == srcs["s1"] == "inferred" and srcs["s2"] == srcs["s3"] == "none"


def test_a_node_with_two_parents_is_one_item_counted_under_both():
    pr = ProbeNode(id="hub-pr", name="概率论", cells=("m2.procedure", "p1.term"), region=True,
                   members=(("m2", "矩阵乘法"), ("p1", "条件概率")))
    nodes = [LA, pr, M1, M2, P1]
    items, _ = structure(nodes)
    assert list(items).count("m2") == 1
    st = _State(nodes, (), {})
    assert st.leaves(LA) == ["m1", "m2"] and st.leaves(pr) == ["m2", "p1"]
