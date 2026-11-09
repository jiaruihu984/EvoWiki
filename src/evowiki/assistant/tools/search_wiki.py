"""search_wiki: what THIS user's Wiki says, and which reviewed library segments cover a topic.
Returns numbered snippets with sources (Claude Code `Grep`: hits and where, not whole files —
the model reads a section with read_page if it needs more). User pages are matched by words
(they are not embedded); the library reuses store/vectors.search_segments."""

import re
from collections.abc import Sequence
from typing import Any, Protocol

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import (
    SEARCH_CHARS,
    SNIPPET_CHARS,
    TITLE_CHARS,
    clip,
    run_guarded,
    spec,
)
from evowiki.agent.tools import Source, ToolContext, ToolResult
from evowiki.content.loaded import LoadedContent
from evowiki.goals.contracts import OutlineSection
from evowiki.retrieval.embed import Embedder
from evowiki.schema import PUBLISHABLE
from evowiki.store.vectors import search_segments
from evowiki.wikiapp.page_reader import PageStore, WikiPage

DESCRIPTION = ("在用户自己的 Wiki 页面和已审核的内容库里找相关内容。用户问「我之前学过的…」、"
               "或回答前想先看他的 Wiki 里怎么讲时用。返回编号片段和出处（页面段 / 内容库段），"
               "要看全文再用 read_page。最多 8 条，结果最多 4000 字。")

_LATIN = re.compile(r"[a-z0-9]+")
_HAN = re.compile(r"[\u4e00-\u9fff]+")


class SegmentHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    segment_id: str
    title: str
    text: str
    score: float


class ContentSearch(Protocol):
    def search(self, query: str, k: int) -> Sequence[SegmentHit]: ...


class VectorContentSearch:
    """Reviewed library segments by embedding similarity (same embedder tag only)."""

    def __init__(self, conn: psycopg.Connection, content: LoadedContent, embedder: Embedder):
        self._conn, self._content, self._embedder = conn, content, embedder
        self._by_id = {s.id: s for s in content.graph.segments}

    def search(self, query: str, k: int) -> Sequence[SegmentHit]:
        rows = search_segments(self._conn, self._content.version_id, self._embedder, query,
                               k=k * 2)
        out: list[SegmentHit] = []
        for sid, sim in rows:
            seg = self._by_id.get(sid)
            if seg is None or seg.review_status not in PUBLISHABLE:
                continue
            name = self._content.graph.concept(seg.concept_id).name_zh
            out.append(SegmentHit(segment_id=sid, title=f"{name} · {seg.kind.value}",
                                  text=seg.body, score=sim))
            if len(out) == k:
                break
        return out


def _terms(query: str) -> list[str]:
    q = query.lower()
    terms = list(_LATIN.findall(q))
    for run in _HAN.findall(q):
        terms += [run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)]
    return list(dict.fromkeys(terms))


def _find(term: str, low: str) -> int:
    """Position of `term` in lowercased `low`, or -1. A single Latin letter/digit only counts as
    a whole word (not inside another Latin word), so "z" finds "z-score" / "z分数", not "zebra"."""
    if len(term) == 1 and _LATIN.fullmatch(term):
        m = re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", low)
        return m.start() if m else -1
    return low.find(term)


def score_text(query: str, text: str) -> int:
    t = text.lower()
    return sum(1 for w in _terms(query) if _find(w, t) >= 0)


def snippet(query: str, text: str, limit: int = SNIPPET_CHARS) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    low = flat.lower()
    pos = min((p for p in (_find(w, low) for w in _terms(query)) if p >= 0), default=0)
    start = max(0, min(pos - limit // 3, len(flat) - limit + 2))
    body = flat[start:start + limit - 2]
    return ("…" if start > 0 else "") + body + "…"


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=8,
                       description="Wiki 和内容库各最多返回几条，1–8")


class SearchWikiTool:
    def __init__(self, pages: PageStore, content: ContentSearch | None = None):
        self._pages, self._content = pages, content
        self.spec = spec("search_wiki", DESCRIPTION, Args, "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(Args, args, lambda a: self._run(a, ctx))

    def _run(self, a: Args, ctx: ToolContext) -> ToolResult:
        # (score, page, section — None for a page-level hit, text to take the snippet from)
        scored: list[tuple[int, WikiPage, OutlineSection | None, str]] = []
        for wp in self._pages.all(ctx.user_id):
            title_score = score_text(a.query, wp.title)
            bonus = 1 if title_score else 0
            section_hit = False
            for s in wp.page.outline.sections:
                text = wp.page.texts.get(s.id)
                if text is None:
                    continue
                score = 2 * score_text(a.query, s.heading) + score_text(a.query, text.body)
                if score:
                    section_hit = True
                    scored.append((score + bonus, wp, s, text.body))
            if title_score and not section_hit:   # the page is about it, no section says so
                headings = "；".join(s.heading for s in wp.page.outline.sections)
                scored.append((title_score, wp, None, headings))
        scored.sort(key=lambda x: -x[0])
        # (block text without its [Wn]/[Cn] number, its source), in display order
        found: list[tuple[str, str, Source]] = []
        for _, wp, s, body in scored[:a.limit]:
            name = wp.display_title if s is None else f"{wp.display_title} › {s.heading}"
            title = clip(name, TITLE_CHARS)
            where = f"page_id={wp.page_id}" + ("" if s is None else f"，section_id={s.id}")
            found.append(("W", f"《{title}》（{where}）\n{snippet(a.query, body)}",
                          Source(title=title, page_id=wp.page_id)))
        tail = ""
        if self._content is not None:
            try:
                hits = list(self._content.search(a.query, a.limit))[:a.limit]
            except Exception:  # noqa: BLE001 — the user's own pages are still worth returning
                hits, tail = [], "\n（内容库暂时不可用）"
            for h in hits:
                title = clip(h.title or h.segment_id, TITLE_CHARS)
                entry = (f"内容库 · {title}（segment_id={h.segment_id}）\n"
                        f"{snippet(a.query, h.text)}")
                found.append(("C", entry, Source(title=title, segment_id=h.segment_id)))
        if not found:
            return ToolResult(content="没有找到相关内容" + tail)
        # Whole blocks only, and a source only for a block the model actually sees; the tail's
        # room is reserved up front so a long result can't push out the "library down" note.
        budget = SEARCH_CHARS - len(tail)
        blocks: list[str] = []
        sources: list[Source] = []
        counts = {"W": 0, "C": 0}
        used = 0
        for tag, entry, src in found:
            block = f"[{tag}{counts[tag] + 1}] {entry}"
            cost = len(block) + (2 if blocks else 0)
            if used + cost > budget:
                if blocks:
                    break
                block, cost = clip(block, budget), budget   # pathological ids: show its head
            counts[tag] += 1
            blocks.append(block)
            sources.append(src)
            used += cost
        return ToolResult(content="\n\n".join(blocks) + tail, sources=tuple(sources))
