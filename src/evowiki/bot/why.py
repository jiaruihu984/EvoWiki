"""The Bot's explanations (spec §7): the "why" chain of one suggestion / trigger and the recent
activity list. Read-only."""

from datetime import datetime, timedelta

from evowiki.bot.actions import can_undo
from evowiki.bot.bot_schemas import (
    ActivityOut,
    WhyGate,
    WhyOut,
    WhyOutput,
    WhyReaction,
    WhyTrigger,
    WriteActivity,
)
from evowiki.bot.presentation import action_summary, gate_detail, trigger_text
from evowiki.i18n import current_language, tr
from evowiki.store import bot as store
from evowiki.wikiapp import path_order
from evowiki.wikiapp.page_run_store import PageRunRow
from evowiki.wikiapp.repo import load_in


def why(pool, user_id: str, *, suggestion_id: str | None = None,
        trigger_id: str | None = None) -> WhyOut:
    """Exactly one of the ids (else ValueError → 422). Unknown or another user's → LookupError
    (→ 404)."""
    if (suggestion_id is None) == (trigger_id is None):
        raise ValueError("give exactly one of suggestion_id / trigger_id")
    with pool.connection() as conn:
        sugg = None
        if suggestion_id is not None:
            sugg = store.get_suggestion(conn, suggestion_id)
            if sugg is None or sugg.user_id != user_id:
                raise LookupError("没有找到这张卡片")
            trigger_id = sugg.trigger_id
        trig = None
        if trigger_id is not None:
            trig = store.get_trigger(conn, trigger_id)
            if trig is None or trig.user_id != user_id:
                raise LookupError("没有找到这条记录")
        suggs, actions, changes = [], [], []
        if trig is not None:
            suggs = store.suggestions_of_trigger(conn, trig.trigger_id)
            actions = store.actions_of_trigger(conn, trig.trigger_id)
            if trig.route == "rsi":
                changes = store.bot_cycle_changes(conn, user_id, trig.trigger_id)
    if sugg is None and suggs:
        sugg = suggs[0]
    if sugg is None:
        suggs = []
    elif sugg.suggestion_id not in {s.suggestion_id for s in suggs}:
        suggs = [sugg]                      # a card with no trigger
    return WhyOut(
        trigger=None if trig is None else WhyTrigger(
            kind=trig.kind, rule_id=trig.rule_id, rule_version=trig.rule_version,
            reason=(trigger_text(trig.kind, trig.payload)[1] if current_language() == "en" else trig.reason), evidence=trig.evidence, created_at=trig.created_at),
        gate=None if trig is None else WhyGate(
            outcome=trig.gate_outcome, reason=trig.gate_reason,
            detail=(gate_detail(trig.gate_reason, trig.gate_outcome) if current_language() == "en" else trig.gate_detail or None), at=trig.gated_at),
        outputs=[WhyOutput(type="suggestion", id=s.suggestion_id, kind=s.kind, title=(trigger_text(s.kind, trig.payload if trig is not None else {})[0]
                                  if current_language() == "en" and s.kind != "chat" else s.title),
                           status=s.status) for s in suggs]
        + [WhyOutput(type="action", id=a.action_id, kind=a.kind, title=action_summary(a), status=a.status)
           for a in actions]
        + [WhyOutput(type="rsi_change", id=c["patch_id"], kind=f"rsi_{c['track']}",
                     title=c["why"][:80] or "讲解调整", status=c["status"])
           for c in changes if c["patch_id"] not in {a.content.get("patch_id") for a in actions}],
        reaction=None if sugg is None else WhyReaction(status=sugg.status,
                                                       reacted_at=sugg.reacted_at))


def _result(t: store.TriggerRow, has_suggestion: bool) -> str | None:
    if t.status in ("handed_off", "awaiting_rsi"):
        return "handed_to_rsi"
    if t.status == "failed":
        return None                   # a fault, not an activity: never shown (spec §4)
    if (t.kind == "goal_review" and t.payload.get("situation") == "normal"
            and t.status == "gated" and t.gate_outcome == "pass"):
        return "reviewed"            # a quiet week: one line, no card (B5 spec §5.4)
    if t.status == "gated" and t.gate_outcome == "pass" and has_suggestion:
        return "suggested"
    if (t.status == "gated" and t.gate_outcome == "skip") or (
            t.status == "new" and t.gate_outcome == "defer"):
        return "skipped"
    return None                       # not gated yet, or nothing to say


def _activity_title(t: store.TriggerRow, suggs: list, result: str) -> str:
    """The 最近 line of a trigger: what the Bot did (a card made), else what it was about."""
    if current_language() == "en":
        return trigger_text(t.kind, t.payload)[0]
    title = t.title or t.reason
    if result != "suggested":
        return title
    card = suggs[0].title if suggs else title
    if t.kind == "review_due" and isinstance(t.payload.get("count"), int):
        return f"安排了今日复习（{t.payload['count']} 张卡片）"
    if t.kind == "goal_idle":
        return f"提醒继续学习「{card}」"
    return title


def recent(pool, user_id: str, *, now: datetime, days: int = 14,
           include_skipped: bool = False, writes=None) -> list[ActivityOut]:
    """`writes`: the user's page-write queue (`WriteQueue.runs_since`); with it the pages written in
    the window (the Bot's idle ones and the ones the learner asked the assistant for) are rows too."""
    since = now - timedelta(days=days)
    out: list[ActivityOut] = []
    with pool.connection() as conn:
        for t in store.triggers_since(conn, user_id, since):
            suggs = store.suggestions_of_trigger(conn, t.trigger_id)
            result = _result(t, bool(suggs))
            if result is None or (result == "skipped" and not include_skipped):
                continue
            out.append(ActivityOut(
                trigger_id=t.trigger_id, kind=t.kind, title=_activity_title(t, suggs, result),
                reason=trigger_text(t.kind, t.payload)[1] if current_language() == "en" else t.reason,
                at=t.created_at, gate_outcome=t.gate_outcome, gate_reason=t.gate_reason,
                gate_detail=gate_detail(t.gate_reason, t.gate_outcome) if current_language() == "en" else t.gate_detail or None, result=result,
                suggestion_title=(trigger_text(t.kind, t.payload)[0] if current_language() == "en" and t.kind != "chat"
                                  else suggs[0].title) if suggs else None,
                suggestion_status=suggs[0].status if suggs else None,
                cost_usd=sum(s.cost_usd for s in suggs),
                page_id=next((s.page_id for s in suggs if s.page_id), None)))
        if writes is not None:
            out.extend(_write_rows(conn, user_id, writes.runs_since(user_id, since)))
    out.sort(key=lambda a: a.at, reverse=True)
    return out


def _write_rows(conn, user_id: str, runs: list[PageRunRow]) -> list[ActivityOut]:
    """One row per page-writing run (spec 「每写一页一行」): the Bot's idle writes and every write
    for the learner (an assistant action, 看不懂, 我想学这个, a path add, an opened unwritten page,
    重新写). Its 撤销 is the run's own ledger row (`bot_write` / `user_write`): the page goes back
    to unwritten. A run that failed or was dropped before it finished is not an activity."""
    # (a queued run has no row yet: it shows when it begins, as in the demo)
    runs = [r for r in runs if r.kind == "organize" and (
        r.status == "writing" and r.discarded_at is None and r.began_at is not None
        or r.status == "done")]
    if not runs:
        return []
    state = load_in(conn, user_id).state
    led = {a.run_id: a for a in store.write_rows(conn, user_id, [r.run_id for r in runs])}
    out: list[ActivityOut] = []
    for r in runs:
        idle = r.origin == "idle"
        row = led.get(r.run_id)
        rec = state.pages.get(r.page_id)
        title = rec.title if rec is not None else r.page_id
        goal, n = None, None
        for prec in state.paths.values():
            ids = path_order.startable_page_ids(prec)
            if r.page_id in ids:
                goal, n = prec.path.goal, ids.index(r.page_id) + 1
                break
        res = (row.result or {}) if row is not None else {}
        prog = r.progress or {}
        cost = r.cost_usd if r.cost_usd is not None else float(
            res.get("cost_usd") or prog.get("cost_usd") or 0.0)
        end = r.finished_at or r.started_at
        opened = state.opened_at.get(r.page_id)
        writing = r.status == "writing"
        asked = None if row is None or idle else row.content.get("asked")
        out.append(ActivityOut(
            trigger_id=None, run_id=r.run_id, kind="idle_write" if idle else "user_write",
            title=(tr("Creating page: {title}" if writing else "Created page: {title}",
                      lang=current_language(), title=title) if current_language() == "en" else
                   f"正在创建「{title}」页面" if writing else f"创建了「{title}」页面"), reason=title,
            at=r.began_at or r.started_at, gate_outcome=None, gate_reason=None, gate_detail=None,
            result="writing" if writing else "written", suggestion_title=None,
            suggestion_status=None, cost_usd=cost,
            write=WriteActivity(
                page_id=r.page_id, title=title, folder=rec.folder if rec is not None else "",
                goal=goal, n=n, asked=asked if isinstance(asked, str) else None,
                origin=r.origin, action_id=None if row is None else row.action_id,
                can_undo=row is not None and can_undo(row),
                undone=row is not None and row.status == "undone",
                opened=opened is not None and opened >= end,
                calls=r.llm_calls if r.llm_calls is not None else int(res.get("calls") or 0),
                outline_calls=int(prog.get("outline_calls") or 0),
                section_calls=int(prog.get("section_calls") or 0))))
    return out
