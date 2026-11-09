"""Forged tools in the change list (track H, design 2026-10-22 §6.1, §6.3). `changes` gives the
rows the harness change list shows; `undo` rolls one back. Text is plain Chinese for the learner:
no internal words.

A global tool's row shows in every user's list, but one learner must not remove it for everyone:
their undo masks it for them only (`forge_setting (user, "mask.<name>") = candidate id`); only the
operator (env `EVOWIKI_FORGE_OPERATOR` equal to the acting user id) rolls a global change back."""

import json
import os
import re
from datetime import UTC, datetime
from typing import Any

import psycopg

from evowiki.forge import ids, registry
from evowiki.store import forge as store


def _first_sentence(text: str) -> str:
    m = re.match(r"\s*(.+?)(?:[。！？!?]|\.(?:\s|$)|\n|$)", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def _description(files: dict[str, str]) -> str:
    try:
        return _first_sentence(str(json.loads(files.get("schema.json", ""))["description"]))
    except (ValueError, KeyError, TypeError):
        return ""


def _title(cand: store.CandidateRow | None) -> str:
    """The requester's short title; phase-1 rows without one fall back to the tool name."""
    if cand is None:
        return "新工具"
    spec = cand.spec if isinstance(cand.spec, dict) else {}
    return str(spec.get("title") or cand.name or "新工具")


def changes(conn: psycopg.Connection, user_id: str) -> list[dict[str, Any]]:
    """Promote changes of the user's scope and the global one, newest first."""
    ids.check_user(user_id)
    hidden = registry.masks(conn, user_id)
    rows = []
    for scope in (user_id, registry.GLOBAL):
        history = store.registry_history(conn, scope, limit=1000)
        undone = registry.undone_ids(history)
        rows += [(scope, r, registry.change_id(scope, r.version) in undone)
                 for r in history if r.change.get("op") == "promote"]
    rows.sort(key=lambda t: (t[1].created_at, t[1].version), reverse=True)
    out: list[dict[str, Any]] = []
    for scope, r, was_undone in rows:
        cand = store.get_candidate(conn, str(r.change.get("candidate_id")))
        title = _title(cand)
        desc = _description(cand.files) if cand else ""
        note = ""
        if cand and isinstance(cand.evidence, dict):
            note = str(cand.evidence.get("note") or "")
        cid, name = registry.promoted_name(r)
        masked = (scope == registry.GLOBAL and not was_undone and name is not None
                  and hidden.get(name) == cid)
        why = "；".join(x for x in (desc, note or str(r.change.get("why") or ""),
                                   "已为你撤下" if masked else "") if x)
        out.append({
            "change_id": registry.change_id(scope, r.version),
            "text": f"新增了工具「{title}」",
            "why": why,
            "cost_usd": cand.cost_usd if cand else None,
            "can_undo": not (was_undone or masked),
            "undone": was_undone or masked,
            "at": r.created_at,
        })
    return out


def undo(conn: psycopg.Connection, user_id: str, change_id: str, *, by: str = "user",
         operator: bool | None = None) -> int | None:
    """Undo a change; unknown ids raise `ForgeNotFound`. A change of the user's own scope is rolled
    back (`registry.rollback`). A global change is rolled back for everyone only when `operator`
    (None → env `EVOWIKI_FORGE_OPERATOR` equals `user_id`); otherwise it is masked for this user
    only. Returns the new registry version, or None when no version was written (already undone,
    or masked for this user)."""
    ids.check_user(user_id)
    if operator is None:
        operator = os.environ.get("EVOWIKI_FORGE_OPERATOR") == user_id
    scope, row, history = registry.promote_row(conn, user_id, change_id)
    if scope != registry.GLOBAL or operator:
        return registry.rollback(conn, user_id, change_id, by=by, operator=operator)
    cid, name = registry.promoted_name(row)
    if change_id in registry.undone_ids(history) or name is None:
        return None
    store.set_setting(conn, user_id, f"{registry.MASK}{name}", cid, datetime.now(UTC))
    return None


# --- L2 patches: rows `forgep:<candidate_id>` (plan P3 T12; review mode, ruling 2026-10-23) ------
PATCH_PREFIX = "forgep:"
# patch state -> (row status, line prefix). Every built patch is listed: under review it waits for
# the user (approve / discard), approved it waits for the supervisor, landed it can be undone.
_PATCH_ROWS: dict[str, tuple[str, str]] = {
    "built": ("awaiting_user", "等你审阅的代码改动"),
    "awaiting_approval": ("awaiting_user", "等你审阅的代码改动"),
    "queued": ("personal_accepted", "已批准、还没生效的代码改动"),
    "landed": ("personal_accepted", "修改了代码"),
    "revert_queued": ("rolled_back", "修改了代码"),
    "reverted": ("rolled_back", "修改了代码"),
    "canary_failed": ("rolled_back", "修改了代码"),
    "withdrawn": ("rolled_back", "修改了代码"),
}
# a patch the user discarded under review (withdrawn with reason DISCARDED): its own line
DISCARDED_LINE = "没采用的代码改动"


def patch_changes(conn: psycopg.Connection, user_id: str) -> list[dict[str, Any]]:
    """Every built patch, newest first. A patch is global in effect, so every user sees every row
    (and may decide or undo it). `status`: awaiting_user (built / awaiting_approval: the user
    approves or discards), personal_accepted (queued or landed), rejected (the user discarded it),
    rolled_back (reverted, withdrawn, canary failed; revert_queued too: the learner asked, the
    supervisor acts at the next idle). `can_undo` only for a landed patch."""
    from evowiki.forge.patch.land import DISCARDED
    from evowiki.store import forge_patch as ps
    ids.check_user(user_id)
    out: list[dict[str, Any]] = []
    for p in ps.patches(conn, states=list(_PATCH_ROWS), limit=1000):  # type: ignore[arg-type]
        cand = store.get_candidate(conn, p.candidate_id)
        spec = cand.spec if cand is not None and isinstance(cand.spec, dict) else {}
        ev = cand.evidence if cand is not None and isinstance(cand.evidence, dict) else {}
        why = "；".join(x for x in (_first_sentence(str(spec.get("goal") or "")),
                                   str(ev.get("note") or ""), p.reason) if x)
        status, line = _PATCH_ROWS[p.state]
        if p.state == "withdrawn" and p.reason == DISCARDED:
            status, line = "rejected", DISCARDED_LINE
        out.append({
            "change_id": f"{PATCH_PREFIX}{p.candidate_id}",
            "text": f"{line}：{p.title}",
            "why": why,
            "cost_usd": p.cost_usd,
            "status": status,
            "can_undo": p.state == "landed",
            "undone": status == "rolled_back",
            "at": p.updated_at,
        })
    out.sort(key=lambda r: r["at"], reverse=True)
    return out


def _patch_id(change_id: str) -> str:
    from evowiki.forge.models import ForgeNotFound
    if not change_id.startswith(PATCH_PREFIX):
        raise ForgeNotFound(f"no such change {change_id!r}")
    return change_id[len(PATCH_PREFIX):]


def undo_patch(conn: psycopg.Connection, user_id: str, change_id: str, *, by: str = "user",
               now: datetime | None = None) -> None:
    """Queue the revert of a landed patch (`land.revert_patch`)."""
    from evowiki.forge.patch import land
    land.revert_patch(conn, user_id, _patch_id(change_id), by=by, now=now or datetime.now(UTC))


def decide_patch(conn: psycopg.Connection, user_id: str, change_id: str, *, approve: bool,
                 now: datetime | None = None) -> None:
    """The user's review of a patch: `land.approve` or `land.discard`."""
    from evowiki.forge.patch import land
    ids.check_user(user_id)
    fn = land.approve if approve else land.discard
    fn(conn, _patch_id(change_id), by=user_id, now=now or datetime.now(UTC))
