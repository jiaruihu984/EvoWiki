"""Pure trigger rules (B3 spec §3): facts in, `Intent`s out. No DB access.

Rule ids, versions, priorities and reason strings are fixed by the plan. Whether a kind is
enabled is the gate's job (so `kind_off` stays a visible skip reason); `evaluate` runs every
rule of the requested mode. A rule that raises is logged and reported, never silently dropped.
"""

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from evowiki.bot.facts_types import GoalWeek, PageFacts, UserFacts, WrongAnswer
from evowiki.bot.settings import BotSettings, TriggerKind
from evowiki.teach.signal_kinds import HARMFUL_KINDS

log = logging.getLogger(__name__)

# Signals about the explanation itself (strategy_negative v2): the harmful signal kinds, card feedback
# (its negative texts) and 「我懂了」 (positive: it breaks a streak). Answer outcomes are not in it.
TEACHING_KINDS = HARMFUL_KINDS | {"card_feedback", "section_know"}

Route = Literal["direct", "rsi"]
Mode = Literal["event", "daily", "page", "weekly"]


@dataclass(frozen=True)
class Intent:
    kind: TriggerKind
    rule_id: str
    rule_version: int
    source_key: str
    dedup_key: str
    route: Route
    evidence: tuple[str, ...]
    payload: dict
    priority: int
    reason: str
    placement: Literal["today", "page"]
    page_id: str | None
    title: str = ""        # what the intent is about, shown in the 最近 list


@dataclass(frozen=True)
class RuleInput:
    facts: UserFacts
    new_event_ids: frozenset[str]
    settings: BotSettings
    now: datetime
    local_date: date
    mode: Mode
    page: PageFacts | None = None      # mode "page": the page being opened
    weeks: tuple[GoalWeek, ...] = ()   # mode "weekly": the active goals' week (goal_week)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: int
    kind: TriggerKind
    mode: Mode
    fn: Callable[[RuleInput], list[Intent]]


def _label(facts: UserFacts, page_id: str | None) -> str:
    """A page's title with its path position; never a raw id."""
    if page_id is None:
        return "未命名页面"
    return facts.labels.get(page_id) or facts.names.get(page_id) or "未命名页面"


def _review_due(inp: RuleInput) -> list[Intent]:
    d = inp.facts.due_review
    if d.count <= 0:
        return []
    return [Intent(
        kind="review_due", rule_id="review_due", rule_version=1,
        source_key=f"review_due:{inp.local_date}", dedup_key="review_due", route="direct",
        evidence=(), payload={"count": d.count, "cells": list(d.cells), "page_id": d.first_page_id},
        priority=50, reason=f"今天有 {d.count} 张卡片到期", placement="today",
        page_id=d.first_page_id, title=f"复习 {d.count} 张卡片")]


def _goal_idle(inp: RuleInput) -> list[Intent]:
    out: list[Intent] = []
    for g in inp.facts.goals:
        ref = g.last_opened or g.created_at
        age = inp.now - ref
        if age < timedelta(days=inp.settings.idle_days):
            continue
        d = int(age.total_seconds() // 86400)
        out.append(Intent(
            kind="goal_idle", rule_id="goal_idle", rule_version=1,
            source_key=f"goal_idle:{g.path_id}:{inp.local_date}",
            dedup_key=f"goal_idle:{g.path_id}", route="direct", evidence=(),
            payload={"path_id": g.path_id, "title": g.title, "page_id": g.next_page_id,
                     "days": d},
            priority=40, reason=f"目标「{g.title}」已经 {d} 天没打开相关页面",
            placement="today", page_id=g.next_page_id,
            title=g.title))
    return out


def _wrong_answers(inp: RuleInput) -> list[Intent]:
    """Different wrong questions about one cell; attempts never increase the question count.

    Events react promptly; the daily check can revisit evidence skipped while paused. The
    ordinary gate owns the switch, cap, cooldown and the learner's reminder preference.
    """
    by_cell: dict[str, dict[str, WrongAnswer]] = {}
    since = inp.now - timedelta(days=inp.settings.wrong_days)
    for answer in inp.facts.wrong_answers:
        if (not since <= answer.at <= inp.now
                or answer.page_id not in inp.facts.labels):
            continue
        by_cell.setdefault(answer.cell, {}).setdefault(answer.question, answer)
    out = []
    for cell, questions in by_cell.items():
        answers = sorted(questions.values(), key=lambda w: (w.at, w.event_id))
        if len(answers) < inp.settings.wrong_count:
            continue
        if inp.mode == "event" and not any(w.event_id in inp.new_event_ids for w in answers):
            continue
        newest = answers[-1]
        name = inp.facts.names.get(cell, "这部分内容")
        key = f"cell:{cell}"
        source = (f"{key}:{newest.event_id}" if inp.mode == "event"
                  else f"{key}:{inp.local_date}")
        out.append(Intent(
            kind="wrong_answers", rule_id="wrong_answers", rule_version=1,
            source_key=f"wrong_answers:{source}", dedup_key=key, route="direct",
            evidence=tuple(w.event_id for w in answers),
            payload={"cell": cell, "name": name, "count": len(answers),
                     "page_id": newest.page_id},
            priority=55, reason=f"{inp.settings.wrong_days} 天内，「{name}」有 {len(answers)} 道不同的题答错了",
            placement="today", page_id=newest.page_id, title=f"补讲：{name}"))
    return out


def _strategy_negative(inp: RuleInput) -> list[Intent]:
    """v2 (B5 spec §2.1): only signals about the explanation count (`TEACHING_KINDS`); answer
    outcomes neither count nor break the streak. Whether the three share one teaching approach is
    left to the RSI round."""
    last = [s for s in inp.facts.signals if s.kind in TEACHING_KINDS][-3:]
    if len(last) < 3 or any(s.polarity != -1 for s in last):
        return []
    if not any(s.event_id in inp.new_event_ids for s in last):
        return []
    ids = [s.event_id for s in last]
    return [Intent(
        kind="strategy_negative", rule_id="strategy_negative", rule_version=2,
        source_key=f"strategy:{ids[-1]}", dedup_key="strategy_negative", route="rsi",
        evidence=tuple(ids),
        payload={"signals": ids, "written_only": all(s.written for s in last)}, priority=30,
        reason="连续 3 次觉得讲解不合适，已开始调整讲解方式", placement="today",
        page_id=None, title="讲解方式调整")]


def _open_question(inp: RuleInput) -> list[Intent]:
    out: list[Intent] = []
    for q in inp.facts.open_questions:
        if q.page_opened_since or inp.now - q.at < timedelta(days=inp.settings.open_question_days):
            continue
        out.append(Intent(
            kind="open_question", rule_id="open_question", rule_version=1,
            source_key=f"open_q:{q.session_id}:{inp.local_date}",
            dedup_key=f"open_q:{q.session_id}", route="direct", evidence=(),
            payload={"page_id": q.page_id, "text": q.text, "session_id": q.session_id},
            priority=45, reason=f"上次在这一页还没懂：{q.text[:60]}", placement="today",
            page_id=q.page_id,
            title=" ".join(q.text.split())[:40]))
    return out


def chip_hash(text: str) -> str:
    """Stable short id of a chip's text (its cooldown key)."""
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def _chat(inp: RuleInput) -> list[Intent]:
    """The page's chat chips (spec §5 陪读面板): at most one intent per page and local day, its
    payload the chips (<= `page_cap`, deduplicated): the newest 还没懂的 of the page's sessions
    (no 「我答错过」 re-explain chips: the user removed them, 2026-10-15)."""
    page = inp.page
    if page is None:
        return []
    texts: list[str] = []
    if page.open_text:
        texts.append(f"上次还没懂：{page.open_text}，能再讲讲吗？")
    chips = list(dict.fromkeys(texts))[:inp.settings.page_cap]
    if not chips:
        return []
    return [Intent(
        kind="chat", rule_id="chat", rule_version=1,
        source_key=f"chat:{page.page_id}:{inp.local_date}", dedup_key=f"chat:{page.page_id}",
        route="direct", evidence=(), payload={"chips": [{"text": t} for t in chips]},
        priority=35, reason=f"这一页有 {len(chips)} 条可以接着聊的内容",
        placement="page", page_id=page.page_id, title=f"{page.label}的 {len(chips)} 条小建议")]


# --- weekly goal review (B5 spec §1.3) -----------------------------------------------------

Situation = Literal["prereq_gap", "prereq_missing", "stuck_page", "review_debt", "mostly_known",
                    "want_park", "normal"]
STUCK_ASK = "这一页我读了很久还是没弄懂，能换个讲法吗？"


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def situation(g: GoalWeek, s: BotSettings) -> tuple[Situation, dict]:
    """The one situation of a goal's week, by priority, with what it rests on (pure)."""
    for w in g.weak_prereqs:
        # only a prerequisite the path does not already teach in order; the edit goes right before
        # the page that needs it
        if w.taught in ("page", "missing") and w.before_key is not None:
            return (("prereq_gap" if w.taught == "page" else "prereq_missing"),
                    {"node_id": w.node_id, "name": w.name, "for_title": w.for_title,
                     "page_key": w.page_key, "page_title": w.page_title,
                     "before_key": w.before_key})
    if (g.reading_seconds is not None and g.stuck is not None
            and g.stuck.seconds >= s.weekly_stuck_minutes * 60):
        return "stuck_page", {"page_id": g.stuck.page_id, "page_title": g.stuck.title,
                              "minutes": g.stuck.seconds // 60}
    if (g.review_cap > 0 and g.review_due >= s.weekly_debt_ratio * g.review_cap
            and g.new_pages_this_week >= s.weekly_debt_pages):
        return "review_debt", {"due": g.review_due, "cap": g.review_cap}
    if (g.known_share is not None and g.known_share >= s.weekly_known_share
            and g.pages_shown - g.pages_done >= 2):
        return "mostly_known", {"share": round(g.known_share, 2)}
    if g.days_since_open >= s.weekly_park_days and g.idle_declines >= s.weekly_park_declines:
        return "want_park", {"days": g.days_since_open, "declines": g.idle_declines}
    return "normal", {}


def _week_line(g: GoalWeek) -> str:
    line = f"本周读完 {g.done_this_week} 页（共 {g.pages_shown} 页）"
    if g.reading_seconds is not None:
        line += f"，读了 {g.reading_seconds // 60} 分钟"
    return line


def _review_text(g: GoalWeek, sit: Situation, d: dict) -> tuple[str, str]:
    """(title, reason) of a goal_review card (templates, no LLM)."""
    base = _week_line(g)
    match sit:
        case "prereq_gap" | "prereq_missing":
            return (f"先补「{d['name']}」再往下",
                    f"{base}；「{d['for_title']}」要用到「{d['name']}」，它还没打牢")
        case "stuck_page":
            return (f"「{d['page_title']}」读了 {d['minutes']} 分钟还没学完，换个讲法？",
                    f"{base}；「{d['page_title']}」读了 {d['minutes']} 分钟还没学完")
        case "review_debt":
            return ("这周先复习，再读新页",
                    f"{base}；{d['due']} 张卡片到期，每天最多复习 {d['cap']} 张")
        case "mostly_known":
            return ("后面大半你已经会了，重新规划？",
                    f"{base}；剩下的内容里 {round(d['share'] * 100)}% 你已经会了")
        case "want_park":
            return (f"先把「{g.title}」搁置？",
                    f"「{g.title}」已经 {d['days']} 天没打开，继续的提醒你点过 {d['declines']} 次不感兴趣")
    raise ValueError(f"no card for situation {sit!r}")


def _goal_review(inp: RuleInput) -> list[Intent]:
    """One intent per goal whose week needs something; every normal goal goes into one quiet
    intent (no card: at most one line in the Bot's activity list, user 2026-10-15)."""
    week = iso_week(inp.local_date)
    out: list[Intent] = []
    normal: list[str] = []
    for g in inp.weeks:
        sit, d = situation(g, inp.settings)
        if sit == "normal":
            normal.append(g.path_id)
            continue
        title, reason = _review_text(g, sit, d)
        page = d.get("page_id") if sit == "stuck_page" else g.next_page_id
        out.append(Intent(
            kind="goal_review", rule_id="goal_review", rule_version=1,
            source_key=f"goal_review:{g.path_id}:{week}", dedup_key=f"goal_review:{g.path_id}",
            route="direct", evidence=(),
            payload={"situation": sit, "path_id": g.path_id, "goal": g.title, "week": week, **d},
            priority=60, reason=reason, placement="today", page_id=page, title=title))
    if normal:
        n = len(normal)
        out.append(Intent(
            kind="goal_review", rule_id="goal_review", rule_version=1,
            source_key=f"goal_review:normal:{week}", dedup_key="goal_review:normal",
            route="direct", evidence=(),
            payload={"situation": "normal", "paths": normal, "week": week}, priority=0,
            reason=f"{n} 个目标这周都正常", placement="today", page_id=None,
            title=f"每周回顾：{n} 个目标都正常"))
    return out


def _source_added(inp: RuleInput) -> list[Intent]:
    """One 今天 suggestion per new `SourceMatched` event that found pages (资料更新)."""
    out = []
    for m in inp.facts.source_matches:
        if m.event_id not in inp.new_event_ids or not m.pages:
            continue
        n = len(m.pages)
        head = f"《{m.title}》讲到了 {n} 页已有的内容"
        out.append(Intent(
            kind="source_added", rule_id="source_added", rule_version=1,
            source_key=f"source_added:{m.source_id}", dedup_key=f"source_added:{m.source_id}",
            route="direct", evidence=(m.event_id,),
            payload={"source_id": m.source_id, "title": m.title,
                     "pages": [{"page_id": pid, "title": t} for pid, t in m.pages]},
            priority=40, reason=f"{head}：{'、'.join(t for _, t in m.pages)}。要按它更新吗？",
            placement="today", page_id=None, title=head))
    return out


def _card_stale(inp: RuleInput) -> list[Intent]:
    """A hand edit: is the section's recall card still right? (`bot.card_stale` asks the model.)"""
    return [Intent(kind="card_stale", rule_id="card_stale", rule_version=1, source_key=f"card_stale:{h.event_id}",
                   dedup_key=f"card_stale:{h.event_id}", route="direct", evidence=(h.event_id,),
                   payload={"sections": list(h.sections)}, priority=30, reason="手改过的一节，看复习卡还成不成立",
                   placement="today", page_id=h.page_id, title="复习卡更新")
            for h in inp.facts.hand_edits if h.event_id in inp.new_event_ids]


RULES: tuple[Rule, ...] = (
    Rule("review_due", 1, "review_due", "daily", _review_due),
    Rule("goal_idle", 1, "goal_idle", "daily", _goal_idle),
    Rule("wrong_answers", 1, "wrong_answers", "event", _wrong_answers),
    Rule("wrong_answers", 1, "wrong_answers", "daily", _wrong_answers),
    Rule("strategy_negative", 2, "strategy_negative", "event", _strategy_negative),
    Rule("open_question", 1, "open_question", "daily", _open_question),
    # removed from the UI 2026-10-21; code kept until the architecture cleanup
    # Rule("chat", 1, "chat", "page", _chat),
    Rule("goal_review", 1, "goal_review", "weekly", _goal_review),
    Rule("source_added", 1, "source_added", "event", _source_added),
    Rule("card_stale", 1, "card_stale", "event", _card_stale),
)


DAILY_KINDS = frozenset(r.kind for r in RULES if r.mode == "daily")


def evaluate(inp: RuleInput) -> tuple[list[Intent], list[tuple[str, str]]]:
    """Run every rule of `inp.mode`; a raising rule yields (rule_id, error text), no intents."""
    intents: list[Intent] = []
    errors: list[tuple[str, str]] = []
    for rule in RULES:
        if rule.mode != inp.mode:
            continue
        try:
            intents.extend(rule.fn(inp))
        except Exception as e:
            log.exception("bot rule %s failed", rule.rule_id)
            errors.append((rule.rule_id, f"{type(e).__name__}: {e}"))
    return intents, errors
