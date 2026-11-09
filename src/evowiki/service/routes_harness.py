"""GET /wiki/harness and /wiki/harness/history (design 2026-10-21 §3): the user's harness pack
next to the defaults, and the versions that built it; GET /wiki/traces/{run_id}: one agent run
with its steps resolved (§9.1), and /wiki/traces/otel/{trace_id} the same run by the id Phoenix
has (obs/). Read-only; changes go through Prompt 设置 and the improver.
The harness loop (§5, §13): GET /wiki/harness/rounds[/{id}] and /experience read its rounds and
lessons; POST /wiki/harness/run runs one round now under the user's RSI cycle lock (the cycle's
own deps, `rsi_runs._harness`). Its on/off and month limit are RsiSettings (/wiki/rsi/settings)."""

import calendar
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from evowiki.harness import keys as hk
from evowiki.harness.keys import KEYS
from evowiki.harness.round import HRound, run_round
from evowiki.memory.forget import hidden_of
from evowiki.obs.exporter import hidden_run
from evowiki.service.deps import Services, check_user, current_user, get_services
from evowiki.service.harness_defaults import defaults
from evowiki.service.rsi_runs import _exclusive, _harness
from evowiki.store import harness as store
from evowiki.store import harness_rsi, otel_export, trace
from evowiki.store.db import fetch_all
from evowiki.store.rsi import PgRsiStore
from evowiki.store.usage import month_start, usage_tz

router = APIRouter(prefix="/wiki")
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]


class HarnessKeyOut(BaseModel):
    name: str
    dim: str
    kind: str
    lo: int | None
    hi: int | None
    doc: str
    default: Any                  # H10 thinking: None (the provider's own setting)
    value: Any                    # None: the pack sets nothing, the default applies
    source: str | None
    approval: str                 # "auto" | "ask" (spec §7)
    options: list[str]            # enum: allowed values; list: allowed items


class HarnessOut(BaseModel):
    version: int
    keys: list[HarnessKeyOut]


class HarnessVersionOut(BaseModel):
    version: int
    key: str
    before: Any
    after: Any
    source: str
    why: str
    created_at: datetime


@router.get("/harness", response_model=HarnessOut)
def get_harness(svc: Svc, uid: User):
    check_user(svc, uid)
    with svc.pool.connection() as conn:
        pack = store.load_pack(conn, uid)
        sources = store.source_of(conn, uid)
    base = defaults()
    return HarnessOut(version=pack.version, keys=[
        HarnessKeyOut(name=k.name, dim=k.dim, kind=k.kind, lo=k.lo, hi=k.hi, doc=k.doc,
                      default=base[k.name], value=pack.get(k.name), source=sources.get(k.name),
                      approval=k.approval, options=list(k.options))
        for k in KEYS])


class ToolToggleOut(BaseModel):
    agent: str
    tool: str
    locked: bool


class HookOut(BaseModel):
    at: str
    kinds: list[str]
    tiers: list[str]
    tools: list[str]
    output_to: str


class StepTierOut(BaseModel):
    step: str
    purpose: str
    tiers: list[str]


class HarnessStructureOut(BaseModel):
    tools: list[ToolToggleOut]          # toggleable and locked, per agent
    hooks: list[HookOut]
    steps: list[StepTierOut]
    spec_parts: list[str]
    components: list[str]               # component ids in the library (for H8 note / preset keys)
    hook_max_tool_calls: int


@router.get("/harness/structure", response_model=HarnessStructureOut)
def get_structure(svc: Svc, uid: User):
    """The static allow-lists behind the structural keys (anchors in harness/keys.py)."""
    check_user(svc, uid)
    tools = [ToolToggleOut(agent=a, tool=t, locked=False)
             for a, ts in hk.TOGGLE_TOOLS.items() for t in ts]
    tools += [ToolToggleOut(agent=a, tool=t, locked=True)
              for a, ts in hk.LOCKED_TOOLS.items() for t in ts]
    return HarnessStructureOut(
        tools=tools,
        hooks=[HookOut(at=at, kinds=list(h.kinds), tiers=list(h.tiers), tools=list(h.tools),
                       output_to=h.output_to) for at, h in hk.HOOKS.items()],
        steps=[StepTierOut(step=s, purpose=p, tiers=list(t))
               for s, (p, t) in hk.STEPS_H10.items()],
        spec_parts=list(hk.SPEC_PARTS),
        components=[c.component_id for c in svc.library.list()] if svc.library is not None else [],
        hook_max_tool_calls=hk.HOOK_MAX_TOOL_CALLS)


@router.get("/harness/history", response_model=list[HarnessVersionOut])
def get_history(svc: Svc, uid: User, limit: Annotated[int, Query(ge=1, le=500)] = 50):
    check_user(svc, uid)
    with svc.pool.connection() as conn:
        rows = store.history(conn, uid, limit=limit)
    return [HarnessVersionOut(**vars(r)) for r in rows]


class TraceOut(BaseModel):
    run: dict[str, Any]
    steps: list[dict[str, Any]]


def _trace_out(conn, run_id: str, uid: str) -> TraceOut:
    run = trace.run(conn, run_id)
    # another user's run, or one an open forget request hides, is as missing as an unknown one
    if run is None or run["user_id"] != uid:
        raise HTTPException(404, "no such run")
    ancestors, _ = otel_export.lineage(conn, run)
    if hidden_run({"run_id": run_id, "input_session": (run["input"] or {}).get("session_id")},
                  hidden_of(conn, uid), ancestors):
        raise HTTPException(404, "no such run")
    return TraceOut(run=run, steps=trace.steps(conn, run_id))


@router.get("/traces/{run_id}", response_model=TraceOut)
def get_trace(run_id: str, svc: Svc, uid: User):
    check_user(svc, uid)
    with svc.pool.connection() as conn:
        return _trace_out(conn, run_id, uid)


@router.get("/traces/otel/{trace_id}", response_model=TraceOut)
def get_trace_by_otel_id(trace_id: str, svc: Svc, uid: User):
    """The run behind a Phoenix trace (its `evowiki.trace_url`): Phoenix holds no raw text and no
    run id, only this derived trace id (obs/spans.py)."""
    check_user(svc, uid)
    with svc.pool.connection() as conn:
        run_id = otel_export.run_by_trace_id(conn, uid, trace_id.lower())
        if run_id is None:
            raise HTTPException(404, "no such run")
        return _trace_out(conn, run_id, uid)


class RoundLineOut(BaseModel):
    round_id: str
    started_at: datetime
    status: str
    reason: str | None
    cost: float | None
    change_id: str | None         # the harness_change this round recorded (applied / asked)
    # that change's status now (asked / applied / rejected / undone / rolled_back): a round whose
    # change was later undone or rolled back reads as such (rounds are written once)
    change_status: str | None


class RoundsOut(BaseModel):
    month_cost: float
    month_projection: float       # month cost / elapsed days (at least one) * days in the month
    month_limit: float | None     # RsiSettings.harness_month_limit_usd; None = no cap
    rounds: list[RoundLineOut]


class RoundOut(BaseModel):
    round_id: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    reason: str | None
    finding: str | None
    signal_ids: Any
    cases: Any
    candidates: Any
    comparisons: Any
    decision: Any
    diff: Any
    cost: float | None
    change_id: str | None
    change_status: str | None     # as RoundLineOut's


class LessonOut(BaseModel):
    exp_id: str
    situation: Any
    change: Any
    evidence: Any
    verdict: str
    scope: str
    created_at: datetime
    weight: float


def _changes(conn, uid: str, round_ids: list[str]) -> dict[str, tuple[str, str]]:
    """round id -> (change id, its current status)."""
    if not round_ids:
        return {}
    rows = fetch_all(conn, "select round_id, change_id, status from harness_change "
                           "where user_id = %s and round_id = any(%s) order by created_at",
                     (uid, round_ids))
    return {r["round_id"]: (r["change_id"], r["status"]) for r in rows}


@router.get("/harness/rounds", response_model=RoundsOut)
def get_rounds(svc: Svc, uid: User, limit: Annotated[int, Query(ge=1, le=200)] = 20):
    check_user(svc, uid)
    now = svc.clock()
    tz = usage_tz()
    with svc.pool.connection() as conn:
        rows = harness_rsi.rounds(conn, uid, limit)
        changes = _changes(conn, uid, [r.round_id for r in rows])
        spent = harness_rsi.month_cost(conn, uid, now, tz)
    start = month_start(now, tz)
    elapsed = max((now.astimezone(tz) - start).total_seconds() / 86400, 1.0)
    days = calendar.monthrange(start.year, start.month)[1]
    return RoundsOut(
        month_cost=spent, month_projection=spent / elapsed * days,
        month_limit=PgRsiStore(svc.pool).settings(uid).harness_month_limit_usd,
        rounds=[RoundLineOut(round_id=r.round_id, started_at=r.started_at, status=r.status,
                             reason=r.reason, cost=r.cost,
                             change_id=changes.get(r.round_id, (None, None))[0],
                             change_status=changes.get(r.round_id, (None, None))[1])
                for r in rows])


@router.get("/harness/rounds/{round_id}", response_model=RoundOut)
def get_round(round_id: str, svc: Svc, uid: User):
    check_user(svc, uid)
    with svc.pool.connection() as conn:
        r = harness_rsi.get_round(conn, uid, round_id)
        # another user's round is as missing as an unknown one
        if r is None:
            raise HTTPException(404, "no such round")
        change, status = _changes(conn, uid, [round_id]).get(round_id, (None, None))
    return RoundOut(
        **{k: v for k, v in vars(r).items() if k not in ("user_id", "improver_run_id")},
        change_id=change, change_status=status)


@router.get("/harness/experience", response_model=list[LessonOut])
def get_experience(svc: Svc, uid: User, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    check_user(svc, uid)
    with svc.pool.connection() as conn:
        rows = harness_rsi.experience(conn, uid, query=None, limit=limit, now=svc.clock())
    return [LessonOut(**{k: v for k, v in vars(r).items() if k != "user_id"}) for r in rows]


@router.post("/harness/run", response_model=HRound)
def post_run(svc: Svc, uid: User):
    """One round now (`manual=True`, whatever the settings say) under the user's cycle lock:
    409 (CycleBusy) while a cycle or round runs. 503 without a deep tier or gateway."""
    check_user(svc, uid)
    track = _harness(svc)
    if track is None:
        raise HTTPException(503, "改进功能不可用")
    with _exclusive(svc, uid):
        return run_round(uid, track.deps, now=svc.clock(), manual=True)
