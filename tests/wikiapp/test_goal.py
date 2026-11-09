from datetime import UTC, datetime

import pytest

from evowiki.belief.aspects import AddNote, Declare, DefineConcept, Observe
from evowiki.goals.contracts import Aspect, CheckItem
from evowiki.goals.diagnostic import IDK
from evowiki.teach.contracts import RepParams
from evowiki.teach.render import param_lines
from evowiki.wikiapp.events import (
    ConceptsNamed,
    DraftClosed,
    DraftCreated,
    GoalMapSaved,
    GraphSaved,
    PageSaved,
)
from evowiki.wikiapp.goal import stop_draft
from evowiki.wikiapp.kwrite import (
    IDK_LABEL,
    answer_label,
    apply_in_memory,
    concept_roles,
    existing_refs,
    observation_events,
    page_spec,
)
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict, WikiInvalid
from evowiki.wikiapp.teachspec import page_teach
from evowiki.wikiapp.views import goal_draft_out
from tests.wikiapp.flat_goal import flat_goal, flat_page
from tests.wikiapp.goal_script import (
    PROBE,
    answer_round,
    push_no_question,
    push_one_page,
    push_round,
    question_of,
)
from tests.wikiapp.script import Script

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
GOAL = "看懂 z 分数"
BACKGROUND = "我会算 z 分数，但是不太会解读"

INTAKE_1 = {
    "title": "z 分数",
    "concepts": [
        {"id": "n1", "name": "z 分数", "target": True, "prereqs": ["n2"],
         "aspects": ["procedure", "interpret"],
         "subitems": [{"aspect": "interpret", "name": "z=±1 的含义"}]},
        {"id": "n2", "name": "标准差", "aspects": ["term"]},
    ],
    "observations": [
        {"concept": "n1", "aspect": "procedure", "polarity": "+", "strength": 0.5,
         "quote": "会算 z 分数"},
        {"concept": "n2", "aspect": "term", "polarity": "-", "strength": 0.5,
         "quote": "完全不懂标准差"},                       # not in the description -> dropped
    ],
}
QUESTIONS = [
    ("c1.procedure", "x=7, μ=5, σ=2 时 z 是多少？", ["1", "2", "0.5", "-1"], 0),
    ("c2.term", "标准差衡量什么？", ["离散程度", "平均水平", "样本量", "最大值"], 0),
]


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n:04d}"


def script_diag(s: Script, questions=QUESTIONS[:1]) -> list[CheckItem]:
    """The probe model's item for each round, in the order the KST diagnosis picks them (first
    z 分数, closest to 1/2; after a wrong answer 标准差 — `second(s)`), and the path model
    keeping the goal on one page when the draft finishes. Returns the quizzes as the client sees
    them."""
    items = [push_round(s, [q])[0] for q in questions]
    push_one_page(s, ["c1", "c2"])
    return items


def second(s: Script) -> CheckItem:
    """The item for round 2 (标准差), pushed by a test that answers round 1 wrong."""
    return push_round(s, [QUESTIONS[1]])[0]


@pytest.fixture
def env():
    repo = MemoryRepo(["u1"])
    return repo, Script(), Ids()


def goal1(env):
    repo, s, ids = env
    s.push("goals.intake", INTAKE_1)
    items = script_diag(s)
    res = flat_goal(repo, CountingGateway(s.gateway()), "u1", GOAL, BACKGROUND,
                      now=NOW, new_id=ids)
    return res, items


def answer(env, draft_id, answers):
    repo, s, ids = env
    return answer_round(repo, CountingGateway(s.gateway()), "u1", draft_id, answers, now=NOW,
                        new_id=ids)


def stop(env, draft_id):
    repo, s, ids = env
    return stop_draft(repo, CountingGateway(s.gateway()), "u1", draft_id, now=NOW, new_id=ids)


def stop_on_one_page(env, draft_id) -> str:
    """The draft skipped and finished as one page (tests.wikiapp.flat_goal.flat_page)."""
    repo, s, ids = env
    return flat_page(repo, CountingGateway(s.gateway()), "u1", draft_id, now=NOW, new_id=ids,
                     outcome="stopped")


def test_draft_with_questions_writes_k_and_hides_keys(env):
    repo, s, _ = env
    res, items = goal1(env)
    assert res.page_id is None and res.draft is not None
    # the diagnosis's question is the assistant's (ask_check, stage 3 T11): no probe call
    assert res.calls == s.calls() and s.calls(PROBE) == 0
    assert s.calls("goals.diagnostic.verify") == 0
    d = res.draft
    assert [c.id for c in d.concepts] == ["c1", "c2"] and d.targets == ("c1",)
    assert res.open and d.items == () and len(d.rounds) == 1
    # one quiz per round, on the item the KST diagnosis picked (z 分数: closest to 1/2), on the
    # cell code picked; the round carries the pick only until the assistant asks it
    it = items[0]
    r0 = d.rounds[0]
    assert r0.questions == () and (r0.pick.intent, r0.pick.node, r0.pick.cell) == (
        "goal_use", "c1", it.cell)
    q = question_of(r0)
    assert (q.kind, q.target, q.options, q.answer, q.verified, q.intent, q.node) == (
        "quiz", it.cell, it.options, it.answer, True, "goal_use", "c1")
    assert (d.from_background, d.reused, d.dropped_questions) == (1, 0, 0)

    snap = repo.load("u1")
    assert snap.state.drafts[d.draft_id] == d
    assert [type(e.body) for e in repo.wiki_log("u1")[1:]] == [ConceptsNamed, GraphSaved,
                                                               GoalMapSaved, DraftCreated]
    g = snap.state.graph                 # a flat goal's concepts are graph nodes too (plan T4)
    assert [(n.id, n.name) for n in g.nodes] == [("c1", "z 分数"), ("c2", "标准差")]
    assert [(r.kind, r.src, r.dst, r.source, r.verified) for r in g.relations] == [
        ("recommended", "c2", "c1", "intake", False)]      # intake@v20 writes no prerequisite
    assert snap.state.names["c1"].name == "z 分数"
    alog = repo.aspect_log("u1")
    assert [type(e) for e in alog] == [DefineConcept, DefineConcept, Observe, AddNote]
    assert snap.k.prereqs("c1") == ()            # only verified prerequisites reach K
    obs = alog[2].observation
    assert (obs.cell, obs.kind, obs.evidence_ref) == ("c1.procedure", "self", f"goal:{d.draft_id}")
    notes = snap.k.notes("c1")
    assert [(n.cell, n.text, n.evidence_ref) for n in notes] == [
        ("c1.procedure", "会算 z 分数", f"goal:{d.draft_id}")]       # raw quote, no prefix
    assert snap.k.notes("c2") == ()                                  # non-verbatim quote dropped

    out = goal_draft_out(res)
    assert out.draft_id == d.draft_id and out.page_id is None and out.title == "z 分数"
    assert out.round == 1 and out.map is None and out.results == []
    assert out.questions == []              # asked in the chat (ask_check), never in the draft
    assert [x.node_id for x in out.decisions] == ["c1", "c2"]
    assert (out.notes.from_background, out.notes.reused_concepts,
            out.notes.dropped_questions) == (1, 0, 0)
    assert [(c.id, c.target, c.reused) for c in out.concepts] == [
        ("c1", True, False), ("c2", False, False)]


def test_second_goal_reuses_concepts_by_exact_name(env):
    repo, s, ids = env
    goal1(env)
    s.push("goals.intake", {"title": "z 检验", "concepts": [
        {"id": "n1", "name": "z 检验", "target": True, "prereqs": ["n2"],
         "aspects": ["procedure"]},
        {"id": "n2", "name": "Z 分数", "aspects": ["interpret"]}]})
    # K already says z 分数 is still to learn (goal 1's description); z 检验 only has the model's
    # unverified edge to it (relation layers): nothing is inferred for z 检验, so the diagnosis
    # asks about it (the model writes nothing usable here: it ends, fail closed), then the path
    push_no_question(s)
    push_one_page(s, ["c3", "c1"])
    res = flat_goal(repo, CountingGateway(s.gateway()), "u1", "学 z 检验", "", now=NOW,
                      new_id=ids)
    assert s.calls(PROBE) == 0 and res.path_id is not None
    decided = {x.node: (x.decision, x.source) for x in res.draft.rounds[0].decisions}
    assert decided["c1"] == ("teach", "k") and decided["c3"][1] != "inferred"
    intake_prompt = s.requests("goals.enter")[1].prompt
    assert "c1 z 分数" in intake_prompt and "c2 标准差" in intake_prompt
    d = res.draft
    assert [(c.id, c.name, c.reused) for c in d.concepts] == [
        ("c3", "z 检验", False), ("c1", "z 分数", True)]
    assert d.reused == 1 and goal_draft_out(res).notes.reused_concepts == 1
    snap = repo.load("u1")
    assert snap.k.concept_ids() == ("c1", "c2", "c3")
    assert snap.k.prereqs("c3") == ()
    assert snap.state.names["c1"].name == "z 分数"


def test_no_question_plans_the_path_at_once(env):
    repo, s, ids = env
    first, _ = goal1(env)
    stop_on_one_page(env, first.draft.draft_id)                    # c1 -> learn
    snap = repo.load("u1")
    cells = ("c1.procedure", "c1.interpret", "c2.term")
    repo.commit(snap, Commit(aspect=[Declare(event_id=f"decl{i}", ts=NOW, cell=c, value="know")
                                     for i, c in enumerate(cells)]))
    s.push("goals.intake", {"title": "再看 z 分数", "concepts": [{"id": "c1", "target": True}]})
    push_one_page(s, ["c1"])
    probes = s.calls(PROBE)
    res = flat_goal(repo, CountingGateway(s.gateway()), "u1", "再看 z 分数", "", now=NOW,
                    new_id=ids)
    # K already has evidence for the concept: nothing is asked, not even a probe call
    assert s.calls(PROBE) == probes and not res.open and res.path_id is not None
    out = goal_draft_out(res)
    assert out.draft_id is None and out.questions == [] and out.path_id == res.path_id
    assert res.draft.draft_id not in repo.load("u1").state.drafts


def test_answers_write_quiz_observations_and_notes(env):
    repo, s, ids = env
    res, items = goal1(env)
    items.append(second(s))
    d = res.draft
    wrong = (items[0].answer + 1) % 4
    k_seq = repo.load("u1").aspect_seq
    calls = s.calls()
    r2 = answer_round(repo, CountingGateway(s.gateway()), "u1", d.draft_id, [wrong], now=NOW,
                      new_id=ids)
    assert r2.open and r2.draft.rounds[-1].pick.node == "c2"   # 答错: 标准差 next
    path_id = answer(env, d.draft_id, [IDK]).path_id
    snap = repo.load("u1")
    new = repo.aspect_log("u1")[k_seq:]
    obs = [e.observation for e in new if isinstance(e, Observe)]
    assert [(o.cell, o.polarity, o.strength, o.kind, o.evidence_ref) for o in obs] == [
        ("c1.procedure", "-", 1.0, "quiz", f"probe:{d.draft_id}:1:0"),
        ("c2.term", "-", 0.8, "quiz", f"probe:{d.draft_id}:2:0")]
    notes = [(e.cell, e.text, e.evidence_ref) for e in new if isinstance(e, AddNote)]
    assert notes == [
        ("c1.procedure", items[0].options[wrong], f"probe:{d.draft_id}:1:0"),
        ("c2.term", items[1].stem, f"probe:{d.draft_id}:2:0")]
    assert s.calls() == calls + 1             # no probe call: only the path model
    final = snap.state.drafts[d.draft_id].rounds[-1]
    assert final.done and [(x.node, x.decision, x.source) for x in final.decisions] == [
        ("c1", "teach", "quiz"), ("c2", "teach", "quiz")]
    assert d.draft_id in snap.state.closed_drafts and path_id in snap.state.paths
    closed = next(e.body for e in reversed(repo.wiki_log("u1")) if isinstance(e.body, DraftClosed))
    assert closed == DraftClosed(draft_id=d.draft_id, outcome="answered", path_id=path_id)
    assert [p.concepts for p in snap.state.paths[path_id].path.pages] == [("c1", "c2")]

    with pytest.raises(WikiConflict):
        answer(env, d.draft_id, [0])
    with pytest.raises(WikiConflict):
        stop(env, d.draft_id)


def test_quiz_notes_carry_a_stem_label(env):
    repo, s, _ = env
    res, items = goal1(env)
    items.append(second(s))
    d = res.draft
    k_seq = repo.load("u1").aspect_seq
    wrong = (items[0].answer + 1) % 4
    answer(env, d.draft_id, [wrong])
    answer(env, d.draft_id, [IDK])
    notes = [e for e in repo.aspect_log("u1")[k_seq:] if isinstance(e, AddNote)]
    assert [n.text for n in notes] == [items[0].options[wrong], items[1].stem]
    assert notes[0].label == f"题目：{items[0].stem}；选了「{items[0].options[wrong]}」"
    assert notes[1].label == f"题目：{items[1].stem}；选了「不知道」"
    assert notes[1].label.endswith("选了「不知道」")
    assert [n.label for n in repo.load("u1").k.notes("c2")] == [notes[1].label]   # replayed


def test_answer_label_caps_the_stem():
    assert IDK_LABEL == "不知道"
    assert answer_label("z=-1 表示？", "低一个标准差") == "题目：z=-1 表示？；选了「低一个标准差」"
    long = answer_label("问" * 2000, "选项" * 100)
    assert len(long) <= 600 and long.startswith("题目：" + "问" * 500 + "…；选了「")
    assert len(answer_label("问" * 500, "a")) == len("题目：；选了「a」") + 500    # no … at 500


def test_answers_are_validated(env):
    repo, _, _ = env
    res, _ = goal1(env)
    did = res.draft.draft_id
    for bad in ([4], [-2]):                   # one question a round, its options or 不知道
        with pytest.raises(WikiInvalid):
            answer(env, did, bad)
    assert issubclass(WikiInvalid, ValueError)
    with pytest.raises(LookupError):
        stop(env, "dr_nope")
    assert did not in repo.load("u1").state.closed_drafts


def test_skip_keeps_k_and_sets_targets_learning(env):
    repo, _, _ = env
    res, _ = goal1(env)
    before = repo.load("u1")
    pid = stop_on_one_page(env, res.draft.draft_id)
    snap = repo.load("u1")
    assert snap.aspect_seq == before.aspect_seq and snap.k.to_json() == before.k.to_json()
    bodies = [e.body for e in repo.wiki_log("u1")[before.wiki_seq:]]
    assert [type(b) for b in bodies] == [PageSaved, DraftClosed]
    assert bodies[1].outcome == "stopped"
    assert concept_roles(snap.state, pid) == {"c1": "learn", "c2": "support"}
    assert snap.state.current_page == pid and snap.state.page_order == (pid,)


def test_kwrite_helpers(env):
    repo, _, ids = env
    res, _ = goal1(env)
    pid = stop_on_one_page(env, res.draft.draft_id)
    snap = repo.load("u1")
    refs = existing_refs(snap.state, snap.k)
    assert [(r.id, r.name, r.aspects, r.prereqs) for r in refs] == [
        ("c1", "z 分数", (Aspect.PROCEDURE, Aspect.INTERPRET), ()),
        ("c2", "标准差", (Aspect.TERM,), ())]
    assert refs[0].subitems == ()       # intake@v20 writes no subitems (outline blocks only)

    spec = page_spec(snap.state, snap.k, pid)
    assert [(c.id, c.name, c.prereqs) for c in spec.concepts] == [
        ("c1", "z 分数", ()), ("c2", "标准差", ())]
    assert spec.concepts[0].subitems == {}
    assert spec.targets == ("c1",) and spec.goal == GOAL   # background left PageSpec with the profile (2e1704ca)
    assert spec.other_pages == () and spec.roles == {"c1": "learn", "c2": "support"}
    # guidelines = the teaching spec in force (default items for a concept page) + 4 param lines
    teach = page_teach(snap.state, pid)
    assert spec.guidelines == teach.guidelines and teach.version == 1
    assert spec.guidelines[-4:] == param_lines(RepParams())

    obs = repo.aspect_log("u1")[2].observation
    evs = observation_events([obs], ts=NOW, new_id=ids, note_text=lambda o: "x" * 300)
    assert [type(e) for e in evs] == [Observe, AddNote] and len(evs[1].text) == 240
    k2 = apply_in_memory(snap, [e.model_copy(update={"event_id": e.event_id + "b"}) for e in evs])
    assert k2 is not snap.k and len(k2.notes("c1")) == 2 and len(snap.k.notes("c1")) == 1
