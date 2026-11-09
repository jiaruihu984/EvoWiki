"""z-score → PCA end to end over the flow functions, scripted synthetic LLM (non-reportable).
Assertions use structured outputs only (ChangeOut, PageOut / DiffNoteOut, CatalogOut, KGridOut),
never Chinese sentences."""

import pytest

from evowiki.goals.contracts import Band
from evowiki.wikiapp.repo import MemoryRepo
from tests.runtime.helpers import T0, FixedClock
from tests.wikiapp.e2e_driver import (
    MEAN_RECAP,
    USER,
    VARIANCE_RECAP,
    Ids,
    load_script,
    run_scenario,
)
from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
from tests.wikiapp.fake_writer import WRITE, writing
from tests.wikiapp.goal_script import PROBE


@pytest.fixture(scope="module")
def run():
    script = load_script()
    repo = MemoryRepo([USER])
    out = run_scenario(repo, script.gateway(), FixedClock(T0), Ids(), writing(repo, script))
    return out, script, repo


def section(page, sid):
    return next(s for s in page.sections if s.id == sid)


def test_whole_script_is_used_once(run):
    _out, s, _repo = run
    assert s.pending() == {}
    assert {p: s.calls(p) for p in ("goals.enter", PROBE, "goals.path", ORGANIZE, WRITE)} == {
        "goals.enter": 2, PROBE: 0, "goals.path": 2, ORGANIZE: 2, WRITE: 7}   # one outline per page


def test_zscore_page_is_built_and_opened_for_free(run):
    out, _s, _repo = run
    assert out["z_draft"].page_id is None
    # the KST diagnosis asks one quiz per round. The description claims 均值 (「我会算平均数」), and a
    # claim is confirmed by an answer first (user ruling: 对关键知识点用实际答题验证); then 方差
    # (closest to 1/2), then z 分数; all right → every node known from answers and it stops
    assert out["z_draft_out"].questions == []           # asked in the chat (ask_check)
    assert out["z_draft"].draft.rounds[0].pick.cell == "c2.procedure"
    assert list(out["z_answers"]) == ["c2.procedure", "c3.notation", "c1.procedure"]
    final = out["z_final"]
    assert final.done and [(d.node, d.decision, d.source) for d in final.decisions] == [
        ("c1", "known", "quiz"), ("c2", "known", "quiz"), ("c3", "known", "quiz")]
    done = out["z_organize"][-1]
    assert done[0] == "done" and done[1].page_id == out["z_page"]
    opened = out["z_open"]
    assert opened.organized and opened.llm_calls == 0 and opened.diff is None
    assert section(opened, VARIANCE_RECAP).cells[0].band is Band.KNOWN


def test_know_on_mean_recap_folds_with_zero_calls(run):
    out, _s, _repo = run
    know = out["know"]
    assert know.section.id == MEAN_RECAP and know.section.folded and know.section.fold_locked
    assert know.change.llm_calls == 0 and know.change.folded[0] == MEAN_RECAP
    assert know.change.rewritten == [] and know.change.inserted == []


def test_pca_diagnostic_does_not_reask_measured_cells_and_reuses_ids(run):
    out, _s, _repo = run
    measured = out["measured_before_pca"]
    assert {"c1.procedure", "c3.notation"} <= measured       # answered right in the z quiz
    # z 分数 and 方差 already have evidence from the z goal: never asked again (K prior); the
    # description (「我知道 PCA 用来降维」) is weak and cannot end the diagnosis: one quiz on PCA
    r = out["pca_draft"].draft.rounds[0]
    # the cell whose answer tells the most (the description already spoke of what PCA is for)
    assert (r.pick.node, r.pick.cell) == ("c4", "c4.purpose")
    assert r.dropped == () and list(out["pca_answers"]) == ["c4.purpose"]
    assert [(d.node, d.source) for d in r.decisions] == [
        ("c4", "described"), ("c1", "k"), ("c3", "k")]
    concepts = [(c.id, c.name, c.reused) for c in out["pca_draft_out"].concepts]
    assert concepts == [("c4", "主成分分析", False), ("c1", "z 分数", True), ("c3", "方差", True)]
    assert out["pca_draft_out"].notes.reused_concepts == 2
    grid = out["k_grid"]
    assert [c.id for c in grid.concepts] == ["c1", "c2", "c3", "c4"]
    c4 = next(c for c in grid.concepts if c.id == "c4")
    assert c4.prereqs == ["c1", "c3"] and c4.role == "learn"


def test_pca_learning_leaves_the_zscore_page_as_it_is(run):
    """方差 was already known from the z quiz, so learning PCA moves no band of the z page."""
    out, _s, _repo = run
    zp = out["z_page"]
    cards = {c.page_id: c for c in out["catalog_after_pca"].learning}
    assert cards[zp].stale is False and cards[out["pca_page"]].stale is False
    page = out["z_reopen"]
    assert page.llm_calls == 0
    assert page.unresolved_conflicts == []
    after = {c.page_id: c for c in out["catalog_final"].learning}
    assert after[zp].stale is False
