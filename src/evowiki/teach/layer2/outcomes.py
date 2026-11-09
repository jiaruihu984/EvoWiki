"""Long-term outcome of one applied RSI change on the fixed anchors (RSI design §3, `teach.anchors`).

- Track A (`outcome_a`): the change's old vs new θ_infer predict the same outcomes dated after it
  was applied (and not after `now`): paired log loss and paired AUC.
- The harness loop's changes are measured in `harness.observe` (its own anchors; REACTION_KINDS:
  know / deeper / other / page feedback / expand, never check answers or review grades). Track B's
  own outcome (RSI-B) retired with it on 2026-10-29.
- Layer 2 (`outcome_method`): the children of a method version (changes proposed under it) vs the
  children of the version before: child_helpful = share of decided children judged helpful,
  undo_rate = share of children the user undid.

Every delta is oriented so + is better; the verdict is `anchors.judge` (pending below MIN_EVENTS
per side, Bonferroni-corrected over the metrics that can decide, no_effect when inconclusive). Pure functions: no I/O; bootstrap seeds come from the change
id, so the same data give the same outcome."""

import zlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from evowiki.belief.aspects import AspectConfig, AspectEvent
from evowiki.i18n import current_language
from evowiki.teach import anchors
from evowiki.teach.contracts import (
    AnchorDelta,
    AnchorMetric,
    InferParams,
    OutcomeOut,
    Verdict,
)
from evowiki.teach.predict import predict_outcomes


@dataclass(frozen=True)
class ChangeRef:
    change_id: str
    layer: int                       # 0 = an A patch, 2 = an A2 method change
    kind: str                        # "A:fit" | "A:set_param" | "A2:<what>"
    applied_at: datetime
    method_version: int | None       # FitModel version in force when it was proposed
    signal_kinds: tuple[str, ...]    # kinds of the triggering signals; () for A


METRIC_LABEL: Mapping[AnchorMetric, str] = {
    "pred_logloss": "预测误差", "pred_auc": "预测区分度", "check_correct": "确认题正确率",
    "unclear_rate": "「看不懂」比例", "review_recall": "复习时记得的比例",
    "feedback_negative": "负面反馈比例", "feedback_too_shallow": "「太浅」反馈比例",
    "feedback_too_hard": "「太难」反馈比例", "undo_rate": "撤销率", "child_helpful": "改动有效的比例",
    "page_cost": "每页花费", "page_latency": "每页耗时",
    "assistant_down_rate": "助手回答被点踩的比例"}
_RAW = frozenset({"pred_logloss", "pred_auc"})


def _seed(change_id: str) -> int:
    return zlib.crc32(change_id.encode("utf-8"))


def _fmt(metric: AnchorMetric, x: float | None) -> str:
    if x is None:
        return "—"
    if metric == "page_cost":
        return f"${x:.3f}"            # DeepSeek pages cost cents
    if metric == "page_latency":
        return f"{x:.0f} 秒"
    return f"{x:.2f}" if metric in _RAW else f"{x:.0%}"


def note(v: Verdict, deltas: Sequence[AnchorDelta]) -> str:
    """One plain-Chinese line for the 参数 tab."""
    if v == "pending":
        return f"还在观察：改动后的相关记录还不够（每项至少 {anchors.MIN_EVENTS} 条）"
    n = max((d.n_after for d in deltas), default=0)
    decided = _decided(deltas)
    if v == "no_effect" or not decided:
        return f"看不出差别（改动后 {n} 条记录）"
    if v == "harmful":
        d = min(decided, key=lambda x: x.hi)          # type: ignore[arg-type, return-value]
        word = "变差"
    else:
        d = max(decided, key=lambda x: x.lo)          # type: ignore[arg-type, return-value]
        word = "变好"
    return (f"改动后{METRIC_LABEL[d.metric]}{word}了（{_fmt(d.metric, d.before)} → "
            f"{_fmt(d.metric, d.after)}，改动后 {d.n_after} 条记录）")


# effect_line's wording of each metric (「之后看不懂的比例 30% → 12%」)
EFFECT_LABEL: Mapping[AnchorMetric, str] = {
    "pred_logloss": "预测误差", "pred_auc": "预测区分度", "check_correct": "确认题正确率",
    "unclear_rate": "看不懂的比例", "review_recall": "复习时记得的比例",
    "feedback_negative": "负面反馈的比例", "feedback_too_shallow": "说「太浅」的比例",
    "feedback_too_hard": "说「太难」的比例", "undo_rate": "撤销的比例", "child_helpful": "改动有效的比例",
    "page_cost": "每页花费", "page_latency": "每页耗时",
    "assistant_down_rate": "助手回答被点踩的比例"}


def _decided(deltas: Sequence[AnchorDelta]) -> list[AnchorDelta]:
    return [d for d in deltas if d.lo is not None and d.hi is not None
            and d.n_before >= anchors.MIN_EVENTS and d.n_after >= anchors.MIN_EVENTS]


# Notes of the points written when a change stops being applied (or could not be): who rolled it
# back. `effect_line` reads them; they are never shown as such.
NOTE_USER_UNDO = "user_undo"                       # the user's 撤销
NOTE_AUTO_ROLLBACK = "auto_rollback"               # confirmed harmful, rolled back automatically
NOTE_ROLLBACK_BLOCKED = "rollback_blocked_by_newer"   # confirmed harmful, a newer change holds it


def undone_by_user(points: Sequence[OutcomeOut], *, legacy_harmful: bool) -> bool:
    """Whether the user (not the automatic rollback) undid a rolled-back change: its last point's
    note says so; a change rolled back before those notes existed falls back to its last verdict
    (`legacy_harmful`: a harmful one was the automatic rollback)."""
    note = points[-1].note if points else ""
    if note == NOTE_USER_UNDO:
        return True
    if note == NOTE_AUTO_ROLLBACK:
        return False
    return not legacy_harmful


# What a change's after-side evidence counts (measure word, noun), by change kind: track A
# counts predicted outcomes (questions), a teaching-rule change (the harness loop's rows,
# `harness.change_source`) the signals after it, layer 2 the finished changes proposed under its
# method version.
UNIT_A: tuple[str, str] = ("道", "相关的题")
UNIT_B: tuple[str, str] = ("条", "相关记录")
UNIT_METHOD: tuple[str, str] = ("个", "后续改动")


def collecting_line(o: OutcomeOut, n_now: int | None, unit: tuple[str, str] = UNIT_A) -> str:
    """The effect line of an applied change whose verdict is still pending. `n_now` = the cheap
    count of its after-side evidence now (`method_after_count` / track A's
    outcomes after it; None = not known, e.g. its count failed): below MIN_EVENTS
    「数据还在收集：之后 3 道相关的题，满 10 道再判断」; at or above it, 「…，下次检查时判断」, or, when its
    last point already had that much after-side evidence but too little before it,
    「…，改动前的记录还不够比较」. Never pretends to count: unknown → 「数据还在收集：满 10 道相关的题再判断」."""
    mw, noun = unit
    m = anchors.MIN_EVENTS
    if n_now is None:
        return f"数据还在收集：满 {m} {mw}{noun}再判断"
    head = f"数据还在收集：之后 {n_now} {mw}{noun}"
    if n_now < m:
        return f"{head}，满 {m} {mw}再判断"
    if any(d.n_after >= m and d.n_before < m for d in o.deltas):
        return f"{head}，改动前的记录还不够比较"
    return f"{head}，下次检查时判断"


def effect_line(o: OutcomeOut, *, rolled_back: bool = False, n_now: int | None = None,
                unit: tuple[str, str] = UNIT_A) -> str:
    """One plain-Chinese line for a change's latest outcome point (the 参数 change list):
    pending `collecting_line` (with `n_now` / `unit`; without `n_now` the point's own largest
    n_after) 「数据还在收集：之后 7 道相关的题，满 10 道再判断」; helpful 「之后 24 道确认题：预测误差 0.41 → 0.35，有效」 /
    「之后看不懂的比例 30% → 12%，有效」; harmful 「…，变差，还在确认」 (one harmful point never rolls a
    change back); no_effect 「看不出差别」. The metric shown is the decisive one (helpful: highest
    lower bound; harmful: lowest upper).
    A change no longer applied never reads 「数据还在收集」: the user's undo (note NOTE_USER_UNDO) reads
    「你撤销了」; the automatic rollback (NOTE_AUTO_ROLLBACK) 「…，变差，已自动撤回」; any other
    rolled-back change (`rolled_back`, rolled back before those notes) its last measurement and
    「已撤销」. A harmful change whose rollback a newer change blocks (NOTE_ROLLBACK_BLOCKED):
    「…，变差；之后的改动改了同样的地方，没有自动撤回」."""
    if current_language() == "en":
        return _english_effect(o, rolled_back=rolled_back, n_now=n_now, unit=unit)
    if o.note == NOTE_USER_UNDO:
        return "你撤销了"
    auto = o.note == NOTE_AUTO_ROLLBACK
    after = "，已自动撤回" if auto else "，已撤销" if rolled_back else ""
    if o.verdict == "pending":
        if auto or rolled_back:
            return after.lstrip("，") + "，不再观察"
        if n_now is None and o.deltas:
            n_now = max(d.n_after for d in o.deltas)
        return collecting_line(o, n_now, unit)
    decided = _decided(o.deltas)
    if o.verdict == "no_effect" or not decided:
        return "看不出差别" + after
    if o.verdict == "harmful":
        d = min(decided, key=lambda x: x.hi)          # type: ignore[arg-type, return-value]
        if auto or rolled_back:
            end = "变差" + after
        elif o.note == NOTE_ROLLBACK_BLOCKED:
            end = "变差；之后的改动改了同样的地方，没有自动撤回"
        else:
            end = "变差，还在确认"
    else:
        d = max(decided, key=lambda x: x.lo)          # type: ignore[arg-type, return-value]
        end = "有效" + after
    move = f"{EFFECT_LABEL[d.metric]} {_fmt(d.metric, d.before)} → {_fmt(d.metric, d.after)}"
    if d.metric in _RAW:
        return f"之后 {d.n_after} 道确认题：{move}，{end}"
    return f"之后{move}，{end}"


def _out(ref: ChangeRef, metrics: Sequence[anchors.Sampled], now: datetime) -> OutcomeOut:
    deltas, v = anchors.judge(metrics)
    return OutcomeOut(change_id=ref.change_id, at=now, verdict=v, deltas=tuple(deltas),
                      note=note(v, deltas))


# ---- track A ----

def outcome_a(ref: ChangeRef, events: Sequence[AspectEvent], base: AspectConfig,
              domain_of: Mapping[str, str], old: InferParams, new: InferParams, *,
              now: datetime) -> OutcomeOut:
    """pred_logloss + pred_auc of `new` vs `old` on the outcomes after `ref.applied_at` (paired)."""
    since = ref.applied_at

    def after(infer: InferParams) -> dict:
        return {o.event_id: o for o in predict_outcomes(events, base, infer, domain_of)
                if since < o.ts <= now}
    o_old, o_new = after(old), after(new)
    ids = sorted(o_old.keys() & o_new.keys(), key=lambda i: (o_old[i].ts, i))
    pairs_old = [(o_old[i].p, o_old[i].y) for i in ids]
    pairs_new = [(o_new[i].p, o_new[i].y) for i in ids]
    seed = _seed(ref.change_id)
    metrics = (anchors.logloss_sampled(pairs_old, pairs_new, seed=seed),
               anchors.auc_sampled(pairs_old, pairs_new, seed=seed + 1))
    return _out(ref, metrics, now)


# ---- the reader's reactions (the harness loop's page outcome, `harness.observe`) ----

REACTION_KINDS = frozenset({"section_know", "section_deeper", "section_other", "page_feedback",
                            "expand"})


# ---- layer 2 ----

def _helpful(xs: Sequence[OutcomeOut]) -> list[float]:
    return [1.0 if o.verdict == "helpful" else 0.0 for o in xs if o.verdict != "pending"]


def _undo(xs: Sequence[OutcomeOut], undone: Collection[str],
          undo_among: Collection[str] | None) -> list[float]:
    return [1.0 if o.change_id in undone else 0.0 for o in xs
            if undo_among is None or o.change_id in undo_among]


def method_after_count(child_outcomes: Sequence[OutcomeOut],
                       undo_among: Collection[str] | None = None) -> int:
    """What `outcome_method` would report as its largest n_after, without the bootstraps."""
    return max(len(_helpful(child_outcomes)), len(_undo(child_outcomes, (), undo_among)))


def outcome_method(ref: ChangeRef, child_outcomes: Sequence[OutcomeOut],
                   baseline: Sequence[OutcomeOut], *, now: datetime,
                   undone: Collection[str] = (),
                   undo_among: Collection[str] | None = None) -> OutcomeOut:
    """`child_outcomes` = latest outcomes of the changes proposed under the new method version,
    `baseline` = those under the previous one (a pending one is left out of child_helpful);
    `undone` = change ids the user undid; `undo_among` = the ids undo_rate is computed over (the
    applied children; None = all)."""
    seed = _seed(ref.change_id)
    metrics = (anchors.rate_sampled("child_helpful", _helpful(baseline), _helpful(child_outcomes),
                                    higher_is_better=True, seed=seed),
               anchors.rate_sampled("undo_rate", _undo(baseline, undone, undo_among),
                                    _undo(child_outcomes, undone, undo_among),
                                    higher_is_better=False, seed=seed + 1))
    return _out(ref, metrics, now)


_METRIC_EN = {
    "pred_logloss": "prediction error", "pred_auc": "prediction discrimination",
    "check_correct": "confirmation accuracy", "unclear_rate": "unclear feedback rate",
    "review_recall": "review recall rate", "feedback_negative": "negative feedback rate",
    "feedback_too_shallow": "too-shallow feedback rate", "feedback_too_hard": "too-hard feedback rate",
    "undo_rate": "undo rate", "child_helpful": "helpful change rate", "page_cost": "cost per page",
    "page_latency": "time per page", "assistant_down_rate": "assistant dislike rate",
}


def _english_effect(o: OutcomeOut, *, rolled_back: bool, n_now: int | None,
                    unit: tuple[str, str]) -> str:
    if o.note == NOTE_USER_UNDO:
        return "You undid this change."
    auto = o.note == NOTE_AUTO_ROLLBACK
    suffix = "; automatically rolled back" if auto else "; undone" if rolled_back else ""
    if o.verdict == "pending":
        if auto or rolled_back:
            return ("Automatically rolled back" if auto else "Undone") + "; observation ended."
        count = n_now if n_now is not None else max((d.n_after for d in o.deltas), default=None)
        noun = "subsequent changes" if unit == UNIT_METHOD else "questions" if unit == UNIT_A else "records"
        if count is None:
            return f"Collecting data: at least {anchors.MIN_EVENTS} relevant {noun} are needed."
        head = f"Collecting data: {count} relevant {noun} after the change"
        if count < anchors.MIN_EVENTS:
            return head + f"; assess after {anchors.MIN_EVENTS}."
        if any(d.n_after >= anchors.MIN_EVENTS and d.n_before < anchors.MIN_EVENTS for d in o.deltas):
            return head + "; there are not enough records from before the change."
        return head + "; assess at the next check."
    decided = _decided(o.deltas)
    if o.verdict == "no_effect" or not decided:
        return "No clear difference" + suffix + "."
    harmful = o.verdict == "harmful"
    delta = min(decided, key=lambda d: d.hi) if harmful else max(decided, key=lambda d: d.lo)
    def number(value):
        if value is None:
            return "—"
        if delta.metric == "page_latency":
            return f"{value:.0f} s"
        return _fmt(delta.metric, value)
    change = f"{_METRIC_EN[delta.metric]} {number(delta.before)} → {number(delta.after)}"
    if harmful:
        end = "worse" + suffix if auto or rolled_back else (
            "worse; a later change affects the same setting, so automatic rollback was blocked"
            if o.note == NOTE_ROLLBACK_BLOCKED else "worse; awaiting confirmation")
    else:
        end = "helpful" + suffix
    return f"After {delta.n_after} records: {change}; {end}."
