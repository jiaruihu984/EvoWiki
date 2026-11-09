"""The improver agent (design 2026-10-21 §4, §5.2–§5.6, §6.3), modelled on Harness²: it reads the
round's signals and the traces behind them, checks the experience book, proposes two pack diffs (one
general, one specific), probes both on the same cases in the sandbox (Task 3), asks the judge
(Task 4) and composes a choice that cites what it saw. The procedure around it is fixed: the
anchors, the judge prompt and the gate rules are not reachable from any tool here, and the round
(not the improver) runs the final replays and the gate.

Tools (`ImproverRun.tools`; descriptions are plain Chinese for the model):
  read_signals, read_trace, read_pack, read_experience  — reads;
  propose(kind, ops, spec_ops, rationale, citations)     — records a candidate (general / specific,
                                                            or a `composed` merge to probe);
  probe(candidate, case_ids)                             — sandboxed replays; the current pack's
                                                            replay of a case runs once and is the
                                                            reference; every replay counts,
                                                            a failed one too (it is not retried);
  read_probe(case_id, kind, offset)                      — a probed text in full;
  compare(case_id, x, y)                                 — the judge on two probed texts;
  compose(decision, ops, spec_ops, citations, rationale) — the final choice; ends the run. With `ImproverDeps.evals`,
                                                            a compose while a candidate is unmeasured does not end it:
                                                            the code runs them on the evaluation set and returns the
                                                            board, and the model composes again, or proposes and is
                                                            measured again (GEPA's minibatch gate: measuring is the
                                                            procedure's job, 6 of 9 runs did not when it was optional);
  run_evals(candidate)                                   — only with `ImproverDeps.evals`: real turns under the
                                                            current pack or a candidate on the evaluation set, graded
                                                            by code: per case pass / fail and why (MAX_EVALS per run);
  propose_patch(goal, title, failing_case, paths_hint, citations)
                                                         — only when `ImproverDeps.patch` is set and
                                                            the forge's patch switch is on (off by
                                                            default): records one code-patch request
                                                            (at most one per run); the round builds,
                                                            evaluates and hands it to the user's
                                                            review after its key path;
  forge_tool(name, title, purpose, examples, citations)  — only when `ImproverDeps.forge` is set and
                                                            the forge's tool switch is on: builds one
                                                            tool for the page writer (at most
                                                            FORGE_PER_ROUND per run). A built one
                                                            joins the forged tools read_pack lists;
                                                            using it is a key change like any other
                                                            (its H3 opt-in + its H12.tool record),
                                                            and only a forged tool the registry has
                                                            or this run built may be named. A tool
                                                            already in the registry is switched on
                                                            or off by its H3 opt-in alone (no H12
                                                            op, nothing promoted).

Citations (`propose`, `compose`): at least one, and every id must exist in this run:
`signal:<id>` (the round's signals), `trace:<run_id>` (the cases' runs, runs read, probe runs),
`cmp:<…>` (comparisons made in this run). Otherwise the tool returns an error to the model.

Comparison orientation: the side that is not "current" is `a` (judge.compare's candidate side);
when two candidates are compared, `x` is `a`. Texts by different writers are a tool error.

Ending: `compose` → its Composition. The tool cap (IMPROVER_MAX_TOOL_CALLS) or a model that stops
without composing → Composition(decision="none") with a plain reason; never an exception. The
replay cap (MAX_PROBES) limits replays, not the run: a probe request that would go past it is
refused whole (nothing spent) as a tool error, and the model can still compare and compose. A model, provider, judge-routing or store error raises
(fail closed): a tool that hits one stops the run and the error is raised after the loop.

A probe's replays run at once (`replay.run_all`) in two waves: the current pack's references, then
the candidate on the cases whose reference was written (a candidate is never replayed without one
to compare to). Their results, failures, costs and trace runs are recorded in the order the probe
listed them, as one at a time would."""

import functools
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, get_args

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from evowiki.agent.loop import run_loop
from evowiki.agent.spec import AgentSpec, LoopLimits, RequestState
from evowiki.agent.tools import ToolCategory, ToolContext, ToolResult, ToolSpec
from evowiki.agent.trace import NO_TRACE, NotReplayable, TraceSink, finish_failed
from evowiki.deep.library import ComponentLibrary
from evowiki.forge.models import ForgeDisabled, ForgeMisconfigured, PatchRequest, ToolRequest
from evowiki.harness import experience
from evowiki.harness import judge as judge_mod
from evowiki.harness.anchors import (
    IMPROVER_DIMS,
    IMPROVER_MAX_TOOL_CALLS,
    MAX_PROBES,
)
from evowiki.harness.cases import CaseSet
from evowiki.harness.diff import KeyOp, PackDiff, assistant_refusal, closed_h1, op_key, refuse
from evowiki.harness.forging import FORGE_PER_ROUND, candidate_schema, forge_tool, tool_docs
from evowiki.harness.improver_prompt import NUDGE, PROMPT_VERSION, PURPOSE, REFUSED, SYSTEM
from evowiki.harness.judge import Comparison, JudgeConfigs
from evowiki.harness.keys import (
    HOOK_MAX_TOOL_CALLS,
    HOOK_TEXT_MAX,
    HOOKS,
    KEYS,
    LOCKED_TOOLS,
    SKILL_NAME_MAX,
    SKILL_USE_MAX,
    TOGGLE_TOOLS,
    Key,
    h2_key,
    h12_tool_key,
    is_forged_opt_in,
    key_of,
)
from evowiki.harness.leak import CaseWords, leaks
from evowiki.harness.pack import HarnessInvalid, HarnessPack, Value
from evowiki.harness.patching import PatchProposal
from evowiki.harness.replay import (
    Case,
    ReplayDeps,
    ReplayResult,
    candidate_spec,
    replay,
    run_all,
)
from evowiki.harness.snapshot import SnapshotBoundary
from evowiki.harness.tool_names import RESERVED, TOOLS, is_builtin
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn, ToolDef
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import LLMCall, ModelRef
from evowiki.llm.usage import Account
from evowiki.rules.authority import RuleAuthorityError
from evowiki.rules.authority import check as authority_check
from evowiki.rules.decisions import NO_DECISIONS, Decisions
from evowiki.store import trace as store_trace
from evowiki.teach.contracts import (
    CHIP_LABEL,
    ParamPatchOp,
    RepParams,
    Signal,
    SpecPatchOp,
    TeachingSpec,
    rsi_may_change,
)
from evowiki.teach.rsi_store import MemoryRecord
from evowiki.teach.spec_ops import PARAM_ZH, PatchError, body_key
from evowiki.wikiapp.refine_service import ReplayFailed

if TYPE_CHECKING:
    from evowiki.harness.forging import ForgeHooks
    from evowiki.harness.patching import PatchHooks
    from evowiki.rules.guard import GuardResult

log = logging.getLogger("evowiki.harness.improver")

AGENT = "improver"
CLIP = 8000                     # chars of one read result the model gets at a time
PACK_CLIP = 11500               # read_pack: a row past the first page was never read, and what it did not read it did not change
ANSWER_CLIP = 800               # the end of a traced turn: what the learner saw and rated
SAID_CLIP = 600
MAX_EVALS = 5                   # evaluation runs of one improver run (the reference counts): each is minutes of real turns
EVAL_ANSWER_CLIP = 300
FAILED_PATCH = frozenset({"gate_failed", "rejected", "rolled_back"})

Kind = Literal["general", "specific"]
Decision = Literal["a", "b", "merge", "none"]
Probed = Literal["current", "general", "specific", "composed"]

NO_COMPOSE = "改进者没有交卷就停下了，这一轮不改"
VERDICT_ZH = {"effective": "有效", "ineffective": "无效", "harmful": "有害"}
STATUS_ZH = {"gate_failed": "没通过比较", "rejected": "被拒绝", "rolled_back": "上线后撤回"}
KIND_ZH = {"general": "通用候选", "specific": "针对性候选", "composed": "合并候选",
           "current": "现在的手册"}


@dataclass(frozen=True)
class Candidate:
    kind: Kind
    diff: PackDiff
    rationale: str
    citations: tuple[str, ...]


@dataclass(frozen=True)
class Composition:
    """The improver's result. `candidates`: (general, specific) as proposed, in that order; fewer
    when the run ended before both were proposed. `chosen`: the diff of the decision (a = the
    general candidate, b = the specific one, merge = the composed diff), None for "none".
    `probes`: every replay of the run that produced a text (references included); `cost`: the
    recorded cost of the improver's model calls, all its replays (failed ones too) and its judge
    calls (USD). A re-proposed kind drops its earlier probes and comparisons."""

    candidates: tuple[Candidate, ...]
    chosen: PackDiff | None
    decision: Decision
    citations: tuple[str, ...]
    rationale: str
    probes: tuple[ReplayResult, ...]
    comparisons: tuple[Comparison, ...]
    run_id: str
    cost: float
    # the code patch the improver asked for (`propose_patch`), built after the round's key path
    patch: PatchProposal | None = None


@dataclass(frozen=True)
class ImproverDeps:
    """What the improver's tools need; the round builds it.
    `gateway`: the improver's model (good tier, role generator), metered to `account` (group
    "rsi"); the judge calls are charged to `account` too. `replay`: the deep service + repo of the
    sandbox. `base_pack` / `sources` / `defaults`: the user's pack now (`store.harness.load_pack` /
    `source_of`) and `harness_defaults.defaults()`. `base_spec`: the teaching spec the candidates'
    H7 ops apply to. `judges` + `judge_gateway(ref)`: the judge configs and a gateway for a judge
    (the round passes `lambda ref: judge_gateway(deep_llm, ref, providers=judge_providers())`).
    `memory` / `failure`: the user's Patch Memory rows and the finding's failure class (read-only
    negative experience). `trace`: where the improver's run is recorded. `forged`: agent → the
    forged tools the registry lists for this user (read_pack shows the deep ones' H3 opt-in and
    H12.tool keys; none by default). `replay.assistant`: the assistant replay deps (assistant
    cases); `signal_runs`: the turn behind each assistant signal (read_signals shows it).
    `decisions`: the user's rule decisions read with `base_spec` (the proposal-time permission
    check, `rules.authority.check` actor rsi). `banned`: the op keys (`diff.op_key`) of the changes
    the user rejected or undid or that were rolled back (`harness_rsi.refused_changes`): a
    candidate with one of them is refused."""

    pool: ConnectionPool
    gateway: Gateway
    account: Account
    replay: ReplayDeps
    base_pack: HarnessPack
    sources: Mapping[str, str]
    defaults: Mapping[str, Value]
    base_spec: TeachingSpec
    judges: JudgeConfigs
    judge_gateway: Callable[[ModelRef], Gateway]
    signals: tuple[Signal, ...]
    now: datetime
    round_id: str
    snapshot: SnapshotBoundary | None = None
    memory: tuple[MemoryRecord, ...] = ()
    failure: str | None = None
    trace: TraceSink = NO_TRACE
    forged: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    components: Mapping[str, "ComponentInfo"] = field(default_factory=dict)
    # code patches (harness 3b): offered only with hooks and the forge's patch switch on
    patch: "PatchHooks | None" = None
    # forged tools (harness 3b, Task 7): `forge_tool` offered only with hooks and the forge's tool
    # switch on; `forge_binding()` gives the gateway + account the forge's calls go through (the
    # round: its recording gateway on the forge tier); None = `gateway` / `account`
    forge: "ForgeHooks | None" = None
    forge_binding: Callable[[], tuple[Gateway, Account]] | None = None
    # harness 3b follow-up 3: an assistant_rejected signal id -> the run id of its turn
    signal_runs: Mapping[str, str] = field(default_factory=dict)
    decisions: Decisions = NO_DECISIONS
    banned: frozenset[str] = frozenset()
    # the words only this round's cases and this learner say; a diff that uses them is refused
    words: CaseWords = field(default_factory=CaseWords)
    # the rule guard on what a diff adds (the round's step 5 asks it again on the composed diff);
    # asked when the improver proposes, so it can still change course
    guard: "Callable[[PackDiff], GuardResult] | None" = None
    # Evaluation under a diff: {cases: case id -> row, cost: recorded usage}; None = no run_evals.
    evals: Callable[[PackDiff], Mapping[str, Any] | None] | None = None


@dataclass(frozen=True)
class ComponentInfo:
    """One component of the writer's library as read_pack shows it: its description (what its H8
    note key replaces) and, for a configurable one, a compact summary of its config schema and an
    example config (`config` None = fixed content: it takes no presets)."""

    description: str
    config: str | None = None


COMPONENT_CONFIG_MAX = 1500       # chars of one component's schema + example summary


def component_infos(library: ComponentLibrary | None) -> dict[str, ComponentInfo]:
    """`ImproverDeps.components` from the writer's library (none without one)."""
    if library is None:
        return {}
    out: dict[str, ComponentInfo] = {}
    for c in library.list():
        config = None
        if c.config_schema is not None:
            example = _dump(c.example_configs[0]) if c.example_configs else "（无）"
            config = f"配置格式（JSON Schema）：{_dump(c.config_schema)}；例子：{example}"
            if len(config) > COMPONENT_CONFIG_MAX:
                config = config[:COMPONENT_CONFIG_MAX] + "…"
        out[c.component_id] = ComponentInfo(c.description, config)
    return out


# --- tool argument shapes ---------------------------------------------------------------------

class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _KeyOpIn(_Args):
    name: str = Field(min_length=1, max_length=120)
    value: Any = None            # any JSON; diff.refuse validates it against the key

    @field_validator("value")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v


ParamPath = Literal["recap_max_per_page", "analogy_level", "fading_level", "interactive_bias"]


class _SpecOpIn(_Args):
    op: Literal["add_item", "edit_item", "remove_item", "set_param"]
    item_id: str | None = Field(default=None, max_length=80)
    text: str | None = None
    scope: str = "any"
    domain: str | None = None
    path: ParamPath | None = None          # set_param only
    value: int | None = None               # set_param only


class _TraceArgs(_Args):
    run_id: str = Field(min_length=1, max_length=200)
    seq: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)


PackDim = Literal["H1", "H2", "H3", "H4", "H5", "H7", "H8", "H9", "H10", "H11", "H12"]


class _PackArgs(_Args):
    dim: PackDim | None = None
    tool: str | None = Field(default=None, max_length=80)
    offset: int = Field(default=0, ge=0)


class _ExpArgs(_Args):
    query: str = Field(default="", max_length=200)
    mode: Literal["search", "object", "change", "round"] = "search"
    object_id: str = ""
    change_id: str = ""
    round_id: str = ""
    cursor: int = Field(default=0, ge=0)
    text_cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=20)


class _ProposeArgs(_Args):
    kind: Literal["general", "specific", "composed"]
    ops: list[_KeyOpIn] = Field(default_factory=list)
    spec_ops: list[_SpecOpIn] = Field(default_factory=list)
    rationale: str = Field(min_length=1, max_length=2000)
    citations: list[str] = Field(default_factory=list)


class _ProbeArgs(_Args):
    candidate: Literal["general", "specific", "composed"]
    case_ids: list[str] = Field(min_length=1)


class _EvalArgs(_Args):
    candidate: Literal["current", "general", "specific", "composed"]


class _ReadProbeArgs(_Args):
    case_id: str = Field(min_length=1)
    kind: Probed
    offset: int = Field(default=0, ge=0)


class _CompareArgs(_Args):
    case_id: str = Field(min_length=1)
    x: Probed
    y: Probed


class _PatchArgs(_Args):
    goal: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=2, max_length=16)
    failing_case: str = Field(min_length=1, max_length=4000)
    paths_hint: list[str] = Field(default_factory=list, max_length=12)
    citations: list[str] = Field(default_factory=list)


class _ExampleIn(_Args):
    args: dict[str, Any]
    expect: dict[str, Any]


class _ForgeArgs(_Args):
    name: str = Field(min_length=2, max_length=41)
    title: str = Field(min_length=2, max_length=16)
    does: str = Field(min_length=2, max_length=80)
    purpose: str = Field(min_length=1, max_length=2000)
    examples: list[_ExampleIn] = Field(min_length=1, max_length=20)
    citations: list[str] = Field(default_factory=list)


FORGE_ONCE = "这一轮已经造过一个工具"
FORGE_UNKNOWN = "没有这个自己造的工具"
FORGE_OFF = "现在不能造工具（造工具的开关关了，或者造工具用的模型不可用）"
FORGE_BUILTIN = "这个名字是写页面已有的工具，换一个名字"
FORGE_NOT_THIS_RUN = "H12.tool 只能写这一轮造好的工具候选"
FORGE_HINT = ('要用它，就在候选里加 H3.deep.{name}=true 和 '
              'H12.tool.deep.{name}={{"candidate_id": "{cid}"}}')

PATCH_NOTED = ("已记下这个代码修改请求。这一轮结束后会照它写出代码改动、跑测试，写页面的问题还会在同几段上重放比较，"
               "然后交给学习者审，批准后才生效；你这边不用再做什么，接着处理设置改动并 compose 交卷，设置改不了就交 none。")
PATCH_ONCE = "这一轮已经记下一个代码修改请求了，一轮只能提一个"


class _ComposeArgs(_Args):
    decision: Decision
    ops: list[_KeyOpIn] = Field(default_factory=list)
    spec_ops: list[_SpecOpIn] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1, max_length=2000)


RATIONALE_DOC = ("第一行原样给学习者看，让他能判断要不要撤销：一两句大白话，说他遇到了什么问题、这次改了什么、"
                 "以后会有什么不同；不写设置名（H1、H2 这类）、工具的英文名和 id。后面的行可以写技术细节。")
_OP_SCHEMA = {"type": "array", "items": {"type": "object", "required": ["name", "value"],
              "properties": {"name": {"type": "string", "description": "设置名，如 H1.write"},
                             "value": {"description": "新值，按 read_pack 写的类型给（文字、整数、"
                                                      "true/false、列表或对象）；null 表示恢复默认"}}}}
_SPEC_OP_SCHEMA = {"type": "array", "items": {"type": "object", "required": ["op"], "properties": {
    "op": {"type": "string", "enum": ["add_item", "edit_item", "remove_item", "set_param"]},
    "item_id": {"type": "string", "description": "改或删时必填；新增时可不填"},
    "text": {"type": "string"}, "scope": {"type": "string"}, "domain": {"type": "string"},
    "path": {"type": "string", "enum": list(get_args(ParamPath)),
             "description": "set_param 时必填：要调的讲解设置"},
    "value": {"type": "integer", "description": "set_param 时必填：新的整数值"}}}}
# what the four teaching-spec params mean (the spec_ops tool text and read_pack's H7 block)
PARAM_DOCS = {
    "recap_max_per_page": "一页最多放几张提醒卡（0–8）",
    "analogy_level": "类比用多少（0 不用，1 只用学习者已经懂的东西打比方，2 放开用）",
    "fading_level": "例题渐隐（0 每步都写全，1 逐步少写，2 尽早让学习者自己补）",
    "interactive_bias": "交互块（0 尽量不用，1 需要时用，2 优先用）"}
SPEC_OPS_DOC = ("spec_ops 改讲解规则（add_item / edit_item / remove_item），或用 set_param 调讲解设置"
                "（path + 整数 value）：" + "；".join(f"{p}「{PARAM_ZH[p]}」{PARAM_DOCS[p]}"
                                               for p in RepParams.model_fields) + "。")
BANNED = "{what} 以前被学习者拒绝、撤销或自动撤回过，不能再提"

_CITES = {"type": "array", "items": {"type": "string"},
          "description": "引用的 id：signal:<id>、trace:<run_id>、cmp:…"}


# --- read_pack: which keys are listed, and how a structural key is described ---------------------

_STRUCTURAL = frozenset({"H3", "H8", "H9", "H10", "H11", "H12"})
_AGENT_ZH = {"deep": "写页面", "assistant": "助手"}
ASSISTANT_H3 = "只能填 false（助手的工具只能自动关掉，不能自动打开）"


def _assistant_probe(name: str, dim: str) -> KeyOp:
    """The op read_pack checks an assistant key with: an H3 tool switched off (the only H3 op
    rule R1 allows), any other key back to its default."""
    return KeyOp(name, False if dim == "H3" else None)


def _editable(k: Key) -> bool:
    """Whether read_pack lists a static key as changeable: its dim is open, and it is an H1 key
    (not of an RSI judge / review / method step: `diff.closed_h1`),
    a key of the page writer (`deep`; the forge's own keys, H4.forge.* / H10.forge.*, are not),
    or an assistant key an assistant-turn replay measures (harness 3b follow-up 3, rule R1:
    `diff.assistant_refusal`; H9 / H10 / the context sizes and H6 are not listed)."""
    if k.dim not in IMPROVER_DIMS:
        return False
    if k.dim == "H1":
        return not closed_h1(k.name)
    agent = k.name.split(".")[1]
    if agent == "assistant":
        return assistant_refusal(_assistant_probe(k.name, k.dim), k) is None
    return agent == "deep"


def _hook_shape(name: str) -> str:
    at = name.split(".", 1)[1]
    h = HOOKS[at]
    return (f'列表，每一步 {{"at": "{at}", "kind": "{h.kinds[0]}", "tier": {"、".join(h.tiers)} 之一, '
            f'"tools": [从 {"、".join(h.tools)} 里选], "instruction": 说明（最多 {HOOK_TEXT_MAX} 字）, '
            f'"output_to": "{h.output_to}", "max_tool_calls": 0–{HOOK_MAX_TOOL_CALLS}}}')


def _shape(k: Key) -> str:
    """The kind, allowed values and approval of a structural key ("" for the others)."""
    if k.dim not in _STRUCTURAL:
        return ""
    if k.dim == "H3" and k.name.split(".")[1] == "assistant":
        what = ASSISTANT_H3
    elif k.kind == "bool":
        what = "true 或 false"
    elif k.kind == "enum":
        what = f"只能是 {'、'.join(k.options)} 之一"
    elif k.kind == "list":
        what = f"列表，从 {'、'.join(k.options)} 里选 {k.lo}–{k.hi} 项"
    elif k.kind == "steps":
        what = _hook_shape(k.name)
    elif k.dim == "H12":
        what = '{"candidate_id": "fc_…"}'
    elif k.name == "H8.deep.evoui_examples":
        what = (f'列表，每项 {{"name": 名字（最多 {SKILL_NAME_MAX} 字）, "use": 什么时候用'
                f'（最多 {SKILL_USE_MAX} 字）, "block": 交互块}}')
    elif k.kind == "json" and k.name.endswith(".presets"):
        what = (f'列表，每项 {{"name": 名字（最多 {SKILL_NAME_MAX} 字）, "use": 什么时候用'
                f'（最多 {SKILL_USE_MAX} 字）, "config": 按下面这个组件的配置格式填的对象}}')
    elif k.kind == "text":
        what = f"文字，最多 {k.hi} 字"
    else:
        what = k.kind
    return f"｜{what}｜{'要你批准' if k.approval == 'ask' else '自动'}"


def _tools_head() -> str:
    parts = []
    for a in ("deep", "assistant"):
        if a not in TOGGLE_TOOLS:
            continue
        locked = LOCKED_TOOLS.get(a, ())
        parts.append(f"{_AGENT_ZH[a]}能关掉的工具：{'、'.join(TOGGLE_TOOLS[a])}"
                     + (f"；{'、'.join(locked)} 一直开着" if locked else ""))
    return "H3 工具开关（" + "；".join(parts) + "）："


_DIM_HEAD: dict[str, Callable[[], str]] = {
    "H3": _tools_head,
    "H8": lambda: "H8 技能（写页面时读教学规范的哪几部分、额外的交互块示例）：",
    "H9": lambda: "H9 动笔前的准备步骤（一个小助手先用读取工具找材料，结果放进写作者的上下文）：",
    "H11": lambda: "H11 写完后自查（一个小助手检查写好的内容，发现的问题交给写作者再改一轮）：",
    "H10": lambda: "H10 模型档和深度思考（换档会改变花费）：",
}
_FORGED_HEAD = {"H3": ("自己造的工具的开关（「实际」是这个工具现在用不用。已经上线的工具可以只改这个开关，"
                       "打开或关上都行；这一轮新造的工具打开时要和它的 H12.tool 一起改，一次只能加一个）："),
                "H12": "H12 自己造的工具（和它的 H3 开关一起改；一次只能改一个）："}


@functools.cache
def deep_tool_texts() -> dict[str, str]:
    """The default H2 texts of the deep writer: each tool's own description and each argument's,
    by H2 key. The tool classes only keep their constructor arguments, so they are built empty."""
    from evowiki.agent.read_k import ReadKTool
    from evowiki.agent.read_page import ReadPageTool
    from evowiki.deep import tools as dt
    from evowiki.deep.skill import read_ref_tool

    empty = TeachingSpec(version=1, items=())
    tools = (ReadKTool(None, {}), ReadPageTool(None), dt.ReadSpecTool(empty),  # type: ignore[arg-type]
             dt.WriteSectionTool(None), dt.PlaceComponentTool(None),  # type: ignore[arg-type]
             read_ref_tool(),
             dt.WriteBlockTool(None), dt.RenderCheckTool(None, None),  # type: ignore[arg-type]
             dt.FinishTool(None, "", None, {}))  # type: ignore[arg-type]
    out: dict[str, str] = {}
    for t in tools:
        out[h2_key("deep", t.spec.name)] = t.spec.description
        for arg, schema in (t.spec.parameters.get("properties") or {}).items():
            if schema.get("description"):
                out[h2_key("deep", t.spec.name, arg)] = schema["description"]
    return out


@functools.cache
def assistant_tool_texts() -> dict[str, str]:
    """The default H2 texts of the assistant's built-in tools, by H2 key (the texts an assistant
    replay rebuilds from: `replay_assistant.builtin_specs`)."""
    from evowiki.harness.replay_assistant import builtin_specs
    out: dict[str, str] = {}
    for name, spec in builtin_specs().items():
        out[h2_key("assistant", name)] = spec.description
        for arg, schema in (spec.parameters.get("properties") or {}).items():
            if schema.get("description"):
                out[h2_key("assistant", name, arg)] = schema["description"]
    return out


class _ToolError(Exception):
    """A refusal the model sees as a tool error (plain Chinese)."""


def _said(steps: Sequence[dict]) -> str:
    """What the learner said in a traced assistant turn: the first model step's last user message, after its context pack."""
    first = next((s for s in steps if s["kind"] == "model"), None)
    for m in reversed((first or {}).get("messages") or []):
        if m.get("role") == "user" and m.get("text"):
            return m["text"].rsplit("他问：", 1)[-1].strip()[:SAID_CLIP]
    return ""


def _offered(steps: Sequence[dict]) -> str:
    """The tools the traced turn's model could call at each point (the list grows when a skill loads)."""
    names: dict[str, None] = {}
    for s in steps:
        if s["kind"] == "model":
            names.update(dict.fromkeys(t["name"] for t in s.get("tools") or []))
    return "、".join(names)


def _clip(text: str, offset: int = 0, size: int = CLIP) -> str:
    part = text[offset:offset + size]
    rest = len(text) - offset - len(part)
    if offset >= len(text) and text:
        return f"（offset 超出了长度 {len(text)}）"
    return part + (f"\n（还有 {rest} 字，用 offset={offset + len(part)} 接着读）" if rest > 0 else "")


def _dump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _spec_op_text(s: SpecPatchOp | ParamPatchOp) -> str:
    if isinstance(s, ParamPatchOp):
        return f"{PARAM_ZH.get(s.path, s.path)} 设为 {s.value:g}"
    return f"{s.op} {s.item_id or ''} {s.text or ''}".strip()


def _problems(exc: ValidationError) -> str:
    return "；".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                    for e in exc.errors())[:600]


class _Tool:
    def __init__(self, spec: ToolSpec, fn: Callable[[dict[str, Any]], str], run: "ImproverRun"):
        self.spec = spec
        self._fn = fn
        self._run = run

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return self._run.guard(self.spec.name, lambda: self._fn(args))


def _spec(name: str, description: str, properties: dict, required: Sequence[str] = (),
          category: ToolCategory = "read") -> ToolSpec:
    return ToolSpec(name=name, description=description, effect="read", category=category,
                    parameters={"type": "object", "required": list(required),
                                "properties": properties})


class _CostingGateway:
    """The judge's gateway, keeping each call it makes (the judge calls' recorded cost)."""

    def __init__(self, gw: Gateway, calls: dict[str, LLMCall]):
        self._gw = gw
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        return getattr(self._gw, name)

    def call(self, role, request, schema, *, account=None):
        parsed, c = self._gw.call(role, request, schema, account=account)
        self._calls[c.id] = c
        return parsed, c


# --- one run ----------------------------------------------------------------------------------

class ImproverRun:
    """The state of one improver run, which its tools read and write."""

    def __init__(self, user_id: str, cases: CaseSet, deps: ImproverDeps):
        if deps.snapshot is not None:
            visible = []
            with deps.pool.connection() as conn:
                for signal in deps.signals:
                    nodes = {f'signal:{signal.signal_id}'}
                    if run := deps.signal_runs.get(signal.signal_id):
                        from evowiki.harness.replay_assistant import trace_source_closure
                        nodes.update(trace_source_closure(conn, user_id, run))
                    nodes = experience.dependency_closure(conn, user_id, sorted(nodes))
                    from evowiki.store import dreaming
                    if deps.snapshot.allows(signal.at, nodes) and dreaming.sources_visible(conn, user_id, sorted(nodes)):
                        visible.append(signal)
            deps = replace(deps, signals=tuple(visible))
        self.user_id, self.cases, self.deps = user_id, cases, deps
        self.case_by_id: dict[str, Case] = {c.case_id: c for c in (*cases.problem, *cases.anchors)}
        self.candidates: dict[str, Candidate] = {}
        self.probes: dict[tuple[str, str], ReplayResult] = {}
        self.all_probes: list[ReplayResult] = []
        self.comparisons: dict[str, Comparison] = {}
        self.traces: set[str] = {c.run_id for c in self.case_by_id.values()}
        self.judge_gws: dict[str, _CostingGateway] = {}
        self.judge_calls: dict[str, LLMCall] = {}
        self.composition: tuple[Decision, PackDiff | None, tuple[str, ...], str] | None = None
        self.ended: str | None = None            # a plain reason the run stopped early
        self.fatal: Exception | None = None      # raised after the loop
        self.attempts = 0                        # replays tried, failed ones included (the cap)
        self.failed: dict[tuple[str, str], str] = {}   # (kind, case) -> why its replay failed
        self.failed_cost = 0.0                   # what the failed replays were charged
        self.cmp_kinds: dict[str, tuple[str, str]] = {}  # cmp id -> the two kinds it compared
        self.patch: PatchProposal | None = None
        self.patch_offered = self._patch_offered()
        self.forge_calls = 0                     # forge_tool calls (FORGE_PER_ROUND)
        self.forge_cost = 0.0                    # what the forge's builds cost (USD)
        self.built: dict[str, str] = {}          # forged tool name -> this run's built candidate
        self.forged_args: dict[str, tuple[str, ...]] = {}   # deep forged tool -> its arguments
        self.forged_docs: dict[str, str] = {}    # H2 key -> the registry's description
        self.registered: frozenset[str] = frozenset()   # deep forged tools the registry has now
        self.forged_on: frozenset[str] = frozenset()    # those of them the writer uses now
        self.broken: dict[str, str] = {}         # registry entries that do not load -> why
        self.forge_offered = self._forge_offered()
        self.eval_runs: dict[str, Mapping[str, Any]] = {}    # "current" and each candidate's kind
        self.eval_usage: dict[str, Mapping[str, Any]] = {}   # same run's cost, seconds and calls
        self.evals_used = 0
        self.experience_sources: set[str] = set()
        self.tools = self._tools()

    # the loop catches every tool exception; a refusal goes back to the model, anything else
    # stops the run and is raised after it (fail closed)
    def guard(self, name: str, fn: Callable[[], str]) -> ToolResult:
        if self.done():
            return ToolResult(content="这一轮已经结束了，不要再调用工具。", error="run ended")
        try:
            return ToolResult(content=fn())
        except _ToolError as e:
            return ToolResult(content=str(e)[:CLIP], error=str(e)[:500])
        except Exception as e:       # noqa: BLE001 - kept and re-raised after the loop
            self.fatal = e
            return ToolResult(content="出错了，这一轮停止。", error=f"{type(e).__name__}: {e}"[:500])

    def done(self) -> bool:
        return self.composition is not None or self.ended is not None or self.fatal is not None

    # --- citations ---
    def known(self) -> set[str]:
        return ({f"signal:{s.signal_id}" for s in self.deps.signals}
                | {f"trace:{r}" for r in self.traces} | set(self.comparisons) | self.experience_sources)

    def _cited(self, citations: Sequence[str]) -> tuple[str, ...]:
        cites = tuple(dict.fromkeys(c.strip() for c in citations if c.strip()))
        if not cites:
            raise _ToolError("至少要引用一个这一轮看到的 id（signal:…、trace:…、cmp:…）")
        bad = [c for c in cites if c not in self.known()]
        if bad:
            raise _ToolError(f"这些引用在这一轮里不存在：{'、'.join(bad)}。只能引用 read_signals 里的反馈、"
                             "读过或重放过的执行记录，以及 compare 给出的 cmp: id")
        return cites

    # --- diffs ---
    def _diff(self, kind: str, ops: Sequence[_KeyOpIn], spec_ops: Sequence[_SpecOpIn]) -> PackDiff:
        used = {i.item_id for i in self.deps.base_spec.items}
        sops: list[SpecPatchOp | ParamPatchOp] = []
        for n, s in enumerate(spec_ops, 1):
            if s.op == "set_param":
                if s.path is None or s.value is None:
                    raise _ToolError(f"第 {n} 个 spec_ops：set_param 要给 path 和整数 value")
                sops.append(ParamPatchOp(track="B", path=s.path, value=s.value))
                continue
            item_id = s.item_id
            if s.op == "add_item" and not item_id:
                item_id = f"harness-{kind}-{n}"
                while item_id in used:
                    item_id += "x"
            used.add(item_id or "")
            try:
                sops.append(SpecPatchOp.model_validate({**s.model_dump(exclude={"path", "value"}),
                                                       "item_id": item_id}))
            except ValidationError as e:
                raise _ToolError(f"讲解规则改动不合格：{_problems(e)}") from e
        diff = PackDiff(tuple(KeyOp(o.name, o.value) for o in ops), tuple(sops))
        self._check_forged(diff)
        # a tool this run built is not in the registry yet: its opt-in needs its H12 op
        reason = refuse(diff, self.deps.base_pack, self.deps.sources, defaults=self.deps.defaults,
                        registered=self.registered - set(self.built))
        if reason is not None:
            raise _ToolError(f"这个改动不能用：{reason}")
        try:
            spec = candidate_spec(self.deps.base_spec, diff.spec_ops)
        except (PatchError, ValueError) as e:
            raise _ToolError(f"讲解规则改动用不上：{e}") from e
        if diff.spec_ops:
            # the checkpoint the commit runs again (`round._commit`), here so the improver hears
            # why now: a user / pending / protected rule, a param the user set, a rule he removed
            try:
                authority_check(self.deps.base_spec, spec, actor="rsi",
                                decisions=self.deps.decisions)
            except RuleAuthorityError as e:
                raise _ToolError(f"讲解规则改动不允许：{'；'.join(e.problems)}") from e
        self._check_banned(diff)
        said = leaks(diff, {o.name: self.deps.base_pack.values.get(o.name) for o in diff.ops},
                     self.deps.defaults, self.deps.words)
        if said:
            raise _ToolError(f"这个改动只针对个别内容，不能当通用设置：{'；'.join(said)}")
        if self.deps.guard is not None:
            rg = self.deps.guard(diff)
            if rg.outcome == "block":
                why = "；".join(r.why for r in rg.records if r.verdict == "block")
                raise _ToolError(f"这个改动通不过规则检查：{rg.reason}。{why}".strip())
        return diff

    def _check_banned(self, diff: PackDiff) -> None:
        """A change the user rejected or undid, or that was rolled back, is never proposed again
        (the same rule change by `spec_ops.body_key`, the same key value)."""
        if not self.deps.banned:
            return
        for o in diff.ops:
            if op_key(o) in self.deps.banned:
                raise _ToolError(BANNED.format(what=f"{o.name}={_dump(o.value)}"))
        for s in diff.spec_ops:
            if body_key(s) in self.deps.banned:
                raise _ToolError(BANNED.format(what=f"讲解规则改动「{_spec_op_text(s)}」"))

    def _forged(self) -> dict[str, tuple[str, ...]]:
        """agent -> forged tool names: `deps.forged`, the registry's (with the forge offered) and
        this run's built candidate."""
        out = {a: tuple(ts) for a, ts in self.deps.forged.items()}
        deep = dict.fromkeys((*out.get("deep", ()), *self.forged_args, *self.built))
        if deep:
            out["deep"] = tuple(deep)
        return out

    def _check_forged(self, diff: PackDiff) -> None:
        """`refuse` checks forged-tool keys by pattern only (P3): every forged opt-in and H12.tool
        op must name a forged tool the registry has or this run built, and an H12.tool value must
        name this run's built candidate of that tool."""
        known = set(self._forged().get("deep", ()))
        for o in diff.ops:
            parts = o.name.split(".")
            if is_forged_opt_in(o.name):
                tool = parts[2]
            elif o.name.startswith("H12.tool.") and len(parts) == 4:
                tool = parts[3]
                if (isinstance(o.value, dict)
                        and o.value.get("candidate_id") != self.built.get(tool)):
                    raise _ToolError(f"{FORGE_NOT_THIS_RUN}：{o.name}")
            else:
                continue
            if tool in self.broken and tool not in self.built:
                raise _ToolError(f"{self.broken[tool]}：{tool}")
            if tool not in known:
                raise _ToolError(f"{FORGE_UNKNOWN}：{tool}")

    # --- tools ---
    def read_signals(self, args: dict) -> str:
        _Args.model_validate(args)
        lines = [f"这一轮的问题：{self.cases.finding or '（没有归到一类）'}", "反馈："]
        for s in self.deps.signals:
            chips = "、".join(CHIP_LABEL[c] for c in s.chips) or "无"
            turn = self.deps.signal_runs.get(s.signal_id)
            where = (f"助手的一次回答 trace:{turn}" if turn is not None
                     else f"{s.page_id or '?'} / {s.section_id or '整页'}")
            lines.append(f"- signal:{s.signal_id}｜{s.kind}｜{where}｜按钮：{chips}｜"
                         f"原话：{s.text.strip() or '（无）'}｜{s.at:%Y-%m-%d %H:%M}")
        lines.append("要重放的段（problem 是出问题的段或助手的回答，anchor 是你的范例页，用来防止变差）：")
        for c in (*self.cases.problem, *self.cases.anchors):
            if c.kind == "assistant":
                lines.append(f"- {c.case_id}｜problem｜助手的一次回答｜执行记录 trace:{c.run_id}｜"
                             f"重放时用这次回答当时的请求和工具结果，按候选设置重新回答｜"
                             f"反馈 {'、'.join(c.signal_ids) or '无'}")
                continue
            saved = ("" if c.page_seq is None
                     else f"这一段不在那次写作的输入里，重放时按那次写作存下的页面"
                          f"（wiki 第 {c.page_seq} 条）改写这一段的原文，不是从头写｜")
            lines.append(f"- {c.case_id}｜{'anchor' if c.anchor else 'problem'}｜页 {c.page_id} "
                         f"段 {c.section_id}｜写作记录 trace:{c.run_id}｜方式 {c.mode}｜{saved}"
                         f"反馈 {'、'.join(c.signal_ids) or '无'}")
        lines.append(self._budget())
        if self.cases.unusable:
            lines.append("用不上的反馈：")
            lines += [f"- {sid}：{why}" for sid, why in self.cases.unusable]
        return _clip("\n".join(lines))

    def _budget(self) -> str:
        left = max(0, MAX_PROBES - self.attempts)
        return (f"重放次数：这一轮最多重放 {MAX_PROBES} 次（每段的现在手册和候选各算一次，"
                f"失败的也算），已用 {self.attempts} 次，还剩 {left} 次。")

    def read_trace(self, args: dict) -> str:
        a = _TraceArgs.model_validate(args)
        with self.deps.pool.connection() as conn:
            run = store_trace.run(conn, a.run_id)
            if run is None or run["user_id"] != self.user_id:
                raise _ToolError(f"没有这条执行记录：{a.run_id}")
            from evowiki.harness.replay_assistant import trace_source_closure
            nodes = trace_source_closure(conn, self.user_id, a.run_id, run.get('input'))
            generated = {probe.run_id for probe in self.all_probes}
            nodes = experience.dependency_closure(conn, self.user_id, sorted(nodes))
            from evowiki.store import dreaming
            boundary = self.deps.snapshot
            from evowiki.harness.snapshot import recorded_at
            if (boundary is not None and a.run_id not in generated
                    and not boundary.allows(recorded_at(conn, self.user_id, 'agent_run', a.run_id), nodes)):
                raise _ToolError('这条执行记录不在本轮允许的历史快照中')
            if not dreaming.sources_visible(conn, self.user_id, sorted(nodes)):
                raise _ToolError('这条执行记录的来源已不可用')
            steps = store_trace.steps(conn, a.run_id)
            dreaming.register_consumption(conn, self.user_id, f'round:{self.deps.round_id}', sorted(nodes))
        self.traces.add(a.run_id)
        if a.seq is not None:
            step = next((s for s in steps if s["seq"] == a.seq), None)
            if step is None:
                raise _ToolError(f"这条记录没有第 {a.seq} 步（共 {len(steps)} 步）")
            return _clip(_dump({k: v for k, v in step.items() if k != "ts"}), a.offset)
        lines = [(f"trace:{a.run_id}｜{run['agent']}｜对象 {run['subject']}｜手册包版本 "
                  f"{run['pack_version']}｜结束：{run['stop'] or run['error'] or '未结束'}"),
                 f"输入：{_dump(run['input'])[:600]}"]
        if run["agent"] == "assistant.turn":
            lines += [f"学习者这一轮说：{_said(steps)}",
                      f"这一轮模型能调的工具（说明文字是 H2.assistant.<工具名>，read_pack 能看）：{_offered(steps)}"]
        lines.append("步骤（用 seq 读某一步的完整内容）：")
        for s in steps:
            if s["kind"] == "model":
                t = s.get("turn") or {}
                names = "、".join(c["name"] for c in t.get("tool_calls") or []) or "无"
                lines.append(f"#{s['seq']} 模型（{s['purpose']}）：调用 {names}；"
                             f"{(t.get('text') or s.get('error') or '')[:150 if t.get('tool_calls') else ANSWER_CLIP]}")
            elif s["kind"] == "call":     # a JSON call inside the turn (new_goal's split, ...)
                lines.append(f"#{s['seq']} 模型（{s.get('purpose', '')}）："
                             f"{s.get('error') or '结构化输出'}")
            elif s["kind"] == "reject":
                lines.append(f"#{s['seq']} 被拒绝的回复：{s.get('reason', '')}")
            else:
                r = s["result"]
                mark = "（出错）" if r.get("error") else ""
                lines.append(f"#{s['seq']} 工具 {s['name']}{mark}：{_dump(s['args'])[:150]} → "
                             f"{r.get('content', '')[:200]}")
        return _clip("\n".join(lines), a.offset)

    def read_pack(self, args: dict) -> str:
        a = _PackArgs.model_validate(args)
        d = self.deps
        lines = [(f"手册包版本 {d.base_pack.version}。来源：user = 学习者自己设的（不能改），improver = "
                  "以前自动改的，rollback = 撤回后的值，默认 = 没设过。结构设置（H3、H8–H12）后面写着"
                  "类型、能填什么，以及「要你批准」（学习者批准后才生效）或「自动」（通过比较后就生效）。")]

        # a round only about the assistant's answers can only measure the assistant's keys
        only_assistant = bool(self.cases.problem) and all(c.kind == "assistant" for c in self.cases.problem)

        def row(name: str, doc: str, default: Any, shape: str = "") -> str:
            cur = d.base_pack.values.get(name)
            src = d.sources.get(name, "默认") if cur is not None else "默认"
            now = "（未设，用默认）" if cur is None else _dump(cur)
            dflt = "（没有固定值）" if default is None else _dump(default)
            return f"- {name}｜{doc}｜来源 {src}｜现在：{now}｜默认：{dflt}{shape}"

        # static keys by dim; right after a dim's block, its rows built on the fly (forged H3
        # opt-ins under H3, component keys under H8)
        extra = {"H3": lambda: self._forged_rows("H3", row),
                 "H8": lambda: self._component_rows(row)}
        prev: str | None = None
        for k in KEYS:
            if not _editable(k) or (a.dim and k.dim != a.dim):
                continue
            if only_assistant and (k.name.split(".")[1] != "assistant" or (a.dim is None and k.dim != "H1")):
                continue
            if k.dim != prev:
                if prev in extra:
                    lines += extra.pop(prev)()
                if k.dim in _DIM_HEAD:
                    lines.append(_DIM_HEAD[k.dim]())
                prev = k.dim
            lines.append(row(k.name, k.doc, d.defaults.get(k.name), _shape(k)))
        if prev in extra and not only_assistant:
            lines += extra.pop(prev)()
        if a.dim in (None, "H12") and not only_assistant:
            lines += self._forged_rows("H12", row)
        if a.dim in (None, "H2"):
            lines.append("H2 工具说明：")
            if not only_assistant:
                texts = deep_tool_texts()
                for tool, targs in TOOLS["deep"].items():
                    for name in (h2_key("deep", tool), *(h2_key("deep", tool, x) for x in targs)):
                        lines.append(row(name, "工具说明" if name.count(".") == 2 else "参数说明",
                                         texts.get(name, "（没有说明）")))
                for tool in self._forged().get("deep", ()):
                    targs = self.forged_args.get(tool, ())
                    for name in (h2_key("deep", tool), *(h2_key("deep", tool, x) for x in targs)):
                        if key_of(name) is None:
                            continue
                        what = "自己造的工具的说明" if name.count(".") == 2 else "自己造的工具的参数说明"
                        lines.append(row(name, what, self.forged_docs.get(name, "（没有说明）")))
            texts = assistant_tool_texts()
            brief = only_assistant and a.tool is None
            if brief:
                lines.append("（参数的说明没列出来：read_pack 给 dim=H2 和 tool=工具名 就能看这个工具的全部说明）")
            for tool, targs in TOOLS["assistant"].items():
                if a.tool not in (None, tool):
                    continue
                for name in (h2_key("assistant", tool),
                             *(h2_key("assistant", tool, x) for x in (() if brief else targs))):
                    what = "助手的工具说明" if name.count(".") == 2 else "助手的参数说明"
                    lines.append(row(name, what, texts.get(name, "（没有说明）")))
        if a.dim in (None, "H7") and not only_assistant:
            lines.append(f"H7 讲解规则（教学规范 v{d.base_spec.version}；只有「可以改」的能改或删，"
                         "其余是学习者自己的、待他确认的或基本约束）：")
            lines += [f"- {i.item_id}｜{i.scope}｜{'可以改' if rsi_may_change(i) else '不能改'}"
                      f"｜{i.text}" for i in d.base_spec.items] or ["（没有条目）"]
            lines.append("H7 讲解设置（set_param；学习者自己定的不能改）：")
            lines += [f"- {p}「{PARAM_ZH[p]}」｜{PARAM_DOCS[p]}｜现在：{getattr(d.base_spec.params, p)}"
                      f"｜{'学习者自己定的，不能改' if p in d.base_spec.user_params else '可以改'}"
                      for p in RepParams.model_fields]
        if len(lines) == 1:
            lines.append(f"{a.dim}：现在没有你能改的设置。")
        return _clip("\n".join(lines), a.offset, PACK_CLIP)

    def _forged_rows(self, dim: Literal["H3", "H12"], row: Callable[..., str]) -> list[str]:
        """The opt-in (H3) or the record (H12.tool) keys of the deep forged tools `_forged` lists
        (an assistant forged tool stays closed, rule R1); a name that is not a forged-tool key is
        skipped."""
        out: list[str] = []
        for agent in ("deep",):
            for tool in self._forged().get(agent, ()):
                opt, rec = f"H3.{agent}.{tool}", h12_tool_key(agent, tool)
                ko, kr = key_of(opt), key_of(rec)
                if ko is None or kr is None or not is_forged_opt_in(opt):
                    continue
                if not out:
                    out.append(_FORGED_HEAD[dim])
                if dim == "H12":
                    out.append(row(rec, kr.doc, None, _shape(kr)))
                elif tool in self.registered and tool not in self.built:
                    state = "开着" if tool in self.forged_on else "关着"
                    out.append(row(opt, ko.doc, False, _shape(ko)) + f"｜实际：{state}（已上线）")
                elif tool in self.built:
                    out.append(row(opt, ko.doc, False, _shape(ko)) + "｜实际：这一轮新造的版本，还没上线")
                else:
                    out.append(row(opt, ko.doc, False, _shape(ko)))
        return out

    def _component_rows(self, row: Callable[..., str]) -> list[str]:
        """`H8.component.<id>.note` for every component of the writer's library, `.presets` for
        the configurable ones (with their config format); an id no key accepts is skipped."""
        out: list[str] = []
        for cid, info in sorted(self.deps.components.items()):
            note, presets = f"H8.component.{cid}.note", f"H8.component.{cid}.presets"
            kn, kp = key_of(note), key_of(presets)
            if kn is None or kp is None:
                continue
            if not out:
                out.append("H8 交互组件（写页面能放的组件：什么时候用它的说明，以及可配置组件的预设）：")
            out.append(row(note, kn.doc, info.description, _shape(kn)))
            if info.config is not None:
                out.append(row(presets, kp.doc, [], _shape(kp)))
                out.append(f"  {cid} 的{info.config}")
        return out

    def read_experience(self, args: dict) -> str:
        from evowiki.harness.snapshot import recorded_at
        a = _ExpArgs.model_validate(args)
        boundary = self.deps.snapshot
        with self.deps.pool.connection() as conn:
            dossiers = experience.load_dossiers(
                conn, self.user_id, cutoff=boundary.cutoff if boundary else None,
                excluded_sources=boundary.excluded_sources if boundary else frozenset())
            for record in self.deps.memory:
                if record.user_id != self.user_id or record.entry.status not in FAILED_PATCH:
                    continue
                entry = record.entry.model_dump(mode="json", include={
                    "patch_id": True, "track": True, "signature": True, "path": True,
                    "status": True, "at": True, "gate": {"passed", "vetoes", "cases"}})
                sources = experience.source_nodes(entry)
                if boundary and not boundary.allows(record.counts_at, sources):
                    continue
                from evowiki.store import dreaming
                if not dreaming.sources_visible(conn, self.user_id, sorted(sources)):
                    continue
                dossiers.append({"id": f"patch-memory:{record.entry.patch_id}",
                                 "problem": entry["signature"], "objects": [],
                                 "experiences": [entry], "observations": [], "gates": [],
                                 "sources": sorted(sources), "historical_patch": entry})
            page = experience.read_page(dossiers, user_id=self.user_id, **a.model_dump())
            if a.mode == "object" and a.cursor == 0:
                from evowiki.store import dreaming
                digest = dreaming.get(conn, self.user_id, "object", a.object_id)
                if (digest and not digest.dirty
                        and (boundary is None or boundary.allows(
                            recorded_at(conn, self.user_id, 'dream_summary', 'object:' + a.object_id),
                            digest.sources))
                        and dreaming.sources_visible(conn, self.user_id, digest.sources)):
                    page["digest"] = {"id": digest.node, "payload": digest.payload,
                                      "sources": digest.sources}
                    dreaming.register_consumption(conn, self.user_id,
                                                  f"round:{self.deps.round_id}",
                                                  [digest.node, *digest.sources])
            self.experience_sources.update(page["sources"])
            if page["dependency_sources"]:
                from evowiki.store import dreaming
                dreaming.register_consumption(conn, self.user_id, f"round:{self.deps.round_id}",
                                              page["dependency_sources"])
        return _dump(page)

    def propose(self, args: dict) -> str:
        a = _ProposeArgs.model_validate(args)
        cites = self._cited(a.citations)
        diff = self._diff(a.kind, a.ops, a.spec_ops)
        self.candidates[a.kind] = Candidate(a.kind, diff, a.rationale, cites)  # type: ignore[arg-type]
        self.eval_runs.pop(a.kind, None)         # it was of another diff
        # earlier probes, failures and comparisons of this kind were of another diff
        dropped = [k for k in self.probes if k[0] == a.kind]
        for key in dropped:
            del self.probes[key]
        for key in [k for k in self.failed if k[0] == a.kind]:
            del self.failed[key]
        gone = [c for c, kinds in self.cmp_kinds.items() if a.kind in kinds]
        for cid in gone:
            del self.cmp_kinds[cid], self.comparisons[cid]
        ids = "、".join(s.item_id or "" for s in diff.spec_ops if isinstance(s, SpecPatchOp))
        out = (f"已记录{KIND_ZH[a.kind]}：{len(diff.ops)} 个设置改动，{len(diff.spec_ops)} 条讲解规则或讲解设置改动"
               + (f"（规则 id：{ids}）" if ids else "") + "。")
        if dropped or gone:
            left = max(0, MAX_PROBES - self.attempts)
            out += (f"注意：它替换了上一次的{KIND_ZH[a.kind]}，上一次的 {len(dropped)} 次重放和"
                    f" {len(gone)} 次比较作废了（" + ("、".join(gone) if gone else "没有比较")
                    + f"），不能再引用；用掉的重放次数不退回，还剩 {left} 次。")
        return out

    def probe(self, args: dict) -> str:
        a = _ProbeArgs.model_validate(args)
        cand = self.candidates.get(a.candidate)
        if cand is None:
            raise _ToolError(f"还没有 {a.candidate} 候选")
        unknown = [c for c in a.case_ids if c not in self.case_by_id]
        if unknown:
            raise _ToolError(f"没有这些段：{'、'.join(unknown)}")
        ids = list(dict.fromkeys(a.case_ids))
        pairs = [(k, cid) for cid in ids for k in ("current", a.candidate)]
        failed = [f"{cid} · {k}：{self.failed[(k, cid)]}" for k, cid in pairs
                  if (k, cid) in self.failed]
        if failed:
            raise _ToolError("这些重放已经失败过，不再重试：" + "；".join(failed))
        todo = [p for p in pairs if p not in self.probes]
        if self.attempts + len(todo) > MAX_PROBES:
            left = max(0, MAX_PROBES - self.attempts)
            raise _ToolError(
                f"重放次数不够：这一轮最多重放 {MAX_PROBES} 次，已用 {self.attempts} 次，还剩 {left} 次，"
                f"这次要重放 {len(todo)} 次，所以一次也没有重放。"
                + ("重放次数用完了，" if left == 0 else "可以少挑几段再试，或者")
                + "用已经重放过的文字去 compare，然后 compose 交卷。")
        out: list[str] = []
        failures = (ReplayFailed, LookupError, PatchError, HarnessInvalid, NotReplayable)

        def wave(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], ReplayResult | Exception]:
            jobs = [functools.partial(replay, self.case_by_id[cid],
                                      PackDiff() if kind == "current" else cand.diff,
                                      base_pack=self.deps.base_pack, base_spec=self.deps.base_spec,
                                      deps=self.deps.replay) for kind, cid in pairs]
            return dict(zip(pairs, run_all(jobs), strict=True))
        # module doc: the references first, then the candidates whose reference was written
        done = wave([p for p in todo if p[0] == "current"])
        done.update(wave([(k, cid) for k, cid in todo if k != "current" and (
            ("current", cid) in self.probes
            or isinstance(done.get(("current", cid)), ReplayResult))]))
        for kind, cid in todo:
            if (kind, cid) not in done:
                continue                         # its reference just failed: nothing to compare to
            self.attempts += 1
            res = done[(kind, cid)]
            if isinstance(res, Exception) and not isinstance(res, failures):
                raise res
            if isinstance(res, Exception):
                e = res
                runs: tuple[str, ...] = ()
                if isinstance(e, ReplayFailed):  # the writer ran: its spend and its runs count
                    self.failed_cost += e.cost
                    runs = e.run_ids
                    self.traces.update(runs)
                why = str(e)[:300]
                self.failed[(kind, cid)] = why
                seen = "、".join(f"trace:{r}" for r in runs)
                out.append(f"【{cid} · {kind}】没写出这一段：{why}" + (f"｜{seen}" if seen else ""))
                continue
            self.probes[(kind, cid)] = res
            self.all_probes.append(res)
            self.traces.add(res.run_id)
        each = max(500, CLIP // max(1, 2 * len(ids)))
        for cid in ids:
            for kind in ("current", a.candidate):
                r = self.probes.get((kind, cid))
                if r is not None:
                    text = (r.text if len(r.text) <= each else r.text[:each]
                            + f"…（截断，共 {len(r.text)} 字；用 read_probe 读全文）")
                    out.append(f"【{cid} · {kind}】写作模型 {r.writer_provider}｜trace:{r.run_id}\n{text}")
        if any(r.actions for (_, cid), r in self.probes.items() if cid in ids):
            out.append("【模拟】的动作在重放里没有真正执行，只说明会调用，不说明做成了；做没做成看评测集。")
        out.append(f"已重放 {self.attempts} 次，还剩 {max(0, MAX_PROBES - self.attempts)} 次"
                   f"（这一轮最多 {MAX_PROBES} 次）。")
        return "\n\n".join(out)[:CLIP + 2000]

    def read_probe(self, args: dict) -> str:
        a = _ReadProbeArgs.model_validate(args)
        r = self.probes.get((a.kind, a.case_id))
        if r is None:
            raise _ToolError(f"{a.kind} 在 {a.case_id} 上还没有重放")
        return _clip(r.text, a.offset)

    def compare(self, args: dict) -> str:
        a = _CompareArgs.model_validate(args)
        c = self.case_by_id.get(a.case_id)
        if c is None:
            raise _ToolError(f"没有这一段：{a.case_id}")
        if a.x == a.y:
            raise _ToolError("x 和 y 要是两份不同的文字")
        first, second = (a.y, a.x) if a.x == "current" else (a.x, a.y)   # first is `a`
        ra, rb = self.probes.get((first, a.case_id)), self.probes.get((second, a.case_id))
        missing = [k for k, r in ((first, ra), (second, rb)) if r is None]
        if missing:
            raise _ToolError(f"{'、'.join(missing)} 在 {a.case_id} 上还没有重放，先用 probe")
        assert ra is not None and rb is not None
        if ra.writer_provider != rb.writer_provider:
            raise _ToolError(f"这两份是不同的写作模型写的（{first}：{ra.writer_provider}，{second}："
                             f"{rb.writer_provider}），没法用同一个评审比较；换一对比较")
        ref = judge_mod.judge_for(ra.writer_provider, self.deps.judges)   # unavailable → raises
        gw = self.judge_gws.get(ref.model + "|" + ref.provider)
        if gw is None:
            gw = _CostingGateway(self.deps.judge_gateway(ref), self.judge_calls)
            self.judge_gws[ref.model + "|" + ref.provider] = gw
        text, more, kind = judge_mod.case_inputs(c, self.deps.signals)
        cmp = judge_mod.compare(c, ra, rb, signals_text=text, asked_for_more=more,
                                gw=gw, account=self.deps.account,  # type: ignore[arg-type]
                                kind=kind)
        self.comparisons[cmp.cmp_id] = cmp
        self.cmp_kinds[cmp.cmp_id] = (first, second)
        won = {"a": first, "b": second, "tie": "持平"}[cmp.winner]
        named = {"a": first, "b": second, "same": "持平"}
        return (f"{cmp.cmp_id}｜{a.case_id}｜{first} 对 {second}｜结论：{won}｜"
                f"两种顺序分别选：{named[cmp.sides[0]]}、{named[cmp.sides[1]]}"
                + (f"｜评审引用：「{cmp.quote}」" if cmp.quote else "") + f"｜理由：{cmp.reason}")

    def _eval_run(self, kind: str, diff: PackDiff) -> Mapping[str, Any]:
        if kind in self.eval_runs:
            return self.eval_runs[kind]
        if self.evals_used >= MAX_EVALS:
            raise _ToolError(f"这一轮的评测次数用完了（最多 {MAX_EVALS} 次，现行手册也算一次）")
        assert self.deps.evals is not None       # offered only with it
        self.evals_used += 1
        got = self.deps.evals(diff)
        if got is None:
            raise _ToolError("评测没跑完，这次没有结果")
        self.eval_runs[kind] = got["cases"]
        self.eval_usage[kind] = got["cost"]
        return self.eval_runs[kind]

    def _eval_cost(self, kind: str) -> str:
        usage = self.eval_usage[kind]
        judge = usage.get("judge") or {}
        product_usd, judge_usd = usage.get("product_usd"), judge.get("cost_usd")
        dollars = (f"${product_usd + judge_usd:.4f}"
                   if product_usd is not None and judge_usd is not None else "未知")
        product_calls, judge_calls = usage.get("calls"), judge.get("calls")
        calls = (str(product_calls + judge_calls)
                 if product_calls is not None and judge_calls is not None else "未知")
        seconds = usage.get("seconds")
        elapsed = f"{seconds:.1f}" if seconds is not None else "未知"
        return f"{KIND_ZH.get(kind, kind)}评测代价：{dollars}（产品＋裁判），{elapsed} 秒，{calls} 次调用。"

    def run_evals(self, args: dict) -> str:
        a = _EvalArgs.model_validate(args)
        base = self._eval_run("current", PackDiff())
        if a.candidate == "current":
            now = base
            head = "现行手册"
        else:
            cand = self.candidates.get(a.candidate)
            if cand is None:
                raise _ToolError(f"还没有 {a.candidate} 候选")
            now = self._eval_run(a.candidate, cand.diff)
            head = f"{KIND_ZH[a.candidate]}（括号里是现行手册的结果）"
        lines = [f"评测集，{head}。每条用例跑几次真实对话，代码判分，全过才算过（T 过，× 没过）。"]
        lines.append(self._eval_cost(a.candidate))
        ok = 0
        for cid, row in now.items():
            tr = row.get("trials") or []
            passed = bool(tr) and all(tr)
            ok += passed
            mark = "".join("T" if t else "×" for t in tr) or "没结果"
            was = "" if now is base else "（" + ("".join("T" if t else "×" for t in (base.get(cid) or {}).get("trials") or []) or "没结果") + "）"
            lines.append(f"- {cid}｜{mark}{was}")
            if not passed:
                said = "；".join(row.get("say") or [])
                for f in (row.get("failures") or [])[:1]:
                    lines.append(f"  他说：{said}\n  没过的原因：{f.get('basis', '')[:EVAL_ANSWER_CLIP]}\n"
                                 f"  回答：{(f.get('answer') or '')[:EVAL_ANSWER_CLIP]}")
        lines.append(f"全过 {ok}/{len(now)} 条。已用评测 {self.evals_used} 次，最多 {MAX_EVALS} 次。")
        return _clip("\n".join(lines), 0, PACK_CLIP)

    def _patch_offered(self) -> bool:
        hooks = self.deps.patch
        if hooks is None:
            return False
        with self.deps.pool.connection() as conn:
            return bool(hooks.enabled(conn, self.user_id))

    def _forge_offered(self) -> bool:
        """The forge's tool switch, and the registry's deep forged tools (their arguments and
        descriptions, `registered`, and which of them the writer uses now, `forged_on`), each
        entry read on its own (`forge_entries.read_all`): an entry that does not load is in
        `broken` (never offered for switching on, never read as unregistered), the others as
        usual. A registry whose heads cannot be read: the tool is not offered and no registered
        tool can be switched on (fail closed)."""
        hooks = self.deps.forge
        if hooks is None:
            return False
        try:
            with self.deps.pool.connection() as conn:
                if not hooks.enabled(conn, self.user_id):
                    return False
                read = hooks.read(conn, self.user_id, "deep")
                on = frozenset(hooks.opted_in(conn, self.user_id, "deep", tuple(read.args),
                                              self.deps.base_pack.values))
        except Exception:  # broad on purpose: no forging rather than no improver run
            log.warning("forged tools of %s unreadable; forge_tool not offered", self.user_id,
                        exc_info=True)
            return False
        self.forged_args = {n: tuple(a) for n, a in read.args.items()}
        self.forged_docs = dict(read.docs)
        self.registered = frozenset(read.args)
        self.broken = dict(read.broken)
        self.forged_on = on
        return True

    def forge_tool(self, args: dict) -> str:
        a = _ForgeArgs.model_validate(args)
        hooks = self.deps.forge
        assert hooks is not None                 # offered only with hooks
        if self.forge_calls >= FORGE_PER_ROUND:
            raise _ToolError(FORGE_ONCE)
        cites = self._cited(a.citations)
        if is_builtin("deep", a.name) or a.name in RESERVED or key_of(f"H3.deep.{a.name}") is None:
            raise _ToolError(FORGE_BUILTIN)
        try:
            req = ToolRequest.model_validate({
                "name": a.name, "title": a.title, "does": a.does, "purpose": a.purpose,
                "agent": "deep",
                "scope": "user", "examples": [e.model_dump() for e in a.examples]})
        except ValidationError as e:
            raise _ToolError(f"造工具的请求不合格：{_problems(e)}") from e
        self.forge_calls += 1
        try:
            gw, account = (self.deps.forge_binding() if self.deps.forge_binding is not None
                           else (self.deps.gateway, self.deps.account))
        except ValueError as e:                  # the forge tier is not available
            raise _ToolError(f"{FORGE_OFF}：{e}") from e
        note = f"这一轮的问题：{self.cases.finding}" if self.cases.finding else ""
        try:
            res = forge_tool(hooks, gw, account, self.user_id, req, cites,
                             pack=self.deps.base_pack, note=note)
        except (ForgeDisabled, ForgeMisconfigured) as e:
            raise _ToolError(FORGE_OFF) from e
        b = res.build
        self.forge_cost += b.cost_usd or 0.0
        cost = "未知" if b.cost_usd is None else f"${b.cost_usd:.3f}"
        head = (f"候选 {res.candidate_id}｜{'造好了' if b.ok else '没造成：' + b.reason}｜"
                f"测试通过 {b.tests_passed}/{b.tests_passed + b.tests_failed}｜"
                f"例子通过 {b.examples_passed}/{b.examples_passed + b.examples_failed}｜花费 {cost}")
        if not b.ok:
            return head
        self.built[a.name] = res.candidate_id
        with self.deps.pool.connection() as conn:
            schema = candidate_schema(conn, self.user_id, res.candidate_id)
        props = schema.get("properties")
        self.forged_args[a.name] = tuple(sorted(props)) if isinstance(props, Mapping) else ()
        self.forged_docs.update(tool_docs(schema, "deep", a.name))
        return f"{head}\n{FORGE_HINT.format(name=a.name, cid=res.candidate_id)}"

    def propose_patch(self, args: dict) -> str:
        a = _PatchArgs.model_validate(args)
        if self.patch is not None:
            raise _ToolError(PATCH_ONCE)
        cites = self._cited(a.citations)
        req = PatchRequest(goal=a.goal, title=a.title, failing_case=a.failing_case,
                           paths_hint=tuple(p for p in a.paths_hint if p))
        self.patch = PatchProposal(req, cites)
        return PATCH_NOTED

    def _board(self) -> str | None:
        """The candidates not measured yet, measured (`_eval_run`), and the board of all of them; None when there is
        nothing new to measure or no evaluation runs left."""
        kinds = [k for k in self.candidates if k not in self.eval_runs]
        need = len(kinds) + ("current" not in self.eval_runs)
        if not kinds or self.deps.evals is None or self.evals_used + need > MAX_EVALS:
            return None
        base = self._eval_run("current", PackDiff())
        for k in kinds:
            self._eval_run(k, self.candidates[k].diff)
        names = ["current", *self.candidates]
        def mark(kind: str, cid: str) -> str:
            tr = (self.eval_runs[kind].get(cid) or {}).get("trials") or []
            return "".join("T" if t else "×" for t in tr) or "?"
        def good(kind: str, cid: str) -> bool:
            tr = (self.eval_runs[kind].get(cid) or {}).get("trials") or []
            return bool(tr) and all(tr)
        lines = ["还没有交卷。这是评测集的真实结果（真实对话，代码判分，T 过，× 没过），提议通过时看的就是它：",
                 "用例｜" + "｜".join(KIND_ZH.get(k, k) for k in names)]
        lines[1:1] = [self._eval_cost(k) for k in names]
        moved = [c for c in base if len({good(k, c) for k in names}) > 1 or not good("current", c)]
        lines += [f"- {c}｜" + "｜".join(mark(k, c) for k in names) for c in moved]
        lines.append("全过的条数：" + "，".join(f"{KIND_ZH.get(k, k)} {sum(good(k, c) for c in base)}/{len(base)}" for k in names))
        for c in moved:
            bad = next((k for k in reversed(names) if not good(k, c) and (self.eval_runs[k].get(c) or {}).get("failures")), None)
            if bad is None:
                continue
            row = self.eval_runs[bad][c]
            f = row["failures"][0]
            lines.append(f"{c}（{KIND_ZH.get(bad, bad)}）他说：{'；'.join(row.get('say') or [])}｜没过的原因：{f.get('basis', '')[:EVAL_ANSWER_CLIP]}｜"
                         f"回答：{(f.get('answer') or '')[:EVAL_ANSWER_CLIP]}")
        lines.append(f"看完再调 compose 交卷：选真的修好、又没弄坏别的那个；都没修好就照原因改了再 propose（再 compose 时会重测），或者交 none。"
                     f"评测已用 {self.evals_used} 次，最多 {MAX_EVALS} 次。")
        return _clip("\n".join(lines), 0, PACK_CLIP)

    def compose(self, args: dict) -> str:
        a = _ComposeArgs.model_validate(args)
        cites = self._cited(a.citations)
        given = bool(a.ops or a.spec_ops)
        if self.deps.evals is not None:
            if a.decision == "merge" and given:
                self.candidates["composed"] = Candidate("composed", self._diff("composed", a.ops, a.spec_ops), a.rationale, cites)  # type: ignore[arg-type]
                self.eval_runs.pop("composed", None)
            board = self._board()
            if board is not None:
                return board
        chosen: PackDiff | None
        if a.decision == "none":
            if given:
                raise _ToolError("选 none 时不要给改动")
            chosen = None
        elif a.decision in ("a", "b"):
            if given:
                raise _ToolError("选 a 或 b 时不要再给改动；要改动后再用，就选 merge")
            cand = self.candidates.get("general" if a.decision == "a" else "specific")
            if cand is None:
                raise _ToolError(f"还没有{'通用' if a.decision == 'a' else '针对性'}候选")
            chosen = cand.diff
        elif given:
            chosen = self._diff("composed", a.ops, a.spec_ops)
        elif "composed" in self.candidates:
            chosen = self.candidates["composed"].diff
        else:
            raise _ToolError("merge 需要给出合并后的改动，或者先用 propose 提一个 composed 候选")
        self.composition = (a.decision, chosen, cites, a.rationale)
        return "已交卷。"

    def _tools(self) -> tuple[_Tool, ...]:
        off = {"type": "integer", "minimum": 0, "description": "从第几个字开始读（结果太长时用）"}
        specs: list[tuple[ToolSpec, Callable[[dict], str]]] = [
            (_spec("read_signals", "看这一轮要处理的反馈（每条带 signal: id）、要重放的段（case id，"
                   "以及写它的执行记录 trace: id），和用不上的反馈及原因。", {}), self.read_signals),
            (_spec("read_trace", "读一次运行的执行记录：不给 seq 时是每一步的摘要（模型调用了什么工具、"
                   "工具返回了什么）；给 seq 时是那一步的完整请求或结果。结果太长时用 offset 接着读。",
                   {"run_id": {"type": "string"}, "seq": {"type": "integer", "minimum": 1},
                    "offset": off}, ("run_id",)), self.read_trace),
            (_spec("read_pack", "看手册包里你能改的设置：现在的值、默认值和来源（user 是学习者自己设的，"
                   "不能改），结构设置的类型、能填什么和要不要批准，以及讲解规则（H7）的条目。"
                   "可以只看一类（dim）。",
                   {"dim": {"type": "string", "enum": list(get_args(PackDim))},
                    "tool": {"type": "string", "description": "dim=H2 时只看这一个助手工具的说明和它每个参数的说明"},
                    "offset": off}), self.read_pack),
            (_spec("read_experience", "查改动经验。search 按 query 跨对象查问题，不给关键词看最近档案；"
                   "object 按 object_id 看这处改过什么及对比摘要；change/round 按 change_id/round_id "
                   "展开原始心得、门控和每次观察。cursor=next_cursor 接着读，limit 每页条数。"
                   "单条太长时按返回的 entry_cursor、limit=1、text_cursor=next_text_cursor 读后续全文。"
                   "sources 中实际返回的原始 ID 可以在提案引用；联合改动不能当单对象独立效果。",
                   {"query": {"type": "string"},
                    "mode": {"type": "string", "enum": ["search", "object", "change", "round"]},
                    "object_id": {"type": "string"}, "change_id": {"type": "string"},
                    "round_id": {"type": "string"}, "cursor": {"type": "integer", "minimum": 0},
                    "text_cursor": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20}}), self.read_experience),
            (_spec("propose", "提出一个候选改动。kind：general 是通用候选，specific 是针对这位学习者或"
                   "这类内容的候选，composed 是把两者合并后想先重放看看的候选。ops 改设置；"
                   + SPEC_OPS_DOC + "要写理由，并引用看到的 id。同一类再提一次会替换掉上一次的。",
                   {"kind": {"type": "string", "enum": ["general", "specific", "composed"]},
                    "ops": _OP_SCHEMA, "spec_ops": _SPEC_OP_SCHEMA, "rationale": {"type": "string"},
                    "citations": _CITES}, ("kind", "rationale", "citations"), "draft"),
             self.propose),
            (_spec("probe", "用某个候选在沙箱里重放几段（不会改动任何页面），返回重放出来的文字，以及现在"
                   "的手册在同一段上的文字。重放次数有限（read_signals 里写着这一轮一共几次），超出的请求整个不执行。",
                   {"candidate": {"type": "string", "enum": ["general", "specific", "composed"]},
                    "case_ids": {"type": "array", "items": {"type": "string"}}},
                   ("candidate", "case_ids"), "draft"), self.probe),
            (_spec("read_probe", "读某一段重放出来的全文（probe 返回的文字太长被截断时用）。kind 取 "
                   "current、general、specific、composed；用 offset 接着读。",
                   {"case_id": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KIND_ZH)}, "offset": off},
                   ("case_id", "kind")), self.read_probe),
            (_spec("compare", "请评审比较同一段上重放过的两份文字（x、y 取 current、general、specific、"
                   "composed）。评审不知道哪份出自哪份手册，两种顺序各比一次。返回结论和它的 cmp: id。",
                   {"case_id": {"type": "string"},
                    "x": {"type": "string", "enum": list(KIND_ZH)},
                    "y": {"type": "string", "enum": list(KIND_ZH)}},
                   ("case_id", "x", "y"), "draft"), self.compare),
            (_spec("compose", "交卷，结束这一轮：a 用通用候选，b 用针对性候选，merge 用合并的改动（在这里"
                   "给出 ops / spec_ops，或者用已经提出的 composed 候选），none 不改。要写理由，并引用"
                   "看到的 id。",
                   {"decision": {"type": "string", "enum": ["a", "b", "merge", "none"]},
                    "ops": _OP_SCHEMA, "spec_ops": _SPEC_OP_SCHEMA,
                    "citations": _CITES, "rationale": {"type": "string", "description": RATIONALE_DOC}},
                   ("decision", "citations", "rationale"), "draft"), self.compose),
        ]

        if self.deps.evals is not None:
            specs.append((_spec(
                "run_evals", "在评测集上真跑：用现行手册（current）或某个候选，让助手回答一批固定的用例，代码判分，返回每条过没过、"
                "没过的原因和回答。这就是判断补丁有没有用的客观标准，比 compare 可靠：提议通过时也要过这一关。"
                "先跑 current 看现在哪些用例没过（也是证据）；提出候选后跑候选，没修好就看原因，改了再提再跑。每次要几分钟。",
                {"candidate": {"type": "string", "enum": ["current", "general", "specific", "composed"]}},
                ("candidate",), "draft"), self.run_evals))

        if self.patch_offered:
            specs.append((_spec(
                "propose_patch", "提一个代码修改请求：程序有缺陷，或者学习者要的能力产品根本没有（写页面和助手都算），"
                "任何设置都改不了时用。goal 写要解决的问题、已诊断的根因和修复方向，title 是给学习者看的 2–16 字标题，"
                "failing_case 写出错的具体情况（输入、得到了什么、应该是什么）及已读轨迹的结论，"
                "paths_hint 填相关文件路径，未知时可不填；不要让写代码模型重复查轨迹。"
                "citations 引用看到的 id。一轮最多一个；这一轮结束后才会写出"
                "改动、测试和重放比较，学习者批准后才生效。",
                {"goal": {"type": "string"}, "title": {"type": "string"},
                 "failing_case": {"type": "string"},
                 "paths_hint": {"type": "array", "items": {"type": "string"}},
                 "citations": _CITES},
                ("goal", "title", "failing_case", "citations"), "draft"), self.propose_patch))

        if self.forge_offered:
            specs.append((_spec(
                "forge_tool", "给写页面的 agent 造一个新工具（在沙箱里写代码、跑测试）。name 是工具名"
                "（小写字母开头，字母数字下划线），title 是给学习者看的 2–16 字标题，does 是给学习者看的一句大白话，"
                "说它能帮他做什么（上线时这句话会通知他），purpose 写它做什么、输入输出是什么，examples 是几组 {args, expect} 例子（工具对 args 应该返回 expect），"
                "citations 引用看到的 id。一轮最多造一个；造好后它只是候选，要用它就在候选改动里同时"
                "加它的 H3 开关和 H12.tool 记录，经过重放比较通过后才上线。",
                {"name": {"type": "string"}, "title": {"type": "string"},
                 "does": {"type": "string"}, "purpose": {"type": "string"},
                 "examples": {"type": "array", "items": {
                     "type": "object", "required": ["args", "expect"],
                     "properties": {"args": {"type": "object"}, "expect": {"type": "object"}}}},
                 "citations": _CITES},
                ("name", "title", "does", "purpose", "examples", "citations"), "draft"), self.forge_tool))

        def checked(fn: Callable[[dict], str]) -> Callable[[dict], str]:
            def run(args: dict) -> str:
                try:
                    return fn(args)
                except ValidationError as e:
                    raise _ToolError(f"参数不合格：{_problems(e)}") from e
            return run
        return tuple(_Tool(s, checked(fn), self) for s, fn in specs)

    # --- result ---
    def cost(self, calls: Sequence[LLMCall]) -> float:
        own = {c.id: c.cost or 0.0 for c in calls}
        return (sum(own.values()) + sum(c.cost or 0.0 for c in self.judge_calls.values())
                + sum(p.cost for p in self.all_probes) + self.failed_cost + self.forge_cost)

    def result(self, run_id: str, stop: str, calls: Sequence[LLMCall]) -> Composition:
        cands = tuple(self.candidates[k] for k in ("general", "specific") if k in self.candidates)
        if self.composition is not None:
            decision, chosen, cites, why = self.composition
        else:
            decision, chosen, cites = "none", None, ()
            why = self.ended or (
                f"工具调用次数用完了（最多 {IMPROVER_MAX_TOOL_CALLS} 次），这一轮不改"
                if stop == "tool_cap" else NO_COMPOSE)
        return Composition(candidates=cands, chosen=chosen, decision=decision, citations=cites,
                           rationale=why, probes=tuple(self.all_probes),
                           comparisons=tuple(self.comparisons.values()), run_id=run_id,
                           cost=self.cost(calls), patch=self.patch)


def _opening(user_id: str, cases: CaseSet, deps: ImproverDeps) -> str:
    return (f"学习者 {user_id}，这一轮（{deps.round_id}）有 {len(deps.signals)} 条反馈、"
            f"{len(cases.problem)} 段出问题的输入和 {len(cases.anchors)} 段范例。"
            f"问题：{cases.finding or '（没有归到一类）'}。"
            + ("这一轮的反馈都是助手的回答，能改的是助手的设置（H1.assistant、H2.assistant.<工具>），"
               "设置改不了而你有 propose_patch 时就用它。"
               if cases.problem and all(c.kind == "assistant" for c in cases.problem) else "")
            + "先调用 read_signals。")


def run_improver(user_id: str, cases: CaseSet, *, deps: ImproverDeps) -> Composition:
    """One improver run on `cases` (module doc). Raises on a model, provider, judge-routing or
    store error; a cap or a run without compose is Composition(decision="none")."""
    state = ImproverRun(user_id, cases, deps)
    run_id = f"harness:{deps.round_id}:improver"
    gw, account = deps.gateway, deps.account
    defs = tuple(ToolDef(name=t.spec.name, description=t.spec.description,
                         parameters=t.spec.parameters) for t in state.tools)
    calls: list[LLMCall] = []

    def request(s: RequestState) -> ChatRequest:
        return ChatRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=deps.round_id,
                           system=SYSTEM, messages=s.messages, tools=defs,
                           tool_choice=s.tool_choice)

    def converse(req: ChatRequest) -> tuple[ChatTurn, LLMCall]:
        turn, c = gw.converse("generator", req, account=account)
        calls.append(c)
        return turn, c

    spec = AgentSpec(
        name=AGENT, tools=state.tools,
        limits=LoopLimits(max_tool_calls=IMPROVER_MAX_TOOL_CALLS, on_cap="stop",
                          text_is_answer=False, max_nudges=1, nudge_text=NUDGE,
                          refused_text=REFUSED),
        request=request, finished=state.done, trace=deps.trace, run_id=run_id)
    deps.trace.start(run_id, user_id=user_id, agent=AGENT, subject=deps.round_id,
                     pack_version=deps.base_pack.version,
                     input={"round_id": deps.round_id,
                            "signals": [s.signal_id for s in deps.signals],
                            "cases": sorted(state.case_by_id), "finding": cases.finding})
    stop = ""
    try:
        gen = run_loop(spec, converse, ToolContext(user_id=user_id, session_id=deps.round_id),
                       [ChatMessage(role="user", text=_opening(user_id, cases, deps))])
        while True:
            try:
                next(gen)
            except StopIteration as done:
                stop = done.value.stop
                break
        if state.fatal is not None:
            raise state.fatal
    except BaseException as exc:
        finish_failed(deps.trace, run_id, exc)
        raise
    deps.trace.finish(run_id, stop=stop, error=None)
    return state.result(run_id, stop, calls)
