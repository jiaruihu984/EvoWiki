"""Mode "ask" (a chat attachment): no Claude, no quote, no summaries, embedded only when too long
to hand over whole; an image is read locally only when the assistant cannot see it."""

from pathlib import Path

import pytest

from evowiki.sources import store
from evowiki.sources.vision import PageImageRead
from tests.sources.test_importer import CountingLLM, add_source, src
from tests.sources.test_importer_vision import Reader, go, make_env, make_pdf, names

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "sources"


@pytest.fixture
def env(pool):
    return make_env(pool)


TEXT = "# 标题\n\n第一段文字，讲一件事。\n\n## 小节\n\n第二段文字，讲另一件事。".encode()


def _chunks(pool, sid):
    with pool.connection() as conn:
        return store.chunks_of(conn, "u1", [sid])


def test_ask_image_is_read_locally_when_the_assistant_cannot_see_it(env):
    pool, deps = env
    add_source(pool, "i1", (FIX / "photo.png").read_bytes(), kind="image")
    local = Reader("local", answer=lambda _p: PageImageRead("图里的字", True))
    claude = Reader("claude")
    items = go(deps(local=local, claude=claude, assistant_sees_images=lambda _u: False), "i1",
               mode="ask")
    assert len(local.calls) == 1 and claude.calls == []      # a figure, but never Claude here
    assert names(items) == ["usable", "done"] and items[-1][1].status == "ready"
    with pool.connection() as conn:
        assert "图里的字" in store.sections(conn, "u1", "i1")[0].text


def test_ask_image_is_not_read_when_the_assistant_sees_images(env):
    pool, deps = env
    add_source(pool, "i1", (FIX / "photo.png").read_bytes(), kind="image")
    local, claude = Reader("local"), Reader("claude")
    items = go(deps(local=local, claude=claude, assistant_sees_images=lambda _u: True), "i1",
               mode="ask")
    assert local.calls == [] and claude.calls == []
    done = items[-1][1]
    assert items[-1][0] == "done" and done.status == "ready" and done.unreadable_pages == []
    usable = next(p for n, p in items if n == "usable")
    assert usable.status == "ready" and usable.unreadable_pages == []
    row = src(pool, "i1")
    assert row.status == "ready" and row.unreadable_pages == ()


def test_ask_scan_reads_locally_and_leaves_the_rest_without_a_quote(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("b" * 25))
    local = Reader("local", answer=lambda p: PageImageRead(None, False, "repeat") if p > 20
                   else PageImageRead(f"本地第{p}页", p == 3))
    claude = Reader("claude")
    done = go(deps(local=local, claude=claude), "s1", mode="ask")[-1][1]
    assert local.calls == list(range(1, 26)) and claude.calls == []
    assert done.status == "partial" and done.unreadable_pages == [21, 22, 23, 24, 25]
    assert "看图读" not in (done.note or "")


def test_ask_small_file_is_neither_embedded_nor_summarized(env):
    pool, deps = env
    llm = CountingLLM()
    add_source(pool, "t1", TEXT, kind="text")
    items = go(deps(llm_for=lambda _u: llm), "t1", mode="ask")
    assert [p.stage for n, p in items if n == "progress"].count("embed") == 0
    assert items[-1][0] == "done" and items[-1][1].status == "ready"
    assert _chunks(pool, "t1") == [] and llm.calls == []
    assert src(pool, "t1").progress["mode"] == "ask"     # a learning use reads it again in full


def test_ask_long_file_is_embedded_but_not_summarized(env, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")
    pool, deps = env
    llm = CountingLLM()
    add_source(pool, "t1", TEXT, kind="text")
    items = go(deps(llm_for=lambda _u: llm), "t1", mode="ask")
    assert items[-1][0] == "done" and items[-1][1].note is None
    assert _chunks(pool, "t1") and llm.calls == []
    with pool.connection() as conn:
        assert not any(s.summary for s in store.sections(conn, "u1", "t1"))


def test_full_mode_of_the_same_long_file_summarizes(env, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")
    pool, deps = env
    llm = CountingLLM()
    add_source(pool, "t1", TEXT, kind="text")
    go(deps(llm_for=lambda _u: llm), "t1")
    assert llm.calls and set(llm.calls) == {"sources.glossary", "sources.summary"}
    assert src(pool, "t1").progress["mode"] == "full"
