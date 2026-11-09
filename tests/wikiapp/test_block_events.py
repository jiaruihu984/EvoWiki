"""EvoUI v2 result events (D3/D4): every item event graded against the stored block into K or the
score, summaries / routes / requests into the block_result signal, resent events skipped, progress
stored only when newer. The block is a plain dict with the node fields later tasks add (it is not
validated here). MemoryRepo + MemoryProgressStore, no network, no LLM."""

import copy
from dataclasses import replace

import pytest
from pydantic import ValidationError

from evowiki.belief.aspects import AddNote, Observe
from evowiki.deep.components import inject
from evowiki.deep.store import MemoryAssetStore
from evowiki.wikiapp.block_progress import MemoryProgressStore, Progress
from evowiki.wikiapp.block_results import block_progress, block_results
from evowiki.wikiapp.events import SignalRecorded
from evowiki.wikiapp.repo import MemoryRepo, WikiConflict, WikiInvalid
from evowiki.wikiapp.wiki_schemas import BlockAnswerIn, BlockEventIn, BlockResultsIn
from tests.wikiapp.script import Script
from tests.wikiapp.test_block_results import stored
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized

V2 = {"type": "evoui", "title": "v2 练习", "question": "怎么练？", "state": {}, "derived": {},
      "body": {"kind": "box", "children": [
          {"kind": "quiz", "id": "dq", "layout": "paged",
           "cases": [{"id": "sign", "label": "正负号", "advice": "回看符号"}],
           "questions": [
               {"q": "z = -1？", "options": ["低一个标准差", "低 1 分"], "correct": 0, "why": "w",
                "case": "sign", "cell": "c1.interpret"},
               {"q": "哪些对？", "type": "multi", "options": ["a", "b", "c"], "correct": [0, 2],
                "why": "w", "cell": "c1.interpret"},
               {"q": "无格子", "options": ["a", "b"], "correct": 1, "why": "w"}]},
          {"kind": "flashcards", "cards": [{"front": "あ", "back": "a", "cell": "c1.interpret"},
                                           {"front": "い", "back": "i"}]},
          {"kind": "fill-blank", "sentence": "z 的单位是 ___", "options": [{"label": "分"},
                                                                       {"label": "标准差"}],
           "correct": 1, "why": "w", "cell": "c1.interpret"},
          {"kind": "sentence", "sentence": "___ 除以 ___", "words": ["σ", "a", "x − μ"],
           "answer": [2, 0], "why": "w"},
          {"kind": "route", "prompt": "想怎么学？", "preference": "teaching_mode",
           "options": [{"label": "看图", "value": "visual", "next": "先看图"},
                       {"label": "读文", "value": "text", "next": "先读文字"}]},
          {"kind": "app", "id": "lab", "html": "<p>x</p>",
           "items": [{"id": "spin", "label": "转一圈", "cell": "c1.interpret"},
                     {"id": "free", "label": "随便玩"}]}]},
      "fallback": "f"}


def item(node, kind, it, response, *, attempt=1, revision=5, hint=False, correct=None):
    return {"type": "item", "block": "ia_q", "node": node, "kind": kind, "item": it,
            "attempt": attempt, "revision": revision, "response": response, "correct": correct,
            "hintUsed": hint, "latencyMs": 1200}


ALL = [
    item("dq", "quiz", 0, 1),                                  # 0 wrong, cell → k
    item("dq", "quiz", 1, [2, 0], hint=True),                  # 1 multi right, hint → k 0.5
    item("dq", "quiz", 2, 1),                                  # 2 right, no cell → score
    item("flashcards#0", "flashcard", 0, "know"),              # 3 → k
    item("flashcards#0", "flashcard", 1, "missed"),            # 4 → score, wrong
    item("fill-blank#0", "fill-blank", 0, 1),                  # 5 right → k
    item("sentence#0", "sentence", 0, [2, 0]),                 # 6 right → score
    item("lab", "app", "spin", {"turns": 1}, correct=True),    # 7 client → k 0.6
    item("lab", "app", "free", {"x": 2}),                      # 8 unjudged → signal
    {"type": "summary", "block": None, "node": "dq", "kind": "quiz", "attempt": 1, "revision": 6,
     "total": 3, "right": 2, "missed": [0], "timeMs": 42000, "hintsUsed": 1},   # 9
    {"type": "route", "block": None, "node": "route#0", "option": "visual", "next": "先看图",
     "preference": "teaching_mode", "revision": 7},                            # 10
    {"type": "request", "block": None, "node": "flashcards#0", "what": "more", "revision": 8},  # 11
]


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def post(env, store, prog, aid, events=(), answers=(), progress=None, state=None):
    body = BlockResultsIn(events=list(events), answers=list(answers), progress=progress,
                          state=state)
    return block_results(env[0], store, None, "u1", aid, body, now=NOW, new_id=env[2],
                         progress=prog)


def observes(env) -> dict[str, Observe]:
    return {e.observation.evidence_ref: e for e in env[0].aspect_log("u1") if isinstance(e, Observe)}


def last_signal(env):
    return [e.body.signal for e in env[0]._wiki["u1"] if isinstance(e.body, SignalRecorded)][-1]


def test_every_event_kind_is_graded_and_reported(env):
    pid = organized(env)
    store, aid = stored(pid, block=V2)
    prog = MemoryProgressStore()
    out = post(env, store, prog, aid, ALL)
    st = [(o.index, o.status, o.correct, o.graded) for o in out.events]
    assert st == [(0, "k", False, "server"), (1, "k", True, "server"), (2, "score", True, "server"),
                  (3, "k", True, "server"), (4, "score", False, "server"),
                  (5, "k", True, "server"), (6, "score", True, "server"),
                  (7, "k", True, "client"), (8, "signal", None, "client"),
                  (9, "signal", None, None), (10, "signal", None, None),
                  (11, "signal", None, None)]
    assert (out.right, out.total) == (6, 8)
    assert [(c.case, c.right, c.total) for c in out.cases] == [("sign", 0, 1)]
    obs = observes(env)
    refs = [o.answer_ref for o in out.events if o.status == "k"]
    assert all(r.startswith(f"block:{pid}:s2:{aid}:") for r in refs)
    got = [(obs[r].observation.polarity, obs[r].observation.strength, obs[r].observation.kind)
           for r in refs]
    assert got == [("-", 1.0, "quiz"), ("+", 0.5, "quiz"), ("+", 1.0, "quiz"), ("+", 1.0, "quiz"),
                   ("+", 0.6, "quiz")]
    notes = {e.evidence_ref: e for e in env[0].aspect_log("u1") if isinstance(e, AddNote)}
    assert notes[refs[0]].text == "低 1 分" and "正确答案「低一个标准差」" in notes[refs[0]].label
    assert "选了「a / c」" in notes[refs[1]].label
    assert notes[refs[3]].label.startswith("填空：") and "正确答案「标准差」" in notes[refs[3]].label
    sig = last_signal(env)
    assert sig.signal_id == out.signal_id and sig.outcome == pytest.approx(6 / 8)
    assert "测验答对 6/8" in sig.text and "dq 第 1 轮完成 2/3" in sig.text
    assert "route#0 选了「看图」→先看图（偏好 teaching_mode = \"visual\"）" in sig.text
    assert "flashcards#0 要更多（more）" in sig.text and 'lab free：{"x": 2}' in sig.text


def test_resent_events_are_duplicates_and_never_counted_twice(env):
    pid = organized(env)
    store, aid = stored(pid, block=V2)
    prog = MemoryProgressStore()
    post(env, store, prog, aid, ALL)
    n_aspect, n_wiki = len(env[0].aspect_log("u1")), len(env[0]._wiki["u1"])
    again = post(env, store, prog, aid, ALL)
    assert {o.status for o in again.events} == {"duplicate"}
    assert (again.right, again.total, again.signal_id) == (0, 0, None)
    assert (len(env[0].aspect_log("u1")), len(env[0]._wiki["u1"])) == (n_aspect, n_wiki)
    # a new attempt of the same item is new; a repeat inside one post is a duplicate
    out = post(env, store, prog, aid, [item("dq", "quiz", 0, 0, attempt=2, revision=9)] * 2)
    assert [o.status for o in out.events] == ["k", "duplicate"] and (out.right, out.total) == (1, 1)


def test_two_renders_without_restored_progress_are_both_counted(env):
    """attempt / revision restart in a render that did not restore progress: the render's `session`
    keeps a fresh answer from passing for a resend; the same session resent is still a duplicate."""
    pid = organized(env)
    store, aid = stored(pid, block=V2)
    prog = MemoryProgressStore()
    first = {**item("dq", "quiz", 0, 0, attempt=1, revision=3), "session": "a1b2c3d4e5f60718"}
    second = {**first, "session": "0f0e0d0c0b0a0908"}           # same node / item / attempt / revision
    one = post(env, store, prog, aid, [first])
    two = post(env, store, prog, aid, [second])
    assert [o.status for o in one.events] == ["k"] and [o.status for o in two.events] == ["k"]
    assert (two.right, two.total) == (1, 1)
    again = post(env, store, prog, aid, [second])
    assert [o.status for o in again.events] == ["duplicate"] and again.signal_id is None
    # no session (an older client): the old key, so the same event resent is a duplicate as before
    bare = item("dq", "quiz", 0, 0, attempt=1, revision=3)
    assert [o.status for o in post(env, store, prog, aid, [bare]).events] == ["k"]
    assert [o.status for o in post(env, store, prog, aid, [bare]).events] == ["duplicate"]


def test_an_answer_the_same_post_also_sends_as_an_event_counts_once(env):
    pid = organized(env)
    store, aid = stored(pid, block=V2)
    out = post(env, store, MemoryProgressStore(), aid, [item("dq", "quiz", 0, 0)],
               answers=[{"quiz": 0, "question": 0, "chosen": 0},
                        {"quiz": 0, "question": 2, "chosen": 1}])
    assert [(a.quiz, a.question) for a in out.answers] == [(0, 2)]
    assert (out.right, out.total) == (2, 2)


def test_progress_is_stored_only_when_newer(env):
    pid = organized(env)
    store, aid = stored(pid, block=V2)
    prog = MemoryProgressStore()
    assert block_progress(store, prog, "u1", aid).model_dump() == {"revision": 0, "data": None}
    p5 = {"revision": 5, "data": {"v": 2, "revision": 5, "state": {}, "nodes": {"dq": {"i": 1}}}}
    out = post(env, store, prog, aid, progress=p5)
    assert (out.progress_revision, out.progress_stale, out.signal_id) == (5, False, None)
    old = {"revision": 4, "data": {"v": 2, "revision": 4, "state": {}, "nodes": {}}}
    out = post(env, store, prog, aid, progress=old)
    assert (out.progress_revision, out.progress_stale) == (5, True)
    out = post(env, store, prog, aid, progress=p5)                     # same revision: stale
    assert (out.progress_revision, out.progress_stale) == (5, True)
    got = block_progress(store, prog, "u1", aid)
    assert got.revision == 5 and got.data == p5["data"]
    zero = post(env, store, MemoryProgressStore(), aid, progress={"revision": 0, "data": {}})
    assert (zero.progress_revision, zero.progress_stale) == (0, True)
    with pytest.raises(LookupError):
        block_progress(store, prog, "u1", "ia_nope")


@pytest.mark.parametrize("ev", [
    item("nope", "quiz", 0, 0),                                 # unknown node
    item("dq", "quiz", 3, 0),                                   # no such question
    item("dq", "quiz", 0, 2),                                   # no such option
    item("dq", "quiz", 0, True),                                # a bool is not an index
    item("dq", "quiz", 1, [0, 0]),                              # multi: repeated
    item("dq", "quiz", 1, 0),                                   # multi: not a list
    item("dq", "flashcard", 0, 0),                              # wrong kind for the node
    item("flashcards#0", "flashcard", 2, "know"),               # no such card
    item("flashcards#0", "flashcard", 0, "yes"),                # not know / missed
    item("flashcards#0", "flashcard", 0, ["know"]),             # unhashable: still 422
    item("flashcards#0", "flashcard", 0, {"know": 1}),
    item("fill-blank#0", "fill-blank", 1, 0),                   # only item 0
    item("fill-blank#0", "fill-blank", 0, 5),
    item("sentence#0", "sentence", 0, [2]),                     # one word per blank
    item("sentence#0", "sentence", 0, [2, 9]),
    item("lab", "app", "undeclared", 1, correct=True),
    item("lab", "app", 0, 1, correct=True),                     # app items are ids
    {"type": "route", "node": "route#0", "option": "audio", "preference": "teaching_mode",
     "revision": 1},
    {"type": "route", "node": "route#0", "option": "visual", "preference": None, "revision": 1},
    {"type": "route", "node": "dq", "option": "visual", "revision": 1},
    {"type": "summary", "node": "pager#0", "attempt": 1, "revision": 1},
    {"type": "request", "node": "nope", "what": "more", "revision": 1},
])
def test_an_event_the_block_lacks_refuses_the_whole_post(env, ev):
    pid = organized(env)
    store, aid = stored(pid, block=V2)
    prog = MemoryProgressStore()
    n = (len(env[0].aspect_log("u1")), len(env[0]._wiki["u1"]))
    with pytest.raises(WikiInvalid):
        post(env, store, prog, aid, [ALL[0], ev],
             progress={"revision": 3, "data": {"v": 2}})
    assert (len(env[0].aspect_log("u1")), len(env[0]._wiki["u1"])) == n
    assert prog.get("u1", aid) is None and prog.events == {}


def test_input_shapes():
    ev = BlockEventIn.model_validate(item("dq", "quiz", 0, 1, hint=True))
    assert ev.hint_used is True and ev.latency_ms == 1200
    for bad in ({"type": "item", "node": "dq", "revision": 1},                     # no kind/item
                {"type": "summary", "node": "dq", "revision": 1},                  # no attempt
                {"type": "route", "node": "r", "revision": 1},                     # no option
                {"type": "request", "node": "r", "revision": 1},                   # no what
                {**item("lab", "app", "x", "y" * 2001)},                           # response size
                {**item("dq", "quiz", 0, 1), "extra": 1},
                {**item("dq", "quiz", 0, 1), "session": "ABCDEF12"},                # not lower hex
                {**item("dq", "quiz", 0, 1), "session": "abc"},                     # too short
                {**item("dq", "quiz", 0, 1), "session": "a" * 33},                  # too long
                {**item("dq", "quiz", 0, 1), "revision": 2_147_483_648}):           # past Postgres integer
        with pytest.raises(ValidationError):
            BlockEventIn.model_validate(bad)
    with pytest.raises(ValidationError):
        BlockResultsIn.model_validate({"events": [item("dq", "quiz", 0, 1)] * 201})
    with pytest.raises(ValidationError):
        BlockResultsIn.model_validate({"progress": {"revision": 1, "data": {"x": "y" * 20001}}})
    with pytest.raises(ValidationError):
        BlockResultsIn.model_validate({})
    assert BlockEventIn.model_validate({**item("dq", "quiz", 0, 1), "session": "0123456789abcdef",
                                        "revision": 2_147_483_647}).session == "0123456789abcdef"
    assert BlockResultsIn.model_validate(
        {"progress": {"revision": 2_147_483_647, "data": {}}}).progress.revision == 2_147_483_647
    with pytest.raises(ValidationError):
        BlockResultsIn.model_validate({"progress": {"revision": 2_147_483_648, "data": {}}})
    assert BlockAnswerIn(quiz=0, question=40, chosen=0).question == 40     # no count cap: the stored block decides
    with pytest.raises(ValidationError):
        BlockAnswerIn(quiz=0, question=-1, chosen=0)


def test_events_without_a_progress_store_fail_loudly(env):
    pid = organized(env)
    store, aid = stored(pid, block=V2)
    with pytest.raises(RuntimeError):
        post(env, store, None, aid, [ALL[0]])


def test_a_racing_writer_conflicts():
    prog = MemoryProgressStore()
    prog.events_writer("u1", "ia_q", ["dq:0:1:5"], at=NOW)(None)
    with pytest.raises(WikiConflict):
        prog.events_writer("u1", "ia_q", ["dq:0:1:5", "dq:1:1:5"], at=NOW)(None)
    assert set(prog.events[("u1", "ia_q")]) == {"dq:0:1:5"}
    assert prog.put("u1", "ia_q", Progress(2, {"a": 1}), at=NOW) == (2, True)


def test_cells_unknown_to_k_only_score(env):
    pid = organized(env)
    b = copy.deepcopy(V2)
    b["body"]["children"][1]["cards"][0]["cell"] = "zz.term"
    store, aid = stored(pid, block=b)
    out = post(env, store, MemoryProgressStore(), aid, [ALL[3]])
    assert out.events[0].status == "score" and out.events[0].answer_ref is None


def _requests(n: int, base: int) -> list[dict]:
    return [{"type": "request", "node": "flashcards#0", "what": "more", "revision": base + i}
            for i in range(n)]


def test_event_lines_come_first_and_must_fit_the_signal(env):
    from evowiki.wikiapp.block_results import SIGNAL_TEXT_MAX
    pid = organized(env)
    store, aid = stored(pid, block=V2)
    prog = MemoryProgressStore()
    head, line = "交互结果《v2 练习》", "flashcards#0 要更多（more）"
    n = max(k for k in range(1, 200) if len("；".join([head] + [line] * k)) <= SIGNAL_TEXT_MAX)
    big_state = {"s": "x" * 3900}
    out = post(env, store, prog, aid, [ALL[0]] + _requests(n, 100), state=big_state)
    sig = last_signal(env)
    assert sig.signal_id == out.signal_id and len(sig.text) == SIGNAL_TEXT_MAX
    assert sig.text.startswith("；".join([head] + [line] * n))     # every event line kept
    assert "x" * 3900 not in sig.text                               # the state tail was cut instead
    before = (len(env[0].aspect_log("u1")), len(env[0]._wiki["u1"]),
              {k: dict(v) for k, v in prog.events.items()})
    with pytest.raises(WikiInvalid):
        post(env, store, prog, aid, [ALL[1]] + _requests(n + 1, 1000),
             progress={"revision": 3, "data": {"v": 2}})
    assert (len(env[0].aspect_log("u1")), len(env[0]._wiki["u1"]),
              {k: dict(v) for k, v in prog.events.items()}) == before
    assert prog.get("u1", aid) is None


def test_a_flashcards_round_batch_counts_each_card_once(env):
    """evoui.js sends a flashcards round as one item per card (final ratings, all at the summary's
    revision) plus the summary: distinct keys, each card graded once, a resend is all duplicates."""
    pid = organized(env)
    store, aid = stored(pid, block=V2)
    prog = MemoryProgressStore()
    batch = [item("flashcards#0", "flashcard", 0, "know", revision=9, correct=True),
             item("flashcards#0", "flashcard", 1, "missed", revision=9, correct=False),
             {"type": "summary", "block": "ia_q", "node": "flashcards#0", "kind": "flashcard", "attempt": 1,
              "revision": 9, "total": 2, "right": 1, "missed": [1], "timeMs": 2400, "hintsUsed": 0}]
    out = post(env, store, prog, aid, batch)
    assert [(o.status, o.correct) for o in out.events] == [("k", True), ("score", False), ("signal", None)]
    assert (out.right, out.total) == (1, 2)
    refs = [o.answer_ref for o in out.events if o.status == "k"]
    assert [observes(env)[r].observation.polarity for r in refs] == ["+"]
    again = post(env, store, prog, aid, batch)
    assert [o.status for o in again.events] == ["duplicate"] * 3


def test_a_summary_finished_while_a_timer_ran_carries_timer_ms(env):
    """EvoUI v2 Task 5a: a practice summary sent while a timer node runs carries `timerMs` (optional,
    whole ms >= 0); it is accepted and reaches the signal text next to the node's own time."""
    summary = {"type": "summary", "block": "ia_q", "node": "dq", "kind": "quiz", "attempt": 1, "revision": 6,
               "total": 3, "right": 2, "missed": [0], "timeMs": 42000, "hintsUsed": 0, "timerMs": 61400}
    assert BlockEventIn.model_validate(summary).timer_ms == 61400
    assert BlockEventIn.model_validate({k: v for k, v in summary.items() if k != "timerMs"}).timer_ms is None
    for bad in (-1, 1.5, "61s"):
        with pytest.raises(ValidationError):
            BlockEventIn.model_validate({**summary, "timerMs": bad})
    pid = organized(env)
    store, aid = stored(pid, block=V2)
    out = post(env, store, MemoryProgressStore(), aid, [summary])
    assert [o.status for o in out.events] == ["signal"]
    assert "dq 第 1 轮完成 2/3 没过 0 用时 42 秒 计时 61 秒" in last_signal(env).text


def test_task_5b_events_from_a_validated_block(env):
    """EvoUI v2 Task 5b, the events exactly as evoui.js sends them for the practice demo (a validated
    block): a fill-blank whose options are {label, why} objects is graded by its label, a sentence by
    word indices, the pager's summary (missed = node keys) and the route (+ preference) reach the
    signal text."""
    from evowiki.deep.evoui import validate
    from tests.deep.test_evoui_practice_nodes import DEMO
    assert validate(DEMO) == []
    pid = organized(env)
    store, aid = stored(pid, block=DEMO)
    evs = [{"type": "route", "block": "ia_q", "node": "how", "option": "quiz", "next": "请出几道 z 分数的练习题考我。",
            "preference": "teaching_mode", "revision": 1},
           item("fb", "fill-blank", 0, 1, revision=2, correct=False),
           {"type": "summary", "block": "ia_q", "node": "fb", "kind": "fill-blank", "attempt": 1, "revision": 2,
            "total": 1, "right": 0, "missed": [0], "timeMs": 3000, "hintsUsed": 0},
           item("sb", "sentence", 0, [0, 2, 1], revision=3, correct=True),
           {"type": "summary", "block": "ia_q", "node": "pg", "kind": "pager", "attempt": 1, "revision": 9,
            "total": 4, "right": 3, "missed": ["fb"], "timeMs": 52000, "hintsUsed": 0}]
    out = post(env, store, MemoryProgressStore(), aid, evs)
    assert [(o.status, o.correct) for o in out.events] == [("signal", None), ("k", False), ("signal", None),
                                                           ("score", True), ("signal", None)]
    note = {e.evidence_ref: e for e in env[0].aspect_log("u1") if isinstance(e, AddNote)}[out.events[1].answer_ref]
    assert note.text == "15" and "正确答案「1.5」" in note.label
    text = last_signal(env).text
    assert "how 选了「考考我」→请出几道 z 分数的练习题考我。（偏好 teaching_mode = \"quiz\"）" in text
    assert "pg 第 1 轮完成 3/4 没过 fb 用时 52 秒" in text
    bad = [{**evs[0], "preference": None}]                     # the node's preference must be echoed
    with pytest.raises(WikiInvalid):
        post(env, store, MemoryProgressStore(), aid, bad)


def test_route_values_compare_numbers_numerically(env):
    """Fix round 1: a stored route value 2.0 equals the client's 2 (JavaScript has one number type), so the
    post is accepted; a bool never equals a number and text never equals a number."""
    from evowiki.wikiapp.block_results import _same
    assert _same(2.0, 2) and _same(2, 2.0) and _same("a", "a")
    assert not _same(1, True) and not _same(True, 1) and not _same("2", 2) and not _same(0, False)
    block = copy.deepcopy(V2)
    route = block["body"]["children"][4]
    route["options"] = [{"label": "两倍", "value": 2.0, "next": "按两倍讲"}, {"label": "一倍", "value": 1, "next": "按一倍讲"}]
    pid = organized(env)
    store, aid = stored(pid, block=block)
    ev = {"type": "route", "block": "ia_q", "node": "route#0", "option": 2, "next": "按两倍讲",
          "preference": "teaching_mode", "revision": 3}
    out = post(env, store, MemoryProgressStore(), aid, [ev])
    assert [o.status for o in out.events] == ["signal"]
    assert "route#0 选了「两倍」→按两倍讲" in last_signal(env).text
    with pytest.raises(WikiInvalid):                            # true is not the value 1
        post(env, store, MemoryProgressStore(), aid, [{**ev, "option": True, "revision": 4}])


APP_BLOCK = {"type": "evoui", "title": "小程序", "question": "转一转", "state": {}, "derived": {},
             "body": {"kind": "box", "children": [
                 {"kind": "text", "text": "转动看看"},
                 {"kind": "app", "id": "spinner", "height": 360,
                  "html": "<button onclick=\"evowiki.item('turn', {deg: 90}, true)\">转</button>"
                          "<script>evowiki.ready()</script>",
                  "items": [{"id": "turn", "label": "转到 90 度", "cell": "c1.interpret"},
                            {"id": "play", "label": "随便玩"}]}]},
             "fallback": "转到 90 度时投影为 0。"}


def test_a_validator_valid_app_blocks_items_are_graded_and_undeclared_ones_refused(env):
    from evowiki.deep.evoui import validate
    assert validate(APP_BLOCK) == []
    pid = organized(env)
    store, aid = stored(pid, block=APP_BLOCK)
    prog = MemoryProgressStore()
    evs = [item("spinner", "app", "turn", {"deg": 90}, correct=True),
           item("spinner", "app", "turn", {"deg": 45}, revision=6, correct=False, hint=True),
           item("spinner", "app", "play", [1, 2], revision=7),
           {"type": "summary", "block": "ia_q", "node": "spinner", "kind": "app", "attempt": 1, "revision": 8,
            "total": 2, "right": 1, "missed": ["turn"], "timeMs": 5000, "hintsUsed": 1}]
    out = post(env, store, prog, aid, evs)
    assert [(o.status, o.correct, o.graded) for o in out.events] == [
        ("k", True, "client"), ("k", False, "client"), ("signal", None, "client"), ("signal", None, None)]
    obs = observes(env)
    assert [(obs[o.answer_ref].observation.polarity, obs[o.answer_ref].observation.strength)
            for o in out.events[:2]] == [("+", 0.6), ("-", 0.5)]
    sig = last_signal(env)
    assert "spinner play：[1, 2]" in sig.text and "spinner 第 1 轮完成 1/2" in sig.text
    n_aspect = len(env[0].aspect_log("u1"))
    with pytest.raises(WikiInvalid):
        post(env, store, prog, aid, [item("spinner", "app", "nope", 1, revision=9, correct=True)])
    assert len(env[0].aspect_log("u1")) == n_aspect


def test_latency_is_bounded_to_an_hour():
    BlockEventIn.model_validate(item("lab", "app", "spin", 1) | {"latencyMs": 3_600_000})
    with pytest.raises(ValidationError):
        BlockEventIn.model_validate(item("lab", "app", "spin", 1) | {"latencyMs": 3_600_001})


# ---- a graded library component (an `html` asset with an answer key) ----

SORT = {"title": "分一分", "question": "放哪？", "mode": "classify",
        "targets": [{"id": "a", "label": "甲", "hint": None}, {"id": "b", "label": "乙", "hint": None}],
        "items": [{"id": "1", "label": "x", "answer": "a", "explain": "e", "wrong_reasons": [], "cell": "c1.interpret"},
                  {"id": "2", "label": "y", "answer": "b", "explain": "e", "wrong_reasons": [], "cell": "c1.interpret"},
                  {"id": "3", "label": "z", "answer": "a", "explain": "e", "wrong_reasons": [], "cell": None},
                  {"id": "4", "label": "w", "answer": "b", "explain": "e", "wrong_reasons": [], "cell": "c1.interpret"}]}


def from_blocks(env):
    return {r: o for r, o in observes(env).items() if r.startswith("block:")}


def stored_component(pid, config=SORT):
    store, aid = stored(pid, kind="html")
    a = store.get("u1", aid)
    html = inject("<!doctype html><body><p>x</p><script>1</script></body>", config)
    fresh = MemoryAssetStore()
    fresh.writer([replace(a, html=html)])(None)
    return fresh, aid


def placed(it, target):
    return {"type": "item", "node": "component", "kind": "component", "item": it, "attempt": 1, "response": target, "revision": 0}


def test_a_component_placement_is_graded_against_the_stored_config_not_the_client(env):
    pid = organized(env)
    store, aid = stored_component(pid)
    prog = MemoryProgressStore()
    # the client's own `correct` says the opposite of the key
    out = post(env, store, prog, aid, [{**placed("1", "a"), "correct": False}, {**placed("2", "a"), "correct": True},
                                       placed("3", "a")])
    assert [(o.status, o.correct, o.graded) for o in out.events] == [
        ("k", True, "server"), ("k", False, "server"), ("score", True, "server")]
    assert (out.right, out.total) == (2, 3)
    got = [(o.observation.polarity, o.observation.strength, o.observation.kind) for o in from_blocks(env).values()]
    assert got == [("+", 0.6, "quiz"), ("-", 0.6, "quiz")]
    note = [e for e in env[0].aspect_log("u1") if isinstance(e, AddNote)][-1]
    assert "「y」放进「甲」" in note.label and "正确答案「乙」" in note.label


def test_only_the_first_placement_of_an_item_counts_whatever_render_sends_it(env):
    pid = organized(env)
    store, aid = stored_component(pid)
    prog = MemoryProgressStore()
    post(env, store, prog, aid, [placed("2", "a")])
    again = post(env, store, prog, aid, [placed("2", "b"), {**placed("2", "b"), "session": "abcdef0123456789"}])
    assert [o.status for o in again.events] == ["duplicate", "duplicate"]
    assert [o.observation.polarity for o in from_blocks(env).values()] == ["-"]


def test_an_answer_shown_first_is_no_evidence_and_spends_the_item(env):
    pid = organized(env)
    store, aid = stored_component(pid)
    prog = MemoryProgressStore()
    out = post(env, store, prog, aid, [placed("4", None)])
    assert [(o.status, o.correct) for o in out.events] == [("signal", None)]
    assert (out.right, out.total) == (0, 0)
    later = post(env, store, prog, aid, [placed("4", "b")])    # a reloaded page: it may not count now
    assert [o.status for o in later.events] == ["duplicate"]
    assert from_blocks(env) == {}


@pytest.mark.parametrize("bad", [placed("9", "a"), placed("1", "z"), placed("1", 1), placed(1, "a"),
                                 {"type": "summary", "node": "component", "kind": "component", "attempt": 1, "revision": 0}])
def test_a_component_event_the_config_does_not_have_is_refused(env, bad):
    pid = organized(env)
    store, aid = stored_component(pid)
    with pytest.raises(WikiInvalid):
        post(env, store, MemoryProgressStore(), aid, [bad])
    assert from_blocks(env) == {}


def test_a_component_without_an_answer_key_takes_no_results(env):
    pid = organized(env)
    store, aid = stored_component(pid, {"title": "t", "question": "q", "steps": []})
    with pytest.raises(WikiInvalid):
        post(env, store, MemoryProgressStore(), aid, [placed("1", "a")])
    old, oid = stored(pid, kind="html")                        # an asset without a config
    with pytest.raises(WikiInvalid):
        post(env, old, MemoryProgressStore(), oid, [placed("1", "a")])


def test_a_component_takes_no_progress(env):
    pid = organized(env)
    store, aid = stored_component(pid)
    with pytest.raises(WikiInvalid):
        post(env, store, MemoryProgressStore(), aid, progress={"revision": 1, "data": {"revision": 1}})
