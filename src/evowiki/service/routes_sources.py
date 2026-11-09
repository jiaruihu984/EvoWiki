"""/wiki/sources: upload (file / url / pasted text), list, detail, import progress (SSE), the
original, uses, rename, delete, retry. Every query is scoped to the caller; another user's
source is a 404. User-facing details are Chinese.

Import runs (`sources.importer`): an upload whose only use is `ask` (a chat attachment) is read
in mode "ask"; anything else in mode "full". A retry and the first learning use of an ask-only
source bump the source's run generation and start the next run (`sources.uses.next_run` /
`start_next`, which retries a run that cannot start durably); what earlier runs read or
summarized is reused, not paid again.
Every upload ends with the 30-day sweep of chat attachments nobody kept (Q7)."""

import hashlib
import logging
import mimetypes
from collections.abc import Iterator
from typing import Annotated, get_args
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from evowiki.assistant.tools.fetch_url import Fetcher
from evowiki.service.deps import Services, check_user, current_user, get_services
from evowiki.service.routes_wiki import Events, item_events
from evowiki.service.source_schemas import (
    SectionEditIn,
    SourceDetail,
    SourceOut,
    SourcePatchIn,
    UseIn,
    UseOut,
    source_detail,
    source_out,
)
from evowiki.sources import config, events, store
from evowiki.sources import uses as material_uses
from evowiki.sources.arxiv import arxiv_pdf_url, arxiv_title
from evowiki.sources.cleanup import sweep_ask_attachments
from evowiki.sources.importer import ImportJob, image_pages_of
from evowiki.sources.models import SourceKind, SourceRow, Use
from evowiki.sources.parse import (
    default_fetcher,
    detect_kind,
    fetch_and_parse,
    parse_text,
)
from evowiki.sources.parse_pdf import check_pdf, pdf_title
from evowiki.sources.parsed import ParseError
from evowiki.sources.titles import clean_title
from evowiki.store.locks import user_writer
from evowiki.wikiapp.exam_view import exam_summary

Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]
router = APIRouter(prefix="/wiki")

MSG_NOT_FOUND = "找不到这份资料"
MSG_ONE_INPUT = "文件、网址、文字三选一"
MSG_BAD_FORM = "上传的内容读不了，请重新添加"
MSG_BAD_USE = "不认识这种用途"
MSG_STILL_READING = "资料还在读，读完再改目录"
MSG_NO_SECTION = "目录里没有这一节"
MSG_NO_RETRY = "这份资料不需要重试"
_FORM_OVERHEAD = 1 << 20      # multipart framing and the small fields, on top of the cap
log = logging.getLogger(__name__)
UNTITLED = "未命名资料"
_EXT_BY_MIME = {
    "application/pdf": ".pdf", "text/markdown": ".md", "text/plain": ".txt",
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "text/html": ".html", "application/json": ".json",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx"}
_MIME_BY_KIND = {"pdf": "application/pdf", "md": "text/markdown", "text": "text/plain"}


def get_fetcher(request: Request) -> Fetcher:
    """The URL fetcher: `app.state.source_fetcher` when set (tests), else the guarded real one."""
    return getattr(request.app.state, "source_fetcher", None) or default_fetcher()


def _too_big() -> HTTPException:
    return HTTPException(413, {"code": "upload_too_large", "params": {"limit": config.max_bytes() // 1024 // 1024},
                                      "message": "The file is too large. The limit is {limit} MB."})


def _mime(kind: SourceKind, filename: str) -> str:
    return _MIME_BY_KIND.get(kind) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _live(conn, uid: str, source_id: str) -> SourceRow:
    row = store.get_source(conn, uid, source_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(404, MSG_NOT_FOUND)
    return row


def _stem(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return (base[:dot] if dot > 0 else base).strip() or UNTITLED


@router.post("/sources", response_model=SourceOut, status_code=201)
async def post_source(request: Request, response: Response, svc: Svc, uid: User,
                      fetcher: Annotated[Fetcher, Depends(get_fetcher)]) -> SourceOut:
    """multipart: one of `file` / `url` / `text`; optional title, origin, use, goal_id, page_id,
    session_id. The form is parsed here (not by FastAPI) so pasted text may be as large as the
    cap instead of Starlette's 1 MB field limit."""
    cap = config.max_bytes()
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap + _FORM_OVERHEAD:
        raise _too_big()                        # refuse before the body is read
    try:
        form = await request.form(max_part_size=cap + 1, max_files=1)
    except (MultiPartException, StarletteHTTPException) as e:
        # Starlette turns a parser refusal into HTTPException(400, "... exceeded maximum size")
        detail = str(getattr(e, "detail", e))
        if "exceeded maximum size" in detail:
            raise _too_big() from e
        raise HTTPException(422, MSG_BAD_FORM) from e
    try:
        fields = {k: v for k, v in form.items() if isinstance(v, str)}
        file = next((v for v in form.values() if isinstance(v, UploadFile)), None)
        if file is not None and not file.filename:
            file = None
        use = fields.get("use") or None
        if use is not None and use not in get_args(Use):
            raise HTTPException(422, MSG_BAD_USE)
        row, created = await run_in_threadpool(_import, svc, uid, fetcher, file, fields, use)
    finally:
        await form.close()
    try:                         # Q7: never in the way of the upload itself
        await run_in_threadpool(sweep_ask_attachments, svc.pool, svc.sources, svc.clock())
    except Exception:
        log.exception("the chat attachment cleanup after an upload failed")
    response.status_code = 201 if created else 200
    with svc.pool.connection() as conn:
        return source_out(row, store.uses_of(conn, uid, row.source_id))


def _import(svc: Services, uid: str, fetcher: Fetcher, file: UploadFile | None,
            fields: dict[str, str], use: str | None,
            named_by: str = "given") -> tuple[SourceRow, bool]:
    """`named_by`: what a `title` field counts as, "document" when the assistant chose it."""
    check_user(svc, uid)
    url, text = fields.get("url"), fields.get("text")
    title = (fields.get("title") or "").strip() or None
    origin = fields.get("origin") or None
    if (file is not None) + bool(url) + bool(text) != 1:
        raise HTTPException(422, MSG_ONE_INPUT)
    cap = config.max_bytes()
    try:
        if file is not None:
            data = file.file.read(cap + 1)
            if len(data) > cap:
                raise _too_big()
            filename = file.filename or ""
            try:
                kind = detect_kind(filename, data)
            except ParseError as e:
                raise HTTPException(415, str(e)) from e
            if kind == "pdf":
                check_pdf(data)        # locked or broken: said now, not after a queue of books
            # a PDF the user did not name: its own title (metadata / first bookmark) over the
            # file name (follow-up B: "lecture" / "big" told the model nothing)
            own = pdf_title(data) if kind == "pdf" and title is None else None
            mime, name, src_origin = _mime(kind, filename), title or own or _stem(filename), origin
            title_source = named_by if title else "document" if own else "fallback"
        elif url:
            url = arxiv_pdf_url(url)
            doc, data, mime = fetch_and_parse(url, fetcher)
            own = (title is None and doc.kind == "pdf" and pdf_title(data)) or None
            if title is None and own is None:
                own = arxiv_title(url, fetcher)
            # a fetched PDF is a PDF to the learner too; `origin` says where it came from
            kind = "pdf" if doc.kind == "pdf" else "url"
            name, src_origin = title or own or doc.title, url.strip()
            title_source = (named_by if title
                            else "document" if own or doc.kind != "pdf" else "fallback")
        else:
            data = (text or "").encode("utf-8")
            if len(data) > cap:
                raise _too_big()
            doc = parse_text(text or "", title_hint=title)
            kind, mime, name, src_origin = "text", "text/plain", title or doc.title, origin
            title_source = named_by if title else "document"
    except ParseError as e:
        raise HTTPException(422, str(e)) from e
    sha = hashlib.sha256(data).hexdigest()
    now = svc.clock()
    created = False
    rerun: ImportJob | None = None
    with user_writer(svc.pool, uid) as tx:
        row = store.find_by_sha(tx.conn, uid, sha)
        before = [] if row is None else store.uses_of(tx.conn, uid, row.source_id)
        if row is None:
            row = SourceRow(source_id=svc.new_id("src"), user_id=uid, kind=kind,
                            title=clean_title(name) or UNTITLED, sha256=sha, bytes=len(data),
                            pages=None, status="reading", error=None, unreadable_pages=(),
                            toc_mode=None, progress={}, origin=src_origin, created_at=now,
                            updated_at=now, deleted_at=None, title_source=title_source)
            store.insert_source(tx.conn, row, data, mime)
            created = True
        pictured = image_pages_of(kind, data) if not created and kind in ("pdf", "pptx") else None
        if pictured is not None:                           # the check may be newer than the row
            store.set_text_first(tx.conn, uid, row.source_id, text_first=row.text_first,
                                 imprecise_pages=list(row.imprecise_pages), image_pages=pictured)
        if use is None and not created and material_uses.ask_only(before):
            # a chat attachment added again without a use (参数 → 资料): kept now, like a plain
            # add (listed, matched, imported in full) instead of staying ask-only
            use = "supplement"
        if use is not None:
            events.source_added(tx.conn, uid, row.source_id, use, now,
                                goal_id=fields.get("goal_id") or None,
                                page_id=fields.get("page_id") or None,
                                session_id=fields.get("session_id") or None)
            if not created and material_uses.needs_full_run(row, before, use):
                rerun = material_uses.next_run(tx.conn, uid, row, mode="full")
    if rerun is not None:              # the same file again, now to learn from: read it all
        _start_next(svc, rerun)
    if created:     # retried durably when it cannot start; never a "reading" row with no run
        _start_next(svc, ImportJob(user_id=uid, source_id=row.source_id,
                                   mode="ask" if use == "ask" else "full"))
    with svc.pool.connection() as conn:     # an inline run has progressed already
        row = store.get_source(conn, uid, row.source_id) or row
    return row, created


@router.get("/sources", response_model=list[SourceOut])
def list_sources(svc: Svc, uid: User) -> list[SourceOut]:
    """参数 → 资料: a chat attachment nobody kept (only ever used to ask) stays in its
    conversation (spec §7 C4); it is listed once it has another use."""
    with svc.pool.connection() as conn:
        rows = [(r, store.uses_of(conn, uid, r.source_id)) for r in store.list_sources(conn, uid)]
    return [source_out(r, u) for r, u in rows if not material_uses.ask_only(u)]


@router.get("/sources/{source_id}", response_model=SourceDetail)
def get_source(source_id: str, svc: Svc, uid: User) -> SourceDetail:
    with svc.pool.connection() as conn:
        row = _live(conn, uid, source_id)
        secs = store.sections(conn, uid, source_id)
        uses = store.uses_of(conn, uid, source_id)
        exam = exam_summary(conn, uid, source_id)
    return source_detail(row, uses, secs, exam)


@router.patch("/sources/{source_id}", response_model=SourceOut)
def patch_source(source_id: str, body: SourcePatchIn, svc: Svc, uid: User) -> SourceOut:
    with user_writer(svc.pool, uid) as tx:
        _live(tx.conn, uid, source_id)
        if body.title is not None:
            store.rename(tx.conn, uid, source_id, clean_title(body.title) or UNTITLED)
        if body.origin is not None:
            store.set_origin(tx.conn, uid, source_id, body.origin)
        row = _live(tx.conn, uid, source_id)
        uses = store.uses_of(tx.conn, uid, source_id)
    return source_out(row, uses)


@router.patch("/sources/{source_id}/sections", response_model=SourceDetail)
def patch_sections(source_id: str, body: list[SectionEditIn], svc: Svc,
                   uid: User) -> SourceDetail:
    """Rename / relevel the material's sections (its 目录). Structure only: nothing is re-cut."""
    with user_writer(svc.pool, uid) as tx:
        row = _live(tx.conn, uid, source_id)
        if row.status not in ("ready", "partial"):
            raise HTTPException(409, MSG_STILL_READING)
        try:
            store.edit_sections(tx.conn, uid, source_id,
                                [(e.id, e.title, e.depth) for e in body])
        except KeyError as e:
            raise HTTPException(422, MSG_NO_SECTION) from e
        secs = store.sections(tx.conn, uid, source_id)
        uses = store.uses_of(tx.conn, uid, source_id)
        exam = exam_summary(tx.conn, uid, source_id)
    return source_detail(row, uses, secs, exam)


def _progress_events(source_id: str, svc: Svc, uid: User) -> Iterator[Events]:
    with svc.pool.connection() as conn:
        _live(conn, uid, source_id)
    yield from item_events(svc.sources.follow(uid, source_id))


@router.get("/sources/{source_id}/progress", response_class=EventSourceResponse)
def get_progress(events: Annotated[Events, Depends(_progress_events)]) -> Events:
    """SSE (SOURCE_EVENTS): the source's import run, replayed and followed to its end; 404 for a
    source that is not the caller's (or deleted) before the stream opens."""
    yield from events


@router.get("/sources/{source_id}/file")
def get_file(source_id: str, svc: Svc, uid: User) -> Response:
    with svc.pool.connection() as conn:
        row = _live(conn, uid, source_id)
        got = store.get_file(conn, uid, source_id)
    if got is None:
        raise HTTPException(404, MSG_NOT_FOUND)
    data, mime = got
    bare = mime.split(";")[0].strip().lower()
    ext = _EXT_BY_MIME.get(bare, "")
    name = row.title if not ext or row.title.lower().endswith(ext) else row.title + ext
    # A fetched page is someone else's html: never render it inline from the API origin.
    disposition = "attachment" if "html" in bare or "xml" in bare else "inline"
    return Response(data, media_type=mime, headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''" + quote(name, safe=""),
        "X-Content-Type-Options": "nosniff"})


@router.post("/sources/{source_id}/uses", response_model=UseOut, status_code=201)
def post_use(source_id: str, body: UseIn, svc: Svc, uid: User) -> UseOut:
    """Records the use (`auto`: the material decides; 409 while it has no sections yet); a
    learning use starts a full import (summaries, every page read) when none is done yet: the
    first one of a chat attachment, or one after a full run that could not start or ended with
    an error. See `sources.uses.needs_full_run`."""
    try:
        added = material_uses.apply_use(svc.pool, svc.sources, uid, source_id, body.use,
                                        svc.clock(), goal_id=body.goal_id, page_id=body.page_id,
                                        session_id=body.session_id)
    except (material_uses.StillReading, material_uses.ReadFailed) as e:
        raise HTTPException(409, e.detail) from e
    except material_uses.RunNotStarted as e:
        raise HTTPException(503, e.detail) from e
    except LookupError as e:
        raise HTTPException(404, MSG_NOT_FOUND) from e
    return UseOut(id=added.use_id, created_at=added.created_at, use=added.use,
                  goal_id=body.goal_id, page_id=body.page_id, session_id=body.session_id)


@router.post("/sources/{source_id}/retry", response_model=SourceOut, status_code=202)
def retry_source(source_id: str, svc: Svc, uid: User) -> SourceOut:
    """A failed import, or one that ended with an error after the source became usable (402,
    spending cap, a late crash), runs again as the next generation; anything else is a 409.
    The sections stay unless that run ended at its toc (`sources.uses.keeps_sections`)."""
    with user_writer(svc.pool, uid) as tx:
        row = _live(tx.conn, uid, source_id)
        late = row.status in ("ready", "partial") and row.progress.get("end") == "error"
        if row.status != "failed" and not late:
            raise HTTPException(409, MSG_NO_RETRY)
        ask = material_uses.ask_only(store.uses_of(tx.conn, uid, source_id))
        mode = "ask" if ask else "full"
        job = material_uses.next_run(tx.conn, uid, row, mode=mode)
    _start_next(svc, job)
    return _out(svc, uid, source_id)


def _start_next(svc: Services, job: ImportJob) -> None:
    try:
        material_uses.start_next(svc.pool, svc.sources, job)
    except material_uses.RunNotStarted as e:
        raise HTTPException(503, e.detail) from e


def _out(svc: Services, uid: str, source_id: str) -> SourceOut:
    with svc.pool.connection() as conn:     # an inline run has progressed already
        row = _live(conn, uid, source_id)
        return source_out(row, store.uses_of(conn, uid, source_id))


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: str, svc: Svc, uid: User) -> Response:
    with svc.pool.connection() as conn:
        _live(conn, uid, source_id)
    svc.sources.cancel(uid, source_id)
    with user_writer(svc.pool, uid) as tx:
        if not store.soft_delete(tx.conn, uid, source_id, svc.clock()):
            raise HTTPException(404, MSG_NOT_FOUND)
    return Response(status_code=204)
