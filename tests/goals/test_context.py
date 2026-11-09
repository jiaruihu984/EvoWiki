import pytest

from evowiki.goals.context import (
    ContextOverBudget,
    PageDigest,
    Part,
    assemble,
    digest_line,
    estimate_tokens,
    page_summary,
    rank_pages,
    tail,
)


def test_estimate_tokens_counts_cjk_per_char_and_ascii_per_four():
    assert estimate_tokens("") == 0
    assert estimate_tokens("标准化") == 3
    assert estimate_tokens("abcd") == 1 and estimate_tokens("abcde") == 2
    assert estimate_tokens("z 值") == 2


def test_assemble_keeps_required_and_trims_low_priority_from_the_tail():
    parts = [Part(name="sel", header="选中", items=("这一段" * 10,), required=True),
             Part(name="k", header="K", items=("格子一" * 5, "格子二" * 5)),
             Part(name="pages", header="页面", items=tuple(f"页面{i}" * 10 for i in range(20)))]
    pack = assemble(parts, budget=120)
    assert pack.tokens <= 120
    assert pack.kept("sel") == 1 and pack.kept("k") == 2
    assert 0 < pack.kept("pages") < 20
    assert pack.text("pages").startswith("页面0")       # most relevant survives


def test_assemble_never_exceeds_budget_for_many_shapes():
    for budget in range(10, 400, 37):
        parts = [Part(name=f"p{i}", items=tuple("内容" * (j + 1) for j in range(8)))
                 for i in range(4)]
        assert assemble(parts, budget).tokens <= budget


def test_assemble_stops_a_part_at_the_first_item_that_does_not_fit():
    pack = assemble([Part(name="a", items=("短", "很长" * 50, "短"))], budget=10)
    assert pack.kept("a") == 1


def test_required_over_budget_fails_closed():
    with pytest.raises(ContextOverBudget):
        assemble([Part(name="sel", items=("字" * 100,), required=True)], budget=50)


def test_rank_pages_by_shared_concepts_then_prereqs_keeps_unrelated_last():
    pages = [PageDigest(page_id="p1", title="PCA", concepts=("pca",)),
             PageDigest(page_id="p2", title="均值", concepts=("mean",)),
             PageDigest(page_id="p3", title="z", concepts=("z_score", "std"))]
    ranked = rank_pages(pages, focus={"z_score", "std"}, prereqs={"mean"})
    assert [p.page_id for p in ranked] == ["p3", "p2", "p1"]
    assert [p.page_id for p in rank_pages(pages, {"z_score"}, exclude={"p3"})] == ["p1", "p2"]


def test_page_summary_joins_cues_and_caps():
    assert page_summary(["z = (x−μ)/σ", "", "例：身高"]) == "z = (x−μ)/σ；例：身高"
    assert page_summary(["很长" * 200], max_chars=10).endswith("…")


def test_tail_and_digest_line():
    assert tail("abcdef", 3) == "…def" and tail("ab", 3) == "ab"
    d = PageDigest(page_id="p1", title="PCA", concepts=("pca",), summary="降维")
    assert digest_line(d) == "- [PCA](#p:p1)：概念 pca；讲了：降维"


def test_goals_and_llm_share_one_token_estimator():
    from evowiki.goals import context
    from evowiki.llm import providers
    from evowiki.tokens import estimate_tokens

    assert context.estimate_tokens is estimate_tokens is providers.estimate_tokens
    assert estimate_tokens("abc") == 1       # rounded up overall, not floored
