"""真题 on a page (wikiapp.exam_view): the quiz block and its asset, open items, self-reports, the
material's summary. MemoryRepo for the wiki, the disposable Postgres for materials; no LLM."""

import json
from datetime import timedelta

import pytest

from evowiki.belief.aspects import AddNote, Observe
from evowiki.deep.evoui import validate
from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.store import MemoryAssetStore
from evowiki.sources import exam_store, store
from evowiki.wikiapp.block_progress import MemoryProgressStore
from evowiki.wikiapp.block_results import block_results
from evowiki.wikiapp.exam_view import exam_summary, page_exam, quiz_block, self_report
from evowiki.wikiapp.repo import MemoryRepo, WikiInvalid
from evowiki.wikiapp.wiki_schemas import BlockResultsIn
from tests.runtime.helpers import make_user
from tests.sources.test_exam_store import _item
from tests.sources.test_store import _src
from tests.wikiapp.script import Script
from tests.wikiapp.test_block_events import item as event
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized

NINE = tuple(f"{chr(65 + i)}. 选项{i}" for i in range(9))
ITEMS = [
    _item("g", 0, kind="group", stem="阅读下面材料：某班成绩均值 70，标准差 10。", options=(),
          answer=None, has_key=False, node_id=None, attach="pending"),
    _item("i1", 1, parent_id="g", stem="z = -1 表示？", options=("A. 低一个标准差", "B. 低 1 分"),
          answer="A", page=5, node_id="c1", cell="c1.interpret"),
    _item("i2", 2, kind="multi", stem="哪些说法对？", options=("A. 甲", "B. 乙", "C. 丙"),
          answer="AC", explanation="甲和丙都对。", node_id="c2", cell="c2.term"),
    _item("i3", 3, kind="tf", stem="z 没有单位。", options=(), answer="错", node_id="c1"),
    _item("i4", 4, kind="open", stem="写出 z 的公式。", options=(), answer="z=(x-μ)/σ",
          node_id="c1", cell="c1.interpret"),
    _item("i5", 5, stem="没有答案的选择题", answer=None, has_key=False, node_id="c2"),
    _item("i6", 6, stem="九个选项", options=NINE, answer="B", node_id="c2"),
    _item("i7", 7, stem="别的点", node_id="zz"),
]


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    with pool.connection() as conn:
        store.insert_source(conn, _src("s1", "u1", sha="a", title="2023 真题", page_offset=2),
                            b"x", "application/pdf")
        store.add_use(conn, "u1", "s1", "exam", now=NOW)
        exam_store.replace_items(conn, "u1", "s1", ITEMS, gen=1, now=NOW)
    e = (MemoryRepo(["u1"]), Script(), Ids())
    pid = organized(e)
    return e, pid, pool


def _exam(env, assets):
    (repo, _, ids), pid, pool = env
    with pool.connection() as conn:
        return page_exam(conn, repo.load("u1"), assets, "u1", pid, now=NOW, new_id=ids)


def test_page_exam_quiz_asset_and_open_items(env):
    assets = MemoryAssetStore()
    out = _exam(env, assets)
    assert out.quiz_count == 3 and out.asset_id is not None
    assert [o.item_id for o in out.open_items] == ["i4", "i5", "i6"]  # i6: 9 options, refused
    assert out.open_items[2].options[:2] == ["选项0", "选项1"]   # the card draws the letters (no 「A. A. 选项0」)
    assert out.sources == ["2023 真题"] and out.frequent is True     # c1: i1, i3, i4
    a = assets.rows[out.asset_id]
    assert a.job_id.startswith(f"exam:{env[1]}:") and a.meta.kind == "evoui"
    assert a.meta.section_id == "s4" and a.meta.title == "真题练习"
    assert a.meta.interactive_id == "exam-" + a.job_id.rsplit(":", 1)[1]
    block = json.loads(a.html)
    assert validate(block) == [] and EvoUIBlock.model_validate(block)
    quiz = block["body"]
    assert (quiz["kind"], quiz["layout"], quiz["title"]) == ("quiz", "paged", "真题练习")
    q1, q2, q3 = quiz["questions"]
    assert q1["q"] == "材料：阅读下面材料：某班成绩均值 70，标准差 10。\n\nz = -1 表示？"
    assert (q1["correct"], q1["cell"], q1["why"]) == (0, "c1.interpret", "答案：A（见《2023 真题》p.3）")
    assert "type" not in q1
    assert (q2["type"], q2["correct"], q2["why"], q2["cell"]) == ("multi", [0, 2], "甲和丙都对。",
                                                                  "c2.term")
    assert (q3["type"], q3["options"], q3["correct"]) == ("tf", ["对", "错"], 1)
    assert "cell" not in q3
    i4 = out.open_items[0]
    assert (i4.kind, i4.has_key, i4.answer, i4.page, i4.printed_page, i4.done) == (
        "open", True, "z=(x-μ)/σ", 3, "1", False)
    assert i4.source_title == "2023 真题" and i4.context is None
    assert out.open_items[1].answer is None and out.open_items[1].has_key is False
    # the same items again: the stored asset is reused
    again = _exam(env, assets)
    assert again.asset_id == out.asset_id and len(assets.rows) == 1


def test_page_exam_questions_past_the_quiz_size_stay_listed(env, monkeypatch):
    monkeypatch.setattr("evowiki.wikiapp.exam_view.EXAM_QUIZ_SIZE", 2)
    out = _exam(env, MemoryAssetStore())
    assert out.quiz_count == 2
    assert [o.item_id for o in out.open_items] == ["i3", "i4", "i5", "i6"]


def test_page_exam_nothing_attached_and_unknown_page(env):
    (repo, _, ids), _, pool = env
    with pool.connection() as conn:
        exam_store.replace_items(conn, "u1", "s1", [_item("x", node_id="zz")], gen=2, now=NOW)
    out = _exam(env, MemoryAssetStore())
    assert (out.asset_id, out.quiz_count, out.open_items, out.frequent) == (None, 0, [], False)
    with pool.connection() as conn, pytest.raises(LookupError):
        page_exam(conn, repo.load("u1"), MemoryAssetStore(), "u1", "pg_nope", now=NOW, new_id=ids)


def test_page_exam_frequent_and_deleted_source(env):
    _, _, pool = env
    many = [_item(f"f{i}", i, kind="open", options=(), answer="x", node_id="c1") for i in range(3)]
    with pool.connection() as conn:
        exam_store.replace_items(conn, "u1", "s1", many, gen=2, now=NOW)
    out = _exam(env, MemoryAssetStore())
    assert out.frequent is True and out.asset_id is None and len(out.open_items) == 3
    with pool.connection() as conn:
        store.soft_delete(conn, "u1", "s1", NOW)
    out = _exam(env, MemoryAssetStore())
    assert (out.open_items, out.frequent) == ([], False)


def test_quiz_block_validates_and_caps_text():
    long = _item("l", stem="题" * 700, options=("A. 一", "B. 二"), answer="B")
    block = quiz_block([long], {}, "真题练习")
    assert validate(block) == []
    assert len(block["body"]["questions"][0]["q"]) == 600
    with pytest.raises(ValueError):
        quiz_block([_item("o", kind="open", options=())], {}, "真题练习")


def test_quiz_options_lose_their_printed_labels():
    """The quiz draws A / B / C itself: 「A. 1」 is shown as 「1」 (each option only its own label;
    nothing is taken off an option that would then be empty)."""
    it = _item("o", stem="选哪个？", options=("A. 1", "B、可逆", "(C) 三", "（D）四", "E)", "F 不变", "A. 错位"),
               answer="B")
    block = quiz_block([it], {}, "真题练习")
    assert block["body"]["questions"][0]["options"] == ["1", "可逆", "三", "四", "E)", "F 不变",
                                                        "A. 错位"]
    assert validate(block) == []


def test_exam_asset_answered_right_is_quiz_evidence(env):
    (repo, _, ids), _, _ = env
    assets = MemoryAssetStore()
    aid = _exam(env, assets).asset_id
    ev = {**event("exam", "quiz", 0, 0), "block": aid}
    out = block_results(repo, assets, None, "u1", aid, BlockResultsIn(events=[ev]), now=NOW,
                        new_id=ids, progress=MemoryProgressStore())
    assert [(e.status, e.correct) for e in out.events] == [("k", True)]
    [obs] = [e for e in repo.aspect_log("u1") if isinstance(e, Observe)
             and e.observation.evidence_ref == out.events[0].answer_ref]
    assert (obs.observation.kind, obs.observation.polarity, obs.observation.cell) == (
        "quiz", "+", "c1.interpret")


def _self(env, item_id, result, now=NOW):
    (repo, _, ids), _, pool = env
    return self_report(repo, pool, "u1", item_id, result, now=now, new_id=ids).status


def test_self_report_evidence_once_a_day(env):
    repo = env[0][0]
    assert _self(env, "i4", "right") == "k"
    [obs] = [e for e in repo.aspect_log("u1") if isinstance(e, Observe)
             and e.observation.evidence_ref.startswith("exam:i4:")]
    o = obs.observation
    assert (o.kind, o.polarity, o.strength, o.cell, o.evidence_ref) == (
        "self", "+", 0.5, "c1.interpret", "exam:i4:2026-10-08")
    [note] = [e for e in repo.aspect_log("u1") if isinstance(e, AddNote)
              and e.evidence_ref == o.evidence_ref]
    assert note.text == "我答对了" and "自评「我答对了」" in note.label
    assert _self(env, "i4", "wrong") == "duplicate"           # graded once a day
    assert _self(env, "i4", "right") == "duplicate"
    assert _self(env, "i4", "wrong", NOW + timedelta(days=1)) == "k"
    log = [e for e in repo.aspect_log("u1") if isinstance(e, Observe)
           and e.observation.evidence_ref.startswith("exam:i4:")]
    assert [e.observation.polarity for e in log] == ["+", "-"]


def test_self_report_score_done_and_refusals(env):
    repo = env[0][0]
    before = len(repo.aspect_log("u1"))
    assert _self(env, "i3", "right") == "score"               # no cell
    assert _self(env, "i5", "done") == "done"
    assert _self(env, "i5", "done") == "duplicate"
    assert len(repo.aspect_log("u1")) == before                # no evidence for either
    with pytest.raises(WikiInvalid, match="这道题没有答案，只能标做完"):
        _self(env, "i5", "right")
    for bad in ("nope", "g"):
        with pytest.raises(LookupError):
            _self(env, bad, "done")
    out = _exam(env, MemoryAssetStore())
    assert {o.item_id: o.done for o in out.open_items} == {"i4": False, "i5": True, "i6": False}


def test_exam_summary(env):
    _, _, pool = env
    with pool.connection() as conn:
        _undo_exam_use(conn)
        assert exam_summary(conn, "u1", "s1") is None              # no exam use
        store.add_use(conn, "u1", "s1", "exam", now=NOW)
        lost = [_item("u1x", 0, number="3", stem="已知函数 f(x)=x²，" + "求" * 70,
                      attach="unattached", node_id=None, attach_note="没有对应的知识点"),
                _item("u2x", 1, number=None, stem="短题", attach="unattached", node_id=None),
                _item("bad", 2, status="unreadable"), _item("ok", 3)]
        exam_store.replace_items(conn, "u1", "s1", lost, gen=1, now=NOW)
        s = exam_summary(conn, "u1", "s1")
        assert s is not None and s.state == "extracting"            # no record yet
        assert (s.items, s.attached, s.unattached, s.pending, s.unreadable, s.incomplete) == (
            4, 1, 2, 0, 1, 0)
        assert s.unattached_items[0].startswith("第 3 题：已知函数 f(x)=x²，")
        assert s.unattached_items[0].endswith("…（没有对应的知识点）")
        assert s.unattached_items[1] == "短题"
        store.set_progress(conn, "u1", "s1", {"exam": {"state": "failed", "detail": "题目没抽出来"}})
        s = exam_summary(conn, "u1", "s1")
        assert (s.state, s.detail) == ("failed", "题目没抽出来")


def _undo_exam_use(conn) -> None:
    for u in store.uses_of(conn, "u1", "s1"):
        if u["use"] == "exam":
            assert store.delete_use(conn, "u1", u["use_id"])


def test_undoing_the_exam_use_hides_the_block_and_frequent(env):
    """「这不是往年题」: the assistant's undo deletes the exam use; the page's 真题练习 block,
    its open items, 常考 and self-reports go with it."""
    out = _exam(env, MemoryAssetStore())
    assert out.quiz_count == 3 and out.frequent is True
    _, _, pool = env
    with pool.connection() as conn:
        _undo_exam_use(conn)
        assert exam_store.frequent_nodes(exam_store.exam_counts(conn, "u1")) == frozenset()
    out = _exam(env, MemoryAssetStore())
    assert (out.asset_id, out.quiz_count, out.open_items, out.frequent) == (None, 0, [], False)
    with pytest.raises(LookupError):
        _self(env, "i4", "right")


@pytest.mark.parametrize(("progress", "status", "want"), [
    ({}, "reading", ("extracting", None)),                              # the import is running
    ({"end": "done", "mode": "full"}, "ready", ("extracting", None)),   # the workflow is next
    ({"end": "done"}, "ready", ("extracting", None)),                   # an end before modes
    ({"end": "error", "detail": "坏了"}, "ready", ("failed", "题目没抽出来，请重试")),
    ({}, "failed", ("failed", "题目没抽出来，请重试")),
    ({"end": "done", "mode": "ask"}, "ready", None),                    # no extraction comes
])
def test_exam_summary_without_a_record_never_extracts_forever(env, progress, status, want):
    _, _, pool = env
    with pool.connection() as conn:
        store.set_progress(conn, "u1", "s1", progress)
        conn.execute("update source set status = %s where source_id = 's1'", (status,))
        s = exam_summary(conn, "u1", "s1")
    assert (None if s is None else (s.state, s.detail)) == want


def test_self_report_runs_k_change_and_records_signal(env):
    from evowiki.wikiapp.events import PageSaved, SignalRecorded
    repo, pid = env[0][0], env[1]
    n = len(repo._wiki["u1"])
    assert _self(env, "i4", "right") == "k"
    new = [e.body for e in repo._wiki["u1"][n:]]
    saved = [b for b in new if isinstance(b, PageSaved)]
    assert saved and all(b.action == "exam_self" and b.record.page_id == pid for b in saved)
    [sig] = [b.signal for b in new if isinstance(b, SignalRecorded)]
    assert (sig.kind, sig.page_id, sig.outcome) == ("block_result", pid, 1.0)
    assert "我答对了" in sig.text
    n = len(repo._wiki["u1"])
    assert _self(env, "i3", "wrong") == "score"                # no cell: signal only, no K change
    new = [e.body for e in repo._wiki["u1"][n:]]
    assert [type(b).__name__ for b in new] == ["SignalRecorded"] and new[0].signal.outcome == 0.0


def test_self_report_day_is_local_and_one_graded_key(env):
    _, _, pool = env
    late = NOW.replace(hour=20)                                # 04:00 next day in Asia/Shanghai
    assert _self(env, "i4", "wrong", late) == "k"
    log = [e for e in env[0][0].aspect_log("u1") if isinstance(e, Observe)]
    assert log[-1].observation.evidence_ref == "exam:i4:2026-10-09"
    assert _self(env, "i4", "right", late) == "duplicate"
    assert _self(env, "i4", "done", late) == "done"            # done has its own key
    with pool.connection() as conn:
        keys = {r["mark_key"] for r in conn.execute("select mark_key from source_item_mark").fetchall()}
        # a racing right after the check: the shared key refuses it
        assert not exam_store.add_mark(conn, "u1", "i4", "right", day=late.date().replace(day=9),
                                       at=late)
    assert keys == {"exam:i4:2026-10-09", "exam:i4:2026-10-09:done"}
