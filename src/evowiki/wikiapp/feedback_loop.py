"""Page feedback that is understood and answered at once (Obsidian flow design §4.2, B1).

POST /wiki/pages/{id}/feedback:
0. the `page_feedback` signal + usage 「整页反馈」 are recorded first (always kept);
1. one understanding call (`wikiapp.understand`); unusable / failed / no gateway → fail closed:
   only the signal stays, the answer says the feedback was not understood (and why);
2. K: concepts in `missing` → 「不会」 on every cell of the concept (same events as POST declare),
   then the one K-change pass on this page (`kchange.k_changed`): sections that became weak unfold,
   a one-line / recap section that now conflicts is patched (the existing patch path);
3. this page: the understanding's `page` plan as a minimal patch — each named section gets the
   added paragraph (marked rewritten), at most one LIBRARY component is placed in a new section
   (a D1 asset, written in the same commit; a configurable component with the understanding's
   validated config, render-checked on component + config right here — no checker or a failing
   check → not placed, the reason in `dropped`) — or, instead of a component, one EvoUI block
   (validated by the understanding call, render-checked here, stored as an `evoui` asset and carried
   in the new section as a ```evoui fence); the record gets a `FeedbackMark`, and a
   `feedback_applied` event keeps the record from before steps 2–3 for undo;
4. No RSI round runs for this one feedback (that retired with RSI-B on 2026-10-29): its signal
   counts toward the user's next RSI round like any other (`rsi` still says whether it was asked).

Steps 2–3 go into ONE commit. When it conflicts (a background curate committed between the load
and the commit), it is retried once on a fresh snapshot, never repeating a model call: the reading
and the placed interactive are kept, the sections the reading is about must still have the content
they had (else PageChanged, as the section rewrite: 409, nothing of steps 2–3 committed), the rest
is recomputed purely, and the K-change pass is recomputed only when it made no model call
(`kchange.k_change_again`; a patch built on a page or K that changed since → WikiConflict;
a recompute may make a NEW patch call for a conflict only the fresh page has: not a repeat,
allowed by R1). When the page moved under the reading itself (PageChanged, or a second conflict),
the feedback — already stored in step 0 — is read again against the fresh page, at most
READ_TRIES - 1 more times (a new understanding call on new content, not a repeat); only when that
keeps conflicting does it raise (409). The signal of step 0 is kept either way.

Undo (POST /wiki/pages/{id}/feedback/{fid}/undo) puts the page back to the record before the
feedback while its content (outline + texts) is still exactly what the feedback left; otherwise 409.
K statements stay (they are the user's)."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from evowiki.belief.aspects import AddNote, AspectEvent, Declare
from evowiki.deep.components import ConfigInvalid, instantiate
from evowiki.deep.contracts import (
    AssetMeta,
    InteractiveDraft,
    LibraryComponent,
    RenderCheck,
    RenderChecker,
    embed_block,
)
from evowiki.deep.evoui import asset_fence, block_json
from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.library import LIBRARY_SECTION
from evowiki.deep.store import AssetStore, StoredAsset, sha256
from evowiki.deep.tools import Checker
from evowiki.goals.contracts import OutlineSection, PageOutline, SectionRole, SectionText, cell_id
from evowiki.goals.fold import Page, states_for
from evowiki.llm.run_context import traced_entry
from evowiki.teach.contracts import FeedbackReading, PageFeedbackIn
from evowiki.wikiapp.events import (
    FeedbackApplied,
    FeedbackMark,
    FeedbackUndone,
    PageRecord,
    PageSaved,
    SectionChange,
    WikiEvent,
)
from evowiki.wikiapp.feedback import page_feedback_flow
from evowiki.wikiapp.kchange import (
    COMMIT_TRIES,
    DECLARE_NOTE,
    KChange,
    k_change_again,
    k_change_restaled,
    k_changed,
    section_content,
)
from evowiki.wikiapp.kwrite import Ids, apply_in_memory, concept_name
from evowiki.wikiapp.llm import LLM_BUDGET, LLM_FAILURES, LLM_UNAVAILABLE, CountingGateway
from evowiki.wikiapp.organize import PageChanged
from evowiki.wikiapp.refine_apply import asset_src
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict
from evowiki.wikiapp.sectionlog import partial_write
from evowiki.wikiapp.state import apply_event
from evowiki.wikiapp.understand import (
    NotUnderstood,
    component_lines,
    component_schemas,
    context,
    understand,
)

RsiStatus = Literal["queued", "not_requested", "no_text", "not_understood"]
FEEDBACK_ACTION = "feedback"
NOT_UNDERSTOOD = "已记录这条反馈，这一页没有改。请切换模型重试"
UNDO_ACTION = "feedback_undo"


class FeedbackSectionOut(BaseModel):
    section_id: str
    heading: str
    change: Literal["added", "inserted"]


class FeedbackComponentOut(BaseModel):
    """The one interactive the feedback placed: a library component, or an EvoUI block
    (`kind="block"`, `component_id` = `BLOCK_ID`, the asset is an `evoui` asset)."""

    kind: Literal["component", "block"] = "component"
    component_id: str
    name: str                            # the placed title (a configurable component's config title)
    asset_id: str
    section_id: str
    why: str = ""                        # why the understanding call picked it


class FeedbackMissingOut(BaseModel):
    concept_id: str
    name: str
    cells: list[str]


class PageFeedbackOut(BaseModel):
    """POST /wiki/pages/{id}/feedback (201)."""

    signal_id: str
    understood: bool
    reason: str = ""                                 # why not understood / not changed
    reading: FeedbackReading | None = None           # 「系统的理解」 + missing + page + style
    dropped: list[str] = []                          # named by the model but not on the page / in the library
    feedback_id: str | None = None                   # set when the page changed (undo needs it)
    summary: str = ""                                # 「这一页按你的反馈改了：…」
    sections: list[FeedbackSectionOut] = []
    component: FeedbackComponentOut | None = None
    missing: list[FeedbackMissingOut] = []           # marked 「不会」
    unfolded: list[str] = []                         # sections the K change unfolded
    # queued: the feedback counts toward the user's next RSI round (the harness loop, due by
    # signal count; the round right after the feedback retired with RSI-B on 2026-10-29)
    rsi: RsiStatus = "not_requested"
    llm_calls: int = 0


class FeedbackUndoOut(BaseModel):
    feedback_id: str
    page_id: str


def _content(rec: PageRecord) -> tuple | None:
    return None if rec.page is None else (rec.page.outline, rec.page.texts)


BLOCK_ID = "evoui-block"
BLOCK_HEIGHT = 400


@dataclass(frozen=True)
class Placed:
    """An interactive ready to place: a library component's HTML (with the config, if configurable)
    or an EvoUI block's JSON (`kind="evoui"`), the title and question the reader sees, and the
    render check that let it in."""

    iid: str                                  # the component id, or BLOCK_ID
    kind: Literal["html", "evoui"]
    content: str                              # the component HTML / the block JSON
    title: str
    question: str
    height: int
    check: RenderCheck


def place(comp: LibraryComponent, config: dict | None,
          checker: RenderChecker | None) -> tuple[Placed | None, str]:
    """(Placed, "") or (None, why not). A fixed-content component is placed as registered (its own
    check); a configurable one needs a valid config and a passing check on component + config."""
    try:
        html, probes, title, question = instantiate(comp, config)
    except ConfigInvalid as e:
        return None, f"组件 {comp.component_id}（config 不合法：{e}）"[:300]
    if not comp.configurable:
        return Placed(comp.component_id, "html", html, title, question, comp.height, comp.check), ""
    if checker is None:
        return None, f"组件 {comp.component_id}（没有渲染检查，不放可配置组件）"
    check = checker.check(InteractiveDraft(
        interactive_id=comp.component_id, section_id=LIBRARY_SECTION, title=title,
        learning_question=question, html=html, height=comp.height, probes=probes))
    if not check.ok:
        return None, f"组件 {comp.component_id}（这份 config 没通过渲染检查：{check.detail}）"[:300]
    return Placed(comp.component_id, "html", html, title, question, comp.height, check), ""


def place_block(block: dict, checker: Checker | None) -> tuple[Placed | None, str]:
    """(Placed, "") or (None, why not): the understanding's EvoUI block (already validated there)
    through the block render check; no checker → not placed (fail closed)."""
    try:
        b = EvoUIBlock.model_validate(block)
    except ValueError as e:
        return None, f"EvoUI 块（不合法：{e}）"[:300]
    if checker is None:
        return None, "EvoUI 块（没有渲染检查，不放）"
    check = checker.check_block(BLOCK_ID, b)
    if not check.ok:
        return None, f"EvoUI 块（没通过渲染检查：{check.detail}）"[:300]
    return Placed(BLOCK_ID, "evoui", block_json(b), b.title, b.question, BLOCK_HEIGHT, check), ""


def _patch(rec: PageRecord, reading: FeedbackReading, placed: Placed | None, *,
           fid: str, user_id: str, now: datetime, new_id: Ids
           ) -> tuple[PageRecord, list[FeedbackSectionOut], FeedbackComponentOut | None,
                      list[StoredAsset]]:
    """The page plan applied to `rec` (sections that no longer exist are skipped)."""
    page = rec.page
    assert page is not None
    plan = reading.page
    texts = dict(page.texts)
    changed: dict[str, SectionChange] = dict(rec.changed)
    secs = list(page.outline.sections)
    by_id = {s.id: s for s in secs}
    out: list[FeedbackSectionOut] = []
    for a in plan.sections:
        t = texts.get(a.section_id)
        if t is None or a.section_id not in by_id or page.state(a.section_id).pinned:
            continue
        texts[a.section_id] = t.model_copy(update={"body": f"{t.body.rstrip()}\n\n{a.add}",
                                                   "note_body": None,
                                                   "cache_key": f"feedback:{fid}:{a.section_id}"})
        changed[a.section_id] = "rewritten"
        out.append(FeedbackSectionOut(section_id=a.section_id,
                                      heading=by_id[a.section_id].heading, change="added"))
    comp_out = None
    assets: list[StoredAsset] = []
    if placed is not None:
        sid = new_id("fs")
        asset_id = new_id("ia")
        meta = AssetMeta(asset_id=asset_id, page_id=rec.page_id, section_id=sid,
                         interactive_id=placed.iid, title=placed.title[:120],
                         height=placed.height, sha256=sha256(placed.content), check=placed.check,
                         kind=placed.kind)
        assets.append(StoredAsset(user_id=user_id, meta=meta, html=placed.content,
                                  job_id=f"feedback:{fid}", created_at=now))
        shown = (asset_fence(asset_id) if placed.kind == "evoui"
                 else embed_block(meta, asset_src(asset_id)))
        taught = [s.id for s in secs if s.role is not SectionRole.CHECK]
        after = plan.component_after if plan.component_after in by_id else (
            taught[0] if taught else None)
        at = next((i + 1 for i, s in enumerate(secs) if s.id == after), len(secs))
        secs.insert(at, OutlineSection(id=sid, heading=f"动手看一看：{placed.title}"[:120],
                                       role=SectionRole.EXPLAIN, cells=()))
        texts[sid] = SectionText(section_id=sid, cue=placed.title[:30],
                                 body=f"{placed.question}\n\n{shown}",
                                 cache_key=f"feedback:{fid}:{sid}")
        changed[sid] = "inserted"
        block = placed.kind == "evoui"
        comp_out = FeedbackComponentOut(kind="block" if block else "component",
                                        component_id=placed.iid, name=placed.title,
                                        asset_id=asset_id, section_id=sid,
                                        why=plan.block_why if block else plan.component_why)
    if not out and comp_out is None:
        return rec, [], None, []
    outline = PageOutline(**{**page.outline.model_dump(), "sections": tuple(secs)})
    new_page = Page(outline=outline, states=states_for(outline, page.states), texts=texts,
                    snapshot=page.snapshot, unresolved_conflicts=page.unresolved_conflicts,
                    conflicts=page.conflicts)
    order = [s.id for s in outline.sections
             if s.id in {o.section_id for o in out} or (comp_out and s.id == comp_out.section_id)]
    mark = FeedbackMark(feedback_id=fid, at=now, summary=plan.summary, reading=reading.reading,
                        sections=tuple(order), assets=tuple(a.meta.asset_id for a in assets))
    return (rec.model_copy(update={"page": new_page, "changed": changed, "feedback": mark}),
            out, comp_out, assets)


READ_TRIES = 3          # the reading, then at most two fresh re-readings when the page moved under it


@traced_entry("feedback", subject="page_id")
def page_feedback_loop(repo: Repo, gw: CountingGateway | None, user_id: str, page_id: str,
                       body: PageFeedbackIn, *, now: datetime, new_id: Ids,
                       components: Sequence[LibraryComponent] = (),
                       assets: AssetStore | None = None,
                       checker: Checker | None = None) -> PageFeedbackOut:
    """See the module docstring. `components`: the library (empty → no component can be placed);
    `assets`: where a placed component's asset is written (None → components are not placed);
    `checker`: render-checks a configurable component with its config, or the EvoUI block (None →
    only fixed-content components can be placed)."""
    signal_id = page_feedback_flow(repo, user_id, page_id, body, now=now,
                                   new_id=new_id).signal_id
    calls0 = gw.calls if gw is not None else 0

    def out(**kw) -> PageFeedbackOut:
        kw.setdefault("rsi", "not_understood")
        return PageFeedbackOut(signal_id=signal_id,
                               llm_calls=(gw.calls - calls0) if gw is not None else 0, **kw)

    if gw is None:
        return out(understood=False, reason=NOT_UNDERSTOOD)
    library = {c.component_id: c for c in components} if assets is not None else {}
    for read in range(READ_TRIES):
        try:
            return _read_and_apply(repo, gw, user_id, page_id, body, out, library, signal_id,
                                   now=now, new_id=new_id, assets=assets, checker=checker)
        except (PageChanged, WikiConflict):
            if read + 1 == READ_TRIES:
                raise
            logging.getLogger(__name__).info("page moved under a feedback reading; reading again")
    raise AssertionError("unreachable")


def _read_and_apply(repo: Repo, gw: CountingGateway, user_id: str, page_id: str,
                    body: PageFeedbackIn, out, library: dict, signal_id: str, *, now: datetime,
                    new_id: Ids, assets: AssetStore | None,
                    checker: Checker | None) -> PageFeedbackOut:
    """Steps 1–3 on a fresh snapshot: the understanding call, then the patch, whose conflicting
    commit is retried once without a model call (module doc). PageChanged / WikiConflict when the
    page moved under the reading (the caller reads again)."""
    snap = repo.load(user_id)
    try:
        ctx = context(snap, page_id, now, component_lines(library.values()),
                      component_schemas(library.values()))
    except ValueError:
        return out(understood=False, reason="页面还没生成：只记下了这条反馈")
    try:
        u = understand(gw, ctx, body, subject=f"{user_id}/{page_id}")
    except NotUnderstood:
        return out(understood=False, reason=NOT_UNDERSTOOD)
    except (*LLM_FAILURES, *LLM_UNAVAILABLE, *LLM_BUDGET):
        logging.getLogger(__name__).warning("feedback understanding call failed", exc_info=True)
        return out(understood=False,
                   reason=NOT_UNDERSTOOD)
    reading = u.reading
    # the interactive to place: from the reading and the library only, computed once
    dropped = list(u.dropped)
    placed = None
    if reading.page.component and reading.page.component in library:
        placed, why_not = place(library[reading.page.component], reading.page.component_config,
                                checker)
        if why_not:
            dropped.append(why_not)
    elif reading.page.block is not None:
        if assets is None:
            dropped.append("EvoUI 块（这里不能存交互，不放）")
        else:
            placed, why_not = place_block(reading.page.block, checker)
            if why_not:
                dropped.append(why_not)
    rsi: RsiStatus = ("queued" if body.apply and body.text.strip() else
                      "no_text" if body.apply else "not_requested")

    # 2.–3. on the snapshot; a conflicting commit is retried once on a fresh one (module doc)
    first: _Applied | None = None
    for attempt in range(COMMIT_TRIES):
        if first is not None:
            snap = repo.load(user_id)
            _require_targets(first.snap, snap, page_id, reading, body)
        done = _apply(snap, first, gw, reading, placed, signal_id, user_id, page_id,
                      now=now, new_id=new_id)
        if done.new is done.rec and not done.aspect:
            return out(understood=True, reading=reading, dropped=dropped, rsi=rsi,
                       reason="理解了，但这一页不需要改", missing=done.missing)
        extra = [assets.writer(done.stored)] if done.stored and assets is not None else []
        try:
            repo.commit(snap, Commit(aspect=done.aspect, wiki=done.wiki, extra=extra))
            break
        except WikiConflict:
            if attempt + 1 == COMMIT_TRIES:
                raise
            first = done
    return out(understood=True, reading=reading, dropped=dropped, feedback_id=done.fid,
               summary=reading.page.summary if (done.sections or done.comp) else "",
               sections=done.sections, component=done.comp, missing=done.missing,
               unfolded=done.unfolded, rsi=rsi)


@dataclass(frozen=True)
class _Applied:
    """One attempt of steps 2–3 on `snap`: K events, the K-change pass, the patched page and every
    wiki event of the one commit."""

    snap: Snapshot
    aspect: list[AspectEvent]
    kc: KChange | None
    missing: list[FeedbackMissingOut]
    unfolded: list[str]
    fid: str
    rec: PageRecord                      # the page after the K-change pass
    new: PageRecord                      # … and after the feedback's patch
    sections: list[FeedbackSectionOut]
    comp: FeedbackComponentOut | None
    stored: list[StoredAsset]
    wiki: list[WikiEvent]


def _apply(snap: Snapshot, first: "_Applied | None", gw: CountingGateway, reading: FeedbackReading,
           placed: Placed | None, signal_id: str, user_id: str, page_id: str, *, now: datetime,
           new_id: Ids) -> _Applied:
    """Steps 2–3 on `snap`, pure but for the K-change pass. On a retry (`first` set) the missing
    concepts are re-read against the fresh K (a concept K no longer has gets no 「不会」), sections
    the page no longer has are skipped (`_patch`), and the K-change pass follows
    `kchange.k_change_again`: recomputed when the first one made no model call, re-applied with
    the first attempt's K events when it made one and nothing it read changed, else WikiConflict."""
    original = snap.state.pages[page_id]

    # 2. K: 「不会」 on the missing concepts, then the K-change pass on this page
    missing_out: list[FeedbackMissingOut] = []
    aspect: list[AspectEvent] = []
    for cid in reading.missing:
        cells = [cell_id(cid, a) for a in snap.k.aspects(cid)] if cid in snap.k.concept_ids() else []
        for cell in cells:
            aspect += [Declare(event_id=new_id("aev"), ts=now, cell=cell, value="dont"),
                       AddNote(event_id=new_id("aev"), ts=now, cell=cell,
                               text=DECLARE_NOTE["dont"], evidence_ref=f"feedback:{signal_id}")]
        missing_out.append(FeedbackMissingOut(concept_id=cid, cells=cells,
                                              name=concept_name(snap.state, cid,
                                                                user_id=user_id)))
    kc: KChange | None = None
    if aspect:
        if first is not None and first.kc is not None and k_change_again(
                first.kc, first.snap, snap, page_id) == "reuse":
            aspect = first.aspect
            kc = k_change_restaled(first.kc, snap, apply_in_memory(snap, aspect), page_id, now)
        else:
            kc = k_changed(snap, apply_in_memory(snap, aspect), page_id, gw,
                           skip=None, now=now, new_id=new_id, action="declare", declared_too=True)
    unfolded = [s for s, m in (kc.record.changed.items() if kc and kc.record else ())
                if m == "unfolded"]
    k_wiki = kc.wiki if kc is not None else []
    state = snap.state
    for e in k_wiki:                      # the page as the K-change pass leaves it
        state = apply_event(state, e)

    # 3. this page
    fid = new_id("fb")
    rec = state.pages[page_id]
    new, sections, comp, stored = _patch(rec, reading, placed, fid=fid, user_id=user_id,
                                         now=now, new_id=new_id)
    wiki: list[WikiEvent] = list(k_wiki)
    logs: list[WikiEvent] = []
    if new is not rec and new.feedback is not None:
        new, logs = partial_write(
            snap, new, new.feedback.sections, kind="feedback", ref=fid, producer="writer",
            now=now, new_id=new_id, state=state,
            k=apply_in_memory(snap, aspect) if aspect else None)
    if new is not rec or aspect:
        wiki += [WikiEvent(event_id=new_id("wev"), ts=now,
                           body=PageSaved(record=new, action=FEEDBACK_ACTION)),
                 *logs,
                 WikiEvent(event_id=new_id("wev"), ts=now,
                           body=FeedbackApplied(feedback_id=fid, page_id=page_id,
                                                signal_id=signal_id, before=original, after=new))]
    return _Applied(snap=snap, aspect=aspect, kc=kc, missing=missing_out, unfolded=unfolded,
                    fid=fid, rec=rec, new=new, sections=sections, comp=comp, stored=stored,
                    wiki=wiki)


PAGE_CHANGED = "这一页刚被别的流程改过，这次没有按反馈改它（反馈已经记下），请刷新后重试"


def _require_targets(first: Snapshot, snap: Snapshot, page_id: str, reading: FeedbackReading,
                     body: PageFeedbackIn) -> None:
    """R3 of the retry: the sections the reading is about (the ones it adds to, the one the
    component goes after, the one the user named) must have the content they had on the first
    snapshot; a section that is gone is skipped (`_patch` drops it), one whose content differs
    → PageChanged (as 「再讲深一点」: nothing committed, the route answers 409). The page gone or
    unorganized → PageChanged."""
    now_rec = snap.state.pages.get(page_id)
    if now_rec is None or now_rec.page is None:
        raise PageChanged(PAGE_CHANGED)
    was = first.state.pages[page_id]
    targets = {a.section_id for a in reading.page.sections}
    targets |= {x for x in (reading.page.component_after, body.section_id) if x}
    for sid in sorted(targets):
        before, after = section_content(was, sid), section_content(now_rec, sid)
        if before is not None and after is not None and before != after:
            raise PageChanged(PAGE_CHANGED)


def undo_feedback(repo: Repo, user_id: str, page_id: str, feedback_id: str, *, now: datetime,
                  new_id: Ids) -> FeedbackUndoOut:
    """404 unknown feedback (or another page's); 409 already undone, or the page changed since."""
    snap = repo.load(user_id)
    fb = snap.state.feedbacks.get(feedback_id)
    if fb is None or fb.page_id != page_id:
        raise LookupError(f"没有这条反馈改动：{feedback_id}")
    if feedback_id in snap.state.feedbacks_undone:
        raise WikiConflict("这条反馈的改动已经撤回了")
    cur = snap.state.pages[page_id]
    if _content(cur) != _content(fb.after):
        raise WikiConflict("这一页在这条反馈之后又改过，不能直接撤回")
    restored = fb.before.model_copy(update={"done": cur.done, "checks": cur.checks})
    repo.commit(snap, Commit(wiki=[
        WikiEvent(event_id=new_id("wev"), ts=now, body=PageSaved(record=restored,
                                                                 action=UNDO_ACTION)),
        WikiEvent(event_id=new_id("wev"), ts=now, body=FeedbackUndone(feedback_id=feedback_id))]))
    return FeedbackUndoOut(feedback_id=feedback_id, page_id=page_id)
