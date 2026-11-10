"""The assistant's live status line (like Codex's): one short plain-Chinese line per tool call,
streamed as a `StatusLine` event when the call starts and shown under the question in place of
the last one (「正在读《基因配对》第 2 节」, 「正在搜索：碱基配对」, 「正在写草稿」).

The line says what is being done, never how: no tool names, ids, model or prompt text. Names come
from the call's arguments, resolved against what the turn already has at hand (`StatusNames`:
the titles the context pack read, the turn's attachments); an id that is not there gives the
generic line (「正在读页面」), never the raw id. Every built-in assistant tool has a line
(`tests/assistant/test_status_line.py` keeps `LINES` equal to harness/tool_names.py's assistant
tools); anything else (a forged tool, the forge's own tools, an unknown name) says `FALLBACK`."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from evowiki.i18n import tr

FALLBACK = "正在处理…"
ARG_CHARS = 24                       # a query / title shown in the line is cut to this many characters


@dataclass(frozen=True)
class StatusNames:
    """What the turn can name without another read. `pages`: page id → title (the current page and
    the pages the context pack listed); `current_page`: the session's page (read_page without a
    page_id reads it); `sections`: section id of the current page → its number (1-based, in page
    order); `sources`: source id → title (the session's attachments)."""

    pages: Mapping[str, str] = field(default_factory=dict)
    current_page: str | None = None
    sections: Mapping[str, int] = field(default_factory=dict)
    sources: Mapping[str, str] = field(default_factory=dict)
    language: str = "zh"


NO_NAMES = StatusNames()


def _clip(value: object) -> str:
    """An argument as shown: one line, at most ARG_CHARS characters ("" for a non-string)."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= ARG_CHARS else text[:ARG_CHARS] + "…"


def _book(title: str) -> str:
    return f"《{title}》" if title else ""


def _page(args: Mapping[str, Any], names: StatusNames, key: str = "page_id") -> str:
    """The title of the page an argument names (the current page when the argument is absent and
    `key` is read_page's page_id), else ""."""
    pid = args.get(key)
    if pid is None and key == "page_id":
        pid = names.current_page
    return _clip(names.pages.get(pid, "")) if isinstance(pid, str) else ""


def _source(args: Mapping[str, Any], names: StatusNames) -> str:
    sid = args.get("source_id")
    return _clip(names.sources.get(sid, "")) if isinstance(sid, str) else ""


def _read_page(args: Mapping[str, Any], names: StatusNames) -> str:
    title = _page(args, names)
    if not title:
        return "正在读页面"
    sec = args.get("section_id")
    pid = args.get("page_id") or names.current_page
    n = names.sections.get(sec) if isinstance(sec, str) and pid == names.current_page else None
    return f"正在读《{title}》第 {n} 节" if n is not None else f"正在读《{title}》"


def _read_source(args: Mapping[str, Any], names: StatusNames) -> str:
    title = _source(args, names)
    a, b = args.get("page_from"), args.get("page_to")
    pages = ""
    if isinstance(a, int) and not isinstance(a, bool):
        pages = (f"第 {a}–{b} 页" if isinstance(b, int) and not isinstance(b, bool) and b > a
                 else f"第 {a} 页")
    verb = "正在精确读" if args.get("precise") is True else "正在读"
    return f"{verb}{_book(title) or '资料'}{pages}" if title or pages else f"{verb}资料"


def _search(prefix: str, plain: str) -> Callable[[Mapping[str, Any], StatusNames], str]:
    def line(args: Mapping[str, Any], names: StatusNames) -> str:
        q = _clip(args.get("query"))
        return f"{prefix}：{q}" if q else plain
    return line


def _search_source(args: Mapping[str, Any], names: StatusNames) -> str:
    q, title = _clip(args.get("query")), _source(args, names)
    where = f"正在{_book(title)}里搜索" if title else "正在资料里搜索"
    return f"{where}：{q}" if q else where


def _fetch_url(args: Mapping[str, Any], names: StatusNames) -> str:
    url = args.get("url")
    try:
        host = urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        host = None
    return f"正在打开网页：{_clip(host)}" if host else "正在打开网页"


def _with_page(on: str, plain: str, key: str = "page") -> Callable[[Mapping[str, Any], StatusNames], str]:
    """「正在准备拆分《T》」 when the page is known, else the plain line. `on` has a {} for 《T》."""
    def line(args: Mapping[str, Any], names: StatusNames) -> str:
        title = _page(args, names, key)
        return on.format(_book(title)) if title else plain
    return line


def _with_title(on: str, plain: str) -> Callable[[Mapping[str, Any], StatusNames], str]:
    """A line naming the `title` argument itself (a page or a material not written yet)."""
    def line(args: Mapping[str, Any], names: StatusNames) -> str:
        title = _clip(args.get("title"))
        return on.format(_book(title)) if title else plain
    return line


def _const(text: str) -> Callable[[Mapping[str, Any], StatusNames], str]:
    return lambda args, names: text


def _pause_goal(args: Mapping[str, Any], names: StatusNames) -> str:
    return "正在准备继续学习目标" if args.get("paused") is False else "正在准备暂停学习目标"


LINES: Mapping[str, Callable[[Mapping[str, Any], StatusNames], str]] = {
    # reads
    "search_wiki": _search("正在搜索", "正在你的 Wiki 里查找"),
    "read_page": _read_page,
    "read_k": _const("正在看你的掌握情况"),
    "read_path": _const("正在看学习路径"),
    "read_goals": _const("正在看你的学习目标"),
    "read_graph": _const("正在看知识图谱"),
    "read_reviews": _const("正在看到期的复习"),
    "open_review": _const("正在打开复习"),
    "read_review_digest": _const("正在看你最近学得怎么样"),
    "today_plan": _const("正在排今天的学习"),
    "why": _const("正在查这条提醒的原因"),
    "read_activity": _const("正在看 Bot 最近做了什么"),
    "read_usage": _const("正在看用量"),
    "read_rules": _const("正在看你的讲解规则"),
    "read_memory_conflicts": _const("正在看待确认的记忆"),
    "list_sources": _const("正在看你的资料"),
    "read_source": _read_source,
    "search_source": _search_source,
    "search_memory": _search("正在找以前的对话", "正在找以前的对话"),
    "read_past_turn": _const("正在读以前的对话"),
    # network
    "web_search": _search("正在网上搜索", "正在网上搜索"),
    "fetch_url": _fetch_url,
    "run_python": _const("正在试运行代码"),
    # drafts and actions (the card that follows asks or says what was done)
    "propose_page_edit": _const("正在写草稿"),
    "illustrate_section": _with_page("正在给{}配图", "正在生成配图草稿"),
    "forget_memory": _const("正在找要忘掉的内容"),
    "cancel_card": _const("正在取消这张卡片"),
    "forget_about_me": _const("正在准备删掉档案里的这一句"),
    "split_page": _with_page("正在准备拆分{}", "正在准备拆分页面"),
    "merge_pages": _with_title("正在准备合并成一页{}", "正在准备合并页面"),
    "add_practice": _with_page("正在准备给{}加练习", "正在准备加练习"),
    "write_page": _with_title("正在准备写新的一页{}", "正在准备写新的一页"),
    "cheat_sheet": _with_title("正在准备做速查页{}", "正在准备做速查页"),
    "reorder_path": _const("正在调整学习顺序"),
    "postpone": _const("正在准备把页面往后放"),
    "set_deadline": _const("正在准备设截止日期"),
    "new_goal": _const("正在准备新的学习目标"),
    "edit_goal_blocks": _const("正在调整学习目标的分块"),
    "edit_reading": _const("正在调整怎么分段读"),
    "set_goal_limits": _const("正在调整学习目标的条件"),
    "pause_goal": _pause_goal,
    "split_node": _const("正在准备拆细知识点"),
    "edit_relation": _const("正在准备修改知识点之间的关系"),
    "mark_known": _const("正在准备记成已经会了"),
    "move_node": _const("正在准备移动知识点"),
    "make_cards": _with_page("正在给{}做复习卡", "正在做复习卡"),
    "fix_card": _const("正在改复习卡"),
    "drop_card": _const("正在准备删掉复习卡"),
    "postpone_reviews": _const("正在准备推后复习"),
    "restyle_pages": _const("正在准备按新规则重写旧页面"),
    "remember_preference": _const("正在记下你的偏好"),
    "resolve_memory_conflict": _const("正在处理这组待确认的记忆"),
    "report_problem": _const("正在记下这个问题"),
    "remember_about_me": _const("正在记进你的档案"),
    "bot_settings": _const("正在准备修改 Bot 设置"),
    "mark_source_use": _const("正在记下资料的用途"),
    "set_exemplar": _with_page("正在准备把{}设为样板页", "正在准备设样板页", "page_id"),
    "undo": _const("正在撤销"),
    "add_source": _with_title("正在收进资料{}", "正在收进资料"),
    "revise_page": _with_page("正在对照资料改写{}", "正在对照资料改写页面", "page_id"),
    # stage 3: load_skill / read_ref / offer are never shown (tool_rows.HIDDEN_ROWS); the lines
    # exist so every tool has one
    "load_skill": _const("正在想怎么做"),
    "read_ref": _const("正在想怎么做"),
    "offer": _const("正在记下要你确认的操作"),
    "confirm": _const("正在执行你同意的操作"),
    "record_evidence": _const("正在记下"),
    "ask_check": _const("正在出题"),
    "ask_rough": _const("正在准备摸底表"),
    "skip_diagnosis": _const("正在结束摸底"),
    "edit_path_blocks": _const("正在改学习路径"),
}


def attachments_line(titles: Sequence[str]) -> str:
    """The line while the model is given the files sent with this message: 「正在读《讲义.pdf》」, or
    「正在读你发的 3 份资料」 for more than one (a title not known: 「正在读你发的资料」)."""
    if len(titles) > 1:
        return f"正在读你发的 {len(titles)} 份资料"
    title = _clip(titles[0]) if titles else ""
    return f"正在读《{title}》" if title else "正在读你发的资料"


def blocks_step_line(step: str, titles: Sequence[str] = ()) -> str:
    """new_goal's step while it runs (goals.blocks.plan_blocks' progress): the blocks call reads
    the attached materials and splits the goal (one call)."""
    named = [t for t in titles if t.strip()]
    if named:
        more = f"等 {len(named)} 份资料" if len(named) > 1 else ""
        return f"正在看{_book(named[0])}{more}，拆解学习路径"
    return "正在拆解学习路径"


_READING_STEPS = {"plan": "正在排段落", "check": "正在核对段落", "repair": "正在修正段落"}


def reading_step_line(step: str, titles: Sequence[str] = ()) -> str:
    """new_goal's step for 读懂这篇, as it really runs (goals.blocks' READING_*_STEP): the model reads
    the material's outline, writes the units, code checks them, a repair call fixes them."""
    named = [t for t in titles if t.strip()]
    return _READING_STEPS.get(step) or (f"正在看{_book(named[0])}" if named else "正在看资料")


EN_OPERATIONS = {'search_wiki': ('Searching your Wiki', 'Searched your Wiki'),
 'read_page': ('Reading a page', 'Read the page'),
 'read_k': ('Checking your mastery', 'Checked your mastery'),
 'read_path': ('Reading the learning path', 'Read the learning path'),
 'read_goals': ('Reading your learning goals', 'Read your learning goals'),
 'read_graph': ('Reading the knowledge graph', 'Read the knowledge graph'),
 'read_reviews': ('Checking reviews due', 'Checked reviews due'),
 'open_review': ('Opening review', 'Opened review'),
 'read_review_digest': ('Checking recent learning', 'Checked recent learning'),
 'today_plan': ("Planning today's learning", "Planned today's learning"),
 'why': ('Checking this reminder', 'Checked this reminder'),
 'read_activity': ('Checking recent assistant activity', 'Checked recent assistant activity'),
 'read_usage': ('Checking usage', 'Checked usage'),
 'read_rules': ('Reading your teaching rules', 'Read your teaching rules'),
 'read_memory_conflicts': ('Reading memories awaiting confirmation',
                           'Read memories awaiting confirmation'),
 'list_sources': ('Reading your materials', 'Read your materials'),
 'read_source': ('Reading material', 'Read material'),
 'search_source': ('Searching material', 'Searched material'),
 'search_memory': ('Searching past conversations', 'Searched past conversations'),
 'read_past_turn': ('Reading a past conversation', 'Read a past conversation'),
 'web_search': ('Searching the web', 'Searched the web'),
 'fetch_url': ('Opening a web page', 'Opened the web page'),
 'run_python': ('Running code', 'Ran code'),
 'propose_page_edit': ('Writing a draft', 'Wrote a draft'),
 'illustrate_section': ('Preparing a visual draft', 'Prepared a visual draft'),
 'forget_memory': ('Finding content to forget', 'Found content to forget'),
 'cancel_card': ('Canceling this card', 'Canceled this card'),
 'forget_about_me': ('Preparing to remove a background entry', 'Prepared background removal'),
 'split_page': ('Preparing to split a page', 'Prepared a page split'),
 'merge_pages': ('Preparing to merge pages', 'Prepared a page merge'),
 'add_practice': ('Preparing practice', 'Prepared practice'),
 'write_page': ('Preparing a new page', 'Prepared a new page'),
 'cheat_sheet': ('Preparing a quick reference', 'Prepared a quick reference'),
 'reorder_path': ('Adjusting learning order', 'Adjusted learning order'),
 'postpone': ('Preparing to move a page later', 'Prepared to move a page later'),
 'set_deadline': ('Preparing a deadline', 'Prepared a deadline'),
 'new_goal': ('Preparing a learning goal', 'Prepared a learning goal'),
 'edit_goal_blocks': ('Adjusting goal blocks', 'Adjusted goal blocks'),
 'edit_reading': ('Adjusting reading sections', 'Adjusted reading sections'),
 'set_goal_limits': ('Adjusting goal constraints', 'Adjusted goal constraints'),
 'pause_goal': ('Preparing to pause a goal', 'Prepared to pause a goal'),
 'split_node': ('Preparing to split a concept', 'Prepared a concept split'),
 'edit_relation': ('Preparing to edit concept relations', 'Prepared concept relation changes'),
 'mark_known': ('Preparing a mastery self-report', 'Prepared a mastery self-report'),
 'move_node': ('Preparing to move a concept', 'Prepared to move a concept'),
 'make_cards': ('Making review cards', 'Made review cards'),
 'fix_card': ('Editing a review card', 'Edited a review card'),
 'drop_card': ('Preparing to delete a review card', 'Prepared review card deletion'),
 'postpone_reviews': ('Preparing to postpone reviews', 'Prepared to postpone reviews'),
 'restyle_pages': ('Preparing to rewrite pages with new rules', 'Prepared page rewrites'),
 'remember_preference': ('Recording your preference', 'Recorded your preference'),
 'resolve_memory_conflict': ('Resolving memories awaiting confirmation',
                             'Resolved memories awaiting confirmation'),
 'report_problem': ('Recording this problem', 'Recorded this problem'),
 'remember_about_me': ('Recording your background', 'Recorded your background'),
 'bot_settings': ('Preparing assistant settings', 'Prepared assistant settings'),
 'mark_source_use': ('Recording how to use this material', 'Recorded material use'),
 'set_exemplar': ('Preparing an example page', 'Prepared an example page'),
 'undo': ('Undoing the change', 'Undid the change'),
 'add_source': ('Importing material', 'Imported material'),
 'revise_page': ('Revising a page against its sources', 'Revised a page against its sources'),
 'load_skill': ('Preparing', 'Prepared'),
 'read_ref': ('Preparing', 'Prepared'),
 'offer': ('Preparing an action for confirmation', 'Prepared an action for confirmation'),
 'confirm': ('Executing your confirmed action', 'Executed your confirmed action'),
 'record_evidence': ('Recording evidence', 'Recorded evidence'),
 'ask_check': ('Preparing a question', 'Prepared a question'),
 'ask_rough': ('Preparing an initial assessment', 'Prepared an initial assessment'),
 'skip_diagnosis': ('Ending the initial assessment', 'Ended the initial assessment'),
 'edit_path_blocks': ('Editing the learning path', 'Edited the learning path')}

def english_operation(name: str, args: Mapping[str, Any], names: StatusNames,
                      *, done: bool = False) -> str:
    pair = EN_OPERATIONS.get(name, ("Working…", "Completed"))
    text = tr(pair[1 if done else 0], lang=names.language)
    if name == "pause_goal" and args.get("paused") is False:
        text = "Prepared to resume a goal" if done else "Preparing to resume a goal"
    title = _page(args, names, "page" if "page" in args else "page_id")
    if name in ("read_source", "search_source"):
        title = _source(args, names)
    if not title and name in ("write_page", "merge_pages", "add_source", "cheat_sheet"):
        title = _clip(args.get("title"))
    if title:
        text += f": {title}"
    if name in ("search_wiki", "search_source", "search_memory", "web_search"):
        query = _clip(args.get("query"))
        if query:
            text += f" — {query}"
    if name == "read_page":
        section = names.sections.get(args.get("section_id"))
        if section is not None:
            text += f", section {section}"
    if name == "read_source" and isinstance(args.get("page_from"), int):
        a, b = args["page_from"], args.get("page_to")
        text += f", pages {a}–{b}" if isinstance(b, int) and b > a else f", page {a}"
    return text


def status_line(name: str, args: Mapping[str, Any], names: StatusNames) -> str:
    """The line for a tool call that is starting. Never raises: arguments of any shape give at
    worst the tool's plain line, an unknown tool `FALLBACK`."""
    if names.language == "en":
        return english_operation(name, args if isinstance(args, Mapping) else {}, names)
    line = LINES.get(name)
    if line is None:
        return FALLBACK
    try:
        return line(args if isinstance(args, Mapping) else {}, names) or FALLBACK
    except Exception:  # noqa: BLE001 - a status line never fails a turn
        return FALLBACK
