"""Plain Chinese for the harness rows in the change list (harness 3b follow-up 11b, final fix I2).

The user never sees an internal name: a toggle row says what the agent does with the tool
("写页面时不再查看讲解规则和范例页"). `ACTIONS` holds one verb phrase per toggleable tool of
`keys.TOGGLE_TOOLS`; `tests/harness/test_tool_labels.py` keeps the two in step, so a new toggleable
tool cannot ship without its label. `tier` (H10 tier values, the plugin's 质量优先 / 速度优先
wording), `spec_parts` (H8 spec parts) and `COMPONENT` / `component_ref` (an H8 component by its
library name) cover the other values 3b's improver writes."""

from collections.abc import Mapping
from types import MappingProxyType

from evowiki.assistant.status_line import EN_OPERATIONS
from evowiki.i18n import current_language

FORGED_TOOL = "一个自己做的工具"
FORGED_REF = "forged:"               # forged_ref's prefix
DOES_REF = "does:"                   # a forged tool's plain sentence, by candidate id (`load_titles`)
COMPONENT = "一个交互组件"            # an H8 component whose library name is not known
TIERS: Mapping[str, str] = MappingProxyType({"good": "质量优先", "fast": "速度优先", "deep": "最强"})
SPEC_PARTS: Mapping[str, str] = MappingProxyType({"items": "讲解规则条目", "exemplar": "范例页"})
_WHO = {"deep": "写页面时", "assistant": "助手"}

ACTIONS: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "deep": MappingProxyType({
        "read_k": "查看你对各个知识点的掌握情况",
        "read_page": "查看已有的页面",
        "read_spec": "查看讲解规则和范例页",
        "place_component": "在页面里放交互组件",
        "list_sources": "查看你给的资料清单",
        "read_source": "阅读资料原文",
        "search_source": "在资料里搜索",
        "find_image": "在开放图库里找配图",
        "place_image": "把找到的图放进页面",
        "draw_visual": "画一张能动手的图",
    }),
    "assistant": MappingProxyType({
        "search_wiki": "搜索 Wiki 里的页面和内容库",
        "read_page": "查看某一页或某一段",
        "read_k": "查看你对各个知识点的掌握情况",
        "web_search": "联网搜索",
        "fetch_url": "打开网页读内容",
        "run_python": "试运行给你的代码",
        "propose_page_edit": "提出修改某一页的建议",
        "illustrate_section": "给某一节配图并生成修订草稿",
        "read_path": "查看学习路径",
        "read_goals": "查看学习目标和进度",
        "read_graph": "查看知识图谱里的某个点",
        "read_reviews": "查看到期的复习卡",
        "open_review": "打开复习卡",
        "read_review_digest": "查看这段时间学得怎么样",
        "set_exemplar": "把一页设为样板页",
        "today_plan": "排出今天该学什么",
        "why": "查某条提醒为什么会出现",
        "read_activity": "查看 Bot 最近做了什么",
        "read_usage": "查看用量和上限",
        "read_rules": "查看你的讲解规则",
        "read_memory_conflicts": "查看待确认的记忆",
        "resolve_memory_conflict": "处理一组待确认的记忆",
        "split_page": "把一页拆成几页",
        "merge_pages": "把几页合成一页",
        "add_practice": "给页面加练习",
        "write_page": "给某个点写新的一页",
        "cheat_sheet": "做速查页",
        "reorder_path": "调整学习路径的顺序",
        "postpone": "把路径上的某些页往后放",
        "set_deadline": "设考试或截止日期",
        "new_goal": "开新的学习目标",
        "edit_goal_blocks": "按你说的改新目标的分块",
        "edit_reading": "按你说的改带读的分段",
        "set_goal_limits": "按你说的改学习目标的条件",
        "pause_goal": "暂停或继续学习目标",
        "split_node": "拆细图谱里的点",
        "edit_relation": "改图谱里的关系",
        "mark_known": "把某个点记成已经会了",
        "move_node": "把图谱里的点挪到别的上级下面",
        "make_cards": "给页面做复习卡",
        "fix_card": "按你的意见改复习卡",
        "drop_card": "删掉某张复习卡",
        "postpone_reviews": "把到期的复习往后推",
        "restyle_pages": "按新的讲解规则重写旧页面",
        "remember_preference": "把你在聊天里说的偏好记成原则",
        "report_problem": "记下你对系统做法的抱怨",
        "remember_about_me": "把你说起自己的话记进档案「我」",
        "bot_settings": "改 Bot 的设置",
        "undo": "撤销已经做的动作",
        "add_source": "收进你给的资料",
        "mark_source_use": "记下资料的用途",
        "revise_page": "参照资料改写页面",
        "list_sources": "查看你给的资料清单",
        "read_source": "阅读资料原文",
        "search_source": "在资料里搜索",
        "search_memory": "查找以前对话里说过、定过的事",
        "read_past_turn": "读以前的一整轮对话",
        "forget_memory": "按你的要求提议忘掉一件事",
        "cancel_card": "按你的要求取消一张等确认的卡片",
        "forget_about_me": "按你的要求提议删掉档案里的一句",
        "load_skill": "按需载入做某类事的方法",
        "read_ref": "按需查看做某类事的参考",
        "offer": "记下等你同意的操作",
        "confirm": "在你同意后执行提出的操作",
        "record_evidence": "把你这句话说明的学习情况记进 K",
        "ask_check": "在聊天里问你一道摸底题",
        "ask_rough": "给你一张摸底表，自己标每一块会不会",
        "skip_diagnosis": "结束这次摸底，直接开始",
        "edit_path_blocks": "改学习路径上的块：删除、改名、加一块、调顺序",
    }),
})


def who(agent: str) -> str:
    if current_language() == "en":
        return "Page writer" if agent == "deep" else "Assistant"
    return _WHO.get(agent, "助手")


def off_line(agent: str, tool: str) -> str | None:
    """The change line for turning the tool off; None for a tool without a label."""
    if current_language() == "en":
        act = action_label(agent, tool)
        return f"Disabled for {who(agent)}: {act}" if act else None
    act = ACTIONS.get(agent, {}).get(tool)
    return f"{who(agent)}不再{act}" if act else None


def on_line(agent: str, tool: str) -> str | None:
    """The change line for turning the tool (back) on."""
    if current_language() == "en":
        act = action_label(agent, tool)
        return f"Enabled for {who(agent)}: {act}" if act else None
    act = ACTIONS.get(agent, {}).get(tool)
    return f"{who(agent)}会{act}" if act else None


def state_line(agent: str, tool: str, on: bool) -> str | None:
    return on_line(agent, tool) if on else off_line(agent, tool)


def forged_line(agent: str, on: bool, title: str | None = None) -> str:
    """A forged tool's opt-in on its own: by its title, never its identifier."""
    if current_language() == "en":
        return f"{who(agent)} {'started using' if on else 'stopped using'} {title or 'a generated tool'}"
    return f"{who(agent)}{'开始用' if on else '不再用'}{title or FORGED_TOOL}"


def tier(value: object) -> str:
    """An H10 tier value in plain words (质量优先 / 速度优先 / 最强); never the identifier."""
    if current_language() == "en":
        return {"good": "Quality", "fast": "Speed", "deep": "Highest capability"}.get(str(value), "Another model tier")
    return TIERS.get(str(value), "另一档")


def spec_parts(parts: object) -> str:
    """H8 spec parts (a list of keys.SPEC_PARTS) in plain words, 不读 for none."""
    if current_language() == "en":
        return ", ".join({"items": "teaching rules", "exemplar": "example pages"}.get(str(p), "another section")
                         for p in parts) if isinstance(parts, list) and parts else "None"
    if not isinstance(parts, list):
        return "不读"
    return "、".join(SPEC_PARTS.get(str(p), "另一部分") for p in parts) or "不读"


def component_ref(component_id: str) -> str:
    """The key of a component's library name in the change list's names map (`change_source.
    load_titles`), apart from forge candidate ids and forged tool names."""
    return f"component:{component_id}"


def forged_ref(agent: str, tool: str) -> str:
    """The key of a forged tool's title in the change list's names map (`change_source.
    load_titles`, the title of the candidate the user's registry has under that name), for a
    forged tool's opt-in on its own (no H12 op, so no candidate id in the change)."""
    return f"{FORGED_REF}{agent}:{tool}"


def action_label(agent: str, tool: str) -> str | None:
    if tool not in ACTIONS.get(agent, {}):
        return None
    extra = {"read_spec": "Read teaching rules and examples", "place_component": "Place an interactive component",
             "find_image": "Find an openly licensed image", "place_image": "Place an image",
             "draw_visual": "Draw an interactive visual"}
    return extra.get(tool) or EN_OPERATIONS.get(tool, ("Use a tool", "Used a tool"))[1]


def forged_before(agent: str, enabled: bool, title: str | None) -> str:
    if current_language() == "en":
        return f"{who(agent)} {'used' if enabled else 'did not use'} {title or 'a generated tool'}"
    return f"{who(agent)}{'用了' if enabled else '没用'}{title or FORGED_TOOL}"
