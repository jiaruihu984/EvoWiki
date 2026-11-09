"""Watching applied harness changes (design 2026-10-21 §5.7, §6.2): the fixed anchor metrics before
vs after each change in force, an automatic rollback after two harmful looks in a row, and the
lessons the outcome earns. No LLM call; `teach.cycle.RsiCycle.run` runs it at the start of every
cycle with the harness loop on (through `round.HarnessRounds.observe`).

Metrics, each oriented so + is better (`teach.anchors.rate_sampled`, verdict `teach.anchors.judge`:
pending below MIN_EVENTS per side, Bonferroni over the metrics that can decide):
- unclear_rate: the 「看不懂」 share of the reader's page signals (check_unclear, or page feedback with
  the unclear chip, among check answers, check_unclear and the reactions of
  `layer2.outcomes.REACTION_KINDS`), lower is better;
- check_correct: check-answer outcomes, higher is better;
- page_cost / page_latency: per page job (the deep write runs of one refine, which share the run id
  prefix before ":write:", harness replays left out), the summed `llm_call.cost` of its model
  steps / the wall time from its first round's start to its last round's end, lower is better.
  These are the two anchor metrics §5.7 adds; `teach/anchors.py` belongs to the user, so they are
  defined here and go through its `rate_sampled` / `judge` unchanged. A job left out of them (a
  call without a price, a round not finished, rounds under two pack versions) is counted per side
  in `dropped` (on the result, the stored point and the lesson's evidence), never dropped silently.

Before / after: a change is in force from `applied_at` (its settle time; a change recorded applied
settles when recorded) until the next watched event after it: the next change still applied
(`applied_at` and version of that one) or the next watched patch landing (below), whichever comes
first (`_windows`, one timeline of both). The before side is bounded the way the retired RSI-B's was (the from-version
population only): it starts at the later of the previous harness change (applied, undone or rolled
back) and the moment the pack version before this change became current (the last
`harness_version` row before `applied_at`), so older drift never decides. Signals carry no run id,
so they split by time alone; the round's trigger signals stay on the before side (the retired
RSI-B outcome's rule). A signal on a page still written by the old pack (not yet rewritten on open, design §5.7's
lazy rewrite) counts on the after side; with the rewrite on open this is a short lag, accepted.
Page jobs split by pack version and their first round's start: after = started in the window with
pack_version ≥ the change's version, before = started in the before window with an older version
(a job fitting neither, e.g. an old-pack job still going after the apply, counts on no side). A
spec-only change (version None) splits jobs by time alone.

Points (`store.harness_rsi.observations`, table harness_observation, so they survive restarts)
follow `teach.layer2.store`: a point is computed only when the after side grew by MIN_EVENTS since
the last one (`due_for_point`), and the rollback rule is `confirmed_harmful` (the last two points
harmful, the newer one with more evidence). A confirmed harmful change is undone
(`change_source.undo_change`, status rolled_back, keys restored with source "rollback") with a
`harmful` lesson whose evidence is the deltas (and the id of an earlier `effective` lesson it
supersedes). When the undo is refused (a newer change touched the same key, the user edited it),
the harmful lesson is still written once and a point with note ROLLBACK_REFUSED and the reason is
stored; the undo is tried again only when new evidence confirms the harm again, never every cycle.
A helpful verdict writes one `effective` lesson, the first time only. No verdict (pending /
no_effect) writes no lesson. Each change is re-read under the user's harness lock and skipped
unless it is still applied.

Assistant changes (harness 3b follow-ups, #12; 助手回答评价): a change's metrics follow the agents
it touches (`diff.agents_touched`, `_metrics`). The page metrics above, which an assistant-only
change cannot move, judge every change that touches the deep agent (and every code patch); a
change that touches the assistant is judged by
- assistant_down_rate: the 👎 share of the learner's rated answers (`ObserveDeps.rated`, from
  `wikiapp.signals.rated_answers`: one value per answer with a current rating, 1 for 👎, 0 for 👍,
  a cleared one left out), lower is better. An answer is placed by when it was GIVEN (the
  assistant entry's time), not when it was rated, within the same window bounds as the signals;
  ratings are read now, so a later change of mind counts. The 👎 answers that triggered the
  round were given before the change, so they fall on the before side (as the page metrics keep
  their trigger signals there): this leans slightly toward "helpful".
An assistant-only change has this metric alone, a mixed change both kinds. The rest is as for any
change: no point until the after side grew by MIN_EVENTS rated answers, `teach.anchors.judge`
pending until both sides have MIN_EVENTS, two harmful points in a row roll it back. Without
`rated` wired (no session store) the metric has no data, so an assistant-only change stays
pending. Its apply time bounds the windows of the changes around it as before.

Code patches (harness 3b, Task 10): a forge patch a harness round evaluated (its `gate_ref` is a
`harness_gate` row of this user with subject "patch:<cid>") is watched the same way once it is
`landed`: the after side starts at the forge supervisor's newest `land` event for it (no event
yet: not watched this time) and ends at the next watched event (a change applied or another watched
patch landed; a following change's version also bounds its jobs), the before side ends at the
landing and starts at the later of the last harness change, pack version or watched patch landing
before it; a patch changes no pack version, so its page jobs split by time alone. Known limit: a
landed patch runs for every user, but it is watched only with the signals and jobs of the user
whose round's gate passed it, and patches landed through other users' gates or forge rounds do not
bound this user's windows. Points go to `harness_patch_observation` (append-only); a
confirmed harmful patch is reverted through the forge (`forge.patch.land.revert_patch`,
by "observe": landed → revert_queued, the supervisor reverts it at its next idle point) with a
`harmful` lesson; a refused revert (no longer landed) stores a ROLLBACK_REFUSED point, as for a
change. Results carry `kind="patch"` and the candidate id."""

import logging
import re
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from psycopg_pool import ConnectionPool

from evowiki.agent.trace import REPLAY_RUN_PREFIX, REPLAY_TRIGGER
from evowiki.forge.patch import land as forge_land
from evowiki.harness import diff as diff_mod
from evowiki.harness.change_source import ROLLBACK_REFUSED, undo_change
from evowiki.store import forge_patch as patch_store
from evowiki.store import harness as pack_store
from evowiki.store import harness_rsi, pinned
from evowiki.store.db import fetch_all, fetch_one
from evowiki.teach import anchors
from evowiki.teach.contracts import AnchorDelta, AnchorMetric, OutcomeOut, Signal
from evowiki.teach.layer2.outcomes import NOTE_AUTO_ROLLBACK, REACTION_KINDS
from evowiki.teach.layer2.store import confirmed_harmful, due_for_point, is_new_point
from evowiki.teach.spec_registry import SpecRegistry

log = logging.getLogger("evowiki.harness.observe")

WRITE_AGENT = "deep.write"
JOB_SEP = ":write:"               # deep.agent run ids: f"{job}:write:{round}:w{writer}"
ObsVerdict = Literal["helpful", "harmful", "not_judged"]
ROLLED_BACK = "连续两次观察都变差了，已自动撤回"
PATCH_GONE = "这次代码改动已经不在线上了"
BLOCKED = "连续两次观察都变差了，但没能撤回：{e}"

# metric -> + is better when higher (the order fixes each metric's bootstrap seed: append only)
_HIGHER_IS_BETTER: Mapping[AnchorMetric, bool] = {
    "unclear_rate": False, "check_correct": True, "page_cost": False, "page_latency": False,
    "assistant_down_rate": False}
PAGE_METRICS: frozenset[AnchorMetric] = frozenset(
    {"unclear_rate", "check_correct", "page_cost", "page_latency"})
ASSISTANT_METRICS: frozenset[AnchorMetric] = frozenset({"assistant_down_rate"})


@dataclass(frozen=True)
class ObserveDeps:
    pool: ConnectionPool
    registry: SpecRegistry | None              # the teaching spec (undo of H7 ops)
    signals: Callable[[str, datetime | None], Sequence[Signal]]   # (user, since) -> signals
    # user -> (answer time, 👎?) of every rated assistant answer (`signals.rated_answers`);
    # None: no session store, the assistant metric has no data
    rated: Callable[[str], Sequence[tuple[datetime, bool]]] | None = None


@dataclass(frozen=True)
class ObserveResult:
    change_id: str                            # bare harness_change id (a patch: its candidate id)
    verdict: ObsVerdict
    deltas: Mapping[str, float] = field(default_factory=dict)   # metric -> oriented delta
    point: bool = False                       # a new point was stored this time
    rolled_back: bool = False
    lesson: str | None = None                 # the verdict of a lesson written this time
    note: str = ""
    # page jobs the cost / latency metrics left out: {"before"|"after": {reason: n}}
    dropped: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    kind: Literal["change", "patch"] = "change"


@dataclass(frozen=True)
class _Window:
    lower: datetime | None                    # where the before side starts (None: unbounded)
    start: datetime
    end: datetime | None                      # the next change's applied_at
    version: int | None
    end_version: int | None


@dataclass(frozen=True)
class _Job:
    pack_version: int | None                  # None: rounds under several pack versions
    started_at: datetime
    cost: float | None                        # None: a call without a price
    latency: float | None                     # None: a round not finished


def _applied_at(row: harness_rsi.ChangeRow) -> datetime:
    return row.settled_at or row.created_at


def _lower_at(conn, user_id: str, start: datetime, *, change_id: str = "",
              landings: Sequence[datetime] = ()) -> datetime | None:
    """`_lower` for a change `change_id` or (change_id "") a patch; `landings`: the landing times
    of the watched patches, of which the last one before `start` bounds the before side too."""
    r = fetch_one(
        conn, "select max(case when status = 'applied' then coalesce(settled_at, created_at) "
              "else created_at end) as at from harness_change where user_id = %s and "
              "change_id <> %s and status in ('applied', 'undone', 'rolled_back') and "
              "(case when status = 'applied' then coalesce(settled_at, created_at) "
              "else created_at end) < %s", (user_id, change_id, start))
    v = fetch_one(conn, "select max(created_at) as at from harness_version "
                        "where user_id = %s and created_at < %s", (user_id, start))
    found = [x["at"] for x in (r, v) if x is not None and x["at"] is not None]
    found += [t for t in landings if t < start]
    return max(found, default=None)


def _windows(conn, user_id: str, rows: Sequence[harness_rsi.ChangeRow],
             landings: Sequence[tuple[str, datetime]] = ()) -> dict[tuple[str, str], _Window]:
    """One timeline of the watched events, the changes in force (by apply time) and the watched
    patches (by landing time, `landings` = (candidate id, landed at)): each window's after side
    ends at the next event of either kind (a following change's version also bounds its jobs), and
    its before side starts no earlier than the previous one (`_lower_at`, which also counts older
    changes and pack versions). Keys: ("change", change id) / ("patch", candidate id)."""
    events: list[tuple[datetime, str, str, int | None]] = (
        [(_applied_at(r), "change", r.change_id, r.version) for r in rows]
        + [(at, "patch", cid, None) for cid, at in landings])
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    times = [at for _, at in landings]
    out = {}
    for i, (start, kind, eid, version) in enumerate(events):
        nxt = events[i + 1] if i + 1 < len(events) else None
        lower = _lower_at(conn, user_id, start, change_id=eid if kind == "change" else "",
                          landings=times)
        out[(kind, eid)] = _Window(lower, start, None if nxt is None else nxt[0], version,
                                   None if nxt is None or nxt[1] != "change" else nxt[3])
    return out


def _signal_values(sigs: Sequence[Signal]) -> dict[AnchorMetric, list[float]]:
    return {
        "unclear_rate": [1.0 if s.kind == "check_unclear"
                         or (s.kind == "page_feedback" and "unclear" in s.chips) else 0.0
                         for s in sigs
                         if s.kind in ("check_answer", "check_unclear") or s.kind in REACTION_KINDS],
        "check_correct": [float(s.outcome) for s in sigs
                          if s.kind == "check_answer" and s.outcome is not None]}


def _split_signals(signals: Sequence[Signal], w: _Window, trigger: set[str], now: datetime
                   ) -> tuple[list[Signal], list[Signal]]:
    page = [s for s in signals if s.page_id is not None and s.at <= now]
    before = [s for s in page if s.at < w.start and (w.lower is None or s.at >= w.lower)]
    after = [s for s in page if s.at >= w.start and (w.end is None or s.at < w.end)
             and s.signal_id not in trigger]
    return before, after


def _jobs(conn, user_id: str, now: datetime) -> list[_Job]:
    """The user's page jobs: deep write runs (no harness replays) grouped by job id, with the
    summed model cost and the wall time of their rounds."""
    rows = fetch_all(
        conn,
        "select r.run_id, r.pack_version, r.started_at, r.finished_at, sum(c.cost) as cost, "
        "count(c.id) as calls, count(c.cost) as priced "
        "from agent_run r left join agent_step s on s.run_id = r.run_id and s.kind = 'model' "
        "left join llm_call c on c.id = s.call_id "
        "where r.user_id = %s and r.agent = %s and r.started_at <= %s "
        "and not starts_with(r.run_id, %s) "
        "and coalesce(r.input->'job'->>'trigger', '') <> %s "
        "group by r.run_id, r.pack_version, r.started_at, r.finished_at",
        (user_id, WRITE_AGENT, now, REPLAY_RUN_PREFIX, REPLAY_TRIGGER))
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["run_id"].split(JOB_SEP)[0], []).append(r)
    out = []
    for runs in groups.values():
        versions = {r["pack_version"] for r in runs}
        started = min(r["started_at"] for r in runs)
        unfinished = any(r["finished_at"] is None for r in runs)
        unpriced = any(r["priced"] != r["calls"] for r in runs)
        out.append(_Job(
            pack_version=next(iter(versions)) if len(versions) == 1 else None, started_at=started,
            # an unfinished job's cost is partial (lower): left out like its latency
            cost=None if unpriced or unfinished else float(sum(r["cost"] or 0.0 for r in runs)),
            latency=None if unfinished else
            (max(r["finished_at"] for r in runs) - started).total_seconds()))
    return out


def _split_jobs(jobs: Sequence[_Job], w: _Window) -> tuple[list[_Job], list[_Job], dict]:
    """(before, after, dropped per side); a job under several pack versions is placed by time
    only to count it, and left out of both metrics."""
    before, after = [], []
    dropped: dict[str, dict[str, int]] = {"before": {}, "after": {}}
    for j in jobs:
        pv, at = j.pack_version, j.started_at
        if pv is None:
            side = ("before" if at < w.start and (w.lower is None or at >= w.lower)
                    else "after" if at >= w.start and (w.end is None or at < w.end) else None)
            if side is not None:
                dropped[side]["mixed_pack"] = dropped[side].get("mixed_pack", 0) + 1
            continue
        if (at < w.start and (w.lower is None or at >= w.lower)
                and (w.version is None or pv < w.version)):
            before.append(j)
        elif (at >= w.start and (w.version is None or pv >= w.version)
              and (w.end is None or at < w.end)
              and (w.end_version is None or pv < w.end_version)):
            after.append(j)
    for side, js in (("before", before), ("after", after)):
        # an unfinished job is left out of both metrics: counted once, as unfinished
        for reason, n in (("unpriced", sum(j.cost is None and j.latency is not None for j in js)),
                          ("unfinished", sum(j.latency is None for j in js))):
            if n:
                dropped[side][reason] = n
    return before, after, {k: v for k, v in dropped.items() if v}


def _job_values(jobs: Sequence[_Job]) -> dict[AnchorMetric, list[float]]:
    return {"page_cost": [j.cost for j in jobs if j.cost is not None],
            "page_latency": [j.latency for j in jobs if j.latency is not None]}


@dataclass(frozen=True)
class _Sides:
    before: dict[AnchorMetric, list[float]]
    after: dict[AnchorMetric, list[float]]
    dropped: dict

    def n_after(self) -> int:
        return max((len(v) for v in self.after.values()), default=0)


def _rated_values(rated: Sequence[tuple[datetime, bool]], w: _Window, now: datetime
                  ) -> tuple[list[float], list[float]]:
    """assistant_down_rate per side: 1.0 per 👎 answer, 0.0 per 👍, by the answer's time."""
    given = [(at, down) for at, down in rated if at <= now]
    before = [1.0 if d else 0.0 for at, d in given
              if at < w.start and (w.lower is None or at >= w.lower)]
    after = [1.0 if d else 0.0 for at, d in given
             if at >= w.start and (w.end is None or at < w.end)]
    return before, after


def _sides(w: _Window, signals: Sequence[Signal], jobs: Sequence[_Job], trigger: set[str],
           now: datetime, rated: Sequence[tuple[datetime, bool]] = (),
           metrics: frozenset[AnchorMetric] = PAGE_METRICS) -> _Sides:
    """The values of `metrics` (the subject's, `_metrics`) on each side; the dropped page jobs
    only when the page metrics are among them."""
    sb, sa = _split_signals(signals, w, trigger, now)
    jb, ja, dropped = _split_jobs(jobs, w)
    rb, ra = _rated_values(rated, w, now)
    before = {**_signal_values(sb), **_job_values(jb), "assistant_down_rate": rb}
    after = {**_signal_values(sa), **_job_values(ja), "assistant_down_rate": ra}
    return _Sides({m: v for m, v in before.items() if m in metrics},
                  {m: v for m, v in after.items() if m in metrics},
                  dropped if metrics & PAGE_METRICS else {})


def _outcome(change_id: str, sides: _Sides, now: datetime) -> OutcomeOut:
    seed = zlib.crc32(change_id.encode("utf-8"))
    metrics = tuple(anchors.rate_sampled(m, sides.before[m], sides.after[m], higher_is_better=hb,
                                         seed=seed + i)
                    for i, (m, hb) in enumerate(_HIGHER_IS_BETTER.items())
                    if m in sides.before)
    deltas, v = anchors.judge(metrics)
    return OutcomeOut(change_id=change_id, at=now, verdict=v, deltas=deltas)


def _point(o: harness_rsi.ObsRow) -> OutcomeOut:
    return OutcomeOut(change_id=o.change_id, at=o.at, verdict=o.verdict,
                      deltas=tuple(AnchorDelta.model_validate(d) for d in o.deltas), note=o.note)


def _verdict(v: str) -> ObsVerdict:
    return v if v in ("helpful", "harmful") else "not_judged"     # type: ignore[return-value]


def _deltas(o: OutcomeOut | None) -> dict[str, float]:
    return {} if o is None else {d.metric: d.delta for d in o.deltas if d.delta is not None}


def _dump(o: OutcomeOut) -> list[dict[str, Any]]:
    return [d.model_dump(mode="json") for d in o.deltas]


def _situation(conn, user_id: str, round_id: str, signals: Sequence[Signal]
               ) -> tuple[dict, Literal["user", "general"], set[str]]:
    """The lesson's situation and scope from the change's round (as the round's own lessons:
    the kinds of the problem cases' signals; scope general when the improver chose the general
    candidate), and the round's trigger signal ids."""
    rnd = harness_rsi.get_round(conn, user_id, round_id)
    cases = (rnd.cases if rnd is not None else None) or {}
    ids = {i for c in cases.get("problem", []) for i in c.get("signal_ids", [])}
    decision = (rnd.decision if rnd is not None else None) or {}
    situation = {"agent": cases.get("agent", "deep"),      # round.case_agent; older rounds: deep
                 "signal_kinds": sorted({s.kind for s in signals if s.signal_id in ids}),
                 "finding": rnd.finding if rnd is not None else None}
    return situation, "general" if decision.get("improver") == "a" else "user", ids


@dataclass(frozen=True)
class _Subject:
    """What is watched: a harness change (`kind` "change", `id` its bare id) or a landed code
    patch (`kind` "patch", `id` its candidate id). `change`: what its lessons record as changed."""

    kind: Literal["change", "patch"]
    id: str
    round_id: str
    change: Any
    metrics: frozenset[AnchorMetric] = PAGE_METRICS       # what judges it (`_metrics`)

    def still_on(self, conn, user_id: str) -> bool:
        if self.kind == "change":
            row = harness_rsi.get_change(conn, user_id, self.id)
            return row is not None and row.status == "applied"
        p = patch_store.get_patch(conn, self.id)
        return p is not None and p.state == "landed"

    def stored(self, conn, user_id: str) -> list[harness_rsi.ObsRow]:
        if self.kind == "change":
            return harness_rsi.observations(conn, user_id, self.id)
        return harness_rsi.patch_observations(conn, user_id, self.id)

    def add(self, conn, user_id: str, verdict: str, deltas: Any, **kw: Any) -> str:
        if kw.get("reason") is None:
            label = {"pending": "数据不足", "no_effect": "未观察到效果",
                     "helpful": "有效", "harmful": "有害"}[verdict]
            signals = "; ".join(
                f"{d['metric']}: {d.get('before')} → {d.get('after')}，"
                f"变化={d.get('delta')}（正数更好），"
                f"样本={d.get('n_before', 0)}/{d.get('n_after', 0)}"
                for d in deltas)
            kw["reason"] = f"{label}；{signals}" if signals else label
        add = (harness_rsi.add_observation if self.kind == "change"
               else harness_rsi.add_patch_observation)
        return add(conn, user_id, self.id, verdict, deltas, **kw)

    def undo(self, conn, user_id: str, deps: "ObserveDeps", now: datetime) -> None:
        """ValueError (or LookupError for a patch row gone) when refused."""
        if self.kind == "change":
            undo_change(conn, user_id, self.id, registry=deps.registry, status="rolled_back",
                        now=now)
        else:
            forge_land.revert_patch(conn, user_id, self.id, by="observe", now=now)


@dataclass
class _Ctx:
    conn: Any
    user_id: str
    subject: _Subject
    situation: dict
    scope: Literal["user", "general"]
    now: datetime

    def lesson(self, verdict: Literal["effective", "harmful"], o: OutcomeOut, dropped: Any,
               point_ids: Sequence[str], supersedes: str | None = None) -> str:
        key = "change_id" if self.subject.kind == "change" else "candidate_id"
        evidence = {key: self.subject.id, "round_id": self.subject.round_id,
                    "verdict": o.verdict, "points": list(point_ids), "deltas": _dump(o),
                    "dropped": dropped}
        if verdict == "harmful":
            evidence["supersedes"] = supersedes
        return harness_rsi.add_experience(self.conn, self.user_id, self.situation,
                                          self.subject.change, evidence, verdict, self.scope,
                                          weight=1.0, now=self.now)


def _observe_one(user_id: str, subj: _Subject, w: _Window, signals: Sequence[Signal],
                 jobs: Sequence[_Job], deps: ObserveDeps, now: datetime,
                 rated: Sequence[tuple[datetime, bool]] = ()) -> ObserveResult | None:
    cid, kind = subj.id, subj.kind
    with pinned.transaction(deps.pool), pinned.connection(deps.pool) as conn:
        pack_store.lock_key(conn, user_id)              # one harness writer per user
        if not subj.still_on(conn, user_id):            # undone / reverted since it was listed
            return None
        stored = subj.stored(conn, user_id)
        pts = [_point(o) for o in stored]
        situation, scope, trigger = _situation(conn, user_id, subj.round_id, signals)
        ctx = _Ctx(conn, user_id, subj, situation, scope, now)
        effective = next((o.lesson_id for o in stored
                          if o.lesson_id is not None and o.verdict == "helpful"), None)
        harmful_done = any(o.lesson_id is not None and o.verdict == "harmful" for o in stored)
        refused = bool(stored) and stored[-1].note == ROLLBACK_REFUSED
        sides = _sides(w, signals, jobs, trigger, now, rated, subj.metrics)
        new: OutcomeOut | None = None
        if due_for_point(pts, sides.n_after()):
            o = _outcome(cid, sides, now)
            if is_new_point(pts[-1] if pts else None, o):
                new = o
        last = new or (pts[-1] if pts else None)
        dropped = sides.dropped if new is not None else (stored[-1].dropped if stored else {})
        if new is None and (refused or not confirmed_harmful(pts)):
            return ObserveResult(cid, _verdict(last.verdict) if last else "not_judged",
                                 _deltas(last), dropped=dropped,
                                 note=(stored[-1].reason or "") if refused else "", kind=kind)
        ids = [o.obs_id for o in stored]
        lesson_id = lesson = None
        if new is not None and new.verdict == "helpful" and effective is None:
            lesson, lesson_id = "effective", ctx.lesson("effective", new, dropped, ids)
        if new is not None:
            ids.append(subj.add(conn, user_id, new.verdict, _dump(new), dropped=dropped,
                                lesson_id=lesson_id, now=now))
            pts.append(new)
        assert last is not None
        res = ObserveResult(cid, _verdict(last.verdict), _deltas(last), point=new is not None,
                            lesson=lesson, dropped=dropped, kind=kind)
        if not confirmed_harmful(pts):
            return res
        harm = pts[-1]
        try:
            with pinned.transaction(deps.pool):        # a savepoint: a refused undo keeps the point
                hid = None if harmful_done else ctx.lesson("harmful", harm, dropped, ids,
                                                           effective)
                subj.add(conn, user_id, harm.verdict, _dump(harm), note=NOTE_AUTO_ROLLBACK,
                         dropped=dropped, lesson_id=hid, now=now)
                # last: its spec commit is its own last step (`undo_change`), so nothing after
                # it can fail and leave the spec reverted while the change stays applied
                subj.undo(conn, user_id, deps, now)
        except (ValueError, LookupError) as e:
            log.warning("%s %s/%s is confirmed harmful but cannot be undone: %s",
                        "harness change" if kind == "change" else "code patch", user_id, cid, e)
            # the forge's refusal may be English (ForgeNotFound): the detail stays in the log
            if kind == "patch" and (isinstance(e, LookupError) or re.search("[A-Za-z]", str(e))):
                e = ValueError(PATCH_GONE)
            hid = None if harmful_done else ctx.lesson("harmful", harm, dropped, ids, effective)
            subj.add(conn, user_id, harm.verdict, _dump(harm), note=ROLLBACK_REFUSED,
                     reason=str(e), dropped=dropped, lesson_id=hid, now=now)
            return ObserveResult(cid, "harmful", res.deltas, point=res.point,
                                 lesson="harmful" if hid else res.lesson,
                                 note=BLOCKED.format(e=e), dropped=dropped, kind=kind)
        return ObserveResult(cid, "harmful", res.deltas, point=res.point, rolled_back=True,
                             lesson="harmful" if hid else res.lesson, note=ROLLED_BACK,
                             dropped=dropped, kind=kind)


def observe_changes(user_id: str, deps: ObserveDeps, *, now: datetime) -> list[ObserveResult]:
    """One result per harness change in force (oldest first): its verdict now (the newest point;
    not_judged without one), a new point when the after side grew by MIN_EVENTS, the rollback of
    a confirmed harmful change and the lessons earned. Each change is written in its own
    transaction; an error other than a refused undo propagates (the cycle records it)."""
    with deps.pool.connection() as conn:
        rows = harness_rsi.applied_changes(conn, user_id)
        patches = _landed_patches(conn, user_id)
        if not rows and not patches:
            return []
        jobs = _jobs(conn, user_id, now)
        wins = _windows(conn, user_id, rows, [(p.candidate_id, at) for p, at in patches])
    signals = deps.signals(user_id, None)
    rated = deps.rated(user_id) if deps.rated is not None else ()
    rows = sorted(rows, key=lambda r: (_applied_at(r), r.change_id))
    out = [_observe_one(user_id, _Subject("change", r.change_id, r.round_id, r.diff, _metrics(r)),
                        wins[("change", r.change_id)], signals, jobs, deps, now, rated)
           for r in rows]
    out += [_observe_one(user_id, _Subject("patch", p.candidate_id, p.round_id, _patch_change(p)),
                         wins[("patch", p.candidate_id)], signals, jobs, deps, now)
            for p, _ in patches]
    return [r for r in out if r is not None]


def _metrics(row: harness_rsi.ChangeRow) -> frozenset[AnchorMetric]:
    """The metrics that judge a change (module doc): the page metrics unless it touches only the
    assistant (`diff.agents_touched`), plus the assistant metric when it touches the assistant."""
    agents = diff_mod.agents_touched(diff_mod.diff_from_json(row.diff)[0])
    out: frozenset[AnchorMetric] = frozenset() if agents == {"assistant"} else PAGE_METRICS
    return out | ASSISTANT_METRICS if "assistant" in agents else out


@dataclass(frozen=True)
class _LandedPatch:
    candidate_id: str
    title: str
    commit: str
    round_id: str                  # the harness round whose gate passed it


def _patch_change(p: _LandedPatch) -> dict:
    return {"patch": {"candidate_id": p.candidate_id, "title": p.title, "commit": p.commit}}


def _landed_patches(conn, user_id: str) -> list[tuple[_LandedPatch, datetime]]:
    """The landed forge patches a harness round of this user evaluated (gate_ref = a harness_gate
    row of this user with subject "patch:<cid>"), with their landing time, oldest landing first.
    A patch with no `land` event yet is left out (not watched this time)."""
    # known limit: only the gating user's signals and jobs watch a patch every user runs
    out = []
    for p in patch_store.patches(conn, states=["landed"], limit=100000):
        if not p.gate_ref:
            continue
        g = harness_rsi.get_gate(conn, user_id, p.gate_ref)
        if g is None or g.subject != f"patch:{p.candidate_id}":
            continue
        at = harness_rsi.patch_landed_at(conn, p.candidate_id)
        if at is None:
            continue
        out.append((_LandedPatch(p.candidate_id, p.title, p.commit, g.round_id), at))
    return sorted(out, key=lambda x: (x[1], x[0].candidate_id))
