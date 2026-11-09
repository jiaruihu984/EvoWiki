"""/wiki/rsi/*: run, list, undo, approve / discard (changes awaiting review) and settings of the per-user RSI cycle. The cycle, its lock, the
background tick and the stale-page rewrite live in `service.rsi_runs`."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from evowiki.bot.actions import NotDecidable
from evowiki.service import rsi_runs
from evowiki.service.deps import Services, check_user, current_user, get_services
from evowiki.store.rsi import PgRsiStore
from evowiki.teach.change_source import source_for
from evowiki.teach.contracts import (
    RSI_SWITCH_OFF,
    RSI_SWITCH_ON,
    ChangeOut,
    RsiSettings,
    RsiSwitch,
    rsi_on,
)
from evowiki.teach.cycle import CycleResult

router = APIRouter(prefix="/wiki/rsi")
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]


@router.post("/run")
def post_run(svc: Svc, uid: User) -> CycleResult:
    """Manual trigger: one cycle now, whatever the settings say. 409 while one is running. A2's
    model selection is deferred to the background tick (`a2` = "deferred"), so the request never
    runs its fits."""
    check_user(svc, uid)
    with rsi_runs._exclusive(svc, uid):
        # A2's model selection (1 + k fits) never runs on the request: the tick does it
        res = rsi_runs._cycle(svc, uid).run(uid, rsi_runs._data(svc, uid), trigger="manual",
                                            now=svc.clock(), select_a2=False)
    return res


@router.get("/changes")
def get_changes(svc: Svc, uid: User) -> list[ChangeOut]:
    """Every RSI change of every layer (and of the external sources), newest first."""
    return rsi_runs._cycle(svc, uid).changes(uid, rsi_runs._data(svc, uid), now=svc.clock())


@router.post("/patches/{patch_id}/undo")
def post_undo(patch_id: str, svc: Svc, uid: User) -> ChangeOut:
    """Undo one change: an id with a registered source prefix goes to that source; else a track-A
    patch; else a layer-2 method change (its registry rolls back to from_version). 404 unknown id
    (or another user's), 409 not undoable or a cycle running."""
    check_user(svc, uid)
    return rsi_runs.undo_rsi_change(svc, uid, patch_id)


def _decide(patch_id: str, svc: Services, uid: str, verb: str) -> ChangeOut:
    """A change that waits for the user's review (L2 code patches, review mode): only a source that
    decides its own rows (`approve` / `discard`) takes it. 404 unknown id (track-A patches decide
    through the Bot's ledger, not here), 409 not under review."""
    check_user(svc, uid)
    with rsi_runs._exclusive(svc, uid):
        src = source_for(svc.change_sources, patch_id)
        fn = getattr(src, verb, None) if src is not None else None
        if fn is None:
            raise LookupError(f"no change {patch_id} awaiting review")
        try:
            return fn(uid, patch_id, now=svc.clock())
        except ValueError as e:
            # a refusal's machine code (e.g. `patches_off`) goes out as the 409's `code`
            raise NotDecidable(str(e), error_code=getattr(e, "error_code", None)) from e


@router.post("/patches/{patch_id}/approve")
def post_approve(patch_id: str, svc: Svc, uid: User) -> ChangeOut:
    """Approve a change that waits for the user's review (an L2 code patch: queued for landing)."""
    return _decide(patch_id, svc, uid, "approve")


@router.post("/patches/{patch_id}/discard")
def post_discard(patch_id: str, svc: Svc, uid: User) -> ChangeOut:
    """Discard a change that waits for the user's review (an L2 code patch: never lands)."""
    return _decide(patch_id, svc, uid, "discard")


@router.get("/settings")
def get_rsi_settings(svc: Svc, uid: User) -> RsiSettings:
    check_user(svc, uid)
    return PgRsiStore(svc.pool).settings(uid)


@router.put("/settings")
def put_rsi_settings(body: RsiSettings, svc: Svc, uid: User) -> RsiSettings:
    check_user(svc, uid)
    _require_available(svc, rsi_on(body))
    store = PgRsiStore(svc.pool)
    store.put_settings(uid, body)
    return store.settings(uid)


@router.get("/switch")
def get_rsi_switch(svc: Svc, uid: User) -> RsiSwitch:
    """Whether rounds can run under the user's settings."""
    check_user(svc, uid)
    return RsiSwitch(on=rsi_on(PgRsiStore(svc.pool).settings(uid)))


@router.put("/switch")
def put_rsi_switch(body: RsiSwitch, svc: Svc, uid: User) -> RsiSwitch:
    """The settings' switch: writes every value that decides whether rounds run."""
    check_user(svc, uid)
    _require_available(svc, body.on)
    store = PgRsiStore(svc.pool)
    store.put_settings(uid, RSI_SWITCH_ON if body.on else RSI_SWITCH_OFF)
    return RsiSwitch(on=rsi_on(store.settings(uid)))


def _require_available(svc: Services, enabling: bool) -> None:
    if enabling:
        from evowiki.service.routes_setup import rsi_blockers
        if blockers := rsi_blockers(svc):
            raise HTTPException(409, detail={"code": "rsi_setup_incomplete", "params": {"blockers": blockers}})
