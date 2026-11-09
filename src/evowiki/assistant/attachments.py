"""Chat attachments in an assistant turn (materials phase 2, T6): what of each attached material
the model gets, decided in code, never by the model.

Placement: an attachment stays in the user message it was sent with and is sent again, with the
same parts, in every later turn (`plan` is deterministic over the same inputs, so the history's
attachment blocks stay byte-identical for the prompt cache). An attachment of a turn folded into
the compaction summary is only named in the statement (read it with read_source); one of a turn
that never answered (not in the history, so it has no message of its own) rides with the next
answered message, or the current one when none answered since (`Placement.carried`). It belongs to
the message it was sent with: wherever it rides it is context the model may use, never the current
message's own, so it never decides routing (needs_eyes) and a picture of it no model could get is
stated as not read, not as "seen then" — a later message about something else is answered.

What the model gets:
- documents: the whole text (`full`) while every full attachment fits the turn's budget
  (`budget`); over it, the oldest turns' attachments, largest first within a turn, become a table
  of contents with each section's first line (`toc`; the model reads on with read_source). A page
  with a picture (`SourceRow.image_pages`) is left out, down to its first line: its text looks
  complete and is not, and a model that has the text answers from it without calling any tool
  (live 2026-11-04). With nothing to answer from it calls read_source, which looks at the page.
- pictures (image attachments and PDF figure pages, `PageRead.figure`): with a model that can see
  (`sees`), the picture itself, at most MAX_FIGURE_PAGES per message (each message's own pictures,
  so an older message's parts never change when new pictures arrive); without one, the local OCR
  text of a picture that is not a figure. Only the CURRENT message's pictures decide routing: one
  the model cannot get either way makes the plan `needs_eyes` (the caller sends the turn to the
  Claude gateway `eyes` and plans again with `sees=True` over `current_only`: that request carries
  only the current message's attachments, earlier ones are named; or fails the turn with
  `needs_vision`). An older picture the model cannot get is only named ("seen in that turn, see
  the answer then"): it never sends a later turn to `eyes`.
- budget: each picture given counts IMAGE_TOKENS, an image given as its OCR text that text; the
  documents take what is left of `budget` (never below 0: then every document is a toc). Over
  `limit` (80% of the window less the history) older messages give way, earliest first: their
  pictures become OCR text or "seen then", their documents tocs. Only when the current
  message's own attachments are over it does the turn fail with ContextOverBudget; nothing over
  it is sent. Pictures over a message's cap were never given, so they are stated as not read,
  never as "seen then".
- every turn's user message starts with `statement`, one line per attachment telling the model what
  it got (the user never sees it): numbered in upload order (#1, #2 …) with its kind, the message
  it came with (「随第 N 条消息」, `Order.origin`) and 「最近的」 on the one most recently attached or
  mentioned by title (`Order.recent`, `order`), so 「这份 / 刚才那份」 can be resolved.

Material text is untrusted data: every text part is fenced (`sources.config.fence`). Image bytes are
loaded here when the request is built and never logged: the session log holds attachment ids.

A document may come as a `Digest` (its sizes, without sections or page texts: a durable turn's
recorded load, assistant/durable.py); the plan then decides from the sizes and reads only the text
it gives (`AttachmentReader.texts`: the whole text of a `full` one, the contents of a `toc` one),
and fails closed (AttachmentGone) when that text no longer has the recorded size."""

import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol

from psycopg_pool import ConnectionPool

from evowiki.agent.source_tools import IMAGE_LATER, without_image_pages
from evowiki.assistant.contracts import AttachmentOut, AttachmentRead
from evowiki.goals.context import ContextOverBudget, estimate_tokens
from evowiki.llm.chat import DocPart, Part
from evowiki.sources import events, store
from evowiki.sources.config import PAGE_MARK_RE, fence, full_text_tokens
from evowiki.sources.images import image_part
from evowiki.sources.models import PageRead, SectionRow, SourceRow
from evowiki.sources.reader import page_ranges
from evowiki.sources.titles import prompt_title
from evowiki.store.db import fetch_one
from evowiki.store.locks import user_writer

log = logging.getLogger(__name__)

MAX_FIGURE_PAGES = 10          # pictures (images + figure pages) given to the model per turn
CLEANUP_DAYS = 30              # an ask-only attachment's original is cleaned up after this
WINDOW_SHARE = 0.5             # of the model's context window, for attachments + history
CONTEXT_WINDOWS = {"anthropic": 200_000, "anthropic-think": 200_000,
                   "deepseek": 128_000, "deepseek-nothink": 128_000,
                   "openai": 128_000, "gemini": 128_000, "kimi": 128_000,
                   "glm": 128_000}  # conservative product budget, not a vendor context claim
DEFAULT_WINDOW = 128_000
WINDOW_LIMIT = 0.8             # attachments + history never exceed this share of the window
IMAGE_TOKENS = 1600            # estimated tokens of one picture given to the model
TOC_MAX_TOKENS = 4000          # a table of contents is cut at this size
FIRST_LINE_CHARS = 120
_NOT_CHARS = re.compile(rf"{PAGE_MARK_RE.pattern}|\s")   # not counted in `text_chars`
# the same count in SQL, over one source's sections
_CHARS_SQL = ("select count(*) as n, coalesce(sum(char_length(regexp_replace(text, %s, '', 'g')))"
              " filter (where read_by <> 'none'), 0) as chars from source_section "
              "where source_id = %s")

ONLY_FILES_ASK = ("（用户只放了附件，没有说话）用一两句话说这份附件是什么、讲了什么，"
                  "再用一句话问他想拿它做什么。")
NEEDS_VISION = "这张图片现在看不了"
READING_DETAIL = "附件还在读，读完再发"
FAILED_DETAIL = "附件没有读出来，去掉它再发"
GONE_DETAIL = "附件已经删除了，去掉它再发"

STATEMENT_HEAD = ("本会话的附件（引用时写 〔资料名 p.N：「原文片段」〕；按上传顺序编号；每份附件的内容在它被发来的那条消息里；"
                  "这一段是给你的说明）：")
KIND_LABELS = {"pdf": "PDF", "pptx": "PPT", "docx": "Word 文档", "xlsx": "表格", "image": "图片",
               "md": "Markdown", "text": "文本", "url": "网页"}
RECENT = "最近的"
MENTION_MIN_CHARS = 2          # a shorter title is never taken as mentioned
IMAGE_TEXT_NOTE = "（图片里识别出的文字）"
TOC_NOTE = "（只给了目录和每节第一行；要看某一节用 read_source(source_id, section_id)）"
UNREADABLE_SECTION = "（这一节读不出字）"


# ---- errors ----

class AttachmentGone(LookupError):
    """The attachment is not this user's, unknown, or deleted (HTTP 409 at send, GONE_DETAIL)."""


class AttachmentUnusable(RuntimeError):
    """A new attachment that cannot be sent yet (HTTP 409); `detail` is the user-facing text."""

    detail = ""


class AttachmentReading(AttachmentUnusable):
    detail = READING_DETAIL


class AttachmentFailed(AttachmentUnusable):
    detail = FAILED_DETAIL


class NeedsVision(RuntimeError):
    """The turn has a picture no available model can look at (ErrorEv code needs_vision)."""


# ---- what is read from the store ----

TextMode = Literal["full", "toc"]


@dataclass(frozen=True)
class Digest:
    """What the plan needs of a document without its text: the section count and the estimated
    tokens of its whole text and of its table of contents (`full_text` / `toc_text`)."""

    sections: int
    full_tokens: int
    toc_tokens: int


@dataclass(frozen=True)
class Attachment:
    """One attached material as read when the turn (or the session view) is built. `sections`
    and `reads` are empty when loaded without content or when deleted."""

    source: SourceRow
    uses: tuple[str, ...] = ()
    linked: bool = False                      # a node links to one of its sections
    sections: tuple[SectionRow, ...] = ()
    reads: Mapping[int, PageRead] = field(default_factory=dict)
    chars: int | None = None                  # characters of its text (`text_chars`)
    digest: Digest | None = None              # set: `sections` are left out, page reads carry
                                              # no text (`digested`)
    last_used: datetime | None = None         # its newest use (the cleanup counts from it)

    @property
    def source_id(self) -> str:
        return self.source.source_id

    @property
    def deleted(self) -> bool:
        return self.source.deleted_at is not None

    @property
    def section_count(self) -> int:
        return self.digest.sections if self.digest is not None else len(self.sections)


class AttachmentReader(Protocol):
    def load(self, user_id: str, ids: Sequence[str], *,
             content: bool = True) -> list[Attachment]:
        """The user's materials in `ids` order (deleted ones included, flagged). An id that is
        not this user's → AttachmentGone. `content`: also the sections and page reads."""
        ...

    def file(self, user_id: str, source_id: str, *, sha256: str | None = None) -> bytes:
        """The original file; AttachmentGone when it is not there (deleted / cleaned up).
        `sha256`: the hash the caller's loaded row has (a durable reader checks it)."""
        ...

    def texts(self, user_id: str, wanted: Mapping[str, TextMode]) -> dict[str, str]:
        """source_id -> its whole text (`full_text`) or its contents (`toc_text`), unfenced, for
        the plan of an attachment loaded as a `Digest`."""
        ...

    def asked(self, user_id: str, session_id: str, ids: Sequence[str], now: datetime) -> None:
        """A message of `session_id` sends these attachments: each gets an `ask` use (so a file
        attached again is young again for the cleanup). Deleted meanwhile → AttachmentGone.
        Called by the service at send, never inside a durable turn."""
        ...


def texts_of(atts: Sequence[Attachment], wanted: Mapping[str, TextMode]) -> dict[str, str]:
    """`AttachmentReader.texts` over attachments loaded with content."""
    by = {a.source_id: a for a in atts}
    return {sid: full_text(by[sid]) if mode == "full" else toc_text(by[sid])
            for sid, mode in wanted.items()}


def digested(a: Attachment) -> Attachment:
    """A document loaded with content, as its `Digest`: no sections, page reads without text
    (only figure flags are planned from; an image's OCR text is kept). Others unchanged."""
    if a.deleted or a.source.kind == "image" or a.digest is not None:
        return a
    d = Digest(len(a.sections), estimate_tokens(full_text(a)), estimate_tokens(toc_text(a)))
    reads = {p: replace(r, text=None) for p, r in a.reads.items()}
    return replace(a, sections=(), reads=reads, digest=d)


def ensure_usable(atts: Sequence[Attachment]) -> None:
    """New attachments must be readable now: deleted → AttachmentGone, reading → AttachmentReading,
    failed → AttachmentFailed."""
    for a in atts:
        if a.deleted:
            raise AttachmentGone(f"attachment {a.source_id} was deleted")
    for a in atts:
        if a.source.status == "reading":
            raise AttachmentReading(f"attachment {a.source_id} is still being read")
        if a.source.status == "failed":
            raise AttachmentFailed(f"attachment {a.source_id} could not be read")


def text_chars(sections: Sequence[SectionRow]) -> int | None:
    """Characters of a material's text (page marks and white space left out; sections that
    could not be read count 0); None without sections (still reading, an image)."""
    if not sections:
        return None
    return sum(len(_NOT_CHARS.sub("", s.text)) for s in sections if s.read_by != "none")


def attachment_out(a: Attachment, read: AttachmentRead | None = None) -> AttachmentOut:
    s = a.source
    # the cleanup's rule (`sources.store.stale_ask_only`): only ever asked with, nothing links
    # to it, 30 days after its newest use (a file attached again is young again); without a
    # recorded newest use (step values from before it) the upload's time
    keeps = a.linked or not a.uses or any(u != "ask" for u in a.uses)
    since = a.last_used or s.created_at
    expires = None if a.deleted or keeps else since + timedelta(days=CLEANUP_DAYS)
    importing = (not a.deleted and s.status != "failed" and bool(s.progress)
                 and "end" not in s.progress)
    return AttachmentOut(source_id=s.source_id, title=s.title, kind=s.kind, status=s.status,
                         deleted=a.deleted, uses=a.uses, bytes=s.bytes, pages=s.pages,
                         expires_at=expires, unreadable_pages=s.unreadable_pages,
                         chars=None if a.deleted or s.kind == "image" else a.chars, read=read,
                         importing=importing)


class PgAttachmentReader:
    """`AttachmentReader` over the source tables (read-only); one pool connection per call."""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def load(self, user_id: str, ids: Sequence[str], *,
             content: bool = True) -> list[Attachment]:
        out: list[Attachment] = []
        with self._pool.connection() as conn:
            for sid in ids:
                src = store.get_source(conn, user_id, sid)
                if src is None:
                    raise AttachmentGone(f"attachment {sid} not found for user {user_id}")
                rows = store.uses_of(conn, user_id, sid)
                uses = tuple(dict.fromkeys(r["use"] for r in rows))
                last = max((r["created_at"] for r in rows), default=None)
                linked = fetch_one(
                    conn, "select 1 as x from source_link l join source_section t using "
                          "(section_id) where t.source_id = %s and l.user_id = %s limit 1",
                    (sid, user_id)) is not None
                if src.deleted_at is not None:
                    out.append(Attachment(src, uses, linked, last_used=last))
                    continue
                if not content:
                    got = fetch_one(conn, _CHARS_SQL, (_NOT_CHARS.pattern, sid))
                    chars = int(got["chars"]) if got is not None and got["n"] else None
                    out.append(Attachment(src, uses, linked, chars=chars, last_used=last))
                    continue
                secs = tuple(store.sections(conn, user_id, sid))
                out.append(Attachment(src, uses, linked, secs,
                                      store.page_reads(conn, user_id, sid),
                                      chars=text_chars(secs), last_used=last))
        return out

    def file(self, user_id: str, source_id: str, *, sha256: str | None = None) -> bytes:
        with self._pool.connection() as conn:
            got = store.get_file(conn, user_id, source_id)
        if got is None:
            raise AttachmentGone(f"the original of attachment {source_id} is gone")
        return got[0]

    def texts(self, user_id: str, wanted: Mapping[str, TextMode]) -> dict[str, str]:
        return texts_of(self.load(user_id, list(wanted)), wanted)

    def asked(self, user_id: str, session_id: str, ids: Sequence[str], now: datetime) -> None:
        try:
            with user_writer(self._pool, user_id) as tx:
                for sid in ids:
                    events.source_added(tx.conn, user_id, sid, "ask", now,
                                        session_id=session_id)
        except AttachmentGone:
            raise
        except LookupError as e:          # deleted (or cleaned up) since it was checked
            raise AttachmentGone(str(e)) from e


class MemoryAttachmentReader:
    """In-memory `AttachmentReader` for tests: {user_id: {source_id: (Attachment, file bytes)}}."""

    def __init__(self, data: Mapping[str, Mapping[str, tuple[Attachment, bytes | None]]]):
        self._data = data
        self.files_read: list[str] = []
        self.asks: list[tuple[str, str, str]] = []      # (user_id, session_id, source_id)

    def load(self, user_id: str, ids: Sequence[str], *,
             content: bool = True) -> list[Attachment]:
        mine = self._data.get(user_id, {})
        out: list[Attachment] = []
        for sid in ids:
            if sid not in mine:
                raise AttachmentGone(f"attachment {sid} not found for user {user_id}")
            a = mine[sid][0]
            chars = a.chars if a.chars is not None else text_chars(a.sections)
            if a.deleted:
                a = Attachment(a.source, a.uses, a.linked, last_used=a.last_used)
            elif not content:
                a = Attachment(a.source, a.uses, a.linked, chars=chars, last_used=a.last_used)
            else:
                a = replace(a, chars=chars)
            out.append(a)
        return out

    def file(self, user_id: str, source_id: str, *, sha256: str | None = None) -> bytes:
        got = self._data.get(user_id, {}).get(source_id)
        if got is None or got[1] is None or got[0].deleted:
            raise AttachmentGone(f"the original of attachment {source_id} is gone")
        self.files_read.append(source_id)
        return got[1]

    def texts(self, user_id: str, wanted: Mapping[str, TextMode]) -> dict[str, str]:
        return texts_of(self.load(user_id, list(wanted)), wanted)

    def asked(self, user_id: str, session_id: str, ids: Sequence[str], now: datetime) -> None:
        for sid in ids:
            got = self._data.get(user_id, {}).get(sid)
            if got is None or got[0].deleted:
                raise AttachmentGone(f"attachment {sid} not found for user {user_id}")
            self.asks.append((user_id, session_id, sid))


# ---- budget ----

def window(provider: str) -> int:
    """The context window of the turn's model, by provider; EVOWIKI_CONTEXT_WINDOW overrides."""
    env = os.environ.get("EVOWIKI_CONTEXT_WINDOW")
    if env:
        return int(env)
    return CONTEXT_WINDOWS.get(provider, DEFAULT_WINDOW)


def budget(provider: str, history: int) -> int:
    """Tokens the attachments may take as full text this turn: the materials threshold, and at
    most half the window less the history (a long history makes attachments give way); never
    below 0."""
    return max(0, min(full_text_tokens(), int(window(provider) * WINDOW_SHARE) - history))


def limit(provider: str, history: int) -> int:
    """The hard bound for the attachments as sent (tocs and pictures included)."""
    return int(window(provider) * WINDOW_LIMIT) - history


# ---- texts ----

def _first_line(text: str) -> str:
    for line in PAGE_MARK_RE.sub("\n", text).splitlines():
        if line.strip():
            return line.strip()[:FIRST_LINE_CHARS]
    return ""


def _texted(a: Attachment) -> None:
    if a.digest is not None:
        raise ValueError(f"attachment {a.source_id} was loaded as a digest: read its text with "
                         "AttachmentReader.texts")


def _image_only(a: Attachment, s: SectionRow) -> str | None:
    """What stands for a section with no text whose pages have a picture: a turn can read those,
    so it is no unreadable section. None for any other section."""
    if s.read_by != "none" or s.page_from is None:
        return None
    here = [p for p in range(s.page_from, (s.page_to or s.page_from) + 1)
            if p in a.source.image_pages]
    return IMAGE_LATER.format(pages=page_ranges(here)) if here else None


def full_text(a: Attachment) -> str:
    """The material's whole text, section by section (unfenced)."""
    _texted(a)
    blocks: list[str] = []
    for s in a.sections:
        head = f"{'#' * min(s.depth + 1, 6)} {s.title}"
        body = (_image_only(a, s) or UNREADABLE_SECTION if s.read_by == "none"
                else without_image_pages(s.text.strip(), a.source.image_pages))
        blocks.append(f"{head}\n{body}" if body else head)
    return "\n\n".join(blocks)


def toc_text(a: Attachment) -> str:
    """The table of contents: per section its id, title, pages and summary or first line
    (unfenced), cut at TOC_MAX_TOKENS."""
    _texted(a)
    lines: list[str] = []
    used = 0
    for s in a.sections:
        pages = ""
        if s.page_from is not None:
            to = s.page_to if s.page_to is not None else s.page_from
            pages = f"（p.{s.page_from}）" if to == s.page_from else f"（p.{s.page_from}–{to}）"
        first = (_image_only(a, s) or UNREADABLE_SECTION if s.read_by == "none"
                 else s.summary or _first_line(s.text))
        line = f"{'  ' * s.depth}§{s.section_id} {s.title}{pages}"
        line = f"{line}：{first}" if first else line
        used += estimate_tokens(line) + 1
        if used > TOC_MAX_TOKENS:
            lines.append(f"（目录太长，后面还有 {len(a.sections) - len(lines)} 节没列出；"
                         "用 read_source 按页读）")
            break
        lines.append(line)
    return "\n".join(lines)


def _ocr_text(a: Attachment) -> str | None:
    """The OCR text of an image attachment that is not a figure, else None."""
    texts = [r.text.strip() for _, r in sorted(a.reads.items())
             if r.text and r.text.strip() and not r.figure]
    if not texts or any(r.figure for r in a.reads.values()):
        return None
    return "\n\n".join(texts)


def _tokens_label(n: int) -> str:
    return f"约 {round(n / 1000)}k token" if n >= 1000 else f"约 {n} token"


# ---- the plan ----

Mode = Literal["full", "toc", "image", "text_only", "unseen"]   # AttachmentRead.mode

@dataclass(frozen=True)
class Placement:
    """Where each attachment of the session goes: (message_id, source ids) of every history turn
    and the current one, in order; `folded`: attachments of turns folded into the summary;
    `away`: attachments of history turns whose messages this request leaves out (`current_only`).
    Folded and away attachments are only named in the statement. A message may have two entries
    in `turns` (same message_id): first the `carried` attachments riding with it, then its own;
    the last entry is the current message's own attachments."""

    turns: tuple[tuple[str, tuple[str, ...]], ...]
    folded: tuple[str, ...] = ()
    away: tuple[str, ...] = ()
    carried: frozenset[str] = frozenset()   # sent with a turn that never answered (module doc)


@dataclass(frozen=True)
class Order:
    """Where each attachment of the session came from: `origin` = source id -> the 1-based number
    of the session's user message it was first sent with; `recent`: the one most recently attached
    or mentioned (None: unknown)."""

    origin: Mapping[str, int] = field(default_factory=dict)
    recent: str | None = None


def order(messages: Sequence[tuple[Sequence[str], str]],
          titles: Mapping[str, str]) -> Order:
    """`messages`: (attachments, text) of every user message of the session in order, the current
    one last; `titles`: source id -> title of the attachments that may be called recent (not
    deleted). Per message, a title its text names (case-insensitive, at least MENTION_MIN_CHARS) is
    a mention of an attachment sent by then; the message's own attachments come after its
    mentions. Pure over its inputs, so a replay gives the same order."""
    origin: dict[str, int] = {}
    recent: str | None = None
    for n, (ids, text) in enumerate(messages, 1):
        for sid in ids:
            origin.setdefault(sid, n)
        low = text.lower()
        for sid in origin:
            t = titles.get(sid, "").strip().lower()
            if len(t) >= MENTION_MIN_CHARS and t in low:
                recent = sid
        for sid in ids:
            if sid in titles:
                recent = sid
    return Order(origin=origin, recent=recent)


def current_only(placement: Placement) -> Placement:
    """The placement of a request that carries only the current message's attachments (the eyes
    hand-off, assistant.loop): every earlier turn's attachments are `away`."""
    earlier = tuple(i for _, ids in placement.turns[:-1] for i in ids)
    return Placement(turns=placement.turns[-1:], folded=placement.folded,
                     away=(*placement.away, *earlier), carried=placement.carried)


@dataclass(frozen=True)
class _Pick:
    """Decisions for one placed attachment."""

    mode: Mode
    image: bool = False                 # the attachment itself as an ImagePart
    figure_pages: tuple[int, ...] = ()  # PDF pages given as page images
    unseen_pages: tuple[int, ...] = ()
    tokens: int | None = None
    earlier: bool = False               # an older message's image the model cannot get now
    earlier_pages: tuple[int, ...] = ()  # an older message's figure pages it cannot get now


@dataclass(frozen=True)
class Plan:
    parts: dict[str, tuple[Part, ...]]        # message_id -> its attachment parts
    statement: str                            # the 「本会话的附件」 block ("" without any)
    reads: dict[str, AttachmentRead]          # source_id -> how it was given
    needs_eyes: bool                          # a picture the model cannot get (sees=False)


def place(turns: Sequence[tuple[str, Sequence[str], bool]], current: tuple[str, Sequence[str]],
          folded: Sequence[str]) -> Placement:
    """`turns`: (message_id, attachments, in history) of every kept turn in order. An attachment
    is placed at its first answered turn (the current message counts as answered); one only ever
    sent with unanswered turns rides (carried) with the next answered turn, or the current message
    when none answered since, ahead of that message's own; one already in the summary's turns is
    folded unless sent again later. Sending a carried one again makes it that message's own."""
    sent = {i for _, ids, answered in turns if answered for i in ids} | set(current[1])
    seen: set[str] = set()
    placed: list[tuple[str, tuple[str, ...]]] = []
    stray: list[str] = []

    def put(mid: str, ids: Sequence[str], *, keep_empty: bool = False) -> None:
        mine = tuple(i for i in dict.fromkeys(ids) if i not in seen)
        seen.update(mine)
        if mine or keep_empty:
            placed.append((mid, mine))

    for mid, ids, answered in turns:
        if not answered:
            stray.extend(i for i in ids if i not in sent)
            continue
        put(mid, stray)
        stray = []
        put(mid, ids)
    put(current[0], stray)
    put(current[0], current[1], keep_empty=True)
    carried = frozenset(i for _, ids, answered in turns if not answered for i in ids
                        if i not in sent)
    return Placement(turns=tuple(placed),
                     folded=tuple(i for i in dict.fromkeys(folded) if i not in seen),
                     carried=carried)


@dataclass
class _State:
    """One placed attachment while deciding (mutable; becomes a `_Pick`)."""

    image: bool = False                  # the image itself is given
    shown: tuple[int, ...] = ()          # figure pages given
    hidden: tuple[int, ...] = ()         # figure pages not given
    capped: tuple[int, ...] = ()         # figure pages over its message's cap: never given
    earlier: bool = False                # an older picture not given now: "seen then"
    toc: bool = False


def _decide(placement: Placement, atts: Mapping[str, Attachment], *, sees: bool,
            budget_tokens: int, limit_tokens: int) -> tuple[dict[str, _Pick], bool]:
    """The picks and whether the current message needs eyes. Over `limit_tokens`, older messages
    give way first, earliest first (pictures → their OCR text or "seen then", full documents →
    toc); when the current message's own attachments are still over it → ContextOverBudget
    (not checked while the current message needs eyes: the caller re-plans or fails)."""
    order = [sid for _, ids in placement.turns for sid in ids]
    live = [sid for sid in order if not atts[sid].deleted]
    docs = [sid for sid in live if atts[sid].source.kind != "image"]
    sizes = {sid: _sizes(atts[sid])[0] for sid in docs}
    tocs = {sid: _sizes(atts[sid])[1] for sid in docs}
    st: dict[str, _State] = {sid: _State() for sid in live}
    needs_eyes = False
    last = len(placement.turns) - 1

    # pictures: each message's own cap, so older messages keep their parts
    for ti, (_, ids) in enumerate(placement.turns):
        current = ti == last
        left = MAX_FIGURE_PAGES
        for sid in ids:
            if sid not in st:
                continue
            a, x = atts[sid], st[sid]
            # seen (by eyes) in its own turn; a carried one's turn never answered: never seen
            x.earlier = not sees and not current and sid not in placement.carried
            # the cap is counted as if the model sees (as the eyes turn of that message did), so
            # what was over it is never said to have been seen then
            if a.source.kind == "image":
                fits = left > 0
                left -= int(fits)
                x.image = sees and fits
                x.earlier = x.earlier and fits
                if current and not sees and _ocr_text(a) is None:
                    needs_eyes = True
                continue
            figs = sorted(p for p, r in a.reads.items() if r.figure)
            if figs and current and not sees:
                needs_eyes = True
            allowed = tuple(figs[:max(left, 0)])
            left -= len(allowed)
            x.shown = allowed if sees else ()
            x.capped = tuple(figs[len(allowed):])
            x.hidden = tuple(p for p in figs if p not in x.shown)

    # an image not given as itself goes as its OCR text, when it has some
    ocr = {sid: estimate_tokens(f"{IMAGE_TEXT_NOTE}\n{fence(t)}") for sid in live
           if atts[sid].source.kind == "image" and (t := _ocr_text(atts[sid])) is not None}

    def total() -> int:
        pictures = sum(int(x.image) + len(x.shown) for x in st.values())
        return (pictures * IMAGE_TOKENS
                + sum(n for sid, n in ocr.items() if not st[sid].image)
                + sum(tocs[sid] if st[sid].toc else sizes[sid] for sid in docs))

    # documents: full while they fit the budget, else the oldest turns' largest become toc
    for _, ids in placement.turns:
        for sid in sorted((i for i in ids if i in sizes), key=lambda i: -sizes[i]):
            if total() <= budget_tokens:
                break
            st[sid].toc = True

    # over the hard limit: older messages give way, earliest first
    if not needs_eyes:
        for _, ids in placement.turns[:-1]:
            if total() <= limit_tokens:
                break
            for sid in ids:
                if sid not in st:
                    continue
                x = st[sid]
                if x.image or x.shown:
                    x.earlier = sid not in placement.carried
                    x.hidden = tuple(sorted({*x.hidden, *x.shown}))
                    x.image, x.shown = False, ()
                if sid in sizes:
                    x.toc = True
        if total() > limit_tokens:
            raise ContextOverBudget(f"this message's attachments need ~{total()} tokens, "
                                    f"over the limit of {limit_tokens}")

    picks: dict[str, _Pick] = {}
    for sid in order:
        a = atts[sid]
        if a.deleted:
            picks[sid] = _Pick(mode="unseen")
            continue
        x = st[sid]
        if a.source.kind == "image":
            mode: Mode = ("image" if x.image else "text_only" if _ocr_text(a) is not None
                          else "unseen")
            picks[sid] = _Pick(mode=mode, image=x.image, earlier=x.earlier and mode == "unseen")
            continue
        unreadable = (set(a.source.unreadable_pages) | set(x.hidden)) - set(x.shown)
        picks[sid] = _Pick(mode="toc" if x.toc else "full", figure_pages=x.shown,
                           unseen_pages=tuple(sorted(unreadable)), tokens=sizes[sid],
                           earlier_pages=tuple(p for p in x.hidden if p not in x.capped)
                           if x.earlier else ())
    return picks, needs_eyes


def _read(a: Attachment, pick: _Pick) -> AttachmentRead:
    total = a.source.pages
    unseen = tuple(sorted({*pick.unseen_pages, *a.source.image_pages}))
    read = None if total is None else total - len([p for p in unseen if p <= total])
    secs = a.section_count if a.source.kind != "image" else None
    return AttachmentRead(mode=pick.mode, pages_total=total, pages_read=read, sections=secs,
                          tokens=pick.tokens, unseen_pages=unseen)


def _tag(a: Attachment, n: int, ordered: Order) -> str:
    """「#2（随第 3 条消息，PDF，最近的）」: the number, where it came from, kind, the recent mark."""
    got = ordered.origin.get(a.source_id)
    bits = [f"随第 {got} 条消息"] if got is not None else []
    bits.append(KIND_LABELS.get(a.source.kind, a.source.kind))
    if ordered.recent == a.source_id:
        bits.append(RECENT)
    return f"#{n}（{'，'.join(bits)}）"


def _text_first_note(a: Attachment) -> str:
    """What a text-first PDF's text is, and what to do about it (`sources.importer`, module doc).
    Tool results do not stay in the history: without the pages read so far, the next turn sees only
    their opening words and takes back what it read (live 2026-11-04)."""
    s = a.source
    rest = [p for p in s.imprecise_pages if p not in s.image_pages]
    worst = f"，第 {page_ranges(rest)} 页尤其" if rest else ""
    return (f"这是 PDF 文字层，公式、表格可能是碎的或缺失{worst}；"
            "问到这些先用 read_source(precise=true) 读相关页，读不到就直说，不要凭记忆或碎文字补")


def _image_note(a: Attachment) -> str:
    return (f"第 {page_ranges(a.source.image_pages)} 页有图片或绘制的公式，这些页只给了开头。"
            "他问的内容可能在这些页上（公式、定义、图、表、某页写了什么），就先用 read_source 读相关页再答，"
            f"不要先按通识答；没读就不能说它写了什么或没写什么{_read_before(a)}")


def _read_before(a: Attachment) -> str:
    seen = [p for p, r in a.reads.items() if r.by == "claude" and p in a.source.image_pages]
    if not seen:
        return ""
    return f"；第 {page_ranges(sorted(seen))} 页已有读取结果，调 read_source 立刻返回；之前答过的是读了才说的，不用撤回，也不用提读过几次"


def _line(a: Attachment, pick: _Pick | None, *, away: bool = False,
          unanswered: bool = False) -> str:
    """`pick` None: a folded attachment, or with `away` one of a message this request leaves out."""
    s = a.source
    head = [prompt_title(s.title), f"source_id={s.source_id}"]
    if a.deleted:
        return f"【{' · '.join(head)} · 已删除或已清理，内容不在了】"
    if pick is None and away and unanswered:
        return (f"【{' · '.join(head)} · 随之前一条没有回答的消息发来，这一轮没有带上；"
                "文档要看内容用 read_source，图片没有看到过】")
    if pick is None and away:
        return (f"【{' · '.join(head)} · 在之前的消息里给过，这一轮没有带上；"
                "文档要看内容用 read_source，图片见当时的回答】")
    if pick is None:
        return (f"【{' · '.join(head)} · 在更早（已摘要）的对话里给过，现在不在上下文里；"
                "要看内容用 read_source】")
    if s.kind == "image":
        if pick.earlier:
            return f"【{' · '.join([*head, '图片', '之前那一轮已经看过，见当时的回答'])}】"
        what = {"image": "已给出原图", "text_only": "只有识别出的文字",
                "unseen": "这一轮没给出原图，也没有识别出的文字"}[pick.mode]
        return f"【{' · '.join([*head, '图片', what])}】"
    if s.pages is not None:
        head.append(f"{s.pages} 页")
    if pick.tokens is not None:
        head.append(_tokens_label(pick.tokens))
    if s.text_first:
        head.append(_text_first_note(a))
    if s.image_pages:
        head.append(_image_note(a))
    if pick.mode == "full":
        head.append("其余页已给出" if s.image_pages else "整份已给出")
    else:
        head.append(f"只给了目录（{a.section_count} 节）和每节第一行；要看内容用 read_source")
    if pick.figure_pages:
        head.append(f"第 {page_ranges(pick.figure_pages)} 页是图，已给出页面图片")
    if pick.earlier_pages:
        head.append(f"第 {page_ranges(pick.earlier_pages)} 页是图，之前那一轮已经看过，见当时的回答")
    if missing := [p for p in pick.unseen_pages if p not in pick.earlier_pages]:
        head.append(f"第 {page_ranges(missing)} 页没有读到内容")
    return f"【{' · '.join(head)}】"


def plan(placement: Placement, atts: Mapping[str, Attachment], *, sees: bool,
         budget_tokens: int, limit_tokens: int, user_id: str, reader: AttachmentReader,
         render: Callable[[bytes, list[int]], list[bytes]], ordered: Order | None = None) -> Plan:
    """The turn's attachment parts per message, its statement and reads. With `needs_eyes` the
    parts are not built (the caller re-plans with `sees=True` or fails); no file is read then.
    Over `limit_tokens` older messages give way first; ContextOverBudget only when the current
    message's own attachments do not fit.
    `render(pdf bytes, pages)` -> PNG bytes per page (sources.parse_pdf.render_pages).
    `ordered`: the statement lists the attachments in upload order (by `Order.origin`; without
    it, in placement order) and marks the recent one."""
    picks, needs_eyes = _decide(placement, atts, sees=sees, budget_tokens=budget_tokens,
                                limit_tokens=limit_tokens)
    ordered = ordered or Order()
    rows = [(sid, _line(atts[sid], picks[sid])) for _, ids in placement.turns for sid in ids]
    rows += [(sid, _line(atts[sid], None, away=True, unanswered=sid in placement.carried))
             for sid in placement.away]
    rows += [(sid, _line(atts[sid], None)) for sid in placement.folded]
    last = len(rows)
    rows = [r for _, r in sorted(enumerate(rows), key=lambda x: (
        ordered.origin.get(x[1][0], last + 1), x[0]))]
    lines = [f"{_tag(atts[sid], n, ordered)}{line}" for n, (sid, line) in enumerate(rows, 1)]
    statement = f"{STATEMENT_HEAD}\n" + "\n".join(lines) if lines else ""
    reads = {sid: _read(atts[sid], p) for sid, p in picks.items() if not atts[sid].deleted}
    if needs_eyes:
        return Plan(parts={}, statement=statement, reads=reads, needs_eyes=True)
    texts = _texts(placement, atts, picks, user_id, reader)
    parts: dict[str, tuple[Part, ...]] = {}
    for mid, ids in placement.turns:
        out: list[Part] = []
        for sid in ids:
            out.extend(_parts(atts[sid], picks[sid], texts.get(sid), user_id, reader, render))
        if out:   # a message's carried attachments, then its own (same message_id)
            parts[mid] = (*parts.get(mid, ()), *out)
    return Plan(parts=parts, statement=statement, reads=reads, needs_eyes=False)


def _sizes(a: Attachment) -> tuple[int, int]:
    """Estimated tokens of a document's whole text and of its contents."""
    if a.digest is not None:
        return a.digest.full_tokens, a.digest.toc_tokens
    return estimate_tokens(full_text(a)), estimate_tokens(toc_text(a))


def _texts(placement: Placement, atts: Mapping[str, Attachment], picks: Mapping[str, _Pick],
           user_id: str, reader: AttachmentReader) -> dict[str, str]:
    """The text each placed document gives (unfenced): its whole text or its contents. A digest's
    is read in one `reader.texts` call and must still have the size it was planned with."""
    wanted: dict[str, TextMode] = {}
    for _, ids in placement.turns:
        for sid in ids:
            a = atts[sid]
            if not a.deleted and a.source.kind != "image":
                wanted[sid] = "full" if picks[sid].mode == "full" else "toc"
    local = {sid: mode for sid, mode in wanted.items() if atts[sid].digest is None}
    out = texts_of([atts[sid] for sid in local], local)
    remote = {sid: mode for sid, mode in wanted.items() if sid not in local}
    if remote:
        got = reader.texts(user_id, remote)
        for sid, mode in remote.items():
            want = _sizes(atts[sid])[0 if mode == "full" else 1]
            if sid not in got or estimate_tokens(got[sid]) != want:
                raise AttachmentGone(f"attachment {sid} changed since this turn read it")
            out[sid] = got[sid]
    return out


def _parts(a: Attachment, pick: _Pick, text: str | None, user_id: str, reader: AttachmentReader,
           render: Callable[[bytes, list[int]], list[bytes]]) -> list[Part]:
    s = a.source
    if a.deleted:
        return []
    if s.kind == "image":
        if pick.image:
            return [image_part(reader.file(user_id, s.source_id, sha256=s.sha256), s.title)]
        ocr = _ocr_text(a)
        if ocr is None:
            return []
        return [DocPart(source_id=s.source_id, title=s.title,
                        text=f"{IMAGE_TEXT_NOTE}\n{fence(ocr)}")]
    if text is None:
        raise ValueError(f"no text was read for attachment {s.source_id}")
    body = fence(text) if pick.mode == "full" else f"{TOC_NOTE}\n{fence(text)}"
    out: list[Part] = [DocPart(source_id=s.source_id, title=s.title, text=body)]
    if pick.figure_pages:
        pngs = render(reader.file(user_id, s.source_id, sha256=s.sha256), list(pick.figure_pages))
        out += [image_part(png, f"{s.title} 第 {p} 页")
                for p, png in zip(pick.figure_pages, pngs, strict=True)]
    return out
