"""A hand edit keeps the section's recall card: the old card stays until a new one takes its place, so a card never
disappears in between. This Bot trigger (rule `card_stale`, on a `HandEdited` event) asks one model call whether the card
still holds on the new text; when it does not, the card is replaced through the ledger (`card_fix`, undo in 活动记录) and
一条今日提醒 says so. The model decides on the text; code only checks the new face is usable."""

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from evowiki.bot.gate import gate
from evowiki.bot.schedule import local_day_bounds
from evowiki.llm.models import LLMRequest
from evowiki.store import bot as store_bot
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.repo import load_in
from evowiki.wikiapp.review_cards import CUE_MAX, card_id, summary_cue

log = logging.getLogger(__name__)
KIND = "card_stale"
PURPOSE = "review.card_stale"


class Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    holds: bool
    cue: str = ""
    answer: str = ""


def stale_request(heading: str, body: str, cue: str, answer: str, user_id: str) -> LLMRequest:
    prompt = (f"学习者手改了一节笔记。这一节现在是：\n\n## {heading}\n{body}\n\n这一节原来的复习卡：\n问：{cue}\n答：{answer}\n\n"
              "这张卡的问题和答案，按现在的正文还成立吗？手改只是多写了几句、换了说法，卡还成立；正文改掉了答案或者不再讲这个问题，才算不成立。"
              f"不成立时写一张新卡：问一个正文里具体的点，一两句能答，问题不超过 {CUE_MAX} 字。\n"
              '只回复 JSON：{"holds": true} 或 {"holds": false, "cue": "…", "answer": "…"}')
    return LLMRequest(purpose=PURPOSE, prompt_version="card-stale@v1", subject=f"user:{user_id}",
                      system="你判断一张复习卡在正文改动后还成不成立。只回复 JSON。", prompt=prompt)


def dispatch_card_stale(pool: Any, row: store_bot.TriggerRow, *, now: datetime, new_id: Any, ledger: Any,
                        gateway: Any, gate_context: Any) -> str:
    """One trigger: the model call runs outside any transaction. Returns the trigger's status."""
    trigger_id = row.trigger_id
    from evowiki.bot.pipeline import _intent_of

    with pool.connection() as conn:
        settings = store_bot.get_settings(conn, row.user_id)
        d = gate(_intent_of(row), gate_context(conn, row, settings))
        snap = load_in(conn, row.user_id)
    rec = snap.state.pages.get(row.page_id or "")
    if d.outcome != "pass" or rec is None or rec.page is None or ledger is None or gateway is None:
        with pool.connection() as conn, conn.transaction():
            store_bot.set_trigger_gate(conn, trigger_id, status="gated", outcome="skip", reason=d.reason or "empty",
                                       at=now, detail=d.detail)
        return "gated"
    gw = gateway(row.user_id)
    swapped: list[str] = []
    for sid in row.payload.get("sections", ()):
        sec = next((s for s in rec.page.outline.sections if s.id == sid), None)
        t = rec.page.texts.get(sid)
        if sec is None or t is None or not t.cue.strip() or not t.answer.strip():
            continue
        v, _ = gw.call("generator", stale_request(sec.heading, t.body, t.cue, t.answer, row.user_id), Verdict)
        if v.holds or not v.cue.strip() or not v.answer.strip() or len(v.cue) > CUE_MAX or summary_cue(v.cue):
            continue
        act = ledger.propose(row.user_id, kind="card_fix", scope="suggestion", source="bot",
                             summary=f"手改后换了「{sec.heading}」的复习卡", why="原来的题按改后的正文不再成立",
                             content={"op": "fix", "card_id": card_id(rec.page_id, sid, "cue"),
                                      "cue": v.cue.strip(), "answer": v.answer.strip(), "opinion": ""},
                             key=f"card_stale:{trigger_id}:{sid}", page_id=rec.page_id,
                             page_version=page_fingerprint(rec), trigger_id=trigger_id, approved_now=True, level="auto_undo")
        ledger.approve(row.user_id, act.action_id)
        swapped.append(sec.heading)
        rec = load_state_page(pool, row.user_id, rec.page_id)
    with pool.connection() as conn, conn.transaction():
        if swapped:
            title = f"「{swapped[0]}」的复习卡换了一张"
            start_end = local_day_bounds(now, store_bot.get_settings(conn, row.user_id).timezone)[1]
            store_bot.insert_suggestion(conn, store_bot.SuggestionRow(
                suggestion_id=new_id("bsg"), user_id=row.user_id, kind=KIND, title=title,
                why="手改后旧题不再成立；可在活动记录里撤销", action={"type": "open_page", "page_id": rec.page_id},
                placement=row.placement, priority=row.priority, dedup_key=row.dedup_key, expires_at=start_end,
                created_at=now, trigger_id=trigger_id, page_id=rec.page_id))
            store_bot.set_trigger_gate(conn, trigger_id, status="gated", outcome="pass", reason=None, at=now, detail=title)
        else:
            store_bot.set_trigger_gate(conn, trigger_id, status="gated", outcome="skip", reason="empty", at=now,
                                       detail="复习卡还成立")
    return "gated"


def load_state_page(pool: Any, user_id: str, page_id: str) -> Any:
    with pool.connection() as conn:
        return load_in(conn, user_id).state.pages[page_id]
