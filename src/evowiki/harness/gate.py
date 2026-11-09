"""The gates over the judge's comparisons. Orientation: `a` is the candidate text, `b` the current-pack
reference, so a loss is winner "b". A flipped comparison is already a tie, so every loss is a clear
loss.

`check` is the first step of the harness round's gate (`round`, step 5; `harness.evidence` is the
judged part after it). A smoke check, not a proof. The candidate pack rewrites a section or two (`anchors.SMOKE_CASES`); the change is
applied when it wrote every one it was asked to (a case it could not write is `broken`, and one fails
it) and the composition cites its evidence. Nothing is judged: the live A/A run showed the judge picks
the longer text 9 times in 10 and splits identical packs into wins and losses, so a win count over a
handful of cases is noise, not worth the replays of the current pack it needs. Protection after the apply is the user's undo (a lesson)
and `observe`'s rollback.

`layers` is the round's last word: code on whether the task got done, the judge on the content.

`decide` is the stricter evidence rule (spec §5.6), kept for forge code patches
(`patch_gate`), whose pass only hands the patch to the user's review."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from evowiki.harness.anchors import LAMBDA, MIN_CASES, WIN_MARGIN
from evowiki.harness.judge import Comparison


@dataclass(frozen=True)
class GateDecision:
    verdict: Literal["pass", "thin", "fail"]
    reasons: tuple[str, ...]
    wins: int
    losses: int
    ties: int
    anchor_losses: int
    utility_delta: float


def decide(
    problem: Sequence[Comparison],
    anchors: Sequence[Comparison],
    *,
    complexity: float,
    cited: bool,
) -> GateDecision:
    wins = sum(c.winner == "a" for c in problem)
    losses = sum(c.winner == "b" for c in problem)
    ties = len(problem) - wins - losses
    anchor_losses = sum(c.winner == "b" for c in anchors)
    delta = wins - losses - LAMBDA * complexity

    fail: list[str] = []
    thin: list[str] = []
    if losses:
        fail.append(f"有 {losses} 段明确输给现行版本")
    if anchor_losses:
        fail.append(f"锚点样例输了 {anchor_losses} 段")
    if len(problem) < MIN_CASES:
        thin.append(f"问题样例不足：{len(problem)} 段，至少需要 {MIN_CASES} 段")
    if wins < WIN_MARGIN:
        thin.append(f"赢的段数不足：{wins} 段，至少需要 {WIN_MARGIN} 段")
    if wins < losses + WIN_MARGIN:
        fail.append(f"赢 {wins} 段、输 {losses} 段，差距不到 {WIN_MARGIN} 段")
    if delta <= 0:
        fail.append(f"净收益 {delta:.2f} 不大于 0")
    if not cited:
        fail.append("改动没有引用证据")

    if losses or anchor_losses:
        verdict = "fail"
    elif thin:
        verdict = "thin"
    elif fail:
        verdict = "fail"
    else:
        verdict = "pass"
    reasons = tuple(fail + thin) or ("证据充分，通过",)
    return GateDecision(verdict, reasons, wins, losses, ties, anchor_losses, delta)


@dataclass(frozen=True)
class TaskLayer:
    """What code knows of the task, from real turns graded by code (never a replay). `outcome`:
    restored (cases fixed past the sign test, none broken), broken (a confirmed new failure),
    unchanged, unknown (a run that did not finish or cannot be compared), not_run. `says`: in words."""

    outcome: Literal["restored", "broken", "unchanged", "unknown", "not_run"]
    says: str = ""


NOT_RUN = "没有跑真实回归"
ACTS_UNPROVEN = "重放里的动作变了（{what}），重放不执行写操作，只有真实回归修好才算证明，证据不足"
WORSE = "任务修好了，但内容输给现行版本"


def layers(task: TaskLayer, content: GateDecision, *, acted: Sequence[str] = ()) -> GateDecision:
    """The gate's verdict from its two layers. A task code confirmed failing never wins on its words;
    a replay that acts differently proves nothing until real turns do; with the task settled, the
    judge decides on content, and its loss on content stands against a task restored."""
    if task.outcome == "broken":
        verdict: Literal["pass", "thin", "fail"] = "fail"
        why = task.says
    elif task.outcome == "unknown":
        verdict, why = "thin", f"证据不足：{task.says}"
    elif task.outcome == "restored":
        lost = content.verdict == "fail" and content.losses > content.wins
        verdict, why = ("fail", WORSE) if lost else ("pass", task.says)
    elif content.verdict == "fail":
        verdict, why = "fail", "；".join(content.reasons)
    elif acted:
        verdict, why = "thin", ACTS_UNPROVEN.format(what="、".join(acted))
    else:
        verdict, why = content.verdict, "；".join(content.reasons)
    first = f"第一层（代码判任务，真实回归）：{task.says or NOT_RUN}"
    second = f"第二层（裁判比内容，重放）：{'；'.join(content.reasons)}"
    return GateDecision(verdict, (why, first, second), content.wins, content.losses, content.ties,
                        content.anchor_losses, content.utility_delta)


def check(written: int, *, broken: Sequence[tuple[str, str]], complexity: float,
          cited: bool) -> GateDecision:
    """The round's smoke check (module doc). `written`: the touched cases the candidate pack wrote;
    `broken`: (case id, reason) of every one it could not write while the current pack could.
    Verdicts: fail on a broken case or no citation; thin when nothing could be written at all;
    else pass. Nothing is judged, so the win / loss fields are 0."""
    fail: list[str] = []
    if broken:
        fail.append(f"新设置写不出 {len(broken)} 段：{broken[0][1]}")
    if not cited:
        fail.append("改动没有引用证据")
    if fail:
        verdict: Literal["pass", "thin", "fail"] = "fail"
    elif not written:
        verdict, fail = "thin", ["没有一段能拿来重写"]
    else:
        verdict, fail = "pass", [f"新设置写出了 {written} 段"]
    return GateDecision(verdict, tuple(fail), 0, 0, 0, 0, -LAMBDA * complexity)
