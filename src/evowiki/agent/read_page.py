"""read_page: the current page's outline, or one section in full (Claude Code `Read`'s two steps:
see the structure, then read one unit)."""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import (
    PAGE_OUTLINE_CHARS,
    SECTION_CHARS,
    TITLE_CHARS,
    clip,
    fail,
    run_guarded,
    spec,
)
from evowiki.agent.tools import Source, ToolContext, ToolResult
from evowiki.wikiapp.page_reader import (
    READ_SCOPE,
    PageStore,
    UnwrittenPage,
    WikiPage,
    check_status_lines,
)

NOT_WRITTEN = "这一页还没写，还没有大纲；要拆它或改它的计划，按标题和它要讲的点来定。"
WRITING = "这一页正在写或没写完：下面是它计划的大纲，标「未写」的段还没有正文。"

DESCRIPTION = ("读用户的一页 Wiki。不给 section_id 时返回大纲（概念 id；每段 id、标题、是否折叠、"
               "回想提示、格子 id）；"
               "给 section_id 时返回那一段全文（最多 6000 字），确认题返回题干、选项和作答状态。"
               "只读服务端受管内容，不含本地个人笔记；看不到不代表没有。page_id 省略时读当前页。"
               "用户问到页面上别的段、或要引用页面原文时才读全文；"
               "计划拆页、合并、调顺序，大纲就够，不必一段一段读。")


# 「手改内容和那一段直接不改」 (ruling 2026-11-04): the writer keeps a pinned section by code (goals.fold.keep_locked); a
# draft here he confirms himself, so the model has to know which sections those are
PINNED = "他亲手改过或锁定了，整页重写时不动这一节，他点名要改才改"


_FENCE = re.compile(r"\s*(?:```|~~~|\$\$)")


def paragraph_spans(text: str) -> list[tuple[int, int]]:
    """The natural paragraphs of `text`: runs of lines between blank lines; a blank line inside a code
    block or a display formula divides nothing."""
    spans: list[tuple[int, int]] = []
    start = None
    at = 0
    fenced = False
    for line in text.splitlines(keepends=True):
        fence = _FENCE.match(line)
        twice = fence is not None and line.count(fence.group(0).strip()) % 2 == 0
        if not line.strip() and not fenced:
            if start is not None:
                spans.append((start, at))
                start = None
        elif start is None:
            start = at
        if fence is not None and not twice:
            fenced = not fenced
        at += len(line)
    if start is not None:
        spans.append((start, at))
    return [(a, a + len(text[a:b].rstrip())) for a, b in spans]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: str | None = Field(default=None, min_length=1, max_length=200,
                                description="页面 id，省略=当前页")
    section_id: str | None = Field(default=None, min_length=1, max_length=200,
                                   description="段 id（见大纲），省略=返回大纲")


def _unwritten(up: UnwrittenPage) -> str:
    lines = [f"《{up.title or up.page_id}》 page_id={up.page_id}", READ_SCOPE, NOT_WRITTEN]
    if up.goal:
        lines.append(f"要讲的：{up.goal}")
    if up.points:
        lines.append(f"讲的点：{'、'.join(up.points)}")
    return clip("\n".join(lines), PAGE_OUTLINE_CHARS)


def _outline(wp: WikiPage) -> str:
    p = wp.page
    lines = [f"《{wp.display_title}》 page_id={wp.page_id}", READ_SCOPE,
             *([WRITING] if wp.writing else []),
             f"概念：{', '.join(p.outline.concepts)}"]
    checks = check_status_lines(p, wp.check_answers)
    for s in p.outline.sections:
        if s.id in checks:
            lines.append(checks[s.id])
            continue
        state = p.state(s.id)
        text = p.texts.get(s.id)
        flags = ([s.role.value] + (["已折叠"] if state.folded else []) + ([] if text else ["未写"])
                 + ([PINNED] if state.pinned else []))
        line = f"- {s.id} · {s.heading}（{'，'.join(flags)}）"
        if text and text.cue:
            line += f" 回想提示：{text.cue}" + (f" 答案：{text.answer}" if text.answer else "")
        if s.cells:
            line += f" 格子：{', '.join(s.cells)}"
        lines.append(line)
    return clip("\n".join(lines), PAGE_OUTLINE_CHARS)


class ReadPageTool:
    def __init__(self, pages: PageStore):
        self._pages = pages
        self.spec = spec("read_page", DESCRIPTION, Args, "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(Args, args, lambda a: self._run(a, ctx))

    def _run(self, a: Args, ctx: ToolContext) -> ToolResult:
        pid = a.page_id or ctx.page_id
        if pid is None:
            return fail("没有当前页，请给出 page_id（不知道就先用 search_wiki 查）")
        wp = self._pages.get(ctx.user_id, pid)
        if wp is None:
            unwritten = getattr(self._pages, "unwritten", None)
            up = unwritten(ctx.user_id, pid) if unwritten is not None else None
            if up is None:
                return fail(f"找不到页面 {pid}")
            if a.section_id is not None:
                return fail(f"段 {a.section_id} 还没写：{NOT_WRITTEN}")
            return ToolResult(content=_unwritten(up), sources=(
                Source(title=clip(up.title or up.page_id, TITLE_CHARS), page_id=up.page_id),))
        title = clip(wp.display_title, TITLE_CHARS)
        if a.section_id is None:
            return ToolResult(content=_outline(wp),
                              sources=(Source(title=title, page_id=wp.page_id),))
        sec = next((s for s in wp.page.outline.sections if s.id == a.section_id), None)
        if sec is None:
            return fail(f"页面 {pid} 没有段 {a.section_id}")
        if sec.id in (checks := check_status_lines(wp.page, wp.check_answers)):
            content = f"{READ_SCOPE}\n{checks[sec.id]}"
            if sec.check is not None and sec.check.verified:
                content += f"\n题目：{sec.check.stem}\n选项：\n" + "\n".join(
                    f"- {option}" for option in sec.check.options)
            return ToolResult(content=content, sources=(
                Source(title=clip(f"{wp.display_title} › {sec.heading}", TITLE_CHARS), page_id=wp.page_id),))
        text = wp.page.texts.get(sec.id)
        if text is None:
            return fail(f"段 {a.section_id} 还没写")
        count = len(paragraph_spans(text.body))
        body = clip(f"{READ_SCOPE}\n## {sec.heading}（{count} 个自然段）\n{text.body}", SECTION_CHARS)
        return ToolResult(content=body, sources=(
            Source(title=clip(f"{wp.display_title} › {sec.heading}", TITLE_CHARS), page_id=wp.page_id),))
