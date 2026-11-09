"""RSI cycle controller (architecture spec §7.1–7.7): one run = at most one track-A patch (θ_infer,u,
channel A), A2, the observation of the harness changes in force and, with `RsiSettings.harness`,
one harness round, then a CycleRecord. Also the user's undo and the change list. (Track B, RSI-B,
and its layer 2, B2, retired on 2026-10-29: the harness loop is the one improvement round.)

Track A per run (A1 = statistical fitting; no LLM proposes numbers):
1. An open `candidate` is the track's one patch: it is re-gated when it has had fewer than
   MAX_GATE_RUNS decisive gate runs and its held-out outcomes grew by ≥ REGATE_MIN_NEW; else it waits.
2. Otherwise: outcomes under the current θ, a time split (retrospective if there is room, else
   prospective with cutoff = now), `fit.fitter.fit_infer` of the user's FitModel paths on the
   training side only (ts < cutoff — the fitter never sees the validation window, §7.7). Paths that
   moved by ≥ MIN_MOVE of their range form one FitPatchOp; none moved → "fit_no_change" (no patch, no
   memory row). The patch is gated by `gate_a` on ts ≥ cutoff.
3. Decisions write the PatchRecord and its MemoryRecord (a fit patch: path "fit", direction 0,
   from/to None, complexity = moved paths); every negative close (gate_failed, gate-run cap, user
   undo) bumps harmful, an acceptance bumps helpful.
4. Long-term outcome of every accepted track-A patch (`layer2.outcomes.outcome_a`: its old vs new θ
   on the outcomes after its acceptance, paired log loss + AUC with CIs), recomputed only when its
   outcomes after acceptance grew by `anchors.MIN_EVENTS` since its last point
   (`layer2.store.due_for_point`: bounded work per run). A point is appended to the change's series
   (`layer2.store.record_point`); the verdict goes to Patch Memory (`long_term`; a new helpful bumps
   helpful). A harmful verdict confirmed on two consecutive points (`layer2.store.confirmed_harmful`)
   rolls the patch back automatically (the undo path, which bumps harmful; reason
   A_ROLLBACK_REASON) — unless a newer accepted patch changed one of its paths: then it is skipped
   and logged (point note NOTE_ROLLBACK_BLOCKED), as the user's undo is refused then. Every rollback
   writes a final point saying who rolled the change back (NOTE_USER_UNDO / NOTE_AUTO_ROLLBACK). The
   outcomes are computed before the write transaction.
5. (retired: track B's observation.)
6. (retired: B2.)
7. A2 (layer 2 of track A, `fit.select`), after track A's transaction: every applied A2 change's
   outcome is measured when its children's evidence grew by MIN_EVENTS (`_monitor_a2`: `outcome_method` over the A1 fit patches made
   under its FitModel version vs under its from_version — an accepted fit counts by its latest
   long-term verdict, a gate_failed / rejected fit as concluded and not helpful; automatic rollback
   of the latest A2 change when confirmed harmful; rows join `outcomes`). With
   `fit.select.A2_ENABLED` (on since A2's offline validation passed), when the user has at least
   A2_MIN_OUTCOMES held-out outcomes (the latest max(A2_MIN_OUTCOMES, TEST_SHARE of all), with
   MIN_SPLIT training outcomes before them), A2_EVERY new outcomes since the last A2 run (the
   newest cycle record with `a2_n`), and the current A2 version's latest point is not an
   unconfirmed harmful one, `select_model` (the current model fitted from the default θ, so values
   fitted earlier on the held-out window cannot leak in; each neighbour from that train-window
   fit; Bonferroni over the neighbours) compares the neighbours of the current FitModel by held-out
   likelihood; a winner is committed as the next A2 version (used by the next A1 fit) with an
   `applied` MethodChange (gate = the comparison). The A1 fit patches record the registry version
   they were fitted under (`FitPatchOp.model_version`, from the registry's current model), which
   is what the monitoring keys on. No LLM. An A2 failure is logged and reported in `a2`; the rest
   of the run goes on; a failed selection still counts as a run (`a2_n`), so it is retried only
   after A2_EVERY new outcomes. The selection runs only in a run with `select_a2` (the background
   tick's `maybe_run`); the manual POST /wiki/rsi/run passes False and reports "deferred".
8. The harness loop (`HarnessTrack`, design 2026-10-21 §5): every cycle with a harness track
   first observes the harness changes in force (`HarnessTrack.observe`: the fast rollback of
   changes with new clear failures, `harness.fast_check`; rollback of confirmed harmful ones,
   lessons), also while the loop is off; its rows join the outcomes, and a failure is logged and
   named in `b` ("…; observe error: <Type>"). With `RsiSettings.harness` on: one harness round
   (without a harness track: `b` = "harness skipped: …", never anything else); the cycle is then
   due by the signals since the last harness round, and the round's cost is the cycle's. Its
   changes waiting for the user are approved / rejected through `approve_pending` /
   `reject_pending` like the others. With it off there is no improvement round (`b` = "off").
   The result keeps the field name `b` (stored in `rsi_cycle.result`) for the round's status.

The user's approval (`teach.approval`, Bot level `ask` for teaching_params / rsi): a track-A patch
whose Gate passed or an A2 method change that won its comparison is stored `awaiting_user` with a
ledger proposal instead of being applied (the daily accept cap does not apply to it); while one A
patch (or one A2 change) waits, that track proposes nothing new. `approve_pending` applies it if
what it was checked against has not moved (else closes it, PendingStale); `reject_pending` closes
it rejected. A run first closes every waiting change whose ledger row was rejected. Automatic
rollbacks and undo are never asked.

Track-A rows written before A1 carry a set_param body (ParamPatchOp); they stay readable, re-gatable
and undoable. The fit is computed before the write transaction. All datetimes are normalized to UTC."""

import logging
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any, Protocol

from evowiki.belief.aspects import AspectConfig, AspectEvent, Observe
from evowiki.goals.contracts import split_cell
from evowiki.i18n import current_language, tr
from evowiki.teach import infer_paths as ip
from evowiki.teach import registry
from evowiki.teach.approval import (
    METHOD_KIND,
    PATCH_KIND,
    RSI,
    STALE_A,
    STALE_METHOD,
    TEACHING_PARAMS,
    Approvals,
    PendingGone,
    PendingStale,
    line,
    method_key,
    patch_key,
)
from evowiki.teach.change_source import ChangeSource
from evowiki.teach.contracts import (
    ChangeOut,
    FitModel,
    FitPatchOp,
    GateResult,
    InferParams,
    Localization,
    MethodChange,
    OutcomeOut,
    ParamPatchOp,
    Patch,
    PatchMemoryEntry,
    PatchStatus,
    RsiSettings,
    Verdict,
)
from evowiki.teach.fit import select as a2
from evowiki.teach.fit.fitter import fit_infer
from evowiki.teach.fit.priors import PathPrior, load_priors
from evowiki.teach.gate_a import MIN_OUTCOMES, gate_a
from evowiki.teach.layer2 import method_registry
from evowiki.teach.layer2.outcomes import (
    NOTE_AUTO_ROLLBACK,
    NOTE_ROLLBACK_BLOCKED,
    NOTE_USER_UNDO,
    UNIT_A,
    UNIT_METHOD,
    ChangeRef,
    effect_line,
    method_after_count,
    outcome_a,
    outcome_method,
    undone_by_user,
)
from evowiki.teach.layer2.store import (
    MAX_SHOWN_POINTS,
    Layer2Store,
    MemoryLayer2Store,
    add_final_point,
    confirmed_harmful,
    due_for_point,
    record_point,
)
from evowiki.teach.layer2.store import transaction as l2_transaction
from evowiki.teach.memory import bump, recursion_metrics, signature
from evowiki.teach.predict import OTHER_DOMAIN, predict_outcomes
from evowiki.teach.rsi_store import CycleRecord, MemoryRecord, PatchRecord, RsiStore

log = logging.getLogger("evowiki.teach.cycle")

# B5c: a cycle the Bot started for strategy feedback (trigger "bot")
BOT_TRIGGER = "bot"

DAILY_ACCEPT_CAP = 2
MAX_GATE_RUNS = 3
REGATE_MIN_NEW = 10
MIN_SPLIT = 20                 # training outcomes needed for a retrospective split; outcomes for any split
TEST_SHARE = 0.4
MIN_MOVE = 0.01                # a fitted path counts as moved at ≥ 1 % of its range
FIT_PATH = "fit"               # Patch Memory path of a fit patch
A_ROLLBACK_REASON = "调整后预测变差，已自动撤回"
A2_ROLLBACK_REASON = "按新的估计方法做出的调整效果变差，已自动撤回"
HARNESS_UNAVAILABLE = "没有可用的写页面模型，这一轮跳过"   # harness on, no harness track wired
USER_REJECTED = "你没有批准这条改动"     # why prefix of a change the user rejected (teach.approval)
# change-row wording of one track-A parameter moving up / down (plain learner language, no numbers)
_A_PLAIN: Mapping[str, tuple[str, str]] = {
    "evidence_weight.quiz": ("测验题的结果更被看重了", "测验题的结果没那么被看重了"),
    "evidence_weight.check": ("页面里确认题的结果更被看重了", "页面里确认题的结果没那么被看重了"),
    "evidence_weight.self": ("你自己说的话更被相信了", "你自己说的话没那么被相信了"),
    "evidence_weight.feedback": ("你的反馈更被看重了", "你的反馈没那么被看重了"),
    "evidence_weight.ask": ("你的提问更被看重了", "你的提问没那么被看重了"),
    "evidence_weight.recall": ("复习时回忆的结果更被看重了", "复习时回忆的结果没那么被看重了"),
    "evidence_weight.slip_claim": ("你说「粗心答错」时更容易被相信了",
                                   "你说「粗心答错」时没那么容易被相信了"),
    "evidence_weight.learned_claim": ("你说「现在会了」时更容易被相信了",
                                      "你说「现在会了」时没那么容易被相信了"),
    "forgetting_scale": ("复习的间隔拉长了一些", "复习的间隔缩短了一些"),
    "slip": ("觉得你会做却失手的可能大了一些", "觉得你会做却失手的可能小了一些"),
    "guess_scale": ("觉得你蒙对的可能大了一些", "觉得你蒙对的可能小了一些"),
    "inference_strength": ("更多地用相关知识来判断你会不会", "更少地用相关知识来判断你会不会"),
    "start_known": ("觉得你一开始就会的更多了", "觉得你一开始就会的更少了"),
}


_A_EN = {
    "evidence_weight.quiz": "Quiz results", "evidence_weight.check": "Page-check results",
    "evidence_weight.self": "Self-reports", "evidence_weight.feedback": "Learning feedback",
    "evidence_weight.ask": "Questions asked", "evidence_weight.recall": "Review recall",
    "evidence_weight.slip_claim": "Accidental-mistake self-reports",
    "evidence_weight.learned_claim": "Newly-learned self-reports",
    "forgetting_scale": "Review intervals", "slip": "Estimated accidental-mistake probability",
    "guess_scale": "Estimated guessing probability", "inference_strength": "Related-knowledge inference",
    "start_known": "Estimated initial mastery",
}


def _a_phrase(moves: Sequence[tuple[str, bool]]) -> str:
    """One plain sentence for parameter moves [(path, went_up)]; at most three are named."""
    if current_language() == "en":
        parts = [f"{_A_EN[path]}: {'increased' if up else 'decreased'}" for path, up in moves[:3]]
        return "; ".join(parts) + ("; other parameters also adjusted" if len(moves) > 3 else "")
    parts = [_A_PLAIN[path][0 if up else 1] for path, up in moves[:3]]
    return "；".join(parts) + ("，还有几项也调了" if len(moves) > 3 else "")


def _a_why(moves: Sequence[tuple[str, bool]], failure: str) -> str:
    """The plain reason of a track-A move: from the direction, never from the fit statistics."""
    if current_language() == "en":
        if len(moves) == 1 and moves[0][0] == "forgetting_scale":
            return "You usually recalled the material after a few days." if moves[0][1] else "You often forgot the material after a few days."
        return ("Previous mastery estimates were too confident; this adjusts them."
                if failure == "overconfident" else "Previous mastery estimates were too low; this adjusts them.")
    if len(moves) == 1 and moves[0][0] == "forgetting_scale":
        return "你隔几天复习大多还记得" if moves[0][1] else "你隔几天复习常常已经忘了"
    if failure == "overconfident":
        return "之前总觉得你比实际更会，这样调一下更准"
    return "之前总觉得你比实际更不会，这样调一下更准"


def _unprefixed(why: str) -> str:
    """A stored why without its status prefix (rejected / stale / rolled back): the plugin shows
    the status itself."""
    for pre in (USER_REJECTED, STALE_A, STALE_METHOD, A_ROLLBACK_REASON, A2_ROLLBACK_REASON):
        if why.startswith(pre + "。"):
            return why[len(pre) + 1:]
    return why


@dataclass(frozen=True)
class UserData:
    """Everything the cycle reads; built by the service from observable logs."""

    events: Sequence[AspectEvent]
    domain_of: Mapping[str, str]
    domain_label: Mapping[str, str]
    signals_since: Callable[[datetime | None], int]   # observable signals after a time (None = all)


@dataclass
class Budget:
    limit_usd: float | None              # None = no cap (spend is still counted)
    spent_usd: float = 0.0

    def can_spend(self, usd: float) -> bool:
        return usd >= 0 and (self.limit_usd is None or self.spent_usd + usd <= self.limit_usd)

    def spend(self, usd: float) -> None:
        if usd < 0:
            raise ValueError("spend must be ≥ 0")
        self.spent_usd += usd


class HarnessTrack(Protocol):
    """The harness RSI loop (`harness.round.HarnessRounds`, design 2026-10-21 §5): with
    `RsiSettings.harness` on, a cycle runs one harness round, and the cycle is due by the signals since the last round (`last_round_at`). `run` owns its writes and
    returns the round's status line (shown in the cycle result's `b`) and its cost; it never
    raises (an error ends the round with a recorded reason). Its changes waiting for the user
    have ids `owns` recognizes; `approve` / `reject` decide them (PendingGone / PendingStale as
    `approve_pending`), `asked_keys` lists (change id, ledger key) of every one still waiting.
    `observe` watches the applied changes (`harness.observe`) and returns one outcome row each
    (`observation_row` keys); it may raise (the cycle records the failure)."""

    def observe(self, user_id: str, *, now: datetime) -> list[dict]: ...

    def last_round_at(self, user_id: str) -> datetime | None: ...

    def due_count(self, user_id: str, since: datetime | None) -> int: ...

    def run(self, user_id: str, *, now: datetime, manual: bool) -> tuple[str, float]: ...

    def owns(self, change_id: str) -> bool: ...

    def approve(self, user_id: str, change_id: str, *, now: datetime,
                on_accept: Callable[[], None]) -> None: ...

    def reject(self, user_id: str, change_id: str, *, now: datetime) -> None: ...

    def asked_keys(self, user_id: str) -> list[tuple[str, str]]: ...


@dataclass(frozen=True)
class CycleResult:
    ran: bool
    trigger: str
    a: str                     # e.g. "accepted p_x", "candidate p_x", "no_cluster"
    # the harness round's status line ("off" with `RsiSettings.harness` off); named `b` because
    # it is stored in rsi_cycle.result (it was track B's before RSI-B retired)
    b: str
    patch_ids: tuple[str, ...]
    cost_usd: float
    # "off" | "deferred" | "too_few_outcomes" | "not_due" | "waiting_harmful" | "no_better" |
    # "applied <id>" | "error …"
    a2: str = "off"


def _utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    return t.astimezone(UTC)


WEEKLY_MIN_SIGNALS = 10       # a weekly cycle needs this many new signals (user, 2026-10-28)
WEEK = timedelta(days=7)


def within_week(last_cycle: datetime | None, now: datetime) -> bool:
    """A cycle ran less than a week before `now` (the weekly setting's cap)."""
    return last_cycle is not None and _utc(now) - _utc(last_cycle) < WEEK


def due(settings: RsiSettings, last_cycle: datetime | None, signals_since_last: int,
        now: datetime) -> str | None:
    """The trigger name when a cycle is due, else None. manual is never due; weekly is due at most
    once a week and only with WEEKLY_MIN_SIGNALS new signals (fewer: it waits, however long)."""
    if settings.cycle == "every_10_feedback":
        return "every_10_feedback" if signals_since_last >= 10 else None
    if settings.cycle == "every_5_feedback":
        return "every_5_feedback" if signals_since_last >= 5 else None
    if settings.cycle == "weekly":
        enough = signals_since_last >= WEEKLY_MIN_SIGNALS
        return "weekly" if enough and not within_week(last_cycle, now) else None
    return None


@dataclass(frozen=True)
class _Draft:
    """Track A's new fit patch, computed before the write transaction."""

    body: FitPatchOp
    localization: Localization
    why: str
    cutoff: datetime
    from_version: int


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


class RsiCycle:
    def __init__(self, store: RsiStore, base: AspectConfig, *,
                 new_id: Callable[[str], str],
                 fit_model: Callable[[str], FitModel] | None = None,
                 priors: Mapping[str, PathPrior] | None = None,
                 layer2: Layer2Store | None = None,
                 sources: Sequence[ChangeSource] = (),
                 approvals: Approvals | None = None,
                 harness: HarnessTrack | None = None) -> None:
        self.store, self.base = store, base
        # the harness RSI loop; its round runs when the user's settings turn it on
        self.harness = harness
        # the user's approval before a change goes live (`teach.approval`); None: never asked
        self.approvals = approvals
        self.sources: tuple[ChangeSource, ...] = tuple(sources)
        self.new_id = new_id
        self._priors = priors
        self.layer2: Layer2Store = layer2 if layer2 is not None else MemoryLayer2Store()
        # A1's model structure in force: the user's A2 registry entry (implicit v1 = method_registry.DEFAULT_FIT_MODEL)
        self.fit_model: Callable[[str], FitModel] = (
            fit_model if fit_model is not None
            else lambda uid: method_registry.current_model(self.layer2, uid))

    @property
    def priors(self) -> Mapping[str, PathPrior]:
        """The hierarchical priors; the committed file is read on first use."""
        if self._priors is None:
            self._priors = load_priors()
        return self._priors

    # ---- public ----

    def run(self, user_id: str, data: UserData, *, trigger: str, now: datetime,
            select_a2: bool = True, context: Mapping[str, Any] | None = None) -> CycleResult:
        """One cycle. `select_a2` False (the manual POST /wiki/rsi/run): A2's model selection
        (1 + k fits) is left to the background tick (`a2` = "deferred"); its monitoring still
        runs. `context`: what started it, stored in the cycle record's result (the Bot's
        `bot_trigger_id`, B5c)."""
        now = _utc(now)
        self._close_rejected(user_id, now)
        pre = self._prepare_a(user_id, data, now)
        measured = self._measure_a(user_id, data, now)          # CPU work outside the transaction
        with self._tx():
            a, a_ids = self._track_a(user_id, data, now, pre)
            outcomes = self._apply_outcomes_a(user_id, measured, now)
        a2_status, a2_rows, a2_n = self._a2(user_id, data, now, select=select_a2)   # outside the tx
        outcomes += a2_rows
        settings = self.store.settings(user_id)
        h_obs = ""
        if self.harness is not None:
            # the harness changes in force are watched before the round, and also while the loop
            # is off (no LLM call; no applied change = one query); a failure is logged and the run
            # goes on (track A already committed: the cycle record must be written)
            try:
                outcomes += self.harness.observe(user_id, now=now)
            except Exception as e:  # broad on purpose: recorded in `b`, the run goes on
                log.exception("harness observation failed for %s", user_id)
                h_obs = f"observe error: {type(e).__name__}"
        budget = Budget(limit_usd=None)            # the harness round has its own month cap
        if settings.harness and self.harness is None:
            # no harness track wired (no deep tier or gateway): skipped, never anything else
            b = f"harness skipped: {HARNESS_UNAVAILABLE}"
        elif settings.harness and self.harness is not None:
            # the cycle is due by the round's own count; a manual run, and one the light review of his
            # corrections started (`harness.triage`), skip that check
            try:
                b, cost = self.harness.run(user_id, now=now, manual=trigger in ("manual", "correction"))
                budget.spend(cost)
            except Exception as e:  # broad on purpose, like the observation: track A committed
                log.exception("harness round failed for %s", user_id)
                b = f"error: {type(e).__name__}"
        else:
            b = "off"                             # no improvement round with the loop off
        if h_obs:
            b = f"{b}; {h_obs}"
        res = CycleResult(ran=True, trigger=trigger, a=a, b=b, patch_ids=a_ids,
                          cost_usd=budget.spent_usd, a2=a2_status)
        mem = self.store.memory(user_id)
        result = {**dict(context or {}), **asdict(res), "patch_ids": list(res.patch_ids),
                  "outcomes": outcomes, "a2_n": a2_n,
                  "recursion_metrics": {"A": asdict(recursion_metrics(mem, "A"))}}
        with self._tx():
            self.store.add_cycle(CycleRecord(user_id=user_id, at=now, trigger=trigger, result=result,
                                             cost_usd=res.cost_usd))
        return res

    def maybe_run(self, user_id: str, data: UserData, *, now: datetime) -> CycleResult | None:
        now = _utc(now)
        settings = self.store.settings(user_id)
        last = self.last_run_at(user_id, settings)
        # the harness round is due by its own count (page signals + assistant feedback)
        n = (self.harness.due_count(user_id, last) if settings.harness and self.harness is not None
             else data.signals_since(last))
        trigger = due(settings, last, n, now)
        return None if trigger is None else self.run(user_id, data, trigger=trigger, now=now)

    def last_run_at(self, user_id: str, settings: RsiSettings) -> datetime | None:
        """What `due` counts signals from: the last harness round when the harness loop is on,
        else the last cycle."""
        if settings.harness and self.harness is not None:
            at = self.harness.last_round_at(user_id)
            return None if at is None else _utc(at)
        return max((_utc(c.at) for c in self.store.cycles(user_id)), default=None)

    def undo(self, user_id: str, patch_id: str, *, now: datetime) -> PatchRecord:
        """The user's undo of an accepted track-A patch (ValueError unless accepted or for a
        record of the retired track B, LookupError for an unknown id)."""
        now = _utc(now)
        found = next((r for r in self.store.patches(user_id) if r.patch.patch_id == patch_id), None)
        if found is not None and found.patch.track != "A":
            raise ValueError(f"patch {patch_id} belongs to the retired track B; it cannot be undone")
        with self._tx():
            rec = next((r for r in self.store.patches(user_id) if r.patch.patch_id == patch_id), None)
            if rec is None:
                raise LookupError(f"no patch {patch_id}")
            return self._rollback_a(user_id, rec, now, by_user=True)

    def changes(self, user_id: str, data: UserData, *, now: datetime | None = None
                ) -> list[ChangeOut]:
        """Every RSI change of every layer, newest first: A patches (layer 0), A2 method changes
        (layer 2) and the rows of the external sources (the harness loop's, the graph's …); stored
        rows of the retired track B are left out. Each A / method row carries its
        target, its outcome series from `layer2` (oldest first), the latest verdict and its effect
        line; an applied change still pending says how much after-side evidence it has now (the
        cheap counts of `_after_counts`, nothing written). A source whose `list` raises is logged
        and skipped; the rest still shows. `now` (default: the wall clock) bounds the counts."""
        now = _utc(now) if now is not None else datetime.now(UTC)
        mem = {r.entry.patch_id: r for r in self.store.memory(user_id)}
        series: dict[str, list[OutcomeOut]] = defaultdict(list)
        for o in self.layer2.outcome_points(user_id):          # oldest first
            series[o.change_id].append(o)
        patches = self.store.patches(user_id)
        mcs = self.layer2.method_changes(user_id)
        counts = self._after_counts(user_id, data, patches, mcs, series, now)
        out = []
        for rec in patches:
            if rec.patch.track != "A":               # the retired track B (migration 0099)
                continue
            row = self._change_a(user_id, rec, mem.get(rec.patch.patch_id), data,
                                 points=series[rec.patch.patch_id],
                                 held=bool(self._newer_overlap(rec, patches)))
            out.append(_with_effect(row, 0, _TARGET["A"], series[row.patch_id],
                                    n_now=counts.get(row.patch_id), unit=UNIT_A))
        latest = {method_registry.latest_applied(mcs, "A2")}
        for c in mcs:
            out.append(_with_effect(_change_method(c, c.change_id in latest), 2, _TARGET[c.kind],
                                    series[c.change_id], n_now=counts.get(c.change_id),
                                    unit=UNIT_METHOD))
        for src in self.sources:
            try:
                out.extend(src.list(user_id))
            except Exception:  # broad on purpose: one failing source must not hide the others
                log.exception("change source %r failed for %s; its rows are left out",
                              src.prefix, user_id)
        out.sort(key=lambda c: c.at, reverse=True)
        return out

    def _after_counts(self, user_id: str, data: UserData, patches: Sequence[PatchRecord],
                      mcs: Sequence[MethodChange], series: Mapping[str, Sequence[OutcomeOut]],
                      now: datetime) -> dict[str, int]:
        """Change id → the cheap after-side count (what `due_for_point` compares; no bootstrap, no
        point written) of every applied change whose latest point is pending or missing — the
        only rows whose effect line shows it. Track A: the predicted outcomes after acceptance
        (one replay, as in `_measure_a`); A2: the decided fit children. A count that fails is
        logged and left out (its line then says no number)."""
        def waiting(cid: str) -> bool:
            pts = series.get(cid, ())
            return not pts or pts[-1].verdict == "pending"

        out: dict[str, int] = {}
        a = [r for r in patches if r.patch.track == "A" and r.patch.status == "personal_accepted"
             and r.updated_at is not None and waiting(r.patch.patch_id)]
        if a:
            try:
                targets = [_utc(o.ts) for o in predict_outcomes(data.events, self.base,
                                                                InferParams(), data.domain_of)]
            except Exception:  # broad on purpose: a count must not hide the change list
                log.exception("track A after-side counts failed for %s", user_id)
            else:
                for r in a:
                    applied = _utc(r.updated_at)                     # type: ignore[arg-type]
                    out[r.patch.patch_id] = sum(1 for t in targets if applied < t <= now)
        applied = [c for c in mcs if c.status == "applied" and waiting(c.change_id)]
        if any(c.kind == "A2" for c in applied):
            try:
                children, applied_ids, _ = self._a2_children(user_id, series)
                for c in applied:
                    if c.kind == "A2" and c.to_version is not None:
                        out[c.change_id] = method_after_count(children.get(c.to_version, []),
                                                              applied_ids)
            except Exception:  # broad on purpose, as above
                log.exception("A2 after-side counts failed for %s", user_id)
        return out

    def undo_method(self, user_id: str, change_id: str, *, now: datetime) -> MethodChange:
        """Undo an applied layer-2 method change: its registry rolls back to the change's
        from_version (a new version copying it) and the change becomes rolled_back. Only the latest
        applied change of its kind can be undone (the highest to_version among the applied ones;
        undoing the newest makes the one before it the latest again): rolling back an older one
        would copy a whole version and silently drop the later changes.
        LookupError for an unknown id, ValueError unless applied and latest."""
        now = _utc(now)
        mcs = self.layer2.method_changes(user_id)
        c = next((m for m in mcs if m.change_id == change_id), None)
        if c is None:
            raise LookupError(f"no method change {change_id}")
        if c.status != "applied":
            raise ValueError(f"method change {change_id} is {c.status}, only applied can be undone")
        if method_registry.latest_applied(mcs, c.kind) != change_id:
            raise ValueError(f"method change {change_id} is not the latest {c.kind} change; "
                             "undo the newer one first")
        out = c.model_copy(update={"status": "rolled_back", "updated_at": now})
        with l2_transaction(self.layer2):       # the rollback version and the change row together
            method_registry.rollback(self.layer2, user_id, c.kind, c.from_version, now=now)
            self.layer2.put_method_change(user_id, out)
            add_final_point(self.layer2, user_id, change_id, NOTE_USER_UNDO, now)
        return out

    # ---- the user's approval (teach.approval) ----

    def _asks(self, user_id: str, scope: str) -> bool:
        return self.approvals is not None and self.approvals.asks(user_id, scope)

    def approve_pending(self, user_id: str, change_id: str, *, now: datetime,
                        on_accept: Callable[[], None]) -> None:
        """Apply a change waiting for the user: an A patch or a layer-2 method change in
        status `awaiting_user`. `on_accept()` (the action ledger's exactly-once effect) runs first
        inside the transaction that commits the change, so both commit or neither. What it was
        checked against must not have moved since the proposal (track A: the θ_infer version; a
        method change: its registry version); if it did, the change is
        closed (gate_failed, saying so) without being applied and PendingStale is raised.
        PendingGone: no such change waiting. The caller holds the user's cycle lock. A harness
        change (`HarnessTrack.owns`) goes to the harness loop."""
        now = _utc(now)
        if self.harness is not None and self.harness.owns(change_id):
            self.harness.approve(user_id, change_id, now=now, on_accept=on_accept)
            return
        rec = next((r for r in self.store.patches(user_id) if r.patch.patch_id == change_id), None)
        if rec is not None and rec.patch.track != "A":
            raise PendingGone(f"patch {change_id} belongs to the retired track B")
        if rec is not None:
            self._approve_a(user_id, change_id, now, on_accept)
        else:
            self._approve_method(user_id, change_id, now, on_accept)

    def reject_pending(self, user_id: str, change_id: str, *, now: datetime) -> None:
        """The user rejected a change waiting for them: it is closed `rejected` (never applied)
        and, for a patch, remembered in Patch Memory as harmful (the proposer's memory of what the
        user turned down). PendingGone: no such change waiting."""
        now = _utc(now)
        if self.harness is not None and self.harness.owns(change_id):
            self.harness.reject(user_id, change_id, now=now)
            return
        rec = next((r for r in self.store.patches(user_id) if r.patch.patch_id == change_id), None)
        if rec is not None and rec.patch.track != "A":
            raise PendingGone(f"patch {change_id} belongs to the retired track B")
        if rec is not None:
            with self._tx():
                rec = self._awaiting_patch(user_id, change_id)
                p = rec.patch
                self.store.put_patch(replace(rec, patch=p.model_copy(update={
                    "status": "rejected", "why": f"{USER_REJECTED}。{p.why}"[:600]}),
                    updated_at=now))
                m = self._memory_of(user_id, change_id)
                if m is not None:
                    self.store.put_memory(bump(self._with_status(m, "rejected"), harmful=1.0,
                                               now=now))
            return
        with l2_transaction(self.layer2):
            c = self._awaiting_method(user_id, change_id)
            self.layer2.put_method_change(user_id, c.model_copy(update={
                "status": "rejected", "updated_at": now,
                "why": f"{USER_REJECTED}。{c.why}"[:600]}))

    def _close_rejected(self, user_id: str, now: datetime) -> None:
        """Close every change still waiting whose ledger row the user rejected (the ledger's
        reject hook did not get to run, e.g. a cycle held the lock). Logged, never raises."""
        ap = self.approvals
        if ap is None:
            return
        try:
            ids = [r.patch.patch_id for r in self.store.patches(user_id)
                   if r.patch.status == "awaiting_user"
                   and ap.rejected(user_id, patch_key(user_id, r.patch.patch_id))]
            ids += [c.change_id for c in self.layer2.method_changes(user_id)
                    if c.status == "awaiting_user"
                    and ap.rejected(user_id, method_key(user_id, c.change_id))]
            if self.harness is not None:
                ids += [cid for cid, key in self.harness.asked_keys(user_id)
                        if ap.rejected(user_id, key)]
            for cid in ids:
                self.reject_pending(user_id, cid, now=now)
        except Exception:  # broad on purpose: the cycle goes on; the next run retries
            log.exception("closing rejected changes failed for %s", user_id)

    def _awaiting_patch(self, user_id: str, patch_id: str) -> PatchRecord:
        rec = next((r for r in self.store.patches(user_id) if r.patch.patch_id == patch_id), None)
        if rec is None or rec.patch.status != "awaiting_user":
            raise PendingGone(f"patch {patch_id} is not waiting for the user")
        return rec

    def _awaiting_method(self, user_id: str, change_id: str) -> MethodChange:
        c = next((m for m in self.layer2.method_changes(user_id) if m.change_id == change_id),
                 None)
        if c is None or c.status != "awaiting_user":
            raise PendingGone(f"method change {change_id} is not waiting for the user")
        return c

    def _propose_a(self, user_id: str, rec: PatchRecord, cur_version: int, old: InferParams,
                   new: InferParams, close: dict, delta: float | None, now: datetime) -> str:
        """A track-A patch that passed its Gate, the user's level `ask`: stored awaiting the user
        (gated against `cur_version`, its from_version now) with its Patch Memory row (no counts
        yet) and its ledger proposal, inside the caller's transaction."""
        p = rec.patch.model_copy(update={"status": "awaiting_user", "from_version": cur_version})
        self.store.put_patch(replace(rec, patch=p))
        self._close(user_id, p, status="awaiting_user", delta=delta, **close)
        values = self._a_values(p)
        moves = [(path, ip.get(new, path) > ip.get(old, path)) for path in values]
        diff = [line("ctx", "学习状态评估")]
        for path in values:
            diff += [line("del", f"{self._label(path)} {ip.get(old, path):.2f}"),
                     line("add", f"{self._label(path)} {ip.get(new, path):.2f}")]
        assert self.approvals is not None
        self.approvals.propose(
            user_id, kind=PATCH_KIND, scope=TEACHING_PARAMS, key=patch_key(user_id, p.patch_id),
            summary=_a_phrase(moves)[:300], why=_a_why(moves, p.localization.failure),
            content={"patch_id": p.patch_id, "track": "A", "diff": diff}, at=now)
        return f"awaiting_user {p.patch_id}"

    def _approve_a(self, user_id: str, patch_id: str, now: datetime,
                   on_accept: Callable[[], None]) -> None:
        stale = False
        with self._tx():
            rec = self._awaiting_patch(user_id, patch_id)
            p = rec.patch
            cur = registry.current(self.store, user_id, "A")
            m = self._memory_of(user_id, patch_id)
            if cur.version != p.from_version:
                self.store.put_patch(replace(rec, patch=p.model_copy(update={
                    "status": "gate_failed", "why": f"{STALE_A}。{p.why}"[:600]}),
                    updated_at=now))
                if m is not None:
                    self.store.put_memory(self._with_status(m, "gate_failed"))
                stale = True
            else:
                on_accept()
                new = self._apply(self._infer_of(cur.infer), self._a_values(p))
                v = registry.commit(self.store, user_id, "A", infer=new, patch_id=patch_id,
                                    now=now)
                self.store.put_patch(replace(rec, patch=p.model_copy(update={
                    "status": "personal_accepted", "to_version": v.version}), updated_at=now))
                if m is not None:
                    self.store.put_memory(bump(self._with_status(m, "personal_accepted"),
                                               helpful=1.0, now=now))
        if stale:
            raise PendingStale(STALE_A)

    def _approve_method(self, user_id: str, change_id: str, now: datetime,
                        on_accept: Callable[[], None]) -> None:
        stale = False
        with l2_transaction(self.layer2):   # the version and its change row together
            c = self._awaiting_method(user_id, change_id)
            cur = method_registry.current(self.layer2, user_id, c.kind)
            if cur.version != c.from_version:
                self.layer2.put_method_change(user_id, c.model_copy(update={
                    "status": "gate_failed", "updated_at": now,
                    "why": f"{STALE_METHOD}。{c.why}"[:600]}))
                stale = True
            else:
                on_accept()
                gate = dict(c.gate)
                v = method_registry.commit(
                    self.layer2, user_id, "A2",
                    fit_model=FitModel.model_validate(gate["to_model"]), change_id=change_id,
                    now=now)
                gate["to_model"] = v.fit_model.model_dump(mode="json")
                self.layer2.put_method_change(user_id, c.model_copy(update={
                    "status": "applied", "to_version": v.version, "updated_at": now,
                    "gate": gate}))
        if stale:
            raise PendingStale(STALE_METHOD)

    # ---- A2 (layer 2 of track A) ----

    def _a2(self, user_id: str, data: UserData, now: datetime, *, select: bool = True
            ) -> tuple[str, list[dict], int | None]:
        """Step 7 of the module doc: (status, outcome rows, outcomes counted when select ran).
        `select` False: monitoring only, the selection is "deferred" (left to the tick)."""
        try:
            rows = self._monitor_a2(user_id, now)
        except Exception as e:  # broad on purpose: A2 must not stop the rest of the run
            log.exception("A2 monitoring failed for %s", user_id)
            return f"error {type(e).__name__}: {str(e)[:160]}", [], None
        if not a2.A2_ENABLED:
            return "off", rows, None
        if not select:
            return "deferred", rows, None
        try:
            status, n = self._run_a2(user_id, data, now)
        except Exception as e:  # broad on purpose: reported, nothing written
            log.exception("A2 failed for %s", user_id)
            return f"error {type(e).__name__}: {str(e)[:160]}", rows, None
        return status, rows, n

    def _last_a2_n(self, user_id: str) -> int | None:
        """Outcomes counted at the last A2 run (the newest cycle record with `a2_n`)."""
        runs = [c for c in self.store.cycles(user_id) if c.result.get("a2_n") is not None]
        return None if not runs else int(max(runs, key=lambda c: _utc(c.at)).result["a2_n"])

    def _run_a2(self, user_id: str, data: UserData, now: datetime) -> tuple[str, int | None]:
        # the target set does not depend on θ, so the default θ counts the same outcomes
        outcomes = predict_outcomes(data.events, self.base, InferParams(), data.domain_of)
        n = len(outcomes)
        n_test = max(a2.A2_MIN_OUTCOMES, ceil(TEST_SHARE * n))
        if n - n_test < MIN_SPLIT:
            return "too_few_outcomes", None
        last = self._last_a2_n(user_id)
        if last is not None and n - last < a2.A2_EVERY:
            return "not_due", None
        waiting = next((c for c in self.layer2.method_changes(user_id, "A2")
                        if c.status == "awaiting_user"), None)
        if waiting is not None:                         # one proposal at a time
            return f"awaiting_user {waiting.change_id}", None
        cur = method_registry.current(self.layer2, user_id, "A2")
        made = next((c for c in self.layer2.method_changes(user_id, "A2")
                     if c.status == "applied" and c.to_version == cur.version), None)
        if made is not None:
            pts = self.layer2.outcome_points(user_id, made.change_id)
            if pts and pts[-1].verdict == "harmful" and not confirmed_harmful(pts):
                return "waiting_harmful", None          # wait for the confirming / denying point
        cutoff = _utc(outcomes[n - n_test].ts)
        model = method_registry.current_model(self.layer2, user_id)
        # an undone, automatically rolled-back or user-rejected structure is never selected again
        # (撤销 / 不批准 sticks)
        banned = {a2.structure_key(FitModel.model_validate(c.gate["to_model"]))
                  for c in self.layer2.method_changes(user_id, "A2")
                  if c.status in ("rolled_back", "rejected") and c.gate.get("to_model")}
        try:
            pick = a2.select_model(data.events, self.base, InferParams(), data.domain_of, model,
                                   self.priors, cutoff=cutoff, seed=f"{user_id}/a2/{n}",
                                   banned=banned)
        except Exception as e:  # broad on purpose: reported, nothing written
            # counts as a run (a2_n = n): a persistent failure is retried after A2_EVERY new
            # outcomes, not on every cycle (each try costs 1 + k fits)
            log.exception("A2 selection failed for %s at %d outcomes", user_id, n)
            return f"error {type(e).__name__}: {str(e)[:160]}", n
        if pick is None:
            return "no_better", n
        label, new, cmp = pick
        cid = self.new_id("a2")
        summary = _a2_summary(label)            # built first: a bad label writes nothing
        why = "用新方法回头猜你最近做过的题，猜得比现在的方法更准"
        gate = {**cmp, "label": label, "cutoff": cutoff.isoformat(), "n_outcomes": n,
                "from_model": model.model_dump(mode="json")}
        if self._asks(user_id, RSI):            # the user approves it first (teach.approval)
            gate["to_model"] = new.model_dump(mode="json")      # what the approval commits
            with l2_transaction(self.layer2):   # the change row and its ledger proposal together
                self.layer2.put_method_change(user_id, MethodChange(
                    change_id=cid, kind="A2", status="awaiting_user", summary=summary,
                    why=why[:600], from_version=cur.version, gate=gate, created_at=now))
                assert self.approvals is not None
                self.approvals.propose(
                    user_id, kind=METHOD_KIND, scope=RSI, key=method_key(user_id, cid),
                    summary=summary[:300], why=why[:600],
                    content={"change_id": cid, "kind": "A2",
                             "diff": [line("ctx", "估计方法"), line("add", summary)]}, at=now)
            return f"awaiting_user {cid}", n
        with l2_transaction(self.layer2):       # the version and its change row together
            v = method_registry.commit(self.layer2, user_id, "A2", fit_model=new, change_id=cid,
                                       now=now)
            gate["to_model"] = v.fit_model.model_dump(mode="json")
            self.layer2.put_method_change(user_id, MethodChange(
                change_id=cid, kind="A2", status="applied", summary=summary, why=why[:600],
                from_version=cur.version, to_version=v.version, gate=gate, created_at=now))
        return f"applied {cid}", n

    def _a2_children(self, user_id: str, series: Mapping[str, Sequence[OutcomeOut]]
                     ) -> tuple[dict[int, list[OutcomeOut]], set[str], set[str]]:
        """(FitModel version → outcomes of the A1 fit patches made under it, the ids of the
        applied ones, the ids the user undid): an accepted fit counts by its latest point, a
        gate_failed / rejected one as concluded and not helpful."""
        mem = {m.entry.patch_id: m for m in self.store.memory(user_id)}
        children: dict[int, list[OutcomeOut]] = defaultdict(list)
        applied_ids: set[str] = set()
        undone: set[str] = set()
        for r in self.store.patches(user_id):
            p = r.patch
            if p.track != "A" or not isinstance(p.body, FitPatchOp):
                continue
            v = p.body.model_version
            if p.status in ("gate_failed", "rejected"):
                children[v].append(OutcomeOut(change_id=p.patch_id, at=r.updated_at or p.created_at,
                                              verdict="no_effect", note="没有生效"))
            elif p.status in _EVER_APPLIED:
                applied_ids.add(p.patch_id)
                m = mem.get(p.patch_id)
                if p.status == "rolled_back" and undone_by_user(
                        series.get(p.patch_id, []),
                        legacy_harmful=m is not None and m.entry.long_term == "harmful"):
                    undone.add(p.patch_id)             # the user's undo, not the automatic one
                if series.get(p.patch_id):
                    children[v].append(series[p.patch_id][-1])
        return children, applied_ids, undone

    def _monitor_a2(self, user_id: str, now: datetime) -> list[dict]:
        """The outcome of every applied A2 change (stored rows only, no fits), recomputed only when
        its children's evidence grew by MIN_EVENTS (`due_for_point`); the latest applied one is
        rolled back when its stored series confirms a harmful verdict (checked every run, so a
        failed rollback is retried). One row per change it looked at (the keys of the cycle's
        outcome rows)."""
        changes = self.layer2.method_changes(user_id, "A2")
        applied = [c for c in changes if c.status == "applied" and c.to_version is not None]
        if not applied:
            return []
        series: dict[str, list[OutcomeOut]] = defaultdict(list)
        for o in self.layer2.outcome_points(user_id):          # oldest first
            series[o.change_id].append(o)
        children, applied_ids, undone = self._a2_children(user_id, series)
        top = method_registry.latest_applied(changes, "A2")
        out = []
        for c in applied:
            kids = children.get(c.to_version, [])    # type: ignore[arg-type]
            pts = series.get(c.change_id, [])
            point = False
            o: OutcomeOut | None = None
            if due_for_point(pts, method_after_count(kids, applied_ids)):
                ref = ChangeRef(change_id=c.change_id, layer=2, kind="A2:model",
                                applied_at=c.created_at, method_version=c.to_version,
                                signal_kinds=())
                o = outcome_method(ref, kids, children.get(c.from_version, []), now=now,
                                   undone=undone, undo_among=applied_ids)
                point = record_point(self.layer2, user_id, o)
                pts = self.layer2.outcome_points(user_id, c.change_id)
            if o is None and not confirmed_harmful(pts):
                continue
            roll = c.change_id == top and confirmed_harmful(pts)
            if roll:
                with l2_transaction(self.layer2):   # the rollback version and the change row together
                    method_registry.rollback(self.layer2, user_id, "A2", c.from_version, now=now)
                    self.layer2.put_method_change(user_id, c.model_copy(update={
                        "status": "rolled_back", "updated_at": now,
                        "why": f"{A2_ROLLBACK_REASON}。{c.why}"[:600],
                        "gate": {**c.gate, "rollback_reason": A2_ROLLBACK_REASON}}))
                    add_final_point(self.layer2, user_id, c.change_id, NOTE_AUTO_ROLLBACK, now)
            last = o if o is not None else pts[-1]
            out.append({"change_id": c.change_id, "verdict": last.verdict,
                        "n": max((d.n_after for d in last.deltas), default=0), "point": point,
                        "rolled_back": roll, "note": A2_ROLLBACK_REASON if roll else last.note})
        return out

    # ---- track A ----

    def _prepare_a(self, user_id: str, data: UserData, now: datetime) -> _Draft | str | None:
        """None when an open candidate exists (handled in the write phase); a status string when no
        patch will be gated; else the draft."""
        if self._open_candidate(user_id) is not None:
            return None
        waiting = next((r for r in self.store.patches(user_id)
                        if r.patch.track == "A" and r.patch.status == "awaiting_user"), None)
        if waiting is not None:          # one track-A proposal at a time: the user decides first
            return f"awaiting_user {waiting.patch.patch_id}"
        cur = registry.current(self.store, user_id, "A")
        infer = self._infer_of(cur.infer)
        outcomes = predict_outcomes(data.events, self.base, infer, data.domain_of)
        n = len(outcomes)
        n_test = max(MIN_OUTCOMES, ceil(TEST_SHARE * n))
        if n - n_test >= MIN_SPLIT:
            cutoff = _utc(outcomes[n - n_test].ts)
        elif n >= MIN_SPLIT:
            cutoff = now
        else:
            return "too_few_outcomes"
        train = [o for o in outcomes if o.ts < cutoff]
        if not train:                            # e.g. every outcome is dated after `now`
            return "too_few_outcomes"
        model = self.fit_model(user_id)
        fit = fit_infer(data.events, self.base, infer, data.domain_of, model=model,
                        priors=self.priors, cutoff=cutoff)
        moved = []
        for post in fit.posteriors:
            lo, hi = ip.bounds(post.path)
            if abs(post.value - ip.get(infer, post.path)) >= MIN_MOVE * (hi - lo):
                moved.append(post)
        if not moved:
            return "fit_no_change"
        body = FitPatchOp(values={q.path: q.value for q in moved},
                          intervals={q.path: (min(q.lo, q.value), max(q.hi, q.value)) for q in moved},
                          model_version=model.version)
        residual = sum(o.p - o.y for o in train) / len(train)
        over = residual > 0
        reason = (f"过去 {len(train)} 道题里，系统平均{'高估' if over else '低估'}了你答对的可能性"
                  f"（{abs(residual):.2f}）。")
        failure = "overconfident" if over else "underconfident"
        why = _a_why([(q.path, q.value > ip.get(infer, q.path)) for q in moved], failure)
        loc = Localization(track="A", failure=failure,
                           target="infer_param", ref=FIT_PATH,
                           signals=tuple(o.event_id for o in train), reason=reason[:600])
        return _Draft(body=body, localization=loc, why=why[:600], cutoff=cutoff,
                      from_version=cur.version)

    def _track_a(self, user_id: str, data: UserData, now: datetime, pre: _Draft | str | None
                 ) -> tuple[str, tuple[str, ...]]:
        cand = self._open_candidate(user_id)
        if cand is not None:
            status, ids = self._regate(user_id, data, now, cand)
        elif isinstance(pre, _Draft):
            status, ids = self._new_patch(user_id, data, now, pre)
        else:
            status, ids = (pre if pre is not None else "candidate_closed"), ()
        return status, ids

    def _new_patch(self, user_id: str, data: UserData, now: datetime, d: _Draft
                   ) -> tuple[str, tuple[str, ...]]:
        pid = self.new_id("rsi")
        patch = Patch(patch_id=pid, track="A", localization=d.localization, body=d.body, why=d.why,
                      from_version=d.from_version, created_at=now)
        rec = PatchRecord(user_id=user_id, patch=patch, cutoff=d.cutoff, updated_at=now)
        self.store.put_patch(rec)
        return self._gate(user_id, data, now, rec)

    def _regate(self, user_id: str, data: UserData, now: datetime, rec: PatchRecord
                ) -> tuple[str, tuple[str, ...]]:
        pid = rec.patch.patch_id
        if rec.gate_runs >= MAX_GATE_RUNS:       # cap reached without a decision
            return self._reject_capped(user_id, data, now, rec, None), (pid,)
        if rec.cutoff is None:
            raise ValueError(f"channel-A candidate {pid} has no cutoff")
        infer = registry.infer_for(self.store, user_id)
        cutoff = _utc(rec.cutoff)
        n_test = sum(1 for o in predict_outcomes(data.events, self.base, infer, data.domain_of)
                     if o.ts >= cutoff)
        if n_test - rec.last_gate_outcomes < REGATE_MIN_NEW:
            return f"waiting {pid}", ()
        return self._gate(user_id, data, now, rec)

    def _gate(self, user_id: str, data: UserData, now: datetime, rec: PatchRecord
              ) -> tuple[str, tuple[str, ...]]:
        """Gate `rec` against the current θ and decide."""
        p = rec.patch
        pid = p.patch_id
        cur = registry.current(self.store, user_id, "A")
        old = self._infer_of(cur.infer)
        new = self._apply(old, self._a_values(p))
        assert rec.cutoff is not None
        g = gate_a(pid, data.events, self.base, old, new, cutoff=_utc(rec.cutoff),
                   domain_of=data.domain_of)
        n_gate = g.n_outcomes or 0
        if "insufficient_outcomes" in g.vetoes:
            self.store.put_patch(replace(rec, last_gate_outcomes=n_gate, updated_at=now))
            return f"candidate {pid}", (pid,)
        rec = replace(rec, gate_runs=rec.gate_runs + 1, last_gate_outcomes=n_gate, updated_at=now)
        n_all = len(predict_outcomes(data.events, self.base, old, data.domain_of))
        close = {"gate": g, "domain": self._domain_of_patch(p, data), "old": old,
                 "n_outcomes": n_all, "now": now}
        if g.passed and self._asks(user_id, TEACHING_PARAMS):   # the user approves it first
            du = (g.utility_after - g.utility_before
                  if g.utility_after is not None and g.utility_before is not None else None)
            return self._propose_a(user_id, rec, cur.version, old, new, close, du, now), (pid,)
        if g.passed and self._accepted_today(user_id, now) < DAILY_ACCEPT_CAP:
            v = registry.commit(self.store, user_id, "A", infer=new, patch_id=pid, now=now)
            self.store.put_patch(replace(rec, patch=p.model_copy(update={
                "status": "personal_accepted", "from_version": cur.version, "to_version": v.version})))
            du = (g.utility_after - g.utility_before
                  if g.utility_after is not None and g.utility_before is not None else None)
            self._close(user_id, p, status="personal_accepted", helpful=1.0, delta=du, **close)
            return f"accepted {pid}", (pid,)
        if g.passed:                             # daily cap: stays candidate unless out of gate runs
            if rec.gate_runs >= MAX_GATE_RUNS:
                return self._reject_capped(user_id, data, now, rec, close), (pid,)
            self.store.put_patch(rec)
            return f"capped {pid}", (pid,)
        self.store.put_patch(replace(rec, patch=p.model_copy(update={"status": "gate_failed"})))
        self._close(user_id, p, status="gate_failed", harmful=1.0, **close)
        return f"gate_failed {pid}", (pid,)

    def _reject_capped(self, user_id: str, data: UserData, now: datetime, rec: PatchRecord,
                       close: dict | None) -> str:
        p = rec.patch
        if close is None:
            infer = registry.infer_for(self.store, user_id)
            close = {"gate": GateResult(patch_id=p.patch_id, channel="A", passed=False,
                                        vetoes=("max_gate_runs",)),
                     "domain": self._domain_of_patch(p, data), "old": infer,
                     "n_outcomes": len(predict_outcomes(data.events, self.base, infer, data.domain_of)),
                     "now": now}
        self.store.put_patch(replace(rec, patch=p.model_copy(update={"status": "rejected"}),
                                     updated_at=now))
        self._close(user_id, p, status="rejected", harmful=1.0, **close)
        return f"rejected {p.patch_id}"

    def _measure_a(self, user_id: str, data: UserData, now: datetime
                   ) -> list[tuple[str, OutcomeOut]]:
        """(patch id, outcome) of the accepted track-A patches whose evidence after acceptance grew
        by MIN_EVENTS since their last stored point (`due_for_point`; the cheap count is one replay
        per run, shared by every patch); pure, before the transaction."""
        recs = [r for r in self.store.patches(user_id)
                if r.patch.track == "A" and r.patch.status == "personal_accepted"
                and r.updated_at is not None and r.patch.to_version is not None]
        if not recs:
            return []
        series: dict[str, list[OutcomeOut]] = defaultdict(list)
        for o in self.layer2.outcome_points(user_id):          # oldest first
            series[o.change_id].append(o)
        # the target set does not depend on θ: one replay under the default θ counts them all
        targets = [_utc(o.ts) for o in predict_outcomes(data.events, self.base, InferParams(),
                                                        data.domain_of)]
        out = []
        for rec in recs:
            p = rec.patch
            assert rec.updated_at is not None and p.to_version is not None
            applied = _utc(rec.updated_at)
            if not due_for_point(series[p.patch_id],
                                 sum(1 for t in targets if applied < t <= now)):
                continue
            body = p.body
            ref = ChangeRef(change_id=p.patch_id, layer=0,
                            kind="A:fit" if isinstance(body, FitPatchOp) else "A:set_param",
                            applied_at=applied,
                            method_version=body.model_version if isinstance(body, FitPatchOp)
                            else None, signal_kinds=())
            out.append((p.patch_id, outcome_a(ref, data.events, self.base, data.domain_of,
                                              self._infer_at(user_id, p.from_version),
                                              self._infer_at(user_id, p.to_version), now=now)))
        return out

    def _apply_outcomes_a(self, user_id: str, measured: Sequence[tuple[str, OutcomeOut]],
                          now: datetime) -> list[dict]:
        """Step 4 of the module doc for the measured outcomes, inside the caller's transaction
        (with Postgres the series joins it: `store.pinned`). The rollback decision reads the
        stored series, so it is also checked for a patch not measured this run (a rollback that
        failed is retried). A confirmed harmful patch whose paths a newer accepted patch changed is
        not rolled back (that would overwrite the newer fit): skipped, logged, and its series gets
        a NOTE_ROLLBACK_BLOCKED point (once per new harmful point)."""
        patches = self.store.patches(user_id)
        measured_of = dict(measured)
        out = []
        for rec in patches:
            pid = rec.patch.patch_id
            if rec.patch.track != "A" or rec.patch.status != "personal_accepted":
                continue
            o = measured_of.get(pid)
            appended = False
            if o is not None:
                appended = record_point(self.layer2, user_id, o)
                m = self._memory_of(user_id, pid)
                if m is not None and m.entry.long_term != o.verdict:
                    if o.verdict == "helpful":
                        m = bump(m, helpful=1.0, now=now)
                    self.store.put_memory(replace(m, entry=m.entry.model_copy(
                        update={"long_term": o.verdict})))
            pts = self.layer2.outcome_points(user_id, pid)
            if o is None and not confirmed_harmful(pts):
                continue
            last = o if o is not None else pts[-1]
            roll = confirmed_harmful(pts)
            note = last.note
            if roll:
                newer = self._newer_overlap(rec, patches)
                if newer:
                    roll, note = False, NOTE_ROLLBACK_BLOCKED
                    log.info("patch %s/%s confirmed harmful; not rolled back: newer patch(es) %s "
                             "changed the same paths", user_id, pid, ", ".join(newer))
                    add_final_point(self.layer2, user_id, pid, NOTE_ROLLBACK_BLOCKED, now)
                else:                              # the undo path; it bumps harmful
                    self._rollback_a(user_id, rec, now, long_term="harmful", by_user=False)
                    note = A_ROLLBACK_REASON
            out.append({"change_id": pid, "verdict": last.verdict,
                        "n": max((d.n_after for d in last.deltas), default=0), "point": appended,
                        "rolled_back": roll, "note": note})
        return out

    def _newer_overlap(self, rec: PatchRecord, patches: Sequence[PatchRecord]) -> list[str]:
        """Ids of the later accepted track-A patches (higher to_version) that set a path of `rec`:
        rolling `rec` back would overwrite their values, so it is refused while they are applied."""
        p = rec.patch
        if p.track != "A" or p.to_version is None:
            return []
        paths = set(self._a_values(p))
        return [q.patch.patch_id for q in patches
                if q.patch.track == "A" and q.patch.status == "personal_accepted"
                and q.patch.patch_id != p.patch_id and q.patch.to_version is not None
                and q.patch.to_version > p.to_version and paths & set(self._a_values(q.patch))]

    def _rollback_a(self, user_id: str, rec: PatchRecord, now: datetime,
                    long_term: Verdict | None = None, *, by_user: bool) -> PatchRecord:
        """Undo an accepted track-A patch: only its paths go back (later changes to other paths
        stay). Refused (ValueError) while a later accepted patch changed one of its paths
        (`_newer_overlap`): undo that one first. Patch Memory gets rolled_back + harmful; the
        series gets a final point saying who rolled it back (`by_user`). The caller holds the
        transaction."""
        p = rec.patch
        if p.status != "personal_accepted":
            raise ValueError(f"patch {p.patch_id} is {p.status}, only personal_accepted can be undone")
        newer = self._newer_overlap(rec, self.store.patches(user_id))
        if newer:
            raise ValueError(f"patch {p.patch_id} cannot be undone: the newer change "
                             f"{', '.join(newer)} re-estimated the same values; undo it first")
        before = self._infer_at(user_id, p.from_version)
        restored = registry.infer_for(self.store, user_id)
        for path in self._a_values(p):
            restored = ip.set_(restored, path, ip.get(before, path))
        registry.commit(self.store, user_id, "A", infer=restored, patch_id=None, now=now)
        out = replace(rec, patch=p.model_copy(update={"status": "rolled_back"}), updated_at=now)
        self.store.put_patch(out)
        m = self._memory_of(user_id, p.patch_id)
        if m is None:                           # accepted patches always have one; be safe
            m = self._new_memory(user_id, p, status="rolled_back",
                                 gate=GateResult(patch_id=p.patch_id, channel="A", passed=True),
                                 domain=None, old=before, n_outcomes=0, now=now)
        if long_term is not None:
            m = replace(m, entry=m.entry.model_copy(update={"long_term": long_term}))
        self.store.put_memory(bump(self._with_status(m, "rolled_back"), harmful=1.0, now=now))
        add_final_point(self.layer2, user_id, p.patch_id,
                        NOTE_USER_UNDO if by_user else NOTE_AUTO_ROLLBACK, now)
        return out

    # ---- helpers ----

    @contextmanager
    def _tx(self) -> Iterator[None]:
        tx = getattr(self.store, "transaction", None)
        if tx is None:
            yield
        else:
            with tx():
                yield

    def _open_candidate(self, user_id: str) -> PatchRecord | None:
        cands = [r for r in self.store.patches(user_id)
                 if r.patch.track == "A" and r.patch.status == "candidate"]
        return cands[-1] if cands else None

    def _accepted_today(self, user_id: str, now: datetime) -> int:
        day = now.date()
        return sum(1 for r in self.store.patches(user_id)
                   if r.patch.track == "A" and r.patch.status == "personal_accepted"
                   and r.updated_at is not None and _utc(r.updated_at).date() == day)

    @staticmethod
    def _infer_of(infer: InferParams | None) -> InferParams:
        if infer is None:                        # PolicyVersion's validator makes this unreachable
            raise ValueError("track A version without infer")
        return infer

    def _infer_at(self, user_id: str, version: int) -> InferParams:
        for v in self.store.versions(user_id, "A"):
            if v.version == version:
                return self._infer_of(v.infer)
        if version == 1:                         # the implicit v1
            return InferParams()
        raise ValueError(f"{user_id}/A: no version {version}")

    @staticmethod
    def _a_values(p: Patch) -> dict[str, float]:
        """path → value of a track-A patch: a fit (A1) or a pre-A1 set_param body. Anything else
        (a track-B body) is refused: track-A code never touches it."""
        if isinstance(p.body, FitPatchOp):
            return dict(p.body.values)
        if isinstance(p.body, ParamPatchOp) and p.body.track == "A":
            return {p.body.path: p.body.value}
        raise TypeError(f"track A patch {p.patch_id} must carry a fit or a set_param body")

    @staticmethod
    def _apply(infer: InferParams, values: Mapping[str, float]) -> InferParams:
        for path, value in values.items():
            infer = ip.set_(infer, path, value)
        return infer

    @staticmethod
    def _label(path: str) -> str:
        return ip.PATH_LABEL[path]

    def _domain_of_patch(self, p: Patch, data: UserData) -> str | None:
        """A pre-A1 patch's cluster domain; a fit spans every domain (None)."""
        if isinstance(p.body, FitPatchOp):
            return None
        return self._domain_of_signals(p.localization.signals, data)

    @staticmethod
    def _domain_of_signals(signals: Sequence[str], data: UserData) -> str | None:
        """The cluster's domain, recovered from its outcome event ids (a cluster is per domain)."""
        ids = set(signals)
        domains = {data.domain_of.get(split_cell(e.observation.cell)[0], OTHER_DOMAIN)
                   for e in data.events if isinstance(e, Observe) and e.event_id in ids}
        return domains.pop() if len(domains) == 1 else None

    def _memory_of(self, user_id: str, patch_id: str) -> MemoryRecord | None:
        return next((m for m in self.store.memory(user_id) if m.entry.patch_id == patch_id), None)

    @staticmethod
    def _with_status(m: MemoryRecord, status: PatchStatus) -> MemoryRecord:
        return replace(m, entry=m.entry.model_copy(update={"status": status}))

    def _new_memory(self, user_id: str, p: Patch, *, status: PatchStatus, gate: GateResult,
                    domain: str | None, old: InferParams, n_outcomes: int, now: datetime,
                    delta: float | None = None) -> MemoryRecord:
        """`old` = θ the patch was gated against. A fit row: path "fit", direction 0, from/to None,
        complexity = its paths; a pre-A1 set_param row: its path, sign, from → to (clamped), 1."""
        values = self._a_values(p)
        if isinstance(p.body, FitPatchOp):
            path, direction, frm, to = FIT_PATH, 0, None, None
        else:
            ((path, value),) = values.items()
            frm, to = ip.get(old, path), ip.get(ip.set_(old, path, value), path)
            direction = _sign(to - frm)
        entry = PatchMemoryEntry(patch_id=p.patch_id, track="A",
                                 signature=signature(p.localization.failure, path, domain),
                                 path=path, gate=gate, status=status, delta_utility=delta, at=now)
        return MemoryRecord(user_id=user_id, entry=entry, direction=direction, from_value=frm,
                            to_value=to, counts_at=now, repeat_of_rejected=False,
                            outcomes_at_close=n_outcomes, complexity=len(values))

    def _close(self, user_id: str, p: Patch, *, status: PatchStatus, gate: GateResult,
               domain: str | None, old: InferParams, n_outcomes: int, now: datetime,
               helpful: float = 0.0, harmful: float = 0.0, delta: float | None = None) -> None:
        m = self._new_memory(user_id, p, status=status, gate=gate, domain=domain, old=old,
                             n_outcomes=n_outcomes, now=now, delta=delta)
        old_m = self._memory_of(user_id, p.patch_id)
        if old_m is not None:                    # keep its online counts
            m = replace(m, entry=m.entry.model_copy(update={
                "online_helpful": old_m.entry.online_helpful,
                "online_harmful": old_m.entry.online_harmful,
                "long_term": old_m.entry.long_term}), counts_at=old_m.counts_at)
        self.store.put_memory(bump(m, helpful=helpful, harmful=harmful, now=now))

    def _change_a(self, user_id: str, rec: PatchRecord, m: MemoryRecord | None,
                  data: UserData, *, points: Sequence[OutcomeOut] = (),
                  held: bool = False) -> ChangeOut:
        """`points` = its outcome series; `held` = a newer accepted patch changed one of its
        paths (`_newer_overlap`), so it cannot be undone now."""
        p = rec.patch
        before = after = ""
        if m is not None and m.entry.gate.utility_before is not None and m.entry.gate.utility_after is not None:
            before = (f"Confirmation prediction error {-m.entry.gate.utility_before:.2f}" if current_language() == "en"
                      else f"确认题预测误差 {-m.entry.gate.utility_before:.2f}")
            after = f"{-m.entry.gate.utility_after:.2f}"
        reason = p.localization.reason
        if isinstance(p.body, FitPatchOp):
            start = self._infer_at(user_id, p.from_version)
            moves = [(path, v > ip.get(start, path)) for path, v in p.body.values.items()]
            summary = _a_phrase(moves)
        else:
            ((path, value),) = self._a_values(p).items()
            if m is not None and m.from_value is not None:
                frm = m.from_value
            else:
                frm = ip.get(self._infer_at(user_id, p.from_version), path)
            to = m.to_value if m is not None and m.to_value is not None else value
            moves = [(path, to > frm)]
            summary = _a_phrase(moves)
            domain = self._domain_of_signals(p.localization.signals, data)
            if domain is not None and domain in data.domain_label:
                reason = reason.replace(f"领域 {domain} 中", f"领域「{data.domain_label[domain]}」中")
        why = _a_why(moves, p.localization.failure)
        return ChangeOut(patch_id=p.patch_id, track="A", status=p.status, summary=summary, why=why,
                         signals=(reason, f"{len(p.localization.signals)} questions" if current_language() == "en" else
                                  f"{len(p.localization.signals)} 道题"), before=before,
                         after=after, at=rec.updated_at or p.created_at,
                         can_undo=p.status == "personal_accepted" and not held)


_TARGET: Mapping[str, str] = {"A": "学习状态评估", "A2": "估计方法"}
_EVER_APPLIED = frozenset({"personal_accepted", "rolled_back"})


def _with_effect(row: ChangeOut, layer: int, target: str, points: Sequence[OutcomeOut], *,
                 n_now: int | None = None, unit: tuple[str, str] = UNIT_A) -> ChangeOut:
    """The row with its layer, target and outcome series (the newest MAX_SHOWN_POINTS points). A
    change never applied (candidate, gate_failed, …) has no effect line; an applied one still
    pending reads 「数据还在收集：之后 n …」 with its cheap count `n_now` (`effect_line`); a
    rolled-back one says who rolled it back."""
    line = ""
    if points and row.status in _EVER_APPLIED:
        line = effect_line(points[-1], rolled_back=row.status == "rolled_back", n_now=n_now,
                           unit=unit)
    elif row.status == "personal_accepted":
        line = effect_line(OutcomeOut(change_id=row.patch_id, at=row.at, verdict="pending"),
                           n_now=n_now, unit=unit)
    return row.model_copy(update={
        "layer": layer, "target": ({"学习状态评估": "Mastery estimation", "估计方法": "Estimation method",
                                     "讲解规则": "Teaching rules", "提议方法": "Proposal method"}.get(target, target)
                                    if current_language() == "en" else target), "effect": tuple(points[-MAX_SHOWN_POINTS:]),
        "outcome": points[-1].verdict if points and row.status in _EVER_APPLIED else "pending",
        "effect_line": line})


def _change_method(c: MethodChange, latest: bool) -> ChangeOut:
    """A layer-2 method change's row (status applied is shown as personal_accepted). Undoable only
    when applied and the latest applied change of its kind (`latest`, see `method_registry.latest_applied`)."""
    status: PatchStatus = {"applied": "personal_accepted", "rolled_back": "rolled_back",
                           "awaiting_user": "awaiting_user", "rejected": "rejected"}.get(
        c.status, "gate_failed")                                # type: ignore[assignment]
    return ChangeOut(patch_id=c.change_id, track="A", status=status,
                     summary=(_a2_summary(c.gate["label"]) if current_language() == "en" and c.gate.get("label")
                              else "Updated estimation method" if current_language() == "en" else c.summary),
                     why=tr(_unprefixed(c.why), lang=current_language()), signals=(),
                     at=c.updated_at or c.created_at, can_undo=c.status == "applied" and latest)


def _a2_interval(cmp: Mapping) -> str:
    """The comparison's interval in plain Chinese, at the level it was computed: `select_model`
    tests each of its k candidate changes at ALPHA / k (Bonferroni), so with k = 15 the interval
    is a 99.7 % one, not 95 %."""
    lo, hi = f"{cmp['ci_low']:.3f}", f"{cmp['ci_high']:.3f}"
    alpha, k = cmp.get("alpha"), cmp.get("k")
    if alpha is None:
        return f"区间 {lo}–{hi}"
    level = f"{(1 - alpha) * 100:.1f}".rstrip("0").rstrip(".")
    many = f"同时比较了 {k} 种改法，" if k else ""
    return f"{many}按多重比较校正后的 {level} % 区间 {lo}–{hi}"


def _a2_summary(label: str) -> str:
    """Plain Chinese for an A2 move (a `fit.select.neighbours` label)."""
    verb, _, arg = label.partition(" ")
    if current_language() == "en":
        if verb in ("add", "drop"):
            return ("Estimation now considers: " if verb == "add" else "Estimation keeps this parameter fixed: ") + _A_EN.get(arg, "an estimation parameter")
        if label == "prior ×2":
            return "Estimation gives more weight to population patterns"
        if label == "prior ÷2":
            return "Estimation gives more weight to your own answers"
        if verb == "algorithm":
            return ("Estimation gives more weight to recent performance" if arg == "coord_map_recency@1"
                    else "Estimation gives equal weight to all records")
        raise ValueError(f"unknown A2 move {label!r}")
    if verb == "add":
        return f"估计时多考虑了「{ip.PATH_LABEL[arg]}」这一项"
    if verb == "drop":
        return f"估计时不再考虑「{ip.PATH_LABEL[arg]}」这一项（保持现在的值）"
    if label == "prior ×2":
        return "估计时更相信一般人的规律"
    if label == "prior ÷2":
        return "估计时更相信你自己的作答"
    if verb == "algorithm":
        return ("换成更看重最近表现的估计" if arg == "coord_map_recency@1"
                else "换回同等看待全部记录的估计")
    raise ValueError(f"unknown A2 move {label!r}")
