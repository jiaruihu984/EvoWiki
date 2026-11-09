import hashlib
import re
from collections.abc import Sequence

from evowiki.content.models import Passage
from evowiki.pipeline.sources import Fetcher, SourceRegistration, require_eligible
from evowiki.pipeline.text import html_to_text

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def passage_id(source_id: str, locator: str, text: str) -> str:
    return "p_" + hashlib.sha256(f"{source_id}|{locator}|{text}".encode()).hexdigest()[:16]


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]
    pieces: list[str] = []
    cur = ""
    for sentence in _SENTENCE_SPLIT.split(paragraph):
        candidate = f"{cur} {sentence}" if cur else sentence
        if cur and len(candidate) > max_chars:
            pieces.append(cur)
            cur = sentence
        else:
            cur = candidate
    if cur:
        pieces.append(cur)
    return pieces


def chunk_document(source_id: str, locator: str, text: str, max_chars: int = 1200,
                   exclude: Sequence[str] = ()) -> list[Passage]:
    unquoted = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", unquoted)
                  if p.strip() and not any(x in p for x in exclude)]
    pieces = [piece for p in paragraphs for piece in _split_long(p, max_chars)]
    chunks: list[list[str]] = []
    cur: list[str] = []
    for piece in pieces:
        too_long = len("\n\n".join([*cur, piece])) > max_chars
        if cur and (piece.startswith("#") or too_long):
            chunks.append(cur)
            cur = []
        cur.append(piece)
    if cur:
        chunks.append(cur)
    chunks = [c for c in chunks if not all(part.startswith("#") for part in c)]
    out = []
    for n, parts in enumerate(chunks):
        body = "\n\n".join(parts)
        loc = f"{locator}#c{n}"
        out.append(Passage(id=passage_id(source_id, loc, body), source_id=source_id,
                           locator=loc, text=body))
    return out


def _looks_like_html(raw: str) -> bool:
    head = raw[:2000].lower()
    return "<" in head and "</" in head


def ingest_registration(reg: SourceRegistration, fetch: Fetcher,
                        max_chars: int = 1200) -> list[Passage]:
    require_eligible(reg.source)
    out: dict[str, Passage] = {}
    for doc in reg.documents:
        raw = fetch(doc.locator)
        text = html_to_text(raw) if _looks_like_html(raw) else raw
        for p in chunk_document(reg.source.id, doc.locator, text, max_chars, doc.exclude):
            out.setdefault(p.id, p)
    return list(out.values())
