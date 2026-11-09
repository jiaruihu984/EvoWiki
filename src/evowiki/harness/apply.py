"""Apply a pack to an agent's settings (design 2026-10-21 §3.1 H2 / H4 / H5 / H6). Pure: each
function takes the agent's own default object and returns it unchanged — the same object — when
the pack sets nothing for it, so an empty pack cannot change a request or a limit."""

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from evowiki.agent import durable
from evowiki.agent.compact import CompactPolicy
from evowiki.agent.spec import ElidePolicy, LoopLimits
from evowiki.agent.tools import Tool, ToolContext, ToolResult, ToolSpec
from evowiki.harness.hooks import HookOutput, check_tools, run_hook  # noqa: F401 - apply.run_hook
from evowiki.harness.keys import HOOKS, STEPS_H10, TOGGLE_TOOLS
from evowiki.harness.pack import HarnessInvalid, HarnessPack
from evowiki.harness.steps import HookStep, Preset, parse_examples, parse_presets, parse_steps
from evowiki.llm.models import THINKING_PARAM
from evowiki.llm.usage import Account

Agent = Literal["deep", "assistant", "bot"]

_LIMITS: dict[str, dict[str, str]] = {
    "deep": {"max_tool_calls": "H4.deep.max_tool_calls", "max_nudges": "H4.deep.max_nudges",
             "max_truncated": "H4.deep.max_truncated"},
    "assistant": {"max_tool_calls": "H4.assistant.max_tool_calls"},
    "bot": {"max_tool_calls": "H4.bot.max_tool_calls", "max_nudges": "H4.bot.max_nudges"},
}
_ELIDE = {"deep": ("H5.deep.keep_reads", "H5.deep.stub_over"),
          "assistant": ("H5.assistant.read_keep_calls", "H5.assistant.read_stub_over")}
_H6 = ("history_budget", "keep_turns", "keep_budget", "hard_cap", "summary_max_tokens")
_CONTEXT = ("context_budget", "selection_max", "paragraph_max", "top_k_pages")


def _set(pack: HarnessPack, names: Mapping[str, str]) -> dict[str, Any]:
    return {f: v for f, n in names.items() if (v := pack.get(n)) is not None}


def limits(pack: HarnessPack, agent: Agent, base: LoopLimits) -> LoopLimits:
    changes = _set(pack, _LIMITS[agent])
    return replace(base, **changes) if changes else base


def elide(pack: HarnessPack, agent: Literal["deep", "assistant"],
          base: ElidePolicy) -> ElidePolicy:
    keep, over = _ELIDE[agent]
    changes = _set(pack, {"keep_turns": keep, "stub_over": over})
    return replace(base, **changes) if changes else base


def check_compact(values: Mapping[str, int], base: CompactPolicy) -> CompactPolicy:
    try:
        return replace(base, **values)
    except ValueError as exc:
        raise HarnessInvalid(f"压缩设置互相矛盾：{exc}") from exc


def compact_policy(pack: HarnessPack, base: CompactPolicy) -> CompactPolicy:
    changes = _set(pack, {f: f"H6.{f}" for f in _H6})
    return check_compact(changes, base) if changes else base


@dataclass(frozen=True)
class AssistantContext:
    context_budget: int
    selection_max: int
    paragraph_max: int
    top_k_pages: int


def assistant_context(pack: HarnessPack, base: AssistantContext) -> AssistantContext:
    changes = _set(pack, {f: f"H5.assistant.{f}" for f in _CONTEXT})
    return replace(base, **changes) if changes else base


def max_rewrites(pack: HarnessPack, default: int) -> int:
    return pack.int("H4.deep.max_rewrites", default)


class _Described:
    """A tool with other description text; runs the original. Everything else is the original's
    (`resume` of a pausing tool included: a reworded ask_user is still resumed by the reply), so an H2
    text never changes what a tool does (2026-10-28 audit, constraint 5)."""

    def __init__(self, inner: Tool, spec: ToolSpec):
        self._inner = inner
        self.spec = spec

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return self._inner.run(args, ctx)

    def __getattr__(self, name: str) -> Any:
        if name == "_inner":            # not set yet (copy / unpickle): no recursion
            raise AttributeError(name)
        return getattr(self._inner, name)


def describe(pack: HarnessPack, agent: Agent, tools: Sequence[Tool]) -> Sequence[Tool]:
    prefix = f"H2.{agent}."
    texts = pack.with_prefix(prefix)
    if not texts:
        return tools
    by_name = {t.spec.name: t for t in tools}
    per_tool: dict[str, dict[str | None, str]] = {}
    for name, text in texts.items():
        tool, _, arg = name[len(prefix):].partition(".")
        t = by_name.get(tool)
        if t is None:
            continue            # a tool this run does not offer (the name was checked when stored)
        if arg and arg not in (t.spec.parameters.get("properties") or {}):
            raise HarnessInvalid(f"{name}：这个工具没有这个参数")
        per_tool.setdefault(tool, {})[arg or None] = str(text)
    out = []
    for t in tools:
        texts_t = per_tool.get(t.spec.name)
        if not texts_t:
            out.append(t)
            continue
        params = copy.deepcopy(t.spec.parameters)
        for arg, text in texts_t.items():
            if arg is not None:
                params["properties"][arg]["description"] = text
        spec = t.spec.model_copy(update={"description": texts_t.get(None, t.spec.description),
                                         "parameters": params})
        out.append(_Described(t, spec))
    return tuple(out)


def pack_for_run(gw, user_id: str) -> HarnessPack:
    """The pack for one agent run. Inside a durable workflow the read is a recorded step, so a
    recovered run uses the pack it started with."""
    if not durable.in_workflow():
        return gw.harness_for(user_id)
    out = durable.step("harness.pack", lambda: _dump(gw.harness_for(user_id)))
    return HarnessPack(out["version"], out["values"])


def _dump(p: HarnessPack) -> dict:
    return {"version": p.version, "values": dict(p.values)}


# --- Structural keys (phase 3a): H3 tool toggles, H10 step tiers / thinking, H9 / H11 hook steps,
# --- H8 skills. Each returns its input object unchanged when the pack sets nothing for it.

def toolset(pack: HarnessPack, agent: Agent, tools: Sequence[Tool]) -> Sequence[Tool]:
    """The tools minus those the pack turns off (H3). Returns `tools` itself when the pack turns
    nothing off among them. Locked tools have no key, so they can never be off."""
    toggle = TOGGLE_TOOLS.get(agent, ())
    off = {t.spec.name for t in tools
           if t.spec.name in toggle and not pack.flag(f"H3.{agent}.{t.spec.name}", True)}
    return tools if not off else tuple(t for t in tools if t.spec.name not in off)


def _tier(pack: HarnessPack, step: str) -> str | None:
    if step not in STEPS_H10:
        raise KeyError(step)
    return pack.choice(f"H10.{step}.tier", None)


def forge_limits(pack: HarnessPack, level: Literal["tool", "patch"], cap: int) -> int:
    """The forge coder's tool-call cap: the pack's H4.forge.* may only lower `cap` (the forge's
    anchor, forge/limits.py)."""
    name = "H4.forge.max_tool_calls" if level == "tool" else "H4.forge.patch.max_tool_calls"
    return min(cap, pack.int(name, cap))


def forge_tier(pack: HarnessPack, level: Literal["tool", "patch"]) -> str | None:
    """H10 forge.code: the tier the pack picked for forging, None = the forge's default
    (tool -> good, patch -> deep). `level` is accepted for symmetry; one key covers both."""
    return _tier(pack, "forge.code")


def chain(pack: HarnessPack, step: Literal["deep.plan", "deep.write"],
          roster: Mapping[str, Sequence[Any]]) -> Sequence[Any]:
    """The writer chain for a deep step (H10): roster["deep"] (the same object) when unset, else
    roster[tier]. A tier the roster lacks is HarnessInvalid; the message carries the detail and
    the agent maps it to the plain user text."""
    tier = _tier(pack, step) or "deep"
    got = roster.get(tier)
    if got is None:
        raise HarnessInvalid(f"手册里给「{step}」选的模型档现在用不了（档位 {tier}）")
    return got


def account_for(pack: HarnessPack, step: Literal["assistant.turn", "bot.plan"],
                base: Account | None) -> Account | None:
    """base (same object) when unset or base is None; else base with the H10 tier pin."""
    tier = _tier(pack, step)
    return base if tier is None else account_for_tier(base, tier)


def thinking_params(pack: HarnessPack, step: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """params (same object) when H10.<step>.thinking is unset; else a copy with THINKING_PARAM."""
    if step not in STEPS_H10:
        raise KeyError(step)
    on = pack.get(f"H10.{step}.thinking")
    if on is None:
        return params  # type: ignore[return-value]
    return {**params, THINKING_PARAM: bool(on)}


def hook_steps(pack: HarnessPack, at: str) -> tuple[HookStep, ...]:
    """The H9 / H11 steps at a hook point; () when unset."""
    hook = HOOKS.get(at)
    if hook is None:
        raise KeyError(at)
    key = f"{'H9' if 'delegate' in hook.kinds else 'H11'}.{at}"
    value = pack.items(key)
    return () if value is None else parse_steps(key, value)


def check_hook_tools(pack: HarnessPack, agent: Agent, steps: Sequence[HookStep]) -> None:
    """Fail closed before any model call: a hook step that names a tool the pack switches off
    (H3) is the HarnessInvalid run_hook would raise later, after the plan call or the turn setup."""
    off = {n for n in TOGGLE_TOOLS.get(agent, ()) if not pack.flag(f"H3.{agent}.{n}", True)}
    if not off:
        return
    for s in steps:
        check_tools(s, set(s.tools) - off)


def spec_parts(pack: HarnessPack, step: Literal["deep.plan", "deep.write"],
               default: tuple[str, ...]) -> tuple[str, ...]:
    """The teaching-standard parts a deep step reads first (H8); `default` when unset."""
    value = pack.items(f"H8.deep.spec.{step.split('.')[1]}")
    return default if value is None else tuple(value)


def components(pack: HarnessPack, comps: Sequence[Any]
               ) -> tuple[Mapping[str, str], Mapping[str, tuple[Preset, ...]]]:
    """(notes, presets) for the catalog (H8), keyed by component id. Fail closed: a note or preset
    of a component not in `comps`, a preset of a fixed-content component, or a preset config that
    does not validate (the check place_component runs) is HarnessInvalid."""
    stored = pack.with_prefix("H8.component.")
    if not stored:
        return {}, {}
    # function-level import: deep.agent imports harness.apply, so a module-level import is a cycle
    from evowiki.deep.components import ConfigInvalid, instantiate
    by_id = {c.component_id: c for c in comps}
    notes: dict[str, str] = {}
    presets: dict[str, tuple[Preset, ...]] = {}
    for name, value in stored.items():
        cid, _, what = name[len("H8.component."):].rpartition(".")
        c = by_id.get(cid)
        if c is None:
            raise HarnessInvalid(f"手册里给组件 {cid} 写的内容，组件库里已经没有这个组件了")
        if what == "note":
            notes[cid] = str(value)
            continue
        if c.config_schema is None:
            raise HarnessInvalid(f"手册里给组件 {cid} 加了预设，但它是固定内容的组件")
        items = parse_presets(value)
        for p in items:
            try:
                instantiate(c, p.config)
            except ConfigInvalid as e:
                raise HarnessInvalid(f"组件 {cid} 的预设「{p.name}」配置不合法") from e
        presets[cid] = items
    return notes, presets


def evoui_examples(pack: HarnessPack) -> tuple[dict[str, Any], ...]:
    """Extra EvoUI templates [{name, use, block, group}] (H8); each block must pass the EvoUI validator,
    else HarnessInvalid. () when unset."""
    value = pack.items("H8.deep.evoui_examples")
    if value is None:
        return ()
    # function-level import: deep.agent imports harness.apply, so a module-level import is a cycle
    from evowiki.deep.evoui import validate
    out = []
    for e in parse_examples(value):
        problems = validate(e.block)
        if problems:
            raise HarnessInvalid(f"示例「{e.name}」：块不合法：" + "；".join(problems[:8]))
        out.append({"name": e.name, "use": e.use, "block": e.block,
                    **({"group": e.group} if e.group else {})})
    return tuple(out)


def account_for_tier(base: Account | None, tier: str) -> Account | None:
    """The account a hook step's model call is metered and routed on: base with the step's tier
    pinned (fast / good; the user's own group choice still wins in usage_control.tier_for)."""
    if base is None:
        return None
    return base.model_copy(update={"tier_pin": tier})
