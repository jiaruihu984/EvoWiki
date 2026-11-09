"""Promote, approve, discard, undo and revert-all of L2 patches (plan P3 T12; Codex CLI: the
approval policy is kept apart from the sandbox). These functions only move rows: the supervisor's
`live.step` lands, reverts and restarts. A patch candidate's status follows `live.step`: promoted
until it is withdrawn / rolled back.

Review mode (ruling 2026-10-23, design §9): the agent writes, tests and archives a patch; the USER
reviews it and decides whether it lands. A code patch never reaches `queued` without the user's
explicit `approve`; there is no setting that turns this off. A patch is global in effect and every
user sees every patch row, so any user who sees the row may approve, discard or undo it."""

from collections.abc import Callable
from datetime import datetime

import psycopg

from evowiki.forge import ids, switch
from evowiki.forge.models import ForgeNotFound, PatchesOff, PromoteRefused
from evowiki.store import forge as store
from evowiki.store import forge_patch as ps

DISCARDED = "你没有采用这项改动"
_UNDER_REVIEW = ("built", "awaiting_approval")


def needs_approval(conn: psycopg.Connection, user_id: str | None) -> bool:
    """Always True: a code patch lands only after the user's explicit approval (review mode). Not
    configurable; an old stored `approve.patch` setting is ignored."""
    if user_id is not None and user_id != ids.GLOBAL:
        ids.check_user(user_id)
    return True


def promote_patch(conn: psycopg.Connection, candidate_id: str, *, gate_ref: str,
                  gate_ok: Callable[[str], bool], by: str, why: str, now: datetime) -> str:
    """A gate passed: built → promoted; the patch row → awaiting_approval (it waits for the user's
    `approve`, `needs_approval`). Returns the new state."""
    if not gate_ref or not gate_ref.strip():
        raise PromoteRefused("no gate record given")
    cand = store.get_candidate(conn, candidate_id)
    row = ps.get_patch(conn, candidate_id)
    if cand is None or row is None:
        raise ForgeNotFound(f"no patch candidate {candidate_id!r}")
    if cand.kind != "patch" or cand.status != "built" or row.state != "built":
        raise PromoteRefused(f"candidate {candidate_id!r} is not a built patch")
    if not gate_ok(gate_ref):
        raise PromoteRefused(f"gate {gate_ref!r} did not pass")
    if not switch.enabled(conn, row.user_id, "patch"):
        raise PromoteRefused("代码自动修改已关闭")
    state = "awaiting_approval"            # review mode: never queued without the user (`approve`)
    store.set_status(conn, candidate_id, "promoted", now)
    ps.set_state(conn, candidate_id, state, now=now, gate_ref=gate_ref)
    ps.log_event(conn, "promote", candidate_id=candidate_id,
                 detail={"by": by, "why": why, "state": state}, now=now)
    return state


def approve(conn: psycopg.Connection, candidate_id: str, *, by: str, now: datetime) -> None:
    """The user's explicit approval: a patch under review (built, or awaiting_approval after a
    gate) → queued; its candidate → promoted. The supervisor lands it at its next tick. Refused
    (`PromoteRefused`) when the patch is not under review or patches are switched off."""
    row = ps.get_patch(conn, candidate_id)
    if row is None:
        raise ForgeNotFound(f"no patch candidate {candidate_id!r}")
    if row.state not in _UNDER_REVIEW:
        raise PromoteRefused("这项改动不在等你审阅")
    if not switch.enabled(conn, row.user_id, "patch"):
        raise PatchesOff("代码修改已关闭")
    store.set_status(conn, candidate_id, "promoted", now)
    ps.set_state(conn, candidate_id, "queued", now=now)
    ps.log_event(conn, "approve", candidate_id=candidate_id,
                 detail={"by": by, "from": row.state}, now=now)


def discard(conn: psycopg.Connection, candidate_id: str, *, by: str, now: datetime) -> None:
    """The user does not take a patch under review: it → withdrawn (reason `DISCARDED`), its
    candidate → withdrawn. Its branch stays in the archive. Refused once approved or landed."""
    row = ps.get_patch(conn, candidate_id)
    if row is None:
        raise ForgeNotFound(f"no patch candidate {candidate_id!r}")
    if row.state not in _UNDER_REVIEW:
        raise PromoteRefused("这项改动不在等你审阅")
    store.set_status(conn, candidate_id, "withdrawn", now)
    ps.set_state(conn, candidate_id, "withdrawn", now=now, reason=DISCARDED)
    ps.log_event(conn, "discard", candidate_id=candidate_id,
                 detail={"by": by, "from": row.state}, now=now)


def revert_patch(conn: psycopg.Connection, user_id: str, candidate_id: str, *, by: str,
                 now: datetime) -> None:
    """landed → revert_queued. Every user sees every patch row, so any user may undo it; the
    supervisor executes the revert at the next idle point."""
    ids.check_user(user_id)
    row = ps.get_patch(conn, candidate_id)
    if row is None:
        raise ForgeNotFound(f"no patch candidate {candidate_id!r}")
    if row.state != "landed":
        raise ValueError("这次改动已经撤销过了")
    ps.set_state(conn, candidate_id, "revert_queued", now=now)
    ps.log_event(conn, "revert_requested", candidate_id=candidate_id,
                 detail={"by": by, "user": user_id}, now=now)


def revert_all(conn: psycopg.Connection, *, by: str, now: datetime) -> int:
    """Ask the supervisor to revert every landed forge commit. Returns how many rows are landed;
    no request is written when there are none."""
    n = len(ps.patches(conn, states=["landed"], limit=100000))
    if n:
        ps.set_live_request(conn, "revert_all", by=by, now=now)
    return n
