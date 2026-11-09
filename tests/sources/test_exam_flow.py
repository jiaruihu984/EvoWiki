"""The exam workflow body (`sources.exam_flow.run_exam`) in a virtual workflow, its start hooks
and the 资料加入 trigger: real Postgres, HashingEmbedder, a scripted JsonLLM. No model is ever
called."""

import threading
import time
from datetime import UTC, datetime

import pytest

from evowiki.agent import durable
from evowiki.belief.aspects import DefineConcept
from evowiki.goals.contracts import cell_id
from evowiki.goals.regions import GoalMap, MapNode
from evowiki.llm.gateway import LLMParseError
from evowiki.llm.providers import ProviderRejectedError
from evowiki.llm.usage import Account, BudgetExceededError
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import exam_flow, exam_store, store
from evowiki.sources.document import Block, DocPage, SourceDocument
from evowiki.sources.exam import ExamPlan, RawItem
from evowiki.sources.exam_attach import AttachPick, AttachPlan
from evowiki.sources.exam_flow import (
    DETAIL_FAILED,
    ExamDeps,
    run_exam,
    start_exam,
    wanted,
    workflow_id,
)
from evowiki.sources.models import LinkRow
from evowiki.wikiapp.events import GoalMapSaved, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.sources._fakes import FakeLLM
from tests.sources.test_importer import (
    add_source,
    env,  # noqa: F401  (the import fixture)
    run,
)

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
PAPER = ("# 选择题\n\n1. 均值怎么算？ A. 和除以个数 B. 中间的数\n\n"
         "2. 中位数是什么？ A. 排序后中间的值 B. 最大值\n\n"
         "# 简答题\n\n3. 说明均值和中位数的区别。\n").encode()
ITEMS = ExamPlan(items=[
    RawItem(number="1", kind="single", stem="均值怎么算？", options=["和除以个数", "中间的数"]),
    RawItem(number="2", kind="single", stem="中位数是什么？", options=["排序后中间的值", "最大值"]),
    RawItem(number="3", kind="open", stem="说明均值和中位数的区别。"),
    RawItem(number="4", kind="single", stem="", options=[]),             # incomplete
    RawItem(number="5", kind="open", stem="看不清", status="unreadable")])
PICKS = AttachPlan(picks=[AttachPick(item="q1", node="c1", aspect="procedure"),
                          AttachPick(item="q2", node="c2", aspect="no-such-aspect"),
                          AttachPick(item="q3", node=None)])
BUDGET = BudgetExceededError(Account(user_id="u1", group="other"), "day", 1.0)


def _ids():
    n = iter(range(1, 10_000))
    return lambda prefix: f"{prefix}-{next(n)}"


def _map(map_id, *nodes):
    return GoalMap(map_id=map_id, goal=f"学 {map_id}", created_at=NOW,
                   nodes=tuple(MapNode(id=c, kind="concept", name=name, state="expanded")
                               for c, name in nodes))


@pytest.fixture
def flow(env):  # noqa: F811
    pool, _box, import_deps = env
    repo = PgRepo(pool)
    repo.commit(repo.load("u1"), Commit(
        aspect=[DefineConcept(event_id=f"aev-{c}", ts=NOW, concept_id=c,
                              aspects=("term", "procedure")) for c in ("c1", "c2", "c3")],
        wiki=[WikiEvent(event_id="wev-m1", ts=NOW, body=GoalMapSaved(
                  record=_map("m1", ("c1", "均值"), ("c2", "中位数")), action="planned")),
              WikiEvent(event_id="wev-m2", ts=NOW, body=GoalMapSaved(
                  record=_map("m2", ("c3", "火山喷发")), action="planned"))]))
    add_source(pool, "s1", PAPER, kind="md")
    assert run(import_deps(), "s1")[-1][0] == "done"
    box = {"llm": None, "import_deps": import_deps, "slept": []}
    deps = ExamDeps(pool=pool, repo=repo, embedder=HashingEmbedder(),
                    llm_for=lambda _u: box["llm"], clock=lambda: NOW, new_id=_ids(),
                    sleep=box["slept"].append)
    return pool, repo, deps, box


def use(pool, u, sid="s1", goal_id=None):
    with pool.connection() as conn:
        store.add_use(conn, "u1", sid, u, goal_id=goal_id, now=NOW)


def go(deps, sid="s1"):
    with durable.virtual_workflow(f"source_exam:u1:{sid}:test"):
        return run_exam(deps, "u1", sid)


def row(pool, sid="s1"):
    with pool.connection() as conn:
        return store.get_source(conn, "u1", sid)


def items(pool, sid="s1"):
    with pool.connection() as conn:
        return exam_store.items_of_source(conn, "u1", sid)


def test_an_exam_use_extracts_attaches_and_stores_the_items(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam", goal_id="m1")
    box["llm"] = llm = FakeLLM(ITEMS, PICKS)
    before = row(pool).progress
    out = go(deps)
    want = {"state": "done", "items": 5, "attached": 2, "unattached": 1, "pending": 0,
            "unreadable": 1, "incomplete": 1}
    assert out == want
    got = items(pool)
    assert [(it.number, it.status, it.attach, it.node_id, it.cell) for it in got] == [
        ("1", "ok", "attached", "c1", cell_id("c1", "procedure")),
        ("2", "ok", "attached", "c2", None),            # aspect not one of c2's: no cell
        ("3", "ok", "unattached", None, None),
        ("4", "incomplete", "unattached", None, None),
        ("5", "unreadable", "unattached", None, None)]
    with pool.connection() as conn:
        assert exam_store.exam_gen(conn, "u1", "s1") == row(pool).run_gen
    progress = row(pool).progress
    assert progress["exam"] == want
    assert {k: v for k, v in progress.items() if k != "exam"} == before   # the import's keys kept
    assert [p for _r, p, _t in llm.calls] == ["sources.exam", "sources.exam_attach"]
    attach_prompt = llm.calls[1][2]
    assert "c1" in attach_prompt and "均值" in attach_prompt
    assert "c3" not in attach_prompt                     # the use names goal m1 only


def test_an_exam_use_without_a_goal_looks_at_every_goal(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam")
    box["llm"] = llm = FakeLLM(ITEMS, PICKS)
    assert go(deps)["state"] == "done"
    assert "c3" in llm.calls[1][2]


def test_the_sections_exercise_nodes_are_candidates_first(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam", goal_id="m2")                      # c1 is not in m2
    with pool.connection() as conn:
        sec = store.sections(conn, "u1", "s1")[0]
        store.replace_links(conn, "u1", ["c1"], [LinkRow(
            node_id="c1", section_id=sec.section_id, source_id="s1", role="exercises",
            by="intake")], by="intake", now=NOW)
    box["llm"] = llm = FakeLLM(ITEMS, PICKS)
    assert go(deps)["state"] == "done"
    prompt = llm.calls[1][2]
    assert "- q1：c1、c3" in prompt
    assert items(pool)[0].node_id == "c1"


def test_without_goals_every_question_is_unattached_without_an_attach_call(flow):
    from tests.runtime.helpers import make_user
    pool, _repo, deps, box = flow
    make_user(pool, "u3")                                # a user with no goal at all
    add_source(pool, "s9", PAPER, kind="md", user="u3")
    assert run(box["import_deps"](), "s9", user="u3")[-1][0] == "done"
    with pool.connection() as conn:
        store.add_use(conn, "u3", "s9", "exam", now=NOW)
    box["llm"] = llm = FakeLLM(ITEMS)
    with durable.virtual_workflow("source_exam:u3:s9:test"):
        out = run_exam(deps, "u3", "s9")
    assert out["attached"] == 0 and out["unattached"] == 3
    with pool.connection() as conn:
        got = exam_store.items_of_source(conn, "u3", "s9")
    assert got[0].attach_note == "还没有学习目标"
    assert [p for _r, p, _t in llm.calls] == ["sources.exam"]


@pytest.mark.parametrize("u", [None, "learn", "supplement", "ask"])
def test_no_exam_use_is_skipped_without_a_call(flow, u):
    pool, _repo, deps, box = flow
    if u is not None:
        use(pool, u)
    box["llm"] = llm = FakeLLM()
    assert go(deps) == {"state": "skipped"}
    assert llm.calls == [] and items(pool) == [] and "exam" not in row(pool).progress


def test_a_deleted_source_is_skipped(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam")
    with pool.connection() as conn:
        store.soft_delete(conn, "u1", "s1", NOW)
    box["llm"] = llm = FakeLLM()
    assert go(deps) == {"state": "skipped"} and llm.calls == []


def test_an_import_not_done_in_full_is_skipped(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam")
    with pool.connection() as conn:
        store.set_progress(conn, "u1", "s1", {**row(pool).progress, "mode": "ask"})
    box["llm"] = llm = FakeLLM()
    assert go(deps) == {"state": "skipped"} and llm.calls == []


@pytest.mark.parametrize("script", [
    [],                                                   # no model at all
    [BUDGET],
    [ProviderRejectedError("402 balance")],
    [LLMParseError("not json")] * 3,                      # the only batch failed, every try
    [ITEMS, BUDGET],                                      # the attach call hit the cap
])
def test_failures_end_failed_with_the_learner_detail(flow, script):
    pool, _repo, deps, box = flow
    use(pool, "exam")
    box["llm"] = None if not script else FakeLLM(*script)
    assert go(deps) == {"state": "failed"}
    # only a passing failure is tried again (after the waits); no model / 402 / the cap are not
    passing = len(script) == 3
    assert box["slept"] == (list(exam_flow.RETRY_WAITS_S) if passing else [])
    assert "再跟我说" not in DETAIL_FAILED
    progress = row(pool).progress
    assert progress["exam"] == {"state": "failed", "detail": DETAIL_FAILED}
    assert progress["end"] == "done"                     # the import's own keys stay
    assert items(pool) == []


def test_extracting_is_shown_while_the_model_reads(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam")
    seen = []

    class Peek(FakeLLM):
        def __call__(self, role, purpose, prompt, schema):
            if purpose == "sources.exam":
                seen.append(row(pool).progress.get("exam"))
            return super().__call__(role, purpose, prompt, schema)
    box["llm"] = Peek(ITEMS, PICKS)
    assert go(deps)["state"] == "done"
    assert seen == [{"state": "extracting"}]


def test_a_material_without_question_text_is_done_with_no_items(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam")
    with pool.connection() as conn:
        conn.execute("update source_section set text = '' where source_id = 's1'")
    box["llm"] = llm = FakeLLM()
    assert go(deps) == {"state": "done", "items": 0, "attached": 0, "unattached": 0,
                        "pending": 0, "unreadable": 0, "incomplete": 0}
    assert llm.calls == []


def test_a_reimport_meanwhile_stores_nothing(flow, monkeypatch):
    pool, _repo, deps, box = flow
    use(pool, "exam")
    real = exam_flow.attach_items

    def attach_then_reimport(*a, **kw):
        out = real(*a, **kw)
        with pool.connection() as conn:
            store.bump_run(conn, "u1", "s1")
        return out
    monkeypatch.setattr(exam_flow, "attach_items", attach_then_reimport)
    box["llm"] = FakeLLM(ITEMS, PICKS)
    assert go(deps) == {"state": "skipped"}
    assert items(pool) == []


# ---- the start hooks ------------------------------------------------------------------------

def test_wanted_needs_an_exam_use():
    assert wanted([{"use": "learn"}, {"use": "exam"}])
    assert not wanted([]) and not wanted([{"use": "supplement"}])


def test_start_exam_starts_only_exam_materials_imported_in_full(flow, monkeypatch):
    pool, _repo, deps, _box = flow
    started: list[str] = []
    monkeypatch.setattr(exam_flow, "_start", lambda wid, u, s: started.append(wid))
    exam_flow.install(deps)
    try:
        start_exam("u1", "s1")                           # no exam use
        use(pool, "exam")
        start_exam("u1", "s1")
        add_source(pool, "s3", b"x", kind="text")        # still importing
        use(pool, "exam", "s3")
        start_exam("u1", "s3")
        start_exam("u1", "nope")
    finally:
        exam_flow.uninstall()
    assert started == [workflow_id("u1", "s1", row(pool).run_gen)]
    assert started[0].startswith("source_exam:u1:s1:")


def test_start_exam_never_raises():
    exam_flow.uninstall()
    start_exam("u1", "s1")                               # no deps installed: logged only


def _hook_threads_done(timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while any(t.name == "source-exam-use" for t in threading.enumerate()):
        assert time.monotonic() < end, "the hook's thread did not end"
        time.sleep(0.05)


def test_an_exam_use_starts_the_extraction_once_committed(flow, monkeypatch):
    from evowiki.sources import events
    pool, _repo, deps, _box = flow
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(exam_flow, "start_exam",
                        lambda u, s, *, use_id=None: started.append((u, s, use_id)))
    exam_flow.install(deps)
    try:
        with pool.connection() as conn:
            uid = events.source_added(conn, "u1", "s1", "exam", NOW, goal_id="m1")
            time.sleep(0.3)
            assert started == []                         # not committed yet
        _hook_threads_done()
        assert started == [("u1", "s1", uid)]
        with pool.connection() as conn:                  # other uses start nothing here
            events.source_added(conn, "u1", "s1", "learn", NOW)
        _hook_threads_done()
    finally:
        exam_flow.uninstall()
    assert started == [("u1", "s1", uid)]


def test_a_second_exam_use_after_a_failed_run_starts_a_new_run(flow, monkeypatch):
    """A new exam use on the same import (said again after a failed run) gets its own workflow
    id, so it is not the failed run handed back."""
    pool, _repo, deps, box = flow
    use(pool, "exam")
    box["llm"] = FakeLLM(BUDGET)
    assert go(deps) == {"state": "failed"}
    started: list[str] = []
    monkeypatch.setattr(exam_flow, "_start", lambda wid, u, s: started.append(wid))
    exam_flow.install(deps)
    try:
        gen = row(pool).run_gen
        start_exam("u1", "s1")                           # the import's start: the old id
        start_exam("u1", "s1", use_id="use-2")           # the second 「这是往年题」
        with pool.connection() as conn:                  # an extraction going: no parallel run
            store.set_progress(conn, "u1", "s1",
                               {**row(pool).progress, "exam": {"state": "extracting"}})
        start_exam("u1", "s1", use_id="use-3")
    finally:
        exam_flow.uninstall()
    assert started == [workflow_id("u1", "s1", gen), f"source_exam:u1:s1:{gen}:use-2"]
    with pool.connection() as conn:
        store.set_progress(conn, "u1", "s1", {**row(pool).progress, "exam": {"state": "failed"}})
    box["llm"] = FakeLLM(ITEMS, PICKS)                   # the new run extracts again
    with durable.virtual_workflow(started[1]):
        assert run_exam(deps, "u1", "s1")["state"] == "done"
    assert len(items(pool)) == 5


def test_on_ready_starts_the_match_and_the_exam_apart(monkeypatch):
    from types import SimpleNamespace

    from evowiki.service import deps as service_deps
    from evowiki.sources import match_flow
    calls: list[str] = []

    def boom(_u, _s):
        calls.append("match")
        raise RuntimeError("match down")
    monkeypatch.setattr(match_flow, "start_match", boom)
    monkeypatch.setattr(exam_flow, "start_exam", lambda _u, _s: calls.append("exam"))
    svc = SimpleNamespace(pool=None, embedder=HashingEmbedder(), gateway=None, clock=lambda: NOW,
                          durable=object())
    d = service_deps.import_deps(svc)
    d.on_ready("u1", "s1")
    assert calls == ["match", "exam"]


# ---- review follow-ups ----------------------------------------------------------------------

def test_a_done_run_is_attached_again_without_a_second_extraction(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam", goal_id="m2")                      # m2 has no node these questions fit
    box["llm"] = FakeLLM(ITEMS, AttachPlan(picks=[]))
    assert go(deps)["attached"] == 0
    ids = [it.item_id for it in items(pool)]
    use(pool, "exam", goal_id="m1")                      # a goal that fits, made later
    box["llm"] = llm = FakeLLM(PICKS)
    out = go(deps)
    assert [p for _r, p, _t in llm.calls] == ["sources.exam_attach"]   # no extraction call
    assert out["attached"] == 2 and out["unattached"] == 1
    got = items(pool)
    assert [it.item_id for it in got] == ids             # the same items
    assert [it.node_id for it in got] == ["c1", "c2", None, None, None]


def test_a_passing_extraction_failure_is_tried_again_by_itself(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam")
    box["llm"] = FakeLLM(LLMParseError("not json"), ITEMS, PICKS)
    out = go(deps)
    assert out["state"] == "done" and out["attached"] == 2
    assert box["slept"] == [exam_flow.RETRY_WAITS_S[0]]


def test_pending_questions_are_attached_again_by_themselves(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam")
    box["llm"] = llm = FakeLLM(ITEMS, LLMParseError("not json"), PICKS)
    out = go(deps)
    assert out["state"] == "done" and out["attached"] == 2 and out["pending"] == 0
    assert "detail" not in out and box["slept"] == [exam_flow.RETRY_WAITS_S[0]]
    assert [p for _r, p, _t in llm.calls].count("sources.exam_attach") == 2


def test_every_attach_batch_failing_keeps_the_items_with_a_pending_detail(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam")
    box["llm"] = FakeLLM(ITEMS, *[LLMParseError("not json")] * 3)
    out = go(deps)
    assert box["slept"] == list(exam_flow.RETRY_WAITS_S)
    assert out["state"] == "done" and out["pending"] == 3 and out["attached"] == 0
    assert out["unattached"] == 0
    assert out["detail"] == exam_flow.DETAIL_PENDING
    assert row(pool).progress["exam"] == out
    assert len(items(pool)) == 5
    box["llm"] = FakeLLM(PICKS)                          # said again: attached, no extraction
    out = go(deps)
    assert out["attached"] == 2 and "detail" not in out


def test_a_material_deleted_before_attaching_makes_no_attach_call(flow, monkeypatch):
    pool, _repo, deps, box = flow
    use(pool, "exam")
    real = exam_flow._extract

    def extract_then_delete(*a):
        out = real(*a)
        with pool.connection() as conn:
            store.soft_delete(conn, "u1", "s1", NOW)
        return out
    monkeypatch.setattr(exam_flow, "_extract", extract_then_delete)
    box["llm"] = llm = FakeLLM(ITEMS)
    assert go(deps) == {"state": "skipped"}
    assert [p for _r, p, _t in llm.calls] == ["sources.exam"]


def test_extracted_items_point_at_the_stored_documents_block(flow):
    pool, _repo, deps, box = flow
    use(pool, "exam", goal_id="m1")
    doc = SourceDocument(kind="pdf", backend="mineru", version="4", pages=(DocPage(1),),
                         blocks=(Block("p1-b3", 1, "paragraph", "1. 均值怎么算？ A. 和除以个数",
                                       bbox=(0.1, 0.2, 0.9, 0.3)),))
    with pool.connection() as conn:
        conn.execute("update source_section set text = '〔p.1〕' || text, page_from = 1 "
                     "where source_id = 's1'")
        store.put_document(conn, "u1", "s1", doc, NOW)
    box["llm"] = FakeLLM(ITEMS, PICKS)
    assert go(deps)["state"] == "done"
    by = {it.number: it for it in items(pool)}
    assert by["1"].locator["block_id"] == "p1-b3" and by["1"].locator["page"] == 1
    assert by["2"].locator is None                 # its stem is not in the document
