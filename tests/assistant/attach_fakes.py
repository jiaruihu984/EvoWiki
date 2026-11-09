"""Fakes for the chat-attachment tests: materials as `Attachment`s and a tiny real PNG."""

from datetime import timedelta
from io import BytesIO

from PIL import Image

from evowiki.assistant.attachments import Attachment, MemoryAttachmentReader
from evowiki.sources.models import PageRead, SectionRow, SourceRow
from tests.assistant.fakes import T0


def png(color: str = "red", size: int = 8) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


def src(sid: str, *, kind: str = "pdf", title: str | None = None, status: str = "ready",
        pages: int | None = 3, deleted: bool = False, unreadable: tuple[int, ...] = (),
        user: str = "u1", size: int = 1000) -> SourceRow:
    return SourceRow(source_id=sid, user_id=user, kind=kind,  # type: ignore[arg-type]
                     title=title or (f"{sid}.png" if kind == "image" else f"{sid}.pdf"),
                     sha256="0" * 64, bytes=size, pages=pages, status=status,  # type: ignore[arg-type]
                     error=None, unreadable_pages=unreadable, toc_mode="pages", progress={},
                     origin=None, created_at=T0, updated_at=T0,
                     deleted_at=T0 + timedelta(days=1) if deleted else None)


def sec(sid: str, i: int, text: str, *, page: int | None = None, title: str | None = None,
        read_by: str = "text") -> SectionRow:
    return SectionRow(section_id=f"{sid}-s{i}", source_id=sid, parent_id=None, ord=i, depth=0,
                      title=title or f"第 {i} 节", page_from=page, page_to=page, text=text,
                      tokens=len(text), summary=None, read_by=read_by)  # type: ignore[arg-type]


def doc(sid: str, texts: list[str], *, uses: tuple[str, ...] = ("ask",),
        figures: tuple[int, ...] = (), **kw) -> Attachment:
    s = src(sid, pages=max(len(texts), *figures, 1), **kw)
    reads = {p: PageRead(p, None, "local", True) for p in figures}
    return Attachment(s, uses, False, tuple(sec(sid, i, t, page=i + 1)
                                            for i, t in enumerate(texts)), reads)


def image(sid: str, *, ocr: str | None = None, figure: bool = False,
          uses: tuple[str, ...] = ("ask",), **kw) -> Attachment:
    s = src(sid, kind="image", pages=1, **kw)
    reads = {} if ocr is None and not figure else {1: PageRead(1, ocr, "local", figure)}
    return Attachment(s, uses, False, (), reads)


def reader(*items: tuple[Attachment, bytes | None], user: str = "u1") -> MemoryAttachmentReader:
    return MemoryAttachmentReader({user: {a.source_id: (a, data) for a, data in items}})
