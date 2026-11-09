"""Page-level feedback (R1 signals): one `signal_recorded` and one usage record in one commit, 0 LLM
calls. The usage record (「整页反馈」) is what the curator and 「从我的反馈总结一条」 read; the signal is
what the RSI lanes read (`wikiapp.signals`). A hand edit in a note is `wikiapp.hand_edit`.

Unknown page → LookupError (404); a section_id that is not in the page's outline (or a section_id
on a page that is not organized yet) → WikiInvalid (422)."""

from datetime import datetime

from evowiki.teach.contracts import CHIP_LABEL, PageFeedbackIn, SignalKind
from evowiki.wikiapp.events import UsageAdded, UsageRecord, WikiEvent
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict, WikiInvalid
from evowiki.wikiapp.signals import signal_event
from evowiki.wikiapp.wiki_schemas import SignalCreatedOut

PAGE_FEEDBACK_USAGE = "整页反馈"
PAGE_FIT = "基本合适"


def _heading(snap: Snapshot, page_id: str, section_id: str | None) -> str | None:
    """The section's heading; unknown page → LookupError, unknown section → WikiInvalid."""
    rec = snap.state.pages.get(page_id)
    if rec is None:
        raise LookupError(f"没有这个页面：{page_id}")
    if section_id is None:
        return None
    hit = (next((s for s in rec.page.outline.sections if s.id == section_id), None)
           if rec.page is not None else None)
    if hit is None:
        raise WikiInvalid(f"这一页没有这一段：{section_id}")
    return hit.heading


def feedback_text(body: PageFeedbackIn) -> str:
    """Usage text: chip labels joined by 「、」, then 「：」 and the user's words (either may be
    missing)."""
    labels = "、".join(CHIP_LABEL[c] for c in body.chips)
    words = body.text.strip()
    return f"{labels}：{words}" if labels and words else labels or words


def _record(repo: Repo, user_id: str, page_id: str, section_id: str | None, kind: SignalKind,
            usage: str, *, usage_text: str, signal_text: str, chips=(), now: datetime,
            new_id: Ids, shown: str | None = None,
            rsi_requested: bool | None = None) -> SignalCreatedOut:
    for attempt in range(2):     # a background curate may commit between load and commit: once more
        snap = repo.load(user_id)
        heading = _heading(snap, page_id, section_id)
        ev = signal_event(snap.state, kind, now=now, new_id=new_id, page_id=page_id,
                          section_id=section_id, chips=chips, text=signal_text, target=shown)
        usage_ev = WikiEvent(event_id=new_id("wev"), ts=now, body=UsageAdded(record=UsageRecord(
            action=usage, page=page_id, heading=heading, text=usage_text)))
        def record_rsi(conn, ev=ev):
            if conn is not None and rsi_requested is not None:
                conn.execute("insert into rsi_feedback(signal_id,user_id,event_id,eligible) "
                             "values (%s,%s,%s,%s)",
                             (ev.body.signal.signal_id, user_id, ev.event_id,
                              rsi_requested and bool(signal_text.strip())))
        try:
            repo.commit(snap, Commit(wiki=[ev, usage_ev], extra=[record_rsi]))
            break
        except WikiConflict:
            if attempt:
                raise
    return SignalCreatedOut(signal_id=ev.body.signal.signal_id)


def page_feedback_flow(repo: Repo, user_id: str, page_id: str, body: PageFeedbackIn, *,
                       now: datetime, new_id: Ids) -> SignalCreatedOut:
    """POST /wiki/pages/{id}/feedback: a `page_feedback` signal (chips + the user's words) and a
    usage record 「整页反馈」."""
    return _record(repo, user_id, page_id, body.section_id, "page_feedback", PAGE_FEEDBACK_USAGE,
                   usage_text=feedback_text(body), signal_text=body.text.strip(),
                   chips=body.chips, now=now, new_id=new_id, shown=body.artifact_id,
                   rsi_requested=body.apply)


def page_fit_flow(repo: Repo, user_id: str, page_id: str, *, now: datetime,
                  new_id: Ids) -> SignalCreatedOut:
    """POST /wiki/pages/{id}/fit: 基本合适 at the end of a page, a `page_fit` signal (0 calls)."""
    return _record(repo, user_id, page_id, None, "page_fit", PAGE_FIT, usage_text=PAGE_FIT,
                   signal_text=PAGE_FIT, now=now, new_id=new_id)
