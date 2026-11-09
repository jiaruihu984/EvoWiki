"""Trigger pipeline (B3 spec §3): scan -> intents -> gate -> direct card / hand-off, plus the
sweep query. Plain functions over a psycopg pool; each operation is its own transaction and safe
to re-run. No DBOS here (Task 6 wraps these)."""

import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Literal

from evowiki.bot import card_stale, path_actions
from evowiki.bot.facts import load_facts, load_page_facts, new_event_ids, remedial_is_current
from evowiki.bot.facts_types import DueReview, UserFacts
from evowiki.bot.gate import GateContext, gate
from evowiki.bot.goal_week import load_goal_week
from evowiki.bot.rules import (
    DAILY_KINDS,
    STUCK_ASK,
    Intent,
    RuleInput,
    chip_hash,
    evaluate,
    situation,
)
from evowiki.bot.schedule import Slot, local_day_bounds, nominal_time
from evowiki.bot.settings import BotSettings, effective_paused
from evowiki.localday import local_date
from evowiki.store import bot as store_bot
from evowiki.store import bot_goals
from evowiki.store.bot import ActionRow
from evowiki.store.db import fetch_all
from evowiki.store.locks import xact_lock
from evowiki.wikiapp.repo import load_in

log = logging.getLogger(__name__)

NewId = Callable[[str], str]
EMPTY_DETAIL = "没有要做的"
SWEEP_AGE = timedelta(seconds=60)


@dataclass(frozen=True)
class Dispatched:
    trigger_id: str
    outcome: Literal["pass", "defer", "skip"] | None   # None: already gated earlier (no-op)
    reason: str | None
    status: str                       # trigger status after dispatch
    suggestion_id: str | None
    rsi_attempt: int = 0              # `awaiting_rsi`: the RSI runs this trigger had before


WRITTEN_DETAIL = "这几条页面反馈提交时已经各自处理过，这次不再重复处理"
MANUAL_DETAIL = "讲解调整设为手动，这次没有自动调整"
WEEKLY_DETAIL = "这周已经调整过讲解，下周再一起看"
RSI_BUSY_DETAIL = "讲解调整正在进行"
RSI_UNAVAILABLE_DETAIL = "这次没有调整讲解"
# the cycle's improvement round failed on a provider / infra error (signals kept, retried later)
RSI_GATE_ERROR_DETAIL = "复核时模型调用失败，未修改，之后会重试"   # a rewrite / judge call at the Gate
RSI_ERROR_DETAIL = "模型调用失败，未修改，之后会重试"            # e.g. the proposer call
RSI_UNUSABLE_DETAIL = "模型返回的内容无法使用，未修改"          # the reply could not be read


@dataclass(frozen=True)
class Card:
    title: str
    action: dict
    why: str | None = None      # plain text for the card; None = the trigger's reason (+ tail)


_NUM = "零一二三四五六七八九十"


def _days_phrase(d: int) -> str:
    """Plain Chinese for a small day count: 三天; above ten 「好些天」."""
    return f"{_NUM[d]}天" if 1 <= d <= 10 else "好些天"


def _goal_topic(title: str) -> str:
    """The topic name of a goal title: no leading 「继续：」, no trailing 「（k/n）」."""
    t = title.strip()
    for pre in ("继续：", "继续:"):
        t = t.removeprefix(pre)
    return re.sub(r"[（(]\d+/\d+[）)]\s*$", "", t).strip() or title


def _local_date(now: datetime, settings: BotSettings) -> date:
    return local_date(now, settings.timezone)


def _insert_intents(conn, user_id: str, intents: list[Intent], now: datetime,
                    new_id: NewId) -> list[str]:
    ids = []
    for i in intents:
        row = store_bot.TriggerRow(
            trigger_id=new_id("btr"), user_id=user_id, kind=i.kind, rule_id=i.rule_id,
            rule_version=i.rule_version, source_key=i.source_key, dedup_key=i.dedup_key,
            route=i.route, evidence=list(i.evidence), payload=i.payload, priority=i.priority,
            reason=i.reason, created_at=now, placement=i.placement, page_id=i.page_id,
            title=i.title)
        tid = store_bot.insert_trigger(conn, row)
        if tid is not None:
            ids.append(tid)
    return ids


def scan_events(pool, user_id: str, *, now: datetime, new_id: NewId) -> list[str]:
    """Turn the wiki / K events after the cursor into intents. Cursor advance and intent rows
    commit together; any failure outside the rules rolls both back and propagates."""
    with pool.connection() as conn, conn.transaction():
        wiki_seq, aspect_seq = store_bot.lock_cursor(conn, user_id, now)
        ids, wiki_max, aspect_max = new_event_ids(conn, user_id, wiki_seq, aspect_seq)
        if not ids:
            return []
        settings = store_bot.get_settings(conn, user_id)
        facts = load_facts(conn, user_id, now=now, settings=settings)
        intents, errors = evaluate(RuleInput(
            facts=facts, new_event_ids=ids, settings=settings, now=now,
            local_date=_local_date(now, settings), mode="event"))
        for rule_id, err in errors:
            # A broken rule must not wedge the log: it produced nothing, the cursor still moves.
            log.warning("bot rule %s failed for %s: %s", rule_id, user_id, err)
        out = _insert_intents(conn, user_id, intents, now, new_id)
        store_bot.set_cursor(conn, user_id, wiki_max, aspect_max, now)
        return out


def run_daily(pool, user_id: str, slot: Slot, *, now: datetime, new_id: NewId) -> list[str]:
    with pool.connection() as conn, conn.transaction():
        settings = store_bot.get_settings(conn, user_id)
        facts = load_facts(conn, user_id, now=now, settings=settings)
        intents, errors = evaluate(RuleInput(
            facts=facts, new_event_ids=frozenset(), settings=settings, now=now,
            local_date=slot.local_date, mode="daily"))
        for rule_id, err in errors:
            log.warning("bot rule %s failed for %s: %s", rule_id, user_id, err)
        ids = _insert_intents(conn, user_id, intents, now, new_id)
        # the weekly review in a savepoint: its failure is logged and the day's intents still
        # commit (the review is tried again at the next daily check of the week)
        try:
            with conn.transaction():
                weekly = _weekly(conn, user_id, slot, settings, facts, now=now, new_id=new_id)
        except Exception:
            log.exception("weekly goal review failed for %s; retried at the next daily check",
                          user_id)
            weekly = []
        return ids + weekly


def week_start_of(d: date) -> date:
    """The Monday of `d`'s ISO week."""
    return d - timedelta(days=d.weekday())


def _weekly(conn, user_id: str, slot: Slot, settings: BotSettings, facts: UserFacts, *,
            now: datetime, new_id: NewId) -> list[str]:
    """The weekly goal review (B5 spec §1.1), inside the daily check's transaction: on the
    review weekday or a later day of the same ISO week, once per week (a week with a snapshot
    row is done; a week the computer was off throughout is skipped, never caught up later).
    Intents and the week's snapshot rows (`bot_goal_review`) are written together."""
    d = slot.local_date
    start = week_start_of(d)
    if week_start_of(_local_date(now, settings)) != start:
        return []                       # a backfilled slot of last week: never reviewed now
    if d.weekday() < settings.review_weekday or bot_goals.week_reviewed(conn, user_id, start):
        return []
    weeks = load_goal_week(conn, user_id, now=now, settings=settings, week_start=start)
    intents, errors = evaluate(RuleInput(
        facts=facts, new_event_ids=frozenset(), settings=settings, now=now, local_date=d,
        mode="weekly", weeks=weeks))
    for rule_id, err in errors:
        log.warning("bot rule %s failed for %s: %s", rule_id, user_id, err)
    if errors:
        return []                       # no snapshot either: the next daily check tries again
    ids: list[str] = []
    by_path: dict[str, str] = {}
    for intent in intents:
        [tid] = _insert_intents(conn, user_id, [intent], now, new_id) or [None]
        if tid is None:
            continue
        ids.append(tid)
        for pid in intent.payload.get("paths", [intent.payload.get("path_id")]):
            by_path[pid] = tid
    for g in weeks:
        sit, _ = situation(g, settings)
        bot_goals.put_review(conn, user_id, week_start=start, path_id=g.path_id, situation=sit,
                             facts={k: v for k, v in asdict(g).items() if k != "last_week"},
                             trigger_id=by_path.get(g.path_id), at=now)
    return ids


REVIEW_ACTION: dict[str, str] = {"mostly_known": "replan_goal",
                                 "want_park": "park_goal"}


def _review_card(row: store_bot.TriggerRow) -> Card | None:
    p = row.payload
    sit = p.get("situation")
    if sit in ("prereq_gap", "prereq_missing"):
        return Card(row.title, {"type": "open_goal", "path_id": p["path_id"]})
    if sit == "stuck_page":
        return Card(row.title, {"type": "open_page", "page_id": p["page_id"], "ask": STUCK_ASK})
    if sit == "review_debt":
        return Card(row.title, {"type": "start_practice"})
    if sit in REVIEW_ACTION:
        return Card(row.title, {"type": REVIEW_ACTION[sit], "path_id": p["path_id"]})
    return None


def card_for(row: store_bot.TriggerRow, settings: BotSettings, now: datetime) -> Card | None:
    """The suggestion card of a direct trigger; None when there is nothing to show."""
    p = row.payload
    if row.kind == "review_due":
        n = int(p.get("count", 0))
        if n <= 0:
            return None
        return Card(f"复习 {n} 张卡片", {"type": "start_practice"})
    if row.kind == "goal_idle":
        page = p.get("page_id")
        action = ({"type": "open_page", "page_id": page} if page is not None
                  else {"type": "open_goal", "path_id": p["path_id"]})
        d = int(p.get("days", 0))
        gap = f"{_days_phrase(d)}没看了" if d >= 1 else "有一阵没看了"
        return Card(_goal_topic(str(p["title"])), action, why=f"{gap}，接着上次往下学")
    if row.kind == "goal_review":
        return _review_card(row)
    if row.kind == "wrong_answers":
        if p.get("page_id") is None or int(p.get("count", 0)) < settings.wrong_count:
            return None
        ask = (f"我最近在「{p['name']}」有 {p['count']} 道不同的题答错了。"
               "请结合这一页先帮我找出没理解的地方，换个讲法说明，再出一道题让我练习。")
        return Card(row.title, {"type": "open_page", "page_id": p["page_id"], "ask": ask})
    if row.kind == "open_question":
        text = p.get("text")
        if not text or p.get("page_id") is None:
            return None
        return Card(" ".join(str(text).split())[:40],
                    {"type": "open_page", "page_id": p["page_id"], "ask": text},
                    why="上次在这一页还没弄懂")
    if row.kind == "source_added":
        pages = [x["page_id"] for x in p.get("pages", [])]
        if not pages:
            return None
        return Card(row.title, {"type": "revise_from_source", "source_id": p["source_id"],
                                "page_ids": pages})
    return None


def _intent_of(row: store_bot.TriggerRow) -> Intent:
    return Intent(kind=row.kind, rule_id=row.rule_id, rule_version=row.rule_version,
                  source_key=row.source_key, dedup_key=row.dedup_key, route=row.route,
                  evidence=tuple(row.evidence), payload=row.payload, priority=row.priority,
                  reason=row.reason, placement=row.placement, page_id=row.page_id,
                  title=row.title)


def _gate_context(conn, row: store_bot.TriggerRow, settings: BotSettings, now: datetime,
                  day_start: datetime) -> GateContext:
    placed_today = store_bot.count_suggestions(
        conn, row.user_id, since=day_start, placement=row.placement)
    placed_on_page = (store_bot.count_suggestions(
        conn, row.user_id, since=day_start, placement="page", page_id=row.page_id)
        if row.placement == "page" else 0)
    last = store_bot.last_suggested_at(conn, row.user_id, row.dedup_key)
    if last is not None and row.kind in DAILY_KINDS and row.kind != "wrong_answers":
        last = nominal_time(last, settings)
    return GateContext(
        settings=settings, now=now, placed_today=placed_today, placed_on_page=placed_on_page,
        last_suggested_at=last,
        counters=store_bot.get_counters(conn, row.user_id, row.dedup_key))


def _settings_of(pool, user_id: str) -> BotSettings:
    with pool.connection() as conn:
        return store_bot.get_settings(conn, user_id)


def dispatch(pool, trigger_id: str, *, now: datetime, new_id: NewId,
             ledger: Any = None, gateway: Any = None) -> Dispatched:
    """Gate one trigger and make its card / hand-off. `ledger` (ActionLedger): a weekly review
    that found a weak prerequisite also re-orders the path (B5b): the ledger row is written with
    the card in this transaction (`auto_undo` -> approved, `ask` -> proposed) and an approved row
    is executed right after it commits; a failed execution says so on the card. Without a ledger
    the card is made without any path edit."""
    with pool.connection() as conn:
        first = store_bot.get_trigger(conn, trigger_id)
    if first is not None and first.kind == card_stale.KIND:
        if first.status != "new":
            return Dispatched(trigger_id, None, first.gate_reason, first.status, None, 0)
        start = local_day_bounds(now, _settings_of(pool, first.user_id).timezone)[0]
        status = card_stale.dispatch_card_stale(
            pool, first, now=now, new_id=new_id, ledger=ledger, gateway=gateway,
            gate_context=lambda conn, row, settings: _gate_context(conn, row, settings, now, start))
        return Dispatched(trigger_id, None, None, status, None, 0)
    approve: list[ActionRow] = []
    out = _dispatch(pool, trigger_id, now=now, new_id=new_id, ledger=ledger, approve=approve)
    if ledger is not None and out.outcome is None:
        # already gated: a path edit left `approved` (crash after the commit, or a raising
        # approve) is executed now
        with pool.connection() as conn:
            approve = [a for a in store_bot.actions_of_trigger(conn, trigger_id)
                       if a.kind == path_actions.KIND and a.status == "approved"]
    for act in approve:
        execute_path_edit(pool, ledger, act)
    return out


def execute_path_edit(pool, ledger: Any, act: ActionRow) -> None:
    """Execute an approved Bot path edit; when it fails, its card says so. A raising approve is
    logged and leaves the row `approved` (re-dispatch or the sweep executes it again)."""
    try:
        done = ledger.approve(act.user_id, act.action_id)
    except Exception:
        log.exception("bot path edit %s not executed yet; retried later", act.action_id)
        return
    if done.status == "executed" or act.trigger_id is None:
        return
    with pool.connection() as conn:
        for card in store_bot.suggestions_of_trigger(conn, act.trigger_id):
            store_bot.append_suggestion_why(conn, card.suggestion_id,
                                            f"（没能调整学习路径：{done.detail}）")


def stale_path_edits(pool, *, now: datetime) -> list[ActionRow]:
    """Bot path edits left `approved` for over two minutes (the sweep executes them)."""
    with pool.connection() as conn:
        rows = fetch_all(conn, "select action_id from agent_action where kind = %s "
                               "and status = 'approved' and decided_at < %s order by created_at "
                               "limit 200", (path_actions.KIND, now - store_bot.APPROVED_STALE))
        return [store_bot.get_action(conn, r["action_id"]) for r in rows]


def _path_edit(conn, row: store_bot.TriggerRow, ledger: Any) -> tuple[ActionRow | None, str]:
    """(the ledger row of the review's path edit, the card's why tail); (None, "") when there is
    nothing to edit, no ledger, or the edit no longer fits the path."""
    if ledger is None or row.payload.get("situation") not in ("prereq_gap", "prereq_missing"):
        return None, ""
    state = load_in(conn, row.user_id).state
    rec = state.paths.get(row.payload.get("path_id", ""))
    kw = None if rec is None else path_actions.proposal(state, rec, row.payload)
    if kw is None:
        return None, ""
    level = ledger.level_in(conn, row.user_id, path_actions.SCOPE)
    act = ledger.propose_in(conn, row.user_id, **kw, trigger_id=row.trigger_id, level=level,
                            approved_now=level == "auto_undo")
    what = (f"把「{row.payload.get('page_title') or row.payload['name']}」挪到前面"
            if kw["content"]["op"] == "move" else f"在前面加一页「{row.payload['name']}」")
    if act.status == "proposed":
        return act, f"；{what}，待你确认"
    # tentative until executed (right after this transaction; a failure is appended to the card)
    return act, f"；Bot 将{what}，可在个性化里撤销"


def _dispatch(pool, trigger_id: str, *, now: datetime, new_id: NewId, ledger: Any,
              approve: list) -> Dispatched:
    with pool.connection() as conn, conn.transaction():
        owner = store_bot.get_trigger(conn, trigger_id)
        if owner is None:
            raise ValueError(f"unknown bot trigger {trigger_id!r}")
        # Per-user lock before any count is read: two dispatches of one user never both see room
        # under a cap (the queue serialises a user, this keeps dispatch safe on its own).
        xact_lock(conn, f"bot:{owner.user_id}")
        row = store_bot.lock_trigger(conn, trigger_id)
        if row.status != "new":
            # a re-dispatch of a trigger waiting for its RSI run: the next attempt's id, never 0
            attempt = (store_bot.count_runs(conn, trigger_id, "rsi")
                       if row.status == "awaiting_rsi" else 0)
            return Dispatched(trigger_id, None, row.gate_reason, row.status, None, attempt)
        settings = store_bot.get_settings(conn, row.user_id)
        start, end = local_day_bounds(now, settings.timezone)
        d = gate(_intent_of(row), _gate_context(conn, row, settings, now, start))

        def finish(status: str, outcome: str | None, reason: str | None,
                   suggestion_id: str | None = None, detail: str = "",
                   rsi_attempt: int = 0) -> Dispatched:
            store_bot.set_trigger_gate(conn, trigger_id, status=status, outcome=outcome,
                                       reason=reason, at=now, detail=detail)
            return Dispatched(trigger_id, outcome, reason, status, suggestion_id, rsi_attempt)

        if row.kind == "goal_review" and row.payload.get("situation") == "normal":
            # no card, so no cap / cooldown: only a paused Bot or the kind turned off stops it
            if d.outcome == "skip" and d.reason in ("paused", "kind_off"):
                return finish("gated", "skip", d.reason, detail=d.detail)
            return finish("gated", "pass", None, detail=f"每周回顾：{row.reason}")
        if d.outcome == "skip":
            return finish("gated", "skip", d.reason, detail=d.detail)
        if d.outcome == "defer":
            return finish("new", "defer", d.reason, detail=d.detail)   # the sweep retries it
        if row.route == "rsi":
            if row.payload.get("written_only"):
                # written page feedback counts toward the user's next RSI round (due by signal
                # count); the Bot starts no extra cycle for it
                return finish("handed_off", "pass", None, detail=WRITTEN_DETAIL)
            # the bot_rsi workflow runs the user's cycle (run_rsi)
            return finish("awaiting_rsi", "pass", None, detail=d.detail,
                          rsi_attempt=store_bot.count_runs(conn, trigger_id, "rsi"))
        if row.kind == "chat":
            return _dispatch_chat(conn, row, settings, now, start, end, new_id, finish)
        if row.kind == "wrong_answers" and not remedial_is_current(
                conn, row.user_id, cell=row.payload["cell"], evidence=row.evidence,
                page_id=row.page_id, now=now, settings=settings):
            return finish("gated", "skip", "empty", detail=EMPTY_DETAIL)
        try:
            card = card_for(row, settings, now)
        except Exception:
            log.exception("bot trigger %s: card_for failed", trigger_id)
            return finish("failed", None, None)
        if card is None:
            return finish("gated", "skip", "empty", detail=EMPTY_DETAIL)
        act, tail = _path_edit(conn, row, ledger)
        if act is not None and act.status == "approved":
            approve.append(act)
        sid = new_id("bsg")
        store_bot.insert_suggestion(conn, store_bot.SuggestionRow(
            suggestion_id=sid, user_id=row.user_id, kind=row.kind, title=card.title,
            why=(card.why if card.why is not None else row.reason) + tail, action=card.action, placement=row.placement,
            priority=row.priority,
            dedup_key=row.dedup_key, expires_at=end, created_at=now, trigger_id=trigger_id,
            page_id=row.page_id))
        return finish("gated", "pass", None, sid, detail=d.detail)


def _dispatch_chat(conn, row: store_bot.TriggerRow, settings: BotSettings, now: datetime,
                   day_start: datetime, day_end: datetime, new_id: NewId, finish) -> Dispatched:
    """A page-chip trigger: one suggestion per chip, each through the gate with its own cooldown
    key `chat:{page}:{hash}` (and the page's daily cap counting the ones just created). The
    trigger passes when at least one chip became a card; otherwise it reports why the last chip
    was skipped."""
    chips = [c["text"] for c in row.payload.get("chips", []) if c.get("text")]
    first, last_skip, made = None, None, 0
    for text in dict.fromkeys(chips):
        key = f"chat:{row.page_id}:{chip_hash(text)}"
        chip_row = replace(row, dedup_key=key)
        d = gate(_intent_of(chip_row), _gate_context(conn, chip_row, settings, now, day_start))
        if d.outcome != "pass":
            last_skip = d
            if d.reason == "cap_reached":
                break
            continue
        sid = new_id("bsg")
        store_bot.insert_suggestion(conn, store_bot.SuggestionRow(
            suggestion_id=sid, user_id=row.user_id, kind="chat", title=text, why=row.reason,
            action={"type": "send_to_assistant", "text": text}, placement="page",
            priority=row.priority, dedup_key=key, expires_at=day_end, created_at=now,
            trigger_id=row.trigger_id, page_id=row.page_id))
        first, made = first or (sid, d.detail), made + 1
    if first is not None:
        return finish("gated", "pass", None, first[0], detail=first[1])
    if last_skip is None:
        return finish("gated", "skip", "empty", detail=EMPTY_DETAIL)
    return finish("gated", "skip", last_skip.reason, detail=last_skip.detail)


def page_chips(pool, user_id: str, page_id: str, *, now: datetime, new_id: NewId) -> list[str]:
    """Make the page's chat chips (rule `chat`, mode "page") and dispatch them inline: deterministic
    (no LLM), at most one trigger per (user, page, local day) - a repeat the same day does
    nothing; a paused Bot or a disabled chat kind makes nothing at all. Returns the ids of
    the cards just made. `UnknownPage` for a page the user does not have."""
    with pool.connection() as conn, conn.transaction():
        settings = store_bot.get_settings(conn, user_id)
        if effective_paused(settings, now) or not settings.kinds["chat"]:
            return []           # no skip row either: unpausing the same day still gives chips
        local_date = _local_date(now, settings)
        if store_bot.trigger_exists(conn, user_id, f"chat:{page_id}:{local_date}"):
            return []           # made (or gated) already today
        page = load_page_facts(conn, user_id, page_id, now=now, settings=settings)
        # the chat rule reads `page` only
        facts = UserFacts(user_id=user_id, wrong_answers=(), due_review=DueReview(0, (), None),
                          goals=(), open_questions=(), signals=(), names={})
        intents, errors = evaluate(RuleInput(
            facts=facts, new_event_ids=frozenset(), settings=settings, now=now,
            local_date=local_date, mode="page", page=page))
        for rule_id, err in errors:
            log.warning("bot rule %s failed for %s: %s", rule_id, user_id, err)
        trigger_ids = _insert_intents(conn, user_id, intents, now, new_id)
    made: list[str] = []
    for tid in trigger_ids:
        if dispatch(pool, tid, now=now, new_id=new_id).suggestion_id is not None:
            with pool.connection() as conn:
                made += [c.suggestion_id for c in store_bot.suggestions_of_trigger(conn, tid)]
    return made


def undispatched(pool, *, now: datetime) -> list[tuple[str, str]]:
    """(user_id, trigger_id) of triggers still `new` more than 60 s after creation."""
    with pool.connection() as conn:
        rows = store_bot.new_triggers(conn, now - SWEEP_AGE)
        # a paused dispatch kind (repeated failures) is left alone until its pause is over
        paused = {u for u in {r.user_id for r in rows}
                  if store_bot.kind_paused_until(conn, u, "dispatch", now) is not None}
    return [(r.user_id, r.trigger_id) for r in rows if r.user_id not in paused]


# --- strategy feedback -> RSI (B5c, spec 2026-10-15 §2) -------------------------------------

# runner(user_id, trigger_id) -> {"status": "ran" | "busy" | "manual" | "not_due" | "unavailable", ...};
# "ran" carries the cycle's `a` / `b` lines and `patch_ids` (service glue:
# service.rsi_runs.bot_rsi_runner)
RsiRunner = Callable[[str, str], dict]


@dataclass(frozen=True)
class RsiHandled:
    trigger_id: str
    status: str                      # the trigger's status afterwards
    detail: str
    ran: bool                        # the runner was called and reported a cycle
    # the cycle ran but its improvement round failed on a provider / infra error (`b` = "error: …"):
    # the trigger is `failed` and the bot_rsi run is recorded failed (failure streak)
    error: bool = False


def _a_line(a: str) -> str:
    if a.startswith("accepted"):
        return "调整了学习状态评估"
    if a.startswith("awaiting_user"):
        return "学习状态评估的调整，待你在个性化里确认"
    return ""


def _b_line(b: str) -> str:
    """The cycle's improvement-round status (`CycleResult.b`) in one line. The track-B states
    below stay for cycle records written before RSI-B retired (2026-10-29). A harness round's line
    is "harness <status> [round id]: <reason>" (`harness.round.HarnessRounds.run`); a failed
    observation of the changes in force is appended as "; observe error: …" (`teach.cycle`)."""
    b = b.split("; observe error", 1)[0]
    if b.startswith("harness "):
        status = b.removeprefix("harness ").split(":", 1)[0].split(" ", 1)[0]
        return _H_LINE.get(status) or _unknown(b)
    if b == "off":
        return "讲解的自动改进没有打开，这次没有改"
    if b.startswith("personal_accepted"):
        return "改了一条讲解规则"
    if b.startswith("awaiting_user"):
        return "有一条讲解规则的修改，待你在个性化里确认"
    if b.startswith("judge_unavailable"):
        return "没有第二个模型来复核，讲解规则没有改"
    if b.startswith("gate_failed"):
        return "这条修改没有通过复核，没有采用"
    if b == "none":
        return "没有可用的模型，没有改"
    if b == "no_budget" or b.startswith("budget"):
        return "用量已达上限，没有改"
    if b.startswith("error: gate_error"):
        return RSI_GATE_ERROR_DETAIL
    if b.startswith("error"):
        return RSI_ERROR_DETAIL
    if b.startswith("candidate"):
        return RSI_GATE_ERROR_DETAIL
    if b.startswith("unusable_output"):
        return RSI_UNUSABLE_DETAIL
    if b.startswith(("no_finding", "no_patch")):
        return "没找到要改的"
    return _unknown(b)


# the harness round's statuses (`store.harness_rsi.RoundStatus`); "error" is RSI_ERROR_DETAIL
_H_LINE = {
    "applied": "改了一条讲解规则或写页面的做法",
    "asked": "有一条讲解的修改，待你在个性化里确认",
    "no_change": "没找到要改的",
    "thin": "反馈还不够多，这次没有改",
    "failed": "这条修改没有通过检查，没有采用",
    "skipped": "这次不需要改进，没有改",
    "error": RSI_ERROR_DETAIL,
}


def _unknown(b: str) -> str:
    # fail closed: an unknown state is never passed off as "nothing to change"
    log.warning("bot RSI hand-off: unknown improvement round result %r", b)
    return f"状态未知（{b}）"


def round_failed(b: str) -> bool:
    """The cycle's improvement round failed on a provider / infra error (the trigger fails, the
    bot_rsi run is recorded failed): a track-B "error: …" (old records) or a harness round's
    "harness error …" (`HarnessRounds.run`)."""
    return b.startswith(("error", "harness error"))


def rsi_summary(result: dict) -> str:
    """The one-line result of a Bot-started cycle (the trigger's `gate_detail`)."""
    parts = [_b_line(str(result.get("b", "none")))]
    a = _a_line(str(result.get("a", "")))
    if a:
        parts.append(a)
    return "；".join(parts)


def run_rsi(pool, trigger_id: str, runner: RsiRunner | None, *, now: datetime,
            run_id: str | None = None) -> RsiHandled:
    """Run the user's RSI cycle for an `awaiting_rsi` trigger (no transaction is held while the
    cycle runs). `ran` -> `handed_off` with the one-line result; `manual` (the user's RSI cycle
    setting) -> `handed_off`, nothing ran; `busy` (another cycle holds the user's lock) -> back
    to `new` with a `busy` defer, the sweep dispatches it again; no runner or `unavailable` ->
    `failed` (fail closed). A cycle whose improvement round failed on a provider / infra error (`b`
    "error: …", e.g. a Gate judge call answered HTTP 400) is not a result: the trigger is `failed`
    with the one-line result (「复核时模型调用失败，未修改，之后会重试」) and `error` is set, so the bot_rsi run
    is recorded failed (failure streak); the round left its signals unused, so a later cycle
    retries. A raising runner fails the trigger and re-raises. A trigger in any
    other status is left alone (a repeat after a crash changes nothing)."""
    with pool.connection() as conn:
        row = store_bot.get_trigger(conn, trigger_id)
    if row is None:
        raise ValueError(f"unknown bot trigger {trigger_id!r}")
    if row.status != "awaiting_rsi":
        return RsiHandled(trigger_id, row.status, row.gate_detail, False)

    def close(status: str, detail: str, *, outcome: str = "pass",
              reason: str | None = None) -> RsiHandled:
        with pool.connection() as conn, conn.transaction():
            cur = store_bot.lock_trigger(conn, trigger_id)
            if cur.status != "awaiting_rsi":
                return RsiHandled(trigger_id, cur.status, cur.gate_detail, False)
            store_bot.set_trigger_gate(conn, trigger_id, status=status, outcome=outcome,
                                       reason=reason, at=now, run_id=run_id, detail=detail)
        return RsiHandled(trigger_id, status, detail, status == "handed_off")

    if runner is None:
        log.warning("bot trigger %s: no RSI runner wired; failed closed", trigger_id)
        return close("failed", RSI_UNAVAILABLE_DETAIL)
    try:
        result = runner(row.user_id, trigger_id)
    except Exception:
        close("failed", RSI_UNAVAILABLE_DETAIL)
        raise
    status = result.get("status")
    if status == "ran":
        if round_failed(str(result.get("b", ""))):
            out = close("failed", rsi_summary(result))
            return replace(out, error=out.status == "failed")
        return close("handed_off", rsi_summary(result))
    if status == "manual":
        out = close("handed_off", MANUAL_DETAIL)
        return RsiHandled(out.trigger_id, out.status, out.detail, False)
    if status == "not_due":                 # the weekly cap: next week's cycle reads the signals
        out = close("handed_off", WEEKLY_DETAIL)
        return RsiHandled(out.trigger_id, out.status, out.detail, False)
    if status == "busy":
        return close("new", RSI_BUSY_DETAIL, outcome="defer", reason="busy")
    log.warning("bot trigger %s: RSI runner answered %r; failed closed", trigger_id, status)
    return close("failed", RSI_UNAVAILABLE_DETAIL)
