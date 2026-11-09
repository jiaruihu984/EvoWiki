"""/wiki/*: the goal-driven personal wiki (wire shapes in wikiapp/wiki_schemas.py)."""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent

from evowiki.belief.aspects import AspectKView
from evowiki.deep.evoui import stored_form
from evowiki.deep.store import PgAssetStore, sha256
from evowiki.goals.intake import IntakeError
from evowiki.goals.reading import ReadingRefused, refused_text
from evowiki.profile.service import ProfileService
from evowiki.service.deps import Services, check_user, current_user, get_services
from evowiki.service.engine import params_out, prompts_out
from evowiki.service.http_common import llm_flow
from evowiki.service.rsi_runs import rsi_tick, stale_rewrite
from evowiki.service.sse import closing_stream, event
from evowiki.sources.exam_store import exam_counts, frequent_nodes
from evowiki.sources.excerpts import MSG_SPAN_GONE, SpanGone
from evowiki.store import bot as bot_store
from evowiki.store.rsi import PgRsiStore, current_infer
from evowiki.store.usage import chosen_tiers, usage_tz
from evowiki.teach.contracts import PageFeedbackIn
from evowiki.wikiapp import progress, review_first, shelf
from evowiki.wikiapp.block_progress import PgProgressStore
from evowiki.wikiapp.block_results import block_progress, block_results
from evowiki.wikiapp.block_visibility import guard_asset_write
from evowiki.wikiapp.events import PageRecord
from evowiki.wikiapp.feedback import page_fit_flow
from evowiki.wikiapp.feedback_loop import (
    FeedbackUndoOut,
    PageFeedbackOut,
    page_feedback_loop,
    undo_feedback,
)
from evowiki.wikiapp.followup import confirm_answer, followup
from evowiki.wikiapp.goal import (
    DraftResult,
    draft_status,
    enter_region,
    stop_draft,
)
from evowiki.wikiapp.goal_sources import (
    PooledEnterSources,
)
from evowiki.wikiapp.graph_layout import ensure_layout
from evowiki.wikiapp.graph_out import graph_out
from evowiki.wikiapp.group_domains import domains_out
from evowiki.wikiapp.group_places import places_with_blocks
from evowiki.wikiapp.guidelines import (
    add_guideline,
    list_guidelines,
    remove_guideline,
)
from evowiki.wikiapp.hand_edit import hand_edit_flow, undo_hand_edit
from evowiki.wikiapp.kchange import (
    answer_check,
    declare_flow,
    feedback_know,
)
from evowiki.wikiapp.llm import (
    LLM_FAILED_DETAIL,
    CountingGateway,
    LLMFailed,
    require,
)
from evowiki.wikiapp.map_edits import edit_map
from evowiki.wikiapp.maps import map_out
from evowiki.wikiapp.node_split import split_node
from evowiki.wikiapp.organize import OrganizeItem, PageChanged, organize_stream
from evowiki.wikiapp.page import open_page_flow, set_fold, set_pin
from evowiki.wikiapp.path_edits import edit_path, split_row, undo_split_row, want_node
from evowiki.wikiapp.paths import (
    ahead_pages,
    exam_of,
    next_path_page,
    path_out,
    prefetch_out,
    replan,
)
from evowiki.wikiapp.reading import (
    add_unit_use,
    analyze_unit_with_source,
    start_unit_page,
)
from evowiki.wikiapp.reading_view import unit_out
from evowiki.wikiapp.repo import PgRepo, Snapshot, WikiConflict, WikiInvalid
from evowiki.wikiapp.review import grade_review, review_page, review_today
from evowiki.wikiapp.settings import get_settings, put_settings
from evowiki.wikiapp.source_view import page_sources_out, read_source_info, reading_labels
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.views import catalog_out, file_out, goal_draft_out, k_grid_out, page_out
from evowiki.wikiapp.wiki_schemas import (
    AnswersIn,
    AssetGeneratedIn,
    AssetGeneratedOut,
    BlockProgressOut,
    BlockResultsIn,
    BlockResultsOut,
    CatalogOut,
    CheckAnswerIn,
    CheckAnswerOut,
    CheckFollowupIn,
    CheckFollowupOut,
    ConfirmAnswerIn,
    ConfirmAnswerOut,
    DeclareIn,
    DeclareOut,
    DoneOut,
    EngineOut,
    ErrorEvent,
    FeedbackIn,
    FeedbackOut,
    FileOut,
    FoldIn,
    GoalDraftOut,
    GoalMapOut,
    GraphOut,
    GraphPlacesIn,
    GraphPlacesOut,
    GuidelineIn,
    GuidelineOut,
    GuidelinesOut,
    HandEditIn,
    HandEditOut,
    KGridOut,
    KnownRetractedOut,
    MapEditIn,
    MapEditOut,
    NodeActionOut,
    NodeWantIn,
    PageOut,
    PathEditIn,
    PathEditOut,
    PathNextIn,
    PathNextOut,
    PathOut,
    PathPageDoneOut,
    PinIn,
    ProfileIn,
    ProfileOut,
    ProgressOut,
    ReadingUnitOpenIn,
    ReadingUnitOpenOut,
    ReadingUnitOut,
    ReadingUseIn,
    ReviewFirstIn,
    ReviewGradeIn,
    ReviewGradeOut,
    ReviewTodayOut,
    SectionOut,
    SettingsIO,
    ShelveOut,
    SignalCreatedOut,
    SplitIn,
    SplitOut,
    SplitUndoIn,
    WriteOut,
)

log = logging.getLogger(__name__)
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]


router = APIRouter(prefix="/wiki")


def item_events(items: Iterator[OrganizeItem]) -> Iterator[Iterator[ServerSentEvent]]:
    """A stream dependency's tail for (event name, payload) items (`service.sse`): the events are
    closed, and `items` with them, when the response has ended."""
    yield from closing_stream(_events(items), items)


Events = Iterator[ServerSentEvent]


def _events(items: Iterator[OrganizeItem]) -> Events:
    """Frames the stream and guarantees it ends with `done` or `error`: an exception from the
    iterator, or an iterator that just stops, becomes an `internal` error event."""
    last = None
    try:
        for name, payload in items:
            last = name
            yield event(name, payload.model_dump(mode="json"))
    except Exception as exc:
        log.exception("SSE stream failed")
        err = ErrorEvent(code="internal", retryable=True,
                         detail=f"{type(exc).__name__}: {exc}"[:500])
        yield event("error", err.model_dump(mode="json"))
        return
    if last not in ("done", "error"):
        err = ErrorEvent(code="internal", retryable=True, detail="stream ended without done")
        yield event("error", err.model_dump(mode="json"))


def _labels(svc: Services, uid: str, state: WikiState, map_id: str) -> dict[str, str]:
    """「§3.2」 of a 读懂 goal's material (its units' locations); {} without a reading, no query.
    A database error fails the request (never a reading with its locations left out)."""
    if state.reading_of(map_id) is None:
        return {}
    with svc.pool.connection() as conn:
        return reading_labels(conn, uid, state, map_id)


def _map_out(svc: Services, uid: str, snap: Snapshot, map_id: str) -> GoalMapOut:
    return map_out(snap.state, snap.k, map_id, svc.clock(), user_id=uid,
                   labels=_labels(svc, uid, snap.state, map_id))


def _draft_out(svc: Services, uid: str, res: DraftResult) -> GoalDraftOut:
    """The flow's result with its goal map attached (whenever the draft belongs to one), read
    back after the commit."""
    snap = PgRepo(svc.pool).load(uid)
    mid = res.map_id or (res.draft.map_id if res.draft is not None else None)
    m = (_map_out(svc, uid, snap, mid) if mid is not None and mid in snap.state.maps else None)
    return goal_draft_out(res, user_id=uid, map_out=m)


@router.get("/goals/{draft_id}")
def get_goal_draft(draft_id: str, svc: Svc, uid: User) -> GoalDraftOut:
    """An open draft's current round, or what it finished as (the learn view after the assistant
    created the goal: `wikiapp.goal.draft_status`). 0 LLM calls."""
    with llm_flow():
        res = draft_status(PgRepo(svc.pool), uid, draft_id)
    return _draft_out(svc, uid, res)


@router.post("/goals/{draft_id}/answers", status_code=201)
def post_answers(draft_id: str, body: AnswersIn, svc: Svc, uid: User) -> GoalDraftOut:
    """`skip: true` = stop now (the finished result). The diagnosis's questions are asked and
    answered in the chat (stage 3 T11: next_check / ask_check), so `answers` is refused (422)."""
    if not body.skip:
        raise WikiInvalid("摸底题在聊天里回答")
    gw = _optional_gateway(svc, uid)
    with llm_flow():
        res = stop_draft(PgRepo(svc.pool), gw, uid, draft_id, now=svc.clock(), new_id=svc.new_id)
    kick_writes(svc, uid)
    return _draft_out(svc, uid, res)


@router.post("/goals/{draft_id}/stop", status_code=201)
def post_stop(draft_id: str, svc: Svc, uid: User) -> GoalDraftOut:
    """「够了，开始学」: finish now with the evidence so far (the open round adds none)."""
    gw = _optional_gateway(svc, uid)
    with llm_flow():
        res = stop_draft(PgRepo(svc.pool), gw, uid, draft_id, now=svc.clock(),
                         new_id=svc.new_id)
    kick_writes(svc, uid)
    return _draft_out(svc, uid, res)


@router.get("/maps/{map_id}")
def get_map(map_id: str, svc: Svc, uid: User) -> GoalMapOut:
    with llm_flow():
        snap = PgRepo(svc.pool).load(uid)
        return _map_out(svc, uid, snap, map_id)


@router.post("/maps/{map_id}/edits")
def post_map_edits(map_id: str, body: MapEditIn, svc: Svc, uid: User) -> MapEditOut:
    """The 学习路径 card's edits (reorder / drop / rename / add, or insert = 撤销 of a drop), all or
    none (`wikiapp.map_edits`); `undo`: their inverse ops. 0 LLM calls."""
    repo = PgRepo(svc.pool)
    with llm_flow():
        done = edit_map(repo, uid, map_id, body.ops, now=svc.clock(), new_id=svc.new_id)
        snap = repo.load(uid)
        return MapEditOut(map=_map_out(svc, uid, snap, map_id),
                          undo=list(done.undo))


def _graph(svc: Services, uid: str, snap, now) -> GraphOut:
    """The graph with the materials' § labels and coverage (read here, so a database error fails
    the request instead of leaving the material info out)."""
    with svc.pool.connection() as conn:
        info = read_source_info(conn, uid, snap.state)
    out = graph_out(snap.state, snap.k, now, source_info=info, runs=_runs(svc, uid, snap.state))
    out.domains = domains_out(snap.state, out.groups)
    return out


def _runs(svc: Services, uid: str, state) -> dict:
    """The latest write run of every page a progress view reads (wikiapp.progress)."""
    return svc.runs.store.latest_of(uid, progress.page_ids(state))


def _with_sections(svc: Services, uid: str, out):
    """A path answer (PathEditOut / NodeActionOut) with the rows' § labels, as the graph route
    gives them: `path_items` runs inside the wikiapp functions without a database connection, so
    the labels are read here from the state those functions just committed; so are the rows'
    write states (the page runs live in the database too)."""
    snap = PgRepo(svc.pool).load(uid)
    rec = next((r for r in snap.state.paths.values()
                if {it.key for it in out.path} <= {p.page_id for p in r.path.pages}
                | {e.page_key for e in r.edits}), None)
    if rec is not None:
        ctx = progress.Ctx(snap.state, AspectKView(snap.k, svc.clock()),
                           _runs(svc, uid, snap.state))
        states = progress.row_states(ctx, rec)
        for it in out.path:
            if it.key in states:
                it.state, it.progress = states[it.key]
                it.status = progress.status_of(it.state, it.page_id is not None)
    if not any(it.node_ids for it in out.path):
        return out
    with svc.pool.connection() as conn:
        info = read_source_info(conn, uid, snap.state)
    by_node, counts = info.labels_by_node, info.exam_counts
    for it in out.path:
        it.sections = list(dict.fromkeys(s for n in it.node_ids for s in by_node.get(n, ())))
        it.exam = exam_of(it.node_ids, counts)
    return out


@router.get("/graph")
def get_graph(svc: Svc, uid: User) -> GraphOut:
    """The whole node graph (plan 2026-10-13-unified-node-graph §4). 0 LLM calls, before or after
    the response: groups with no name are served with their fallback names; the graph change
    that made them enqueued their naming (`wikiapp.group_naming`). A graph
    placed by an older layout is laid out again once first (`wikiapp.graph_layout`); when that
    fails (a lost race, anything) the stored positions are served and the next read tries again."""
    with llm_flow():
        repo = PgRepo(svc.pool)
        snap = repo.load(uid)
        try:
            snap = ensure_layout(repo, snap, now=svc.clock(), new_id=svc.new_id, embedder=svc.embedder)
        except WikiConflict:
            log.warning("graph relayout for %s lost a race; the next read retries", uid)
        except Exception:
            log.exception("graph relayout for %s failed; serving the stored positions", uid)
        return _graph(svc, uid, snap, svc.clock())


@router.post("/graph/places")
def post_graph_places(body: GraphPlacesIn, svc: Svc, uid: User) -> GraphPlacesOut:
    """Where a new goal's blocks stand among his groups, similar ones near, before and after they are confirmed. Stores
    nothing, no LLM (the local embedder). A failed layout is retryable; the view keeps its existing map."""
    out = _graph(svc, uid, PgRepo(svc.pool).load(uid), svc.clock())
    try:
        ids = {b.id for b in body.blocks}
        mine = {g.map_id for g in out.goals if ids & set(g.nodes)}
        others = [g for g in out.groups if g.map_id not in mine or g.map_id is None]
        graph = PgRepo(svc.pool).load(uid).state.graph
        places, links = places_with_blocks(graph, others, {n.id: n.name for n in out.nodes},
                                           [(b.id, b.name, b.scope) for b in body.blocks], svc.embedder)
    except Exception as exc:
        log.exception("places of a goal's blocks for %s failed", uid)
        raise HTTPException(status_code=503, detail="知识图谱布局暂时不可用，请重试。") from exc
    return GraphPlacesOut(places=places, links=links)


@router.post("/maps/{map_id}/regions/{region_id}/enter", status_code=201)
def post_enter_region(map_id: str, region_id: str, svc: Svc, uid: User) -> GoalDraftOut:
    """Enter any map node (spec 2026-10-28 §2.2, `MapNodeOut.enter`): split it one level (one
    intake call) or show the parts it has (0 calls) → `map_id`; a point → its diagnosis
    (`draft_id`, only when K has no evidence on it) or its path (`path_id`); pages are produced
    via POST /wiki/paths/{path_id}/next. 502 when the split is unusable twice (nothing stored)."""
    gw = require(svc.gateway, uid)
    with llm_flow():
        res = enter_region(PgRepo(svc.pool), gw, uid, map_id, region_id, now=svc.clock(),
                           new_id=svc.new_id, sources=PooledEnterSources(svc.pool))
    return _draft_out(svc, uid, res)


# --- a 读懂 goal's units (reading-mode spec §3.5, wikiapp/reading.py) -------------------------------

@contextmanager
def _reading_refused() -> Iterator[None]:
    """A stability op or a unit step that cannot be done (ReadingRefused) is 409 with what to do."""
    try:
        yield
    except ReadingRefused as exc:
        raise WikiConflict(refused_text(exc)) from exc
    except SpanGone as exc:            # the detail names a section id: the log only
        log.warning("a reading unit's text is gone: %s", exc)
        raise LookupError(MSG_SPAN_GONE) from exc


@router.post("/readings/{reading_id}/units/{unit_id}/open")
def post_open_unit(reading_id: str, unit_id: str, svc: Svc, uid: User,
                   body: ReadingUnitOpenIn | None = None) -> ReadingUnitOpenOut:
    """Enter a unit: 「看这一段」 when it is not analyzed yet (one call, its text read from the
    material, its teaches links in the same commit; 0 calls after that), then the unit with its
    teaches / uses and their K state; `start` also starts its reading page (no call; open it via
    POST /wiki/pages/{page_id}/organize). 404 unknown reading / unit / material, 409 skipped,
    502 an analysis unusable twice (nothing stored)."""
    repo = PgRepo(svc.pool)
    with llm_flow(), _reading_refused():
        snap = repo.load(uid)
        r = snap.state.readings.get(reading_id)
        needs_call = r is not None and any(u.unit_id == unit_id and not u.analyzed
                                           and not u.skipped for u in r.units)
        gw = require(svc.gateway, uid) if needs_call else _optional_gateway(svc, uid)
        res = analyze_unit_with_source(repo, svc.pool, gw, uid, reading_id, unit_id,
                                       now=svc.clock(), new_id=svc.new_id)
        calls = res.calls
        if body is not None and body.start:
            res = start_unit_page(repo, uid, reading_id, unit_id, now=svc.clock(),
                                  new_id=svc.new_id)
    snap = repo.load(uid)
    unit = next(u for u in res.reading.units if u.unit_id == unit_id)
    return ReadingUnitOpenOut(reading_id=reading_id, map_id=res.reading.map_id,
                              unit=unit_out(snap.state, snap.kview(svc.clock()), res.reading,
                                            unit, labels=_labels(svc, uid, snap.state,
                                                                 res.reading.map_id)),
                              page_id=res.page_id, path_id=res.path_id, llm_calls=calls)


@router.post("/readings/{reading_id}/units/{unit_id}/uses")
def post_unit_use(reading_id: str, unit_id: str, body: ReadingUseIn, svc: Svc,
                  uid: User) -> ReadingUnitOut:
    """A prerequisite found while reading: it joins the unit's uses and the map's top level
    (no call). 409 not analyzed yet; 422 an unknown node or the unit's own point."""
    repo = PgRepo(svc.pool)
    with _reading_refused():
        res = add_unit_use(repo, uid, reading_id, unit_id, existing_id=body.existing_id,
                           name=body.name, summary=body.summary, atomic=body.atomic,
                           now=svc.clock(), new_id=svc.new_id)
    snap = repo.load(uid)
    return unit_out(snap.state, snap.kview(svc.clock()), res.reading, res.unit,
                    labels=_labels(svc, uid, snap.state, res.reading.map_id))


def _organize_events(page_id: str, svc: Svc, uid: User, fresh: bool = False) -> Iterator[Events]:
    gw = require(svc.gateway, uid)
    items = organize_stream(PgRepo(svc.pool), gw, uid, page_id, now=svc.clock(),
                            new_id=svc.new_id, writing=svc.writing(), runs=svc.runs, fresh=fresh)
    yield from item_events(items)


@router.post("/pages/{page_id}/organize", response_class=EventSourceResponse)
def post_organize(events: Annotated[Events, Depends(_organize_events)]) -> Events:
    """SSE (ORGANIZE_EVENTS). 503 without a gateway and 404 for an unknown user / page, both
    before the stream opens; later failures arrive as an `error` event. The page is written by a
    page run (`wikiapp.page_runs`): this follows the page's run (a page the Bot wrote ahead, a
    stream the client lost) or starts one, at the user's priority. When the run ends, the idle Bot
    goes on with the next unwritten page of the learner's paths (`wikiapp.write_queue`)."""
    yield from events


def kick_writes(svc: Services, uid: str) -> None:
    """The idle Bot may have a page to write now (a page was opened, a path or a setting changed):
    `WriteQueue.kick` off the request's thread; never fails the request."""
    if svc.write_queue is not None:
        svc.write_queue.kick_soon(uid)


@router.get("/writes")
def get_writes(svc: Svc, uid: User,
               page_ids: Annotated[str | None, Query(description="comma separated")] = None,
               unwritten: bool = False) -> list[WriteOut]:
    """Where the pages' writes are (the write queue, spec 2026-10-15 §4): queued / planning /
    writing (`section` of `of`) / done / failed / undone, with the cost so far. Without `page_ids`:
    every page with a run queued or writing. `unwritten`: the asked pages with no run and no
    content say so. The plugin polls it while something is."""
    if svc.write_queue is None:
        return []
    ids = None if page_ids is None else [p for p in page_ids.split(",") if p]
    return [WriteOut.model_validate(w) for w in svc.write_queue.status(uid, ids, unwritten=unwritten)]


@router.get("/files")
def get_files(svc: Svc, uid: User) -> list[FileOut]:
    """Every page of the user, as its file needs it (title, folder, parent_name, trashed,
    organized): the plugin's one read to move / rename / trash its files. Only reads: it does not
    open a page, call a model, curate or start a write."""
    state = PgRepo(svc.pool).load(uid).state
    return [file_out(state, state.pages[pid]) for pid in state.page_order]


@router.get("/pages/{page_id}")
def get_page(page_id: str, svc: Svc, uid: User) -> PageOut:
    """The page, read only: no `PageOpened`, no lazy update, no curate, no model call, no write
    started or kicked. A page the user opens goes through `POST /wiki/pages/{id}/open`."""
    snap = PgRepo(svc.pool).load(uid)
    rec = snap.state.pages.get(page_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="This page was not found.")
    out = _with_sources(svc, uid, page_out(snap.state, snap.kview(svc.clock()), rec, user_id=uid), rec)
    after = progress.page_progress(snap.state, AspectKView(snap.k, svc.clock()),
                                   _runs(svc, uid, snap.state), page_id)
    return out.model_copy(update={"next_step": after.after if after.goal_id else None})


def _with_sources(svc: Services, uid: str, out: PageOut, record: PageRecord) -> PageOut:
    """`out` with the materials the page was written from (`PageOut.sources`: what its citation
    marks point at), read from the library; unchanged for a page written without materials."""
    if not record.sources:
        return out
    with svc.pool.connection() as conn:
        return out.model_copy(update={"sources": page_sources_out(conn, uid, record)})


def _run_events(page_id: str, run_id: str, svc: Svc, uid: User) -> Iterator[Events]:
    state = PgRepo(svc.pool).load(uid).state
    if page_id not in state.pages:
        raise HTTPException(status_code=404, detail="This page was not found.")
    row = svc.runs.store.get(run_id)
    if row is None or row.user_id != uid or row.page_id != page_id or row.discarded_at is not None:
        raise HTTPException(status_code=404, detail="这一页现在没有在写")
    yield from item_events(svc.runs.follow(row.run_id, consume=False))


@router.get("/pages/{page_id}/run/stream", response_class=EventSourceResponse)
def get_run_stream(events: Annotated[Events, Depends(_run_events)]) -> Events:
    """SSE (ORGANIZE_EVENTS): replays and follows the requested run (including a run that just finished) from
    its first item, the same events as `organize`. It only watches: it does not start a run, open the
    page, call a model, or mark a finished run read. 404 for a missing, discarded or foreign run."""
    yield from events


@router.post("/pages/{page_id}/run/cancel", status_code=204)
def post_run_cancel(page_id: str, svc: Svc, uid: User) -> Response:
    """「停止」: cancels the page's live write runs (queued or running). A running one stops before
    its next model or tool call (its DBOS workflow is cancelled); nothing it wrote is committed, the
    row ends `failed` with failure `cancelled`, and the idle Bot does not pick the page up again
    (`WriteQueue`). A follower leaving does not do this. 404 unknown page or no live run."""
    state = PgRepo(svc.pool).load(uid).state
    if page_id not in state.pages:
        raise HTTPException(status_code=404, detail="This page was not found.")
    now = svc.clock()
    cancelled = [r.run_id for r in svc.runs.store.live_of(uid)
                 if r.page_id == page_id and svc.runs.discard(r.run_id, now)]
    if not cancelled:
        raise HTTPException(status_code=404, detail="这一页现在没有在写")
    return Response(status_code=204)


def _optional_gateway(svc: Services, uid: str) -> CountingGateway | None:
    """For endpoints that work without an LLM (D16): None stays None, no 503."""
    return None if svc.gateway is None else require(svc.gateway, uid)


@router.post("/pages/{page_id}/open")
def post_page_open(page_id: str, svc: Svc, uid: User,
                   rewrite: Annotated[bool, Query()] = False) -> PageOut:
    """The user opens the page (current page, D7) with a lazy update: 0 calls unless K moved into a
    conflict; without a gateway the conflicts stay unresolved. An unwritten page is only opened:
    nothing writes it (its write starts from 「开始生成」 / the learning flow, `organize`). A page an
    RSI change left out of date is served as it is; only the page view's open (`?rewrite=1`) starts
    its rewrite under the current spec as a page workflow (`rsi_runs.stale_rewrite`) — a re-open
    after an action or a review card's answer does not."""
    with llm_flow():
        out = open_page_flow(PgRepo(svc.pool), _optional_gateway(svc, uid), uid, page_id,
                             now=svc.clock(), new_id=svc.new_id)
    snap = PgRepo(svc.pool).load(uid)
    if out.organized and not out.trashed:
        kick_writes(svc, uid)
        if rewrite and stale_rewrite(svc, uid, page_id):
            out = out.model_copy(update={"rewriting": True})
        out = _with_sources(svc, uid, out, snap.state.pages[page_id])
    after = progress.page_progress(snap.state, AspectKView(snap.k, svc.clock()),
                                   _runs(svc, uid, snap.state), page_id)
    return out.model_copy(update={"next_step": after.after if after.goal_id else None})


@router.post("/pages/{page_id}/sections/{sid}/fold")
def post_fold(page_id: str, sid: str, body: FoldIn, svc: Svc, uid: User) -> SectionOut:
    return set_fold(PgRepo(svc.pool), uid, page_id, sid, body.folded, now=svc.clock(),
                    new_id=svc.new_id)


@router.post("/pages/{page_id}/sections/{sid}/pin")
def post_pin(page_id: str, sid: str, body: PinIn, svc: Svc, uid: User) -> SectionOut:
    return set_pin(PgRepo(svc.pool), uid, page_id, sid, body.pinned, now=svc.clock(),
                   new_id=svc.new_id)


@router.post("/pages/{page_id}/done")
def post_done(page_id: str, svc: Svc, uid: User) -> DoneOut:
    with llm_flow():
        return _complete_page(svc, uid, page_id)


def _complete_page(svc: Services, uid: str, page_id: str) -> DoneOut:
    from evowiki.store.locks import session_lock
    from evowiki.wikiapp.goal_check import complete_page

    with session_lock(svc.pool, f"evowiki.goal-check:{uid}:{page_id}") as acquired:
        if not acquired:
            raise WikiConflict("正在生成目标小测，请稍后重试")
        return complete_page(PgRepo(svc.pool), svc.gateway, uid, page_id,
                             now=svc.clock(), new_id=svc.new_id)


def _tick(tasks: BackgroundTasks, svc: Services, uid: str) -> None:
    """After a request that recorded a signal: the RSI cycle's background tick (runs only when
    the user's settings make a cycle due; never raises) and the Bot's event wake-up."""
    tasks.add_task(rsi_tick, svc, uid)
    if svc.bot_notify is not None:
        tasks.add_task(svc.bot_notify, uid)


@contextmanager
def _page_changed_409() -> Iterator[None]:
    """PageChanged (the page a feedback is about was rewritten meanwhile) → 409 with its text, the
    status the section rewrite's `conflict` error event has."""
    try:
        yield
    except PageChanged as exc:
        raise WikiConflict(str(exc), code="feedback_stale", public_message=
            "This content changed before the feedback could be applied. Refresh and try again.") from exc


@router.post("/pages/{page_id}/sections/{sid}/feedback")
def post_feedback(page_id: str, sid: str, body: FeedbackIn, svc: Svc, uid: User,
                  tasks: BackgroundTasks) -> FeedbackOut:
    """「不会」 on a section (0 calls for the fold; works without a gateway). The RSI tick runs after
    the response."""
    with llm_flow(), _page_changed_409():
        out = feedback_know(PgRepo(svc.pool), _optional_gateway(svc, uid), uid, page_id, sid,
                            now=svc.clock(), new_id=svc.new_id, shown=body.artifact_id)
    _tick(tasks, svc, uid)
    return out


@router.post("/pages/{page_id}/feedback", status_code=201)
def post_page_feedback(page_id: str, body: PageFeedbackIn, svc: Svc,
                       uid: User, tasks: BackgroundTasks) -> PageFeedbackOut:
    """Page feedback, understood and answered at once (Obsidian flow §4, B1; `wikiapp.feedback_loop`):
    the signal is always recorded; one understanding call; 「不会」 for the missing concepts and the
    K-change pass; this page patched (undo: POST …/feedback/{feedback_id}/undo). The signal
    counts toward the user's next RSI round like any other (`rsi` = "queued" with text and `apply`).
    Without a gateway, or when the answer cannot be used: 201 with `understood` false and `reason`
    (only the signal was kept). Unknown page 404; a section_id not in the outline 422."""
    deep = svc.deep
    library = getattr(svc, "library", None)
    with llm_flow(), _page_changed_409():
        out = page_feedback_loop(
            PgRepo(svc.pool), _optional_gateway(svc, uid), uid, page_id, body, now=svc.clock(),
            new_id=svc.new_id, components=library.list() if library is not None else (),
            assets=deep.assets if deep is not None else None,
            checker=deep.checker if deep is not None else None)
    _tick(tasks, svc, uid)
    return out


@router.post("/pages/{page_id}/feedback/{feedback_id}/undo")
def post_page_feedback_undo(page_id: str, feedback_id: str, svc: Svc,
                            uid: User) -> FeedbackUndoOut:
    """撤回 a page-feedback patch: the page goes back to its record before that feedback, while its
    content is still what the feedback left. 404 unknown; 409 already undone / changed since."""
    return undo_feedback(PgRepo(svc.pool), uid, page_id, feedback_id, now=svc.clock(),
                         new_id=svc.new_id)


@router.post("/pages/{page_id}/hand-edit")
def post_hand_edit(page_id: str, body: HandEditIn, svc: Svc, uid: User) -> HandEditOut:
    """A hand edit in the note: the changed sections written and locked, deleted ones gone (0 calls).
    404 unknown page; 409 a section is gone or the page is being written; 422 not allowed."""
    out = hand_edit_flow(PgRepo(svc.pool), uid, page_id, body, now=svc.clock(), new_id=svc.new_id,
                         ledger=svc.actions,
                         running=lambda: any(r.page_id == page_id for r in svc.runs.store.live_of(uid)))
    if svc.bot_notify is not None:
        svc.bot_notify(uid)          # bot.card_stale: is the section's card still right?
    return out


@router.post("/pages/{page_id}/hand-edit/{edit_id}/undo")
def post_hand_edit_undo(page_id: str, edit_id: str, svc: Svc, uid: User) -> HandEditOut:
    """撤销 a hand edit: its sections as they were. 404 unknown; 409 already undone / changed since."""
    return undo_hand_edit(PgRepo(svc.pool), uid, page_id, edit_id, now=svc.clock(),
                          new_id=svc.new_id, ledger=svc.actions)


@router.post("/pages/{page_id}/fit", status_code=201)
def post_page_fit(page_id: str, svc: Svc, uid: User) -> SignalCreatedOut:
    """基本合适 at the end of a page: a `page_fit` signal (0 calls)."""
    return page_fit_flow(PgRepo(svc.pool), uid, page_id, now=svc.clock(), new_id=svc.new_id)


@router.post("/pages/{page_id}/checks/{sid}")
def post_check(page_id: str, sid: str, body: CheckAnswerIn, svc: Svc, uid: User,
               tasks: BackgroundTasks) -> CheckAnswerOut:
    with llm_flow():
        out = answer_check(PgRepo(svc.pool), _optional_gateway(svc, uid), uid, page_id, sid,
                           body.choice, now=svc.clock(), new_id=svc.new_id,
                           shown=body.artifact_id)
    _tick(tasks, svc, uid)
    return out


@router.post("/pages/{page_id}/checks/{sid}/followup")
def post_check_followup(page_id: str, sid: str, body: CheckFollowupIn, svc: Svc, uid: User,
                        tasks: BackgroundTasks) -> CheckFollowupOut:
    """After a wrong answer on this section (page check, or a review card in check / cue mode):
    slip / learned schedule a new confirmation question (one generator + one verifier call; 503
    without a gateway, 502 when no usable question — nothing recorded); unclear records 「看不懂」
    and returns where to review + a quote for the assistant (0 calls). 404 no such answer, 422
    not a wrong answer (or a slip on 不知道), 409 already followed up."""
    gw = _optional_gateway(svc, uid)
    if body.choice != "unclear":
        gw = require(svc.gateway, uid)
    with llm_flow():
        out = followup(PgRepo(svc.pool), gw, uid, page_id, sid, body, now=svc.clock(),
                       new_id=svc.new_id)
    _tick(tasks, svc, uid)
    return out


@router.post("/k/cells/{cell}/declare")
def post_declare(cell: str, body: DeclareIn, svc: Svc, uid: User) -> DeclareOut:
    with llm_flow():
        out = declare_flow(PgRepo(svc.pool), _optional_gateway(svc, uid), uid, cell, body.value,
                           now=svc.clock(), new_id=svc.new_id)
    return out


# ---- Task 10: reference cards, catalog, K grid, settings ----

@router.post("/pages/{page_id}/collapse")
def post_collapse(page_id: str, svc: Svc, uid: User) -> ShelveOut:
    """「收为参考卡」 (0 calls); 409 when the page's targets are not all 确定会 (or it is shelved)."""
    with llm_flow():
        return shelf.collapse(PgRepo(svc.pool), uid, page_id, now=svc.clock(), new_id=svc.new_id)


@router.post("/pages/{page_id}/expand")
def post_expand(page_id: str, svc: Svc, uid: User) -> ShelveOut:
    """「恢复成完整页」 (0 calls); 409 when the page is not shelved."""
    with llm_flow():
        return shelf.expand(PgRepo(svc.pool), uid, page_id, now=svc.clock(), new_id=svc.new_id)


@router.get("")
def get_catalog(svc: Svc, uid: User) -> CatalogOut:
    with llm_flow():
        snap = PgRepo(svc.pool).load(uid)
        return catalog_out(snap.state, snap.k, svc.clock(), user_id=uid)


@router.get("/k")
def get_k(svc: Svc, uid: User) -> KGridOut:
    with llm_flow():
        snap = PgRepo(svc.pool).load(uid)
        return k_grid_out(snap.state, snap.k, svc.clock(), user_id=uid)


@router.get("/engine")
def get_engine(svc: Svc, uid: User) -> EngineOut:
    """The 参数 tab (read-only): each step's prompt versions, the model this user's calls would go
    to now and the prompt text; then θ_infer,u per tunable path with 「默认」 or when / why it was
    tuned. No LLM call."""
    check_user(svc, uid)
    with svc.pool.connection() as conn:
        infer = current_infer(conn, uid)
        chosen = chosen_tiers(conn, uid)
    patches = PgRsiStore(svc.pool).patches(uid)
    return EngineOut(prompts=prompts_out(os.environ, uid, chosen),
                     params=params_out(infer, patches, usage_tz()))


@router.get("/settings")
def get_wiki_settings(svc: Svc, uid: User) -> SettingsIO:
    with llm_flow():
        return get_settings(PgRepo(svc.pool), uid)


@router.put("/settings")
def put_wiki_settings(body: SettingsIO, svc: Svc, uid: User) -> SettingsIO:
    with llm_flow():
        return put_settings(PgRepo(svc.pool), uid, body, now=svc.clock(), new_id=svc.new_id)


# ---- the learner profile (「我」 note, evowiki.profile) ----

def _profile(svc: Services) -> ProfileService:
    if svc.profile is None:
        raise HTTPException(503, "档案不可用")
    return svc.profile


@router.get("/profile")
def get_profile(svc: Svc, uid: User) -> ProfileOut:
    """The note's text and version for the vault (0 calls); the server remembers it as the copy he has."""
    with llm_flow():
        c = _profile(svc).copy(uid)
        return ProfileOut(text=c.text, version=c.version, locale=c.locale)


@router.put("/profile")
def put_profile(body: ProfileIn, svc: Svc, uid: User) -> ProfileOut:
    """His edit of the note: his lines win; 怎么学比较好 changes the teaching spec's items, 已经会的 K
    (0 calls). `base`: the version of the copy it was based on; only the last copy handed out can retract,
    any other save adds lines and answers in `kept` what it could not retract. Returns the note as it now is."""
    with llm_flow():
        c = _profile(svc).save(uid, body.text, body.base)
        took = None if c.retracted is None else KnownRetractedOut(
            lines=list(c.retracted.lines), action_id=c.retracted.action_id)
        return ProfileOut(text=c.text, version=c.version, kept=list(c.kept), retracted=took,
                          locale=c.locale)


# ---- Task 11: guidelines ----

@router.get("/guidelines")
def get_guidelines(svc: Svc, uid: User) -> GuidelinesOut:
    with llm_flow():
        return list_guidelines(PgRepo(svc.pool).load(uid).state)


@router.post("/guidelines", status_code=201)
def post_guideline(body: GuidelineIn, svc: Svc, uid: User) -> GuidelineOut:
    """`text` (source user)."""
    with llm_flow():
        return add_guideline(PgRepo(svc.pool), uid, body, now=svc.clock(), new_id=svc.new_id)


@router.delete("/guidelines/{guideline_id}", status_code=204)
def delete_guideline(guideline_id: str, svc: Svc, uid: User) -> Response:
    with llm_flow():
        remove_guideline(PgRepo(svc.pool), uid, guideline_id, now=svc.clock(),
                         new_id=svc.new_id)
    return Response(status_code=204)


# ---- K1: learning paths, review ----

@router.get("/paths/{path_id}")
def get_path(path_id: str, svc: Svc, uid: User) -> PathOut:
    with llm_flow():
        snap = PgRepo(svc.pool).load(uid)
        ahead = ahead_pages(svc.runs.store, uid, snap.state)
        rec = snap.state.paths.get(path_id)
        with svc.pool.connection() as conn:          # a database error fails the request
            info = read_source_info(conn, uid, snap.state)
        return path_out(snap.state, snap.k, path_id, svc.clock(), user_id=uid,
                        prefetch=prefetch_out(ahead, rec) if rec is not None else None,
                        ahead=ahead, section_labels=info.labels_by_node,
                        sources=info.goals.get(path_id, []), exam_counts=info.exam_counts,
                        runs=_runs(svc, uid, snap.state))


# ---- progress (usability reset item 1): the one answer to 「下一步」 ----

@router.get("/progress")
def get_progress(svc: Svc, uid: User) -> ProgressOut:
    """Every goal's progress and today's next step (wikiapp.progress; 0 LLM calls). The plugin
    reads it again after any action that changes progress."""
    with llm_flow():
        snap = PgRepo(svc.pool).load(uid)
        return progress.progress(snap.state, AspectKView(snap.k, svc.clock()),
                                 _runs(svc, uid, snap.state))


@router.post("/progress/review-first", status_code=204)
def post_review_first(body: ReviewFirstIn, svc: Svc, uid: User) -> None:
    """先复习 / 跳过 on 「建议先复习 X」: closes it. Changes no path and writes no page."""
    review_first.answer(PgRepo(svc.pool), uid, body.node_id, body.choice, now=svc.clock(),
                        new_id=svc.new_id)


@router.post("/paths/{path_id}/next")
def post_path_next(path_id: str, svc: Svc, uid: User,
                   body: Annotated[PathNextIn | None, Body()] = None) -> PathNextOut:
    """0 LLM calls; then POST /wiki/pages/{page_id}/organize (it replays / attaches to the page
    when the Bot wrote it ahead: `prefetched`, nothing is written again). 409 when every page
    started."""
    with llm_flow():
        return next_path_page(PgRepo(svc.pool), uid, path_id, now=svc.clock(),
                              new_id=svc.new_id, runs=svc.runs.store,
                              key=body.key if body is not None else None)


@router.post("/paths/{path_id}/pages/{index}/done")
def post_path_page_done(path_id: str, index: int, svc: Svc, uid: User) -> PathPageDoneOut:
    """「学完」 on page `index` (1-based): 404 no such path / page, 409 page not started."""
    with llm_flow():
        repo = PgRepo(svc.pool)
        snap = repo.load(uid)
        rec = snap.state.paths.get(path_id)
        if rec is None or not 1 <= index <= len(rec.page_ids):
            raise LookupError(f"没有这一页：{path_id} 第 {index} 页")
        page_id = rec.page_ids[index - 1]
        if page_id is None:
            raise WikiConflict("这一页还没开始")
        done = _complete_page(svc, uid, page_id)
        snap = repo.load(uid)
        return PathPageDoneOut(path=path_out(snap.state, snap.k, path_id, svc.clock(), user_id=uid),
                               done=done)


@router.post("/paths/{path_id}/edits")
def post_path_edit(path_id: str, body: Annotated[PathEditIn, Body()], svc: Svc,
                   uid: User) -> PathEditOut:
    """move / remove / add on the shown path (indices 0-based). 404 unknown path or node, 422 no
    such page. 0 LLM calls."""
    with llm_flow():
        return _with_sections(svc, uid, edit_path(
            PgRepo(svc.pool), uid, path_id, body, now=svc.clock(),
            new_id=svc.new_id))                       # (the path hook kicks the idle Bot)


@router.post("/paths/{path_id}/rows/{page_key}/split")
def post_path_row_split(path_id: str, page_key: str, svc: Svc, uid: User) -> PathEditOut:
    """「拆成多页」: the row's node is split into finer nodes (the shared one-level split, source
    path_row; it may leave the node whole: `kept`, the path unchanged) and they take
    the row's place (a row of several nodes: one row per node, no model); `undo` in the answer
    undoes it. 404 unknown path, 422 a row without a node, 503 without a gateway (one-node row), 502 when the model's answer cannot be used (nothing
    written)."""
    gw = _optional_gateway(svc, uid)             # a row of several nodes needs no model
    with llm_flow():
        try:
            return _with_sections(svc, uid, split_row(
                PgRepo(svc.pool), gw, uid, path_id, page_key, now=svc.clock(),
                new_id=svc.new_id))
        except IntakeError as exc:
            raise _graph_failure(exc) from exc


@router.post("/paths/{path_id}/rows/{page_key}/split/undo")
def post_path_row_split_undo(path_id: str, page_key: str, body: SplitUndoIn, svc: Svc,
                             uid: User) -> PathEditOut:
    """Undoes 「拆成多页」 (graph change + the row back). 404 unknown path / change, 409 the change
    cannot be undone any more, 422 the row was not split."""
    with llm_flow():
        return _with_sections(svc, uid, undo_split_row(
            PgRepo(svc.pool), uid, path_id, page_key, body.change_id, now=svc.clock(),
            new_id=svc.new_id))


@router.post("/paths/{path_id}/replan")
def post_path_replan(path_id: str, svc: Svc, uid: User) -> PathEditOut:
    """Plans the path again and keeps the user's edits (LLM; 503 without a gateway). 404 unknown
    path."""
    gw = require(svc.gateway, uid)
    with llm_flow():
        out = _with_sections(svc, uid, replan(
            PgRepo(svc.pool), gw, uid, path_id, now=svc.clock(), new_id=svc.new_id))
    kick_writes(svc, uid)
    return out


@router.post("/nodes/{node_id}/want")
def post_node_want(node_id: str, body: NodeWantIn, svc: Svc, uid: User) -> NodeActionOut:
    """「我想学这个」: a page of the user's own on the goal's path. 404 unknown node / goal, 409 a
    goal without a path. 0 LLM calls."""
    with llm_flow():
        return _with_sections(svc, uid, want_node(
            PgRepo(svc.pool), uid, node_id, body.map_id, now=svc.clock(), new_id=svc.new_id))


def _graph_failure(exc: BaseException) -> LLMFailed:
    return LLMFailed(f"{LLM_FAILED_DETAIL}：{exc}"[:300])


@router.post("/nodes/{node_id}/split")
def post_node_split(node_id: str, svc: Svc, uid: User,
                    body: Annotated[SplitIn | None, Body()] = None) -> SplitOut:
    """「看不懂」 (reason confused, source dont_get) / 「继续拆细」 (requested, continue): the shared
    one-level split writes the node's finer children (a node that already has children can be
    split further), or leaves it whole (`kept`: why, only the atomic mark written). 404 unknown
    node, 503 without a gateway, 502 when the model's answer cannot be used (nothing written)."""
    gw = require(svc.gateway, uid)
    words = body.words if body is not None and body.words else ""
    map_id = body.map_id if body is not None else None
    reason = body.reason if body is not None else "confused"
    now = svc.clock()
    with llm_flow():
        try:
            done = split_node(PgRepo(svc.pool), gw, uid, node_id, reason=reason,
                              source="dont_get" if reason == "confused" else "continue",
                              words=words, now=now, new_id=svc.new_id, map_id=map_id)
        except IntakeError as exc:
            raise _graph_failure(exc) from exc
    return SplitOut(children=list(done.children), kept=done.kept,
                    graph=_graph(svc, uid, done.snap, now))


def _bot_zone(svc: Services, uid: str) -> str:
    """The user's Bot time zone (days of the learning streak)."""
    with svc.pool.connection() as conn:
        return bot_store.get_settings(conn, uid).timezone


@router.get("/review/today")
def get_review_today(svc: Svc, uid: User, more: bool = False) -> ReviewTodayOut:
    """`more=true`: the next full batch once today's (`max_per_day`) is answered."""
    with llm_flow():
        with svc.pool.connection() as conn:          # a database error fails the request
            frequent = frequent_nodes(exam_counts(conn, uid))
        return review_today(PgRepo(svc.pool), uid, now=svc.clock(), timezone=_bot_zone(svc, uid),
                            exam_concepts=frequent, more=more)


@router.get("/review/page/{page_id}")
def get_review_page(page_id: str, svc: Svc, uid: User) -> ReviewTodayOut:
    """「考考我这页」: the page's cards, due or not; graded through POST /review/grade. 404 unknown page."""
    with llm_flow():
        with svc.pool.connection() as conn:
            frequent = frequent_nodes(exam_counts(conn, uid))
        try:
            return review_page(PgRepo(svc.pool), uid, page_id, now=svc.clock(), timezone=_bot_zone(svc, uid),
                               exam_concepts=frequent)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/review/grade")
def post_review_grade(body: ReviewGradeIn, svc: Svc, uid: User,
                      tasks: BackgroundTasks) -> ReviewGradeOut:
    """Works without a gateway (fold only); 404 unknown cell / page / section, 422 a choice on
    a section that is not this cell's check."""
    with llm_flow():
        out = grade_review(PgRepo(svc.pool), _optional_gateway(svc, uid), uid, body,
                           now=svc.clock(), new_id=svc.new_id)
    _tick(tasks, svc, uid)
    return out


@router.post("/assets/generated")
def asset_generated(body: AssetGeneratedIn, svc: Svc, uid: User) -> AssetGeneratedOut:
    """Whether EvoWiki wrote this content for this user: its hash against what the server stored. A marker in a note
    proves nothing, and a copy of the same content is the same content. The plugin asks before it runs an `app` block
    (kind evoui: any JSON form of the block) or an `embedded-html` file (kind html: the file as it is)."""
    form = stored_form(body.text) if body.kind == "evoui" else body.text
    return AssetGeneratedOut(generated=form is not None and PgAssetStore(svc.pool).generated(uid, body.kind, sha256(form)))


@router.post("/assets/{asset_id}/results")
def post_block_results(asset_id: str, body: BlockResultsIn, svc: Svc, uid: User,
                       tasks: BackgroundTasks) -> BlockResultsOut:
    """What the reader did in an EvoUI block (evoui.js `results()` / onQuizResult / onEvent /
    progress()): quiz answers and item events are graded against the stored block; those with a
    known `cell` become quiz evidence in K (the K-change pass runs on the block's page); a resent
    event is reported `duplicate`; progress is stored when its revision is newer. 404 unknown
    asset, 422 not an evoui asset / no such node, item, question or option, 409 the same events
    are being recorded by a concurrent post. Works without a gateway."""
    with llm_flow():
        with svc.pool.connection() as conn, conn.transaction():
            guard_asset_write(conn, uid, asset_id)
        out = block_results(PgRepo(svc.pool), PgAssetStore(svc.pool), _optional_gateway(svc, uid),
                            uid, asset_id, body, now=svc.clock(), new_id=svc.new_id,
                            progress=PgProgressStore(svc.pool))
    _tick(tasks, svc, uid)
    return out


@router.get("/assets/{asset_id}/progress")
def get_block_progress(asset_id: str, svc: Svc, uid: User) -> BlockProgressOut:
    """The reader's stored progress in an EvoUI block (evoui.js `opts.progress`): {revision, data},
    {revision: 0, data: null} when none. 404 unknown asset, 422 not an evoui asset."""
    with llm_flow():
        with svc.pool.connection() as conn, conn.transaction():
            guard_asset_write(conn, uid, asset_id)
        return block_progress(PgAssetStore(svc.pool), PgProgressStore(svc.pool), uid, asset_id)


@router.post("/review/confirms/{followup_id}")
def post_review_confirm(followup_id: str, body: ConfirmAnswerIn, svc: Svc, uid: User,
                        tasks: BackgroundTasks) -> ConfirmAnswerOut:
    """Answer a wrong-answer follow-up's confirmation question (GET /review/today `confirms`):
    correct confirms the claim, anything else refutes it. Works without a gateway. 404 unknown,
    409 answered, 422 no such option."""
    with llm_flow():
        out = confirm_answer(PgRepo(svc.pool), _optional_gateway(svc, uid), uid, followup_id,
                             body.choice, now=svc.clock(), new_id=svc.new_id,
                             shown=body.artifact_id)
    _tick(tasks, svc, uid)
    return out
