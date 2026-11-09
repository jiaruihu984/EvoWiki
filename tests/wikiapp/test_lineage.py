"""Artifact lineage (0091): what a wiki commit registers (`wikiapp.lineage`, hooked in
`Repo.commit`), the ids views show, and signals targeting the version shown (ruling 3)."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from evowiki.assistant.lineage import answer_artifact, compaction_artifact
from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.goals.review import sources_from_page
from evowiki.nodes.model import GraphChange, NodeGraph, NodeRec
from evowiki.store.artifacts import MemoryArtifacts, artifact_id, is_artifact_id
from evowiki.teach.contracts import EVIDENCE, Signal, SpecItem, TeachingSpec
from evowiki.wikiapp.events import (
    GraphSaved,
    PageSaved,
    SignalRecorded,
    TeachingSpecSaved,
    WikiEvent,
)
from evowiki.wikiapp.lineage import commit_lineage, section_artifact
from evowiki.wikiapp.page import open_page_flow
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.review import card_id, grade_review, review_today
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import CheckAnswerIn, ReviewGradeIn
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_review_flow import LATER, NOW, organized_learned_page


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def arts(repo: MemoryRepo, kind: str):
    return {a.ref: a for a in repo.artifacts.of_kind(kind)}


def signals(repo: MemoryRepo) -> list[Signal]:
    return [e.body.signal for e in repo.wiki_log("u1") if isinstance(e.body, SignalRecorded)]


def test_organized_page_registers_sections_outline_check_and_cards(env):
    repo = env[0]
    pid = organized_learned_page(env)
    page = repo.load("u1").state.pages[pid].page
    assert page is not None
    sections = arts(repo, "section")
    written = [s.id for s in page.outline.sections if s.id in page.texts]
    assert written and set(sections) == {f"{pid}/{sid}" for sid in written}
    outline = arts(repo, "outline")[pid]
    for sid in written:
        a = sections[f"{pid}/{sid}"]
        assert a.artifact_id == section_artifact("u1", page, sid)
        parents = repo.artifacts.parents(a.artifact_id)
        assert (outline.artifact_id, "outline") in parents
        roles = {r for _, r in parents}
        assert "rule" in roles                       # from the organize commit's SectionLogs
        cells = next(s.cells for s in page.outline.sections if s.id == sid)
        assert ("k_cell" in roles) == bool(cells)
    assert f"{pid}/s4" in arts(repo, "check")
    cards = {card_id(pid, src.section_id, mode) for src in sources_from_page(page.outline, page.texts)
             for mode in ("cue", "check") if (src.cue if mode == "cue" else src.check)}
    assert set(arts(repo, "card")) == cards and card_id(pid, "s4", "check") in cards


def test_views_name_registered_versions(env):
    repo, _, ids = env
    pid = organized_learned_page(env)
    out = open_page_flow(repo, None, "u1", pid, now=NOW, new_id=ids)
    shown = [s for s in out.sections if s.body]
    assert shown and all(s.artifact_id in repo.artifacts.rows for s in shown)
    check = next(s.check for s in out.sections if s.check is not None)
    assert check.artifact_id in repo.artifacts.rows
    items = review_today(repo, "u1", now=LATER).items
    assert items and all(i.artifact_id in repo.artifacts.rows for i in items)


def test_an_unchanged_save_registers_nothing_new(env):
    repo = env[0]
    pid = organized_learned_page(env)
    before = dict(repo.artifacts.rows)
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id="wev-same", ts=LATER,
                                             body=PageSaved(record=rec, action="opened"))]))
    assert repo.artifacts.rows == before


def test_rewritten_section_points_at_its_previous_version(env):
    repo = env[0]
    pid = organized_learned_page(env)
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    assert rec.page is not None
    old_id = section_artifact("u1", rec.page, "s2")
    texts = dict(rec.page.texts)
    texts["s2"] = texts["s2"].model_copy(update={"body": texts["s2"].body + " 新的一句。"})
    new_rec = rec.model_copy(update={"page": rec.page.model_copy(update={"texts": texts})})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id="wev-rw", ts=LATER,
                                             body=PageSaved(record=new_rec, action="feedback"))]))
    new_id = section_artifact("u1", new_rec.page, "s2")
    assert new_id != old_id and new_id in repo.artifacts.rows
    assert (old_id, "previous") in repo.artifacts.parents(new_id)


def test_signal_targets_the_version_sent_not_the_current_one(env):
    repo, _, ids = env
    pid = organized_learned_page(env)
    item = review_today(repo, "u1", now=LATER).items[0]
    shown = item.artifact_id
    # the page changes after the card was shown: the grade still names what was shown
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    texts = dict(rec.page.texts)
    texts["s2"] = texts["s2"].model_copy(update={"cue": "换过的提示"})
    repo.commit(snap, Commit(wiki=[WikiEvent(
        event_id="wev-cue", ts=LATER, body=PageSaved(
            record=rec.model_copy(update={"page": rec.page.model_copy(update={"texts": texts})}),
            action="card"))]))
    assert review_today(repo, "u1", now=LATER).items[0].artifact_id != shown
    grade_review(repo, None, "u1", ReviewGradeIn(cell=item.cell, grade="fuzzy", page_id=pid,
                                                 section_id=item.section_id, artifact_id=shown),
                 now=LATER + timedelta(minutes=1), new_id=ids)
    sig = signals(repo)[-1]
    assert sig.kind == "review_grade" and sig.target_artifact_id == shown
    assert sig.evidence_type == "medium_weak"


def test_requests_refuse_a_malformed_artifact_id():
    with pytest.raises(ValidationError):
        CheckAnswerIn(choice=0, artifact_id="current")
    assert CheckAnswerIn(choice=0).artifact_id is None


def test_every_signal_kind_has_an_evidence_type_from_the_grading():
    assert EVIDENCE["page_feedback"] == "strong" and EVIDENCE["check_answer"] == "medium"
    assert EVIDENCE["assistant_rejected"] == "not_quality" and EVIDENCE["note_edit"] == "edit_diff"
    s = Signal(signal_id="x", kind="proposal_decision", at=NOW)
    assert s.evidence_type == "very_weak" and Signal.model_validate(s.model_dump()) == s


def test_split_nodes_descend_from_the_removed_node():
    a = NodeRec(id="n1", name="均值")
    b1, b2 = NodeRec(id="n2", name="样本均值"), NodeRec(id="n3", name="总体均值")
    before = WikiState(graph=NodeGraph(nodes=(a,)))
    split = GraphChange(change_id="ch1", at=NOW, kind="split", summary="拆开", why="看不懂",
                        node_ids=("n1",), added_nodes=(b1, b2), removed_nodes=(a,))
    ev = WikiEvent(event_id="w1", ts=NOW, body=GraphSaved(
        graph=NodeGraph(nodes=(b1, b2), changes=(split,)), action="split"))
    lin = commit_lineage("u1", before, [ev], run_id="run-split")
    sink = MemoryArtifacts()
    sink.put(lin)
    made = {a.ref: a for a in sink.of_kind("node") if a.produced_by_run == "run-split"}
    assert set(made) == {"n2", "n3"}
    old = next(x for x in sink.of_kind("node") if x.ref == "n1")
    assert old.produced_by_run is None
    assert sink.parents(made["n2"].artifact_id) == [(old.artifact_id, "split_from")]


def test_rule_revs_chain_to_the_previous_rev():
    v1 = TeachingSpec(version=1, items=(SpecItem(item_id="r1", text="先举例"),))
    item2 = v1.items[0].model_copy(update={"text": "先举一个生活里的例子", "rev": 2})
    v2 = TeachingSpec(version=2, items=(item2,))
    state = WikiState(spec=v1)
    lin = commit_lineage("u1", state, [WikiEvent(event_id="w2", ts=NOW, body=TeachingSpecSaved(
        spec=v2, action="edit"))], run_id=None)
    sink = MemoryArtifacts()
    sink.put(lin)
    new = artifact_id("u1", "rule", "r1", "2")
    assert sink.parents(new) == [(artifact_id("u1", "rule", "r1", str(v1.items[0].rev)),
                                  "previous")]


def test_session_store_registers_answers_and_chains_compactions():
    store = MemorySessionStore()
    store.create(SessionInfo(session_id="s1", user_id="u1", page_id=None, note_concept_id=None,
                             created_at=NOW))
    store.append("s1", "e1", "assistant", {"message_id": "m1", "text": "答"}, NOW)
    store.append("s1", "c1", "compaction", {"summary": "一"}, NOW)
    store.append("s1", "c2", "compaction", {"summary": "二"}, NOW)
    assert answer_artifact("u1", "m1") in store.artifacts.rows
    assert store.artifacts.parents(compaction_artifact("u1", "c2")) == [
        (compaction_artifact("u1", "c1"), "previous")]


def test_artifact_ids_are_per_user_and_well_formed():
    a, b = artifact_id("u1", "section", "p/s", "k"), artifact_id("u2", "section", "p/s", "k")
    assert a != b and is_artifact_id(a) and not is_artifact_id("art_xyz")


def test_a_save_inside_a_traced_step_records_its_run(env):
    from evowiki.llm import run_context
    from tests.llm.test_run_context import Store

    repo = env[0]
    pid = organized_learned_page(env)
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    texts = dict(rec.page.texts)
    texts["s2"] = texts["s2"].model_copy(update={"body": texts["s2"].body + " 再补一句。"})
    new_rec = rec.model_copy(update={"page": rec.page.model_copy(update={"texts": texts})})
    run_context.configure(Store())
    try:
        with run_context.traced_step("write", f"page:{pid}", "u1") as step:
            repo.commit(snap, Commit(wiki=[WikiEvent(event_id="wev-run", ts=LATER, body=PageSaved(
                record=new_rec, action="feedback"))]))
            run_id = run_context.current_run_id()
    finally:
        run_context.configure(None)
    assert run_id is not None and step is not None
    made = repo.artifacts.rows[section_artifact("u1", new_rec.page, "s2")]
    assert made.produced_by_run == run_id
