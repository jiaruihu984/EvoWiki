"""/wiki/teach/*: the teaching spec θ_rep,u (R1; wire shapes in wikiapp/wiki_schemas.py).

Every write saves one whole new version and returns the new `TeachSpecOut`. `?base_version=`
(optional) must equal the current version, else 409. Caps → 422 (`SpecInvalid`, mapped in
service/app.py, also for a user whose >30 old guidelines cannot be migrated), unknown ids → 404."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.service.deps import Services, current_user, get_services
from evowiki.teach.contracts import RepParams
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.signals import list_signals
from evowiki.wikiapp.teach import (
    add_exemplar_flow,
    add_item_flow,
    edit_item_flow,
    get_spec,
    list_versions,
    remove_exemplar_flow,
    remove_item_flow,
    restore_flow,
    set_params_flow,
    take_over_flow,
    version_out,
)
from evowiki.wikiapp.wiki_schemas import (
    DEFAULT_SIGNAL_LIMIT,
    MAX_SIGNAL_LIMIT,
    ExemplarIn,
    SignalsOut,
    SpecItemIn,
    SpecItemPatchIn,
    TeachSpecOut,
    TeachVersionsOut,
)

router = APIRouter(prefix="/wiki/teach")
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]
BaseVersion = Annotated[int | None, Query(ge=1)]


@router.get("/spec")
def get_teach_spec(svc: Svc, uid: User) -> TeachSpecOut:
    return get_spec(PgRepo(svc.pool).load(uid).state)


@router.get("/versions")
def get_teach_versions(svc: Svc, uid: User) -> TeachVersionsOut:
    return list_versions(PgRepo(svc.pool).load(uid).state)


@router.get("/versions/{version}")
def get_teach_version(version: int, svc: Svc, uid: User) -> TeachSpecOut:
    return version_out(PgRepo(svc.pool), uid, version)


@router.post("/versions/{version}/restore")
def post_teach_restore(version: int, svc: Svc, uid: User,
                       base_version: BaseVersion = None) -> TeachSpecOut:
    return restore_flow(PgRepo(svc.pool), uid, version, now=svc.clock(), new_id=svc.new_id,
                        base_version=base_version)


@router.post("/items", status_code=201)
def post_teach_item(body: SpecItemIn, svc: Svc, uid: User,
                    base_version: BaseVersion = None) -> TeachSpecOut:
    return add_item_flow(PgRepo(svc.pool), uid, body, now=svc.clock(), new_id=svc.new_id,
                         base_version=base_version)


@router.patch("/items/{item_id}")
def patch_teach_item(item_id: str, body: SpecItemPatchIn, svc: Svc, uid: User,
                     base_version: BaseVersion = None) -> TeachSpecOut:
    return edit_item_flow(PgRepo(svc.pool), uid, item_id, body, now=svc.clock(),
                          new_id=svc.new_id, base_version=base_version)


@router.delete("/items/{item_id}")
def delete_teach_item(item_id: str, svc: Svc, uid: User,
                      base_version: BaseVersion = None) -> TeachSpecOut:
    return remove_item_flow(PgRepo(svc.pool), uid, item_id, now=svc.clock(),
                            new_id=svc.new_id, base_version=base_version)


@router.post("/items/{item_id}/take_over")
def post_teach_take_over(item_id: str, svc: Svc, uid: User,
                         base_version: BaseVersion = None) -> TeachSpecOut:
    """「设为我的规则」 (rule provenance contract decision 2): an RSI or pending rule becomes the
    user's; RSI no longer changes or removes it."""
    return take_over_flow(PgRepo(svc.pool), uid, item_id, now=svc.clock(), new_id=svc.new_id,
                          base_version=base_version)


@router.put("/params")
def put_teach_params(body: RepParams, svc: Svc, uid: User,
                     base_version: BaseVersion = None) -> TeachSpecOut:
    return set_params_flow(PgRepo(svc.pool), uid, body, now=svc.clock(), new_id=svc.new_id,
                           base_version=base_version)


@router.post("/exemplars", status_code=201)
def post_teach_exemplar(body: ExemplarIn, svc: Svc, uid: User,
                        base_version: BaseVersion = None) -> TeachSpecOut:
    return add_exemplar_flow(PgRepo(svc.pool), uid, body, now=svc.clock(), new_id=svc.new_id,
                             base_version=base_version)


@router.delete("/exemplars/{exemplar_id}")
def delete_teach_exemplar(exemplar_id: str, svc: Svc, uid: User,
                          base_version: BaseVersion = None) -> TeachSpecOut:
    return remove_exemplar_flow(PgRepo(svc.pool), uid, exemplar_id, now=svc.clock(),
                                new_id=svc.new_id, base_version=base_version)


@router.get("/signals")
def get_teach_signals(svc: Svc, uid: User, after: str | None = None,
                      limit: Annotated[int, Query(ge=1, le=MAX_SIGNAL_LIMIT)] = DEFAULT_SIGNAL_LIMIT,
                      kinds: str | None = None) -> SignalsOut:
    """The user's feedback signals — stored ones plus assistant selections derived from the
    assistant sessions — paged by commit order (`wikiapp.signals.list_signals`). `after` = an
    earlier response's `cursor` / `next` (URL-safe, no encoding needed); `kinds` =
    comma-separated SignalKind values. Bad cursor / unknown kind → 422."""
    return list_signals(PgRepo(svc.pool), uid, sessions=PgSessionStore(svc.pool), after=after,
                        limit=limit, kinds=kinds)
