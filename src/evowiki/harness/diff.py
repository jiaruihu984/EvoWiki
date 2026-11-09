"""A candidate change to a user's harness pack (design 2026-10-21 §4–§5): key operations (H1–H5,
H8–H12) and teaching-spec operations (H7). Pure: nothing here reads or writes a store. `refuse` is
the improver's permission check, `complexity` the price the gate charges for a candidate,
`needs_ask` whether the user must approve it (any key whose approval is "ask", keys.needs_approval),
`cost_keys` whether it changes what a run costs (an H4 or H10 key).

Stored form (`harness_change.diff`, `diff_to_json` / `diff_from_json`):
  {"ops": [{"name": ..., "value": ..., "before": ...}],      # before None = the key was at its default
   "spec_ops": [SpecPatchOp / ParamPatchOp dumps],            # H7 (told apart by "op")
   "spec_before_version": int | null}                         # the teaching-spec version the spec ops
                                                              # were applied on (undo restores from it)
The `before` values and the spec version are what undo needs; the round writes them."""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, TypeAdapter

from evowiki.harness.anchors import H1_CLOSED_STEPS, H4_MAX_RAISE, IMPROVER_AGENTS, IMPROVER_DIMS
from evowiki.harness.keys import (
    Key,
    h2_key,
    h12_tool_key,
    is_forged_opt_in,
    key_of,
    needs_approval,
)
from evowiki.harness.pack import HarnessInvalid, HarnessPack, Value, validate
from evowiki.harness.tool_names import is_builtin
from evowiki.llm.prompt_steps import STEP_BY_KEY
from evowiki.teach.contracts import ParamPatchOp, SpecPatchOp
from evowiki.teach.spec_ops import body_key

# an H7 op: a rule op, or a RepParams value (`set_param`, track "B": the teaching spec's params)
SpecOp = Annotated[SpecPatchOp | ParamPatchOp, Field(discriminator="op")]
_SPEC_OP = TypeAdapter(SpecOp)


@dataclass(frozen=True)
class KeyOp:
    name: str
    value: Value | None          # None = back to the default


@dataclass(frozen=True)
class PackDiff:
    ops: tuple[KeyOp, ...] = ()
    spec_ops: tuple[SpecPatchOp | ParamPatchOp, ...] = ()     # H7


def apply_diff(pack: HarnessPack, diff: PackDiff) -> HarnessPack:
    """The pack with the key ops applied (version unchanged; every value validated)."""
    values: dict[str, Value] = dict(pack.values)
    for op in diff.ops:
        if op.value is None:
            values.pop(op.name, None)
        else:
            values[op.name] = validate(op.name, op.value)
    return HarnessPack(pack.version, values)


def complexity(base: HarnessPack, diff: PackDiff, defaults: Mapping[str, Value]) -> float:
    """Added characters / 100 + number of key ops + number of spec ops. Same scale as the teaching
    spec's complexity (teach/contracts.TeachingSpec.complexity: one unit per item / moved param),
    with the added text priced on top. Added characters of a text op = how much longer the new text
    is than the current effective value (pack value, else default); of a steps / json op (H8 / H9 /
    H11 / H12) = how much longer its JSON dump is than the current value's dump (none when unset);
    int / bool / enum / list ops add none; a spec op counts its whole text (a set_param none)."""
    chars = 0
    for op in diff.ops:
        cur = base.values.get(op.name, defaults.get(op.name))
        if isinstance(op.value, str):
            chars += max(0, len(op.value.strip()) - (len(cur) if isinstance(cur, str) else 0))
        elif (op.value is not None and (k := key_of(op.name)) is not None
              and k.kind in ("steps", "json")):
            chars += max(0, len(_dump(op.value)) - (len(_dump(cur)) if cur is not None else 0))
    chars += sum(len(s.text or "") for s in diff.spec_ops if isinstance(s, SpecPatchOp))
    return chars / 100 + len(diff.ops) + len(diff.spec_ops)


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


# keys without an agent segment: H1 / H6 / H7 (spec ops), H8.component.*, H12.patch.*
_NO_AGENT = ("H1", "H6", "H7")
NO_PATCH = "代码改动不走运行设置"               # H12.patch: the forge's own review flow asks for it
ONE_FORGED = "一次只能加一个自己造的工具"
NO_REGISTRY_VERSION = "注册表版本由系统写，不能由改进者给"
RSI_STEP = "是自我改进自己的评判或方法，只能由你在 Prompt 设置里改"


def closed_h1(name: str) -> bool:
    """True for an H1 key of a step the improver may never change (anchors.H1_CLOSED_STEPS)."""
    return name.startswith("H1.") and name[3:] in H1_CLOSED_STEPS


def _agent(k: Key, name: str) -> str | None:
    """The agent a key belongs to, None for a key that has none."""
    parts = name.split(".")
    if k.dim in _NO_AGENT or name.startswith("H8.component."):
        return None
    if k.dim == "H12":
        return parts[2] if parts[1] == "tool" else None
    return parts[1]


def _is_h12_tool(name: str) -> bool:
    return name.startswith("H12.tool.")


# Harness 3b follow-up 3 (rule R1): the assistant keys an assistant-turn replay
# (harness/replay_assistant.py) really rebuilds. Every other assistant key, and H6 (the assistant's
# history compaction: no other agent reads it), is built into the turn's context or history from
# wiki state the trace does not keep, so a change to it cannot be measured.
UNMEASURABLE = "重放测不出这个设置的效果，不能自动改"
ASSISTANT_MEASURED = frozenset({"H4.assistant.max_tool_calls", "H5.assistant.read_keep_calls",
                                "H5.assistant.read_stub_over"})
CLOSED = "所属的功能不能自动改"


def assistant_refusal(op: KeyOp, k: Key) -> str | None:
    """The refusal of an assistant-agent op (or an H6 op) under rule R1, None when a replay
    measures it. Allowed: H2 texts of a built-in tool / argument, a built-in tool switched off (H3 False only),
    H4 max_tool_calls, H5 read_keep_calls / read_stub_over. A forged assistant tool (its H2 texts,
    its H3 opt-in, its H12 record) and switching a built-in tool on stay closed; everything else is UNMEASURABLE.
    (H1 has no agent segment: `H1.assistant` is open like every H1 step.)"""
    name = op.name
    if k.dim == "H2":
        # a forged tool's texts: its default is not in the code, so a replay cannot rebuild it
        return None if is_builtin("assistant", name.split(".")[2]) else f"{name} {CLOSED}"
    if name in ASSISTANT_MEASURED:
        return None
    if k.dim == "H3":
        if is_forged_opt_in(name):
            return f"{name} {CLOSED}"
        if op.value is False:
            return None
        return f"{name}：助手的工具只能自动关掉，不能自动打开"
    if k.dim == "H12":
        return f"{name} {CLOSED}"
    return f"{name}：{UNMEASURABLE}"


def unmeasured(op: KeyOp) -> str | None:
    """The R1 refusal of `op` when it is an assistant key or an H6 key an assistant replay does
    not rebuild (None for every other op, unknown names included: `refuse` reports those)."""
    k = key_of(op.name)
    if k is None:
        return None
    if k.dim == "H6":
        return f"{op.name}：{UNMEASURABLE}"
    return assistant_refusal(op, k) if _agent(k, op.name) == "assistant" else None


def refuse(diff: PackDiff, pack: HarnessPack, sources: Mapping[str, str],
           *, defaults: Mapping[str, Value], registered: Collection[str] = ()) -> str | None:
    """None when the improver may apply `diff`, else the plain reason. `sources` maps a key to who set
    it ("user" keys are never touched). `defaults` gives the effective value of an unset H4 key, for
    the raise limit; without it an unset key has no limit to compare to. `registered`: the deep
    forged tools in the user's pinned forge registry view right now (none without a forge, or with
    its tool switch off); the caller reads it.

    Per op, in order: the dim must be one the improver may change (IMPROVER_DIMS); an H1 key of an
    RSI judge / review / method step (anchors.H1_CLOSED_STEPS) is never changed (RSI_STEP); a code patch
    (H12.patch) is never a harness change (NO_PATCH: the forge's own review flow asks for it); the
    forge's own keys (H4.forge.*, H10.forge.*) are out of reach; a key that belongs to an agent
    (its second dotted segment, the third for H12.tool) must belong to one in IMPROVER_AGENTS, and
    an assistant key (or an H6 key) must be one an assistant replay measures (`assistant_refusal`);
    user-set keys are never touched; the value must validate, and an H4 key may rise at most
    H4_MAX_RAISE × (back to the default included). Then for the whole diff (`_refuse_forged`): at
    most one forged tool (H12.tool op), every forged-tool opt-in switched on either comes with its
    H12.tool op (a candidate built for it: a new tool, or one replacing the registered tool of that
    name) or names a deep tool in `registered` (switching an existing tool back on: no H12 op, no
    promote), and the improver never gives a registry version."""
    if not diff.ops and not diff.spec_ops:
        return "没有任何改动"
    for op in diff.ops:
        k = key_of(op.name)
        if k is None:
            return f"没有这个设置：{op.name}"
        if k.dim == "H6":                 # the assistant's compaction (R1), named on its own
            return f"{op.name}：{UNMEASURABLE}"
        if k.dim not in IMPROVER_DIMS:
            return f"{op.name} 不在可以自动改的范围内"
        if closed_h1(op.name):
            return f"{op.name} {RSI_STEP}"
        if op.name.startswith("H12.patch."):
            return f"{op.name}：{NO_PATCH}"
        if k.dim in ("H4", "H10") and op.name.split(".")[1] == "forge":
            return f"{op.name} 不在可以自动改的范围内"
        agent = _agent(k, op.name)
        if agent is not None and agent not in IMPROVER_AGENTS:
            return f"{op.name} {CLOSED}"
        if agent == "assistant" and (why := assistant_refusal(op, k)) is not None:
            return why
        if sources.get(op.name) == "user":
            return f"{op.name} 是你自己设的，不会被自动改"
        if op.value is None and k.dim != "H4":
            continue
        if op.value is None:
            # back to the default: the same raise rule, against the default it falls back to
            value = defaults.get(op.name)
            if value is None:
                return f"{op.name} 的默认值不明，没法判断能不能改回默认"
        else:
            try:
                value = validate(op.name, op.value)
            except HarnessInvalid as e:
                return str(e)
        if k.dim == "H4":
            cur = pack.values.get(op.name, defaults.get(op.name))
            if not isinstance(cur, int):
                return f"{op.name} 现在的值不明，没法判断能不能提高"
            if value > cur * H4_MAX_RAISE:  # type: ignore[operator]
                return f"{op.name} 一次最多提高到现在的 {H4_MAX_RAISE} 倍"
    return _refuse_forged(diff, pack, registered)


def _refuse_forged(diff: PackDiff, pack: HarnessPack, registered: Collection[str]) -> str | None:
    """The forged-tool pairing rules (phase 3b P4). Fail closed: an opt-in switched on needs an
    H12.tool op that names a candidate (a None value does not) unless it is a deep tool in
    `registered` (harness 3b follow-up 1: an existing tool switched back on), and an H12.tool op
    back to None is refused while the tool's opt-in stays on in the pack after the diff."""
    h12 = [o for o in diff.ops if _is_h12_tool(o.name)]
    if len(h12) > 1:
        return ONE_FORGED
    if any(isinstance(o.value, dict) and "registry_version" in o.value for o in h12):
        return NO_REGISTRY_VERSION
    bad = unpaired(diff, registered)
    if bad:
        return f"{bad[0]} 要和它的 H12 候选一起改"
    after = {o.name: o.value for o in diff.ops}
    for op in h12:
        if op.value is None:
            _, _, agent, tool = op.name.split(".")
            opt = f"H3.{agent}.{tool}"
            if after.get(opt, pack.values.get(opt)) is True:
                return f"{op.name} 去掉时要同时关掉 {opt}"
    return None


def alone_on(diff: PackDiff) -> tuple[tuple[str, str, str], ...]:
    """(key, agent, tool) of every forged-tool opt-in `diff` switches on without an H12.tool op
    naming a candidate for it (a None H12 value names none), in op order."""
    named = {o.name for o in diff.ops if _is_h12_tool(o.name)
             and isinstance(o.value, dict) and o.value.get("candidate_id")}
    out: list[tuple[str, str, str]] = []
    for op in diff.ops:
        if op.value is True and is_forged_opt_in(op.name):
            _, agent, tool = op.name.split(".")
            if h12_tool_key(agent, tool) not in named:
                out.append((op.name, agent, tool))
    return tuple(out)


def unpaired(diff: PackDiff, registered: Collection[str]) -> tuple[str, ...]:
    """The pairing rule (phase 3b P4, harness 3b follow-up 1), the one place it lives: the opt-in
    keys `diff` switches on alone (`alone_on`) that are not a deep tool in `registered`. `refuse`
    names the first; the round's apply reads a non-empty result as the tool gone."""
    return tuple(key for key, agent, tool in alone_on(diff)
                 if not (agent == "deep" and tool in registered))


# The agents a replay case can belong to (`replay.Case.kind`), and the purpose prefix of each.
CASE_AGENTS: tuple[str, ...] = ("deep", "assistant")
_PURPOSE_AGENT = {"deep.": "deep", "assistant.": "assistant"}


def _step_agents(step_key: str) -> frozenset[str]:
    """The case agents whose runs an H1 step's guidance reaches (by its purposes, and the purpose
    whose prompt carries it: `llm.prompt_steps`); a step used by both → both."""
    step = STEP_BY_KEY.get(step_key)
    if step is None:
        return frozenset()
    return frozenset(agent for purpose in (*step.purposes, step.source)
                     for prefix, agent in _PURPOSE_AGENT.items() if purpose.startswith(prefix))


def agents_touched(diff: PackDiff) -> frozenset[str]:
    """The case agents (CASE_AGENTS) whose replays `diff` can change, which the round's final
    probe replays (harness 3b follow-ups, #11): a key of the deep agent, a teaching-spec op (H7),
    an H8 component key, H6 (refused anyway) and an H1 step of the deep writer → "deep"; a key of
    the assistant and the H1 step `assistant` → "assistant". An op that reaches neither (an H1
    step of another model step, another agent's key: refused anyway) touches both, so nothing is
    left out by mistake."""
    out: set[str] = {"deep"} if diff.spec_ops else set()
    for op in diff.ops:
        k = key_of(op.name)
        if k is None:
            out.update(CASE_AGENTS)
            continue
        if k.dim == "H1":
            hit = _step_agents(op.name[3:])
        elif k.dim == "H6" or op.name.startswith("H8.component."):
            hit = frozenset({"deep"})
        else:
            agent = _agent(k, op.name)
            hit = frozenset({agent}) if agent in CASE_AGENTS else frozenset()
        out.update(hit or CASE_AGENTS)
    return frozenset(out)


def needs_ask(diff: PackDiff) -> bool:
    """True when any key op of `diff` must be approved by the user (keys.needs_approval)."""
    return any(needs_approval(o.name) for o in diff.ops)


def cost_keys(diff: PackDiff) -> bool:
    """True when `diff` changes an H4 or H10 key (its change carries the replay costs)."""
    return any((k := key_of(o.name)) is not None and k.dim in ("H4", "H10") for o in diff.ops)


def h12_tool(diff: PackDiff) -> tuple[str, str, str] | None:
    """(agent, tool, candidate id) of the diff's forged tool (its H12.tool op), None without one."""
    for o in diff.ops:
        if _is_h12_tool(o.name) and isinstance(o.value, dict) and "candidate_id" in o.value:
            _, _, agent, tool = o.name.split(".")
            return agent, tool, str(o.value["candidate_id"])
    return None


def tool_only(diff: PackDiff) -> bool:
    """`diff` is one forged tool going live and nothing else: its H12.tool op, its own H3 opt-in and
    its own H2 descriptions. It goes live on its own and the user is told (ruling 2026-11-04)."""
    t = h12_tool(diff)
    if t is None or diff.spec_ops:
        return False
    agent, tool, _ = t
    desc = h2_key(agent, tool)
    return all(o.name in (h12_tool_key(agent, tool), f"H3.{agent}.{tool}", desc)
               or o.name.startswith(f"{desc}.") for o in diff.ops)


def h12_patch(diff: PackDiff) -> str | None:
    """The candidate id of the diff's code patch (its H12.patch op), None without one."""
    return next((o.name.removeprefix("H12.patch.") for o in diff.ops
                 if o.name.startswith("H12.patch.")), None)


def op_key(op: KeyOp) -> str:
    """The identity of a key op (the same key set to the same value again), for the improver's
    ban on re-proposing a refused change; a spec op's is `teach.spec_ops.body_key`."""
    return f"key|{op.name}|{json.dumps(op.value, ensure_ascii=False, sort_keys=True)}"


def refused_keys(diffs: Collection[Mapping]) -> frozenset[str]:
    """The op keys (`op_key`, `spec_ops.body_key`) of stored diffs (`diff_to_json` forms): what
    the improver may not propose again when they are the diffs of refused changes."""
    out: set[str] = set()
    for raw in diffs:
        d = diff_from_json(raw)[0]
        out.update(op_key(o) for o in d.ops)
        out.update(body_key(s) for s in d.spec_ops)
    return frozenset(out)


def diff_to_json(diff: PackDiff, before: Mapping[str, Value | None],
                 spec_before_version: int | None = None) -> dict:
    """The stored form of `diff`; `before` maps each op's key to its value before the change."""
    return {"ops": [{"name": o.name, "value": o.value, "before": before.get(o.name)}
                    for o in diff.ops],
            "spec_ops": [s.model_dump(mode="json") for s in diff.spec_ops],
            "spec_before_version": spec_before_version}


def diff_from_json(raw: Mapping) -> tuple[PackDiff, dict[str, Value | None], int | None]:
    """(the diff, each key's value before it, the spec version before it) from the stored form."""
    ops = raw.get("ops") or []
    diff = PackDiff(tuple(KeyOp(o["name"], o["value"]) for o in ops),
                    tuple(_SPEC_OP.validate_python(s) for s in raw.get("spec_ops") or []))
    return diff, {o["name"]: o.get("before") for o in ops}, raw.get("spec_before_version")
