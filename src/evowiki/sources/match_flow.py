"""The DBOS workflow `evowiki.source_match` (materials design §7 D): when a supplement material
has finished importing, find the user's pages it covers (`sources.match`), record the wiki
event `SourceMatched` and wake the Bot, which turns it into a 资料更新 suggestion.

`start_match` is the import's `on_ready` hook; `start_match_after_use` is the 资料加入 hook
(`sources.events.source_added`) for a `supplement` use added after the import ended. Both look at
the material's uses before starting anything: only a material with no use yet or a `supplement`
use is matched; one that is only a chat attachment (`ask`) or was added to learn / understand /
prepare an exam returns without a workflow, so its id `source_match:<user>:<source>` stays free
for a later `supplement` use. The import runs its end again on every retry, so the hook may be
called many times: the fixed id makes a second start return the first workflow.

Steps (each a `durable.step`, values JSON): `match.uses` checks again (the workflow may start
late) and stops quietly for a deleted / unusable / non-supplement material or one already
matched; `match.vectors` takes the material's section vectors (same embedder tag only) and
embeds the user's EvoWiki pages on the fly (not the user's own notes, not the trash), and
returns the candidates only, not the vectors (they would bloat the checkpoint); a material without
section vectors under that tag ends the run `failed` (logged, no event: `pages=()` would mark it
matched for good); `match.judge`
asks the model, or records `pages=()` without a call when no candidate passes the vectors'
first cut; `match.commit` commits the event (once per source: checked first, fixed event id)
and then calls the Bot's notify. A failed judgement (bad output, no model, a 402, the user's
spending cap) is logged and ends the run without an event or a suggestion; the user can still
revise a page from the material by hand.

DBOS cannot start a workflow inside a step, and a start from the body would be recorded as a
child of it: starts and the Bot's wake-up run on a short thread with an empty context
(`durable.outside_dbos`; the same reason `page_flows` kicks from a thread). Registered by the
decorator at import: import this module before `dbos_app.launch` (`DurableRuntime.start` does)."""

import contextvars
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from dbos import DBOS
from psycopg_pool import ConnectionPool

from evowiki.agent import durable
from evowiki.clock import Clock
from evowiki.goals.observe import JsonLLM
from evowiki.llm.run_context import traced_entry
from evowiki.retrieval.embed import Embedder
from evowiki.sources import store
from evowiki.sources.locate import locate_json
from evowiki.sources.match import (
    CANDIDATES,
    MIN_SCORE,
    Candidate,
    MatchFailed,
    candidates,
    judge,
    page_text,
    section_head,
)
from evowiki.store.wiki import WikiEventConflict
from evowiki.wikiapp.events import MatchedPage, SourceMatched, WikiEvent
from evowiki.wikiapp.llm import LLM_BUDGET, LLM_UNAVAILABLE, LLMUnavailable
from evowiki.wikiapp.repo import Commit, Repo, WikiConflict

log = logging.getLogger("evowiki.sources.match_flow")

EMBED_BATCH = 64
COMMIT_TRIES = 3
USE_WAIT_S = 30.0       # how long `start_match_after_use` waits for the use's commit
USE_POLL_S = 0.2
_USABLE = ("ready", "partial")


@dataclass
class MatchDeps:
    pool: ConnectionPool
    repo: Repo
    embedder: Embedder
    llm_for: Callable[[str], JsonLLM | None]   # user_id -> fast-tier JsonLLM; None: no gateway
    clock: Clock
    notify: Callable[[str], None] | None = None   # the Bot's wake-up; None: the Bot is off


_deps: MatchDeps | None = None


def install(deps: MatchDeps) -> None:
    global _deps
    _deps = deps


def uninstall() -> None:
    global _deps
    _deps = None


def installed() -> MatchDeps:
    if _deps is None:
        raise RuntimeError("sources.match_flow.install(deps) was not called: no deps for matches")
    return _deps


def workflow_id(user_id: str, source_id: str) -> str:
    return f"source_match:{user_id}:{source_id}"


def wanted(uses: Iterable[str]) -> bool:
    """A material is matched when it has no use yet or is a supplement."""
    uses = set(uses)
    return not uses or "supplement" in uses


def _uses(pool: ConnectionPool, user_id: str, source_id: str) -> list[str]:
    with pool.connection() as conn:
        return [u["use"] for u in store.uses_of(conn, user_id, source_id)]


def _start(wid: str, user_id: str, source_id: str) -> None:
    from dbos import SetWorkflowID

    def go() -> None:
        with SetWorkflowID(wid):        # an existing id returns its workflow, nothing reruns
            DBOS.start_workflow(match_workflow, user_id, source_id)
    durable.outside_dbos(go)


def start_match(user_id: str, source_id: str) -> None:
    """The import's `on_ready`: starts the match of a supplement material (idempotent by its
    workflow id). Never raises: a failed start is logged and the import ends as it would."""
    try:
        if not wanted(_uses(installed().pool, user_id, source_id)):
            return
        _start(workflow_id(user_id, source_id), user_id, source_id)
    except Exception:
        log.exception("source match %s/%s could not be started", user_id, source_id)


def start_match_after_use(user_id: str, source_id: str, use_id: str) -> None:
    """The 资料加入 hook (`sources.events.source_added`) for a `supplement` use. That use is
    written in the caller's transaction, not committed yet: a workflow started now could read the
    uses without it and end for good under its fixed id. So a short thread waits until the use
    row is visible (at most USE_WAIT_S; a rolled-back use is logged and nothing starts), then
    calls `start_match` when the material's import has already ended `done`; one still reading
    is matched by the import's `on_ready` (starting earlier would find no section vectors and
    use up the id). Without a durable runtime (not installed: tests, no DBOS) nothing happens.
    Never raises."""
    deps = _deps
    if deps is None:
        log.debug("source %s/%s: no match runtime, the supplement use starts no match", user_id,
                  source_id)
        return

    def body() -> None:
        try:
            deadline = time.monotonic() + USE_WAIT_S
            while True:
                with deps.pool.connection() as conn:
                    seen = any(u["use_id"] == use_id
                               for u in store.uses_of(conn, user_id, source_id))
                    row = store.get_source(conn, user_id, source_id) if seen else None
                if seen:
                    break
                if time.monotonic() >= deadline:
                    log.warning("source %s/%s: use %s never became visible (rolled back?): "
                                "no match started", user_id, source_id, use_id)
                    return
                time.sleep(USE_POLL_S)
            if row is None or row.deleted_at is not None or row.progress.get("end") != "done":
                return               # deleted, or still importing: `on_ready` starts it
            start_match(user_id, source_id)
        except Exception:
            log.exception("source match %s/%s after use %s could not be started", user_id,
                          source_id, use_id)
    threading.Thread(target=contextvars.Context().run, args=(body,), name="source-match-use",
                     daemon=True).start()

# ---- the body -------------------------------------------------------------------------------

def _already(repo: Repo, user_id: str, source_id: str) -> bool:
    return any(e.body.source_id == source_id
               for e in repo.wiki_events_of(user_id, ["source_matched"]))


def _check(deps: MatchDeps, user_id: str, source_id: str) -> dict[str, Any] | None:
    with deps.pool.connection() as conn:
        row = store.get_source(conn, user_id, source_id)
        uses = [u["use"] for u in store.uses_of(conn, user_id, source_id)]
    if row is None or row.deleted_at is not None or row.status not in _USABLE:
        return None
    if not wanted(uses) or _already(deps.repo, user_id, source_id):
        return None
    return {"title": row.title}


def _candidates(deps: MatchDeps, user_id: str, source_id: str) -> list[list[Any]] | None:
    """[page_id, score, [section ids]] of the pages at or above MIN_SCORE, best first. None: the
    material has no section vectors under this embedder (a failure, not "covers nothing").
    Never a page the material was added for (a use's `page_id`: a revise from pasted text or a
    URL) or whose current text was written from it already (`PageRecord.sources`)."""
    e = deps.embedder
    with deps.pool.connection() as conn:
        secs = store.section_vectors(conn, user_id, source_id, (e.id, e.model_version, e.dim))
        used_on = {u["page_id"] for u in store.uses_of(conn, user_id, source_id)
                   if u["page_id"]}
    if not secs:
        return None
    state = deps.repo.load(user_id).state
    recs = [r for pid, r in state.pages.items()
            if r.has_body and pid not in state.trashed_pages   # never the user's notes / a page
            # still being written
            and pid not in used_on and all(s.source_id != source_id for s in r.sources)]
    if not recs:
        return []
    texts = [page_text(r) for r in recs]
    vecs: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        vecs.extend(e.embed_documents(texts[i:i + EMBED_BATCH]))
    found = candidates({r.page_id: v for r, v in zip(recs, vecs, strict=True)}, secs,
                       k=CANDIDATES)
    return [[pid, score, list(sids)] for pid, score, sids in found if score >= MIN_SCORE]


def _judge(deps: MatchDeps, user_id: str, source_id: str, title: str,
           found: list[list[Any]]) -> dict[str, Any]:
    """{"pages": [MatchedPage json]} or {"failed": why} (logged by the body)."""
    if not found:
        return {"pages": []}
    llm = deps.llm_for(user_id)
    if llm is None:
        return {"failed": "no LLM"}
    state = deps.repo.load(user_id).state
    with deps.pool.connection() as conn:
        rows = {s.section_id: s for s in store.sections(conn, user_id, source_id)}
    cands = []
    for pid, score, sids in found:
        rec = state.pages.get(pid)
        if rec is None or not rec.has_body or pid in state.trashed_pages:
            continue                        # gone (or rewritten from scratch) since the vectors
        shown = tuple((sid, rows[sid].title, section_head(rows[sid].title, rows[sid].text))
                      for sid in sids if sid in rows)
        if shown:
            cands.append(Candidate(page_id=pid, title=rec.title, text=page_text(rec),
                                   score=float(score), sections=shown))
    try:
        pages = judge(llm, title, cands)
    except (MatchFailed, LLMUnavailable, *LLM_UNAVAILABLE, *LLM_BUDGET) as exc:
        return {"failed": repr(exc)}
    with deps.pool.connection() as conn:
        doc = store.get_document(conn, user_id, source_id)
    pages = [p.model_copy(update={"locators": tuple(
        locate_json(doc, source_id, rows[sid].page_from, rows[sid].title) for sid in p.section_ids)})
        for p in pages]
    return {"pages": [p.model_dump(mode="json") for p in pages]}


def _commit(deps: MatchDeps, user_id: str, source_id: str, title: str,
            pages: list[dict[str, Any]]) -> bool:
    """Commits the event once per source; True when this call committed it (then the Bot is
    woken). Only conflicting writes are retried: SourceMatched itself is benign to others."""
    body = SourceMatched(source_id=source_id, title=title,
                         pages=tuple(MatchedPage.model_validate(p) for p in pages))
    ev = WikiEvent(event_id=f"source_matched:{user_id}:{source_id}", ts=deps.clock(), body=body)
    for attempt in range(COMMIT_TRIES):
        if _already(deps.repo, user_id, source_id):
            return False
        try:
            deps.repo.commit(deps.repo.load(user_id), Commit(wiki=[ev]))
            break
        except WikiEventConflict:           # another run committed it meanwhile
            return False
        except WikiConflict:
            if attempt == COMMIT_TRIES - 1:
                raise
    notify = deps.notify
    if notify is not None:
        try:
            durable.outside_dbos(lambda: notify(user_id))
        except Exception:      # the event is in; the Bot's sweep picks it up
            log.exception("bot notify after the match of %s/%s failed", user_id, source_id)
    return True


@traced_entry("source_match", subject="source_id")
def run_match(user_id: str, source_id: str, deps: MatchDeps) -> str:
    """The body: "skipped" (nothing to match), "failed" (logged, no event) or "matched"."""
    checked = durable.step("match.uses", lambda: _check(deps, user_id, source_id))
    if checked is None:
        return "skipped"
    found = durable.step("match.vectors", lambda: _candidates(deps, user_id, source_id))
    if found is None:       # fail closed: an empty match would hide the material for good
        log.warning("source match %s/%s: no suggestion, the material has no section vectors "
                    "under embedder %s", user_id, source_id, deps.embedder.model_version)
        return "failed"
    judged = durable.step("match.judge", lambda: _judge(deps, user_id, source_id,
                                                        checked["title"], found))
    if "failed" in judged:
        log.warning("source match %s/%s: no suggestion, the judgement failed: %s", user_id,
                    source_id, judged["failed"])
        return "failed"
    committed = durable.step("match.commit", lambda: _commit(deps, user_id, source_id,
                                                             checked["title"], judged["pages"]))
    return "matched" if committed else "skipped"


@DBOS.workflow(name="evowiki.source_match")
def match_workflow(user_id: str, source_id: str) -> None:
    run_match(user_id, source_id, installed())
