"""The harness gate for forge code patches (harness 3b, Task 9; plan P9 / P10).

A forge patch round (`forge.patch.round.run_patch`) builds a candidate on branch `forge/<cid>`, then
asks its gate. `HarnessPatchGate` replays the round's cases twice and judges every pair in the
parent, with the unmodified judge and gate:

- `a` = the candidate's code: the cases go to the forge replay child (`forge.patch.replay.
  run_replay`, entry `deep.refine`), which runs `forge/<cid>` with no keys or database and asks
  the parent for every model call (`replay_cases.parent_converse`: accounted, one per evaluation).
- `b` = the running code: the same cases replayed in process (`harness.replay.replay` with the
  empty diff).

Both arms run the same pack (`deep_route_pack`): the base pack minus every `H10.*` key (P10: the
child has one model route, the parent's deep tier) and with every deep hook step (H9 / H11) moved
to the `deep` tier. The child sends every tier to that one route anyway; rewriting the steps (not
the baseline's tier map) makes the baseline run them on the same chain the child runs them on:
the writer chain, with its fallback and the writer's error handling, never a separate fast / good
gateway. The rewritten pack is what is encoded for the child.

A case is unusable (listed with its reason, never judged) when it cannot be encoded
(`CaseNotEncodable`, a missing snapshot), the child wrote no text for it, the baseline replay
failed, the two sides were written by different providers (the judge is picked per writer family,
`judge.judge_for` of the patched side, as in phase 2), or there is no judge for its writer
(`JudgeUnavailable`). The decision is `gate.decide(problem, anchors, complexity=diff_lines,
cited=True)` (citations are checked by the improver's tool). A refused child run (no branch, a
changed child file, the call cap, a timeout) is a `fail` with the forge's reason and no
comparisons; a child result whose count does not match the cases sent is a `fail`
(OUTPUT_COUNT); any other error during the evaluation is a `fail` with ERROR (fail closed), with
what was spent before it. These evaluation errors set `PatchEvaluation.error` (the class name stays
there and in the stored detail; reasons are plain Chinese, `plain`).

Every evaluation writes one `harness_gate` row (subject `patch:<cid>`) through its own pooled
connection, committed at once: `promote_patch` reads it back through `ok` (another connection),
and the decision stays on record when the promotion is refused. A `pass` returns the gate ref, so
`run_patch` promotes the candidate to `awaiting_approval`: it lands only after the user's approve
(review mode, `forge.patch.land`). Anything else returns None and the candidate stays `built`.

`evalset(cid)`: the real turns on the patch's code, graded by code (`evalset_redlines.harness_patch_regression`),
and for the assistant's patches the only evidence there is: a recorded request cannot show a code change. It is the
task layer of `gate.layers` over the replays' decision.

This module is under `harness/` (denied to L2 patches, `forge.policy`): a patch can never change
how it is judged."""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from evowiki.forge.patch import replay as forge_replay
from evowiki.forge.patch.replay import ReplayResult as ChildResult
from evowiki.harness import gate as gate_mod
from evowiki.harness.diff import PackDiff
from evowiki.harness.judge import Comparison, JudgeUnavailable
from evowiki.harness.pack import HarnessInvalid, HarnessPack, Value
from evowiki.harness.replay import Case, ReplayDeps, ReplayResult, replay
from evowiki.harness.replay_cases import (
    CaseNotEncodable,
    encode_deep_case,
    parent_converse,
    strip_h10,
)
from evowiki.llm.gateway import Gateway
from evowiki.llm.usage import Account
from evowiki.sources.excerpts import SourceExcerpts
from evowiki.store import harness_rsi
from evowiki.teach.contracts import TeachingSpec
from evowiki.teach.spec_ops import PatchError
from evowiki.wikiapp.refine_service import ReplayFailed
from evowiki.wikiapp.repo import Repo

log = logging.getLogger("evowiki.harness.patch_gate")

ENTRY = "deep.refine"
DEEP_HOOKS = ("H9.deep.", "H11.deep.")      # the deep writer's hook keys (keys.HOOKS)
SUBJECT = "patch:{cid}"

# the round's own wording (harness.round, pinned equal by the tests; not imported: the round module
# imports the improver, which may build this gate)
MIXED = "参照和候选是不同的写作模型写的，没法比较"
REPLAY_FAILED = "重放没写出这一段：{e}"
BAD_OUTPUT = "回放结果格式不对"
NOT_ENCODABLE = "这一段没法交给回放进程：{e}"
NO_CASES = "没有能交给回放进程的样例"
ERROR = "评估这次代码改动时出错了，不采用"
CHILD_FAILED = "回放进程没有完成"
OUTPUT_COUNT = "回放进程返回的结果数和样例数对不上"

# an English exception class name at the start of (or inside) a reason the user may see: the class
# stays in the stored detail (`error`), never in a reason
_CLASS = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Za-z0-9_]*(?:Error|Exception|Unavailable|Refused|"
                    r"Failed|Timeout|NotFound|Misconfigured|Disabled|Forbidden|Exit|Interrupt)"
                    r"(?![A-Za-z0-9_])[：:]?\s*|(?<![A-Za-z0-9_])Error(?![A-Za-z0-9_])[：:]?\s*")


def plain(text: str, fallback: str = CHILD_FAILED) -> str:
    """`text` without English exception class names (and the separator after one), a bare
    `Error:` prefix included; `fallback` when nothing is left. Only separators the removal left
    dangling are trimmed (a reason's own closing 「）」 stays)."""
    out = _CLASS.sub("", str(text)).strip()
    out = re.sub(r"^[：:；;]\s*|[：:；;]$", "", out).replace("（）", "").replace("()", "").strip()
    return out or fallback


def deep_route_pack(pack: HarnessPack) -> HarnessPack:
    """`pack` as both arms of a patch evaluation run it (module doc): no `H10.*` keys, and every
    step of a deep hook key on the `deep` tier. A value that is not a list of steps is left as it
    is (it fails the same way on both sides)."""
    values: dict[str, Value] = {}
    for k, v in strip_h10(pack).values.items():
        if k.startswith(DEEP_HOOKS) and isinstance(v, list):
            v = [{**s, "tier": "deep"} if isinstance(s, dict) else s for s in v]
        values[k] = v
    return HarnessPack(pack.version, values)


def deep_encoder(conn: Any, repo: Repo, spec: TeachingSpec, *, excerpts: SourceExcerpts | None,
                 library: Any = None, now: datetime | None = None
                 ) -> Callable[[Case, HarnessPack], dict]:
    """The gate's `encode`: `replay_cases.encode_deep_case` on `conn` / `repo` with the round's
    spec. `excerpts`: the deep service's `sources` (None only when it has no material reader)."""
    def encode(case: Case, pack: HarnessPack) -> dict:
        return encode_deep_case(conn, repo, case, pack, spec, excerpts=excerpts, library=library,
                                now=now)
    return encode


def forge_child(*, gateway: Gateway, account: Account, main_root: Path,
                runner: forge_replay.PipeRunner, python: str, test_db_url: str, tmp: Path
                ) -> Callable[[str, list[dict]], ChildResult]:
    """The gate's `child`: `run_replay(cid, entry="deep.refine", ...)` with a fresh accounted
    `parent_converse(gateway, account)` per call (one per evaluated candidate). `gateway`: the
    deep tier's gateway the child's model calls are made through."""
    def child(cid: str, cases: list[dict]) -> ChildResult:
        return forge_replay.run_replay(
            cid, entry=ENTRY, cases=cases, main_root=main_root,
            converse=parent_converse(gateway, account), runner=runner, python=python,
            test_db_url=test_db_url, tmp=tmp)
    return child


@dataclass(frozen=True)
class PatchEvaluation:
    gate_ref: str
    decision: gate_mod.GateDecision
    comparisons: tuple[Comparison, ...]
    unusable: tuple[tuple[str, str], ...]       # (case id, plain reason)
    cost: float | None      # child replay + baseline replays + judge; None: the child's is unknown
    complexity: int = 0
    # set when the evaluation itself did not complete (an unexpected error, a refused child run,
    # a malformed child result): the verdict is a fail that says nothing about the patch
    error: str | None = None


@dataclass
class HarnessPatchGate:
    """`forge.patch.round.PatchGate` for the harness round (module doc). `evaluate` replays the
    round's cases on the candidate's commit (forge child, `child`) and on the running code (in
    process, `replay` with `replay_deps`), judges every pair here in the parent with the unmodified
    judge (`judge`: the round's comparison, `a` = patched, `b` = baseline) and gate, and stores the
    decision (`harness_gate`, subject "patch:<cid>"). It returns the gate ref when the verdict is
    `pass` (else None) and always sets `last`. `ok(ref)`: a ref this gate issued, whose stored row
is this user's, about the candidate it evaluated, with verdict `pass`. So
    `run_patch` promotes a passing candidate to `awaiting_approval`, and nothing lands without the
    user's approval.

    `cases`: the round's problem and anchor cases (deep only, P11). `encode(case, pack)`: the
    child's JSON for a case (`deep_encoder`; CaseNotEncodable / LookupError = unusable).
    `diff_lines(cid)`: the build report's `diff_lines` (the gate's complexity). `judge_cost(cmp)`:
    what the comparison's judge calls cost (USD; a call served from the log is 0). `pool`: where
    the gate rows are written and read."""

    round_id: str
    user_id: str
    cases: Sequence[Case]
    base_pack: HarnessPack
    base_spec: TeachingSpec
    replay_deps: ReplayDeps
    child: Callable[[str, list[dict]], ChildResult]
    encode: Callable[[Case, HarnessPack], dict]
    judge: Callable[[Case, ReplayResult, ReplayResult], Comparison]
    judge_cost: Callable[[Comparison], float]
    diff_lines: Callable[[str], int]
    pool: ConnectionPool
    now: datetime
    replay: Callable[..., ReplayResult] = replay
    evalset: Callable[[str], Any] | None = None
    last: PatchEvaluation | None = field(default=None, init=False)
    _issued: dict[str, str] = field(default_factory=dict, init=False, repr=False)  # ref -> cid
    _spent: float | None = field(default=0.0, init=False, repr=False)

    def evaluate(self, conn: Any, candidate_id: str, user_id: str) -> str | None:
        """The candidate's evaluation (class doc). `conn` (run_patch's open transaction) is not
        written: the gate row is committed through `pool` so `ok` can read it."""
        if user_id != self.user_id:
            raise ValueError(f"gate of user {self.user_id!r} asked for user {user_id!r}")
        self._spent = 0.0
        try:
            ev = self._evaluate(candidate_id)
        except Exception as e:  # broad on purpose: fail closed, recorded as a failed gate, logged
            log.exception("patch gate of %s on %s failed", self.user_id, candidate_id)
            g = gate_mod.GateDecision("fail", (ERROR,), 0, 0, 0, 0, 0.0)
            # what was spent before the error (None: the child's cost was unknown)
            ev = _Ev(g, (), (), None if self._spent is None else round(self._spent, 6), 0,
                     {"error": type(e).__name__, "message": str(e)[:300]})
        ref = self._record(candidate_id, ev)
        self._issued[ref] = candidate_id
        err = ev.extra.get("error")
        self.last = PatchEvaluation(gate_ref=ref, decision=ev.decision,
                                    comparisons=ev.comparisons, unusable=ev.unusable,
                                    cost=ev.cost, complexity=ev.complexity,
                                    error=None if err is None else str(err))
        return ref if ev.decision.verdict == "pass" else None

    def ok(self, gate_ref: str) -> bool:
        """The ref is one this gate issued, its stored row is this user's, about the candidate it
        evaluated (subject "patch:<cid>"), and its verdict is `pass`."""
        cid = self._issued.get(gate_ref)
        if cid is None:
            return False
        with self.pool.connection() as conn:
            row = harness_rsi.get_gate(conn, self.user_id, gate_ref)
        return (row is not None and row.subject == SUBJECT.format(cid=cid)
                and row.verdict == "pass")

    # --- internals ---
    def _add(self, cost: float | None) -> None:
        if self._spent is not None:
            self._spent = None if cost is None else self._spent + cost

    def _evaluate(self, cid: str) -> "_Ev":
        pack = deep_route_pack(self.base_pack)
        unusable: list[tuple[str, str]] = []
        sent: list[tuple[Case, dict]] = []
        for case in self.cases:
            try:
                sent.append((case, self.encode(case, pack)))
            except CaseNotEncodable as e:
                unusable.append((case.case_id, NOT_ENCODABLE.format(e=plain(str(e)))))
            except LookupError as e:
                unusable.append((case.case_id, REPLAY_FAILED.format(e=plain(str(e)))))
        complexity = int(self.diff_lines(cid))
        if not sent:
            g = gate_mod.decide([], [], complexity=complexity, cited=True)
            return self._with_evalset(cid, _Ev(g, (), tuple(unusable), 0.0, complexity, {"note": NO_CASES}))
        res = self.child(cid, [enc for _, enc in sent])
        self._add(res.cost_usd)
        child_info = {"ok": res.ok, "calls": res.calls, "cost_usd": res.cost_usd,
                      "exit": res.stats.exit, "wall_s": res.stats.wall_s, "reason": res.reason}
        if not res.ok:
            g = gate_mod.GateDecision("fail", (plain(res.reason),), 0, 0, 0, 0, 0.0)
            return _Ev(g, (), tuple(unusable), res.cost_usd, complexity,
                       {"child": child_info, "error": "child_refused"})
        if len(res.outputs) != len(sent):
            g = gate_mod.GateDecision("fail", (OUTPUT_COUNT,), 0, 0, 0, 0, 0.0)
            return _Ev(g, (), tuple(unusable), res.cost_usd, complexity,
                       {"child": child_info, "error": "output_count"})
        cost: float | None = res.cost_usd
        problem: list[Comparison] = []
        anchors: list[Comparison] = []
        for (case, _), out in zip(sent, res.outputs, strict=True):
            a = _patched(case, out, unusable)
            if a is None:
                continue
            try:
                b = self.replay(case, PackDiff(), base_pack=pack, base_spec=self.base_spec,
                                deps=self.replay_deps)
            except ReplayFailed as e:          # the writer ran: its spend counts
                self._add(e.cost)
                cost = None if cost is None else cost + e.cost
                unusable.append((case.case_id, REPLAY_FAILED.format(e=plain(str(e)))))
                continue
            except (LookupError, PatchError, HarnessInvalid) as e:
                unusable.append((case.case_id, REPLAY_FAILED.format(e=plain(str(e)))))
                continue
            self._add(b.cost)
            cost = None if cost is None else cost + b.cost
            if a.writer_provider != b.writer_provider:
                unusable.append((case.case_id, MIXED))
                continue
            try:
                cmp = self.judge(case, a, b)
            except JudgeUnavailable as e:
                unusable.append((case.case_id, e.reason))
                continue
            jc = self.judge_cost(cmp)
            self._add(jc)
            cost = None if cost is None else cost + jc
            (anchors if case.anchor else problem).append(cmp)
        g = gate_mod.decide(problem, anchors, complexity=complexity, cited=True)
        return self._with_evalset(cid, _Ev(g, (*problem, *anchors), tuple(unusable),
                                           None if cost is None else round(cost, 6), complexity, {"child": child_info}))

    def _with_evalset(self, cid: str, ev: "_Ev") -> "_Ev":
        if self.evalset is None:
            return ev
        r = self.evalset(cid)
        task = (gate_mod.TaskLayer("unknown", r.error) if r.error
                else gate_mod.TaskLayer("broken", f"补丁上有用例新坏了：{'、'.join(r.broken)}") if r.broken
                else gate_mod.TaskLayer("restored", f"补丁的代码上修好了 {len(r.fixed)} 条用例（{'、'.join(r.fixed)}），没有新坏的") if r.restores
                else gate_mod.TaskLayer("unchanged", f"补丁的代码上明显修好 {len(r.fixed)} 条，不够说明修好了（p={r.p:.3f}）"))
        content = ev.decision
        if not ev.comparisons and content.verdict == "fail":     # no case to compare: no verdict
            content = replace(content, verdict="thin")
        return replace(ev, decision=gate_mod.layers(task, content),
                       extra={**ev.extra, "regression": asdict(r), "task": asdict(task)})

    def _record(self, cid: str, ev: "_Ev") -> str:
        g = ev.decision
        detail = {"wins": g.wins, "losses": g.losses, "ties": g.ties,
                  "anchor_losses": g.anchor_losses, "utility_delta": g.utility_delta,
                  "complexity": ev.complexity, "cost": ev.cost,
                  "comparisons": [asdict(c) for c in ev.comparisons],
                  "unusable": [{"id": i, "reason": r} for i, r in ev.unusable], **ev.extra}
        with self.pool.connection() as conn:
            return harness_rsi.record_gate(conn, self.user_id, self.round_id,
                                           SUBJECT.format(cid=cid), g.verdict, g.reasons, detail,
                                           now=self.now)


@dataclass(frozen=True)
class _Ev:
    decision: gate_mod.GateDecision
    comparisons: tuple[Comparison, ...]
    unusable: tuple[tuple[str, str], ...]
    cost: float | None
    complexity: int
    extra: Mapping[str, Any]


def _patched(case: Case, out: Mapping[str, Any],
             unusable: list[tuple[str, str]]) -> ReplayResult | None:
    """The child's output for `case` as a ReplayResult (cost 0: the parent counted the child's
    calls in its `cost_usd`), or None with the reason added to `unusable`."""
    if out.get("case_id") != case.case_id:
        unusable.append((case.case_id, BAD_OUTPUT))
        return None
    if "error" in out:
        unusable.append((case.case_id, REPLAY_FAILED.format(e=plain(str(out["error"])))))
        return None
    text, writer, run_id = out.get("text"), out.get("writer_provider"), out.get("run_id")
    if not (isinstance(text, str) and text.strip() and isinstance(writer, str) and writer
            and isinstance(run_id, str) and run_id):
        unusable.append((case.case_id, BAD_OUTPUT))
        return None
    return ReplayResult(case_id=case.case_id, text=text, writer_provider=writer, run_id=run_id,
                        cost=0.0)
