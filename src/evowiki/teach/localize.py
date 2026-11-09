"""Failure localization (plan rev 3.3 §10.4; written for the retired RSI-B, now read by the harness
loop's cases and judge, `harness.cases` / `harness.judge`): every signal is traced through the SectionLog
of the section it was given on (the log of that page / section in force when the signal came) to the
failure class and to the spec item or RepParams path that most plausibly caused it.

- Chips and section buttons map to teaching-spec failure classes (`CHIP_CLASS`, `KIND_CLASS`).
- A difficulty complaint on a section whose cells were all 确定会 / 可能会 when it was written means
  K over-estimated the learner: that is RSI-A's (target="k_estimate"), never a representation patch.
  The same for 「再展开」 on a folded section, and for 「这部分我懂」 on a section K thought weak.
- Within a failure class, an item scores by how many failing sections it was in force on, minus half
  of the sections with a positive signal it was in force on, times (1 + its keyword affinity to the
  class). Only an item with affinity can be blamed; otherwise a mapped param, otherwise "new" (the
  proposer adds an item).
- A failure class becomes a finding when its signals' votes reach `min_signals`; each signal votes
  `signal_weights.get(kind, 1.0)` (no weights = every signal votes 1).
  Findings are ranked by their votes.
Pure: no IO, no LLM."""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from evowiki.teach.contracts import (
    CHIP_LABEL,
    FailureClass,
    FeedbackChip,
    Localization,
    RepParams,
    SectionLog,
    Signal,
    SpecItem,
    TeachingSpec,
    rsi_may_change,
)

MIN_SIGNALS = 2                       # one complaint alone never starts a patch
NEW_ITEM = "new"
CONFIDENT_BANDS = frozenset({"known", "likely"})
WEAK_BANDS = frozenset({"shaky", "gap"})

CHIP_CLASS: dict[FeedbackChip, FailureClass] = {
    "unclear": "unclear", "term_before_explained": "unclear",   # plan: 术语先用后讲 is `unclear`
    "too_long": "too_long", "no_example": "no_example", "too_shallow": "too_shallow",
    "too_hard": "too_hard"}
KIND_CLASS: dict[str, FailureClass] = {
    "section_deeper": "too_shallow", "section_other": "misframed",
    "assistant_selection": "unclear", "check_unclear": "unclear"}
# not here: `note_edit`. A hand edit may be his own example or a fix to a word; it says the text is
# now his, not that the teaching failed, so it is never a complaint.
# Complaints that mean "this was over my head / skipped something I lack": K's fault when the
# section's cells were all confident at writing time.
_DIFFICULTY_CHIPS = frozenset({"too_hard", "too_shallow", "term_before_explained"})
_DIFFICULTY_KINDS = frozenset({"section_deeper"})
POSITIVE_KINDS = frozenset({"section_know"})
# about the assistant's answer, not a page section: never traced to a section log
NOT_PAGE_KINDS = frozenset({"assistant_rejected", "assistant_rated", "user_complaint"})
# review-card feedback (B4) carries its kind as the text's leading label (wikiapp.review_cards);
# 答案不对 is a content error and 不想再看到 a preference: neither is a teaching-style failure
CARD_UNCLEAR = "题目没说清楚"
CARD_TOO_EASY = "太简单了"

CLASS_ZH: dict[str, str] = {
    "unclear": "讲得不清楚", "too_long": "太长", "no_example": "例子不够", "too_shallow": "太浅",
    "too_hard": "太难", "misframed": "讲法不对路", "prereq_omitted": "先修没讲",
    "redundant": "重复", "missed_connection": "漏了联系", "hub_rejected": "枢纽页被拒",
    "navigation_cost": "不好找", "overconfident": "高估了掌握程度",
    "underconfident": "低估了掌握程度"}
CLASS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "unclear": ("术语", "先讲", "定义", "人话", "口语", "直白", "通俗", "解释", "抽象", "专业",
                "名词", "简练", "精炼", "书面", "新词", "第一次出现", "陌生"),
    "too_long": ("简洁", "长", "字数", "篇幅", "详细", "完整", "全面", "推导", "展开"),
    "no_example": ("例子", "例题", "举例", "示例", "具体", "数值", "抽象"),
    "too_shallow": ("深", "推导", "原理", "边界", "简洁", "一句话", "省略", "结论"),
    "too_hard": ("难", "推导", "形式化", "严格", "公式", "证明", "符号", "一步"),
    "misframed": ("类比", "角度", "切入", "顺序", "从", "先", "直觉"),
    "prereq_omitted": ("先修", "前置", "术语", "定义", "先讲"),
    "redundant": ("重复", "提醒", "回顾", "再讲"),
}
# Fallback when no item is to blame: the param whose move most plausibly helps, and which way.
CLASS_PARAM: dict[str, tuple[str, int]] = {
    "no_example": ("fading_level", -1), "too_hard": ("fading_level", -1),
    "redundant": ("recap_max_per_page", -1), "misframed": ("analogy_level", 1)}


@dataclass(frozen=True)
class Finding:
    """One localization plus what the Gate and the proposer need: the signals themselves and the
    (page, section) pairs they were given on."""

    localization: Localization
    signals: tuple[Signal, ...]
    sections: tuple[tuple[str, str], ...]
    param_direction: int = 0                        # rep_param: the suggested direction


@dataclass(frozen=True)
class Localized:
    b: tuple[Finding, ...] = ()                     # strongest first
    a: tuple[Finding, ...] = ()                     # hand-offs to RSI-A (target="k_estimate")
    unplaced: tuple[str, ...] = field(default=())   # signal ids with no section log to trace


def _log_for(sig: Signal, logs: Sequence[SectionLog]) -> list[SectionLog]:
    """Logs in force for the signal: same page (and section, when the signal names one), written
    at or before the signal, at its spec version when known; latest per section."""
    best: dict[str, SectionLog] = {}
    for lg in logs:
        if lg.page_id != sig.page_id or lg.at > sig.at:
            continue
        if sig.section_id is not None and lg.section_id != sig.section_id:
            continue
        if sig.spec_version is not None and lg.spec_version != sig.spec_version:
            continue
        cur = best.get(lg.section_id)
        if cur is None or lg.at > cur.at:
            best[lg.section_id] = lg
    found = list(best.values())
    if sig.section_id is None:                      # page-level feedback: the teaching sections
        taught = [lg for lg in found if lg.role in ("explain", "compare")]
        found = sorted(taught or found, key=lambda lg: lg.at)[:2]
    return found


def _confident(lg: SectionLog) -> bool:
    return bool(lg.cells) and all(lg.bands.get(c) in CONFIDENT_BANDS for c in lg.cells)


def _weak(lg: SectionLog) -> bool:
    return bool(lg.cells) and all(lg.bands.get(c) in WEAK_BANDS for c in lg.cells)


def classes_of(sig: Signal) -> list[tuple[FailureClass, bool]]:
    """(B failure class, is-a-difficulty-complaint) for each complaint the signal carries."""
    out: list[tuple[FailureClass, bool]] = []
    for chip in dict.fromkeys(sig.chips):
        out.append((CHIP_CLASS[chip], chip in _DIFFICULTY_CHIPS))
    if sig.kind in KIND_CLASS and not (sig.kind == "assistant_selection" and sig.chips):
        out.append((KIND_CLASS[sig.kind], sig.kind in _DIFFICULTY_KINDS))
    if sig.kind == "page_feedback" and not sig.chips and sig.text.strip():
        out.append(("misframed", False))         # his own words: the box has no button to class them
    if sig.kind == "card_feedback" and sig.text.startswith(CARD_UNCLEAR):
        out.append(("unclear", False))           # a recall cue / check worded unclearly
    return out


def _card_too_easy(sig: Signal) -> bool:
    return sig.kind == "card_feedback" and sig.text.startswith(CARD_TOO_EASY)


def affinity(item: SpecItem, failure: str) -> int:
    return sum(1 for kw in CLASS_KEYWORDS.get(failure, ()) if kw in item.text)


def _quote(sig: Signal) -> str:
    parts = [CHIP_LABEL[c] for c in sig.chips]
    if sig.text.strip():
        parts.append(f"「{sig.text.strip()[:40]}」")
    return "、".join(parts) or sig.kind


def _a_finding(failure: FailureClass, pairs: list[tuple[Signal, SectionLog]]) -> Finding:
    sigs = tuple(dict.fromkeys(s for s, _ in pairs))
    cells = sorted({c for _, lg in pairs for c in lg.cells})
    secs = tuple(dict.fromkeys((lg.page_id, lg.section_id) for _, lg in pairs))
    loc = Localization(
        track="A", failure=failure, target="k_estimate", ref=",".join(cells)[:120] or "?",
        signals=tuple(s.signal_id for s in sigs),
        reason=(f"{len(sigs)} 条信号落在系统认为他{'已会' if failure == 'overconfident' else '不会'}"
                f"的段落上：K {CLASS_ZH[failure]}，交给 RSI-A，不改讲法")[:600])
    return Finding(loc, sigs, secs)


def votes(signals: Iterable[Signal], weights: Mapping[str, float] | None) -> float:
    return sum((weights or {}).get(s.kind, 1.0) for s in signals)


def localize(signals: Iterable[Signal], logs: Sequence[SectionLog],
             spec: TeachingSpec, *, min_signals: int = MIN_SIGNALS,
             signal_weights: Mapping[str, float] | None = None) -> Localized:
    """`min_signals` 1: every complaint counts (the harness cases), not only a pile of them.
    `signal_weights`: SignalKind → vote multiplier."""
    by_class: dict[str, list[tuple[Signal, SectionLog]]] = defaultdict(list)
    to_a: dict[str, list[tuple[Signal, SectionLog]]] = defaultdict(list)
    positive: list[SectionLog] = []
    unplaced: list[str] = []
    for sig in signals:
        if sig.page_id is None or sig.kind in NOT_PAGE_KINDS:
            continue
        found = _log_for(sig, logs)
        if not found:
            if classes_of(sig) or sig.kind in ("expand", *POSITIVE_KINDS):
                unplaced.append(sig.signal_id)
            continue
        if _card_too_easy(sig):                      # 「太简单了，别再考」: K under-estimated him
            to_a["underconfident"].extend((sig, lg) for lg in found if _weak(lg) or not lg.cells)
            continue
        if sig.kind in POSITIVE_KINDS:
            positive.extend(found)
            to_a["underconfident"].extend((sig, lg) for lg in found if _weak(lg))
            continue
        if sig.kind == "expand":                     # re-opening what was folded as known
            to_a["overconfident"].extend((sig, lg) for lg in found if _confident(lg))
            continue
        for failure, difficulty in classes_of(sig):
            for lg in found:
                if difficulty and _confident(lg):
                    to_a["overconfident"].append((sig, lg))
                else:
                    by_class[failure].append((sig, lg))
    b = [f for f in (_b_finding(fc, pairs, positive, spec, min_signals, signal_weights)
                     for fc, pairs in by_class.items()) if f is not None]
    b.sort(key=lambda f: (-votes(f.signals, signal_weights), -len(f.signals),
                          f.localization.failure))
    a = [_a_finding(fc, pairs) for fc, pairs in to_a.items()  # type: ignore[arg-type]
         if pairs]
    return Localized(tuple(b), tuple(a), tuple(unplaced))


def _b_finding(failure: str, pairs: list[tuple[Signal, SectionLog]], positive: list[SectionLog],
               spec: TeachingSpec, min_signals: int = MIN_SIGNALS,
               signal_weights: Mapping[str, float] | None = None) -> Finding | None:
    sigs = tuple(dict.fromkeys(s for s, _ in pairs))
    if not sigs or votes(sigs, signal_weights) + 1e-9 < min_signals:     # float sums
        return None
    secs = tuple(dict.fromkeys((lg.page_id, lg.section_id) for _, lg in pairs))
    fail_hits: Counter[str] = Counter()
    for lg in {(lg.page_id, lg.section_id): lg for _, lg in pairs}.values():
        fail_hits.update(set(lg.spec_items))
    ok_hits: Counter[str] = Counter()
    for lg in positive:
        ok_hits.update(set(lg.spec_items))
    scored: list[tuple[float, int, str]] = []
    for item in spec.items:
        if not rsi_may_change(item):         # not RSI's to rewrite (B3, rule provenance)
            continue
        aff = affinity(item, failure)
        if aff == 0 or fail_hits[item.item_id] == 0:
            continue
        score = (fail_hits[item.item_id] - 0.5 * ok_hits[item.item_id]) * (1 + aff)
        if score > 0:
            scored.append((score, aff, item.item_id))
    chips = Counter(q for s in sigs for q in ([CHIP_LABEL[c] for c in s.chips] or [s.kind]))
    said = "、".join(f"{k}×{n}" for k, n in chips.most_common())
    head = f"{len(sigs)} 条反馈（{said}）落在 {len(secs)} 段，{CLASS_ZH.get(failure, failure)}"
    direction = 0
    if scored:
        scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
        ref = scored[0][2]
        item = next(i for i in spec.items if i.item_id == ref)
        target = "spec_item"
        reason = f"{head}；这些段生效的条目 {ref}「{item.text[:80]}」与问题最相关"
    elif failure in CLASS_PARAM and _can_move(spec, *CLASS_PARAM[failure]):
        ref, direction = CLASS_PARAM[failure]
        target = "rep_param"
        reason = (f"{head}；没有相关条目，参数 {ref}（现为 {getattr(spec.params, ref)}）"
                  f"{'调低' if direction < 0 else '调高'}可能有帮助")
    else:
        ref, target = NEW_ITEM, "spec_item"
        reason = f"{head}；生效的条目里没有管这件事的，需要新增一条"
    loc = Localization(track="B", failure=failure, target=target, ref=ref,  # type: ignore[arg-type]
                       signals=tuple(s.signal_id for s in sigs), reason=reason[:600])
    return Finding(loc, sigs, secs, direction)


def _can_move(spec: TeachingSpec, path: str, direction: int) -> bool:
    field_info = RepParams.model_fields[path]
    lo = next(m.ge for m in field_info.metadata if hasattr(m, "ge"))
    hi = next(m.le for m in field_info.metadata if hasattr(m, "le"))
    v = getattr(spec.params, path) + direction
    return lo <= v <= hi


def quotes(signals: Iterable[Signal], n: int = 5) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_quote(s) for s in signals))[:n]


DEPTH_WORDS = (CHIP_LABEL["too_shallow"], CHIP_LABEL["no_example"])       # 太浅 / 例子不够


def asked_for_more(quotes: Sequence[str]) -> bool:
    """The user's quotes asked for depth or examples (太浅 / 例子不够): a longer text may be right."""
    return any(w in q for q in quotes for w in DEPTH_WORDS)
