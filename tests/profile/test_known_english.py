import pytest

from evowiki.profile.known import Matched, clean, match, observations, unmatched
from tests.profile.test_profile_single_source import _k_service


@pytest.mark.parametrize(("line", "cell"), [
    ("z 分数: I know the calculation", "c1.procedure"),
    ("z 分数: I can interpret the result", "c1.interpret"),
    ("标准差: I know the definition", "c2.term"),
])
def test_english_self_report_keeps_only_named_aspect_and_raw_quote(line, cell):
    repo, _ = _k_service()
    snap = repo.load("u1")
    result = match(snap.state, snap.k, line, lang="en")
    assert isinstance(result, Matched)
    assert result.cells == (cell,)
    assert observations(result)[0].quote == line


@pytest.mark.parametrize("line", [
    "z 分数: I don't know the calculation", "z 分数: I cannot calculate it",
    "z 分数: If I know the calculation", "z 分数: Do I know the calculation?",
    "z 分数：不会怎么算", "z 分数：我会这个",
])
def test_uncertainty_does_not_become_affirmative_evidence(line):
    repo, _ = _k_service()
    snap = repo.load("u1")
    assert isinstance(match(snap.state, snap.k, line, lang="en"), str)


def test_english_annotation_is_display_only_and_removed_before_matching():
    line = "z 分数: I know the calculation"
    annotated = unmatched(line, "Example reason", lang="en")
    assert annotated == line + " (Not recorded: Example reason)"
    assert clean(annotated) == line
