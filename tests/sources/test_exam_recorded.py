"""Synthetic replay of the recorded handwriting failure shapes, without live prompts or papers.
The three letter delimiters and an answer-page entry reproduce the original extraction hazards.
No model is called."""

import json
from itertools import count
from pathlib import Path

import pytest

from evowiki.sources.config import PAGE_MARK
from evowiki.sources.exam import ExamPlan, extract_items
from evowiki.sources.models import SectionRow
from tests.sources._fakes import FakeLLM

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/sources/exam-handwriting.json"
CASE = json.loads(FIXTURE.read_text(encoding="utf-8"))
PAPERS = (CASE["paper"],)


def _recorded() -> list[tuple[str, ExamPlan, dict]]:
    return [(CASE["paper"], ExamPlan.model_validate(CASE["plan"]), CASE)]


def _section(paper: str) -> SectionRow:
    """As the eval script built it (like the importer): one section per page, 〔p.1〕 + OCR."""
    text = PAGE_MARK.format(n=1) + "\n" + CASE["text"]
    return SectionRow(section_id=f"{paper}-p1", source_id=paper, parent_id=None, ord=0, depth=0,
                      title="", page_from=1, page_to=1, text=text, tokens=len(text) // 2,
                      summary=None, read_by="vision")


@pytest.mark.parametrize("paper, plan, recorded", _recorded(), ids=PAPERS)
def test_the_recorded_run_replays_with_no_key_and_the_recorded_statuses(paper, plan, recorded):
    ids = count(1)
    llm = FakeLLM(plan)
    out = extract_items([_section(paper)], llm=llm, new_id=lambda p: f"{p}{next(ids)}")
    assert len(llm.calls) == 1 and out.failed_batches == 0
    assert sum(1 for i in out.items if i.has_key) == 0       # no handwritten letter is a key
    assert all(i.answer is None for i in out.items)
    want = [tuple(row) for row in recorded["expected"]]
    assert [(i.number, i.status) for i in out.items] == want
    for i in out.items:
        if i.status == "unreadable":     # v1 wrote free text: stored under the code `other`
            assert i.problem is not None and i.problem.startswith("other: ")


def test_the_three_handwritten_letters_are_blocked():
    """Letters within question text cannot become keys, including a separate answer claim."""
    paper, plan, _ = _recorded()[0]
    offered = [r.number for r in plan.items if r.answer] + [a.number for a in plan.answers]
    assert offered                                           # the model did offer them
    ids = count(1)
    out = extract_items([_section(paper)], llm=FakeLLM(plan), new_id=lambda p: f"{p}{next(ids)}")
    by_number = {i.number: i for i in out.items}
    assert all(not by_number[n].has_key for n in offered)
    assert by_number["1"].problem == "answer_not_in_text"
    assert any("answer page entry rejected" in d for d in out.diagnostics)


def test_the_recorded_totals():
    totals = {"ok": 0, "unreadable": 0, "incomplete": 0}
    for paper, plan, _ in _recorded():
        ids = count(1)
        out = extract_items([_section(paper)], llm=FakeLLM(plan),
                            new_id=lambda p, ids=ids: f"{p}{next(ids)}")
        for i in out.items:
            totals[i.status] += 1
    assert totals == {"ok": 3, "unreadable": 1, "incomplete": 0}
