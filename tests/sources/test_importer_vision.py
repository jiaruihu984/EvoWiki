"""The import's page reading (step 3 before the toc, the ocr stage after `usable`): real
Postgres, HashingEmbedder, fake page readers (no OCR worker, no model). Pages of the generated
PDFs differ in width, so a fake reader can tell from the image which page it was given."""

import io
from datetime import UTC, datetime
from itertools import count

import pytest
from PIL import Image

from evowiki.agent import durable
from evowiki.llm.providers import ProviderRejectedError
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import store
from evowiki.sources.importer import (
    ImportDeps,
    ImportJob,
    SourceDone,
    SourceFailed,
    _swap_page,
    execute,
)
from evowiki.sources.vision import PageImageRead
from tests.runtime.helpers import make_user
from tests.sources.test_importer import add_source, src

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
WIDTH0, STEP, DPI = 300, 10, 150


def _word(n: int) -> str:
    return "".join(chr(97 + (n * 7 + k * 3) % 26) for k in range(6))


def make_pdf(kinds: str) -> bytes:
    """One page per letter: `t` text, `h` text + a picture over half the page (an OCR hint),
    `f` text + a formula-sized picture, `s` a scan (only a picture), `b` blank (unreadable).
    Page p is WIDTH0 + STEP*p points wide."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for p, k in enumerate(kinds, 1):
        w, h = WIDTH0 + STEP * p, 500
        c.setPageSize((w, h))
        if k in "thf":
            y = h - 40
            for i in range(6):
                c.drawString(20, y, f"{_word(p)} {_word(p * 10 + i)} explains the idea well")
                y -= 16
        if k in "hs":
            img = Image.new("RGB", (64, 64), "gray")
            c.drawImage(ImageReader(img), 20, 20, width=w - 40, height=300)
        if k == "f":
            img = Image.new("RGB", (64, 64), "gray")
            c.drawImage(ImageReader(img), 20, 20, width=120, height=40)
        c.showPage()
    c.save()
    return buf.getvalue()


def page_of(png: bytes) -> int:
    width = Image.open(io.BytesIO(png)).size[0]
    return round((width * 72 / DPI - WIDTH0) / STEP)


class Reader:
    """A fake VisionReader; `answer(page)` is its read of that page."""

    def __init__(self, name, answer=None, available=True, timeline=None):
        self.name = name
        self.answer = answer or (lambda p: PageImageRead(f"{name} 读的第{p}页文字", False))
        self._available = available
        self.calls: list[int] = []
        self.timeline = timeline

    def available(self) -> bool:
        return self._available

    def for_user(self, _user_id):
        return self

    def read(self, pngs):
        out = []
        for png in pngs:
            p = page_of(png)
            self.calls.append(p)
            if self.timeline is not None:
                self.timeline.append((self.name, p))
            out.append(self.answer(p))
        return out


def _ids():
    c = count(1)
    return lambda: f"v{next(c):05d}"


@pytest.fixture
def env(pool):
    return make_env(pool)


def make_env(pool):
    """(pool, deps(**overrides)) with user u1 and readers that are not available."""
    make_user(pool, "u1")
    ids = _ids()

    def deps(**kw) -> ImportDeps:
        base = {"pool": pool, "embedder": HashingEmbedder(), "llm_for": lambda _u: None,
                "local": Reader("local", available=False),
                "claude": Reader("claude", available=False),
                "assistant_sees_images": lambda _u: False, "clock": lambda: NOW, "new_id": ids}
        return ImportDeps(**(base | kw))
    return pool, deps


def go(deps: ImportDeps, sid: str, timeline: list | None = None, **job) -> list:
    """Runs the job inline; `timeline` (when given) gets the items next to the readers' calls."""
    out: list = []

    def emit(name, payload):
        if timeline is not None:
            timeline.append(("item", name, payload))
        out.append((name, payload))
    with durable.virtual_workflow(f"t:{sid}:{job.get('gen', 0)}"):
        execute(ImportJob(user_id="u1", source_id=sid, **job), emit, deps)
    return out


def sections(pool, sid):
    with pool.connection() as conn:
        return store.sections(conn, "u1", sid)


def reads(pool, sid):
    with pool.connection() as conn:
        return conn.execute("select page, by, text, figure from source_page_read "
                            "where source_id = %s order by page, by", (sid,)).fetchall()


def names(items):
    return [n for n, _ in items if n != "progress"]


def test_scan_local_first_then_claude_for_the_figure_page(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("bbb"))
    local = Reader("local", answer=lambda p: {
        1: PageImageRead("第一页本地读的文字", False),
        2: PageImageRead("第二页只有图", True),
        3: PageImageRead('<div><img src="imgs/a.jpg"></div>\n第三页本地文字', False)}[p])
    claude = Reader("claude", answer=lambda p: PageImageRead("第二页Claude看图读的", False))
    items = go(deps(local=local, claude=claude), "s1")
    assert local.calls == [1, 2, 3] and claude.calls == [2]
    assert names(items) == ["usable", "done"]
    usable, done = items[[n for n, _ in items].index("usable")][1], items[-1][1]
    assert usable.status == "ready" and done.status == "ready" and done.unreadable_pages == []
    text = "\n".join(s.text for s in sections(pool, "s1"))
    assert "第一页本地读的文字" in text and "第二页Claude看图读的" in text
    assert "第三页本地文字" in text and "<img" not in text and "<div>" not in text
    assert "第二页只有图" not in text            # Claude's reading replaced the figure page's
    assert [(r["page"], r["by"]) for r in reads(pool, "s1")] == [
        (1, "local"), (2, "claude"), (2, "local"), (3, "local")]
    assert [r["figure"] for r in reads(pool, "s1") if r["by"] == "local"] == [False, True, False]


def test_many_unreadable_pages_are_all_read_by_claude_without_a_quote(env):
    """No cost gate: above PRE_READ_MAX they are read after `usable`, in the same run; the
    user's own spending limit is the only stop."""
    pool, deps = env
    add_source(pool, "s1", make_pdf("s" * 25))
    claude = Reader("claude")
    items = go(deps(claude=claude), "s1")
    assert claude.calls == list(range(1, 26))
    assert names(items) == ["usable", "done"]
    usable, done = items[[n for n, _ in items].index("usable")][1], items[-1][1]
    assert usable.status == "partial"                    # usable before the pages are read
    assert done.status == "ready" and done.unreadable_pages == []
    assert "看图读" not in (done.note or "")
    assert "claude 读的第25页文字" in "\n".join(s.text for s in sections(pool, "s1"))


def test_few_characters_and_no_image_never_go_to_claude(env):
    """Without a local reader such a page stays reported as unread; a local reader that found it
    empty, or nearly (a page number), settles it. A figure block or a scan still goes."""
    pool, deps = env
    add_source(pool, "s1", make_pdf("bb"))
    claude = Reader("claude")
    done = go(deps(claude=claude), "s1")[-1][1]
    assert claude.calls == [] and done.unreadable_pages == [1, 2]

    add_source(pool, "s2", make_pdf("bbbs"))
    local = Reader("local", answer=lambda p: {
        1: PageImageRead("", True, thin_only=True), 2: PageImageRead("12", True, thin_only=True),
        3: PageImageRead("", True), 4: PageImageRead("", True, thin_only=True)}[p])
    done = go(deps(local=local, claude=claude), "s2")[-1][1]
    assert claude.calls == [3, 4]
    assert done.status == "ready" and done.unreadable_pages == []


def test_no_claude_reader_leaves_the_pages_unread(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("b" * 25))
    done = go(deps(), "s1")[-1][1]
    assert done.status == "partial" and "看图读" not in done.note


def test_a_later_generation_does_not_read_stored_pages_again(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("bbbt"))
    local = Reader("local", answer=lambda p: PageImageRead(None, False, "repeat") if p == 2
                   else PageImageRead(f"本地第{p}页", False))
    done = go(deps(local=local), "s1")[-1][1]
    assert local.calls == [1, 2, 3] and done.unreadable_pages == [2]
    with pool.connection() as conn:
        gen = store.bump_run(conn, "u1", "s1")
    local2 = Reader("local")
    claude = Reader("claude")
    done = go(deps(local=local2, claude=claude), "s1", gen=gen)[-1][1]
    assert local2.calls == [2]                  # only the page nobody could read yet
    assert claude.calls == []                   # the local reader read it this time
    assert done.status == "ready"
    text = "\n".join(s.text for s in sections(pool, "s1"))
    assert "本地第1页" in text and "local 读的第2页文字" in text


def test_hinted_pages_are_read_after_usable_and_swapped_into_their_sections(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("tttt" + "h" * 25 + "t"))
    timeline: list = []
    local = Reader("local", answer=lambda p: PageImageRead(f"OCR读的第{p}页", p == 7),
                   timeline=timeline)
    claude = Reader("claude", timeline=timeline)
    items = go(deps(local=local, claude=claude), "s1", timeline)
    first_read = next(i for i, t in enumerate(timeline) if t[0] == "local")
    usable_at = next(i for i, t in enumerate(timeline) if t[:2] == ("item", "usable"))
    assert usable_at < first_read                      # usable before any OCR call
    assert local.calls == list(range(5, 30))
    assert claude.calls == []                          # hinted pages never go to Claude
    ocr = [p for n, p in items if n == "progress" and p.stage == "ocr"]
    assert ocr and all(n != "usable" for n, _ in items[usable_at + 1:])
    assert items[-1][0] == "done" and items[-1][1].status == "ready"
    text = "\n".join(s.text for s in sections(pool, "s1"))
    assert "OCR读的第5页" in text and "OCR读的第29页" in text
    assert f"{_word(5)} {_word(50)}" not in text       # the text layer of page 5 is gone
    assert f"{_word(1)} {_word(10)}" in text           # page 1 (no hint) kept its text layer
    with pool.connection() as conn:
        chunks = store.chunks_of(conn, "u1", ["s1"])
        n_emb = conn.execute("select count(*) as n from source_section where source_id = 's1' "
                             "and embedding is null").fetchone()["n"]
    assert any("OCR读的第17页" in c.text for c in chunks)
    assert not any(f"{_word(17)} {_word(170)}" in c.text for c in chunks)
    assert n_emb == 0                                  # changed sections were embedded again


def test_a_hinted_page_the_local_reader_fails_keeps_its_text_layer(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("th"))
    local = Reader("local", answer=lambda p: PageImageRead(None, False, "timeout"))
    done = go(deps(local=local, claude=Reader("claude")), "s1")[-1][1]
    assert local.calls == [2] and done.status == "ready"
    assert f"{_word(2)} {_word(20)}" in "\n".join(s.text for s in sections(pool, "s1"))


def test_three_unreadable_pages_are_read_before_usable(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("tbtbtb"))
    timeline: list = []
    local = Reader("local", timeline=timeline)
    items = go(deps(local=local), "s1", timeline)
    usable_at = next(i for i, t in enumerate(timeline) if t[:2] == ("item", "usable"))
    assert [t for t in timeline[:usable_at] if t[0] == "local"] == [
        ("local", 2), ("local", 4), ("local", 6)]
    usable = next(p for n, p in items if n == "usable")
    assert usable.status == "ready" and usable.unreadable_pages == []


def test_many_unreadable_pages_become_usable_as_partial_then_ready(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("t" + "b" * 25))
    timeline: list = []
    local = Reader("local", timeline=timeline)
    items = go(deps(local=local), "s1", timeline)
    usable_at = next(i for i, t in enumerate(timeline) if t[:2] == ("item", "usable"))
    assert not any(t[0] == "local" for t in timeline[:usable_at])
    usable = next(p for n, p in items if n == "usable")
    assert isinstance(usable, SourceDone) and usable.status == "partial"
    assert usable.unreadable_pages == list(range(2, 27))
    assert items[-1][1].status == "ready" and items[-1][1].unreadable_pages == []
    row = src(pool, "s1")
    assert row.status == "ready" and row.unreadable_pages == ()
    assert "local 读的第26页文字" in "\n".join(s.text for s in sections(pool, "s1"))
    with pool.connection() as conn:
        assert any("第26页" in c.text for c in store.chunks_of(conn, "u1", ["s1"]))


def test_a_402_while_claude_reads_pages_keeps_the_source_usable(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("sst"))

    def answer(p):
        if p == 2:
            raise ProviderRejectedError("402", status_code=402)
        return PageImageRead(f"Claude第{p}页", False)
    claude = Reader("claude", answer=answer)
    items = go(deps(claude=claude), "s1")
    assert claude.calls == [1, 2]
    assert names(items) == ["usable", "error"]
    assert items[-1] == ("error", SourceFailed(detail="The model service rejected the request. Check its credentials, permissions or balance.", code="provider_rejected"))
    row = src(pool, "s1")
    assert row.status == "partial" and row.unreadable_pages == (2,)
    assert row.progress["end"] == "error"
    assert [(r["page"], r["by"]) for r in reads(pool, "s1")] == [(1, "claude")]   # paid: kept


def test_a_newer_generation_stops_the_older_run_quietly(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("tt"))
    with pool.connection() as conn:
        store.bump_run(conn, "u1", "s1")
    items = go(deps(), "s1")                    # gen 0, but the row is at gen 1
    assert items == []
    assert src(pool, "s1").status == "reading"


def test_generation_changed_during_page_read_cannot_save_old_text(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("s"))

    def changed_while_reading(page):
        with pool.connection() as conn:
            store.bump_run(conn, "u1", "s1")
        return PageImageRead("old generation result", False)

    go(deps(local=Reader("local", answer=changed_while_reading)), "s1")
    assert src(pool, "s1").run_gen == 1
    assert reads(pool, "s1") == []


def test_swap_page_replaces_or_inserts_in_page_order():
    text = "〔p.3〕\n三\n\n〔p.5〕\n五"
    assert _swap_page(text, 5, "新五") == "〔p.3〕\n三\n\n〔p.5〕\n新五"
    assert _swap_page(text, 4, "四") == "〔p.3〕\n三\n\n〔p.4〕\n四\n\n〔p.5〕\n五"
    assert _swap_page("", 2, "二") == "〔p.2〕\n二"


class TimedEmbedder(HashingEmbedder):
    def __init__(self, timeline):
        super().__init__()
        self.timeline = timeline

    def embed_documents(self, texts):
        self.timeline.append(("embed", len(texts)))
        return super().embed_documents(texts)


def test_usable_comes_before_any_vector_and_vectors_follow(env):
    pool, deps = env
    add_source(pool, "s1", make_pdf("tttttt"))
    timeline: list = []
    items = go(deps(embedder=TimedEmbedder(timeline)), "s1", timeline)
    usable_at = next(i for i, t in enumerate(timeline) if t[:2] == ("item", "usable"))
    first_embed = next(i for i, t in enumerate(timeline) if t[0] == "embed")
    assert usable_at < first_embed
    embed = [p for n, p in items if n == "progress" and p.stage == "embed"]
    assert embed and embed[-1].done == embed[-1].total
    assert items[-1][0] == "done"
    with pool.connection() as conn:
        assert store.unembedded_chunks(conn, "u1", "s1") == []
        assert store.unembedded_sections(conn, "u1", "s1") == []
        assert store.chunks_of(conn, "u1", ["s1"])


def test_vision_budget_stop_keeps_paid_pages_and_coded_replayed_sse(env):
    from evowiki.i18n import language_context
    from evowiki.service.source_schemas import source_out
    from evowiki.sources.runs import settled
    from tests.sources.test_importer import _over_budget

    pool, deps = env
    add_source(pool, "s1", make_pdf("sst"))

    def answer(page):
        if page == 2:
            raise _over_budget()
        return PageImageRead("original paid page text", False)

    claude = Reader("claude", answer=answer)
    with language_context("en"):
        items = go(deps(claude=claude), "s1")
        assert claude.calls == [1, 2]
        assert items[-1][1].code == "budget_exceeded"
        assert "This material was not fully read" in items[-1][1].detail
        row = src(pool, "s1")
        out = source_out(row)
        assert out.status == "partial" and out.error_code == "budget_exceeded"
        assert out.can_retry
        replayed = settled(pool, "u1", "s1")
        assert replayed == items[-1]
    assert "original paid page text" in "\n".join(section.text for section in sections(pool, "s1"))
