"""A chat attachment's PDF with a text layer is usable without the layout parser; a learning need
parses it and rebuilds the outline; a page is read precisely on demand. Real Postgres, fake parser
and readers."""

import pytest

from evowiki.agent import durable
from evowiki.agent.source_tools import ReadSourceTool, SearchSourceTool
from evowiki.agent.tools import ToolContext
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import store
from evowiki.sources.importer import ImportJob, execute
from evowiki.sources.models import LinkRow
from evowiki.sources.precise import read_precise
from evowiki.sources.reader import PooledSourceReader
from evowiki.sources.uses import MaterialsNotReady, await_layout
from evowiki.sources.vision import PageImageRead
from tests.sources.test_importer import NOW, CountingLLM, add_source, src
from tests.sources.test_importer_layout import FakeLayout, resnet
from tests.sources.test_importer_vision import Reader, env, go, make_pdf, sections  # noqa: F401


def attached(pool, deps, sid="s1", kinds="tth", **kw):
    add_source(pool, sid, make_pdf(kinds))
    return go(deps(**kw), sid, mode="ask")


def next_gen(pool, sid="s1"):
    with pool.connection() as conn:
        return store.bump_run(conn, "u1", sid)


@pytest.mark.parametrize("change", ["rerun", "delete"])
def test_precise_read_from_old_generation_is_not_saved_or_returned(env, change):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="tth")

    def changed(page):
        with pool.connection() as conn:
            if change == "rerun":
                store.bump_run(conn, "u1", "s1")
            else:
                store.soft_delete(conn, "u1", "s1", NOW)
        return PageImageRead("old generation", False)

    eyes = Reader("claude", answer=changed)
    assert read_precise(pool, eyes, "u1", "s1", [3], now=lambda: NOW) == {3: None}
    assert eyes.calls == [3]
    with pool.connection() as conn:
        assert 3 not in store.page_reads(conn, "u1", "s1")


def test_a_pdf_with_text_takes_no_parse_no_page_image_and_no_model(env):  # noqa: F811
    pool, deps = env
    layout, local, llm = FakeLayout(resnet()), Reader("local"), CountingLLM()
    items = attached(pool, deps, layout=layout, local=local, llm_for=lambda _u: llm)
    assert items[-1][0] == "done" and items[-1][1].status == "ready"
    assert layout.calls == [] and local.calls == [] and llm.calls == []
    row = src(pool, "s1")
    assert row.text_first and row.imprecise_pages == (3,)


def test_a_pasted_formula_leaves_clean_text_and_its_page_still_counts_as_imprecise(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="tf")
    assert src(pool, "s1").imprecise_pages == (2,)


def test_a_scan_still_waits_for_the_parse(env):  # noqa: F811
    pool, deps = env
    layout = FakeLayout(resnet())
    attached(pool, deps, kinds="bbb", layout=layout)
    assert layout.calls == [3] and not src(pool, "s1").text_first


def test_a_short_scan_with_pictures_is_read_page_by_page_and_a_goal_still_parses_it(env):  # noqa: F811
    pool, deps = env
    layout = FakeLayout(resnet())
    attached(pool, deps, kinds="ff", layout=layout)
    row = src(pool, "s1")
    assert layout.calls == [] and row.text_first and row.image_pages == (1, 2) and row.status == "ready"


def test_a_few_empty_pages_are_no_scan_and_wait_for_the_turn_that_asks(env):  # noqa: F811
    pool, deps = env
    layout, local = FakeLayout(resnet()), Reader("local")
    attached(pool, deps, kinds="ttb", layout=layout, local=local)
    assert layout.calls == [] and local.calls == []
    assert src(pool, "s1").unreadable_pages == (3,)


def test_a_learning_run_parses_and_builds_the_outline_again(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="ttt")
    before = {s.section_id for s in sections(pool, "s1")}
    layout = FakeLayout(resnet())
    assert go(deps(layout=layout), "s1", mode="full", keep_sections=True,
              gen=next_gen(pool))[-1][0] == "done"
    assert layout.calls == [3]
    secs = sections(pool, "s1")
    assert not before & {s.section_id for s in secs}
    assert any(s.title.startswith("1. Introduction") for s in secs)
    row = src(pool, "s1")
    assert not row.text_first and row.imprecise_pages == ()


def test_layout_now_keeps_the_ask_mode_and_a_second_run_parses_no_more(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="ttt")
    layout = FakeLayout(resnet())
    go(deps(layout=layout), "s1", mode="ask", keep_sections=True, layout_now=True,
       gen=next_gen(pool))
    ids = [s.section_id for s in sections(pool, "s1")]
    assert layout.calls == [3] and not src(pool, "s1").text_first
    assert src(pool, "s1").progress["mode"] == "ask"
    go(deps(layout=layout), "s1", mode="full", keep_sections=True, gen=next_gen(pool))
    assert layout.calls == [3] and [s.section_id for s in sections(pool, "s1")] == ids


def _edited(pool):
    with pool.connection() as conn:
        sec = store.sections(conn, "u1", "s1")[0]
        store.edit_sections(conn, "u1", "s1", [(sec.section_id, "我改的标题", None)])


def _linked(pool):
    with pool.connection() as conn:
        sec = store.sections(conn, "u1", "s1")[0]
        store.replace_links(conn, "u1", node_ids=["n1"], links=[LinkRow(
            node_id="n1", section_id=sec.section_id, source_id="s1", role="teaches", by="user")],
            by="user", now=NOW)


@pytest.mark.parametrize("hold", [_edited, _linked])
def test_an_edited_or_linked_outline_stays_and_takes_the_parsed_formula_pages(env, hold):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="tth")
    hold(pool)
    before = sections(pool, "s1")
    assert go(deps(layout=FakeLayout(resnet())), "s1", mode="full", keep_sections=True,
              gen=next_gen(pool))[-1][0] == "done"
    after = sections(pool, "s1")
    assert [(s.section_id, s.title) for s in after] == [(s.section_id, s.title) for s in before]
    assert not src(pool, "s1").text_first
    parsed = resnet().page_texts()[3][0]
    assert parsed.strip()[:40] in "\n".join(s.text for s in after)


# --- the wait before a goal is planned ------------------------------------------------------------

class Runs:
    """Runs a job inline on `start`."""

    def __init__(self, deps):
        self.deps, self.jobs = deps, []

    def start(self, job: ImportJob) -> str:
        self.jobs.append(job)
        with durable.virtual_workflow(f"t:{job.source_id}:{job.gen}"):
            execute(job, lambda *_a: None, self.deps)
        return "w"


def test_await_layout_parses_once_and_says_what_it_is_doing(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="ttt")
    layout = FakeLayout(resnet())
    runs = Runs(deps(layout=layout))
    await_layout(pool, runs, "u1", ["s1"], parse=True)
    assert [(j.mode, j.layout_now) for j in runs.jobs] == [("ask", True)]
    assert layout.calls == [3] and not src(pool, "s1").text_first
    await_layout(pool, runs, "u1", ["s1"], parse=True)
    assert len(runs.jobs) == 1


def test_await_layout_ends_on_a_failed_parse_with_the_text_layer(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="ttt")
    layout = FakeLayout(None)
    runs = Runs(deps(layout=layout))
    await_layout(pool, runs, "u1", ["s1"], parse=True)
    assert layout.calls == [3] and src(pool, "s1").text_first and len(runs.jobs) == 1


def test_await_layout_shows_the_progress_and_gives_up_at_the_timeout(env):  # noqa: F811
    pool, _deps = env
    add_source(pool, "s1", make_pdf("t"))         # status reading: a run going
    with pool.connection() as conn:
        store.set_progress(conn, "u1", "s1", {"stage": "parse", "done": 16, "total": 48,
                                              "message": "x"})
    lines: list[str] = []
    with pytest.raises(MaterialsNotReady):
        await_layout(pool, Runs(None), "u1", ["s1"], parse=False, say=lines.append,
                     sleep=lambda _s: None, timeout_s=3)
    assert len(lines) == 1 and "16/48 页" in lines[0]


# --- reading a page precisely ---------------------------------------------------------------------

def test_a_page_is_read_once_kept_and_the_sections_do_not_change(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="tth")
    before = sections(pool, "s1")
    eyes = Reader("claude", answer=lambda p: PageImageRead(f"$x_{p}$", True))
    lines: list[tuple] = []
    got = read_precise(pool, eyes, "u1", "s1", [3], now=lambda: NOW,
                       say=lines.append)
    assert got == {3: "$x_3$"} and eyes.calls == [3] and lines == [[3]]
    assert read_precise(pool, eyes, "u1", "s1", [3], now=lambda: NOW) == {3: "$x_3$"}
    assert eyes.calls == [3]
    assert sections(pool, "s1") == before
    with pool.connection() as conn:              # never routes a later turn to a seeing model
        assert store.page_reads(conn, "u1", "s1")[3].figure is False


def test_a_local_read_does_not_count_and_a_failed_read_is_not_kept(env):  # noqa: F811
    from evowiki.sources.models import PageRead
    pool, deps = env
    attached(pool, deps, kinds="tth")
    with pool.connection() as conn:
        store.put_page_reads(conn, "u1", "s1", [PageRead(3, "t-1/2", "local", False)], NOW)
    assert read_precise(pool, None, "u1", "s1", [3], now=lambda: NOW) == {3: None}

    class Broke(Reader):
        def read(self, pngs):
            raise RuntimeError("402")
    assert read_precise(pool, Broke("claude"), "u1", "s1", [3], now=lambda: NOW) == {3: None}
    eyes = Reader("claude", answer=lambda p: PageImageRead("t^2/2", False))
    assert read_precise(pool, eyes, "u1", "s1", [3], now=lambda: NOW) == {3: "t^2/2"}


# --- pages with a picture in a chat attachment ----------------------------------------------------

CTX = ToolContext(user_id="u1", session_id="x", message_id="m")


def _tool(pool, eyes=None):
    return ReadSourceTool(PooledSourceReader(pool, HashingEmbedder(), eyes, lambda: NOW),
                          image_pages=True)


def test_a_page_with_a_picture_is_no_unreadable_page_and_no_reason_to_wait(env):  # noqa: F811
    pool, deps = env
    layout = FakeLayout(resnet())
    attached(pool, deps, kinds="tfhs", layout=layout)    # `s`: a picture, no text
    row = src(pool, "s1")
    assert layout.calls == [] and row.image_pages == (2, 3, 4)
    assert row.unreadable_pages == () and row.status == "ready"


def test_a_full_import_knows_its_pages_with_a_picture_and_no_page_is_unreadable_for_it(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("tf"))
    go(deps(), "s1", mode="full")
    assert src(pool, "s1").image_pages == (2,) and src(pool, "s1").status == "ready"


def test_reading_a_page_with_a_picture_gives_what_the_reader_saw_never_the_text_layer(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="tf")
    eyes = Reader("claude", answer=lambda p: PageImageRead("$\\rho = c/(n-1)$", False))
    res = _tool(pool, eyes).run({"source_id": "s1", "page_from": 1, "page_to": 2}, CTX)
    assert eyes.calls == [2] and "\\rho" in res.content and "可能读错" in res.content
    assert "explains the idea well" in res.content.split("〔p.2〕")[0]        # p.1, the clean page
    assert "explains the idea well" not in res.content.split("〔p.2〕")[1]


def test_a_failed_read_says_it_was_not_read_and_gives_no_text_layer(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="tf")
    res = _tool(pool).run({"source_id": "s1", "page_from": 2}, CTX)
    assert res.error is None and "第 2 页没能从页面图像读出来" in res.content
    assert "不要说资料没写" in res.content and "explains the idea well" not in res.content


def test_a_section_read_leaves_out_its_pages_with_a_picture(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="tf")
    eyes = Reader("claude")
    sec = next(s for s in sections(pool, "s1") if s.page_from is not None)
    res = _tool(pool, eyes).run({"source_id": "s1", "section_id": sec.section_id}, CTX)
    assert eyes.calls == [] and "第 2 页有图片或绘制的公式，这次没有读" in res.content


def test_too_many_pages_with_a_picture_read_a_few_and_say_the_rest(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="fffffff")
    eyes = Reader("claude")
    res = _tool(pool, eyes).run({"source_id": "s1", "page_from": 1, "page_to": 7}, CTX)
    assert sorted(eyes.calls) == [1, 2, 3, 4, 5] and "第 6–7 页有图片或绘制的公式，这次没有读" in res.content


def test_finding_nothing_is_not_saying_the_material_has_nothing(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="tf")
    reader = PooledSourceReader(pool, HashingEmbedder(), None, lambda: NOW)
    res = SearchSourceTool(reader).run({"query": "no such words anywhere", "source_id": "s1"}, CTX)
    assert "搜不到不等于资料没写" in res.content


def test_the_page_writer_still_reads_the_text_layer(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="tf")
    eyes = Reader("claude")
    tool = ReadSourceTool(PooledSourceReader(pool, HashingEmbedder(), eyes, lambda: NOW))
    res = tool.run({"source_id": "s1", "page_from": 2}, CTX)
    assert eyes.calls == [] and "explains the idea well" in res.content


def test_the_tool_reads_at_most_a_few_pages_and_needs_page_numbers(env):  # noqa: F811
    pool, deps = env
    attached(pool, deps, kinds="tttttttt")
    tool = ReadSourceTool(PooledSourceReader(pool, HashingEmbedder(), Reader("claude"), lambda: NOW))
    ctx = ToolContext(user_id="u1", session_id="x", message_id="m")
    assert tool.run({"source_id": "s1", "page_from": 1, "page_to": 6, "precise": True},
                    ctx).error is not None
    assert tool.run({"source_id": "s1", "section_id": "x", "precise": True}, ctx).error is not None
