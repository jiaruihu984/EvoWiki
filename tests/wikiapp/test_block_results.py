"""EvoUI block results come back as evidence: graded against the stored block, questions with a
known cell become quiz evidence, a block_result signal carries the score / cases / final state,
and a wrong block answer can be followed up. MemoryRepo, no network."""

import copy

import pytest

from evowiki.belief.aspects import AddNote, Observe
from evowiki.deep.contracts import AssetMeta, RenderCheck
from evowiki.deep.evoui import block_json
from evowiki.deep.store import MemoryAssetStore, StoredAsset, sha256
from evowiki.wikiapp.block_results import block_results
from evowiki.wikiapp.events import SignalRecorded
from evowiki.wikiapp.followup import followup
from evowiki.wikiapp.repo import MemoryRepo, WikiInvalid
from evowiki.wikiapp.wiki_schemas import BlockResultsIn, CheckFollowupIn
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized

QUIZ = {"type": "evoui", "title": "标准分", "question": "z 分数说的是什么？", "state": {}, "derived": {},
        "body": {"kind": "box", "children": [
            {"kind": "text", "text": "先想一想"},
            {"kind": "quiz", "cases": [{"id": "sign", "label": "正负号", "advice": "回看 z 的符号"},
                                       {"id": "unit", "label": "单位"}],
             "questions": [
                 {"q": "z = -1 表示？", "options": ["低一个标准差", "低 1 分"], "correct": 0,
                  "why": "单位是标准差", "case": "sign", "cell": "c1.interpret"},
                 {"q": "z 的单位？", "options": ["分", "标准差"], "correct": 1, "why": "除以 σ",
                  "case": "unit"},
                 {"q": "未知格子", "options": ["a", "b"], "correct": 0, "why": "w", "case": "unit",
                  "cell": "zz.term"}]}]},
        "fallback": "z = (x − μ)/σ：离均值几个标准差。"}


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def stored(pid: str, kind: str = "evoui", block=QUIZ) -> tuple[MemoryAssetStore, str]:
    store = MemoryAssetStore()
    content = block_json(block) if kind == "evoui" else "<!doctype html><p>x</p>"
    meta = AssetMeta(asset_id="ia_q", page_id=pid, section_id="s2", interactive_id="z-quiz",
                     title="标准分", height=400, sha256=sha256(content), kind=kind,
                     check=RenderCheck(interactive_id="z-quiz", ok=True, probes_passed=0,
                                       probes_total=0))
    store.writer([StoredAsset(user_id="u1", meta=meta, html=content, job_id="j", created_at=NOW)])(None)
    return store, "ia_q"


def send(env, store, aid, answers, state=None):
    body = BlockResultsIn(answers=[{"quiz": 0, "question": q, "chosen": c} for q, c in answers],
                          state=state)
    return block_results(env[0], store, None, "u1", aid, body, now=NOW, new_id=env[2])


def test_answers_are_graded_server_side_and_known_cells_become_quiz_evidence(env):
    pid = organized(env)
    store, aid = stored(pid)
    out = send(env, store, aid, [(0, 1), (1, 1), (2, 0)], state={"x": 3})
    assert (out.right, out.total) == (2, 3)
    a0, a1, a2 = out.answers
    assert (a0.correct, a0.cell, a0.case) == (False, "c1.interpret", "sign")
    assert a0.answer_ref.startswith(f"block:{pid}:s2:{aid}:0.0:")
    assert a1.answer_ref is None and a2.answer_ref is None        # no cell / unknown cell
    assert [(c.case, c.right, c.total) for c in out.cases] == [("sign", 0, 1), ("unit", 2, 2)]
    assert out.cases[0].advice == "回看 z 的符号"
    log = env[0].aspect_log("u1")
    [obs] = [e for e in log if isinstance(e, Observe) and e.observation.evidence_ref == a0.answer_ref]
    assert (obs.observation.kind, obs.observation.polarity) == ("quiz", "-")
    [note] = [e for e in log if isinstance(e, AddNote) and e.evidence_ref == a0.answer_ref]
    assert note.text == "低 1 分" and "正确答案「低一个标准差」" in note.label
    sig = [e.body.signal for e in env[0]._wiki["u1"] if isinstance(e.body, SignalRecorded)][-1]
    assert sig.kind == "block_result" and sig.signal_id == out.signal_id
    assert sig.outcome == pytest.approx(2 / 3) and (sig.page_id, sig.section_id) == (pid, "s2")
    assert "答对 2/3" in sig.text and '"x": 3' in sig.text


def test_state_alone_is_a_signal_without_evidence(env):
    pid = organized(env)
    store, aid = stored(pid)
    n = len(env[0].aspect_log("u1"))
    out = send(env, store, aid, [], state={"deg": 45})
    assert out.total == 0 and out.change is None and len(env[0].aspect_log("u1")) == n


@pytest.mark.parametrize("answers, kind, err", [
    ([(5, 0)], "evoui", WikiInvalid), ([(0, 5)], "evoui", WikiInvalid),
    ([(0, 0)], "html", WikiInvalid)])
def test_refusals(env, answers, kind, err):
    pid = organized(env)
    store, aid = stored(pid, kind)
    with pytest.raises(err):
        send(env, store, aid, answers)
    with pytest.raises(LookupError):
        send(env, store, "ia_nope", [(0, 0)])


def test_a_wrong_block_answer_can_be_followed_up(env):
    pid = organized(env)
    store, aid = stored(pid)
    ref = send(env, store, aid, [(0, 1)]).answers[0].answer_ref
    out = followup(env[0], None, "u1", pid, "s2", CheckFollowupIn(choice="unclear", answer_ref=ref),
                   now=NOW, new_id=env[2])
    assert out.cell == "c1.interpret"
    assert "z = -1 表示？" in out.quote.text and "正确答案「低一个标准差」" in out.quote.text


def test_a_block_without_cells_records_no_evidence(env):
    pid = organized(env)
    b = copy.deepcopy(QUIZ)
    for q in b["body"]["children"][1]["questions"]:
        q.pop("cell", None)
    store, aid = stored(pid, block=b)
    n = len(env[0].aspect_log("u1"))
    out = send(env, store, aid, [(0, 0)])
    assert out.answers[0].answer_ref is None and len(env[0].aspect_log("u1")) == n
