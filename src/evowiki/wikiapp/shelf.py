"""收为参考卡 / 恢复成完整页 (spec 2026-10-29 retire-curate §4): the learner shelves a page whose
targets are all 确定会 (`views.shelvable`), and takes it off the shelf at any time. Both are one
`PageSaved` and make no model call; neither changes the graph nor K.

Shelving folds the page now (`fold.collapse_page`: a section with a 模糊 / 不会 cell stays open) and
sets `collapse_applied`; a regenerated shelved page is folded again on its next open
(`page._open_update`). Taking it off unfolds the not-locked foldable sections whose cells are not
all 确定会 (sections a fold pass would keep folded stay folded)."""

from datetime import datetime

from evowiki.goals.contracts import FOLDABLE_ROLES, Band, KView
from evowiki.goals.fold import collapse_page
from evowiki.wikiapp.events import PageRecord, PageSaved, UsageAdded, UsageRecord, WikiEvent
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Commit, Repo, WikiConflict
from evowiki.wikiapp.signals import signal_event
from evowiki.wikiapp.views import shelvable
from evowiki.wikiapp.wiki_schemas import ChangeOut, ShelveOut

SHELVE_USAGE = "收为参考卡"
EXPAND_USAGE = "恢复成完整页"
NOT_SHELVABLE = "这一页还不能收成参考卡：它的目标还没有全部确定会"
NOT_COLLAPSED = "这一页没有收成参考卡"


def _wev(new_id: Ids, now: datetime, body) -> WikiEvent:
    return WikiEvent(event_id=new_id("wev"), ts=now, body=body)


def shelved_record(rec: PageRecord, k: KView) -> tuple[PageRecord, tuple[str, ...]]:
    """`rec` shelved and folded now (the sections folded)."""
    if rec.page is None:
        return rec.model_copy(update={"shelved": True, "collapse_applied": False}), ()
    c = collapse_page(rec.page.outline, rec.page.states, k)
    changed = {**rec.changed, **{sid: "folded" for sid in c.folded}}
    return rec.model_copy(update={"page": rec.page.model_copy(update={"states": c.states}),
                                  "changed": changed, "shelved": True,
                                  "collapse_applied": True}), c.folded


def unshelved_record(rec: PageRecord, k: KView) -> tuple[PageRecord, tuple[str, ...]]:
    """「恢复成完整页」: off the shelf, every not-locked foldable section whose cells are not all
    确定会 unfolded (the sections opened)."""
    off = {"shelved": False, "collapse_applied": False}
    if rec.page is None:
        return rec.model_copy(update=off), ()
    page = rec.page
    states = dict(page.states)
    opened: list[str] = []
    for s in page.outline.sections:
        st = page.state(s.id)
        if s.role not in FOLDABLE_ROLES or st.fold_locked or st.pinned or not st.folded:
            continue
        if s.cells and all(k.cell(c).band is Band.KNOWN for c in s.cells):
            continue
        states[s.id] = st.model_copy(update={"folded": False})
        opened.append(s.id)
    changed = {**rec.changed, **{sid: "unfolded" for sid in opened}}
    return rec.model_copy(update={"page": page.model_copy(update={"states": states}),
                                  "changed": changed, **off}), tuple(opened)


def _page(repo: Repo, user_id: str, page_id: str):
    snap = repo.load(user_id)
    rec = snap.state.pages.get(page_id)
    if rec is None or page_id in snap.state.trashed_pages:
        raise LookupError(f"没有这个页面：{page_id}")
    return snap, rec


def collapse(repo: Repo, user_id: str, page_id: str, *, now: datetime, new_id: Ids) -> ShelveOut:
    """POST /wiki/pages/{id}/collapse. Not shelvable now → WikiConflict (409)."""
    snap, rec = _page(repo, user_id, page_id)
    k = snap.kview(now)
    if not shelvable(rec, k):
        raise WikiConflict(NOT_SHELVABLE)
    new, folded = shelved_record(rec, k)
    repo.commit(snap, Commit(wiki=[
        _wev(new_id, now, PageSaved(record=new, action="shelved")),
        _wev(new_id, now, UsageAdded(record=UsageRecord(action=SHELVE_USAGE, page=page_id)))]))
    return ShelveOut(change=ChangeOut(folded=list(folded)))


def expand(repo: Repo, user_id: str, page_id: str, *, now: datetime, new_id: Ids) -> ShelveOut:
    """POST /wiki/pages/{id}/expand. Not shelved → WikiConflict (409)."""
    snap, rec = _page(repo, user_id, page_id)
    if not rec.shelved:
        raise WikiConflict(NOT_COLLAPSED)
    new, opened = unshelved_record(rec, snap.kview(now))
    repo.commit(snap, Commit(wiki=[
        _wev(new_id, now, PageSaved(record=new, action="expanded")),
        _wev(new_id, now, UsageAdded(record=UsageRecord(action=EXPAND_USAGE, page=page_id))),
        signal_event(snap.state, "expand", now=now, new_id=new_id, page_id=page_id)]))
    return ShelveOut(change=ChangeOut(unfolded=list(opened)))
