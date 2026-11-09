from evowiki.harness.gate import check, decide
from evowiki.harness.judge import Comparison


def cmp(winner: str, i: int = 0) -> Comparison:
    return Comparison(
        cmp_id=f"c{i}", case_id=f"k{i}", a_id="a", b_id="b", winner=winner,
        reason="", quote="", flipped=False, call_ids=("x", "y"),
    )


def many(*winners: str) -> list[Comparison]:
    return [cmp(w, i) for i, w in enumerate(winners)]


def run(problem, anchors=(), complexity=1.0, cited=True):
    return decide(many(*problem), many(*anchors), complexity=complexity, cited=cited)


def test_two_wins_three_cases_passes():
    d = run(["a", "a", "tie"])
    assert d.verdict == "pass"
    assert (d.wins, d.losses, d.ties) == (2, 0, 1)
    assert d.utility_delta == 2 - 0.05


def test_three_wins_one_loss_fails_on_the_clear_loss():
    d = run(["a", "a", "a", "b"])
    assert d.verdict == "fail"
    assert d.reasons


def test_two_wins_one_loss_fails():
    assert run(["a", "a", "b"]).verdict == "fail"


def test_one_win_is_thin():
    assert run(["a", "tie", "tie"]).verdict == "thin"


def test_too_few_cases_is_thin():
    d = run(["a", "a"])
    assert d.verdict == "thin"
    assert any("不足" in r for r in d.reasons)


def test_anchor_loss_fails_even_with_wins():
    d = run(["a", "a", "a"], anchors=["tie", "b"])
    assert d.verdict == "fail"
    assert d.anchor_losses == 1


def test_anchor_loss_beats_thin():
    assert run(["a"], anchors=["b"]).verdict == "fail"


def test_not_cited_fails():
    assert run(["a", "a", "a"], cited=False).verdict == "fail"


def test_non_positive_utility_fails():
    d = run(["a", "a", "tie"], complexity=40.0)
    assert d.utility_delta <= 0
    assert d.verdict == "fail"


def test_reasons_list_every_rule_that_fired():
    d = run(["a", "b", "tie"], anchors=["b"], cited=False)
    assert d.verdict == "fail"
    assert len(d.reasons) >= 4


# ---- check: the harness round's smoke check ----------------------------------------------------

def smoke(written, broken=(), cited=True):
    return check(written, broken=broken, complexity=1.0, cited=cited)


def test_check_passes_when_the_candidate_wrote_its_cases():
    d = smoke(2)
    assert d.verdict == "pass" and d.reasons == ("新设置写出了 2 段",)


def test_check_fails_on_a_case_the_candidate_cannot_write_or_no_citation():
    d = smoke(1, broken=[("k1", "重放没写出这一段：x")])
    assert d.verdict == "fail" and "写不出 1 段" in d.reasons[0]
    assert smoke(2, cited=False).verdict == "fail"


def test_check_is_thin_with_nothing_written():
    assert smoke(0).verdict == "thin"
