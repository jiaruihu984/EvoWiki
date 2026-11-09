"""The fast rollback channel of applied harness changes (RSI review 2026-10-28, first gap): clear,
reproducible failures that the new pack version shows and the previous one did not roll the change
back at once, without waiting for `observe`'s metric points (MIN_EVENTS per side, two harmful
points in a row). No LLM call and no judge: everything is read from the traces (`agent_run`,
`agent_step`) and the page jobs' outcome (`refine_job`). `round.HarnessRounds.observe` runs it at
the start of every cycle, before `observe.observe_changes`; the two are separate (own table, own
rule), and a change this channel rolled back is no longer applied, so `observe` skips it.

Units: the user's page jobs (deep runs grouped by the run id prefix before ":write:" / ":hook:",
the prefix `observe` groups jobs by) and assistant turns (the "assistant.turn" run plus its hook
runs, prefix "assistant:<session>:<message>"); harness replays are left out, as are other agents'
runs (forge, improver) and units with a run not finished. A unit under several pack versions
counts on no side. Its failures, each a signature (`unit_failures`):
- "job:<failure>": the refine job ended failed (`deep.contracts.RefineFailure`), except the codes
  that do not come from the pack (TRANSIENT_JOB: provider busy / timeout / unavailable, the user's
  spending cap, a page changed under it, a cancel);
- "error:<Type>": a run raised (`agent_run.error`, the exception's type: ProviderOutputError,
  LLMParseError, ToolLimitExceeded, ...), except TRANSIENT_ERRORS (provider outages and timeouts,
  budget caps, cancels);
- TOOL_CAP: a run stopped at the H4 tool-call cap, or a tool call was refused by it;
- "stop:no_tool_calls": a deep run that stopped without calling `finish`;
- "tool:<name>:crash" (the tool raised), "tool:<name>:unknown" (a tool the pack does not offer),
  "tool:<name>:<refusal>": a tool refused the call (render check, a citation of a missing block,
  an invalid section, ...), the refusal reduced to its kind (`refusal_kind`: its text up to the
  first colon, ids and quoted names dropped), so one error with different ids is one signature.

Windows: `observe._windows` (the same before / after bounds, the same pack-version split), with
one more bound on the after side: units under a pack version older than the next harness version
after the change (`_next_version`: the next change, a rollback or the user's own edit of any key),
so failures under a pack the user edited since are never blamed on this change. A spec-only change
(no pack version) bounds its after side by the next harness version made after it.

Rule: for each agent the change touches (`diff.agents_touched`, "deep" / "assistant"), with at
least MIN_BASELINE finished units of that agent on the before side, a signature that no before unit
shows and N_FAILURES or more after units show is a clear failure. N_FAILURES = 2: one failure under
a new pack can be a one-off (a provider's odd answer, a model's bad tool call that once happens to
be new), two units failing the same way where the old version never did are a reproducible
regression; a third would only keep the user on a broken pack longer, while a wrong rollback costs
little (the change can be proposed again, the lesson says why it went). MIN_BASELINE = 3: "the old
version did not have it" needs more old units than the failures counted, or a failure the old pack
would have shown too (it never ran that page kind) reads as new.

Action (under the user's harness lock, the change re-read and skipped unless still applied):
`change_source.undo_change(status="rolled_back")`, a `harmful` lesson (evidence: the failures,
their run ids, by "fast_check"; it names an earlier `effective` lesson of `observe` it supersedes),
a `harness_fast_check` row. A harmful lesson already written for the change (by this channel or by
`observe`) is not written again. A refused undo (a newer change touched the key, the user edited
it: `undo_change`'s ValueError) is recorded as a `refused` row with the reason and the failing-run
count; it is tried again only when more failing units are seen, never every cycle. User edits are
never undone: only `harness_change` rows (the RSI's own changes) are watched, and `undo_change`
refuses when a key no longer holds the value the change set."""

import logging
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg_pool import ConnectionPool

from evowiki.agent.trace import REPLAY_RUN_PREFIX, REPLAY_TRIGGER
from evowiki.harness import diff as diff_mod
from evowiki.harness.change_source import undo_change
from evowiki.harness.observe import _applied_at, _landed_patches, _situation, _Window, _windows
from evowiki.store import harness as pack_store
from evowiki.store import harness_rsi, pinned
from evowiki.store.db import fetch_all, fetch_one
from evowiki.teach.spec_registry import SpecRegistry

log = logging.getLogger("evowiki.harness.fast_check")

N_FAILURES = 2
MIN_BASELINE = 3
ASSISTANT_AGENT = "assistant.turn"     # assistant.loop.PURPOSE (a heavy import for one name)
TOOL_CAP = "tool_cap"
CAP_REFUSAL = "tool cap reached"       # agent.loop's trace error of a call over the cap
CRASH_PREFIX = "工具出错："             # agent.loop.run_tool's content when a tool raised
TRANSIENT_JOB = frozenset({"llm_unavailable", "llm_busy", "llm_timeout", "budget_exceeded",
                           "page_changed", "cancelled"})
TRANSIENT_ERRORS = frozenset({
    "ProviderUnavailableError", "ProviderTransientError", "ProviderNoticeError",
    "ProviderBusyError", "ProviderTimeoutError", "DeadlineExceeded", "BudgetExceededError",
    "PageBudgetExceeded", "Cancelled", "CancelledError", "DBOSWorkflowCancelledError",
    "TimeoutError", "ConnectionError", "KeyboardInterrupt", "SystemExit"})
ROLLED_BACK = "新版本出现了旧版本没有的错误，已自动撤回"
BLOCKED = "新版本出现了旧版本没有的错误，但没能撤回：{e}"

_UNIT = re.compile(r":(?:write|hook):")
_CUT = re.compile(r"[：:；;（(，,]")
_QUOTED = re.compile(r"「[^」]*」|“[^”]*”|\"[^\"]*\"|'[^']*'")
_ASCII = re.compile(r"[A-Za-z0-9_.\-/]+")


@dataclass(frozen=True)
class FastDeps:
    pool: ConnectionPool
    registry: SpecRegistry | None              # the teaching spec (undo of H7 ops)


@dataclass(frozen=True)
class Failure:
    """One clear failure: `signature` shown by `n_after` units of `agent` under the new pack and by
    none of the `before_units` under the previous one; `runs`: the failing run ids."""

    agent: str
    signature: str
    runs: tuple[str, ...]
    n_after: int
    before_units: int
    after_units: int

    def dump(self) -> dict[str, Any]:
        return {"agent": self.agent, "signature": self.signature, "runs": list(self.runs),
                "n_after": self.n_after, "before_units": self.before_units,
                "after_units": self.after_units}


@dataclass(frozen=True)
class FastResult:
    change_id: str                              # bare harness_change id
    rolled_back: bool
    failures: tuple[Failure, ...]
    lesson: str | None = None                   # "harmful" when one was written this time
    note: str = ""


@dataclass(frozen=True)
class _Unit:
    key: str
    agent: str                                  # "deep" | "assistant"
    pack_version: int | None                    # None: runs under several pack versions
    started_at: datetime
    failures: Mapping[str, tuple[str, ...]]     # signature -> the run ids that show it


def refusal_kind(error: str) -> str:
    """A tool refusal's kind: its text up to the first colon / semicolon / bracket, without quoted
    names; ids (ASCII tokens) dropped from a Chinese text, an English one kept lowercased."""
    head = _CUT.split(_QUOTED.sub("", error.strip()), maxsplit=1)[0].strip()
    if head.isascii():
        return " ".join(head.lower().split())[:60]
    return " ".join(_ASCII.sub(" ", head).split())[:60]


def _tool_failure(t: Mapping[str, Any]) -> str:
    name, error = t.get("name") or "?", t.get("error") or ""
    if error == CAP_REFUSAL:
        return TOOL_CAP
    if error.startswith("unknown tool"):
        return f"tool:{name}:unknown"
    if t.get("crash"):
        return f"tool:{name}:crash"
    return f"tool:{name}:{refusal_kind(error)}"


def unit_failures(run: Mapping[str, Any], agent: str) -> list[str]:
    """The failure signatures one traced run shows (module doc)."""
    out = []
    if run.get("job_failure") and run["job_failure"] not in TRANSIENT_JOB:
        out.append(f"job:{run['job_failure']}")
    if run.get("error") and run["error"] not in TRANSIENT_ERRORS:
        out.append(f"error:{run['error']}")
    if run.get("stop") == "tool_cap":
        out.append(TOOL_CAP)
    if agent == "deep" and run.get("stop") == "no_tool_calls" and run["agent"].startswith("deep."):
        out.append("stop:no_tool_calls")
    out += [_tool_failure(t) for t in run.get("tool_errors") or ()]
    return out


def _agent_of(key: str, runs: Sequence[Mapping[str, Any]]) -> str | None:
    if key.startswith("assistant:") or any(r["agent"] == ASSISTANT_AGENT for r in runs):
        return "assistant"
    if any(r["agent"].startswith("deep.") or _UNIT.search(r["run_id"]) for r in runs):
        return "deep"
    return None                                  # forge, improver, ...: not watched


def _units(conn, user_id: str, since: datetime | None, now: datetime) -> list[_Unit]:
    rows = fetch_all(
        conn,
        "select r.run_id, r.agent, r.pack_version, r.started_at, r.finished_at, r.stop, r.error, "
        "(select j.job->>'failure' from refine_job j where j.job_id = r.input->>'job_id' "
        " order by j.id desc limit 1) as job_failure, "
        "(select jsonb_agg(jsonb_build_object('name', s.payload->>'name', "
        "  'error', s.payload->'result'->>'error', "
        "  'crash', starts_with(coalesce(s.payload->'result'->>'content', ''), %s)) order by s.seq) "
        " from agent_step s where s.run_id = r.run_id and s.kind = 'tool' "
        " and s.payload->'result'->>'error' is not null) as tool_errors "
        "from agent_run r where r.user_id = %s and r.started_at <= %s "
        "and (%s::timestamptz is null or r.started_at >= %s) "
        "and not starts_with(r.run_id, %s) "
        "and coalesce(r.input->'job'->>'trigger', '') <> %s",
        (CRASH_PREFIX, user_id, now, since, since, REPLAY_RUN_PREFIX, REPLAY_TRIGGER))
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(_UNIT.split(r["run_id"], maxsplit=1)[0], []).append(r)
    out = []
    for key, runs in groups.items():
        agent = _agent_of(key, runs)
        if agent is None or any(r["finished_at"] is None for r in runs):
            continue
        fails: dict[str, list[str]] = {}
        for r in sorted(runs, key=lambda r: (r["started_at"], r["run_id"])):
            for sig in unit_failures(r, agent):
                fails.setdefault(sig, []).append(r["run_id"])
        versions = {r["pack_version"] for r in runs}
        out.append(_Unit(key, agent, next(iter(versions)) if len(versions) == 1 else None,
                         min(r["started_at"] for r in runs),
                         {s: tuple(dict.fromkeys(ids)) for s, ids in fails.items()}))
    return out


def _next_version(conn, user_id: str, row: harness_rsi.ChangeRow, w: _Window) -> int | None:
    """The first harness version after the change's (a spec-only change: made after it)."""
    if row.version is not None:
        r = fetch_one(conn, "select min(version) as v from harness_version "
                            "where user_id = %s and version > %s", (user_id, row.version))
    else:
        r = fetch_one(conn, "select min(version) as v from harness_version "
                            "where user_id = %s and created_at > %s", (user_id, w.start))
    return None if r is None else r["v"]


def _split(units: Sequence[_Unit], w: _Window, next_version: int | None,
           agents: Collection[str]) -> tuple[list[_Unit], list[_Unit]]:
    before, after = [], []
    for u in units:
        if u.agent not in agents or u.pack_version is None:
            continue
        pv, at = u.pack_version, u.started_at
        if (at < w.start and (w.lower is None or at >= w.lower)
                and (w.version is None or pv < w.version)):
            before.append(u)
        elif (at >= w.start and (w.end is None or at < w.end)
              and (w.version is None or pv >= w.version)
              and (next_version is None or pv < next_version)):
            after.append(u)
    return before, after


def clear_failures(before: Sequence[_Unit], after: Sequence[_Unit],
                   agents: Collection[str]) -> list[Failure]:
    """The rule of the module doc, per agent."""
    out = []
    for agent in sorted(agents):
        b = [u for u in before if u.agent == agent]
        a = [u for u in after if u.agent == agent]
        if len(b) < MIN_BASELINE:
            continue
        old = {s for u in b for s in u.failures}
        hits: dict[str, list[_Unit]] = {}
        for u in a:
            for s in u.failures:
                if s not in old:
                    hits.setdefault(s, []).append(u)
        for s, us in sorted(hits.items()):
            if len(us) >= N_FAILURES:
                out.append(Failure(agent, s, tuple(r for u in us for r in u.failures[s]),
                                   len(us), len(b), len(a)))
    return out


def fast_check(user_id: str, deps: FastDeps, *, now: datetime) -> list[FastResult]:
    """One result per applied change this channel acted on this time (rolled back, or its undo
    refused), oldest change first. Each change is written in its own transaction; an error other
    than a refused undo propagates (the cycle records it)."""
    with deps.pool.connection() as conn:
        rows = harness_rsi.applied_changes(conn, user_id)
        if not rows:
            return []
        patches = _landed_patches(conn, user_id)
        wins = _windows(conn, user_id, rows, [(p.candidate_id, at) for p, at in patches])
        lowers = [wins[("change", r.change_id)].lower for r in rows]
        since = None if any(x is None for x in lowers) else min(x for x in lowers if x)
        units = _units(conn, user_id, since, now)
        nexts = {r.change_id: _next_version(conn, user_id, r, wins[("change", r.change_id)])
                 for r in rows}
    out = []
    for r in sorted(rows, key=lambda r: (_applied_at(r), r.change_id)):
        agents = diff_mod.agents_touched(diff_mod.diff_from_json(r.diff)[0])
        before, after = _split(units, wins[("change", r.change_id)], nexts[r.change_id], agents)
        found = clear_failures(before, after, agents)
        if found:
            res = _act(user_id, r.change_id, tuple(found), deps, now)
            if res is not None:
                out.append(res)
    return out


def _act(user_id: str, cid: str, found: tuple[Failure, ...], deps: FastDeps,
         now: datetime) -> FastResult | None:
    n = sum(f.n_after for f in found)
    with pinned.transaction(deps.pool), pinned.connection(deps.pool) as conn:
        pack_store.lock_key(conn, user_id)               # one harness writer per user
        row = harness_rsi.get_change(conn, user_id, cid)
        if row is None or row.status != "applied":       # undone / rolled back since it was listed
            return None
        done = harness_rsi.fast_checks(conn, user_id, cid)
        if done and done[-1].outcome == "refused" and n <= done[-1].evidence.get("n_failing", 0):
            return None                                  # refused before, nothing new since
        points = harness_rsi.observations(conn, user_id, cid)
        effective = next((o.lesson_id for o in points
                          if o.lesson_id is not None and o.verdict == "helpful"), None)
        harmful_done = (any(d.lesson_id is not None for d in done)
                        or any(o.lesson_id is not None and o.verdict == "harmful" for o in points))
        situation, scope, _ = _situation(conn, user_id, row.round_id, ())
        evidence = {"change_id": cid, "round_id": row.round_id, "by": "fast_check",
                    "failures": [f.dump() for f in found],
                    "runs": sorted({r for f in found for r in f.runs}), "n_failing": n}

        def lesson() -> str | None:
            if harmful_done:
                return None
            return harness_rsi.add_experience(conn, user_id, situation, row.diff,
                                              {**evidence, "supersedes": effective}, "harmful",
                                              scope, weight=1.0, now=now)
        try:
            with pinned.transaction(deps.pool):         # a savepoint: a refused undo keeps nothing
                hid = lesson()
                harness_rsi.add_fast_check(conn, user_id, cid, "rolled_back", evidence,
                                           lesson_id=hid, now=now)
                # last: its spec commit is its own last step (`undo_change`)
                undo_change(conn, user_id, cid, registry=deps.registry, status="rolled_back",
                            now=now)
        except ValueError as e:
            log.warning("harness change %s/%s shows new clear failures but cannot be undone: %s",
                        user_id, cid, e)
            hid = lesson()
            harness_rsi.add_fast_check(conn, user_id, cid, "refused", evidence, reason=str(e),
                                       lesson_id=hid, now=now)
            return FastResult(cid, False, found, "harmful" if hid else None, BLOCKED.format(e=e))
    log.info("harness change %s/%s rolled back by the fast check: %s", user_id, cid,
             ", ".join(f.signature for f in found))
    return FastResult(cid, True, found, "harmful" if hid else None, ROLLED_BACK)
