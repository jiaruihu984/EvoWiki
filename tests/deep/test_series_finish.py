"""`finish` carries the page's series update (plan B2): stored on the workspace, handed out on the
outcome, refused (draft not frozen) when a field breaks the limits. Scripted gateways only."""

from evowiki.deep.writer import PageWriter
from evowiki.goals.fold import Page, states_for
from evowiki.wikiapp.refine_apply import refine_input
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.series import SeriesUpdate
from tests.deep.fakes import DeepScript, FakeChecker, call, library, section, self_check, turn
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized

SERIES = {"example": "抛硬币", "terms": ["方差", "标准差"], "notation": ["μ 表示均值"],
          "summary": "从抛硬币引入方差。"}


def run(finishes: list[dict]):
    env = (MemoryRepo(["u1"]), Script(), Ids())
    pid = organized(env)
    page = env[0].load("u1").state.pages[pid].page
    fresh = Page(outline=page.outline, states=states_for(page.outline), texts={},
                 snapshot=page.snapshot, unresolved_conflicts=(), conflicts=())
    inp = refine_input(env[0].load("u1"), pid, NOW, WikiSpecSource(env[0]), page=fresh, mode="page")
    sections = [turn(section("s1", 1, role="organizer", depth="quick", heading="从你会的出发")),
                turn(section("s2", 1)),
                turn(section("s3", 1, role="recap", depth="quick", heading="标准差提醒"))]
    ds = DeepScript().plan().write(*sections, *[turn(f) for f in finishes])
    return PageWriter(llm=ds.llm(), checker=FakeChecker(), library=library()).write(inp), ds


def fin(i: int, **extra) -> dict:
    return call("finish", i, self_check=self_check(), **extra)


def test_series_in_finish_reaches_the_outcome():
    res, _ = run([fin(1, series=SERIES)])
    assert res.outcome.series_update == SeriesUpdate(
        example="抛硬币", terms=("方差", "标准差"), notation=("μ 表示均值",), summary="从抛硬币引入方差。")


def test_no_series_means_no_update():
    res, _ = run([fin(1)])
    assert res.outcome.series_update is None


def test_an_invalid_series_is_refused_and_the_loop_continues():
    bad = dict(SERIES, summary="长" * 301)
    res, ds = run([fin(1, series=bad), fin(2, series=SERIES)])
    refused = next(m.text for r in ds.requests if r.purpose == "deep.write" for m in r.messages
                   if m.role == "tool" and "series 不合法" in m.text)
    assert "summary" in refused
    assert res.outcome.series_update is not None and res.outcome.series_update.summary == SERIES["summary"]
