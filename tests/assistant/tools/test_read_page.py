import pytest

from evowiki.agent.read_page import ReadPageTool


def test_outline_of_current_page(store, ctx):
    r = ReadPageTool(store).run({}, ctx)
    assert r.error is None
    for s in ("s1", "s2", "s3", "为什么要标准化", "z-score 怎么算", "已折叠", "未写"):
        assert s in r.content
    assert "不同量纲放到一起比" in r.content           # recall cue shown for folded section
    assert r.sources[0].page_id == "p1" and r.sources[0].title == "学会 z-score"


def test_outline_shows_concept_and_cell_ids(store, ctx):
    r = ReadPageTool(store).run({}, ctx)
    lines = r.content.splitlines()
    assert "概念：zscore, stdev" in lines
    s1 = next(x for x in lines if x.startswith("- s1 "))
    s2 = next(x for x in lines if x.startswith("- s2 "))
    s3 = next(x for x in lines if x.startswith("- s3 "))
    assert "格子：zscore.purpose" in s1
    assert "格子：zscore.procedure, stdev.notation" in s2
    assert "格子：zscore.interpret" in s3


def test_empty_title_falls_back_to_page_id(ctx):
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    from tests.assistant.tools.conftest import make_page
    wp = make_page().model_copy(update={"title": ""})
    r = ReadPageTool(InMemoryPageStore({"u1": [wp]})).run({}, ctx)
    assert r.error is None and r.sources[0].title == "p1"
    r2 = ReadPageTool(InMemoryPageStore({"u1": [wp]})).run({"section_id": "s2"}, ctx)
    assert r2.error is None and r2.sources[0].title


def test_args_are_described():
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    props = ReadPageTool(InMemoryPageStore({})).spec.parameters["properties"]
    assert props["page_id"].get("description") and props["section_id"].get("description")


def test_section_full_text(store, ctx):
    r = ReadPageTool(store).run({"section_id": "s2"}, ctx)
    assert r.error is None and "z = (x - μ) / σ" in r.content
    assert r.sources[0].page_id == "p1"


def test_other_page_by_id(store, ctx):
    r = ReadPageTool(store).run({"page_id": "p2", "section_id": "s2"}, ctx)
    assert "方差是离均差平方的平均" in r.content


def test_errors(store, ctx):
    t = ReadPageTool(store)
    assert t.run({"page_id": "nope"}, ctx).error
    assert t.run({"section_id": "zz"}, ctx).error
    assert t.run({"section_id": "s3"}, ctx).error                    # not written yet
    assert t.run({"page_id": "p9"}, ctx).error                       # another user's page
    no_page = ctx.model_copy(update={"page_id": None})
    assert t.run({}, no_page).error                                  # nothing to read
    assert t.run({"section_id": 3}, ctx).error                       # bad args


def test_long_section_is_clipped(ctx):
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    from tests.assistant.tools.conftest import make_page
    big = InMemoryPageStore({"u1": [make_page(body2="长" * 20000)]})
    r = ReadPageTool(big).run({"section_id": "s2"}, ctx)
    assert r.error is None and len(r.content) <= 6000 and "截断" in r.content


def test_spec():
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    s = ReadPageTool(InMemoryPageStore({})).spec
    assert s.name == "read_page" and s.effect == "read" and len(s.description) <= 300


@pytest.mark.parametrize(("answers", "status"), [
    (None, "作答状态未知"), ({}, "未作答"), ({"s5": 1}, "已作答，答对"),
    ({"s5": 0}, "已作答，答错"), ({"s5": -1}, "已作答"),
])
def test_structured_check_without_prose_has_safe_state(page, ctx, answers, status):
    from evowiki.goals.contracts import CheckItem, OutlineSection, SectionRole, SectionText
    from evowiki.wikiapp.page_reader import READ_SCOPE, InMemoryPageStore

    check = OutlineSection(id="s5", heading="确认一下", role=SectionRole.CHECK,
                           check=CheckItem(cell="zscore.procedure", stem="Which expression gives a z-score?",
                                           options=("Add the mean", "Subtract the mean and divide by the standard deviation"),
                                           answer=1, verified=True))
    outline = page.page.outline.model_copy(update={"sections": (*page.page.outline.sections, check)})
    # Even an unexpected text on a check must not leak a cue's answer or bypass structured state.
    for texts in (page.page.texts, {**page.page.texts, "s5": SectionText(
            section_id="s5", cue="private cue", answer="private answer", body="private body", cache_key="k5")}):
        current = page.model_copy(update={"page": page.page.model_copy(update={"outline": outline, "texts": texts}),
                                          "check_answers": answers})
        tool = ReadPageTool(InMemoryPageStore({"u1": [current]}))
        for args in ({}, {"section_id": "s5"}):
            result = tool.run(args, ctx)
            assert result.error is None
            line = next(line for line in result.content.splitlines() if "s5 ·" in line)
            assert "确认题已生成" in line and status in line and "未写" not in line
            assert READ_SCOPE in result.content
            assert "private" not in result.content
            assert "answer=" not in result.content and "correct_index" not in result.content
            assert "正确选项" not in result.content and "正确答案" not in result.content
            for text in (check.check.stem, *check.check.options):
                assert (text in result.content) == ("section_id" in args)


def test_an_unverified_check_is_not_claimed_to_be_generated(page, ctx):
    from evowiki.goals.contracts import CheckItem, OutlineSection, SectionRole
    from evowiki.wikiapp.page_reader import InMemoryPageStore

    section = OutlineSection(id="s5", heading="确认一下", role=SectionRole.CHECK,
                             check=CheckItem(cell="zscore.procedure", stem="question",
                                             options=("A", "B"), answer=0))
    outline = page.page.outline.model_copy(update={"sections": (section,)})
    current = page.model_copy(update={"page": page.page.model_copy(update={"outline": outline}),
                                      "check_answers": {"s5": 0}})
    result = ReadPageTool(InMemoryPageStore({"u1": [current]})).run({"section_id": "s5"}, ctx)
    assert result.error is None and "确认题未就绪" in result.content
    assert "已生成" not in result.content and "答对" not in result.content
