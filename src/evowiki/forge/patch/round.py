"""One patch round (plan P3 T12): propose → gate → promote_patch. Until harness 3b wires a real
gate, `NoPatchGate` passes nothing, so every built patch stays `built` (fail closed)."""

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

import psycopg

from evowiki.forge.models import Evidence, ForgeResult, PatchRequest
from evowiki.forge.patch import land

if TYPE_CHECKING:
    from evowiki.forge.api import PatchDeps


class PatchGate(Protocol):
    def evaluate(self, conn: psycopg.Connection, candidate_id: str, user_id: str) -> str | None:
        """A gate record id when the patch passed, else None."""
        ...

    def ok(self, gate_ref: str) -> bool: ...


class NoPatchGate:
    """Nothing passes: built patches wait in the archive."""

    def evaluate(self, conn: psycopg.Connection, candidate_id: str, user_id: str) -> str | None:
        return None

    def ok(self, gate_ref: str) -> bool:
        return False


def run_patch(request: PatchRequest, evidence: Evidence, *, user_id: str, deps: "PatchDeps",
              gate: PatchGate, now: datetime,
              propose_fn: Callable[..., ForgeResult] | None = None) -> ForgeResult:
    """propose("patch") → for a built one gate.evaluate → promote_patch. Every candidate is
    archived by propose; an exception from it propagates. `propose_fn` is for tests."""
    if propose_fn is None:
        from evowiki.forge.api import propose as propose_fn
    result = propose_fn("patch", request, evidence, user_id=user_id, patch_deps=deps)
    if not result.build.ok:
        return result
    with deps.pool.connection() as conn:
        ref = gate.evaluate(conn, result.candidate_id, user_id)
        if ref is None or not gate.ok(ref):
            return result
        land.promote_patch(conn, result.candidate_id, gate_ref=ref, gate_ok=gate.ok,
                           by="round", why=evidence.note, now=now)
    return result
