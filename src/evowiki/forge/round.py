"""One forge round: the forge's own improver loop until harness 3b supplies the real gate (plan
2026-10-22 P2 T9; design §6.1, §7).

detected repeated work (detect.py, AWM / ASI) → requests minus duplicates (Voyager: reuse an
existing tool rather than forge a near-duplicate) → `propose` each (origin "round"; every
candidate is archived, failures too — DGM) → for built ones `gate.evaluate` → `promote(gate_ok=
gate.ok)` → local opt-in → description vector.

The gate is pluggable. The default `NoGate` passes nothing, so before harness 3b nothing
auto-promotes (fail closed): built candidates wait in the archive and 3b's round promotes them."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import psycopg

from evowiki.forge import detect, ids, index, optin, registry, switch
from evowiki.forge.api import ForgeDeps, propose
from evowiki.forge.limits import REUSE_SIM, ROUND_MAX_PROPOSALS
from evowiki.forge.models import Evidence, ForgeDisabled, ToolRequest
from evowiki.retrieval.embed import Embedder
from evowiki.store import forge as store

# candidate statuses that make a same-name request a duplicate (a failed build may be retried)
_TAKEN = ("building", "built", "promoted")


class Gate(Protocol):
    def evaluate(self, conn: psycopg.Connection, candidate_id: str, user_id: str) -> str | None:
        """A gate record id when the candidate passed (replay + blind judge + anchors: harness 3b),
        else None."""
        ...

    def ok(self, gate_ref: str) -> bool: ...


class NoGate:
    """Default until harness 3b: nothing passes, so nothing auto-promotes (fail closed). Built
    candidates wait in the archive; 3b's round promotes them."""

    def evaluate(self, conn: psycopg.Connection, candidate_id: str, user_id: str) -> str | None:
        return None

    def ok(self, gate_ref: str) -> bool:
        return False


@dataclass(frozen=True)
class RoundResult:
    proposed: tuple[str, ...]
    built: tuple[str, ...]
    promoted: tuple[str, ...]
    skipped: tuple[str, ...]        # plain reasons (duplicate, already promoted, ...)
    cost_usd: float | None


def _plan(conn: psycopg.Connection, user_id: str, *, now: datetime, embedder: Embedder
          ) -> tuple[list[tuple[ToolRequest, Evidence]], list[str]]:
    found = detect.find(detect.load(conn, user_id, now=now))
    if not found:
        return [], []
    reachable = registry.view(conn, user_id, enabled=True).tools
    kept: list[tuple[ToolRequest, Evidence]] = []
    skipped: list[str] = []
    names: set[str] = set()
    for w in found:
        req = detect.to_request(w)
        if req.name in names:
            skipped.append(f"「{req.title}」本轮已在生成")
            continue
        same = [c for c in store.archive(conn, user_id, req.name, limit=100000)
                if c.status in _TAKEN]
        if same:
            word = "已经上线" if any(c.status == "promoted" for c in same) else "已在候选中"
            skipped.append(f"「{req.title}」{word}")
            continue
        if reachable:
            near = index.nearest(conn, embedder, f"{req.purpose} {req.title}", reachable)
            if near is not None and near[1] >= REUSE_SIM:
                skipped.append(f"「{req.title}」和已有的工具 {near[0]} 重复")
                continue
        names.add(req.name)
        kept.append((req, Evidence(
            citations=tuple(f"trace:{r}" for r in w.runs),
            note=f"在 {len(w.runs)} 次运行里都逐个调用 {w.tool} 读取多个 {w.arg}")))
    return kept, skipped


def requests(conn: psycopg.Connection, user_id: str, *, now: datetime, embedder: Embedder
             ) -> list[tuple[ToolRequest, Evidence]]:
    """Detected requests minus duplicates: a candidate of the same name in the user's scope that is
    building / built / promoted, or a reachable tool with nearest() ≥ REUSE_SIM. Harness 3b's
    improver may call this to get forge requests from traces."""
    return _plan(conn, user_id, now=now, embedder=embedder)[0]


def _cost(costs: Sequence[float | None]) -> float | None:
    """None when any proposal has no price (same as `ToolContext.spent_usd`)."""
    return None if any(c is None for c in costs) else float(sum(c or 0.0 for c in costs))


def run_round(user_id: str, *, deps: ForgeDeps, gate: Gate, embedder: Embedder, now: datetime,
              max_proposals: int = ROUND_MAX_PROPOSALS) -> RoundResult:
    """requests → propose each (origin "round") → for built ones gate.evaluate → promote(gate_ok=
    gate.ok) → optin.set_local(user, agent, name, True) → index.ensure. Every candidate is
    archived (DGM); the switch off → ForgeDisabled before anything. A propose exception
    (misconfigured converse, model or sandbox failure) propagates: the round stops, visibly."""
    ids.check_user(user_id)
    with deps.pool.connection() as conn:
        if not switch.enabled(conn, user_id, "tool"):
            raise ForgeDisabled("工具自动生成已关闭")
        plan, skipped = _plan(conn, user_id, now=now, embedder=embedder)
    for req, _ in plan[max(0, max_proposals):]:
        skipped.append(f"「{req.title}」留到下一轮（本轮最多 {max_proposals} 个）")
    proposed: list[str] = []
    built: list[str] = []
    promoted: list[str] = []
    costs: list[float | None] = []
    for req, evidence in plan[:max(0, max_proposals)]:
        result = propose("tool", req, evidence, user_id=user_id, deps=deps)
        cid = result.candidate_id
        proposed.append(cid)
        costs.append(result.build.cost_usd)
        if not result.build.ok:
            continue
        built.append(cid)
        with deps.pool.connection() as conn:
            ref = gate.evaluate(conn, cid, user_id)
            if ref is None or not gate.ok(ref):
                continue
            registry.promote(conn, cid, gate_ref=ref, by="round", why=evidence.note,
                             gate_ok=gate.ok, now=now)
            optin.set_local(conn, user_id, req.agent, req.name, True, now)
            tool = registry.view(conn, user_id, enabled=True).tools[req.name]
            index.ensure(conn, embedder, [(tool, req.title)])
        promoted.append(cid)
    return RoundResult(proposed=tuple(proposed), built=tuple(built), promoted=tuple(promoted),
                       skipped=tuple(skipped), cost_usd=_cost(costs))
