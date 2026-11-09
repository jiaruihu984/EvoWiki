"""The forged-tool registry as an agent run sees it (design 2026-10-22 §5.3, §6.1, §6.2).

`view()` pins the registry when a run starts (global head, then the user's head on top, like the
harness pack), so a promote during the run is not seen by it. `with_candidate` is the run-scoped
overlay a replay uses (like `Gateway.with_harness`): a new view, never a registry write. Forged
code only ever runs through `Sandbox.run_tool`, and only with the tool's own arguments: no
`ToolContext` (no user data) reaches it in phase 1. Every refusal fails closed."""

import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import jsonschema
import psycopg

from evowiki.agent.tool_helpers import clip, fail
from evowiki.agent.tools import ToolContext, ToolResult, ToolSpec
from evowiki.forge import ids
from evowiki.forge.limits import HOST_READ_TOOLS
from evowiki.forge.models import (
    RESERVED_NAMES,
    ForgeDisabled,
    ForgeForbidden,
    ForgeNotFound,
    PromoteRefused,
    RunOutcome,
    TestReport,
)
from evowiki.forge.sandbox import HostPort, HostSpec, Sandbox, SandboxUnavailable
from evowiki.harness.tool_names import TOOLS
from evowiki.store import forge as store

GLOBAL = ids.GLOBAL
RESERVED = RESERVED_NAMES        # the run's meta tools: refused as forged-tool names, like built-ins
MASK = "mask."                   # forge_setting (user, "mask.<name>") = a global candidate hidden for that user
OUTPUT_CHARS = 4000
# Statuses whose build passed: a promoted candidate later rolled back is still a sound build.
_USABLE = frozenset({"built", "promoted", "rolled_back"})
_LIMIT_TEXT = {"timeout": "运行超时", "fuel": "计算量超出上限", "memory": "内存超出上限",
               "output_cap": "输出太长"}


@dataclass(frozen=True)
class ForgedTool:
    name: str
    candidate_id: str
    files: Mapping[str, str]
    schema: Mapping[str, Any]
    host_tools: tuple[str, ...] = ()   # from the candidate's spec (never its files), re-validated


class SandboxedTool:
    """A forged tool behind the `Tool` protocol: effect "read", category "read" (its result is
    recorded and not re-run on replay); `run` goes into the sandbox. With `host` (a port over the
    agent's own read-only tools, built by the caller) and declared `host_tools`, the tool may read
    through exactly those tools; otherwise it gets no host at all."""

    def __init__(self, forged: ForgedTool, sandbox: Sandbox, host: HostPort | None = None):
        self.forged = forged
        self.sandbox = sandbox
        self.host = host
        schema = dict(forged.schema)
        self.spec = ToolSpec(name=forged.name, description=str(schema["description"])[:1000],
                             parameters=schema, effect="read", category="read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        del ctx                                   # never passed on: no user data into forged code
        try:
            jsonschema.validate(args, dict(self.forged.schema))
        except jsonschema.ValidationError as e:
            where = "/".join(map(str, e.absolute_path)) or "参数"
            return fail(f"参数不符合要求（{where}）：{e.message}")
        try:
            if self.forged.host_tools and self.host is not None:
                out = self.sandbox.run_tool(self.forged.files, args,
                                            HostSpec(self.forged.host_tools, self.host))
            else:
                out = self.sandbox.run_tool(self.forged.files, args)
        except SandboxUnavailable:
            return fail("这个工具现在不能运行")
        if not out.ok:
            return fail(_why(out))
        return ToolResult(content=clip(json.dumps(out.output, ensure_ascii=False), OUTPUT_CHARS))


def _why(out: RunOutcome) -> str:
    limit = _LIMIT_TEXT.get(out.stats.exit)
    if limit:
        return limit
    return out.error or "运行出错"


class _LazyWasi:
    """The default sandbox: the WASI runtime, started on the first call. If it is missing the call
    fails with `SandboxUnavailable` (never a fallback to running unsandboxed)."""

    policy = "read-only"

    def __init__(self) -> None:
        self._sb: Sandbox | None = None

    def _get(self) -> Sandbox:
        if self._sb is None:
            try:
                from evowiki.forge.sandbox.wasi import WasiSandbox
            except ImportError as e:
                raise SandboxUnavailable(f"WASI sandbox not installed: {e}") from e
            self._sb = WasiSandbox.from_env()
        return self._sb

    def run_tool(self, files: Mapping[str, str], args: dict[str, Any],
                 host: HostSpec | None = None) -> RunOutcome:
        return self._get().run_tool(files, args, host)

    def run_tests(self, files: Mapping[str, str]) -> TestReport:
        return self._get().run_tests(files)


@dataclass(frozen=True)
class RegistryView:
    version: int                      # user scope head when pinned (global head kept in global_version)
    global_version: int
    tools: Mapping[str, ForgedTool]   # resolved: global then user overlay
    enabled: bool
    sandbox: Sandbox
    _loader: Callable[[str], ForgedTool] = field(repr=False, compare=False)

    def with_candidate(self, candidate_id: str) -> "RegistryView":
        """This view plus one built candidate (replacing a same-name tool), for one run only. The
        candidate is loaded now, so call it while the connection given to `view()` is open."""
        if not self.enabled:
            raise ForgeDisabled("forged tools are switched off")
        tool = self._loader(candidate_id)
        return replace(self, tools=MappingProxyType({**self.tools, tool.name: tool}))

    def tools_for(self, agent: str, opted_in: Collection[str]) -> list[SandboxedTool]:
        """The forged tools `agent` may offer this run: those opted in (H3), sorted by name."""
        builtin = _builtin(agent)
        if not self.enabled:
            return []
        names = sorted(n for n in self.tools if n in opted_in)
        _refuse_collisions(agent, names, builtin)
        return [SandboxedTool(self.tools[n], self.sandbox) for n in names]

    def tool_names(self, agent: str) -> Mapping[str, tuple[str, ...]]:
        """{tool: sorted argument names} for harness/tool_names (H2/H3 keys)."""
        builtin = _builtin(agent)
        if not self.enabled:
            return MappingProxyType({})
        _refuse_collisions(agent, self.tools, builtin)
        return MappingProxyType({
            n: tuple(sorted(dict(t.schema.get("properties") or {})))
            for n, t in sorted(self.tools.items())})


def _builtin(agent: str) -> Collection[str]:
    if agent not in TOOLS:
        raise ValueError(f"unknown agent {agent!r}")
    return TOOLS[agent]


def _refuse_collisions(agent: str, names: Collection[str], builtin: Collection[str]) -> None:
    clash = sorted(n for n in names if n in builtin or n in RESERVED)
    if clash:
        raise ValueError(f"forged tool name(s) {clash} collide with {agent}'s built-in or "
                         f"meta tools")


def masks(conn: psycopg.Connection, user_id: str) -> dict[str, str]:
    """{tool name: global candidate id} the user has taken down for themselves only."""
    return {k[len(MASK):]: str(v)
            for k, v in store.settings_with_prefix(conn, user_id, MASK).items()
            if isinstance(v, str)}


def _load(conn: psycopg.Connection, user_id: str, candidate_id: str) -> ForgedTool:
    c = store.get_candidate(conn, candidate_id)
    if c is None or c.user_id not in (user_id, None):
        raise ForgeNotFound(f"no forged tool candidate {candidate_id!r}")
    if c.kind != "tool" or not c.name or c.status not in _USABLE:
        raise ForgeNotFound(f"candidate {candidate_id!r} is not a built tool (status {c.status})")
    try:
        schema = json.loads(c.files.get("schema.json", ""))
    except ValueError:
        schema = None
    if ("tool.py" not in c.files or not isinstance(schema, dict)
            or not isinstance(schema.get("description"), str)):
        raise ForgeNotFound(f"candidate {candidate_id!r} has no tool.py or no valid schema.json")
    return ForgedTool(name=c.name, candidate_id=c.id, files=MappingProxyType(dict(c.files)),
                      schema=MappingProxyType(schema), host_tools=_host_tools(c.id, c.spec))


def _host_tools(candidate_id: str, spec: Any) -> tuple[str, ...]:
    """The candidate's declared host tools, validated again against `HOST_READ_TOOLS` of its agent
    (the stored spec is not trusted blindly); anything off fails closed."""
    spec = spec if isinstance(spec, Mapping) else {}
    raw = spec.get("host_tools") or ()
    if not raw:
        return ()
    allowed = HOST_READ_TOOLS.get(str(spec.get("agent")), ())
    if (not isinstance(raw, (list, tuple)) or not all(isinstance(t, str) for t in raw)
            or not set(raw) <= set(allowed)):
        raise ForgeNotFound(f"candidate {candidate_id!r} declares host tools it may not call")
    return tuple(raw)


def view(conn: psycopg.Connection, user_id: str, *, enabled: bool,
         sandbox: Sandbox | None = None) -> RegistryView:
    """Pin the registry for one run. `sandbox=None` uses the WASI sandbox (started on first use).
    Disabled (kill switch): no tools, and nothing is loaded. A global entry the user masked (their
    undo of a global change) is skipped for this user only; a newer global promote of the same
    name points at another candidate and shows again."""
    ids.check_user(user_id)
    gv, g_entries = store.registry_head(conn, GLOBAL)
    uv, u_entries = store.registry_head(conn, user_id)

    def loader(candidate_id: str) -> ForgedTool:
        return _load(conn, user_id, candidate_id)

    tools: dict[str, ForgedTool] = {}
    if enabled:
        hidden = masks(conn, user_id)
        g_entries = {n: c for n, c in g_entries.items() if hidden.get(n) != c}
        for name, cid in {**g_entries, **u_entries}.items():
            tool = loader(cid)
            if tool.name != name:
                raise ForgeNotFound(f"registry entry {name!r} points at tool {tool.name!r}")
            tools[name] = tool
    return RegistryView(version=uv, global_version=gv, tools=MappingProxyType(tools),
                        enabled=enabled, sandbox=sandbox if sandbox is not None else _LazyWasi(),
                        _loader=loader)


# --- promote / rollback (design §6.1) -------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(UTC)


def promote(conn: psycopg.Connection, candidate_id: str, *, gate_ref: str, by: str, why: str,
            gate_ok: Callable[[str], bool], now: datetime | None = None) -> int:
    """Point the candidate's tool name at it in a new registry version (scope = the candidate's
    user, or global). `gate_ok(gate_ref)` must say the gate passed: no default, so nobody promotes
    without a gate decision. Refuses (`PromoteRefused`) an empty ref, a failing gate, or a candidate
    that is not a built tool. Returns the new version."""
    if not gate_ref or not gate_ref.strip():
        raise PromoteRefused("no gate record given")
    c = store.get_candidate(conn, candidate_id)
    if c is None:
        raise ForgeNotFound(f"no forged tool candidate {candidate_id!r}")
    scope = GLOBAL if c.user_id is None else c.user_id
    store.lock_scope(conn, scope)
    c = store.get_candidate(conn, candidate_id)            # re-read under the lock
    if c is None or c.kind != "tool" or not c.name:
        raise PromoteRefused(f"candidate {candidate_id!r} is not a tool")
    if c.status != "built":
        raise PromoteRefused(f"candidate {candidate_id!r} is {c.status}, only built ones promote")
    if not gate_ok(gate_ref):
        raise PromoteRefused(f"gate {gate_ref!r} did not pass")
    stamp = now or _now()
    _, entries = store.registry_head(conn, scope)
    version = store.append_registry(
        conn, scope, {**entries, c.name: c.id},
        {"op": "promote", "candidate_id": c.id, "by": by, "why": why, "gate_ref": gate_ref}, stamp)
    store.set_status(conn, c.id, "promoted", stamp)
    return version


def change_id(scope: str, version: int) -> str:
    return f"forge:{scope}:{version}"


def parse_change_id(user_id: str, cid: str) -> tuple[str, int]:
    """(scope, version) of a change id, which must be in the user's scope or the global one."""
    parts = cid.split(":")
    if len(parts) < 3 or parts[0] != "forge" or not parts[-1].isdigit():
        raise ForgeNotFound(f"no such change {cid!r}")
    ids.check_user(user_id)
    scope = ":".join(parts[1:-1])
    if scope not in (user_id, GLOBAL):
        raise ForgeNotFound(f"no such change {cid!r}")
    return scope, int(parts[-1])


def undone_ids(history: list[store.RegistryRow]) -> set[str]:
    return {str(r.change["of_change"]) for r in history if r.change.get("op") == "rollback"}


def promote_row(conn: psycopg.Connection, user_id: str,
                change_id: str) -> tuple[str, store.RegistryRow, list[store.RegistryRow]]:
    """(scope, the promote row, the scope's history) of a change id; `ForgeNotFound` otherwise."""
    scope, version = parse_change_id(user_id, change_id)
    history = store.registry_history(conn, scope, limit=100000)
    row = next((r for r in history if r.version == version), None)
    if row is None or row.change.get("op") != "promote":
        raise ForgeNotFound(f"no such change {change_id!r}")
    return scope, row, history


def promoted_name(row: store.RegistryRow) -> tuple[str, str | None]:
    """(candidate id, tool name) a promote row installed."""
    cand = str(row.change.get("candidate_id") or "")
    return cand, next((n for n, c in row.entries.items() if c == cand), None)


def rollback(conn: psycopg.Connection, user_id: str, change_id: str, *, by: str,
             operator: bool = False, now: datetime | None = None) -> int | None:
    """Undo one promote, touching only the tool that promote installed: if its candidate is still
    the live entry for that name, the name goes back to the entry it had just before the promote
    (when that candidate is still promoted) or is dropped; every other entry, later promotes
    included, is kept. The candidate is marked rolled_back. Returns the new version; None if it
    was already undone. Unknown or non-promote changes raise `ForgeNotFound`.

    A change of the global scope removes the tool for every user, so it needs `operator=True`
    (the operator, or an automatic caller such as harness observe / the forge round); otherwise
    `ForgeForbidden`. A learner's own undo of a global change masks it instead (`undo.undo`)."""
    ids.check_user(user_id)
    scope, version = parse_change_id(user_id, change_id)
    if scope == GLOBAL and not operator:
        raise ForgeForbidden(f"only the operator may roll back the global change {change_id!r}")
    store.lock_scope(conn, scope)
    scope, row, history = promote_row(conn, user_id, change_id)
    if change_id in undone_ids(history):
        return None
    cand, name = promoted_name(row)
    entries = dict(store.registry_head(conn, scope)[1])
    if name is not None and entries.get(name) == cand:
        before = (store.registry_at(conn, scope, version - 1) or {}) if version > 1 else {}
        prior = before.get(name)
        prior_row = store.get_candidate(conn, prior) if prior else None
        if prior is not None and prior_row is not None and prior_row.status == "promoted":
            entries[name] = prior
        else:
            entries.pop(name, None)
    stamp = now or _now()
    new = store.append_registry(
        conn, scope, entries,
        {"op": "rollback", "of_change": change_id, "candidate_id": cand or None, "by": by}, stamp)
    if cand:
        store.set_status(conn, cand, "rolled_back", stamp)
    return new
