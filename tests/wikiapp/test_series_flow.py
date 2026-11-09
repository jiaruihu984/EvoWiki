"""End to end (plan B2 Task 5): `write_page` commits the page's series update with the page, and the
next page's writer prompt carries it. Real PageWriter over scripted gateways; no paid calls."""

from datetime import UTC, datetime

import pytest

from evowiki.bot.settings import BotSettings
from evowiki.deep.store import MemoryAssetStore
from evowiki.deep.writer import PageWriter
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import Writing, organize_stream
from evowiki.wikiapp.page_flows import PageDeps
from evowiki.wikiapp.page_run_store import MemoryPageRunStore
from evowiki.wikiapp.page_runs import InlinePageRuns
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.write_queue import WriteQueue
from tests.deep.fakes import DeepScript, FakeChecker, call, library, section, self_check, turn
from tests.wikiapp.runs import runs_for
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_k1_replay import PAGE1, PAGE2
from tests.wikiapp.test_paths import big_goal

EXAMPLE = "20 人的班级里抽 5 个人量身高"


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def finish(i: int, **series) -> dict:
    return call("finish", i, self_check=self_check(), series=series)


def write_page_through(env, ds: DeepScript, pid: str, outline: dict) -> list:
    repo, s, ids = env
    kit = Writing(writer=PageWriter(llm=ds.llm(), checker=FakeChecker(), library=library()),
                  specs=WikiSpecSource(repo), assets=MemoryAssetStore())
    return list(organize_stream(repo, CountingGateway(s.gateway()), "u1", pid, now=NOW,
                                new_id=ids, writing=kit, runs=runs_for(repo)))


def sections(outline: dict, i: int) -> list[dict]:
    return [turn(section(sec["id"], i, heading=sec["heading"], role="explain"))
            for sec in outline["sections"]]


def test_the_second_page_writer_reads_what_the_first_page_set_up(env):
    repo, _, ids = env
    res, _ = big_goal(env)
    p1 = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids).page_id

    ds1 = DeepScript().plan(outline=PAGE1).write(*sections(PAGE1, 1), turn(finish(
        1, example=EXAMPLE, terms=["投影"], summary="用班级抽样引入总体与样本。")))
    items = write_page_through(env, ds1, p1, PAGE1)
    assert items[-1][0] == "done"

    state = repo.load("u1").state
    note = state.series[res.path_id]
    assert note.example == EXAMPLE
    assert [t.term for t in note.terms] == ["投影"]
    assert [p.page_id for p in note.pages] == [p1]
    kinds = [type(e.body).__name__ for e in repo.wiki_log("u1")]      # the note follows its page
    assert kinds.index("SeriesUpdated") > kinds.index("PageSaved")

    p2 = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids).page_id
    title1 = state.pages[p1].title
    ds2 = DeepScript().plan(outline=PAGE2).write(*sections(PAGE2, 2), turn(finish(
        2, example="换一个例子", terms=["置信水平"], summary="算置信区间。")))
    items = write_page_through(env, ds2, p2, PAGE2)
    assert items[-1][0] == "done"

    prompt = "\n".join(m.text for r in ds2.requests if r.purpose == "deep.write"
                       for m in r.messages)
    assert EXAMPLE in prompt and f"{title1}：投影" in prompt and f"(#p:{p1})" in prompt

    note = repo.load("u1").state.series[res.path_id]
    assert note.example == EXAMPLE                       # the first page's example is kept
    assert {t.term for t in note.terms} == {"投影", "置信水平"}
    assert [p.page_id for p in note.pages] == [p1, p2]


# ---- write-all: the next page is written by the idle Bot only after this page is committed ----

def _open_page1_then_idle_page2(env):
    """Opens page 1 (the user's own run), then lets the idle Bot write page 2, each on page runs
    whose work runs only when the test says so. Returns (path id, page 1, page 1's title, page 2's
    run row, page 2's deep script, whether page 1 and its note were committed when the Bot began)."""
    repo, s, ids = env
    res, _ = big_goal(env)
    p1 = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids).page_id
    ds1 = DeepScript().plan(outline=PAGE1).write(*sections(PAGE1, 1), turn(finish(
        1, example=EXAMPLE, terms=["投影"], summary="用班级抽样引入总体与样本。")))
    ds2 = DeepScript().plan(outline=PAGE2).write(*sections(PAGE2, 2), turn(finish(
        2, example="换一个例子", terms=["置信水平"], summary="算置信区间。")))
    gw = CountingGateway(s.gateway())
    jobs: list = []
    runs = InlinePageRuns(spawn=jobs.append, store=MemoryPageRunStore())

    def kit(ds: DeepScript) -> Writing:
        return Writing(writer=PageWriter(llm=ds.llm(), checker=FakeChecker(), library=library()),
                       specs=WikiSpecSource(repo), assets=MemoryAssetStore())

    def deps(ds: DeepScript) -> PageDeps:
        return PageDeps(repo=repo, gateway=lambda _u: gw, writing=kit(ds), store=runs.store,
                        clock=lambda: NOW, new_id=ids)
    queue = WriteQueue(runs, lambda: deps(ds2), repo, lambda: NOW, ids, _Env(), spawn=lambda w: w())
    stream = organize_stream(repo, gw, "u1", p1, now=NOW, new_id=ids, writing=kit(ds1), runs=runs)
    assert len(jobs) == 1 and runs.store.live_of("u1")[0].page_id == p1
    jobs.pop()()                                # page 1 is written and committed
    items = list(stream)
    assert items[-1][0] == "done"
    state = repo.load("u1").state
    seen = state.pages[p1].page is not None and res.path_id in state.series
    run_id = queue.kick("u1")                   # the user has nothing running: the Bot writes on
    jobs.pop()()                                # page 2 is written
    assert jobs == []
    return (res.path_id, p1, state.pages[p1].title, runs.store.get(run_id), ds2, seen, runs)


class _Env:
    """Bot side of the queue: everything on, nothing paused, no caps."""

    def settings(self, user_id):
        return BotSettings(kinds={"idle_write": True})

    def parked(self, user_id):
        return ()

    def cap_hit(self, user_id, now):
        return None

    def last_seen(self, user_id):
        return datetime.max.replace(tzinfo=UTC)

    def turn_busy(self, user_id):
        return False

    def note_skip(self, *a):
        raise AssertionError("nothing is skipped here")


def test_the_page_written_ahead_is_written_after_this_page_committed(env):
    _, _, title1, run, ds2, seen, _ = _open_page1_then_idle_page2(env)
    assert seen is True                         # page 1 and its note were committed first
    assert run.origin == "idle" and run.status == "done"
    prompt = "\n".join(m.text for r in ds2.requests if r.purpose == "deep.write"
                       for m in r.messages)
    assert EXAMPLE in prompt and "投影" in prompt and title1 in prompt


def test_next_hands_the_written_page_back_and_both_pages_are_in_the_note(env):
    repo, _, ids = env
    path_id, p1, _, run, _, _, runs = _open_page1_then_idle_page2(env)
    nxt = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids, runs=runs.store)
    assert nxt.prefetched and nxt.page_id == run.page_id
    note = repo.load("u1").state.series[path_id]
    assert [p.page_id for p in note.pages] == [p1, run.page_id]
    assert note.example == EXAMPLE                       # page 1's example is kept
    assert {t.term for t in note.terms} == {"投影", "置信水平"}


def test_text_commits_before_any_interactive_and_a_broken_block_is_dropped(env):
    """Stage 4 T3 + T4 through the real writer: every text section is on the page (committed,
    still writing) before the interactive is written; the block fails its render check when it is
    placed, the result says so, and at finish it is dropped without another round: the page is
    committed with its text."""
    from tests.deep.test_evoui_parity import BASE
    repo, s, ids = env
    res, _ = big_goal(env)
    pid = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids).page_id
    seen = []

    class Watch(FakeChecker):                      # what the page holds when the block is checked
        def check_block(self, block_id, block, *args, **kw):
            rec = repo.load("u1").state.pages[pid]
            seen.append((sorted(rec.page.texts), rec.has_body,
                         "block" in rec.page.texts["s2"].body))
            return super().check_block(block_id, block, *args, **kw)

    texts = [turn(section(sec["id"], 1, heading=sec["heading"], role="explain",
                          **({"body": "s2 正文。\n\n{{block:proj}}\n"} if sec["id"] == "s2" else {})))
             for sec in PAGE1["sections"]]
    ds = DeepScript().plan(outline=PAGE1).write(
        *texts, turn(call("write_block", 2, block_id="proj", section_id="s2", why="动手看",
                          block=BASE)),
        turn(finish(1, summary="总体、样本与零假设。")))
    kit = Writing(writer=PageWriter(llm=ds.llm(), checker=Watch(fail_block=lambda b, x: "拖了没反应"),
                                    library=library()),
                  specs=WikiSpecSource(repo), assets=MemoryAssetStore())
    items = list(organize_stream(repo, CountingGateway(s.gateway()), "u1", pid, now=NOW,
                                 new_id=ids, writing=kit, runs=runs_for(repo)))
    assert items[-1][0] == "done" and ds.pending() == {}
    assert seen[0] == (["s1", "s2", "s3"], False, False)      # all text first, still writing
    placed = next(m for r in ds.requests for m in r.messages
                  if m.role == "tool" and "已写入块" in m.text)
    assert placed.is_error and "拖了没反应" in placed.text
    rec = repo.load("u1").state.pages[pid]
    assert rec.has_body and rec.written.assets == () and "block" not in rec.page.texts["s2"].body
    assert rec.page.texts["s2"].body.startswith("s2 正文")
