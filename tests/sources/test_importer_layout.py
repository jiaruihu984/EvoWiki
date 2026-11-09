"""The import's layout step (`source.layout`): real Postgres, HashingEmbedder, a fake layout
parser that returns a recorded MinerU document (ResNet pages 1-3) or a small hand-made one. No
worker, no model."""

import io
import json
import logging
from pathlib import Path

from PIL import Image
from psycopg.types.json import Jsonb

from evowiki.sources import store
from evowiki.sources.config import PAGE_MARK_RE
from evowiki.sources.document import Block, DocPage, SourceDocument
from evowiki.sources.layout_adapt import from_mineru
from tests.sources.test_importer import NOW, add_source
from tests.sources.test_importer_vision import (  # noqa: F401
    WIDTH0,
    Reader,
    env,
    go,
    make_pdf,
    sections,
)

DATA = Path(__file__).parent / "data" / "layout"


def resnet():
    raw = json.loads((DATA / "mineru_resnet_p1-3.json").read_text(encoding="utf-8"))
    return from_mineru(raw, first_page=1, version="4.0.12")


class FakeLayout:
    def __init__(self, doc, available=True):
        self.doc, self._available = doc, available
        self.calls: list[int] = []
        self.batches: list[tuple[int, int]] = []

    def available(self) -> bool:
        return self._available

    def parse_pdf(self, data, pages, *, on_batch=None, should_stop=None):
        assert data.startswith(b"%PDF")
        assert should_stop is not None and should_stop() is False
        self.calls.append(pages)
        if on_batch is not None:
            on_batch(pages, pages)
            self.batches.append((pages, pages))
        return self.doc


def doc_of(pool, sid):
    with pool.connection() as conn:
        return store.get_document(conn, "u1", sid)


def test_layout_document_is_stored_and_feeds_the_toc(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("ttt"))
    layout = FakeLayout(resnet())
    items = go(deps(layout=layout), "s1")
    assert items[-1][0] == "done" and items[-1][1].status in ("ready", "partial")
    assert layout.calls == [3] and layout.batches == [(3, 3)]
    stored = doc_of(pool, "s1")
    assert stored is not None and stored.backend == "mineru" and stored.version == "4.0.12"
    assert stored == resnet()
    secs = sections(pool, "s1")
    assert any(s.title.startswith("1. Introduction") for s in secs)
    assert "Deep Residual Learning" in "\n".join(s.text for s in secs)
    msgs = [p.message for n, p in items if n == "progress"]
    assert "正在读版面 3/3 页" in msgs


def test_layout_failure_falls_back_to_the_text_layer_and_says_so(env, caplog):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("ttt"))
    with caplog.at_level(logging.WARNING, logger="evowiki.sources.importer"):
        items = go(deps(layout=FakeLayout(None)), "s1")
    assert items[-1][0] == "done" and items[-1][1].status in ("ready", "partial")
    stored = doc_of(pool, "s1")
    assert stored is not None and stored.backend == "pypdfium2" and stored.version
    assert [p.page for p in stored.pages] == [1, 2, 3] and stored.blocks == ()
    assert "layout" in caplog.text
    assert "explains the idea well" in "\n".join(s.text for s in sections(pool, "s1"))


def test_a_later_run_reuses_the_stored_document(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("ttt"))
    layout = FakeLayout(resnet())
    go(deps(layout=layout), "s1")
    with pool.connection() as conn:
        gen = store.bump_run(conn, "u1", "s1")
    items = go(deps(layout=layout), "s1", gen=gen)
    assert items[-1][0] == "done"
    assert layout.calls == [3]
    assert any(s.title.startswith("1. Introduction") for s in sections(pool, "s1"))


def test_a_later_run_retries_after_a_failed_parse(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("ttt"))
    go(deps(layout=FakeLayout(None)), "s1")
    assert doc_of(pool, "s1").backend == "pypdfium2"
    with pool.connection() as conn:
        gen = store.bump_run(conn, "u1", "s1")
    layout = FakeLayout(resnet())
    assert go(deps(layout=layout), "s1", gen=gen)[-1][0] == "done"
    assert layout.calls == [3]
    assert doc_of(pool, "s1").backend == "mineru"
    assert any(s.title.startswith("1. Introduction") for s in sections(pool, "s1"))


def test_no_layout_configured_keeps_todays_path(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("ttt"))
    items = go(deps(), "s1")
    assert items[-1][0] == "done"
    assert doc_of(pool, "s1") is None
    assert not [p for n, p in items if n == "progress" and "版面" in p.message]


def test_an_unavailable_parser_stores_nothing(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("ttt"))
    layout = FakeLayout(resnet(), available=False)
    assert go(deps(layout=layout), "s1")[-1][0] == "done"
    assert layout.calls == [] and doc_of(pool, "s1") is None


def test_a_chat_attachment_run_that_keeps_its_sections_never_parses(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("ttt"))
    go(deps(), "s1", mode="ask")
    with pool.connection() as conn:
        gen = store.bump_run(conn, "u1", "s1")
    layout = FakeLayout(resnet())
    assert go(deps(layout=layout), "s1", gen=gen, mode="ask",
              keep_sections=True)[-1][0] == "done"
    assert layout.calls == []


def test_a_pptx_import_stores_its_document(env):  # noqa: F811
    import io

    from pptx import Presentation
    from pptx.util import Emu, Pt
    pool, deps = env
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[6])
    t = s.shapes.add_textbox(Emu(400000), Emu(300000), Emu(6000000), Emu(800000))
    t.text_frame.text = "Residual learning"
    t.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
    b = s.shapes.add_textbox(Emu(400000), Emu(3000000), Emu(6000000), Emu(800000))
    b.text_frame.text = "Skip connections let gradients flow through deep networks."
    b.text_frame.paragraphs[0].runs[0].font.size = Pt(18)
    buf = io.BytesIO()
    prs.save(buf)
    add_source(pool, "s1", buf.getvalue(), kind="pptx",
               mime="application/vnd.openxmlformats-officedocument.presentationml.presentation")
    assert go(deps(), "s1")[-1][0] == "done"
    stored = doc_of(pool, "s1")
    assert stored is not None and stored.backend == "pptx"
    assert [(h.title, h.source) for h in stored.headings] == [("Residual learning", "slide_title")]
    assert any(b.kind == "heading" and b.text == "Residual learning" for b in stored.blocks)


# -- pages the layout parser read from their image are not read again --

P1 = ("Gradient descent moves the weights against the gradient of the loss, one small step "
      "at a time, until the loss stops falling.")
F1 = "w′ = w − η∇L(w), ∑ ≤ ∫ ≥ ± ∞"    # math symbols: a `formula_or_table` hint
P2 = ("Momentum keeps a running average of past gradients, so the steps carry on through flat "
      "regions and damp the zig-zag across narrow valleys.")


def scanned_pdf() -> bytes:
    """Page 1: a text layer + a picture over most of the page (an `image_page` hint); page 2: a
    scan (one picture, no text layer: unreadable). Widths as `make_pdf` (fake readers tell pages
    apart by width)."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    img = ImageReader(Image.new("RGB", (64, 64), "gray"))
    for p in (1, 2):
        w, h = WIDTH0 + 10 * p, 500
        c.setPageSize((w, h))
        if p == 1:
            c.drawString(20, h - 40, "Gradient descent moves the weights against the gradient")
            c.drawImage(img, 20, 20, width=w - 40, height=400)
        else:
            c.drawImage(img, 0, 0, width=w, height=h)
        c.showPage()
    c.save()
    return buf.getvalue()


def scanned_layout() -> SourceDocument:
    return SourceDocument(
        kind="pdf", backend="mineru", version="test", pages=(DocPage(1), DocPage(2)),
        blocks=(Block("p1-b1", 1, "heading", "1. Gradient descent", level=1),
                Block("p1-b2", 1, "paragraph", P1),
                Block("p1-b3", 1, "formula", "", latex=F1),
                Block("p2-b1", 2, "heading", "2. Momentum", level=1),
                Block("p2-b2", 2, "paragraph", P2)))


def body(text: str) -> str:
    """Section text without its page marks."""
    return " ".join(PAGE_MARK_RE.sub(" ", text).split())


def test_layout_pages_are_not_read_again_or_patched(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", scanned_pdf())
    local = Reader("local")
    items = go(deps(layout=FakeLayout(scanned_layout()), local=local), "s1")
    assert items[-1][0] == "done" and items[-1][1].status == "ready"
    assert items[-1][1].unreadable_pages == []
    assert local.calls == []
    secs = {s.title: body(s.text) for s in sections(pool, "s1")}
    assert secs["1. Gradient descent"] == f"1. Gradient descent {P1} $${F1}$$"
    assert secs["2. Momentum"] == f"2. Momentum {P2}"
    assert not any("local" in t for t in secs.values())


def test_a_kept_run_of_a_layout_import_stays_as_it_was(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", scanned_pdf())
    layout = FakeLayout(scanned_layout())
    go(deps(layout=layout), "s1")
    before = [(s.section_id, s.title, s.text) for s in sections(pool, "s1")]
    with pool.connection() as conn:
        gen = store.bump_run(conn, "u1", "s1")
    local = Reader("local")
    items = go(deps(layout=layout, local=local), "s1", gen=gen, keep_sections=True)
    assert items[-1][0] == "done" and items[-1][1].status == "ready"
    assert items[-1][1].unreadable_pages == []
    assert layout.calls == [2]                               # the first run's parse only
    assert local.calls == []
    assert [(s.section_id, s.title, s.text) for s in sections(pool, "s1")] == before


def test_a_parse_stopped_by_a_delete_ends_quietly(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("ttt"))

    class DeletedMidParse(FakeLayout):
        def parse_pdf(self, data, pages, *, on_batch=None, should_stop=None):
            with pool.connection() as conn:
                store.soft_delete(conn, "u1", "s1", NOW)
            assert should_stop is not None and should_stop() is True
            return self.doc                              # None: the client stopped
    items = go(deps(layout=DeletedMidParse(None)), "s1")
    assert not [n for n, _ in items if n in ("done", "error")]
    assert doc_of(pool, "s1") is None                    # no placeholder for a deleted source


def _fallback_named(pool, sid: str, title: str) -> None:
    with pool.connection() as conn:
        conn.execute("update source set title = %s, title_source = 'fallback' where source_id = %s",
                     (title, sid))


def _row(pool, sid: str):
    with pool.connection() as conn:
        return store.get_source(conn, "u1", sid)


def test_a_file_name_gives_way_to_the_first_heading_and_stays_an_earlier_name(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("ttt"))
    _fallback_named(pool, "s1", "resnet")
    first = resnet().headings[0].title
    go(deps(layout=FakeLayout(resnet())), "s1")
    row = _row(pool, "s1")
    assert (row.title, row.title_source, row.earlier_titles) == (first, "document", ("resnet",))


def test_the_users_name_is_never_replaced(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("ttt"))
    _fallback_named(pool, "s1", "resnet")
    with pool.connection() as conn:
        store.rename(conn, "u1", "s1", "我的讲义")
    go(deps(layout=FakeLayout(resnet())), "s1")
    row = _row(pool, "s1")
    assert (row.title, row.title_source, row.earlier_titles) == ("我的讲义", "given", ("resnet",))


def test_a_goal_draft_follows_the_title_and_keeps_its_own_words(env):  # noqa: F811
    pool, deps = env
    add_source(pool, "s1", make_pdf("ttt"))
    _fallback_named(pool, "s1", "resnet")
    first = resnet().headings[0].title
    content = {"source_ids": ["s1"], "goal": "读懂《resnet》", "title": "resnet",
               "source_titles": ["resnet"], "said": "带我读 resnet"}
    with pool.connection() as conn:
        conn.execute(
            "insert into agent_action (action_id, user_id, kind, level, status, scope, source, summary, idempotency_key, "
            "content, content_hash, why, created_at) values ('a1', 'u1', 'goal_new', 'ask', 'proposed', "
            "'structure', 'assistant', '「resnet」分成 2 块', 'k', %s, 'h', '按你发的《resnet》分成了 2 块', now())",
            (Jsonb(content),))
    go(deps(layout=FakeLayout(resnet())), "s1")
    with pool.connection() as conn:
        row = conn.execute("select summary, why, content from agent_action where action_id = 'a1'"
                           ).fetchone()
    assert row["why"] == f"按你发的《{first}》分成了 2 块"
    assert row["content"]["goal"] == f"读懂《{first}》" and row["content"]["title"] == first
    assert row["content"]["source_titles"] == [first] and row["content"]["said"] == "带我读 resnet"
