"""One harness round (design 2026-10-21 §5, §7): from the feedback signals since the last round to a
gated change of the user's harness pack — applied, asked, or refused with a recorded reason.

`run_round` (the round itself is not durable; it runs under the user's RSI cycle lock):
1. Skip. Not due (unless `manual`; with `RsiSettings.harness` off a round is never due; the count
   is the cycle's signal count plus the user's assistant rejections and 👎 ratings since the last round,
   `_due_count`): nothing is recorded. Recorded `skipped` with the reason: no deep tier; no judge for the deep tier's primary
   writer family (`judge.judge_for`, never a same-family judge); the month cap
   (`harness_month_limit_usd`, None = no cap) reached by this month's cost + the estimate (the mean
   cost of the last ESTIMATE_ROUNDS rounds that ran, 0 for the first).
2. `cases.select_cases` on the signals since the last round that decided on its evidence
   (`harness_rsi.CONSUMING`: applied, asked, failed, no_change; a thin, skipped or error round
   leaves its signals for the next round, so thin evidence accumulates; `due` still counts from
   the last round of any status); fewer than MIN_CASES problem cases → `thin`, nothing else runs.
   Assistant cases (harness 3b follow-up 3): with `RoundDeps.sessions`, the user's
   `assistant_rejected` signals since the same moment (`wikiapp.signals.assistant_rejections`, the
   action ledger `RoundDeps.ledger` as their second source) and `assistant_rated` signals (the
   answers' current 👎, `assistant_ratings`; both through `assistant_feedback`) become turn cases
   (`cases.assistant_cases`, checked by `replay_assistant.check_turn` on the round's recording
   gateway) and join the deep ones under one MAX_PROBLEM_CASES cap (`cases.with_assistant`). They
   are among the round's signals from then on (recorded, read by the improver, judged).
3. `improver.run_improver`; decision "none" → `no_change`.
4. Smoke replays: the first SMOKE_CASES problem / anchor cases of the agents the composed diff
   touches (`diff.agents_touched`: deep keys, H7 spec ops and the writer's H1 step → the deep
   cases and anchors; assistant keys and H1.assistant → the turn cases; a mixed diff → both) are
   written once under the composed diff (`a`); the improver's own probes are not reused. The
   current pack writes a case only when the candidate could not (a case neither can write is
   unusable, listed with the reason; for a turn case in plain words, TURN_REPLAY_FAILED, the
   detail in the log; one only the current pack can write is `broken`), and for a diff with a
   cost key (`diff.cost_keys`), whose change line shows what a section costs before vs after.
   Nothing is judged. The other cases are listed in the round's cases as `skipped` (NOT_AFFECTED
   for another agent's, NOT_SMOKED past the first SMOKE_CASES). An assistant case replays through
   `replay_assistant.replay_turn` on the round's recording gateway (`ReplayDeps.assistant`), so
   its spend is the round's. The smoke cases replay at once (`replay.run_all`), and a priced
   case's two packs too (the current pack's replay is the one a failed candidate needs anyway);
   their outcomes are recorded in case order. A deep case writes with the user's model now, for both
   packs: that is what a change will run under, and every old trace stays usable after he switches
   model (a test can pin a writer with `RoundDeps.writer_models`).
5. The gate, deterministic checks before the judge. `gate.check`, the smoke check (complexity =
   `diff.complexity` of the composed diff on the base pack; cited = the composition cites something):
   a broken case or no citation fails it; nothing written is thin. `leak.leaks`: text about one case
   or one item fails it. The rule guard (step 6) runs here too, because it is far cheaper than what
   follows. After a pass of the evidence below, `RoundDeps.redlines` (the evaluation set's P0 cases,
   `harness.evalset_redlines`): any regression fails it, whatever the judge said.
   `RoundDeps.evidence` then (`evidence.evaluate`, the corrected rule of report
   2026-10-28-offline-eval-minimal): k samples per case, canaries, A/A, dev cases, exemplars on
   average, and the held-out cases the improver never saw (`evidence.held_out_split`, taken before
   the improver runs). A change of the assistant alone then goes through `RoundDeps.regression` in
   place of the red lines: real turns graded by code, which outrank the judge either way
   (`evalset_redlines`). Recorded at once as a `harness_gate` row (subject "tool:<cid>"
   for a diff with a forged tool, else "diff"; its ref in `decision.gate.ref`), on its own
   connection before anything applies. thin → `thin`; when it is the THIN_NOTICE_AFTER-th thin round in a
   row on the same finding, its reason is THIN_NOTICE. fail → `failed`; a broken case or a clear
   loss writes an `ineffective` lesson (`harness_rsi.add_experience`, in its evidence). No lesson
   on thin. The protection after an apply is the user's undo (a `harmful` lesson,
   `change_source.undo_change`), `fast_check`'s rollback on new clear failures and `observe`'s
   metric rollback.
6. pass, and the diff has a key that needs the user's approval (`diff.needs_ask`: H3 built-in
   toggles, H9 / H10 / H11, H8 presets), the user's `rsi` level asks (`teach.approval`) or the service
   runs propose-only (`RoundDeps.propose_only`, `anchors.JUDGE_CALIBRATED` is False); a diff
   that is only one forged tool going live, `diff.tool_only`, does neither: it applies, undoable, and
   its row tells the user, `ChangeOut.tool_live`:
   `harness_change` asked + a ledger proposal (kind METHOD_KIND, content change_id "h_…", kind "H",
   the 改动对比 lines of `change_source._line`) in one transaction → `asked`; nothing applies until
   `approve_change`. A diff that must be asked with no ledger to ask through → `failed`
   (NO_LEDGER). Otherwise `_commit` → `applied`.
7. After an applied change the pages of the deep problem cases are marked for a rewrite on their
   next open (`PageMarkedStale`, reason STALE_REASON; the mark ends with the page's next whole-page
   write), only when the change touches the page writer (`_stale_pages`). A change with
   teaching-spec ops also marks every page it left out of date (`wikiapp.rule_stale` through
   `_rule_stale`: the pages written with an edited / removed rule in force, every written page for
   an added rule or a param; also after an approval and a rollback). Before that, the improver's
   proposals are checked at proposal time: `rules.authority.check` (actor rsi, with the decisions
   read when the round starts) and the ban on re-proposing a change the user rejected or undid or
   that was rolled back (`ImproverDeps.banned`, `diff.refused_keys`).
6b. Code patch (harness 3b, Task 10): when the improver asked for one (`propose_patch`, offered
   only with `RoundDeps.patch` and the forge's patch switch on) and the key path ended applied,
   asked, failed or no_change: `forge.patch.round.run_patch` builds it (the forge's coder on the
   round's recording gateway) and evaluates it with a `HarnessPatchGate` on the round's problem
   and anchor cases (the child and the judge on the recording gateway too). A pass leaves it
   `awaiting_approval` in the forge's review flow (the user approves or discards it there); nothing
   lands here. The round's `decision.patch` records the outcome; a fail with a clear loss writes an
   `ineffective` lesson, an evaluation that did not complete (`error`) none. The round's status
   stays the key path's; the patch's spend is in the round's cost (the recording log).
8. `finish_round` with the round's cost.

Applying (`_commit`, both for a new change and an approved one; fail closed): the pack keys must
still hold what the change was checked against and `diff.refuse` must still pass (a forged tool's
opt-in switched on alone, harness 3b follow-up 1, against the forge registry's deep tools read at
that moment: no forge hooks, the tool switch off or the tool gone from the registry refuses it,
TOOL_GONE; nothing is promoted and no H12 value written for it); a forged tool
(an H12.tool op, harness 3b Task 7) is promoted in the forge registry inside the pack transaction
(`forging.promote_for`, gated on the round's `harness_gate` row), and the H12 value written and
stored carries the registry version it got (undo rolls the registry back with it); a refused
promote fails the change (TOOL_NOT_PROMOTED; an approval: TOOL_GONE); the teaching-spec
ops (H7) are committed first through the spec registry (`spec_ops.apply` + `SpecRegistry.commit`,
origin "harness:<round id>"), then the key ops (`store.harness.set_value`, source "improver") and the
change row in one Postgres transaction. The spec lives in the wiki log, so the two cannot share a
transaction: when the pack transaction fails the spec commit is undone (`spec_ops.revert` + an undo
commit). The stored diff carries each key's `before` and the spec version before
(`diff.diff_to_json`), which `change_source.undo_change` needs. A change with an H4 or H10 key
(`diff.cost_keys`) carries `cost_before` / `cost_after`: the mean replay cost of the current pack vs the
composed diff on the smoke cases.

Cost: the sum of `llm_call.cost` over the calls this round made — the improver's, every replay's
and the judge's. Every gateway of the round writes through one `_RecordingLog` over the shared call
log, which keeps each call it appends (once per id); a call served from the log is not new spend
and is not counted."""

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING, Any, Literal

from psycopg_pool import ConnectionPool

from evowiki.agent.trace import NO_TRACE, NotReplayable, TraceSink
from evowiki.deep.contracts import RenderChecker
from evowiki.deep.library import ComponentLibrary
from evowiki.deep.llm import DeepLLM
from evowiki.forge.models import Evidence, ForgeNotFound, PromoteRefused
from evowiki.forge.patch.round import run_patch
from evowiki.harness import forge_entries, skills
from evowiki.harness import gate as gate_mod
from evowiki.harness import judge as judge_mod
from evowiki.harness.anchors import MIN_CASES, SMOKE_CASES, THIN_NOTICE_AFTER
from evowiki.harness.apply import forge_tier
from evowiki.harness.cases import CaseSet, assistant_cases, select_cases, with_assistant
from evowiki.harness.change_source import (
    _cost_note,
    _line,
    candidate_ids,
    clear_marks,
    costed,
    folded,
    load_titles,
    mark_rule_pages,
    plain_names,
    summary,
    text_doc,
)
from evowiki.harness.diff import (
    KeyOp,
    PackDiff,
    agents_touched,
    alone_on,
    complexity,
    cost_keys,
    diff_from_json,
    diff_to_json,
    h12_tool,
    needs_ask,
    refuse,
    refused_keys,
    tool_only,
    unpaired,
)
from evowiki.harness.evidence import EvalCase, EvidenceResult, held_out_split
from evowiki.harness.experience import source_nodes, update_object_digests
from evowiki.harness.fast_check import FastDeps, FastResult, fast_check
from evowiki.harness.forging import ForgeHooks, promote_for
from evowiki.harness.improver import Composition, ImproverDeps, _said, component_infos, run_improver
from evowiki.harness.judge import Comparison, JudgeConfigs, JudgeUnavailable
from evowiki.harness.keys import key_of
from evowiki.harness.leak import CaseWords, added_only, added_text, leaks
from evowiki.harness.observe import ObserveDeps, ObserveResult, observe_changes
from evowiki.harness.pack import HarnessInvalid, HarnessPack, Value
from evowiki.harness.patch_gate import HarnessPatchGate, deep_encoder
from evowiki.harness.patching import PatchHooks, PatchProposal
from evowiki.harness.replay import (
    Case,
    ReplayDeps,
    ReplayResult,
    replay,
    run_all,
)
from evowiki.harness.replay_assistant import AssistantReplayDeps, check_turn, pg_deps
from evowiki.harness.snapshot import SnapshotBoundary
from evowiki.harness.steps import parse_examples, parse_presets
from evowiki.harness.triage import worth_a_round
from evowiki.llm.gateway import RECORD_MAX_ATTEMPTS, Gateway
from evowiki.llm.log import CallStore
from evowiki.llm.models import LLMCall, ModelRef
from evowiki.llm.usage import Account
from evowiki.memory.forget import generation_guard
from evowiki.rules.authority import RuleAuthorityError
from evowiki.rules.decisions import NO_DECISIONS, Decisions
from evowiki.rules.guard import GuardResult
from evowiki.store import forge_patch as patch_store
from evowiki.store import harness as pack_store
from evowiki.store import harness_rsi, pinned
from evowiki.store import trace as store_trace
from evowiki.teach import spec_ops
from evowiki.teach.approval import METHOD_KIND, RSI, Approvals, PendingGone, PendingStale, line
from evowiki.teach.contracts import ParamPatchOp, RsiSettings, Signal, SpecPatchOp, TeachingSpec
from evowiki.teach.cycle import due
from evowiki.teach.render import param_effect
from evowiki.teach.rsi_store import MemoryRecord
from evowiki.teach.spec_ops import PatchError
from evowiki.teach.spec_registry import SpecRegistry
from evowiki.wikiapp.events import PageMarkedStale, WikiEvent
from evowiki.wikiapp.refine_service import DeepService, ReplayFailed
from evowiki.wikiapp.repo import Commit, Repo, WikiConflict
from evowiki.wikiapp.signals import (
    ActionLedgerView,
    assistant_feedback,
    assistant_ratings,
    assistant_rejections,
    rated_answers,
)

if TYPE_CHECKING:
    from evowiki.assistant.session import SessionStore
    from evowiki.harness.evalset_redlines import Regression

log = logging.getLogger("evowiki.harness.round")

PREFIX = "h_"                   # change ids in the change list and the ledger (change_source.PREFIX)
PATCH_PREFIX = "forgep:"         # a code patch's row in the change list (forge.undo.PATCH_PREFIX)
GROUP = "rsi"
ESTIMATE_ROUNDS = 3
STALE_TRIES = 3
STALE_REASON = "harness"

NOT_DUE = "还没到下一轮"
NO_DEEP = "没有可用的写页面模型，这一轮跳过"
SWITCHED_OFF = "「自动改进讲解和助手」关着，这一轮没有运行"
MONTH_CAP = "本月改进的花费已到上限（已花 ${spent:.2f}，这一轮预计 ${est:.2f}，上限 ${limit:.2f}），这一轮跳过"
TOO_FEW = "能用的出问题的段只有 {n} 段，至少要 {min} 段，这一轮不改"
THIN_NOTICE = "这个问题连续两轮证据不足"
MIXED = "参照和候选是不同的写作模型写的，没法比较"
REPLAY_FAILED = "重放没写出这一段：{e}"
TURN_REPLAY_FAILED = "重放没答出这次回答，这一次没法比较"
TURN_MIXED = "参照和候选是不同的模型答的，没法比较"
NOT_AFFECTED = "这次改动不影响这类回答，没有重放"
NOT_SMOKED = "检查只重写前几段，这一段没有重写"
STALE_PACK = "运行设置在这之后又改过了，这次改动没有应用"
STALE_SPEC = "讲解规则在这之后又改过了，这次改动没有应用"
RULE_LOCKED = "这次改动会改到你自己的规则或你删掉过的规则，没有应用"
RULE_BLOCKED = "这次改动和你的讲解规则冲突，没有应用"
LEAKED = "这次改动是针对个别内容写的，不是通用设置：{what}"
REDLINE = "这次改动让红线用例变差了：{what}"
RESTORES = "评测集上修好了 {n} 条用例（{what}），没有新坏的"
NO_CASES = "评测集里没有对应这条反馈的用例"
UNMOVED = "评测集上明显修好 {n} 条，不够说明修好了（p={p:.3f}），也没有新坏的"
NOT_FINISHED = "红线用例没跑完"     # `evalset_redlines`: a run with no verdict
NO_BASELINE = "红线用例没有可比的基线"
NO_REDLINES = "这次没有跑回归用例。"
ERROR = "这一轮出错了（{kind}），没有改动"
APPLIED_ERROR = "改动已经应用（{cid}），但这一轮的记录出错了（{kind}）"
ASKED_ERROR = "改动已经提交等你批准（{cid}），但这一轮的记录出错了（{kind}）"
SPEC_LEFT = ("这一轮出错了（{kind}），运行设置没有改；但讲解规则第 {version} 版没能撤回，"
             "里面还留着这次的讲解规则改动")
NO_RENDER = "没有渲染检查，不能加交互块示例或组件预设"
NO_LEDGER = "这次改动要你批准，但没有地方提交给你，这一轮不改"
PATCH_OFF = "代码修改的开关已经关了，这次代码修改没有做"
PATCH_NO_TIER = "写代码修改选的模型档现在用不了，这次代码修改没有做"
PATCH_ERROR = "做这次代码修改时出错了，没有交给你审"
PATCH_NOT_PROMOTED = "关卡通过了，但这次代码修改没能提交给你审"
PATCH_STEPS = ("applied", "asked", "failed", "no_change")   # key-path ends that run step 6b
TOOL_NOT_PROMOTED = "造好的工具没能上线，这次改动没有应用"
TOOL_GONE = "这个工具已经不能用了"
FORGE_NO_TIER = "造工具选的模型档现在用不了"

Status = Literal["applied", "asked", "thin", "failed", "no_change", "skipped", "error"]


@dataclass(frozen=True)
class HRound:
    round_id: str | None          # None: not due, nothing recorded
    status: Status
    reason: str
    cost: float
    change_id: str | None         # the bare harness_change id (applied / asked)


@dataclass(frozen=True)
class RoundDeps:
    """What a round needs; the service builds it (`service.rsi_runs`).
    `deep`: the deep service whose writer replays (its `llm`: the deep tier, its call log, prices
    and meter; the judge gateway is built on it). `gateway`: the user's metered gateway; the
    improver's calls go through it with Account(group "rsi") → the good tier. `registry`: the
    teaching spec (track B of the Policy Registry), base of the H7 ops and the path they apply
    through. `signals(user, since)` / `signals_since(user, since)`: the round's signals and the
    cheap count `due` compares (the cycle's own count). `judges` / `judge_providers()`: the judge
    configs and their providers (built once per round). `memory(user)`: Patch Memory rows the
    improver reads. `select` / `improve` / `replay` / `mark_stale`: the steps (tests swap them).
    `sessions` / `ledger`: the assistant's session store and action ledger, the sources of the
    `assistant_rejected` signals (module doc, step 2); without `sessions` a round has no
    assistant cases. `assistant_replay(gateway)`: the assistant replay deps over the round's
    recording gateway (default `replay_assistant.pg_deps` on `pool`, traced to `trace`)."""

    pool: ConnectionPool
    repo: Repo
    deep: DeepService | None             # None (no deep tier): every round is skipped (NO_DEEP)
    gateway: Gateway | None              # None (no gateway): the same
    registry: SpecRegistry
    settings: Callable[[str], RsiSettings]
    signals: Callable[[str, datetime | None], Sequence[Signal]]
    signals_since: Callable[[str, datetime | None], int]
    defaults: Mapping[str, Value]
    judges: JudgeConfigs
    judge_providers: Callable[[], Mapping[str, Any]]
    tz: tzinfo
    new_id: Callable[[str], str]
    approvals: Approvals | None = None
    memory: Callable[[str], Sequence[MemoryRecord]] = lambda user_id: ()
    trace: TraceSink = NO_TRACE
    select: Callable[..., CaseSet] = select_cases
    improve: Callable[..., Composition] = run_improver
    replay: Callable[..., ReplayResult] = replay
    # (user, page ids, bare change id, now) -> pages marked; None = `mark_pages` on `repo`
    mark_stale: Callable[[str, Sequence[str], str, datetime], int] | None = None
    # (user, now) -> pages marked: the pages a teaching-spec change left out of date (applied,
    # approved, rolled back); None = `change_source.mark_rule_pages` on `repo` and `pool`
    rule_stale: Callable[[str, datetime], int] | None = None
    # the writer's component library and render checker (H8 presets / examples are render checked)
    library: ComponentLibrary | None = None
    render: RenderChecker | None = None
    # code patches (step 6b); None: the improver is never offered `propose_patch`
    patch: PatchHooks | None = None
    # rule provenance contract §2 / §4.1: (user, diff) → the rule guard's verdict on the rule
    # texts the diff puts in force (H7 adds / edits, H1 / H2 / H8 values; `rules.guard.
    # harness_guard`). block → the round fails, confirm → the change is asked. None: no check.
    guard: Callable[[str, PackDiff], GuardResult] | None = None
    # forged tools (harness 3b, Task 7); None: the improver is never offered `forge_tool`
    forge: ForgeHooks | None = None
    # assistant turns as cases (harness 3b follow-up 3)
    sessions: "SessionStore | None" = None
    ledger: ActionLedgerView | None = None
    assistant_replay: Callable[[Gateway], AssistantReplayDeps] | None = None
    # a deep case's run id -> the writer models it must replay on (`ReplayDeps.writer_models`);
    # None = the user's writer now
    writer_models: Callable[[str], tuple[ModelRef, ...]] | None = None
    # step 5: the evidence over replays (`evidence.evaluate`); None: the smoke check alone (tests,
    # and only tests: the service always passes it)
    evidence: Callable[..., EvidenceResult] | None = None
    # a passing change is only proposed, whatever the user's level says (`anchors.JUDGE_CALIBRATED`)
    propose_only: bool = False
    # his rules, memories and decisions in force (`leak.CaseWords.items`); none: only the cases' words
    memory_items: Callable[[str], Sequence[str]] = lambda user_id: ()
    # step 5: (user, diff, pack, spec) -> the evaluation set's P0 cases this diff makes fail; None:
    # not run (no evaluation set here), which the proposal says
    redlines: Callable[[str, PackDiff, HarnessPack, TeachingSpec], Sequence[str]] | None = None
    # step 5, for a change of the assistant alone: the candidate against the current pack on the
    # evaluation set, real turns graded by code (`evalset_redlines.Regression`); in place of `redlines`
    regression: Callable[[str, PackDiff, HarnessPack, TeachingSpec], "Regression"] | None = None
    # the words of a round's cases; None = page titles from `repo`, `memory_items`, signal texts
    words_of: Callable[[str, CaseSet, Sequence[Signal]], CaseWords] | None = None
    # the evaluation set under the base pack + a diff (`evalset_redlines.harness_evals`); None: no `run_evals`
    evals: Callable[..., Mapping[str, Any] | None] | None = None
    # the evaluation set's cases for a round's feedback (`evalset_redlines.harness_relevant`); () = none
    # speaks to it, and the task layer then says so; None: not wired (every case)
    relevant: Callable[[Any], Sequence[str]] | None = None
    # (candidate id, main checkout, pack, case ids) -> `Regression` of the patch's code; None: replays only
    patch_regression: Callable[..., Any] | None = None
    # the improver's own model (`runtime.usage.runtime_improver_ref`); None = the user's good tier
    improver: ModelRef | None = None


class _RecordingLog:
    """The shared call log, keeping every call appended through it (this round's new calls).
    Replays run in parallel append from several threads: `made` changes under a lock."""

    def __init__(self, inner: CallStore):
        self._inner = inner
        self._made: dict[str, LLMCall] = {}
        self._lock = threading.Lock()

    @property
    def made(self) -> dict[str, LLMCall]:
        with self._lock:
            return dict(self._made)

    def get(self, input_hash: str) -> LLMCall | None:
        return self._inner.get(input_hash)

    def taken(self, call_id: str) -> bool:
        return self._inner.taken(call_id)

    def refuse(self, call_id: str) -> None:
        self._inner.refuse(call_id)

    def append(self, call: LLMCall) -> None:
        self._inner.append(call)
        with self._lock:
            self._made[call.id] = call

    def calls(self) -> list[LLMCall]:
        return self._inner.calls()

    def __len__(self) -> int:
        return len(self._inner)

    def cost(self) -> float:
        return round(sum(c.cost or 0.0 for c in self.made.values()), 6)


class SpecLeftOver(RuntimeError):
    """The pack writes failed after the spec commit and undoing the spec commit failed too:
    teaching-spec `version` still holds the change's H7 ops (raised from the original error)."""

    def __init__(self, version: int):
        super().__init__(f"spec v{version} keeps the H7 ops of a failed harness change")
        self.version = version


class _Skip(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- pure helpers -----------------------------------------------------------------------------

def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _case_json(c: Case) -> dict:
    return {"case_id": c.case_id, "run_id": c.run_id, "page_id": c.page_id,
            "section_id": c.section_id, "signal_ids": list(c.signal_ids), "mode": c.mode,
            **({"page_seq": c.page_seq} if c.page_seq is not None else {}),
            **({"kind": c.kind} if c.kind != "deep" else {})}


def case_agent(cases: Sequence[Case]) -> str:
    """The lesson's `situation.agent` for these cases: "deep" or "assistant" when every case is
    of that kind (or there is none: "deep"), "mixed" when both are there."""
    kinds = {c.kind for c in cases}
    return "mixed" if len(kinds) > 1 else next(iter(kinds), "deep")


def _stale_pages(cs: CaseSet, touched: frozenset[str]) -> list[str]:
    """The pages an applied change marks for a rewrite: those of the deep problem cases, when the
    change touches the page writer (an assistant-only change rewrites no page)."""
    if "deep" not in touched:
        return []
    return [c.page_id for c in cs.problem if c.page_id and c.kind == "deep"]


def _cmp_json(c: Comparison, final: bool) -> dict:
    return {"cmp_id": c.cmp_id, "case_id": c.case_id, "a_id": c.a_id, "b_id": c.b_id,
            "winner": c.winner, "reason": c.reason, "quote": c.quote, "flipped": c.flipped,
            "call_ids": list(c.call_ids), "final": final}


def _before(diff: PackDiff, pack: HarnessPack) -> dict[str, Value | None]:
    return {o.name: pack.values.get(o.name) for o in diff.ops}


def _why(text: str) -> str:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return plain_names(first or "改进者根据这一轮的反馈改的")


def _lines(diff: PackDiff, before: Mapping[str, Value | None],
           costs: tuple[float | None, float | None] = (None, None),
           titles: Mapping[str, str] | None = None,
           defaults: Mapping[str, Value] | None = None) -> list[dict[str, str]]:
    """The 改动对比 of an asked change (`teach.approval.line`): a text key shows the sentences it
    removes and adds against the text in force (`defaults` for a key still at its default; what the
    user approves is the change, not a whole prompt to compare by eye); every other key its
    `change_source._line` (a forged tool's opt-in folded into its tool's line; the cost note on
    H4 / H10 keys)."""
    out = [line("ctx", "运行设置")]
    cost = _cost_note(*costs)
    for o in diff.ops:
        k = key_of(o.name)
        if k is not None and k.kind == "text":
            doc = text_doc(o.name, k.doc, titles)
            was = before.get(o.name)
            if was is None and defaults is not None:
                was = defaults.get(o.name)
            if o.value is None or not isinstance(o.value, str) or not isinstance(was, str):
                if before.get(o.name) is not None:
                    out.append(line("del", f"{doc}：{before[o.name]}"))
                out.append(line("add", f"{doc}：{'默认值' if o.value is None else o.value}"))
                continue
            name = "" if len(diff.ops) == 1 else f"{doc}："     # the row's summary already names one
            out += [line("del", f"{name}{s}") for s in added_text(o.value, was).splitlines()]
            out += [line("add", f"{name}{s}") for s in added_text(was, o.value).splitlines()]
        elif not folded(diff, o.name):
            out.append(line("add", _line(o.name, o.value, before.get(o.name),
                                         cost if costed(o.name) else "", titles)))
    out += [line("add", f"讲解规则：{s.text}") for s in diff.spec_ops
            if isinstance(s, SpecPatchOp) and s.text]
    out += [line("add", f"以后的页面：{param_effect(s.path, int(s.value))}")
            for s in diff.spec_ops if isinstance(s, ParamPatchOp)]
    return out


def _evidence_words(evidence: dict | None) -> str:
    """The comparison the user approves on, in plain words, with what the score does not optimise."""
    if not evidence or "beside" not in evidence:
        return (evidence or {}).get("task_says", "")
    b = evidence["beside"]
    n = b["wins"] + b["losses"] + b["ties"]
    ratio = "" if b.get("len_ratio") is None else f"写出来的长度是现在的 {b['len_ratio']:.1f} 倍。"
    cost = "" if b.get("cost_ratio") is None else f"花费是现在的 {b['cost_ratio']:.1f} 倍。"
    task = evidence.get("task_says", "")
    return f"{task}{'。' if task else ''}重放试写了 {n} 段，{b['wins']} 段更好，{b['losses']} 段更差，其余差不多。{ratio}{cost}"


def ledger_key(user_id: str, change_id: str) -> str:
    """The ledger key of a harness change waiting for the user."""
    return f"harness:{user_id}:{change_id}"


def mark_pages(repo: Repo, new_id: Callable[[str], str], user_id: str, page_ids: Sequence[str],
               change_id: str, now: datetime) -> int:
    """One `PageMarkedStale` per page that exists (job id "h_<change id>", reason STALE_REASON),
    in one commit; retried on a conflicting commit. Returns how many were marked."""
    for attempt in range(STALE_TRIES):
        snap = repo.load(user_id)
        evs = [WikiEvent(event_id=new_id("wev"), ts=now,
                         body=PageMarkedStale(page_id=p, job_id=PREFIX + change_id,
                                              reason=STALE_REASON))
               for p in dict.fromkeys(page_ids) if p in snap.state.pages]
        if not evs:
            return 0
        try:
            repo.commit(snap, Commit(wiki=evs))
            return len(evs)
        except WikiConflict:
            if attempt == STALE_TRIES - 1:
                raise
    return 0                                                            # pragma: no cover


# --- applying ---------------------------------------------------------------------------------

def _commit(user_id: str, origin: str, diff: PackDiff, before: Mapping[str, Value | None],
            spec_version: int | None, deps: RoundDeps, *, why: str, now: datetime,
            write: Callable[[Any, int | None, PackDiff], str],
            on_accept: Callable[[], None] | None = None,
            promote: Callable[[Any, PackDiff], PackDiff] | None = None) -> str:
    """Applies `diff` (module doc) and returns what `write(conn, pack version, applied diff)`
    returns; `write` records the change row inside the pack transaction. `promote(conn, diff)`
    (inside it, before any key is written): the diff as applied (a forged tool promoted, its H12
    value with the registry version), or _Skip. _Skip(STALE_…) when the pack or the spec moved
    since `before` / `spec_version` or the diff is refused now (nothing applied)."""
    with deps.pool.connection() as conn:
        _check(conn, user_id, diff, before, deps)
    committed: tuple[TeachingSpec, TeachingSpec] | None = None      # (before, after)
    if diff.spec_ops:
        cur = deps.registry.current(user_id)
        if cur.spec is None or cur.version != spec_version:
            raise _Skip(STALE_SPEC)
        new = cur.spec
        try:
            for op in diff.spec_ops:
                new = spec_ops.apply(new, op, patch_id=origin)
        except PatchError as e:
            raise _Skip(STALE_SPEC) from e
        try:
            out = deps.registry.commit(user_id, new, patch_id=origin, at=now, actor="rsi")
        except RuleAuthorityError as e:
            raise _Skip(RULE_LOCKED) from e
        assert out.spec is not None
        committed = (cur.spec, out.spec)
    try:
        with pinned.transaction(deps.pool), pinned.connection(deps.pool) as conn:
            pack_store.lock_key(conn, user_id)
            _check(conn, user_id, diff, before, deps)
            applied = promote(conn, diff) if promote is not None else diff
            if on_accept is not None:
                on_accept()
            versions = [_write_op(conn, user_id, o, deps, why=why, now=now) for o in applied.ops]
            return write(conn, max((v for v in versions if v is not None), default=None), applied)
    except BaseException as exc:
        if committed is not None:
            left = _undo_spec(user_id, origin, diff, committed, deps, now)
            if left is not None:
                raise SpecLeftOver(left) from exc
        raise


def _write_op(conn, user_id: str, o: KeyOp, deps: RoundDeps, *, why: str,
              now: datetime) -> int | None:
    """One key op of an applied change. H8 presets / examples go through harness.skills (render
    checked, the 3a rule that a stored preset or example always renders); a None value (back to the
    default) and every other key go straight to the store."""
    if o.value is not None and (o.name == "H8.deep.evoui_examples"
                                or (o.name.startswith("H8.component.") and o.name.endswith(".presets"))):
        if deps.render is None or (o.name != "H8.deep.evoui_examples" and deps.library is None):
            raise _Skip(NO_RENDER)
        try:
            if o.name == "H8.deep.evoui_examples":
                return skills.put_examples(conn, user_id, parse_examples(o.value), checker=deps.render,
                                           source="improver", why=why, now=now)
            comp = o.name.split(".")[2]
            return skills.put_presets(conn, user_id, comp, parse_presets(o.value),
                                      library=deps.library, checker=deps.render,
                                      source="improver", why=why, now=now)
        except HarnessInvalid as e:
            raise _Skip(str(e)) from e
    return pack_store.set_value(conn, user_id, o.name, o.value, source="improver", why=why, now=now)


def _promoter(user_id: str, round_id: str, gate_ref: str | None, deps: RoundDeps, *, why: str,
              now: datetime, reason: str) -> Callable[[Any, PackDiff], PackDiff]:
    """`_commit`'s promote step: a diff with a forged tool promotes it (`forging.promote_for`, on
    the pack transaction's connection); a refused promote, no gate record, no forge hooks or the
    forge's tool switch now off for the user (read on that connection) is _Skip(reason): the
    round's apply fails, an approval settles the change rejected (PendingStale)."""
    def promote(conn, diff: PackDiff) -> PackDiff:
        if h12_tool(diff) is None:
            return diff
        if not gate_ref or deps.forge is None or not deps.forge.enabled(conn, user_id):
            raise _Skip(reason)
        try:
            return promote_for(conn, user_id, round_id, diff, gate_ref, why=why, now=now)
        except (PromoteRefused, ForgeNotFound) as e:
            log.warning("harness round %s of %s: the forged tool was not promoted: %s",
                        round_id, user_id, e)
            raise _Skip(reason) from e
    return promote


def _registered(conn, user_id: str, diff: PackDiff, deps: RoundDeps) -> frozenset[str]:
    """The deep forged tools `diff` switches on alone (`diff.alone_on`) that the user's registry
    has now, read names-first (`forge_entries.entries`) and loading only those entries. Empty
    without forge hooks or with the forge's tool switch off, so such an opt-in is then refused
    (fail closed). An entry of a touched tool that does not load: _Skip(its plain reason) (logged
    by forge_entries; never read as "not registered"). A failed read of the registry heads
    raises: every apply / approval fails with it."""
    touched = sorted({tool for _, agent, tool in alone_on(diff) if agent == "deep"})
    if not touched:
        return frozenset()
    if deps.forge is None or not deps.forge.enabled(conn, user_id):
        return frozenset()
    have = forge_entries.entries(conn, user_id)
    out = set()
    for tool in touched:
        if tool not in have:
            continue
        try:
            forge_entries.load(conn, user_id, "deep", tool, have[tool])
        except forge_entries.EntryBroken as e:
            raise _Skip(e.reason) from e
        out.add(tool)
    return frozenset(out)


def _check(conn, user_id: str, diff: PackDiff, before: Mapping[str, Value | None],
           deps: RoundDeps) -> None:
    pack = pack_store.load_pack(conn, user_id)
    if any(pack.values.get(o.name) != before.get(o.name) for o in diff.ops):
        raise _Skip(STALE_PACK)
    if not diff.ops:
        return
    registered = _registered(conn, user_id, diff, deps)
    if refuse(PackDiff(diff.ops), pack, pack_store.source_of(conn, user_id),
              defaults=deps.defaults, registered=registered) is not None:
        raise _Skip(TOOL_GONE if unpaired(diff, registered) else STALE_PACK)


def _undo_spec(user_id: str, origin: str, diff: PackDiff,
               committed: tuple[TeachingSpec, TeachingSpec], deps: RoundDeps,
               now: datetime) -> int | None:
    """The pack writes failed after the spec commit: commit the spec back (an undo version).
    None when undone; else the spec version that still holds the ops (logged critical)."""
    old, spec = committed
    try:
        for op in reversed(diff.spec_ops):
            spec = spec_ops.revert(spec, old, op)
        deps.registry.commit(user_id, spec, patch_id=origin, at=now, undo=True, actor="rsi")
        return None
    except Exception:  # the original error is raised by the caller; this one must be seen too
        log.critical("harness change %s of %s: pack write failed and the spec commit could not "
                     "be undone; teaching spec v%d keeps its H7 ops", origin, user_id,
                     committed[1].version, exc_info=True)
        return committed[1].version


def approve_change(user_id: str, change_id: str, deps: RoundDeps, *, now: datetime,
                   on_accept: Callable[[], None]) -> None:
    """The user approved the asked change `change_id` (bare id): applied through the same path as
    a new change (`_commit`), `on_accept()` (the ledger's effect) inside the pack transaction that
    settles it applied; then the problem pages are marked (and, for teaching-spec ops, every page
    they left out of date: `_rule_stale`). PendingGone when it is not waiting; when the rule guard
    (`deps.guard`, run again: the user's rules may have changed while it waited) blocks it, or the
    pack or spec moved since it was proposed, it is settled `rejected` and PendingStale raised."""
    with deps.pool.connection() as conn:
        row = harness_rsi.get_change(conn, user_id, change_id)
        rnd = None if row is None else harness_rsi.get_round(conn, user_id, row.round_id)
    if row is None or row.status != "asked":
        raise PendingGone(f"harness change {change_id} is not waiting for the user")
    diff, before, spec_version = diff_from_json(row.diff)
    why = row.reason or _why("")
    gate = ((rnd.decision or {}).get("gate") or {}) if rnd is not None else {}

    def write(conn, version: int | None, applied: PackDiff) -> str:
        # the stored diff is immutable (only status / version settle): an approved tool's
        # registry version is in the pack's H12 value, where undo reads it
        harness_rsi.settle_change(conn, change_id, "applied", version=version, now=now)
        return change_id
    if deps.guard is not None:
        # the user may have added a rule it now conflicts with while it waited; confirm / pass
        # proceed: approving it is his confirmation
        rg = deps.guard(user_id, added_only(diff, before, deps.defaults))
        if rg.outcome == "block":
            with deps.pool.connection() as conn:
                harness_rsi.settle_change(conn, change_id, "rejected", now=now)
            raise PendingStale(f"{RULE_BLOCKED}：{rg.reason}")
    try:
        _commit(user_id, f"harness:{row.round_id}", diff, before, spec_version, deps,
                why=why, now=now, write=write, on_accept=on_accept,
                promote=_promoter(user_id, row.round_id, gate.get("ref"), deps, why=why,
                                  now=now, reason=TOOL_GONE))
    except _Skip as e:
        with deps.pool.connection() as conn:
            harness_rsi.settle_change(conn, change_id, "rejected", now=now)
        raise PendingStale(e.reason) from e
    pages = ([c["page_id"] for c in ((rnd.cases or {}).get("problem") or [])
              if c.get("page_id") and c.get("kind", "deep") == "deep"]
             if rnd and "deep" in agents_touched(diff) else [])
    _mark(user_id, pages, change_id, deps, now)
    if diff.spec_ops:
        _rule_stale(user_id, deps, now)


def reject_change(user_id: str, change_id: str, deps: RoundDeps, *, now: datetime) -> None:
    """The user rejected the asked change: settled `rejected`, never applied. PendingGone when it
    is not waiting."""
    with deps.pool.connection() as conn:
        row = harness_rsi.get_change(conn, user_id, change_id)
        if row is None or row.status != "asked":
            raise PendingGone(f"harness change {change_id} is not waiting for the user")
        harness_rsi.settle_change(conn, change_id, "rejected", now=now)


def _rule_stale(user_id: str, deps: RoundDeps, now: datetime) -> None:
    """The pages a teaching-spec change left out of date, marked (`change_source.mark_rule_pages`);
    like `_mark`, a failure is logged and never undoes the change."""
    try:
        if deps.rule_stale is not None:
            deps.rule_stale(user_id, now)
        elif deps.repo is not None:
            mark_rule_pages(deps.repo, deps.pool, deps.new_id, user_id, now)
    except Exception:  # broad on purpose: the change is in force; the pages are rewritten later
        log.exception("teaching-rule change of %s: its pages were not marked", user_id)


def _mark(user_id: str, pages: Sequence[str], change_id: str, deps: RoundDeps,
          now: datetime) -> None:
    """Marking must not undo an applied change: a failure is logged (as `rsi_runs._mark_stale`)."""
    if not pages:
        return
    mark = deps.mark_stale or (lambda u, p, c, t: mark_pages(deps.repo, deps.new_id, u, p, c, t))
    try:
        mark(user_id, pages, change_id, now)
    except Exception:  # broad on purpose: the change is applied; the pages are rewritten later
        log.exception("harness change %s of %s applied, but its pages were not marked",
                      change_id, user_id)


# --- one round --------------------------------------------------------------------------------

def run_round(user_id: str, deps: RoundDeps, *, now: datetime, manual: bool = False) -> HRound:
    """One round (module doc). Never raises: an error ends the round `error` with the reason
    (logged; a change already applied is named in it), and if even that record fails the HRound
    still says `error`. A cycle the Bot started (trigger "bot") passes manual=False: it keeps the
    due rule (one round per 10 signals, the user's decision)."""
    settings = deps.settings(user_id)
    with deps.pool.connection() as conn:
        last = harness_rsi.last_round(conn, user_id)
        consumed = harness_rsi.last_consuming_round(conn, user_id)
    last_at = last.started_at if last is not None else None
    # due counts new signals since the last round of any status (a thin round does not re-trigger
    # on every new signal); the round reads the signals since the last round that decided on its
    # evidence (harness_rsi.CONSUMING), so a thin / skipped / error round leaves them for the next
    if not manual and (not settings.harness or due(
            settings, last_at, _due_count(user_id, deps, last_at), now) is None):
        return HRound(None, "skipped", NOT_DUE, 0.0, None)
    with generation_guard(deps.pool, user_id):
        r = _Round(user_id, deps, now)
        try:
            r.skip_checks(settings)
            return r.run(consumed.started_at if consumed is not None else None)
        except (_Skip, JudgeUnavailable) as e:
            try:
                return r.end("skipped", e.reason)
            except Exception:  # broad on purpose: the no-raise contract, as the error path below
                log.exception("harness round %s of %s: its skip was not recorded", r.round_id,
                              user_id)
                return HRound(r.round_id, "error", e.reason, r.cost(), None)
        except Exception as e:  # broad on purpose: recorded on the round (fail closed), then logged
            log.exception("harness round of %s failed", user_id)
            kind = type(e).__name__
            if r.change_id is not None:
                msg = ASKED_ERROR if r.asked else APPLIED_ERROR
                reason = msg.format(cid=PREFIX + r.change_id, kind=kind)
            elif isinstance(e, SpecLeftOver):
                cause = type(e.__cause__).__name__ if e.__cause__ is not None else kind
                reason = SPEC_LEFT.format(kind=cause, version=e.version)
                r.rec.setdefault("decision", {})["spec_left_version"] = e.version
            else:
                reason = ERROR.format(kind=kind)
            try:
                return r.end("error", reason, change_id=r.change_id)
            except Exception:  # broad on purpose: the no-raise contract; the log has both errors
                log.exception("harness round %s of %s: its error was not recorded", r.round_id,
                              user_id)
                return HRound(r.round_id, "error", reason, r.cost(), r.change_id)


def _due_count(user_id: str, deps: RoundDeps, since: datetime | None) -> int:
    """The feedback `due` compares: the cycle's signal count since `since` plus the user's
    assistant rejections and 👎 ratings since then (the same sources the round reads: the
    session store and the action ledger, bounded by its LEDGER_SCAN), none without a session
    store. A 👎 on a turn that also has a rejection in that span counts once, as its case does."""
    n = deps.signals_since(user_id, since)
    if deps.sessions is not None:
        n += assistant_feedback_count(deps.sessions, deps.ledger, user_id, since)
    return n


def assistant_feedback_count(sessions: "SessionStore", ledger: ActionLedgerView | None,
                             user_id: str, since: datetime | None) -> int:
    """The assistant part of `_due_count` (also the RSI tick's pre-check and the cycle's due
    rule with the harness on): rejections plus 👎 on turns without a rejection."""
    rejected = assistant_rejections(sessions, ledger, user_id, since)
    turns = {run for _, run in rejected}
    return len(rejected) + sum(run not in turns for _, run in
                               assistant_ratings(sessions, user_id, since))


def worth_review(sessions: "SessionStore", ledger: ActionLedgerView | None, gateway: Gateway,
                 user_id: str, since: datetime | None, *,
                 page_signals: Sequence[Signal] = (), strict: bool = False) -> bool:
    """Triage eligible page corrections and assistant feedback since the last round.
    The caller selects opted-in page signals; their IDs and displayed versions stay with the words."""
    remarks = []
    source_nodes_ = set()
    seen = set()
    for signal in page_signals:
        if (signal.kind not in ("page_feedback", "user_complaint") or not signal.text.strip()
                or signal.signal_id in seen or (since is not None and signal.at < since)):
            continue
        seen.add(signal.signal_id)
        source_nodes_.add(f'signal:{signal.signal_id}')
        remarks.append(f"[{signal.kind} signal={signal.signal_id} page={signal.page_id} "
                       f"section={signal.section_id} artifact={signal.target_artifact_id} "
                       f"chips={','.join(signal.chips)}] "
                       f"{signal.text}")
    for s, run in assistant_feedback(sessions, ledger, user_id, since):
        source_nodes_.add(f'signal:{s.signal_id}')
        _, session, turn = (run.split(":", 2) + ["", ""])[:3]
        source_nodes_.update((f'session:{session}', f'turn:{session}/{turn}'))
        said = next((str(e.payload.get("text", "")) for e in sessions.entries(session)
                     if e.type == "user" and e.payload.get("message_id") == turn), "")
        remarks.append(f"{said} {s.text.rsplit('补充：', 1)[-1]}".strip())
    return worth_a_round(remarks, gateway, user_id, strict=strict, source_nodes=sorted(source_nodes_))


class _Round:
    def __init__(self, user_id: str, deps: RoundDeps, now: datetime):
        self.user_id, self.deps, self.now = user_id, deps, now
        self.round_id: str | None = None
        self.change_id: str | None = None          # set once a change is applied or asked
        self.asked = False                         # the change waits for the user's approval
        self.account = Account(user_id=user_id, group=GROUP)
        llm = deps.deep.llm if deps.deep is not None else None
        self.log: _RecordingLog | None = _RecordingLog(llm.log) if llm is not None else None
        self.rec: dict[str, Any] = {}            # the result columns written by finish_round
        self.comparisons: dict[str, dict] = {}
        self._judge_gws: dict[str, Gateway] = {}
        self._providers: Mapping[str, Any] | None = None
        self._patch: _PatchCtx | None = None       # the improver's patch request (step 6b)
        self.feedback: Any = None                  # `_feedback`, set before the improver runs

    # --- bookkeeping ---
    def start(self, finding: str | None) -> str:
        with self.deps.pool.connection() as conn:
            self.round_id = harness_rsi.start_round(conn, self.user_id, finding=finding,
                                                    now=self.now)
        return self.round_id

    def cost(self) -> float:
        return self.log.cost() if self.log is not None else 0.0

    def end(self, status: Status, reason: str, *, change_id: str | None = None,
            lesson: dict | None = None) -> HRound:
        if self.round_id is None:
            self.start(None)
        assert self.round_id is not None
        lessons = [] if lesson is None else [lesson]
        if status in PATCH_STEPS and self._patch is not None:
            ctx, self._patch = self._patch, None
            patch_lesson = self._run_patch(ctx)
            if patch_lesson is not None:
                lessons.append(patch_lesson)
        cost = self.cost()
        with self.deps.pool.connection() as conn, conn.transaction():
            harness_rsi.finish_round(
                conn, self.round_id, status, reason=reason, cost=cost,
                comparisons=list(self.comparisons.values()) or None, now=self.now, **self.rec)
            for les in lessons:
                harness_rsi.add_experience(conn, self.user_id, **les, now=self.now)
        return HRound(self.round_id, status, reason, cost, change_id)

    # --- step 1 ---
    def skip_checks(self, settings: RsiSettings) -> None:
        llm = self.deps.deep.llm if self.deps.deep is not None else None
        if llm is None or self.deps.gateway is None:
            raise _Skip(NO_DEEP)
        judge_mod.judge_for(llm.config.generator.provider, self.deps.judges)   # JudgeUnavailable
        limit = settings.harness_month_limit_usd
        if limit is None:
            return
        if limit == 0.0 and not settings.harness:      # the switch's off values (RSI_SWITCH_OFF)
            raise _Skip(SWITCHED_OFF)
        with self.deps.pool.connection() as conn:
            spent = harness_rsi.month_cost(conn, self.user_id, self.now, self.deps.tz)
            # rounds that spent something: a thin or no-change round costs ~0 and says nothing
            # about what a full round costs
            ran = [r.cost for r in harness_rsi.rounds(conn, self.user_id, 50)
                   if r.status not in ("running", "skipped") and r.cost]
        est = _mean(ran[:ESTIMATE_ROUNDS]) or 0.0
        if spent + est >= limit:
            raise _Skip(MONTH_CAP.format(spent=spent, est=est, limit=limit))

    # --- the deep service and judges over the recording log ---
    def recording(self) -> _RecordingLog:
        assert self.log is not None              # skip_checks: there is a deep tier
        return self.log

    def deep(self) -> DeepService:
        assert self.deps.deep is not None        # skip_checks
        return self.deps.deep

    def llm(self) -> DeepLLM:
        llm = self.deep().llm
        assert llm is not None                   # skip_checks
        return replace(llm, log=self.recording())

    def _forge_binding(self, pack: HarnessPack) -> tuple[Gateway, Account]:
        """The gateway + account `forge_tool`'s calls go through, on the round's recording log
        (their spend is the round's): the pack's H10 forge.code tier (`apply.forge_tier`), the
        good tier by default (the round's gateway, account group "rsi" pinned to good); "deep" is
        the deep tier's own writer. ValueError (plain) for a tier this deep tier has no config
        for."""
        tier = forge_tier(pack, "tool") or "good"
        if tier == "good":
            assert self.deps.gateway is not None             # skip_checks
            return (self.deps.gateway.with_log(self.recording()),
                    Account(user_id=self.user_id, group=GROUP, tier_pin="good"))
        if tier != "deep":
            raise ValueError(FORGE_NO_TIER)
        gw, _ = self.llm().gateway(None)
        return gw, self.account

    def assistant_deps(self) -> AssistantReplayDeps:
        """The assistant replay deps over the round's recording gateway (their spend is the
        round's; module doc, step 4)."""
        assert self.deps.gateway is not None             # skip_checks
        gw = self.deps.gateway.with_log(self.recording())
        if self.deps.assistant_replay is not None:
            return self.deps.assistant_replay(gw)
        return pg_deps(self.deps.pool, gw, trace=self.deps.trace)

    def rejections(self, since: datetime | None) -> tuple[tuple[Signal, str], ...]:
        """The user's assistant rejections and 👎 ratings since `since` with their turns' run ids
        (`assistant_feedback`; none without a session store)."""
        if self.deps.sessions is None:
            return ()
        return tuple(assistant_feedback(self.deps.sessions, self.deps.ledger, self.user_id,
                                        since))

    def judge_gw(self, ref: ModelRef) -> Gateway:
        key = f"{ref.provider}|{ref.model}"
        if key not in self._judge_gws:
            if self._providers is None:
                self._providers = self.deps.judge_providers()
            self._judge_gws[key] = judge_mod.judge_gateway(self.llm(), ref,
                                                           providers=self._providers)
        return self._judge_gws[key]

    def improver_gw(self) -> Gateway:
        """The improver's gateway, on the recording log: its own model, whatever the product runs on."""
        ref = self.deps.improver
        if ref is None:
            assert self.deps.gateway is not None         # skip_checks
            return self.deps.gateway.with_log(self.recording())
        if self._providers is None:
            self._providers = self.deps.judge_providers()
        return judge_mod.judge_gateway(self.llm(), ref, providers=self._providers,
                                       record_max_attempts=RECORD_MAX_ATTEMPTS)

    # --- steps 2-8 ---
    def run(self, since: datetime | None) -> HRound:
        d, uid = self.deps, self.user_id
        with d.pool.connection() as conn:
            recorded_cutoff = conn.execute('select clock_timestamp() as at').fetchone()['at']
        page_signals = tuple(d.signals(uid, since))
        rejected = self.rejections(since)
        signals = (*page_signals, *(s for s, _ in rejected))
        cur = d.registry.current(uid)
        if cur.spec is None:
            raise RuntimeError("no teaching spec")
        base_spec = cur.spec
        with d.pool.connection() as conn:
            cs = d.select(conn, uid, page_signals, base_spec, now=self.now, repo=d.repo)
            base_pack = pack_store.load_pack(conn, uid)
            sources = pack_store.source_of(conn, uid)
            banned = refused_keys([r.diff for r in harness_rsi.refused_changes(conn, uid)])
        # the proposal-time check's decisions (the commit reads them again); no repo (tests): none
        decisions = (Decisions.of(d.repo.load(uid).state) if d.repo is not None
                     else NO_DECISIONS)
        adeps = self.assistant_deps() if rejected else None
        if adeps is not None:
            turns, bad = assistant_cases(
                rejected, uid, check=lambda c: check_turn(c, base_pack=base_pack, deps=adeps))
            cs = with_assistant(cs, turns, bad)
        round_id = self.start(cs.finding or None)
        assert d.gateway is not None             # skip_checks
        self.rec.update(signal_ids=[s.signal_id for s in signals], cases=self._cases(cs, []))
        if len(cs.problem) < MIN_CASES:
            return self._thin(TOO_FEW.format(n=len(cs.problem), min=MIN_CASES), cs.finding)
        rdeps = ReplayDeps(deep=replace(self.deep(), llm=self.llm()), repo=d.repo, assistant=adeps,
                           writer_models=d.writer_models)
        held: tuple[Case, ...] = ()
        if d.evidence is not None:             # the improver never sees the held-out cases
            dev, held = held_out_split(cs.problem)
            cs = replace(cs, problem=dev)
            self.rec["cases"] = self._cases(cs, [], held=held)
            only_held = {i for c in held for i in c.signal_ids} - {i for c in dev for i in c.signal_ids}
            shown = tuple(s for s in signals if s.signal_id not in only_held)
            cs = replace(cs, finding='根据以下开发反馈和用例判断问题',
                         unusable=tuple(pair for pair in cs.unusable if pair[0] not in only_held))
        else:
            shown = signals
        if cs.problem and all(c.kind == "assistant" for c in cs.problem):
            turns_of = {s.signal_id for s, _ in rejected}
            shown = tuple(s for s in shown if s.signal_id in turns_of)
        words = self._words(cs, shown)
        train_ids = {signal.signal_id for signal in shown}
        train_rejected = tuple((signal, run) for signal, run in rejected if signal.signal_id in train_ids)
        excluded = frozenset(s for c in held for s in
                             (source_nodes(c.case_id) | source_nodes(c.run_id) | {f"trace:{c.run_id}"}
                              | {f"signal:{i}" for i in c.signal_ids}) if not s.startswith('session:'))
        boundary = SnapshotBoundary(recorded_cutoff, excluded)
        if adeps is not None:
            adeps = replace(adeps, cutoff=boundary.cutoff, excluded_sources=excluded)
            rdeps = replace(rdeps, assistant=adeps)
        comp = d.improve(uid, cs, deps=ImproverDeps(
            pool=d.pool, gateway=self.improver_gw(), account=self.account,
            replay=rdeps, base_pack=base_pack, sources=sources, defaults=d.defaults,
            base_spec=base_spec, judges=d.judges, judge_gateway=self.judge_gw, signals=shown,
            now=self.now, round_id=round_id, snapshot=boundary, memory=tuple(d.memory(uid)), trace=d.trace, words=words,
            guard=(None if d.guard is None else lambda df: d.guard(  # type: ignore[misc]
                uid, added_only(df, _before(df, base_pack), d.defaults))),
            components=component_infos(d.library), patch=d.patch, forge=d.forge,
            forge_binding=lambda: self._forge_binding(base_pack),
            signal_runs={s.signal_id: run for s, run in rejected},
            evals=self._evals(base_pack, self._feedback(train_rejected)),
            decisions=decisions, banned=banned))
        self.rec.update(improver_run_id=comp.run_id, candidates=[
            {"kind": c.kind, "diff": diff_to_json(c.diff, _before(c.diff, base_pack)),
             "rationale": c.rationale, "citations": list(c.citations)} for c in comp.candidates])
        for c in comp.comparisons:
            self.comparisons.setdefault(c.cmp_id, _cmp_json(c, False))
        decision: dict[str, Any] = {**self.rec.get("decision", {}), "improver": comp.decision,
                                    "rationale": comp.rationale, "citations": list(comp.citations)}
        self.rec["decision"] = decision
        if comp.patch is not None and d.patch is not None:
            self._patch = _PatchCtx(comp.patch, cs, signals, base_pack, base_spec, rdeps,
                                    comp.decision)
        if comp.decision == "none" or comp.chosen is None:
            return self.end("no_change", comp.rationale or "改进者决定这一轮不改")
        diff = comp.chosen
        before = _before(diff, base_pack)
        stored = diff_to_json(diff, before, base_spec.version if diff.spec_ops else None)
        self.rec["diff"] = stored

        # step 4: the smoke replays: the candidate pack writes the first SMOKE_CASES cases of the
        # agents the diff touches (the current pack only for a case the candidate cannot write, and
        # for the cost line of a cost key)
        touched = agents_touched(diff)
        unusable: list[tuple[str, str]] = []
        broken: list[tuple[str, str]] = []
        smoke = [c for c in (*cs.problem, *cs.anchors) if c.kind in touched][:SMOKE_CASES]
        skipped = [(c.case_id, NOT_AFFECTED if c.kind not in touched else NOT_SMOKED)
                   for c in (*cs.problem, *cs.anchors) if c not in smoke]
        priced = bool(cost_keys(diff))
        ref_cost, new_cost = [], []
        written = 0
        runs = run_all([lambda c=c: self._smoke(c, diff, base_pack, base_spec, rdeps, priced)
                        for c in smoke])
        for res in runs:                         # in case order, as the cases were listed
            if isinstance(res, Exception):
                raise res                        # not a replay failure: the round fails closed
            out, why_unusable, why_broken = res
            unusable.extend(why_unusable)
            broken.extend(why_broken)
            if out is None:
                continue
            written += 1
            new_cost.append(out[0])
            if out[1] is not None:
                ref_cost.append(out[1])
        self.rec["cases"] = self._cases(cs, unusable, skipped, held=held)

        # step 5: the gate, deterministic checks first; the judge only sees what they let through
        g = gate_mod.check(written, broken=broken,
                           complexity=complexity(base_pack, diff, d.defaults),
                           cited=bool(comp.citations))
        gate_rec: dict[str, Any] = {"written": written, "utility_delta": g.utility_delta,
                                    "complexity": complexity(base_pack, diff, d.defaults),
                                    "broken": [{"id": i, "reason": r} for i, r in broken]}
        rule_ask = False
        if g.verdict == "pass":
            g, gate_rec, rule_ask, blocked = self._gate(g, gate_rec, cs, held, diff, before, base_pack,
                                                        base_spec, rdeps, signals, words, touched)
            if blocked is not None:
                return self._record_end(gate_rec, g, decision, diff, blocked)
        decision["gate"] = {"verdict": g.verdict, "reasons": list(g.reasons), **gate_rec}
        tool = h12_tool(diff)
        with d.pool.connection() as conn:
            decision["gate"]["ref"] = harness_rsi.record_gate(
                conn, uid, round_id, f"tool:{tool[2]}" if tool else "diff", g.verdict, g.reasons,
                decision["gate"], now=self.now)
        reason = "；".join(g.reasons)
        if g.verdict == "thin":
            return self._thin(reason, cs.finding)
        if g.verdict == "fail":
            lesson = None
            if broken or g.losses or g.anchor_losses or gate_rec.get("redlines"):   # worth remembering
                lesson = self._lesson(comp, cs, signals, stored, (), g, broken)
            return self.end("failed", reason, lesson=lesson)

        # step 6: ask or apply
        why = _why(comp.rationale)
        costs = ((_mean(ref_cost), _mean(new_cost)) if priced else (None, None))
        ap = d.approvals
        level_asks = not tool_only(diff) and (d.propose_only or (ap is not None and ap.asks(uid, RSI)))
        if needs_ask(diff) or rule_ask or level_asks:
            if ap is None:
                return self.end("failed", NO_LEDGER)
            says = ((gate_rec.get("layers") or {}).get("task") or {}).get("says", "")
            cid = self.change_id = self._ask(diff, before, stored, why, costs,
                                             {**(gate_rec.get("evidence") or {}), "task_says": says})
            self.asked = True
            return self.end("asked", f"检查通过，等你批准：{reason}", change_id=cid)
        spec_before = stored["spec_before_version"]
        written: list[dict] = []

        def write(conn, v: int | None, applied: PackDiff) -> str:
            # the promoted diff: its H12 value carries the registry version undo needs
            final = stored if applied is diff else diff_to_json(applied, before, spec_before)
            written.append(final)
            return harness_rsi.record_change(
                conn, uid, round_id, final, "applied", version=v, cost_before=costs[0],
                cost_after=costs[1], reason=why, now=self.now)
        try:
            cid = self.change_id = _commit(
                uid, f"harness:{round_id}", diff, before, spec_before, d, why=why, now=self.now,
                write=write, promote=_promoter(uid, round_id, decision["gate"]["ref"], d, why=why,
                                               now=self.now, reason=TOOL_NOT_PROMOTED))
        except _Skip as e:
            return self.end("failed", e.reason)
        self.rec["diff"] = written[-1]
        _mark(uid, _stale_pages(cs, touched), cid, d, self.now)
        if diff.spec_ops:
            _rule_stale(uid, d, self.now)
        return self.end("applied", f"已改：{reason}", change_id=cid)

    def _feedback(self, rejected: Sequence[tuple[Signal, str]]) -> Any:
        """His words and the tools of the turns he complained about (`evalset_relevant.Feedback`)."""
        from evowiki.harness.evalset_relevant import Feedback
        said, tools = [], []
        with self.deps.pool.connection() as conn:
            for s, run in rejected:
                steps = store_trace.steps(conn, run)
                said.append(_said(steps))
                tools += [c["name"] for x in steps if x["kind"] == "model" for c in (x.get("turn") or {}).get("tool_calls") or []]
        self.feedback = Feedback(tuple(dict.fromkeys(x for x in said if x)), tuple(dict.fromkeys(tools)))
        return self.feedback

    def _evals(self, pack: HarnessPack, fb: Any) -> Callable[[PackDiff], Mapping[str, Any] | None] | None:
        """The improver's measure, on the cases that speak to this feedback; none speak: no such tool."""
        d = self.deps
        if d.evals is None:
            return None
        only = d.relevant(fb) if d.relevant is not None else None
        if only is not None and not only:
            return None
        self.rec.setdefault("decision", {})["eval_cases"] = list(only or ())
        return lambda df: d.evals(df, pack, only)

    def _patch_evalset(self, ctx: "_PatchCtx", main_root: Any) -> Callable[[str], Any] | None:
        """The patch's real turns (`patch_gate.evalset`), on the cases that speak to this feedback."""
        d = self.deps
        if d.patch_regression is None:
            return None
        only = d.relevant(self.feedback) if d.relevant is not None else None

        def check(cid: str) -> Any:
            from evowiki.harness.evalset_redlines import Regression
            if only is not None and not only:
                return Regression(error=NO_CASES)
            return d.patch_regression(cid, main_root, ctx.base_pack, only)
        return check

    def _words(self, cs: CaseSet, signals: Sequence[Signal]) -> CaseWords:
        """What only these cases and this learner say (`leak.CaseWords`), every problem case's."""
        if self.deps.words_of is not None:
            return self.deps.words_of(self.user_id, cs, signals)
        pages = self.deps.repo.load(self.user_id).state.pages if self.deps.repo is not None else {}
        records = [pages[c.page_id] for c in cs.problem if c.page_id in pages]
        return CaseWords(titles=tuple(sorted({r.title for r in records})),
                         headings=tuple(sorted({s.heading for r in records if r.page is not None
                                                for s in r.page.outline.sections})),
                         items=tuple(self.deps.memory_items(self.user_id)),
                         said=tuple(s.text.strip() for s in signals if s.text.strip()))

    def _gate(self, g: gate_mod.GateDecision, rec: dict[str, Any], cs: CaseSet,
              held: tuple[Case, ...], diff: PackDiff, before: Mapping[str, Value | None],
              pack: HarnessPack, spec: TeachingSpec, rdeps: ReplayDeps, signals: Sequence[Signal],
              words: CaseWords, touched: frozenset[str],
              ) -> tuple[gate_mod.GateDecision, dict[str, Any], bool, str | None]:
        """Past the smoke check: leak, red lines, rule guard, then the evidence (module doc, step 5).
        (decision, gate record, must ask, the round's end reason when the rule guard blocked)."""
        d, uid = self.deps, self.user_id
        failed = lambda why: replace(g, verdict="fail", reasons=(why,))
        hits = leaks(diff, before, d.defaults, words)
        rec["leaks"] = list(hits)
        if hits:
            return failed(LEAKED.format(what="；".join(hits))), rec, False, None
        rule_ask = False
        if d.guard is not None:
            rg = d.guard(uid, added_only(diff, before, d.defaults))
            self.rec.setdefault("decision", {})["rules"] = [r.dump() for r in rg.records]
            if rg.outcome == "block":
                return failed(f"{RULE_BLOCKED}：{rg.reason}"), rec, False, f"{RULE_BLOCKED}：{rg.reason}"
            rule_ask = rg.outcome == "confirm"
        def red_lines(passed: gate_mod.GateDecision) -> gate_mod.TaskLayer:
            """After a pass (they cost more than the judge)."""
            if d.redlines is None or passed.verdict != "pass":
                return gate_mod.TaskLayer("not_run")
            bad = list(d.redlines(uid, diff, pack, spec))
            rec["redlines"] = bad
            if bad and set(bad) <= {NOT_FINISHED, NO_BASELINE}:
                return gate_mod.TaskLayer("unknown", "、".join(bad))
            return (gate_mod.TaskLayer("broken", REDLINE.format(what="、".join(bad))) if bad
                    else gate_mod.TaskLayer("unchanged", "红线用例没有变差"))
        if d.evidence is None:
            task = red_lines(g)
            return (g if task.outcome == "not_run" else gate_mod.layers(task, g)), rec, rule_ask, None
        dev = tuple(c for c in cs.problem if c.kind in touched)
        anchors = tuple(c for c in cs.anchors if c.kind in touched)
        ev = d.evidence(dev, tuple(c for c in held if c.kind in touched), anchors, diff,
                        write=lambda c, df, _i: d.replay(c.case, df, base_pack=pack, base_spec=spec,
                                                         deps=rdeps),
                        judge=lambda c, a, b: self._compare(c.case, a, b, signals),
                        raw_judge=lambda c, a, b: self._raw_compare(c, a, b, signals),
                        prior_aa=self._prior_aa())
        rec["evidence"] = ev.detail
        judged = replace(g, verdict=ev.verdict, reasons=ev.reasons, wins=ev.wins, losses=ev.losses,
                         ties=ev.ties, anchor_losses=ev.anchor_losses)
        acted = tuple(ev.detail.get("action_cases") or ())
        if d.regression is None or touched != {"assistant"}:
            task = gate_mod.TaskLayer("not_run") if acted else red_lines(judged)
        else:
            from evowiki.harness.evalset_redlines import Regression
            only = None
            if d.relevant is not None:
                fb = self.feedback
                touched_tools = tuple(o.name.split(".")[2] for o in diff.ops if o.name.count(".") >= 2)
                only = tuple(d.relevant(replace(fb, tools=(*fb.tools, *touched_tools))))
            rec["regression_cases"] = list(only or ())
            r = (Regression(error=NO_CASES) if only == ()      # the real turns graded by code
                 else d.regression(uid, diff, pack, spec, only))
            rec["regression"] = asdict(r)
            task = (gate_mod.TaskLayer("unknown", r.error) if r.error
                    else gate_mod.TaskLayer("broken", REDLINE.format(what="、".join(r.broken))) if r.broken
                    else gate_mod.TaskLayer("restored", RESTORES.format(n=len(r.fixed), what="、".join(r.fixed))) if r.restores
                    else gate_mod.TaskLayer("unchanged", UNMOVED.format(n=len(r.fixed), p=r.p)))
        out = gate_mod.layers(task, judged, acted=acted)
        rec["layers"] = {"task": asdict(task), "content": {"verdict": judged.verdict, "reasons": list(judged.reasons),
                                                           "action_cases": list(acted), "from": "replay"}}
        return out, rec, rule_ask, None

    def _record_end(self, rec: dict[str, Any], g: gate_mod.GateDecision, decision: dict[str, Any],
                    diff: PackDiff, reason: str) -> HRound:
        """A rule block: the gate's row and the round's end (no lesson: nothing was compared)."""
        decision["gate"] = {"verdict": g.verdict, "reasons": list(g.reasons), **rec}
        tool = h12_tool(diff)
        assert self.round_id is not None
        with self.deps.pool.connection() as conn:
            decision["gate"]["ref"] = harness_rsi.record_gate(
                conn, self.user_id, self.round_id, f"tool:{tool[2]}" if tool else "diff", g.verdict,
                g.reasons, decision["gate"], now=self.now)
        return self.end("failed", reason)

    def _prior_aa(self) -> list[str]:
        """A/A winners of this user's earlier rounds with the same writer and judge config: the gate
        rows keep them, and the pool resets when the judge prompt's version does."""
        with self.deps.pool.connection() as conn:
            return harness_rsi.prior_aa(conn, self.user_id)

    def _raw_compare(self, c: EvalCase, a: ReplayResult, b: ReplayResult,
                     signals: Sequence[Signal]) -> Comparison:
        """`_compare` not recorded among the round's comparisons: canaries and A/A judge no candidate."""
        ref = judge_mod.judge_for(a.writer_provider, self.deps.judges)
        text, more, kind = judge_mod.case_inputs(c.case, signals)
        return judge_mod.compare(c.case, a, b, signals_text=text, asked_for_more=more,
                                 gw=self.judge_gw(ref), account=self.account, kind=kind)

    def _cases(self, cs: CaseSet, final_unusable: Sequence[tuple[str, str]],
               skipped: Sequence[tuple[str, str]] = (), *, held: Sequence[Case] = ()) -> dict:
        """The round's cases: `skipped` ((case id, reason): another agent's case, NOT_AFFECTED, or
        one past the smoke cases, NOT_SMOKED) only when there are some; `agent` = `case_agent` of
        the problem cases, which the round's and observe's lessons record."""
        return {"problem": [_case_json(c) for c in cs.problem],
                "anchors": [_case_json(c) for c in cs.anchors],
                **({"held_out": [_case_json(c) for c in held]} if held else {}),
                "unusable": [{"id": i, "reason": r} for i, r in (*cs.unusable, *final_unusable)],
                **({"skipped": [{"id": i, "reason": r} for i, r in skipped]}
                   if skipped else {}),
                "finding": cs.finding, "agent": case_agent(cs.problem)}

    def _smoke(self, case: Case, diff: PackDiff, pack: HarnessPack, spec: TeachingSpec,
               rdeps: ReplayDeps, priced: bool
               ) -> tuple[tuple[float, float | None] | None, list[tuple[str, str]],
                          list[tuple[str, str]]]:
        """The candidate's replay of `case`: ((its cost, the current pack's cost when `priced`, else
        None), unusable, broken). When the candidate cannot write it, the current pack tries once:
        if it cannot either, the case is unusable (a snapshot gone, a provider down, the writer's
        model gone); if it can, the case is `broken` (the smoke check fails the change on it).
        Nothing is judged. A priced case replays both packs at once (`run_all`): the current
        pack's replay is the cost line, or the retry a failed candidate needs. Raises what is not a
        replay failure."""
        failures = (ReplayFailed, LookupError, PatchError, HarnessInvalid, NotReplayable)

        def run(d: PackDiff) -> ReplayResult:
            return self.deps.replay(case, d, base_pack=pack, base_spec=spec, deps=rdeps)

        def current() -> ReplayResult | Exception:
            try:
                return run(PackDiff())
            except failures as e:
                return e
        if priced:
            a_out, b_out = run_all([lambda: run(diff), current])
        else:
            a_out, b_out = run_all([lambda: run(diff)])[0], None
        if isinstance(a_out, Exception) and not isinstance(a_out, failures):
            raise a_out
        if isinstance(b_out, Exception) and not isinstance(b_out, failures):
            raise b_out
        if isinstance(a_out, Exception):
            e = a_out
            why = TURN_REPLAY_FAILED if case.kind == "assistant" else REPLAY_FAILED.format(e=e)
            log.warning("harness round %s of %s: the candidate's replay of %s failed: %r",
                        self.round_id, self.user_id, case.case_id, e)
            if b_out is None:
                b_out = current()
            if isinstance(b_out, Exception):
                log.warning("harness round %s of %s: the current pack's replay of %s failed too: "
                            "%r", self.round_id, self.user_id, case.case_id, b_out)
                return None, [(case.case_id, why)], []
            return None, [], [(case.case_id, why)]
        if not priced:
            return (a_out.cost, None), [], []
        if isinstance(b_out, Exception) or b_out is None:
            return (a_out.cost, None), [], []    # the cost line goes without this case
        return (a_out.cost, b_out.cost), [], []

    def _compare(self, case: Case, a: ReplayResult, b: ReplayResult,
                 signals: Sequence[Signal]) -> Comparison:
        ref = judge_mod.judge_for(a.writer_provider, self.deps.judges)      # JudgeUnavailable
        text, more, kind = judge_mod.case_inputs(case, signals)
        cmp = judge_mod.compare(case, a, b, signals_text=text, asked_for_more=more,
                                gw=self.judge_gw(ref), account=self.account, kind=kind)
        self.comparisons[cmp.cmp_id] = _cmp_json(cmp, True)
        return cmp

    def _thin(self, reason: str, finding: str) -> HRound:
        if finding:
            with self.deps.pool.connection() as conn:
                prev = [r for r in harness_rsi.rounds(conn, self.user_id, 50)
                        if r.round_id != self.round_id and r.status not in ("running", "skipped")]
            streak = 1
            for r in prev:
                if r.status != "thin" or r.finding != finding:
                    break
                streak += 1
            if streak == THIN_NOTICE_AFTER:
                reason = f"{reason}；{THIN_NOTICE}"
        return self.end("thin", reason)

    def _lesson(self, comp: Composition, cs: CaseSet, signals: Sequence[Signal], stored: dict,
                cmps: Sequence[Comparison], g: gate_mod.GateDecision,
                broken: Sequence[tuple[str, str]] = ()) -> dict:
        ids = {i for c in cs.problem for i in c.signal_ids}
        return {"situation": {"agent": case_agent(cs.problem), "finding": cs.finding,
                              "signal_kinds": sorted({s.kind for s in signals
                                                      if s.signal_id in ids})},
                "change": stored,
                "evidence": {"comparisons": [c.cmp_id for c in cmps], "wins": g.wins,
                             "losses": g.losses, "ties": g.ties,
                             "anchor_losses": g.anchor_losses,
                             "utility_delta": g.utility_delta, "round_id": self.round_id,
                             **({"broken": [{"id": i, "reason": r} for i, r in broken]}
                                if broken else {})},
                "verdict": "ineffective",
                "scope": "general" if comp.decision == "a" else "user", "weight": 1.0}

    # --- step 6b: a code patch ---
    def _run_patch(self, ctx: "_PatchCtx") -> dict | None:
        """Builds and evaluates the improver's patch request (module doc, step 6b); records
        `decision["patch"]` and returns the `ineffective` lesson of a clear loss, else None. Never
        raises: an error is recorded as the patch's `error` (the key path is already decided)."""
        req = ctx.proposal.request
        rec: dict[str, Any] = {"title": req.title, "goal": req.goal,
                               "citations": list(ctx.proposal.citations)}
        self.rec.setdefault("decision", {})["patch"] = rec      # harness_round.decision.patch
        try:
            return self._patch_step(ctx, rec)
        except Exception as e:  # broad on purpose: the round's own outcome stands (fail closed)
            log.exception("harness round %s of %s: the code patch failed", self.round_id,
                          self.user_id)
            rec.update(status="error", reason=PATCH_ERROR, error=type(e).__name__)
            return None

    def _patch_step(self, ctx: "_PatchCtx", rec: dict[str, Any]) -> dict | None:
        d, uid = self.deps, self.user_id
        hooks, req = d.patch, ctx.proposal.request
        assert hooks is not None and self.round_id is not None
        with d.pool.connection() as conn:
            on = hooks.enabled(conn, uid)
        if not on:
            rec.update(status="off", reason=PATCH_OFF)
            return None
        llm = self.llm()                       # the deep tier over the recording log
        tier = forge_tier(ctx.base_pack, "patch")
        config = llm.config if tier in (None, "deep") else (llm.tiers or {}).get(tier)
        if config is None:
            rec.update(status="error", reason=PATCH_NO_TIER)
            return None
        # no tier picked: the improver's own model writes the code, never the product's cheap one
        coder_gw = (self.improver_gw() if tier is None and d.improver is not None
                    else replace(llm, config=config).gateway(None)[0])
        deep_gw, _ = llm.gateway(None)
        pdeps = hooks.deps_for(coder_gw, self.account, ctx.base_pack)
        built: dict[str, Any] = {}

        def propose_fn(*args: Any, **kw: Any):
            inner = hooks.propose
            if inner is None:
                from evowiki.forge.api import propose as inner
            res = inner(*args, **kw)
            built[res.candidate_id] = res
            return res

        def diff_lines(cid: str) -> int:
            return int(built[cid].build.diff_lines)    # a non-patch report fails the gate

        patch_cmps: list[str] = []

        def judge(c: Case, a: ReplayResult, b: ReplayResult) -> Comparison:
            cmp = self._compare(c, a, b, ctx.signals)
            # the patch gate's, not the key gate's final comparisons
            self.comparisons[cmp.cmp_id].update(final=False, patch=True)
            patch_cmps.append(cmp.cmp_id)
            return cmp

        gate = HarnessPatchGate(
            round_id=self.round_id, user_id=uid,      # deep cases only (patch_gate P11)
            cases=(*(c for c in ctx.cases.problem if c.kind == "deep"), *ctx.cases.anchors),
            base_pack=ctx.base_pack, base_spec=ctx.base_spec, replay_deps=ctx.rdeps,
            child=hooks.child_for(deep_gw, self.account),
            encode=hooks.encode or self._encoder(ctx.base_spec), judge=judge,
            judge_cost=self._judge_cost, diff_lines=diff_lines, pool=d.pool, now=self.now,
            replay=d.replay, evalset=self._patch_evalset(ctx, hooks.main_root))
        note = f"这一轮的问题：{ctx.cases.finding}" if ctx.cases.finding else ""
        refused: str | None = None
        try:
            res = run_patch(req, Evidence(citations=ctx.proposal.citations, note=note),
                            user_id=uid, deps=pdeps, gate=gate, now=self.now,
                            propose_fn=propose_fn)
        except PromoteRefused as e:      # the gate passed; promote_patch said no
            log.warning("harness round %s of %s: a passing patch was not promoted: %s",
                        self.round_id, uid, e)
            if gate.last is None or not built:
                raise
            refused = type(e).__name__
            res = next(iter(built.values()))
        b = res.build
        rec.update(candidate_id=res.candidate_id, build_ok=b.ok, build_reason=b.reason,
                   build_cost=b.cost_usd)
        if not b.ok:
            rec.update(status="build_failed", reason=b.reason)
            return None
        ev = gate.last
        assert ev is not None                  # run_patch evaluates every built patch
        g = ev.decision
        with d.pool.connection() as conn:
            row = patch_store.get_patch(conn, res.candidate_id)
        rec.update(verdict=g.verdict, reasons=list(g.reasons), gate_ref=ev.gate_ref,
                   state=None if row is None else row.state, eval_cost=ev.cost,
                   diff_lines=getattr(b, "diff_lines", None), wins=g.wins, losses=g.losses,
                   ties=g.ties, anchor_losses=g.anchor_losses,
                   unusable=[{"id": i, "reason": r} for i, r in ev.unusable])
        if ev.error is not None:
            rec.update(status="error", reason="；".join(g.reasons), error=ev.error)
            return None
        if g.verdict == "pass":
            if rec["state"] == "awaiting_approval":
                rec["status"] = "asked"
                return None
            with d.pool.connection() as conn:
                off = not hooks.enabled(conn, uid)
            rec.update(status="error", error=refused or "not_promoted",
                       reason=f"{PATCH_NOT_PROMOTED}：代码修改的开关已经关了" if off
                       else PATCH_NOT_PROMOTED)
            return None
        rec["status"] = "failed" if g.verdict == "fail" else "thin"
        if g.verdict == "fail" and (g.losses or g.anchor_losses):
            deep = [c for c in ctx.cases.problem if c.kind == "deep"]   # the gate's cases (P11)
            ids = {i for c in deep for i in c.signal_ids}
            return {"situation": {"agent": case_agent(deep), "finding": ctx.cases.finding,
                                  "signal_kinds": sorted({s.kind for s in ctx.signals
                                                          if s.signal_id in ids})},
                    "change": {"patch": {"candidate_id": res.candidate_id, "title": req.title,
                                         "goal": req.goal}},
                    "evidence": {"comparisons": patch_cmps, "wins": g.wins, "losses": g.losses,
                                 "ties": g.ties, "anchor_losses": g.anchor_losses,
                                 "utility_delta": g.utility_delta, "round_id": self.round_id,
                                 "gate_ref": ev.gate_ref},
                    "verdict": "ineffective",
                    "scope": "general" if ctx.decision == "a" else "user", "weight": 1.0}
        return None

    def _encoder(self, spec: TeachingSpec) -> Callable[[Case, HarnessPack], dict]:
        """The gate's `encode`: `patch_gate.deep_encoder` on a fresh pooled connection per case
        (a gate run can take minutes; no connection is held across it)."""
        deep = self.deep()

        def encode(case: Case, pack: HarnessPack) -> dict:
            with self.deps.pool.connection() as conn:
                return deep_encoder(conn, self.deps.repo, spec, excerpts=deep.sources,
                                    library=deep.library, now=self.now)(case, pack)
        return encode

    def _judge_cost(self, cmp: Comparison) -> float:
        """What the comparison's judge calls cost (a call served from the log is not in `made`)."""
        made = self.recording().made
        return sum((made[i].cost or 0.0) for i in cmp.call_ids if i in made)

    def _ask(self, diff: PackDiff, before: Mapping[str, Value | None], stored: dict, why: str,
             costs: tuple[float | None, float | None], evidence: dict | None = None) -> str:
        d, uid = self.deps, self.user_id
        assert d.approvals is not None and self.round_id is not None
        with pinned.transaction(d.pool), pinned.connection(d.pool) as conn:
            titles = load_titles(conn, candidate_ids([stored]), uid)
            cid = harness_rsi.record_change(conn, uid, self.round_id, stored, "asked",
                                            cost_before=costs[0], cost_after=costs[1],
                                            reason=why, now=self.now)
            d.approvals.propose(uid, kind=METHOD_KIND, scope=RSI, key=ledger_key(uid, cid),
                                summary=summary(stored, *costs, titles)[:300],
                                why="\n".join(x for x in (
                                    why, _evidence_words(evidence),
                                    "" if d.redlines is not None else NO_REDLINES) if x),
                                content={"change_id": PREFIX + cid, "kind": "H",
                                         "diff": _lines(diff, before, costs, titles, d.defaults)},
                                at=self.now)
        return cid


@dataclass(frozen=True)
class _PatchCtx:
    proposal: PatchProposal
    cases: CaseSet
    signals: Sequence[Signal]
    base_pack: HarnessPack
    base_spec: TeachingSpec
    rdeps: ReplayDeps
    decision: str


# --- the cycle's harness track (teach.cycle.HarnessTrack) -------------------------------------

def observation_row(r: ObserveResult) -> dict:
    """The cycle result's row for one harness change's observation (track A's keys but `n`, plus
    the deltas, the lesson written and the page jobs the cost metrics left out)."""
    prefix = PREFIX if r.kind == "change" else PATCH_PREFIX
    return {"change_id": prefix + r.change_id, "verdict": r.verdict, "deltas": dict(r.deltas),
            "point": r.point, "rolled_back": r.rolled_back, "lesson": r.lesson, "note": r.note,
            "dropped": {k: dict(v) for k, v in r.dropped.items()}}


def fast_row(r: FastResult) -> dict:
    """The cycle result's row for one action of the fast rollback channel (`fast_check`):
    `observation_row`'s keys (verdict harmful, no deltas, no point) plus the failing signatures
    and their run ids under `failures`."""
    return {"change_id": PREFIX + r.change_id, "verdict": "harmful", "deltas": {},
            "point": False, "rolled_back": r.rolled_back, "lesson": r.lesson, "note": r.note,
            "dropped": {}, "failures": [f.dump() for f in r.failures]}


@dataclass(frozen=True)
class HarnessRounds:
    deps: RoundDeps

    def last_round_at(self, user_id: str) -> datetime | None:
        with self.deps.pool.connection() as conn:
            r = harness_rsi.last_round(conn, user_id)
        return None if r is None else r.started_at

    def due_count(self, user_id: str, since: datetime | None) -> int:
        """What the round's due rule counts (`_due_count`): page signals plus assistant feedback."""
        return _due_count(user_id, self.deps, since)

    def run(self, user_id: str, *, now: datetime, manual: bool) -> tuple[str, float]:
        r = run_round(user_id, self.deps, now=now, manual=manual)
        if self.deps.gateway is not None and r.status not in ("skipped", "error", "thin"):
            update_object_digests(self.deps.pool, user_id, self.deps.gateway,
                                  Account(user_id=user_id, group=GROUP), now=now)
        line_ = f"harness {r.status}" + (f" {r.round_id}" if r.round_id else "")
        return f"{line_}: {r.reason}"[:300], r.cost

    def observe(self, user_id: str, *, now: datetime) -> list[dict]:
        d = self.deps
        sessions = d.sessions
        rated = (None if sessions is None
                 else lambda uid: rated_answers(sessions, uid))   # assistant_down_rate
        # the fast channel first: a change it rolls back is no longer applied, observe skips it
        fast = fast_check(user_id, FastDeps(pool=d.pool, registry=d.registry), now=now)
        for f in fast:
            if f.rolled_back:
                clear_marks(d.repo, d.new_id, user_id, f.change_id, now)
        results = observe_changes(
            user_id, ObserveDeps(pool=d.pool, registry=d.registry, signals=d.signals,
                                 rated=rated), now=now)
        for r in results:
            if r.rolled_back and r.kind == "change":    # its pages need no rewrite any more
                clear_marks(d.repo, d.new_id, user_id, r.change_id, now)
        # every cycle: a rolled-back rule change re-compares its pages, and a marking pass that
        # failed earlier (logged) is retried; idempotent (`stale_checked_version`)
        _rule_stale(user_id, d, now)
        if d.gateway is not None and d.settings(user_id).harness:
            update_object_digests(d.pool, user_id, d.gateway,
                                  Account(user_id=user_id, group=GROUP), now=now)
        return [fast_row(f) for f in fast] + [observation_row(r) for r in results]

    def owns(self, change_id: str) -> bool:
        return change_id.startswith(PREFIX)

    def approve(self, user_id: str, change_id: str, *, now: datetime,
                on_accept: Callable[[], None]) -> None:
        approve_change(user_id, change_id.removeprefix(PREFIX), self.deps, now=now,
                       on_accept=on_accept)

    def reject(self, user_id: str, change_id: str, *, now: datetime) -> None:
        reject_change(user_id, change_id.removeprefix(PREFIX), self.deps, now=now)

    def asked_keys(self, user_id: str) -> list[tuple[str, str]]:
        with self.deps.pool.connection() as conn:
            rows = harness_rsi.asked_changes(conn, user_id)
        return [(PREFIX + r.change_id, ledger_key(user_id, r.change_id)) for r in rows]
