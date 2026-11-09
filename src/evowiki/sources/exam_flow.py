"""The DBOS workflow `evowiki.source_exam` (materials phase 5 F, plan decisions 1-3): when a
material put to an `exam` use has finished its full import, extract its questions
(`sources.exam`), attach them to the nodes of the user's goals (`sources.exam_attach`) and store
them (`sources.exam_store.replace_items`) with a summary in the material's progress record
(`progress.exam`: state extracting / done / failed and the counts the card shows).

Two hooks start it, like `match_flow`: `start_exam` is the import's `on_ready` (the use came
first), `start_exam_after_use` the 资料加入 hook (`sources.events.source_added`) for an exam use
added after the import ended. The workflow id is `source_exam:<user>:<source>:<run_gen>` for the
import's start (a second start of the same import run returns the first workflow, a re-import
extracts again) and `...:<run_gen>:<use_id>` for a use's start (a new exam use, e.g. 「这是往年题」
again after a failed run, extracts again on the same import).

Steps (each a `durable.step`, values JSON; items as `asdict`): `exam.load` checks the material
(live, an exam use, a full import done; else the run is "skipped" without a call) and marks it
extracting; `exam.extract` runs the extraction (an import run already extracted `done`
reuses its stored items, same ids, attachment reset: `exam.stored`, so a goal made later can
attach them without a second extraction); `exam.attach` (no call when the material was deleted or re-imported meanwhile) builds the
candidate nodes
(decision 3: the goals named by the exam uses, else every goal; plus the section's
`role=exercises` nodes) and attaches; `exam.commit` replaces the items and writes the summary,
only while the material is live and still on that import run. A model that is missing, a
provider rejection (402...) or the user's spending cap ends the run "failed" with the
learner-facing detail DETAIL_FAILED (the cause is logged, never shown). Nothing is left to the
user to ask again for a passing failure: an extraction whose every batch failed, and questions
whose attach call failed (pending), are tried again in the same workflow after the waits
RETRY_WAITS_S (durable sleeps; only the pending questions are attached again); only after the
last try does the run end failed / the questions stay pending. A material without question text
ends "done" with 0 items.

Pages the model could not trust (C, after the live MathDoc run): when a fresh extraction has an
item whose problem code is `handwriting_over_print` or `illegible` on a page the local OCR or the
text layer read (never a page Claude read already: no loops), `exam.reread_plan` picks those
pages; `exam.reread` has Claude (`deps.claude`, the import's page reader, in its printed-only
variant source-vision-printed@v1: handwriting is not copied at all) read them, each stored
as a page read by "claude" as soon as it is back; `exam.patch` swaps them into their sections like
the import's `source.patch` (re-chunked and re-embedded, the old summaries kept); and
`exam.reextract` extracts again only the batches whose text changed (the other items keep their
ids; a changed batch whose call fails keeps its first extraction). However many pages that is,
they are read: there is no cost quote, the user's own spending limit is the only stop. No
reader, a 402, the spending cap or a failed read keep the items as they were (logged; the UI
says nothing either way). A re-extraction that fails keeps the first extraction's items.

The progress record is shared with the import (end, mode, stage...): `progress.exam` is merged
into it under the user's writer lock, never written over it."""

import contextvars
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any

from dbos import DBOS
from psycopg_pool import ConnectionPool

from evowiki.agent import durable
from evowiki.goals.observe import JsonLLM
from evowiki.llm.providers import ProviderRejectedError
from evowiki.llm.run_context import traced_entry
from evowiki.retrieval.embed import Embedder
from evowiki.sources import exam_store, store
from evowiki.sources.exam import REREAD_CODES, Previous, extract_items, problem_code
from evowiki.sources.exam_attach import NodeChoice, attach_items
from evowiki.sources.locate import locate_json
from evowiki.sources.models import ExamItem, PageRead
from evowiki.sources.summary import text_hash
from evowiki.sources.vision import NoVision, VisionReader
from evowiki.store.locks import user_writer
from evowiki.wikiapp import kwrite
from evowiki.wikiapp.llm import LLM_BUDGET
from evowiki.wikiapp.path_order import shown
from evowiki.wikiapp.repo import Repo, Snapshot
from evowiki.wikiapp.source_view import _goal_nodes

log = logging.getLogger("evowiki.sources.exam_flow")

DETAIL_FAILED = "题目没抽出来，请重试"
DETAIL_PENDING = "题目没挂上，请重试"
RETRY_WAITS_S = (60.0, 300.0)   # waits before the 2nd and 3rd try of a failed extract / attach
USE_WAIT_S = 30.0       # how long `start_exam_after_use` waits for the use's commit
USE_POLL_S = 0.2
_USABLE = ("ready", "partial")
_SEEN = ("pdf", "image")      # kinds whose pages have an image Claude can read
RENDER_BATCH = 8


@dataclass
class ExamDeps:
    pool: ConnectionPool
    repo: Repo                    # the snapshot: goal nodes, k.aspects, page titles
    embedder: Embedder
    llm_for: Callable[[str], JsonLLM | None]   # user_id -> fast-tier JsonLLM; None: no gateway
    clock: Callable[[], datetime]
    new_id: Callable[[str], str]
    # the import's Claude page reader (ClaudePages, or NoVision): re-reads pages whose items the
    # model could not trust
    claude: VisionReader = field(default_factory=NoVision)
    sleep: Callable[[float], None] = time.sleep   # the waits off DBOS (tests: a fake)


_deps: ExamDeps | None = None


def install(deps: ExamDeps) -> None:
    global _deps
    _deps = deps


def uninstall() -> None:
    global _deps
    _deps = None


def installed() -> ExamDeps:
    if _deps is None:
        raise RuntimeError("sources.exam_flow.install(deps) was not called: no deps for exams")
    return _deps


def workflow_id(user_id: str, source_id: str, run_gen: int, use_id: str | None = None) -> str:
    """The import's start: one per import run; a later exam use (「这是往年题」 again, e.g. after
    a failed run) gets its own run on the same import, keyed by the use."""
    base = f"source_exam:{user_id}:{source_id}:{run_gen}"
    return base if use_id is None else f"{base}:{use_id}"


def wanted(uses: list[dict]) -> bool:
    """The material has an exam use."""
    return any(u["use"] == "exam" for u in uses)


def imported_in_full(progress: Mapping[str, Any]) -> bool:
    """The import's end says a full import finished (an end without a mode predates modes:
    every import was full then)."""
    return progress.get("end") == "done" and progress.get("mode", "full") == "full"


def _start(wid: str, user_id: str, source_id: str) -> None:
    from dbos import SetWorkflowID

    def go() -> None:
        with SetWorkflowID(wid):        # an existing id returns its workflow, nothing reruns
            DBOS.start_workflow(exam_workflow, user_id, source_id)
    durable.outside_dbos(go)


def start_exam(user_id: str, source_id: str, *, use_id: str | None = None) -> None:
    """The import's `on_ready`: starts the extraction of a material with an exam use whose full
    import is done (idempotent by its workflow id). `use_id` (the 资料加入 hook): that use's own
    run, started only when the import is done in full and no extraction is going (so it never
    runs beside another run of the same import). Never raises: a failed start is logged."""
    try:
        with installed().pool.connection() as conn:
            row = store.get_source(conn, user_id, source_id)
            uses = store.uses_of(conn, user_id, source_id)
        if row is None or row.deleted_at is not None or not wanted(uses) \
                or not imported_in_full(row.progress):
            return
        if use_id is not None and (row.progress.get("exam") or {}).get("state") == "extracting":
            log.info("source exam %s/%s: an extraction is going, use %s starts none", user_id,
                     source_id, use_id)
            return
        _start(workflow_id(user_id, source_id, row.run_gen, use_id), user_id, source_id)
    except Exception:
        log.exception("source exam %s/%s could not be started", user_id, source_id)


def start_exam_after_use(user_id: str, source_id: str, use_id: str) -> None:
    """The 资料加入 hook for an `exam` use. The use is written in the caller's transaction, not
    committed yet: a short thread waits until it is visible (at most USE_WAIT_S; a rolled-back
    use is logged and nothing starts), then calls `start_exam` with the use (its own workflow id,
    so a second 「这是往年题」 after a failed run extracts again; nothing while the import is still
    going: its `on_ready` does). Without a runtime (not installed: tests, no
    DBOS) nothing happens. Never raises."""
    deps = _deps
    if deps is None:
        log.debug("source %s/%s: no exam runtime, the exam use starts no extraction", user_id,
                  source_id)
        return

    def body() -> None:
        try:
            deadline = time.monotonic() + USE_WAIT_S
            while True:
                with deps.pool.connection() as conn:
                    seen = any(u["use_id"] == use_id
                               for u in store.uses_of(conn, user_id, source_id))
                if seen:
                    break
                if time.monotonic() >= deadline:
                    log.warning("source %s/%s: use %s never became visible (rolled back?): "
                                "no extraction started", user_id, source_id, use_id)
                    return
                time.sleep(USE_POLL_S)
            start_exam(user_id, source_id, use_id=use_id)
        except Exception:
            log.exception("source exam %s/%s after use %s could not be started", user_id,
                          source_id, use_id)
    threading.Thread(target=contextvars.Context().run, args=(body,), name="source-exam-use",
                     daemon=True).start()

# ---- the body -------------------------------------------------------------------------------

def _to_json(items: Sequence[ExamItem]) -> list[dict[str, Any]]:
    return [asdict(it) for it in items]


def _from_json(items: Sequence[Mapping[str, Any]]) -> list[ExamItem]:
    return [ExamItem(**{**d, "options": tuple(d["options"])}) for d in items]


def _set_exam(deps: ExamDeps, user_id: str, source_id: str, gen: int,
              exam: dict[str, Any]) -> bool:
    """Merges `exam` into the material's progress record under the user's writer lock; False
    (nothing written) when the material is gone or a newer import run started."""
    with user_writer(deps.pool, user_id) as tx:
        row = store.get_source(tx.conn, user_id, source_id)
        if row is None or row.deleted_at is not None or row.run_gen != gen:
            return False
        store.set_progress(tx.conn, user_id, source_id, {**row.progress, "exam": exam})
    return True


def _failed(deps: ExamDeps, user_id: str, source_id: str, gen: int) -> None:
    _set_exam(deps, user_id, source_id, gen, {"state": "failed", "detail": DETAIL_FAILED})


def _load(deps: ExamDeps, user_id: str, source_id: str) -> dict[str, Any] | None:
    with deps.pool.connection() as conn:
        row = store.get_source(conn, user_id, source_id)
        uses = store.uses_of(conn, user_id, source_id)
    if row is None or row.deleted_at is not None or row.status not in _USABLE:
        return None
    if not wanted(uses) or not imported_in_full(row.progress):
        return None
    goals = [u["goal_id"] for u in uses if u["use"] == "exam"]
    with deps.pool.connection() as conn:
        stored = exam_store.exam_gen(conn, user_id, source_id)
    # this import run was extracted already: only attach again (new goals may fit now)
    reuse = stored == row.run_gen and (row.progress.get("exam") or {}).get("state") == "done"
    if not _set_exam(deps, user_id, source_id, row.run_gen, {"state": "extracting"}):
        return None
    # one exam use without a goal (「这是往年题」 in a chat): every goal's nodes
    return {"gen": row.run_gen, "reuse": reuse,
            "goals": None if any(g is None for g in goals) else sorted(set(goals))}


def _stored(deps: ExamDeps, user_id: str, source_id: str) -> dict[str, Any]:
    """The items of a done extraction of this import run, same ids, attachment reset."""
    with deps.pool.connection() as conn:
        got = exam_store.items_of_source(conn, user_id, source_id)
    return {"items": _to_json([
        replace(it, attach="pending", node_id=None, cell=None, attach_note=None)
        if it.status == "ok" and it.kind != "group" else it for it in got])}


def _extract(deps: ExamDeps, user_id: str, source_id: str,
             previous: Previous | None = None) -> dict[str, Any]:
    """{"failed": why, "final": no try again helps (no model, 402, the spending cap)} or the
    extraction."""
    llm = deps.llm_for(user_id)
    if llm is None:
        return {"failed": "no LLM", "final": True}
    with deps.pool.connection() as conn:
        sections = store.sections(conn, user_id, source_id)
        doc = store.get_document(conn, user_id, source_id)
    try:
        got = extract_items(sections, llm=llm, new_id=deps.new_id, previous=previous)
    except (ProviderRejectedError, *LLM_BUDGET) as exc:
        return {"failed": repr(exc), "final": True}
    if got.diagnostics:
        log.info("source exam %s/%s: %d diagnostics: %s", user_id, source_id,
                 len(got.diagnostics), "; ".join(got.diagnostics[:20]))
    if got.batches > 0 and got.failed_batches == got.batches:
        return {"failed": f"all {got.batches} batches failed"}
    located = [replace(it, locator=locate_json(doc, source_id, it.page, it.stem))
               for it in got.items]
    return {"items": _to_json(located), "failed_batches": got.failed_batches,
            "batches": got.batches, "plans": [list(p) for p in got.plans],
            "origins": list(got.origins), "slots": list(got.slots)}


# ---- pages the model could not trust: re-read by Claude ---------------------------------

def reread_candidates(items: Sequence[ExamItem], reads: Mapping[int, PageRead]) -> list[int]:
    """Pages holding an item whose problem code is one of REREAD_CODES, unless Claude read the
    page already (a page read by the local OCR or only by the text layer is a candidate)."""
    pages = {it.page for it in items
             if it.page is not None and problem_code(it.problem) in REREAD_CODES}
    return sorted(p for p in pages if not (p in reads and reads[p].by == "claude"))


def _live_row(deps: ExamDeps, user_id: str, source_id: str, gen: int):
    with deps.pool.connection() as conn:
        row = store.get_source(conn, user_id, source_id)
    if row is None or row.deleted_at is not None or row.run_gen != gen:
        return None
    return row


def _reread_plan(deps: ExamDeps, user_id: str, source_id: str, gen: int,
                 items: list[dict[str, Any]]) -> dict[str, Any]:
    """{"pages": the pages Claude reads now} (empty: nothing to read; "why": logged reason)."""
    row = _live_row(deps, user_id, source_id, gen)
    if row is None or row.kind not in _SEEN:
        return {"pages": []}
    with deps.pool.connection() as conn:
        reads = store.page_reads(conn, user_id, source_id)
    pages = reread_candidates(_from_json(items), reads)
    if not pages:
        return {"pages": []}
    if not deps.claude.available():
        log.info("source exam %s/%s: pages %s need a seeing model; none configured", user_id,
                 source_id, pages)
        return {"pages": [], "why": "no reader"}
    return {"pages": pages}


def _reader(deps: ExamDeps, user_id: str) -> VisionReader:
    """The printed-only variant of the Claude reader (handwriting is not copied at all), metered
    to `user_id`."""
    reader = deps.claude
    printed = getattr(reader, "printed_only", None)       # ClaudePages: its printed variant
    if callable(printed):
        reader = printed()
    for_user = getattr(reader, "for_user", None)          # ClaudePages: metered to this user
    return for_user(user_id) if callable(for_user) else reader


def _reread(deps: ExamDeps, user_id: str, source_id: str, gen: int,
            pages: list[int]) -> dict[str, Any]:
    """Claude reads `pages` one by one, each stored as soon as it is back (a 402 halfway keeps
    what was paid for; a replay after a crash does not read a stored page again). {"read": the
    pages Claude read, "stop": why it stopped early, or None}."""
    from evowiki.sources.importer import _Deleted, _generation_connection, page_images
    row = _live_row(deps, user_id, source_id, gen)
    if row is None:
        return {"read": [], "stop": "gone"}
    with deps.pool.connection() as conn:
        f = store.get_file(conn, user_id, source_id)
        stored = store.page_reads(conn, user_id, source_id)
    if f is None:
        return {"read": [], "stop": "no file"}
    read = [p for p in pages if (r := stored.get(p)) is not None and r.by == "claude"
            and r.text]
    todo = [p for p in pages if p not in read]
    reader = _reader(deps, user_id)
    stop: str | None = None
    try:
        for i in range(0, len(todo), RENDER_BATCH):
            batch = todo[i:i + RENDER_BATCH]
            for p, img in zip(batch, page_images(row.kind, f[0], batch), strict=True):
                res = reader.read([img])
                if len(res) != 1:
                    raise ValueError(f"Claude reader returned {len(res)} reads for one page")
                text = (res[0].text or "").strip()
                if not text:
                    log.warning("source exam %s/%s page %d: Claude read nothing (%s)", user_id,
                                source_id, p, res[0].reason)
                    continue
                with _generation_connection(deps.pool, user_id, source_id, gen) as conn:
                    store.put_page_reads(conn, user_id, source_id,
                                         [PageRead(p, text, "claude", False)], deps.clock())
                read.append(p)
    except _Deleted:
        return {"read": [], "stop": "gone"}
    except ProviderRejectedError:
        log.warning("source exam %s/%s: Claude page reading rejected (402?)", user_id, source_id)
        stop = "rejected"
    except LLM_BUDGET:
        log.warning("source exam %s/%s: Claude page reading over the user's budget", user_id,
                    source_id)
        stop = "budget"
    except Exception:
        log.exception("source exam %s/%s: Claude page reading failed", user_id, source_id)
        stop = "error"
    return {"read": sorted(read), "stop": stop}


def _patch(deps: ExamDeps, user_id: str, source_id: str, gen: int,
           pages: list[int]) -> dict[str, Any]:
    """Claude's reads of `pages` into their sections (the import's swap), the changed sections
    re-chunked and re-embedded, the material's readings re-anchored (`reanchor_readings`: it
    raising fails the step); their summaries are kept (they summarized the same page as the
    OCR read it, under that text's hash). {"changed": section ids} ([]: gone, nothing written)."""
    from evowiki.sources.importer import _generation_connection, embed_pending, rechunk, swap_reads
    if _live_row(deps, user_id, source_id, gen) is None:
        return {"changed": []}
    with deps.pool.connection() as conn:
        stored = store.page_reads(conn, user_id, source_id)
        before = {s.section_id: s for s in store.sections(conn, user_id, source_id)}
    reads = [(p, r.text) for p in pages
             if (r := stored.get(p)) is not None and r.by == "claude" and r.text]
    changed = swap_reads(deps.pool, user_id, source_id, reads, gen=gen)
    if changed:
        from evowiki.wikiapp.reading import reanchor_readings  # its readings follow the text
        reanchor_readings(deps.repo, deps.pool, user_id, source_id, changed,
                          now=deps.clock(), new_id=deps.new_id, gen=gen)
        kept = [(sid, before[sid].summary, text_hash(before[sid].text)) for sid in changed
                if sid in before and before[sid].summary]
        if kept:
            with _generation_connection(deps.pool, user_id, source_id, gen) as conn:
                store.set_summaries(conn, user_id, source_id, kept)
        rechunk(deps.pool, user_id, source_id, changed, lambda: deps.new_id("sc"), gen=gen)
        embed_pending(deps.pool, user_id, source_id, deps.embedder, gen=gen)
    return {"changed": changed}


def _previous(got: Mapping[str, Any]) -> Previous:
    return Previous(plans={k: v for k, v in got["plans"]},
                    ids=dict(zip(got["origins"], (d["item_id"] for d in got["items"]),
                                 strict=True)),
                    slots=dict(zip(got.get("slots", []), (k for k, _ in got["plans"]),
                                   strict=False)))


def _reread_pages(deps: ExamDeps, user_id: str, source_id: str, gen: int,
                  got: dict[str, Any]) -> dict[str, Any]:
    """The steps for pages the model could not trust (module doc); returns the extraction to go
    on with: the re-extraction when pages were patched and it worked, else `got` unchanged."""
    if not any(problem_code(d.get("problem")) in REREAD_CODES for d in got["items"]):
        return got                  # decided from the step value: replay-stable
    plan = durable.step("exam.reread_plan",
                        lambda: _reread_plan(deps, user_id, source_id, gen, got["items"]))
    pages = plan["pages"]
    if not pages:
        return got
    read = durable.step("exam.reread", lambda: _reread(deps, user_id, source_id, gen, pages))
    if read["stop"]:
        log.warning("source exam %s/%s: re-reading pages %s stopped (%s); read %s", user_id,
                    source_id, pages, read["stop"], read["read"])
    if not read["read"]:
        return got
    patched = durable.step("exam.patch",
                           lambda: _patch(deps, user_id, source_id, gen, read["read"]))
    if not patched["changed"]:
        return got
    again = durable.step("exam.reextract",
                         lambda: _extract(deps, user_id, source_id, _previous(got)))
    lost = "failed" not in again and again["failed_batches"] > got["failed_batches"]
    if "failed" in again or lost or (again["batches"] > 0
                                     and again["failed_batches"] == again["batches"]):
        log.warning("source exam %s/%s: re-extracting after the re-read failed (%s): the first "
                    "extraction's items are kept", user_id, source_id,
                    again.get("failed", "every batch failed"))
        return got
    log.info("source exam %s/%s: pages %s re-read by Claude, re-extracted", user_id, source_id,
             read["read"])
    return again


def _scope(snap: Snapshot, goals: Sequence[str] | None) -> set[str]:
    """Decision 3: the nodes of the goals the exam uses name (a map: its nodes, regions' members
    and path pages; a path: its pages' nodes), else of every goal; named goals that no longer
    exist fall back to every goal."""
    state = snap.state

    def of_path(pid: str) -> set[str]:
        rec = state.paths.get(pid)
        return set() if rec is None else {i for it in shown(rec) for i in it.node_ids}

    if goals:
        out: set[str] = set()
        found = False
        for g in goals:
            if g in state.maps:
                out |= _goal_nodes(state, state.maps[g])
                found = True
            elif g in state.paths:
                out |= of_path(g)
                found = True
        if found:
            return out
        log.info("user %s: the exam's goals %s are gone, every goal's nodes are candidates",
                 snap.user_id, list(goals))
    out = set()
    for m in state.maps.values():
        out |= _goal_nodes(state, m)
    for pid in state.paths:
        out |= of_path(pid)
    return out


def _choices(snap: Snapshot, node_ids: set[str]) -> list[NodeChoice]:
    state = snap.state
    map_names: dict[str, str] = {}
    for m in state.maps.values():
        for n in m.nodes:
            if n.name.strip():
                map_names.setdefault(n.id, n.name.strip())
    titles: dict[str, str] = {}
    for pid, rec in state.pages.items():
        if pid in state.trashed_pages:
            continue
        for cid in rec.targets:                        # a page about the node first
            titles.setdefault(cid, rec.title)
    for pid, rec in state.pages.items():
        if pid in state.trashed_pages:
            continue
        for cid in rec.concepts:
            titles.setdefault(cid, rec.title)
    concepts = set(snap.k.concept_ids())
    out = []
    for nid in sorted(node_ids):
        name = (state.names[nid].name if nid in state.names else None) or map_names.get(nid) \
            or kwrite.concept_name(state, nid, user_id=snap.user_id)
        aspects = tuple(a.value for a in snap.k.aspects(nid)) if nid in concepts else ()
        title = titles.get(nid)
        text = f"{name}\n{title}" if title and title != name else name
        out.append(NodeChoice(node_id=nid, name=name, aspects=aspects, text=text))
    return out


def _attach(deps: ExamDeps, user_id: str, source_id: str, gen: int,
            goals: Sequence[str] | None, items: list[dict[str, Any]], *,
            pending_only: bool = False) -> dict[str, Any]:
    """{"items"} attached, {"skipped"} or {"failed"} (402 / the spending cap / no model).
    `pending_only`: a later try: only the questions still pending are attached again (with the
    groups their text needs); the others come back as they were."""
    with deps.pool.connection() as conn:
        row = store.get_source(conn, user_id, source_id)
    if row is None or row.deleted_at is not None or row.run_gen != gen:
        return {"skipped": "deleted or re-imported"}          # no paid call for a gone material
    llm = deps.llm_for(user_id)
    if llm is None:
        return {"failed": "no LLM"}
    snap = deps.repo.load(user_id)
    nodes = _scope(snap, goals)
    with deps.pool.connection() as conn:
        links = store.links_for_sources(conn, user_id, [source_id])
    exercise: dict[str, list[str]] = {}
    for link in links:
        if link.role == "exercises":
            exercise.setdefault(link.section_id, []).append(link.node_id)
            nodes.add(link.node_id)            # decision 3: the section's exercise nodes too
    every = _from_json(items)
    todo = [it for it in every if it.kind == "group" or _is_pending(it)] if pending_only \
        else every
    try:
        out = attach_items(todo, _choices(snap, nodes) if nodes else [],
                           exercise_nodes=exercise, embedder=deps.embedder, llm=llm)
    except (ProviderRejectedError, *LLM_BUDGET) as exc:
        return {"failed": repr(exc)}
    if pending_only:
        again = {it.item_id: it for it in out}
        out = tuple(again[it.item_id] if _is_pending(it) else it for it in every)
    return {"items": _to_json(out)}


def _is_pending(it: ExamItem) -> bool:
    return it.status == "ok" and it.kind != "group" and it.attach == "pending"


def _any_pending(items: Sequence[Mapping[str, Any]]) -> bool:
    return any(_is_pending(it) for it in _from_json(items))


def summary(items: Sequence[ExamItem]) -> dict[str, Any]:
    """`progress.exam` of a finished run: questions only (a group is a passage, not a question).
    `pending`: ok questions whose attach call failed; all of them pending (every attach batch
    failed) keeps state done (the items are stored) with DETAIL_PENDING."""
    qs = [it for it in items if it.kind != "group"]
    ok = [it for it in qs if it.status == "ok"]
    pending = sum(1 for it in ok if it.attach == "pending")
    out: dict[str, Any] = {
        "state": "done", "items": len(qs),
        "attached": sum(1 for it in ok if it.attach == "attached"),
        "unattached": sum(1 for it in ok if it.attach == "unattached"),
        "pending": pending,
        "unreadable": sum(1 for it in qs if it.status == "unreadable"),
        "incomplete": sum(1 for it in qs if it.status == "incomplete")}
    if ok and pending == len(ok):
        out["detail"] = DETAIL_PENDING
    return out


def _commit(deps: ExamDeps, user_id: str, source_id: str, gen: int,
            items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Replaces the items and merges the summary, in one transaction under the writer lock;
    None when the material is gone or a newer import run started (nothing written)."""
    parsed = _from_json(items)
    exam = summary(parsed)
    with user_writer(deps.pool, user_id) as tx:
        row = store.get_source(tx.conn, user_id, source_id)
        if row is None or row.deleted_at is not None or row.run_gen != gen:
            return None
        exam_store.replace_items(tx.conn, user_id, source_id, parsed, gen=gen, now=deps.clock())
        store.set_progress(tx.conn, user_id, source_id, {**row.progress, "exam": exam})
    return exam


@traced_entry("source_exam", subject="source_id")
def run_exam(deps: ExamDeps, user_id: str, source_id: str) -> dict[str, Any]:
    """The body: {"state": "skipped"} (nothing to extract, no call), {"state": "failed"} (logged;
    the material says DETAIL_FAILED) or the summary written ({"state": "done", counts})."""
    loaded = durable.step("exam.load", lambda: _load(deps, user_id, source_id))
    if loaded is None:
        return {"state": "skipped"}
    gen, goals = loaded["gen"], loaded["goals"]
    try:
        if loaded.get("reuse"):
            got = durable.step("exam.stored", lambda: _stored(deps, user_id, source_id))
            got |= {"failed_batches": 0, "batches": 0}
        else:
            got = durable.step("exam.extract", lambda: _extract(deps, user_id, source_id))
            for wait in RETRY_WAITS_S:          # a passing failure: try again, not the user
                if "failed" not in got or got.get("final"):
                    break
                log.warning("source exam %s/%s: extraction failed (%s), trying again in %.0f s",
                            user_id, source_id, got["failed"], wait)
                durable.sleep(wait, deps.sleep)
                got = durable.step("exam.extract", lambda: _extract(deps, user_id, source_id))
        if "failed" in got:
            log.warning("source exam %s/%s: extraction failed: %s", user_id, source_id,
                        got["failed"])
            durable.step("exam.fail", lambda: _failed(deps, user_id, source_id, gen))
            return {"state": "failed"}
        if "plans" in got:          # a fresh extraction (absent in values recorded before)
            got = _reread_pages(deps, user_id, source_id, gen, got)
        if got["failed_batches"]:
            log.warning("source exam %s/%s: %d of %d batches failed", user_id, source_id,
                        got["failed_batches"], got["batches"])
        attached = durable.step("exam.attach", lambda: _attach(deps, user_id, source_id, gen,
                                                               goals, got["items"]))
        if "skipped" in attached:
            log.info("source exam %s/%s: deleted or re-imported before attaching", user_id,
                     source_id)
            return {"state": "skipped"}
        if "failed" in attached:
            log.warning("source exam %s/%s: attaching failed: %s", user_id, source_id,
                        attached["failed"])
            durable.step("exam.fail", lambda: _failed(deps, user_id, source_id, gen))
            return {"state": "failed"}
        for wait in RETRY_WAITS_S:              # questions whose attach call failed: again
            if not _any_pending(attached["items"]):
                break
            log.warning("source exam %s/%s: some questions are not attached yet, trying again "
                        "in %.0f s", user_id, source_id, wait)
            durable.sleep(wait, deps.sleep)
            again = durable.step("exam.attach", lambda a=attached: _attach(
                deps, user_id, source_id, gen, goals, a["items"], pending_only=True))
            if "items" not in again:            # gone, or a 402 / the cap: keep what we have
                break
            attached = again
        done = durable.step("exam.commit", lambda: _commit(deps, user_id, source_id, gen,
                                                           attached["items"]))
    except Exception:     # fail closed: the card never stays 「正在找题」
        log.exception("source exam %s/%s crashed", user_id, source_id)
        try:
            durable.step("exam.fail", lambda: _failed(deps, user_id, source_id, gen))
        except Exception:
            log.exception("source exam %s/%s: the failure could not be recorded", user_id,
                          source_id)
        raise
    if done is None:
        log.info("source exam %s/%s: deleted or re-imported meanwhile, nothing stored", user_id,
                 source_id)
        return {"state": "skipped"}
    return done


@DBOS.workflow(name="evowiki.source_exam")
def exam_workflow(user_id: str, source_id: str) -> str:
    return run_exam(installed(), user_id, source_id)["state"]
