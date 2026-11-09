"""The per-run forged-tool surface (design 2026-10-22 §6.2, plan P2 T8).

`ForgeRuntime` is built once by the service; `for_run` pins one agent run's forged tools and
returns a `ForgeRun`, whose `tools(builtins, ctx)` is what the agent appends to its built-in list.

- **Fixed tool list** (Claude Code deferred tools + ToolSearch; Anthropic's tool search tool): a
  run offers at most `FIRST_CLASS_MAX` forged tools first-class (opted in, ranked by
  `index.search(query)`, sorted by name) plus the fixed meta tools `call_tool` / `find_tool` /
  `request_tool`. The list is decided at pin time and never changes during the run, so the tool
  definitions at the head of the prompt-cache prefix stay valid. Everything else that is
  reachable, and every tool forged mid-run, is reached through `call_tool`.
- **Pinned once** (Codex CLI / Claude Code tool registration): inside a durable workflow the pin
  is one recorded step `forge.pin` returning JSON, so a recovered run offers the same tools with
  the same code and the same registry version.
- **request_tool** (Voyager reuse; LATM): reuse an existing reachable tool when its description is
  close enough (`REUSE_SIM`), else one recorded step `forge.request_tool` calls `api.propose`
  (accounted `ForgeConverse`, origin `run:<run_id>` / `session:<session_id>`). A built tool joins
  this run's reachable set only (the run-scoped overlay); the registry is never written, the
  candidate stays `built` until a gate promotes it. `request_tool` is a `draft` tool (it runs in
  the workflow body, its external work is the recorded step), so on replay the overlay is rebuilt
  from the recorded JSON and the per-run cap counts the same.
- **Overlay**: built candidates of `session:<session_id>` (the assistant session's earlier
  request_tool results) and `candidate` (harness replay, `RegistryView.with_candidate`) join the
  reachable set but never add first-class tools (a candidate replacing a same-name first-class
  tool runs in its place, as `with_candidate` means).
- **Host port**: forged tools read through the agent's own built-in read tools listed in
  `HOST_READ_TOOLS[agent]`, run on the host with the run's `ToolContext`; the context never
  enters the guest, only the result text does.

Fail closed: kill switch off → `None`; a name colliding with a built-in or meta tool, or any
other pin failure → `None`, logged at error level (the run goes on without forged tools)."""

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evowiki.agent import durable
from evowiki.agent.tool_helpers import clip, fail
from evowiki.agent.tools import Tool, ToolContext, ToolResult, ToolSpec
from evowiki.agent.trace import TraceSink
from evowiki.forge import index, optin, registry, switch
from evowiki.forge.api import ForgeDeps, propose
from evowiki.forge.api import _new_id as new_candidate_id
from evowiki.forge.binding import ForgeConverse
from evowiki.forge.limits import (
    FIND_TOOL_K,
    FIRST_CLASS_MAX,
    HOST_READ_TOOLS,
    REQUEST_TOOL_MAX_PER_RUN,
    REUSE_SIM,
)
from evowiki.forge.models import (
    Evidence,
    ForgeDisabled,
    ForgeMisconfigured,
    HostExchange,
    ToolExample,
    ToolRequest,
)
from evowiki.forge.registry import ForgedTool, SandboxedTool
from evowiki.forge.sandbox import HostPort, Sandbox, SandboxUnavailable
from evowiki.llm.usage import Account, PurposeGroup
from evowiki.retrieval.embed import Embedder
from evowiki.store import forge as store

if TYPE_CHECKING:
    from evowiki.harness.pack import HarnessPack

log = logging.getLogger("evowiki.forge.run")

Agent = Literal["deep", "assistant"]
CALL_TOOL, FIND_TOOL, REQUEST_TOOL = "call_tool", "find_tool", "request_tool"
RESULT_CHARS = 4000
UNAVAILABLE = "现在不能生成新工具"


# --- the pin (one durable step) ---------------------------------------------------------------

def _tool_json(t: ForgedTool, title: str) -> dict[str, Any]:
    return {"candidate_id": t.candidate_id, "files": dict(t.files), "schema": dict(t.schema),
            "host_tools": list(t.host_tools), "title": title}


def _forged(name: str, d: Mapping[str, Any]) -> ForgedTool:
    return ForgedTool(name=name, candidate_id=str(d["candidate_id"]),
                      files=MappingProxyType(dict(d["files"])),
                      schema=MappingProxyType(dict(d["schema"])),
                      host_tools=tuple(d.get("host_tools") or ()))


def _title_of(conn: Any, t: ForgedTool) -> str:
    c = store.get_candidate(conn, t.candidate_id)
    spec = c.spec if c is not None and isinstance(c.spec, Mapping) else {}
    title = spec.get("title")
    return title if isinstance(title, str) and title.strip() else t.name


_OFF: dict[str, Any] = {"tools": {}, "first_class": [], "request_tool": False,
                        "version": 0, "global_version": 0}


# --- the runtime ------------------------------------------------------------------------------

@dataclass(frozen=True)
class ForgeRuntime:
    """Service-wide, built once in service/deps.py."""
    pool: ConnectionPool
    gateway: Any                       # Gateway bound to the good DeepSeek tier (H10 forge.code: 3b)
    sandbox: Sandbox                   # registry._LazyWasi() in the service
    embedder: Embedder
    trace: TraceSink
    clock: Callable[[], datetime]

    def for_run(self, *, user_id: str, agent: Agent, run_id: str, group: PurposeGroup,
                pack_values: Mapping[str, Any] | None = None, query: str = "",
                session_id: str | None = None, candidate: str | None = None,
                pack: "HarnessPack | None" = None,
                on_cost: "Callable[[float | None], None] | None" = None) -> "ForgeRun | None":
        """Pin this run's forged tools, or None when there is nothing to offer (kill switch off,
        nothing opted in, request_tool off). Inside a durable workflow the pin is ONE recorded step
        ("forge.pin") returning JSON {tools: {name: {candidate_id, files, schema, host_tools,
        title}}, first_class: [...], request_tool: bool, version, global_version}, so a recovered
        run offers the same tools. Overlay: candidates of `session:<session_id>` (assistant) and
        `candidate` (harness replay, `RegistryView.with_candidate`) join the reachable set but are
        never first-class. `on_cost`: told what each request_tool build cost (USD; None = a call
        had no price), on a fresh run and again from the recording on replay, so the caller adds
        it to the page's `cost_usd` / the turn's `spent_usd` (the forge's calls go through its own
        gateway, not the run's). `pack`: the run's harness pack, read here (agent code reads the pack
        only through `harness.apply`); else `pack_values`, its values."""
        values = dict(pack.values if pack is not None else (pack_values or {}))
        try:
            pin = durable.step("forge.pin", lambda: self._pin(
                user_id, agent, values, query, session_id, candidate))
        except Exception:
            log.exception("forge pin failed for %s run %s: no forged tools this run", agent,
                          run_id)
            return None
        tools = {n: _forged(n, d) for n, d in sorted(pin["tools"].items())}
        if not tools and not pin["request_tool"]:
            return None
        return ForgeRun(
            agent=agent, reachable=tools, first_class=tuple(pin["first_class"]),
            request_tool_on=bool(pin["request_tool"]), user_id=user_id, run_id=run_id,
            group=group, session_id=session_id,
            titles={n: str(d["title"]) for n, d in pin["tools"].items()},
            version=int(pin["version"]), global_version=int(pin["global_version"]),
            runtime=self, on_cost=on_cost)

    def _pin(self, user_id: str, agent: Agent, pack_values: Mapping[str, Any], query: str,
             session_id: str | None, candidate: str | None) -> dict[str, Any]:
        """The pin's body (a step): JSON only, plain types, round-tripped through json."""
        with self.pool.connection() as conn:
            if not switch.enabled(conn, user_id, "tool"):
                return dict(_OFF)
            v = registry.view(conn, user_id, enabled=True, sandbox=self.sandbox)
            opted = optin.opted_in(conn, user_id, agent, [*v.tools, optin.META], pack_values)
            chosen = {n: t for n, t in v.tools.items() if n in opted}
            reachable = dict(chosen)
            overlay: list[str] = []
            if session_id:
                overlay += [c.id for c in store.candidates_by_origin(
                    conn, user_id, f"session:{session_id}", statuses=("built",))]
            if candidate:
                overlay.append(candidate)
            for cid in overlay:            # what `RegistryView.with_candidate` loads, one by one
                loaded = registry._load(conn, user_id, cid)
                reachable[loaded.name] = loaded
            registry._refuse_collisions(agent, reachable, registry._builtin(agent))
            titles = {n: _title_of(conn, t) for n, t in reachable.items()}
            if len(chosen) <= FIRST_CLASS_MAX:
                first = sorted(chosen)
            else:
                index.ensure(conn, self.embedder, [(chosen[n], titles[n]) for n in chosen])
                first = sorted(n for n, _ in index.search(conn, self.embedder, query, chosen,
                                                          FIRST_CLASS_MAX))
            out = {"tools": {n: _tool_json(t, titles[n]) for n, t in reachable.items()},
                   "first_class": first, "request_tool": optin.META in opted,
                   "version": v.version, "global_version": v.global_version}
        return json.loads(json.dumps(out))


# --- the run ----------------------------------------------------------------------------------

def host_port(builtins: Sequence[Tool], agent: str, ctx: ToolContext) -> HostPort:
    """A port over the agent's own read-only built-ins (`HOST_READ_TOOLS[agent]`), run on the host
    with the run's context; only the result text goes back to the guest."""
    allowed = HOST_READ_TOOLS.get(agent, ())
    by_name = {t.spec.name: t for t in builtins if t.spec.name in allowed}

    def port(tool: str, args: dict[str, Any]) -> HostExchange:
        t = by_name.get(tool)
        if t is None:
            return HostExchange(tool=tool, args=args, ok=False, content=f"不能调用 {tool}")
        try:
            r = t.run(dict(args), ctx)
        except Exception as e:  # noqa: BLE001 - a host read failing is an error result for the guest
            return HostExchange(tool=tool, args=args, ok=False, content=f"读取失败：{type(e).__name__}")
        return HostExchange(tool=tool, args=args, ok=r.error is None, content=r.content)

    return port


@dataclass
class ForgeRun:
    agent: str
    reachable: dict[str, ForgedTool]   # opted-in registry tools + overlay (mutable: request_tool adds)
    first_class: tuple[str, ...]       # opted-in, top FIRST_CLASS_MAX by search(query), sorted by name
    request_tool_on: bool
    user_id: str
    run_id: str
    group: PurposeGroup
    runtime: ForgeRuntime
    session_id: str | None = None
    titles: dict[str, str] = field(default_factory=dict)
    version: int = 0                   # the user's registry head when pinned (H12)
    global_version: int = 0
    requested: int = 0                 # request_tool forgings this run (cap REQUEST_TOOL_MAX_PER_RUN)
    on_cost: Callable[[float | None], None] | None = None   # see `ForgeRuntime.for_run`

    def __post_init__(self) -> None:
        # what the tool list is built from: fixed at pin time, never from the mutable overlay
        self._pinned = frozenset(self.reachable)
        self._first = {n: self.reachable[n] for n in self.first_class}

    @property
    def origin(self) -> str:
        if self.agent == "assistant" and self.session_id:
            return f"session:{self.session_id}"
        return f"run:{self.run_id}"

    def title(self, name: str) -> str:
        return self.titles.get(name) or name

    def tools(self, builtins: Sequence[Tool], ctx: ToolContext) -> list[Tool]:
        """The forged tools to append to the agent's built-in list, fixed for the whole run:
        first-class SandboxedTools (host port = the built-ins named in HOST_READ_TOOLS[agent],
        run with `ctx` on the host), then `call_tool` when anything is reachable or request_tool is
        on, `find_tool` when reachable > first-class, `request_tool` when on. Never changes
        during the run (prompt cache: tool defs head the cached prefix). Fail closed: a forged or
        meta name equal to one of the agent's actual built-ins (the pin only knows the static
        `harness.tool_names.TOOLS`, which can lag) → no forged tools at all, logged at error level;
        the same built-ins give the same answer every round, so the list stays fixed."""
        names = {t.spec.name for t in builtins}
        clash = sorted(names & (self._pinned | {CALL_TOOL, FIND_TOOL, REQUEST_TOOL}))
        if clash:
            log.error("forged tools %s collide with %s's built-ins: no forged tools this run %s",
                      clash, self.agent, self.run_id)
            return []
        port = host_port(builtins, self.agent, ctx)
        out: list[Tool] = [SandboxedTool(self._first[n], self.runtime.sandbox, host=port)
                           for n in self.first_class]
        if self._pinned or self.request_tool_on:
            out.append(CallToolTool(self, port))
        if len(self._pinned) > len(self.first_class):
            out.append(FindToolTool(self))
        if self.request_tool_on:
            out.append(RequestToolTool(self, builtins))
        return out


# --- meta tools -------------------------------------------------------------------------------

class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CallArgs(_Args):
    name: str = Field(min_length=1, max_length=64, description="工具名")
    args: dict[str, Any] = Field(default_factory=dict, description="按该工具的参数说明填写")


class FindArgs(_Args):
    query: str = Field(min_length=1, max_length=500, description="想做的事")


class ExampleArgs(_Args):
    args: dict[str, Any]
    expect: dict[str, Any]


class RequestArgs(_Args):
    name: str = Field(description="英文小写加下划线，如 check_balance")
    title: str = Field(description="2–16 字的中文显示名，如 配平检查")
    purpose: str = Field(description="工具要做什么，输入输出各是什么")
    examples: list[ExampleArgs] = Field(min_length=1, max_length=6,
                                        description="具体的输入和正确输出")


# request_tool forges pure tools only: the examples are model-written, so a tool reading host data
# has no recorded host results to build against and its held-out examples would always fail after
# the coder was paid (spec §11). Refused before any model call; the agent reads the data itself
# and passes it in as arguments.
PURE_ONLY = ("request_tool 只能准备纯计算的工具（不读取数据）：先用你自己的工具把需要的内容读出来，"
             "再作为参数传给新工具；去掉 host_tools 重新请求")


# Tool descriptions (P3 T1). The P2 T13 live run: the assistant hand-computed 20 z-scores instead
# of preparing a tool, because the old text only said when NOT to. Anthropic tool-use guidance:
# say when to use it, with the concrete trigger shape and the gain; the agent decides. No code
# triggers (user rule: fix generation in prompt / harness). Constant per run -> prompt cache safe;
# harness H2 may tune these texts later.
REQUEST_TOOL_DESCRIPTION = (
    "准备一个新的小工具，之后用 call_tool 调用。"
    "该用的时候：同一种计算、换算或检查要对很多项逐个做（3 个以上，例如一批成绩换算成 z 分数、"
    "检查一组方程式是否配平），或者要判断两个答案是否等价（如 1/2 和 0.5、x(x+1) 和 x²+x）、"
    "格式是否合规，而现有工具做不到。这时先准备工具，再一次调用处理全部，比你逐个手算更准，"
    "也更省步骤；准备一次，这次对话里都能用。"
    "怎么填：name 用英文小写加下划线；给 2–6 组具体的输入和你确信正确的输出；"
    "新工具只做纯计算，不读取数据，需要的内容先用你自己的工具读出来，作为参数传入。"
    "不用的时候：只算一两次，或现有工具已经能做。"
    "回复用户时只说结果，不提准备工具的过程。")
CALL_TOOL_DESCRIPTION = (
    "按名字调用一个可用的工具（find_tool 能找到的，或 request_tool 刚准备好的）。"
    "args 按该工具的参数说明填写；要处理很多项时，把它们放进一次调用的参数里，不要逐个调用。")


def _spec(name: str, description: str, model: type[BaseModel], category: str) -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters=model.model_json_schema(),
                    effect="read", category=category)  # type: ignore[arg-type]


def _errors(e: ValidationError) -> str:
    return "；".join(f"{'.'.join(map(str, x['loc'])) or '参数'}: {x['msg']}"
                    for x in e.errors(include_url=False))


class CallToolTool:
    """Runs any reachable forged tool (first-class or not) through `SandboxedTool`."""

    def __init__(self, run: ForgeRun, port: HostPort):
        self.run_ = run
        self.port = port
        self.spec = _spec(CALL_TOOL, CALL_TOOL_DESCRIPTION, CallArgs, "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            a = CallArgs.model_validate(args)
        except ValidationError as e:
            return fail(f"参数不合法：{_errors(e)}")
        r = self.run_
        tool = r.reachable.get(a.name)
        if tool is None:
            names = "、".join(sorted(r.reachable)[:10]) or "（暂无）"
            return fail(f"没有这个工具：{a.name}。可用的有：{names}")
        res = SandboxedTool(tool, r.runtime.sandbox, host=self.port).run(a.args, ctx)
        return res.model_copy(update={"summary": f"用了「{r.title(a.name)}」"})


class FindToolTool:
    """The top `FIND_TOOL_K` reachable tools for a query: name, title, description, arguments."""

    def __init__(self, run: ForgeRun):
        self.run_ = run
        self.spec = _spec(FIND_TOOL, "按要做的事查找可用的工具，返回名字、说明和参数；找到后用 "
                          "call_tool 调用。", FindArgs, "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            a = FindArgs.model_validate(args)
        except ValidationError as e:
            return fail(f"参数不合法：{_errors(e)}")
        r, rt = self.run_, self.run_.runtime
        if not r.reachable:
            return ToolResult(content="没有可用的工具", summary=a.query[:200])
        with rt.pool.connection() as conn:
            index.ensure(conn, rt.embedder, [(t, r.title(n)) for n, t in r.reachable.items()])
            hits = index.search(conn, rt.embedder, a.query, r.reachable, FIND_TOOL_K)
        lines = []
        for name, _ in hits:
            schema = r.reachable[name].schema
            lines.append(json.dumps({
                "name": name, "title": r.title(name),
                "description": str(schema.get("description", "")),
                "parameters": {"properties": schema.get("properties") or {},
                               "required": schema.get("required") or []}},
                ensure_ascii=False, separators=(",", ":")))
        return ToolResult(content=clip("\n".join(lines), RESULT_CHARS), summary=a.query[:200])


class RequestToolTool:
    """Forge a missing tool mid-run, for this run only (run-scoped overlay; never the registry).

    A `draft` tool: it runs in the workflow body; the reuse check and the build are one recorded
    step ("forge.request_tool"), never re-run on replay; adding the built tool to the run's
    reachable set happens in the body from the recorded JSON, so a recovered run has it again."""

    def __init__(self, run: ForgeRun, builtins: Sequence[Tool]):
        self.run_ = run
        self.builtin_names = frozenset(t.spec.name for t in builtins)
        self.spec = _spec(REQUEST_TOOL, REQUEST_TOOL_DESCRIPTION, RequestArgs, "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = self.run_
        args = dict(args)
        if args.pop("host_tools", None):       # pure tools only, before any model call
            return fail(PURE_ONLY).model_copy(
                update={"summary": f"准备「{str(args.get('title') or '')[:16]}」"})
        try:
            a = RequestArgs.model_validate(args)
            req = ToolRequest(name=a.name, title=a.title, purpose=a.purpose, agent=r.agent,  # type: ignore[arg-type]
                              examples=tuple(ToolExample(args=e.args, expect=e.expect)
                                             for e in a.examples),
                              scope="user")
        except ValidationError as e:
            return fail(f"参数不合法：{_errors(e)}")
        summary = f"准备「{req.title}」"
        if req.name in self.builtin_names:
            return fail(f"名字 {req.name} 和现有工具重名，换一个").model_copy(
                update={"summary": summary})
        if req.name in r.reachable:
            return ToolResult(content=f"已有可用的工具「{r.title(req.name)}」({req.name})，用 call_tool 调用",
                              summary=summary)
        if r.requested >= REQUEST_TOOL_MAX_PER_RUN:
            return fail("这次已经准备过新工具了，请用现有工具完成").model_copy(
                update={"summary": summary})
        out = durable.step("forge.request_tool", lambda: self._request(req))
        if out.get("reused"):
            name = str(out["reused"])
            return ToolResult(content=f"已有可用的工具「{r.title(name)}」({name})，用 call_tool 调用",
                              summary=summary)
        r.requested += 1
        if r.on_cost is not None:          # fresh and replayed alike: the recorded build cost
            r.on_cost(out.get("cost_usd"))
        if not out.get("ok"):
            return fail(str(out.get("reason") or UNAVAILABLE)).model_copy(
                update={"summary": summary})
        tool = out["tool"]
        r.reachable[req.name] = _forged(req.name, {**tool, "candidate_id": out["candidate_id"]})
        r.titles[req.name] = str(tool["title"])
        props = ", ".join(sorted(tool["schema"].get("properties") or {}))
        return ToolResult(content=(f"工具「{tool['title']}」已就绪：{tool['schema'].get('description', '')}"
                                   f"（参数：{props or '无'}）；用 call_tool 调用。"),
                          summary=summary)

    def _request(self, req: ToolRequest) -> dict[str, Any]:
        """The recorded step: reuse check, then propose. JSON only."""
        r, rt = self.run_, self.run_.runtime
        if r.reachable:
            with rt.pool.connection() as conn:
                index.ensure(conn, rt.embedder, [(t, r.title(n)) for n, t in r.reachable.items()])
                near = index.nearest(conn, rt.embedder, f"{req.purpose} {req.title}", r.reachable)
            if near is not None and near[1] >= REUSE_SIM:
                return {"ok": False, "reused": near[0]}
        made: list[str] = []

        def new_id() -> str:
            made.append(new_candidate_id())
            return made[-1]

        deps = ForgeDeps(pool=rt.pool, sandbox=rt.sandbox, trace=rt.trace, now=rt.clock,
                         converse=ForgeConverse(gateway=rt.gateway,
                                                account=Account(user_id=r.user_id, group=r.group)),
                         origin=r.origin, new_id=new_id)
        evidence = Evidence(citations=(f"trace:{r.run_id or 'untraced'}",), note=req.purpose)

        def failed(reason: str) -> dict[str, Any]:
            """A build that raised: the candidate's stored cost (0 before the candidate existed)."""
            cost: float | None = 0.0
            if made:
                with rt.pool.connection() as conn:
                    c = store.get_candidate(conn, made[0])
                cost = c.cost_usd if c is not None else 0.0
            return {"ok": False, "candidate_id": made[0] if made else None, "reason": reason,
                    "tool": None, "cost_usd": cost}

        try:
            res = propose("tool", req, evidence, user_id=r.user_id, deps=deps)
        except (ForgeDisabled, ForgeMisconfigured, SandboxUnavailable):
            log.warning("request_tool unavailable for %s run %s", r.agent, r.run_id, exc_info=True)
            return failed(UNAVAILABLE)
        except Exception:
            log.exception("request_tool failed for %s run %s", r.agent, r.run_id)
            return failed("这次没能生成工具")
        b = res.build
        if not b.ok:
            return {"ok": False, "candidate_id": res.candidate_id, "reason": b.reason,
                    "tool": None, "cost_usd": b.cost_usd}
        with rt.pool.connection() as conn:
            t = registry._load(conn, r.user_id, res.candidate_id)
        return {"ok": True, "candidate_id": res.candidate_id, "reason": "",
                "tool": {k: v for k, v in _tool_json(t, req.title).items() if k != "candidate_id"},
                "cost_usd": b.cost_usd}
