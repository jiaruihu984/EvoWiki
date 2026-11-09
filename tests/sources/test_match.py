"""Matching a supplement material to the user's pages (`sources.match`): vector candidates, the
page text, the one same-concept judgement. Pure; a scripted JsonLLM, no model is ever called."""

from datetime import UTC, datetime

import pytest

from evowiki.goals.contracts import OutlineSection, PageOutline, SectionRole, SectionText
from evowiki.goals.fold import Page
from evowiki.llm.gateway import LLMParseError
from evowiki.sources.config import DATA_BEGIN, DATA_END, DATA_NOTE
from evowiki.sources.match import (
    MAX_PAGES,
    Candidate,
    MatchFailed,
    MatchPlan,
    MatchVerdict,
    candidates,
    judge,
    page_text,
)
from evowiki.wikiapp.events import PageRecord
from tests.sources._fakes import FakeLLM

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def record(pid="p1", title="均值", bodies=("一" * 400, "短的。")) -> PageRecord:
    secs = tuple(OutlineSection(id=f"s{i}", heading=f"标题{i}", role=SectionRole.EXPLAIN)
                 for i in range(1, len(bodies) + 1))
    outline = PageOutline(page_id=pid, goal="g", concepts=("c1",), targets=("c1",), sections=secs)
    page = Page(outline=outline, states={}, texts={
        f"s{i}": SectionText(section_id=f"s{i}", body=b, cache_key=str(i))
        for i, b in enumerate(bodies, start=1)})
    return PageRecord(page_id=pid, title=title, goal="g", concepts=("c1",),
                      targets=("c1",), page=page, created_at=NOW)


def test_candidates_rank_pages_by_their_best_section_and_keep_k():
    pages = {"pa": [1.0, 0.0, 0.0], "pb": [0.0, 1.0, 0.0], "pc": [0.6, 0.8, 0.0],
             "pd": [0.0, 0.0, 1.0]}
    secs = {"x1": [1.0, 0.0, 0.0], "x2": [0.0, 1.0, 0.0], "x3": [0.8, 0.6, 0.0]}
    out = candidates(pages, secs, k=3)
    assert [p for p, _s, _x in out] == ["pa", "pb", "pc"]       # pd (0.0) is cut by k
    assert out[0][1] == pytest.approx(1.0)
    assert out[0][2] == ("x1", "x3", "x2")                       # its most similar sections first
    assert out[2][1] == pytest.approx(0.96)                      # pc's best is x3
    assert len(candidates(pages, secs, k=1)) == 1
    assert candidates({}, secs) == [] and candidates(pages, {}) == []


def test_candidates_numpy_matches_pure_python_cosine():
    import math
    import random
    rnd = random.Random(7)

    def cos(u, v):
        nu, nv = math.sqrt(sum(x * x for x in u)), math.sqrt(sum(x * x for x in v))
        return sum(x * y for x, y in zip(u, v, strict=True)) / (nu * nv) if nu and nv else 0.0

    pages = {f"p{i}": [rnd.uniform(-1, 1) for _ in range(8)] for i in range(12)}
    pages["pz"] = [0.0] * 8                                      # zero vector -> cosine 0
    secs = {f"s{i}": [rnd.uniform(-1, 1) for _ in range(8)] for i in range(9)}
    expected = []
    for pid, pv in pages.items():
        sims = sorted(((cos(pv, sv), sid) for sid, sv in secs.items()), key=lambda t: (-t[0], t[1]))
        expected.append((pid, sims[0][0], tuple(s for _x, s in sims[:3])))
    expected.sort(key=lambda t: (-t[1], t[0]))
    got = candidates(pages, secs, k=100)
    assert [(p, s) for p, _x, s in got] == [(p, s) for p, _x, s in expected]
    assert [x for _p, x, _s in got] == pytest.approx([x for _p, x, _s in expected], abs=1e-12)


def test_candidates_keep_at_most_three_sections_per_page():
    secs = {f"x{i}": [1.0, i / 10] for i in range(6)}
    (only,) = candidates({"pa": [1.0, 0.0]}, secs)
    assert only[2] == ("x0", "x1", "x2")


def test_page_text_is_the_title_the_headings_and_each_section_start():
    text = page_text(record())
    assert text.startswith("均值\n")
    assert "标题1" in text and "标题2" in text and "短的。" in text
    assert "一" * 300 in text and "一" * 301 not in text
    bare = PageRecord(page_id="p9", title="空页", goal="g", concepts=(),
                      targets=(), created_at=NOW)
    assert page_text(bare) == "空页"


def cand(i, score=0.9, n=1):
    secs = tuple((f"x{i}{k}" if n > 1 else f"x{i}", f"节{i}{k}" if n > 1 else f"节{i}",
                  f"节{i}{k}讲的内容" if n > 1 else f"节{i}讲的内容") for k in range(1, n + 1))
    return Candidate(page_id=f"p{i}", title=f"页{i}", text=f"页{i}的开头", score=score,
                     sections=secs)


def test_judge_keeps_only_the_pages_with_a_same_concept_section():
    llm = FakeLLM(MatchPlan(verdicts=[MatchVerdict(n=1, sections=[1], why="补了例子"),
                                      MatchVerdict(n=2, sections=[], why=""),
                                      MatchVerdict(n=3, sections=[1], why="说法不同")]))
    got = judge(llm, "讲义", [cand(1), cand(2), cand(3)])
    assert [(m.page_id, m.title, m.section_ids, m.why) for m in got] == [
        ("p1", "页1", ("x1",), "补了例子"), ("p3", "页3", ("x3",), "说法不同")]
    ((_role, purpose, prompt),) = llm.calls
    assert purpose == "sources.match"
    # the material's words sit inside the data markers, after the note
    assert DATA_NOTE in prompt
    at = prompt.index("节1讲的内容")
    begin = prompt.rindex(DATA_BEGIN, 0, at)
    assert prompt.rfind(DATA_END, 0, at) < begin < at < prompt.index(DATA_END, at)
    assert prompt.rindex(DATA_BEGIN, 0, prompt.index("讲义")) < prompt.index("讲义")


def test_judge_links_only_the_sections_named_and_drops_made_up_numbers():
    """sources-match@v2: the page gets the sections the model named among its own (in their
    candidate order), not all of them; a number outside them is dropped, a page with none left
    is not matched."""
    llm = FakeLLM(MatchPlan(verdicts=[MatchVerdict(n=1, sections=[3, 1, 3, 7], why="补了证明"),
                                      MatchVerdict(n=2, sections=[0, 4], why="编号都不对")]))
    got = judge(llm, "讲义", [cand(1, n=3), cand(2, n=3)])
    assert [(m.page_id, m.section_ids, m.why) for m in got] == [("p1", ("x11", "x13"), "补了证明")]
    prompt = llm.calls[0][2]
    assert "节 1【节11】" in prompt and "节 3【节13】" in prompt
    assert "sections 只能写这一页下面给出的节编号" in prompt
    # the factual rule is outside the editable span; the span says what counts as related
    span = prompt[prompt.index("⟦g:source_match⟧"):prompt.index("⟦/g⟧")]
    assert "同一个概念" in span and "节编号" not in span


def test_recorded_v1_reply_has_no_sections_so_nothing_is_linked():
    """The live sources-match@v1 reply (2026-10-21, llm_160f…) said same=true for whole pages; read
    as v2 it names no section, so no page gets material sections it was never matched to."""
    import json
    from pathlib import Path
    rec = json.loads((Path(__file__).parents[1] / "fixtures" / "sources" / "recorded"
                      / "match_v1_eigen.json").read_text(encoding="utf-8"))
    plan = MatchPlan.model_validate(rec["reply"])
    assert judge(FakeLLM(plan), "讲义", [cand(i, n=3) for i in range(1, 7)]) == []


def test_judge_keeps_at_most_three_in_candidate_order():
    llm = FakeLLM(MatchPlan(verdicts=[MatchVerdict(n=i, sections=[1]) for i in (5, 4, 3, 2, 1)]))
    got = judge(llm, "讲义", [cand(i, 1 - i / 10) for i in range(1, 6)])
    assert [m.page_id for m in got] == ["p1", "p2", "p3"]
    assert len(got) == MAX_PAGES


def test_judge_ignores_unknown_and_repeated_numbers():
    llm = FakeLLM(MatchPlan(verdicts=[MatchVerdict(n=9, sections=[1]),
                                      MatchVerdict(n=1, sections=[1]),
                                      MatchVerdict(n=1, sections=[])]))
    assert [m.page_id for m in judge(llm, "讲义", [cand(1)])] == ["p1"]


def test_judge_bad_json_is_match_failed():
    with pytest.raises(MatchFailed):
        judge(FakeLLM(LLMParseError("not json")), "讲义", [cand(1)])


def test_judge_without_candidates_makes_no_call():
    llm = FakeLLM()
    assert judge(llm, "讲义", []) == [] and llm.calls == []
