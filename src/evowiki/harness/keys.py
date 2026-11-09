"""The harness pack's keys (design 2026-10-21 §3.1): which settings of the product's agents a pack
may hold, of what kind, within which bounds. The bounds are anchors — only the user changes this
file (same rule as teach/anchors.py). Defaults are not here: each stays the constant of the module
that uses it (service/harness_defaults.py lists them for display), so this module imports no agent.

H2 (tool and argument descriptions) has one key per tool / argument of each agent; its names are
checked by pattern here and against the real tools where they are applied (harness/apply.py)."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from evowiki.forge.limits import CODER_MAX_TOOL_CALLS, L2_MAX_TOOL_CALLS
from evowiki.harness import tool_names
from evowiki.llm.prompt_steps import STEPS

Dim = Literal["H1", "H2", "H3", "H4", "H5", "H6", "H8", "H9", "H10", "H11", "H12"]
Kind = Literal["text", "int", "bool", "enum", "list", "steps", "json"]
Approval = Literal["auto", "ask"]

TEXT_MAX = {"H1": 4000, "H2": 1000}     # H1: bot/prompts.MAX_CHARS; H2: ToolSpec.description
AGENTS = ("deep", "assistant", "bot")
_H2 = re.compile(r"^H2\.(deep|assistant|bot)\.[a-z][a-z0-9_]{1,40}(\.[a-z][a-z0-9_]{0,40})?$")

# One definition in forge/limits.py; H4.forge.* may only lower these caps.
FORGE_CAP = CODER_MAX_TOOL_CALLS
FORGE_PATCH_CAP = L2_MAX_TOOL_CALLS
FORGED_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")    # a forged tool's name (phase 3b P3)


@dataclass(frozen=True)
class Key:
    name: str
    dim: Dim
    kind: Kind
    lo: int | None
    hi: int | None
    doc: str
    approval: Approval = "auto"
    options: tuple[str, ...] = ()      # enum: allowed values; list: allowed items


def h1_key(step: str) -> str:
    return f"H1.{step}"


def h2_key(agent: str, tool: str, arg: str | None = None) -> str:
    return f"H2.{agent}.{tool}" + (f".{arg}" if arg else "")


def is_h2(name: str) -> bool:
    return bool(_H2.fullmatch(name))


def _int(name: str, lo: int, hi: int, doc: str) -> Key:
    return Key(name, name.split(".", 1)[0], "int", lo, hi, doc)  # type: ignore[arg-type]


KEYS: tuple[Key, ...] = (
    *(Key(h1_key(s.key), "H1", "text", 1, TEXT_MAX["H1"], f"「{s.name}」的写法") for s in STEPS),
    _int("H4.deep.max_tool_calls", 10, 200, "写页面：每轮最多调用几次工具"),
    _int("H4.deep.max_rewrites", 0, 4, "写页面：最多重写几轮"),
    _int("H4.deep.max_nudges", 0, 4, "写页面：没调工具时最多催几次"),
    _int("H4.deep.max_truncated", 0, 3, "写页面：回复被截断时最多重试几次"),
    _int("H4.assistant.max_tool_calls", 1, 20, "助手：每轮最多调用几次工具"),
    _int("H4.bot.max_tool_calls", 1, 8, "Bot：每次最多调用几次工具"),
    _int("H4.bot.max_nudges", 0, 3, "Bot：没调工具时最多催几次"),
    _int("H5.deep.keep_reads", 1, 20, "写页面：读到的内容保留几轮后缩成摘要"),
    _int("H5.deep.stub_over", 100, 20000, "写页面：多长的读取结果会被缩成摘要"),
    _int("H5.assistant.context_budget", 500, 20000, "助手：页面上下文最多多少 token"),
    _int("H5.assistant.selection_max", 200, 8000, "助手：选中文字最多放多少字"),
    _int("H5.assistant.paragraph_max", 200, 8000, "助手：所在段落最多放多少字"),
    _int("H5.assistant.top_k_pages", 0, 20, "助手：放几条相关页面"),
    _int("H5.assistant.read_keep_calls", 1, 20, "助手：读到的内容保留几次调用后缩成摘要"),
    _int("H5.assistant.read_stub_over", 100, 20000, "助手：多长的读取结果会被缩成摘要"),
    _int("H6.history_budget", 2000, 100000, "对话超过多少 token 开始压缩"),
    _int("H6.keep_turns", 1, 40, "压缩时保留最近几轮原文"),
    _int("H6.keep_budget", 1000, 100000, "保留的原文最多多少 token"),
    _int("H6.hard_cap", 4000, 150000, "压缩后仍超过多少 token 就丢掉最早的几轮"),
    _int("H6.summary_max_tokens", 500, 16000, "一次压缩摘要最多多少 token"),
    _int("H4.forge.max_tool_calls", 1, FORGE_CAP, "造工具：每次最多调用几次工具"),
    _int("H4.forge.patch.max_tool_calls", 1, FORGE_PATCH_CAP, "改代码：每次最多调用几次工具"),
)

# --- Structural keys (phase 3a: H3 / H8 / H9 / H10 / H11). Anchors: only the user changes them. ---

# H3 (rulings R1–R3): tools a key may switch off, and tools that are always on.
# stage 3's Skill / confirmation tools and its K writers are core contracts, never switched off by a
# key (RSI constraint 1: K updates are not steerable through H keys)
_ASSISTANT_LOCKED = ("load_skill", "read_ref", "offer", "confirm", "record_evidence",
                     "ask_check", "ask_rough", "skip_diagnosis")
TOGGLE_TOOLS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "deep": ("read_k", "read_page", "read_spec", "place_component",
             "list_sources", "read_source", "search_source", "find_image", "place_image", "draw_visual"),
    "assistant": tuple(sorted(set(tool_names.TOOLS["assistant"]) - set(_ASSISTANT_LOCKED))),
})
LOCKED_TOOLS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "deep": ("write_section", "read_ref", "write_block", "render_check", "finish"),
    "bot": ("suggest", "no_action"),
    "assistant": _ASSISTANT_LOCKED,
})


# H9 / H11 hook points (spec §3.3; ruling R4)
@dataclass(frozen=True)
class Hook:
    kinds: tuple[Literal["delegate", "self_check"], ...]
    tiers: tuple[Literal["fast", "good", "deep"], ...]
    tools: tuple[str, ...]          # read tools of that agent the step may use
    output_to: str


HOOKS: Mapping[str, Hook] = MappingProxyType({
    "deep.before_write": Hook(("delegate",), ("fast", "good", "deep"),
                              ("read_k", "read_page", "read_spec", "list_sources",
                               "read_source", "search_source"), "context.notes"),
    "deep.after_write": Hook(("self_check",), ("fast", "good", "deep"),
                             ("read_k", "read_page", "read_spec", "list_sources",
                              "read_source", "search_source"), "context.must_fix"),
    "assistant.before_answer": Hook(("delegate",), ("fast", "good"),
                                    ("search_wiki", "read_page", "read_k", "web_search",
                                     "fetch_url", "read_path", "read_goals", "read_graph",
                                     "read_reviews", "read_activity", "read_rules",
                                     "today_plan", "why", "read_usage"), "context.notes"),
})
HOOK_MAX_TOOL_CALLS = 12
HOOK_TEXT_MAX = 2000          # a step's instruction
HOOK_OUTPUT_MAX = 4000        # clip of a delegate's output (ruling R8)
REPORT_NOTE_MAX = 300         # one note of a self-check's report call

# H10 (ruling R5): step -> (purpose, allowed tiers)
STEPS_H10: Mapping[str, tuple[str, tuple[str, ...]]] = MappingProxyType({
    "deep.plan": ("deep.plan", ("deep", "good")),
    "deep.write": ("deep.write", ("deep", "good")),
    "assistant.turn": ("assistant.turn", ("fast", "good")),
    "bot.plan": ("bot_plan", ("fast", "good")),
    "forge.code": ("forge.code", ("good", "deep")),
})

# H8
SPEC_PARTS = ("items", "exemplar")      # teaching-standard parts a step may read
H8_SPEC_STEPS = ("deep.plan", "deep.write")
COMPONENT_NOTE_MAX = 400                # LibraryComponent.description
SKILL_NAME_MAX = 40                     # an H8 preset's / EvoUI example's name
SKILL_USE_MAX = 200                     # ... and its "when to pick it" text
_H8_COMPONENT = re.compile(r"^H8\.component\.([a-z0-9][a-z0-9_-]{1,60})\.(note|presets)$")

# Phase 3b: a forged tool's opt-in (H3) and the record of an applied forged tool / code patch (H12)
_H3_FORGED = re.compile(r"^H3\.(deep|assistant)\.([a-z][a-z0-9_]{1,40})$")
_H12 = re.compile(
    r"^H12\.(?:(tool)\.(?:deep|assistant)\.[a-z][a-z0-9_]{1,40}|(patch)\.fc_[A-Za-z0-9]{1,40})$")

_AGENT_DOC = {"deep": "写页面", "assistant": "助手"}
_SPEC_DOC = {"deep.plan": "写页面规划时读教学规范的哪几部分",
             "deep.write": "写页面时预先读教学规范的哪几部分"}
_HOOK_DOC = {"H9.deep.before_write": "写页面：动笔前先做的几步",
             "H9.assistant.before_answer": "助手：回答前先做的几步",
             "H11.deep.after_write": "写页面：写完后自查的几步"}
_H10_DOC = {"deep.plan": "写页面规划", "deep.write": "写页面", "assistant.turn": "助手回答",
            "bot.plan": "Bot 规划", "forge.code": "造工具和改代码"}

STRUCTURAL_KEYS: tuple[Key, ...] = (
    *(Key(f"H3.{a}.{t}", "H3", "bool", None, None, f"{_AGENT_DOC[a]}：能不能用 {t}", "ask")
      for a in ("deep", "assistant") for t in TOGGLE_TOOLS[a]),
    *(Key(f"H8.deep.spec.{s.split('.')[1]}", "H8", "list", 0, len(SPEC_PARTS), _SPEC_DOC[s],
          "auto", SPEC_PARTS) for s in H8_SPEC_STEPS),
    Key("H8.deep.evoui_examples", "H8", "json", None, None, "写页面：额外的交互块示例"),
    *(Key(n, n.split(".", 1)[0], "steps", None, None, d, "ask")  # type: ignore[arg-type]
      for n, d in _HOOK_DOC.items()),
    *(Key(f"H10.{s}.tier", "H10", "enum", None, None, f"{_H10_DOC[s]}用哪一档模型", "ask",
          tiers) for s, (_, tiers) in STEPS_H10.items()),
    *(Key(f"H10.{s}.thinking", "H10", "bool", None, None, f"{_H10_DOC[s]}要不要深度思考", "ask")
      for s in STEPS_H10),
)
KEYS = (*KEYS, *STRUCTURAL_KEYS)
KEY_BY_NAME: Mapping[str, Key] = MappingProxyType({k.name: k for k in KEYS})


def key_of(name: str) -> Key | None:
    """The key, or None for an unknown name. H2, H8 component, forged-tool H3 and H12 names map to
    a key built on the fly (whether the tool / component exists is checked where the value is
    applied). Built-in H3 toggles are in KEY_BY_NAME (approval ask) and win."""
    k = KEY_BY_NAME.get(name)
    if k is None and is_h2(name):
        return Key(name, "H2", "text", 1, TEXT_MAX["H2"], "工具说明")
    if k is None and (m := _H3_FORGED.fullmatch(name)):
        agent, tool = m.group(1), m.group(2)
        if (not tool_names.is_builtin(agent, tool) and tool not in tool_names.RESERVED
                and tool not in LOCKED_TOOLS.get(agent, ())):
            return Key(name, "H3", "bool", None, None, f"{_AGENT_DOC[agent]}：用自己造的工具 {tool}")
    if k is None and (m := _H12.fullmatch(name)):
        if m.group(1) == "tool":
            return Key(name, "H12", "json", None, None, "自己造的工具")
        return Key(name, "H12", "json", None, None, "代码改动", "ask")
    if k is None and (m := _H8_COMPONENT.match(name)):
        if m.group(2) == "note":
            return Key(name, "H8", "text", 1, COMPONENT_NOTE_MAX, "交互组件：什么时候用")
        return Key(name, "H8", "json", None, None, "交互组件：新的预设", "ask")
    return k


def is_forged_opt_in(name: str) -> bool:
    """True for `H3.<deep|assistant>.<tool>` whose tool is a forged one (not built-in)."""
    k = key_of(name)
    return k is not None and k.dim == "H3" and name not in KEY_BY_NAME


def h12_tool_key(agent: str, tool: str) -> str:
    return f"H12.tool.{agent}.{tool}"


def h12_patch_key(candidate_id: str) -> str:
    return f"H12.patch.{candidate_id}"


def needs_approval(name: str) -> bool:
    """True when a change to `name` must be approved by the user (spec §7). KeyError for an
    unknown name."""
    k = key_of(name)
    if k is None:
        raise KeyError(name)
    return k.approval == "ask"
