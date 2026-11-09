"""The assistant's tool rows, the way Codex and Claude Code show an agent turn: each read or
network call is one quiet plain-Chinese row. While it runs the row says what is being done (the
status line, `status_line`); once done it says what was done and how much came back
(「在 Wiki 里搜了「协方差」· 5 页」, 「读了《基因配对》第 2 节」, 「联网搜了「…」· 3 条」). The results
are behind a click, as titles only (`items`): page titles, web page titles, material names —
never ids, a section's aspect / depth names or a content-library segment label.

Consecutive calls of one `group` fold into one row (Codex's exploring cell, Claude Code's
「Read 3 files」): `group_label` with `{n}` = how many calls, `{k}` = how many different items
(「读了 {k} 页」). `refs`: the sources the answer can cite (the pages, materials and web pages that
were actually read), listed under the answer like ChatGPT's sources; search hits are not."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from evowiki.agent.tools import Source, ToolResult
from evowiki.assistant.status_line import FALLBACK, StatusNames, english_operation, status_line

SECTION_SEP = " › "           # read_page / search_wiki write 「页 › 节」 into a source title
# action tools shown as a quiet row like a read (no card): what they did is one line
# internal steps never shown in the chat, live or restored (stage 3: no "loading skill" lines)
# record_evidence: what it changed shows as the turn's one `observed` line, never as a tool row
HIDDEN_ROWS = frozenset({"load_skill", "read_ref", "offer", "record_evidence"})
QUIET_ROWS: dict[str, str] = {"report_problem": "已记下这个问题",
                               "remember_about_me": "记进了你的档案「我」",
                               "add_source": "收进了资料",
                               "open_review": "打开了复习"}


@dataclass(frozen=True)
class DoneRow:
    label: str
    group: str = ""
    group_label: str = ""
    items: tuple[str, ...] = ()
    refs: tuple[Source, ...] = ()


def _page_title(src: Source, names: StatusNames) -> str:
    """A page source's own title: the page's name as the turn knows it, else the source title
    without its 「› 节」 part."""
    known = names.pages.get(src.page_id or "")
    return known or src.title.split(SECTION_SEP, 1)[0].strip()


def _pages(sources: Sequence[Source], names: StatusNames) -> tuple[list[str], list[Source]]:
    """The distinct pages among `sources` (titles, and one clean source each), in order."""
    titles: list[str] = []
    refs: list[Source] = []
    seen: set[str] = set()
    for s in sources:
        if not s.page_id or s.page_id in seen:
            continue
        seen.add(s.page_id)
        title = _page_title(s, names)
        if title:
            titles.append(title)
            refs.append(Source(title=title, page_id=s.page_id))
    return titles, refs


def _web(sources: Sequence[Source]) -> list[Source]:
    out: list[Source] = []
    seen: set[str] = set()
    for s in sources:
        if s.url and s.url not in seen:
            seen.add(s.url)
            out.append(s)
    return out


def _past(running: str) -> str:
    """The done form of a plain running line: 「正在看学习路径」 → 「看了学习路径」."""
    if running.startswith("正在") and len(running) > 3:
        rest = running[2:]
        return f"{rest[0]}了{rest[1:]}"
    return running


_MISS_VERBS = frozenset("读看找搜查")


def _missed(running: str) -> str:
    """A read that found nothing it could use, said plainly (the model goes on without it; the answer says what it
    did): 「正在读页面」 → 「没读到页面」. Else the old 「…，没成功」."""
    if running.startswith("正在") and len(running) > 3 and running[2] in _MISS_VERBS:
        return f"没{running[2]}到{running[3:]}"
    return f"{_past(running)}，没成功" if running != FALLBACK else "没成功"


def _q(args: Mapping[str, Any]) -> str:
    q = args.get("query")
    return " ".join(q.split())[:24] if isinstance(q, str) else ""


def _count(n: int, unit: str) -> str:
    return f" · {n} {unit}" if n else " · 没找到"


def done_row(name: str, args: Mapping[str, Any], r: ToolResult,
             names: StatusNames) -> DoneRow:
    """The row of a finished read / network call. Never raises."""
    if names.language == "en":
        return _english_done_row(name, args if isinstance(args, Mapping) else {}, r, names)
    try:
        return _done_row(name, args if isinstance(args, Mapping) else {}, r, names)
    except Exception:  # noqa: BLE001 - a row's wording never fails a turn
        return DoneRow(label=_past(status_line(name, {}, names)))


def _done_row(name: str, args: Mapping[str, Any], r: ToolResult,
              names: StatusNames) -> DoneRow:
    ok = r.error is None
    running = status_line(name, args, names)
    if not ok:
        if name == "open_review":
            return DoneRow(label="这一页的复习卡还不够" if args.get("page") or args.get("this_page")
                           else "现在没有要复习的卡")
        return DoneRow(label=_missed(running))
    if name == "open_review" and r.sources:      # the plugin opens this page's round (refs)
        titles, refs = _pages(r.sources, names)
        return DoneRow(label=f"打开了《{titles[0]}》的复习卡" if titles else QUIET_ROWS[name], refs=tuple(refs))
    if name in QUIET_ROWS:
        if name == "remember_about_me" and r.action is None:      # applies=this_task
            return DoneRow(label="这次照着用，没有记进档案")
        if r.action is not None and r.action.status == "proposed":  # waits for his yes (a card)
            return DoneRow(label="等你确认要不要记进档案")
        return DoneRow(label=QUIET_ROWS[name])
    q = _q(args)
    if name == "search_wiki":
        titles, _ = _pages(r.sources, names)
        where = f"在 Wiki 里搜了「{q}」" if q else "在 Wiki 里搜了"
        return DoneRow(label=where + _count(len(titles), "页"), group=name,
                       group_label="在 Wiki 里搜了 {n} 次", items=tuple(titles))
    if name == "read_page":
        titles, refs = _pages(r.sources, names)
        label = _past(running) if running != "正在读页面" or not titles else f"读了《{titles[0]}》"
        return DoneRow(label=label, group=name, group_label="读了 {k} 页",
                       items=tuple(titles), refs=tuple(refs))
    if name == "web_search":
        hits = _web(r.sources)
        where = f"联网搜了「{q}」" if q else "联网搜了"
        return DoneRow(label=where + _count(len(hits), "条"), group=name,
                       group_label="联网搜了 {n} 次", items=tuple(h.title for h in hits))
    if name == "fetch_url":
        pages = _web(r.sources)
        host = running.split("：", 1)[1] if "：" in running else ""
        return DoneRow(label=f"打开了网页 {host}" if host else "打开了网页", group=name, group_label="打开了 {k} 个网页",
                       items=tuple(p.title for p in pages), refs=tuple(pages))
    if name == "read_source":
        label = (running.replace("正在精确读", "精确读了", 1) if args.get("precise") is True
                 else _past(running))
        return DoneRow(label=label, group=name, group_label="读了 {n} 处资料")
    if name == "search_source":
        sid = args.get("source_id")
        title = names.sources.get(sid, "") if isinstance(sid, str) else ""
        where = f"在《{title}》里搜了" if title else "在资料里搜了"
        return DoneRow(label=f"{where}「{q}」" if q else where, group=name, group_label="在资料里搜了 {n} 次")
    return DoneRow(label=_past(running) if running != FALLBACK else "处理好了")


def _english_done_row(name: str, args: Mapping[str, Any], r: ToolResult,
                      names: StatusNames) -> DoneRow:
    """Presentation from stable tool identity and results, including restored historical rows."""
    if r.error is not None:
        label = ("Not enough review cards on this page" if args.get("page") or args.get("this_page")
                 else "No review cards due") if name == "open_review" else "Could not complete this operation"
        return DoneRow(label=label)
    if name == "remember_about_me":
        if r.action is None:
            return DoneRow(label="Used for this task; not saved to your background")
        if r.action.status == "proposed":
            return DoneRow(label="Waiting for confirmation to save to your background")
    label = english_operation(name, args, names, done=True)
    titles, refs = _pages(r.sources, names)
    if name == "search_wiki":
        return DoneRow(label=f"{label} · {len(titles)} pages", group=name,
                       group_label="Searched your Wiki {n} times", items=tuple(titles))
    if name in ("read_page", "open_review"):
        if titles:
            label = f"Read {titles[0]}" if name == "read_page" else f"Opened review: {titles[0]}"
        return DoneRow(label=label, group=name, group_label="Read {k} pages",
                       items=tuple(titles), refs=tuple(refs))
    if name in ("web_search", "fetch_url"):
        web = _web(r.sources)
        return DoneRow(label=f"{label} · {len(web)} results", group=name,
                       group_label="Searched the web {n} times" if name == "web_search" else "Opened {k} web pages",
                       items=tuple(s.title for s in web), refs=tuple(web) if name == "fetch_url" else ())
    if name in ("read_source", "search_source"):
        return DoneRow(label=label, group=name, group_label="Read {n} material sections" if name == "read_source"
                       else "Searched material {n} times")
    return DoneRow(label=label)
