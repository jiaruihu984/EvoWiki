"""Page revisions from materials (materials spec 2026-10-20 §7 D): start a whole-page draft.

- `POST /wiki/pages/{page_id}/revise` (the assistant's tool, the Bot, tests; no page button calls
  it): a `page_revise` ledger row (`source="user"`, always ask) and the `source_revise` run that
  writes its draft. `url` / `text` are imported first as a material of the page
  (`routes_sources._import`, use=supplement); no material at all: the run picks them. 404 unknown
  page or material, 422 a user note or a page in the trash, 409 the page already has an open draft
  (`action_id` names it).
- `POST /wiki/sources/{source_id}/revise` (the Bot's 更新 on 「资料讲到了已有的页」): the same per
  page with that one material (`source="bot"`); a page that has an open draft, is gone, is in the
  trash or is a user note is skipped (logged), the others still start.

The writing streams on `GET /wiki/pages/{page_id}/run/stream`; the draft is the row
(`GET /wiki/bot/actions`, `ActionOut.revise`), confirmed / rejected through the ledger."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from evowiki.assistant.tools.fetch_url import Fetcher
from evowiki.bot.actions import ActionLedger
from evowiki.bot.revise_schemas import (
    ReviseIn,
    ReviseStarted,
    SourceReviseIn,
    SourceReviseOut,
)
from evowiki.service.deps import Services, check_user, current_user, get_services, page_deps
from evowiki.service.routes_sources import MSG_NOT_FOUND, _import, get_fetcher
from evowiki.sources import store as source_store
from evowiki.wikiapp.repo import PgRepo, WikiInvalid
from evowiki.wikiapp.source_revise import RevisePending, revisable, start_revise

Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]
router = APIRouter(prefix="/wiki")
log = logging.getLogger(__name__)


def _ledger(svc: Services) -> ActionLedger:
    if svc.actions is None:
        raise HTTPException(status_code=503, detail="actions unavailable")
    return svc.actions


def _pending(e: RevisePending) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(e), "action_id": e.row.action_id})


def _start(svc: Services, uid: str, page_id: str, source_ids: list[str], *, source: str,
           origin: str) -> ReviseStarted:
    row, run_id = start_revise(
        ledger=_ledger(svc), repo=PgRepo(svc.pool), runs=svc.runs, deps=lambda: page_deps(svc),
        user_id=uid, page_id=page_id, source_ids=source_ids, source=source,
        key=f"revise:{page_id}:{svc.new_id('rv')}", now=svc.clock(), origin=origin)
    return ReviseStarted(action_id=row.action_id, run_id=run_id, page_id=page_id)


@router.post("/pages/{page_id}/revise", status_code=202, response_model=ReviseStarted)
def post_page_revise(page_id: str, body: ReviseIn, svc: Svc, uid: User,
                     fetcher: Annotated[Fetcher, Depends(get_fetcher)]):
    check_user(svc, uid)
    ledger = _ledger(svc)
    try:
        revisable(PgRepo(svc.pool), ledger, svc.runs, uid, page_id, svc.clock())  # before any import
        source_ids = list(body.source_ids)
        if body.url or body.text:
            fields = {"url": body.url} if body.url else {"text": body.text or ""}
            src, _ = _import(svc, uid, fetcher, None, {**fields, "page_id": page_id},
                             "supplement")
            source_ids.append(src.source_id)
        return _start(svc, uid, page_id, source_ids, source="user", origin="open")
    except RevisePending as e:
        return _pending(e)


@router.post("/sources/{source_id}/revise", response_model=SourceReviseOut)
def post_source_revise(source_id: str, body: SourceReviseIn, svc: Svc, uid: User):
    check_user(svc, uid)
    ledger = _ledger(svc)
    with svc.pool.connection() as conn:
        src = source_store.get_source(conn, uid, source_id)
    if src is None or src.deleted_at is not None:
        raise HTTPException(404, MSG_NOT_FOUND)
    page_ids = list(dict.fromkeys(body.page_ids))
    repo = PgRepo(svc.pool)
    free = []
    for pid in page_ids:                 # every page checked before any draft starts
        try:
            revisable(repo, ledger, svc.runs, uid, pid, svc.clock())
            free.append(pid)
        except RevisePending:
            continue                     # it has a draft already: no second one
        except (LookupError, WikiInvalid) as e:
            # deleted / in the trash / a user note since the suggestion: the other pages still go
            log.info("source %s: page %s/%s skipped: %s", source_id, uid, pid, e)
    started = []
    for pid in free:
        try:
            started.append(_start(svc, uid, pid, [source_id], source="bot", origin="background"))
        except RevisePending:
            continue
        except (LookupError, WikiInvalid) as e:
            log.info("source %s: page %s/%s skipped: %s", source_id, uid, pid, e)
    return SourceReviseOut(started=started)
