"""Repeated-work detection over agent traces (plan 2026-10-22 P2 T9; design §1 row 4, §7).

Agent Workflow Memory (AWM, Wang et al. 2024) and Agent Skill Induction (ASI, Wang et al. 2025):
a sub-trajectory that recurs across successful runs is induced into a reusable program. Phase 2
mines one pattern, the one the spec names (the page writer calling `read_page` six times per page
to collect prerequisites): **read fan-out** — one host-readable tool (`HOST_READ_TOOLS[agent]`)
called with ≥ `DETECT_FANOUT_MIN` different values of its single argument in one run, in
≥ `DETECT_RUNS_MIN` distinct finished runs of the same user and agent within
`DETECT_WINDOW_DAYS`. Error results are ignored. Fixed-order chains of different tools are
deferred to a later phase.

`to_request` turns a finding into a batch-tool `ToolRequest` whose examples are mechanical (the
recorded calls and their results), so they are evidence, not model-written: the host cassette of
each example is the run's exact read calls (VCR strict replay, T5) and the expected output is the
recorded results in order."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from evowiki.forge import ids, limits
from evowiki.forge.limits import HOST_READ_TOOLS
from evowiki.forge.models import HostExchange, ToolExample, ToolRequest
from evowiki.store.db import fetch_all

# agent_run.agent (the AgentSpec / purpose name a run is traced under) -> forge agent
TRACE_AGENTS: Mapping[str, str] = {"deep.write": "deep", "assistant.turn": "assistant"}
SUCCESS_STOPS = frozenset({"finished", "answered"})
TITLES: Mapping[str, str] = {"read_page": "一次读多页", "read_k": "一次读多个知识点",
                             "search_wiki": "一次搜多个词"}


@dataclass(frozen=True)
class ToolStep:
    name: str
    args: Mapping[str, Any]
    content: str
    error: str | None = None


@dataclass(frozen=True)
class RunTrace:
    agent: str                      # forge agent: "deep" / "assistant"
    user_id: str
    stop: str | None
    started_at: datetime
    steps: tuple[ToolStep, ...]     # tool steps in order


@dataclass(frozen=True)
class RepeatedWork:
    agent: str
    tool: str                       # e.g. "read_page"
    arg: str                        # the varying argument, e.g. "page_id"
    runs: tuple[str, ...]           # run ids, newest first (evidence)
    samples: tuple[tuple[HostExchange, ...], ...]   # per run: the fan-out calls in order


def _scalar(v: Any) -> bool:
    return isinstance(v, str | int) and not isinstance(v, bool)


def _fanouts(trace: RunTrace) -> dict[tuple[str, str], tuple[HostExchange, ...]]:
    """{(tool, arg): the run's fan-out calls} — successful single-argument calls of a host-readable
    tool, first call per distinct value, in order; only when ≥ DETECT_FANOUT_MIN values."""
    allowed = HOST_READ_TOOLS.get(trace.agent, ())
    seen: dict[tuple[str, str], dict[str, HostExchange]] = {}
    for s in trace.steps:
        if s.name not in allowed or s.error or len(s.args) != 1:
            continue
        [(arg, value)] = s.args.items()
        if not _scalar(value) or len(s.content) > limits.DETECT_RESULT_CHARS:
            continue
        calls = seen.setdefault((s.name, arg), {})
        key = json.dumps(value)
        if key not in calls:
            calls[key] = HostExchange(tool=s.name, args={arg: value}, ok=True, content=s.content)
    return {k: tuple(v.values()) for k, v in seen.items()
            if len(v) >= limits.DETECT_FANOUT_MIN}


def find(steps_by_run: Mapping[str, RunTrace]) -> list[RepeatedWork]:
    """Pure. Fan-out patterns seen in ≥ DETECT_RUNS_MIN distinct successful runs of the same user
    and agent. Ordered by the number of runs (most first), then agent, tool, argument."""
    groups: dict[tuple[str, str, str, str], list[tuple[datetime, str, tuple[HostExchange, ...]]]]
    groups = {}
    for run_id, trace in steps_by_run.items():
        if trace.stop not in SUCCESS_STOPS:
            continue
        for (tool, arg), calls in _fanouts(trace).items():
            groups.setdefault((trace.user_id, trace.agent, tool, arg), []).append(
                (trace.started_at, run_id, calls))
    found: list[RepeatedWork] = []
    for (_, agent, tool, arg), hits in groups.items():
        if len(hits) < limits.DETECT_RUNS_MIN:
            continue
        hits.sort(key=lambda h: (h[0], h[1]), reverse=True)       # newest first
        hits = hits[:limits.DETECT_SAMPLES_MAX]
        found.append(RepeatedWork(agent=agent, tool=tool, arg=arg,
                                  runs=tuple(h[1] for h in hits),
                                  samples=tuple(h[2] for h in hits)))
    found.sort(key=lambda w: (-len(w.runs), w.agent, w.tool, w.arg))
    return found


def load(conn: psycopg.Connection, user_id: str, *, now: datetime) -> dict[str, RunTrace]:
    """The user's finished runs of a known agent started in the window, with their tool steps."""
    ids.check_user(user_id)
    since = now - timedelta(days=limits.DETECT_WINDOW_DAYS)
    runs = fetch_all(
        conn, "select run_id, agent, stop, started_at from agent_run where user_id = %s "
              "and started_at >= %s and started_at <= %s and finished_at is not null "
              "and stop = any(%s) and agent = any(%s) order by started_at, run_id",
        (user_id, since, now, sorted(SUCCESS_STOPS), sorted(TRACE_AGENTS)))
    if not runs:
        return {}
    rows = fetch_all(
        conn, "select run_id, payload from agent_step where run_id = any(%s) and kind = 'tool' "
              "order by run_id, seq", ([r["run_id"] for r in runs],))
    steps: dict[str, list[ToolStep]] = {}
    for r in rows:
        p = r["payload"]
        result = p.get("result") or {}
        args = p.get("args")
        steps.setdefault(r["run_id"], []).append(ToolStep(
            name=str(p.get("name", "")), args=args if isinstance(args, dict) else {},
            content=str(result.get("content", "")), error=result.get("error")))
    return {r["run_id"]: RunTrace(agent=TRACE_AGENTS[r["agent"]], user_id=user_id,
                                  stop=r["stop"], started_at=r["started_at"],
                                  steps=tuple(steps.get(r["run_id"], ())))
            for r in runs}


def _title(tool: str) -> str:
    return TITLES.get(tool, f"一次调用多次{tool}")[:16]


def _purpose(w: RepeatedWork) -> str:
    plural = f"{w.arg}s"
    return (f"一次读取多个 {w.arg}：参数 {plural} 是 {w.arg} 的列表；按顺序对每个值调用一次 "
            f"host.call(\"{w.tool}\", {{\"{w.arg}\": 值}})，返回 {{\"results\": [每次调用返回的"
            f"文本，顺序与 {plural} 相同]}}。任何一次调用出错就让它报错，不要吞掉。"
            f"在 {len(w.runs)} 次已完成的运行里，每次都逐个调用了 {w.tool} 至少 "
            f"{limits.DETECT_FANOUT_MIN} 次。")


def to_request(w: RepeatedWork) -> ToolRequest:
    """A batch tool whose examples are the recorded runs: name f"batch_{tool}", a short title
    (e.g. 「一次读多页」), host_tools=(tool,), args {f"{arg}s": [values in order]}, host cassette =
    the recorded exchanges, expect {"results": [contents in order]}. One example per sample run
    (≥ 3, so the build holds one out)."""
    examples = tuple(
        ToolExample.model_validate({
            "args": {f"{w.arg}s": [h.args[w.arg] for h in sample]},
            "expect": {"results": [h.content for h in sample]},
            "host": [h.model_dump(mode="json") for h in sample]})
        for sample in w.samples)
    return ToolRequest.model_validate({
        "name": f"batch_{w.tool}", "title": _title(w.tool), "purpose": _purpose(w),
        "agent": w.agent, "examples": examples, "scope": "user", "host_tools": (w.tool,)})


def sample_values(w: RepeatedWork) -> Sequence[Sequence[Any]]:
    """The argument values of each sample run (for printing what a round would ask for)."""
    return [[h.args[w.arg] for h in s] for s in w.samples]
