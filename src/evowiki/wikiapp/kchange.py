"""The one K-change path (plan Task 9): K change → fold the current page and hand its conflicts to
the page writer → other pages
marked 「待更新」. Section feedback, embedded check answers and cell declarations
all compute their aspect events first, apply them in memory (`apply_in_memory`), call `k_changed`
and commit aspect + wiki events in one transaction (D2).

`k_changed` reuses `page.update_page` for the current page (0 calls, as on open). Other
pages are never touched (D7): `other_pages_stale` lists the organized pages whose bands moved
between the snapshot K and the new K, 0 calls.

Results are structured (D17): `ChangeOut` has no note. Note text is raw (D21): the section heading
(feedback), the chosen option text or the stem for 不知道 (check), and
"know" / "dont" / "clear" (declare)."""

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from evowiki.belief.aspects import AddNote, AspectEvent, AspectK, AspectKView, Declare
from evowiki.deep.agent import RefineFailed
from evowiki.goals.contracts import SectionRole, split_cell
from evowiki.goals.diagnostic import IDK, answer_observation
from evowiki.goals.fold import moved_cells
from evowiki.goals.guardrails import page_bands, section_of
from evowiki.goals.page_update import feedback, feedback_observations, feedback_outline
from evowiki.goals.rewrite_knobs import feedback_instruction
from evowiki.llm.budget import JobBudget
from evowiki.sources.cite import CiteIssue
from evowiki.teach.contracts import SectionLog
from evowiki.wikiapp.events import (
    PageRecord,
    PageSaved,
    SectionChange,
    UsageAdded,
    UsageRecord,
    WikiEvent,
)
from evowiki.wikiapp.kwrite import (
    IDK_LABEL,
    Ids,
    answer_label,
    apply_in_memory,
    observation_events,
    page_spec,
)
from evowiki.wikiapp.llm import (
    LLM_BUDGET,
    LLM_FAILURES,
    LLM_UNAVAILABLE,
    CountingGateway,
)
from evowiki.wikiapp.organize import (
    OrganizeItem,
    PageChanged,
    SourcesFailed,
    WriterMissing,
    Writing,
    _error,
    _Stream,
    check_citations,
    read_sources,
    writer_reader,
)
from evowiki.wikiapp.page import section_changes, update_page
from evowiki.wikiapp.page_runs import FeedbackArgs, PageJob, PageRun, PageRuns
from evowiki.wikiapp.refine_apply import (
    content,
    make_assets,
    page_sources,
    refine_input,
    written_record,
)
from evowiki.wikiapp.repo import (
    Commit,
    Repo,
    Snapshot,
    WikiConflict,
    WikiInvalid,
    commit_signal,
    inference_trust,
)
from evowiki.wikiapp.sectionlog import logs_event, section_logs
from evowiki.wikiapp.signals import IDK_TEXT, signal_event
from evowiki.wikiapp.teachspec import SpecInvalid, page_teach
from evowiki.wikiapp.views import cell_belief_out, conflicts_out, record_section_out
from evowiki.wikiapp.wiki_schemas import (
    ChangeOut,
    CheckAnswerOut,
    DeclareOut,
    FeedbackOut,
    RewriteDoneEvent,
    SectionDeltaEvent,
    SectionDoneEvent,
    WriterEvent,
)

log = logging.getLogger(__name__)

# usage records (prototype action labels); the curator reads them later
KNOW_USAGE = "这部分我懂"
DEEPER_USAGE = "再讲深一点"
OTHER_USAGE = "换个讲法"
REWRITE_USAGE = {"deeper": DEEPER_USAGE, "other": OTHER_USAGE}
REWRITE_SIGNAL: dict[str, Literal["section_deeper", "section_other"]] = {
    "deeper": "section_deeper", "other": "section_other"}
DECLARE_NOTE = {"know": "know", "dont": "dont", None: "clear"}
ANSWERED = "这道题已经答过了"


@dataclass(frozen=True)
class KChange:
    record: PageRecord | None          # the updated current page (None: no page to update)
    change: ChangeOut
    wiki: list[WikiEvent]              # PageSaved for the current page when it changed
    calls: int = 0                     # model calls this pass made itself (0: it makes none)


def _calls(gw: CountingGateway | None) -> int:
    return gw.calls if gw is not None else 0


def _stale_others(snap: Snapshot, k_after: AspectK, page_id: str | None,
                  now: datetime) -> list[str]:
    """Organized pages other than `page_id` whose bands moved between the snapshot K and
    `k_after` (D7: nothing is written for them, 0 calls)."""
    before, after = snap.kview(now), AspectKView(k_after, now)
    out: list[str] = []
    for p in snap.state.page_order:
        if p == page_id or not snap.state.pages[p].has_body:
            continue
        b = page_bands(page_spec(snap.state, snap.k, p, user_id=snap.user_id, bands_only=True),
                       before)
        a = page_bands(page_spec(snap.state, k_after, p, user_id=snap.user_id, bands_only=True),
                       after)
        if moved_cells(b, a):
            out.append(p)
    return out


def _own_first(own: Mapping[str, SectionChange], kind: SectionChange,
               rest: list[str]) -> list[str]:
    first = [sid for sid, m in own.items() if m == kind]
    return first + [sid for sid in rest if sid not in first]


def k_changed(snap: Snapshot, k_after: AspectK, page_id: str | None,
              gw: CountingGateway | None, *, skip: str | None,
              now: datetime, new_id: Ids, record: PageRecord | None = None,
              own: Mapping[str, SectionChange] | None = None, action: str = "k_changed",
              calls0: int | None = None, logs: Sequence[SectionLog] = (),
              declared_too: bool = False) -> KChange:
    """The single K-change entry point. `record`: the current page as the user's own action left
    it (default: the snapshot's); `own`: that action's marks on its own sections (listed first in
    the change); `calls0`: the gateway counter at the start of the request (default: now);
    `logs`: SectionLogs of the caller's own sections (the writer's rewrite); `declared_too`: this
    pass also hands over cells marked 「不会」 (`goals.page_update`).
    The current page (organized only) goes through `update_page(opening=False, skip=skip)`;
    the sections it hands to the writer get `patch` SectionLogs (they wait for it:
    `stale.patch_pending`), committed with `logs` in one `section_logs_saved` right after the
    PageSaved. SpecInvalid (>30 old guidelines) propagates."""
    start = _calls(gw)
    calls0 = start if calls0 is None else calls0
    own = dict(own or {})
    stale = _stale_others(snap, k_after, page_id, now)
    if page_id is not None and record is None:
        record = snap.state.pages.get(page_id)
    # a page a run is writing is left to that run: changing the page would stop it
    if record is None or not record.has_body:
        return KChange(record=record, wiki=[], change=ChangeOut(
            llm_calls=_calls(gw) - calls0, other_pages_stale=stale))
    original = snap.state.pages.get(record.page_id)
    teach = page_teach(snap.state, record.page_id)
    spec = page_spec(snap.state, k_after, record.page_id, user_id=snap.user_id, teach=teach)
    kview = AspectKView(k_after, now)
    res = update_page(spec, record.page, kview, opening=False, skip=skip,
                      declared_too=declared_too)
    marks, patched = section_changes(res)
    marks = {**own, **{s: m for s, m in marks.items() if s not in own}}
    new_rec = record.model_copy(update={
        "page": res.page, "changed": marks if marks else record.changed})
    wiki = ([] if new_rec == original else
            [WikiEvent(event_id=new_id("wev"), ts=now,
                       body=PageSaved(record=new_rec, action=action))])
    if wiki:                                     # logs only go with the PageSaved they explain
        own_ids = {g.section_id for g in logs}
        mine = [x for x in patched if x not in own_ids]
        patch_logs = (section_logs(res.page.outline, mine, producer="patch", spec=spec, k=kview,
                                   teach=teach, at=now, infer_version=snap.infer_version)
                      if mine else ())
        wiki += logs_event((*logs, *patch_logs), new_id=new_id, now=now)
    change = ChangeOut(
        rewritten=_own_first(own, "rewritten", [x for x in patched if marks[x] == "rewritten"]),
        inserted=_own_first(own, "inserted", []),
        folded=_own_first(own, "folded", [x for x in res.folded if x not in own]),
        unfolded=[x for x in res.unfolded if x not in own],
        llm_calls=_calls(gw) - calls0, other_pages_stale=stale,
        unresolved_conflicts=list(res.page.unresolved_conflicts),
        conflicts=conflicts_out(res.page))
    return KChange(record=new_rec, change=change, wiki=wiki, calls=_calls(gw) - start)


# ---- a commit retried on a fresh snapshot (a background curate got in first) ----

COMMIT_TRIES = 2       # the understood-feedback flows: the first commit plus one retry


def _k_inputs(snap: Snapshot, page_id: str, record: PageRecord | None) -> tuple:
    """Everything `k_changed` reads for page `page_id` besides the aspect events: K (its aspect
    log position, θ_infer version and trusted prereqs), the page's record as stored and as passed,
    its spec and its teaching spec."""
    stored = snap.state.pages.get(page_id)
    spec = teach = None
    if stored is not None and stored.page is not None:
        teach = page_teach(snap.state, page_id)
        spec = page_spec(snap.state, snap.k, page_id, user_id=snap.user_id, teach=teach)
    return (snap.aspect_seq, snap.infer_version, inference_trust(snap.state), stored,
            record if record is not None else stored, spec, teach)


def k_change_again(first: KChange, first_snap: Snapshot, snap: Snapshot, page_id: str | None, *,
                   first_record: PageRecord | None = None,
                   record: PageRecord | None = None) -> Literal["recompute", "reuse"]:
    """How a flow retrying its commit on the fresh snapshot `snap` gets its K-change pass back
    without repeating a model call that `first` (computed on `first_snap` with `first_record`)
    made:

    - "recompute": `first` made no model call (the K-change pass makes none since the outline-patch
      step was removed, 2026-10-28): call `k_changed` again on `snap` (0 calls);
    - "reuse": it made a call (a patch, or a call that failed and degraded) and everything it
      read is unchanged on `snap` (same K: aspect log position, θ_infer version, trusted prereqs;
      the same stored and passed record, page spec and teaching spec): the caller re-applies
      `first` with the first attempt's aspect events (`k_change_restaled`: only the other pages'
      stale list is read again);
    - otherwise its patch was built on a page or a K that changed since: WikiConflict (409,
      fail closed), never re-called."""
    if first.calls == 0:
        return "recompute"
    if page_id is not None and _k_inputs(first_snap, page_id, first_record) == _k_inputs(
            snap, page_id, record):
        return "reuse"
    raise WikiConflict()


def k_change_restaled(first: KChange, snap: Snapshot, k_after: AspectK, page_id: str | None,
                      now: datetime) -> KChange:
    """`first` re-applied on `snap` ("reuse" above): its record and wiki events as they were (not
    stored: their commit conflicted), the other pages' stale list read from `snap`."""
    stale = _stale_others(snap, k_after, page_id, now)
    return KChange(record=first.record, wiki=first.wiki, calls=first.calls,
                   change=first.change.model_copy(update={"other_pages_stale": stale}))


def section_content(rec: PageRecord | None, sid: str) -> tuple | None:
    """A section's content (outline entry + text) on a stored page; None when it is not there."""
    if rec is None or rec.page is None:
        return None
    hit = next((x for x in rec.page.outline.sections if x.id == sid), None)
    return None if hit is None else (hit, rec.page.texts.get(sid))


SECTION_CHANGED = "这一节刚被别的流程改过，这次没有改，请刷新后重试"


# ---- shared pieces ----

def _wev(new_id: Ids, now: datetime, body) -> WikiEvent:
    return WikiEvent(event_id=new_id("wev"), ts=now, body=body)


def _usage(new_id: Ids, now: datetime, action: str, page_id: str, *, heading: str | None = None,
           text: str | None = None) -> WikiEvent:
    return _wev(new_id, now, UsageAdded(record=UsageRecord(action=action, page=page_id,
                                                           heading=heading, text=text)))


def _record(snap: Snapshot, page_id: str) -> PageRecord:
    rec = snap.state.pages.get(page_id)
    if rec is None:
        raise LookupError(f"没有这个页面：{page_id}")
    return rec


def _organized_section(snap: Snapshot, page_id: str, sid: str):
    rec = _record(snap, page_id)
    if rec.page is None or all(s.id != sid for s in rec.page.outline.sections):
        raise LookupError(f"没有这一段：{sid}")
    return rec, section_of(rec.page.outline, sid)


def _feedback_target(snap: Snapshot, page_id: str, sid: str):
    """Feedback buttons exist on prose sections that are not pinned (prototype)."""
    rec, s = _organized_section(snap, page_id, sid)
    if s.role is SectionRole.CHECK:
        raise WikiInvalid(f"确认题不能反馈：{sid}")
    if rec.page.state(sid).pinned:
        raise WikiInvalid(f"这一段已钉住，不能反馈：{sid}")
    return rec, s


def _commit(repo: Repo, snap: Snapshot, aspect: list[AspectEvent],
            wiki: list[WikiEvent]) -> Snapshot:
    return repo.commit(snap, Commit(aspect=aspect, wiki=wiki))


# ---- section feedback ----

def feedback_know(repo: Repo, gw: CountingGateway | None, user_id: str, page_id: str, sid: str,
                  *, now: datetime, new_id: Ids, shown: str | None = None) -> FeedbackOut:
    """「这部分我懂」: fold + lock (0 calls), `+0.6` feedback observations, then the K-change
    pass (0 calls: a real conflict is handed to the page writer).

    A commit that conflicts (a background curate got in first) is retried once on a fresh
    snapshot; a second conflict raises WikiConflict (409). There the fold and the observations
    are recomputed (pure) and the K-change pass follows `k_change_again`: recomputed when it made
    no model call, re-applied when it made one and its inputs are unchanged, else WikiConflict.
    The section itself changed in content (or is gone) since the first load → PageChanged and
    nothing is committed (the route answers 409 with its text, as 「再讲深一点」 answers
    `conflict`)."""
    calls0 = _calls(gw)
    first: tuple[Snapshot, PageRecord, list[AspectEvent], KChange] | None = None
    for attempt in range(COMMIT_TRIES):
        snap = repo.load(user_id)
        if first is not None and section_content(snap.state.pages.get(page_id), sid) != (
                section_content(first[0].state.pages.get(page_id), sid)):
            raise PageChanged(SECTION_CHANGED)
        rec, s = _feedback_target(snap, page_id, sid)
        spec = page_spec(snap.state, snap.k, page_id, user_id=snap.user_id)
        res = feedback(spec, rec.page, sid, "know")
        fed = rec.model_copy(update={"page": res.page})
        mode = ("recompute" if first is None else
                k_change_again(first[3], first[0], snap, page_id, first_record=first[1],
                               record=fed))
        if first is not None and mode == "reuse":
            aspect = first[2]
            kc = k_change_restaled(first[3], snap, apply_in_memory(snap, aspect), page_id, now)
        else:
            heading = s.heading
            # 「已掌握」 takes back his own 「不会」 on these cells before it counts as evidence
            kview0 = snap.kview(now)
            aspect = [Declare(event_id=new_id("aev"), ts=now, cell=c, value=None)
                      for c in dict.fromkeys(s.cells) if kview0.cell(c).declared == "dont"]
            aspect += observation_events(res.observations, ts=now, new_id=new_id,
                                         note_text=lambda _o, h=heading: h)
            own: dict[str, SectionChange] = (
                {sid: "folded"} if res.page.state(sid).folded and not rec.page.state(sid).folded
                else {})
            kc = k_changed(snap, apply_in_memory(snap, aspect), page_id, gw,
                           skip=sid, now=now, new_id=new_id, record=fed, own=own,
                           action="feedback", calls0=calls0)
        wiki = [*kc.wiki, _usage(new_id, now, KNOW_USAGE, page_id, heading=s.heading),
                signal_event(snap.state, "section_know", now=now, new_id=new_id,
                             page_id=page_id, section_id=sid, target=shown)]
        try:
            done = _commit(repo, snap, aspect, wiki)
            break
        except WikiConflict:
            if attempt + 1 == COMMIT_TRIES:
                raise
            first = (snap, fed, aspect, kc)
    return FeedbackOut(section=record_section_out(done.state, done.kview(now),
                                                  done.state.pages[page_id], sid,
                                                  user_id=user_id),
                       change=kc.change)


def feedback_rewrite_stream(repo: Repo, gw: CountingGateway, user_id: str, page_id: str,
                            sid: str, kind: Literal["deeper", "other"], *, now: datetime,
                            new_id: Ids, writing: Writing | None,
                            runs: PageRuns, shown: str | None = None) -> Iterator[OrganizeItem]:
    """「再讲深一点」 / 「换个讲法」 as SSE items, written by the page writer (single writer): only
    this section is rewritten (scope), streamed as it is written. Validates eagerly (unknown page /
    section → LookupError, check or pinned section → WikiInvalid) before any item, then starts a
    `feedback` page run in `runs` (required: inline runs use the arguments given here, the service's DBOS
    runs their installed deps) and follows it. Every click is its own run (`attach=False`); leaving the stream does not
    cancel the write."""
    if kind not in REWRITE_USAGE:
        raise WikiInvalid(f"未知的反馈：{kind}")
    _feedback_target(repo.load(user_id), page_id, sid)
    from evowiki.wikiapp.page_flows import PageDeps  # page_flows imports this module

    deps = PageDeps(repo=repo, gateway=lambda _uid: gw, writing=writing, store=runs.store,
                    clock=lambda: now, new_id=new_id)
    run_id, _ = runs.start(
        PageJob(kind="feedback", user_id=user_id, page_id=page_id, now=now,
                feedback=FeedbackArgs(section_id=sid, kind=kind, shown=shown)), deps, attach=False)
    return runs.follow(run_id)


def _rewrite(run: PageRun, repo: Repo, gw: CountingGateway, snap: Snapshot, rec: PageRecord,
             heading: str, sid: str, kind: Literal["deeper", "other"], *, now: datetime,
             new_id: Ids, writing: Writing | None, shown: str | None = None) -> None:
    calls0 = gw.calls
    page_id = rec.page_id
    signal = signal_event(snap.state, REWRITE_SIGNAL[kind], now=now, new_id=new_id,
                          page_id=page_id, section_id=sid, target=shown)
    try:
        if writing is None:
            raise WriterMissing("没有配置写作模型，不能改写")
        assert rec.page is not None
        teach = page_teach(snap.state, page_id)
        spec = page_spec(snap.state, snap.k, page_id, user_id=snap.user_id, teach=teach)
        kview = snap.kview(now)
        obs = feedback_observations(spec, rec.page, sid, kind)
        target = rec.page.model_copy(update={"outline": feedback_outline(rec.page, sid, kind)})
        inp = refine_input(snap, page_id, now, writing.specs, page=target, scope=(sid,),
                           mode="feedback",
                           instruction=feedback_instruction(kind, heading))   # rewrite knobs
        inp = read_sources(inp, rec.concepts, writing, snap.state)
        stream = _Stream(run, snap.state, kview, [x.id for x in target.outline.sections],
                         user_id=snap.user_id)
        result = writing.writer.write(
            inp, budget=JobBudget(snap.state.refine.per_page_limit_usd),
            on_writer=lambda model, fb, why: run.emit("writer", WriterEvent(
                model=model, fallback=fb, reason=why)),
            on_change=stream.on_change, reader=writer_reader(inp, writing))
        draft, cite_issues = check_citations(result.outcome.draft, inp, writing)
        if draft.needs_assets and writing.assets is None:
            raise RefineFailed("internal", "没有交互资源库，不能提交带交互的改写")
        stored = make_assets(draft, result.outcome.checks, user_id=snap.user_id,
                             job_id=run.run_id, now=now, new_id=new_id)
        extra = [writing.assets.writer(stored)] if stored and writing.assets is not None else []
        base = content(rec)
        cur_snap, cur = snap, rec
        first: _FirstK | None = None       # attempt 0's K-change pass (「再讲深一点」)
        for attempt in range(REWRITE_COMMIT_TRIES):
            if attempt:                    # the wiki moved during the write: a fresh snapshot
                cur_snap = repo.load(snap.user_id)
                cur = cur_snap.state.pages.get(page_id)
                if cur is None or content(cur) != base:
                    raise PageChanged("这一页在改写的时候被别的流程改过，这次没有写入")
            done = _rewrite_events(cur_snap, cur, draft, stored, result, inp, spec, teach, kview,
                                   gw, run, sid, kind, heading, obs, signal, calls0,
                                   now=now, new_id=new_id, cite_issues=cite_issues, first=first)
            record, change, aspect, wiki, kc, fed = done
            if first is None and kc is not None:
                first = (cur_snap, fed, aspect, kc)
            try:
                repo.commit(cur_snap, Commit(aspect=aspect, wiki=wiki, extra=extra))
                break
            except WikiConflict:
                if attempt + 1 == REWRITE_COMMIT_TRIES:
                    raise
        assert record is not None and record.page is not None
        stream.final(record.page)
        own = record.written.sections if record.written else ()
        for other in (*change.rewritten, *change.inserted):
            if other in own or other not in record.page.texts:
                continue
            t = record.page.texts[other]
            run.emit("section_delta", SectionDeltaEvent(section_id=other, cue=t.cue, body=t.body))
            run.emit("section_done", SectionDoneEvent(section_id=other, cached=False))
        run.emit("change", change)
    except (WikiConflict, PageChanged, SpecInvalid, WriterMissing, SourcesFailed, RefineFailed,
            *LLM_FAILURES, *LLM_UNAVAILABLE, *LLM_BUDGET) as exc:
        commit_signal(repo, snap.user_id, signal)
        run.emit("error", _error(exc))
        return
    run.emit("done", RewriteDoneEvent(page_id=page_id, llm_calls=gw.calls - calls0 + result.calls,
                                      cost_usd=result.cost_usd, model=result.model,
                                      fallback=bool(result.fallback_reason)))


REWRITE_COMMIT_TRIES = 3

# attempt 0's K-change pass of a section rewrite: (snapshot, the record passed, K events, KChange)
_FirstK = tuple[Snapshot, PageRecord, list[AspectEvent], KChange]


def _rewrite_events(snap: Snapshot, rec: PageRecord, draft, stored, result, inp, spec, teach,
                    kview, gw: CountingGateway, run: PageRun, sid: str,
                    kind: Literal["deeper", "other"], heading: str, obs, signal: WikiEvent,
                    calls0: int, *, now: datetime, new_id: Ids,
                    cite_issues: tuple[CiteIssue, ...] = (), first: _FirstK | None = None):
    """(record, change, aspect events, wiki events, KChange or None, the record passed to it) of a
    written section rewrite on `snap` (the K-change pass included for 「再讲深一点」); recomputed
    when a commit has to be retried. On a retry (`first`: attempt 0's pass, always attempt 0's)
    the K-change pass follows `k_change_again`, so a patch call is never repeated: recomputed when
    attempt 0 made no model call (a NEW call for a conflict only the fresh page has is allowed: it
    is not a repeat), re-applied with attempt 0's K events when it made one and nothing it read
    changed, else WikiConflict (the stream's `conflict` error)."""
    assert rec.page is not None
    target = rec.page.model_copy(update={"outline": feedback_outline(rec.page, sid, kind)})
    fed = written_record(rec, draft, stored, job_id=run.run_id, now=now, model=result.model,
                         spec_version=inp.teaching.version, page=target,
                         cost_usd=result.cost_usd, fallback_reason=result.fallback_reason,
                         kind="feedback", sources=page_sources(inp), cite_issues=cite_issues)
    assert fed.page is not None and fed.written is not None
    states = dict(fed.page.states)
    states[sid] = fed.page.state(sid).model_copy(update={"folded": False})
    fed = fed.model_copy(update={"page": fed.page.model_copy(update={"states": states})})
    assert fed.page is not None
    mine = section_logs(fed.page.outline, fed.written.sections, producer="writer", spec=spec,
                        k=kview, teach=teach, at=now, infer_version=snap.infer_version)
    own: dict[str, SectionChange] = dict(fed.changed)
    aspect: list[AspectEvent] = []
    kc: KChange | None = None
    if kind == "deeper":
        if first is not None and k_change_again(first[3], first[0], snap, rec.page_id,
                                                first_record=first[1], record=fed) == "reuse":
            aspect = first[2]
            kc = k_change_restaled(first[3], snap, apply_in_memory(snap, aspect), rec.page_id,
                                   now)
        else:
            aspect = observation_events(obs, ts=now, new_id=new_id, note_text=lambda _o: heading)
            kc = k_changed(snap, apply_in_memory(snap, aspect), rec.page_id, gw,
                           skip=sid, now=now, new_id=new_id, record=fed,
                           own=own, action="feedback", calls0=calls0, logs=mine)
        record, change, wiki = kc.record, kc.change, list(kc.wiki)
        change = change.model_copy(update={"llm_calls": change.llm_calls + result.calls})
    else:                                    # prototype: 换个讲法 does not change K
        record = fed
        change = ChangeOut(rewritten=[x for x, m in own.items() if m == "rewritten"],
                           inserted=[x for x, m in own.items() if m == "inserted"],
                           llm_calls=gw.calls - calls0 + result.calls,
                           unresolved_conflicts=list(fed.page.unresolved_conflicts),
                           conflicts=conflicts_out(fed.page))
        wiki = [_wev(new_id, now, PageSaved(record=record, action="feedback")),
                *logs_event(mine, new_id=new_id, now=now)]
    wiki += [_usage(new_id, now, REWRITE_USAGE[kind], rec.page_id, heading=heading), signal]
    return record, change, aspect, wiki, kc, fed


# ---- embedded checks ----

def answer_check(repo: Repo, gw: CountingGateway | None, user_id: str, page_id: str, sid: str,
                 choice: int, *, now: datetime, new_id: Ids,
                 shown: str | None = None) -> CheckAnswerOut:
    """An embedded check, answered once (again → WikiConflict 409). A verified item turns the
    answer into a `check` observation and runs the K-change pass; an unverified one (only on
    pages stored before verification removed such sections) is refused with WikiInvalid (422)
    and never reveals the key."""
    snap = repo.load(user_id)
    rec, s = _organized_section(snap, page_id, sid)
    item = s.check
    if s.role is not SectionRole.CHECK or item is None:
        raise WikiInvalid(f"这一段不是确认题：{sid}")
    if not item.verified:      # old stored page: never shown, never graded, key never exposed
        raise WikiInvalid(f"这道确认题没有通过独立核对，已不再显示：{sid}")
    if sid in rec.checks:
        raise WikiConflict(ANSWERED)
    if choice != IDK and not 0 <= choice < len(item.options):
        raise WikiInvalid(f"没有这个选项：{choice}")
    calls0 = _calls(gw)
    answered = rec.model_copy(update={"checks": {**rec.checks, sid: choice}})
    obs = answer_observation(item, choice, kind="check", evidence_ref=f"check:{page_id}:{sid}")
    aspect: list[AspectEvent] = []
    if obs is None:
        change = ChangeOut(unresolved_conflicts=list(rec.page.unresolved_conflicts),
                           conflicts=conflicts_out(rec.page))
        wiki = [_wev(new_id, now, PageSaved(record=answered, action="check"))]
    else:
        note = item.stem if choice == IDK else item.options[choice]
        label = answer_label(item.stem, IDK_LABEL if choice == IDK else item.options[choice])
        aspect = observation_events((obs,), ts=now, new_id=new_id, note_text=lambda _o: note,
                                    note_label=lambda _o: label)
        kc = k_changed(snap, apply_in_memory(snap, aspect), page_id, gw,
                       skip=sid,
                       now=now, new_id=new_id, record=answered, action="check", calls0=calls0)
        change, wiki = kc.change, list(kc.wiki)
    wiki.append(signal_event(
        snap.state, "check_answer", now=now, new_id=new_id, page_id=page_id, section_id=sid,
        text=IDK_TEXT if choice == IDK else item.options[choice],
        outcome=1.0 if choice == item.answer else 0.0, target=shown))
    _commit(repo, snap, aspect, wiki)
    return CheckAnswerOut(correct=None if choice == IDK else choice == item.answer,
                          correct_index=item.answer, change=change,
                          answer_ref=f"check:{page_id}:{sid}")


# ---- declarations ----

def _require_cell(k: AspectK, cell: str) -> None:
    try:
        cid, aspect = split_cell(cell)
    except ValueError as exc:
        raise LookupError(f"没有这个格子：{cell}") from exc
    if cid not in k.concept_ids() or aspect not in k.aspects(cid):
        raise LookupError(f"没有这个格子：{cell}")


def declare_flow(repo: Repo, gw: CountingGateway | None, user_id: str, cell: str,
                 value: Literal["know", "dont"] | None, *, now: datetime,
                 new_id: Ids) -> DeclareOut:
    """「会 / 不会 / 清除声明」 on one cell: Declare + AddNote, then the K-change pass on the
    current page (D7; None → only the stale list; a 「不会」 hands nothing to the writer). Works
    without a gateway (degrade path)."""
    snap = repo.load(user_id)
    _require_cell(snap.k, cell)
    aspect: list[AspectEvent] = [
        Declare(event_id=new_id("aev"), ts=now, cell=cell, value=value),
        AddNote(event_id=new_id("aev"), ts=now, cell=cell, text=DECLARE_NOTE[value],
                evidence_ref=f"declare:{cell}")]
    kc = k_changed(snap, apply_in_memory(snap, aspect), snap.state.current_page, gw,
                   skip=None, now=now, new_id=new_id, action="declare")
    done = _commit(repo, snap, aspect, kc.wiki)
    return DeclareOut(cell=cell_belief_out(done.kview(now), cell), change=kc.change)
