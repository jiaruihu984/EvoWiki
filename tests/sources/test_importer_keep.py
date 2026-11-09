"""A later run that keeps the sections (`ImportJob.keep_sections`, D21): a chat attachment put to
a learning use is not re-cut. Section ids, the user's toc edits and the links to the sections
stay; the run adds what the full mode needs (chunks, vectors, glossary, summaries, pages read)."""

import pytest

from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import store
from evowiki.sources.models import LinkRow, PageRead, SourceRow
from evowiki.sources.uses import keeps_sections
from evowiki.sources.vision import PageImageRead
from tests.sources.test_importer import NOW, CountingLLM, add_source, src
from tests.sources.test_importer_ask import TEXT
from tests.sources.test_importer_vision import Reader, go, make_env, make_pdf, names, sections


@pytest.fixture
def env(pool):
    return make_env(pool)


def _next_gen(pool, sid) -> int:
    with pool.connection() as conn:
        return store.bump_run(conn, "u1", sid)


def test_a_learning_run_keeps_the_edited_toc_and_links_and_adds_what_learning_needs(
        env, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")      # long: learning wants summaries
    pool, deps = env
    llm = CountingLLM()
    add_source(pool, "t1", TEXT, kind="md")
    with monkeypatch.context() as m:                          # the chat attachment: short
        m.setenv("EVOWIKI_FULL_TEXT_TOKENS", "30000")
        go(deps(llm_for=lambda _u: llm), "t1", mode="ask")
    assert llm.calls == []
    before = sections(pool, "t1")
    first, second = before[0].section_id, before[1].section_id
    with pool.connection() as conn:
        store.edit_sections(conn, "u1", "t1", [(second, "老师改的标题", 0)])
        store.replace_links(conn, "u1", ["c1"], [LinkRow("c1", second, "t1", "teaches", "user")],
                            by="user", now=NOW)

    items = go(deps(llm_for=lambda _u: llm), "t1", mode="full", keep_sections=True,
               gen=_next_gen(pool, "t1"))
    assert names(items) == ["usable", "done"]
    after = sections(pool, "t1")
    assert [s.section_id for s in after] == [first, second]
    assert (after[1].title, after[1].depth, after[1].parent_id) == ("老师改的标题", 0, None)
    with pool.connection() as conn:
        assert store.links_for_nodes(conn, "u1", ["c1"]) == [
            LinkRow("c1", second, "t1", "teaches", "user")]
        assert store.chunks_of(conn, "u1", ["t1"])
        assert store.unembedded_chunks(conn, "u1", "t1", HashingEmbedder()) == []
        assert store.unembedded_sections(conn, "u1", "t1", HashingEmbedder()) == []
    assert all(s.summary for s in after)
    assert set(llm.calls) == {"sources.glossary", "sources.summary"}
    assert src(pool, "t1").progress["mode"] == "full"      # needs_full_run: done now


def test_a_learning_run_reads_the_rest_of_a_scan_into_the_kept_sections(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("bbb"))
    local = Reader("local", answer=lambda p: PageImageRead(
        "第二页只有图" if p == 2 else f"第{p}页本地读的文字", p == 2))
    go(deps(local=local), "s1", mode="ask")
    ids = [s.section_id for s in sections(pool, "s1")]
    claude = Reader("claude", answer=lambda p: PageImageRead("第二页Claude看图读的", False))
    local.calls.clear()
    items = go(deps(local=local, claude=claude), "s1", mode="full", keep_sections=True,
               gen=_next_gen(pool, "s1"))
    assert local.calls == [] and claude.calls == [2]          # local reads are reused
    assert items[-1][0] == "done" and items[-1][1].status == "ready"
    after = sections(pool, "s1")
    assert [s.section_id for s in after] == ids
    text = "\n".join(s.text for s in after)
    assert "第二页Claude看图读的" in text and "第二页只有图" not in text
    assert text.count("第1页本地读的文字") == 1 and text.count("第3页本地读的文字") == 1


def test_a_read_the_earlier_run_had_not_swapped_in_goes_into_its_section(env):
    """The learning use came while the chat run was still reading pages: that run stopped
    before swapping its reads in; the kept sections get them now."""
    pool, deps = env
    add_source(pool, "s1", make_pdf("tb"))
    go(deps(), "s1", mode="ask")
    with pool.connection() as conn:
        store.put_page_reads(conn, "u1", "s1", [PageRead(2, "第二页后来读出的字", "local", False)],
                             NOW)
    go(deps(), "s1", mode="full", keep_sections=True, gen=_next_gen(pool, "s1"))
    assert "第二页后来读出的字" in "\n".join(s.text for s in sections(pool, "s1"))
    assert src(pool, "s1").unreadable_pages == ()


def test_with_no_sections_yet_the_toc_is_built(env):
    pool, deps = env
    add_source(pool, "t1", TEXT, kind="md")
    items = go(deps(), "t1", mode="full", keep_sections=True)
    assert names(items) == ["usable", "done"] and len(sections(pool, "t1")) == 2


@pytest.mark.parametrize(("progress", "keep"), [
    ({}, True),                                               # a run still going (ask)
    ({"end": "done", "mode": "ask"}, True),
    ({"end": "error", "stage": "summary"}, True),
    ({"end": "error", "stage": "toc"}, False),                # page groups after a 402: rebuild
])
def test_which_later_runs_keep_the_sections(progress, keep):
    row = SourceRow(source_id="s", user_id="u1", kind="pdf", title="t", sha256="s", bytes=1,
                    pages=None, status="ready", error=None, unreadable_pages=(), toc_mode=None,
                    progress=progress, origin=None, created_at=NOW, updated_at=NOW,
                    deleted_at=None)
    assert keeps_sections(row) is keep
