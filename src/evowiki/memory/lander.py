"""Where a consolidated preference / fact about the learner lands (spec §2.3 point 2): the
teaching spec or the profile, owned by S3, never this package.

`S3PreferenceLander`: a preference (kind "teaching") or a fact about him (kind "about_me") goes to
S3's `land_preference` (`wikiapp/preferences.py`, S3 plan T6; the function is injected, so this
module never imports it) with provenance explicit_history: S3 makes it pending (a draft he
confirms), keyed by the candidate key so a retry finds the same pending draft. Deferred keeps the
candidate (the consolidation entry's `pending_landing`) for `retry_pending_landings`.

`AutoRuleGuard`: the rule contract's `rules.guard.check_auto_rule` (actor system) over the user's
wiki state; block → "blocked_rule", needs-confirm → "needs_confirm" (consolidation never asks: the
candidate is dropped); a restatement of one of his own or pending rules (the judge's "same") →
`Same`: the candidate lands as that rule (`same_as`), so one preference keeps one card. The Null ports are for tests and for wiring without a wiki."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from evowiki.memory.contracts import EvidenceRef, MemoryScope, Provenance

if TYPE_CHECKING:
    from evowiki.memory.consolidate import Candidate
    from evowiki.teach.rule_judge import RuleJudge
    from evowiki.wikiapp.repo import Repo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Landed:
    spec_item_id: str | None = None
    profile_ref: dict[str, Any] | None = None
    pending_action_id: str | None = None      # waiting for his confirmation in 参数


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class Deferred:
    reason: str


@dataclass(frozen=True)
class Same:
    """The guard's judge found the candidate restates this spec item."""

    item_id: str


class PreferenceLander(Protocol):
    def land(self, user_id: str, candidate: "Candidate", *, session_id: str,
             provenance: Provenance = "explicit_history",
             idempotency_key: str, same_as: str | None = None,
             page_id: str | None = None) -> Landed | Refused | Deferred:
        """The same `idempotency_key` again (a retry) must find what the first call made (the
        same pending action), never make a second one. `same_as`: the spec item the guard found
        the candidate restates (S3 returns that item instead of a new one). `page_id`: the
        session's page (a page scope the model gave without an id)."""
        ...


class NullPreferenceLander:
    """S3's `land_preference` is not wired yet: every candidate waits for a retry."""

    def land(self, user_id, candidate, *, session_id, provenance="explicit_history",
             idempotency_key, same_as=None, page_id=None) -> Deferred:
        return Deferred("waiting_for_s3")


def evidence_refs(candidate: "Candidate", session_id: str) -> tuple[EvidenceRef, ...]:
    return tuple(EvidenceRef(message_id=e.message_id, session_id=e.session_id or session_id,
                             role=e.role, quote=e.quote if e.role == "user" else None)
                 for e in candidate.evidence)


class S3Landed(Protocol):
    """What S3's `land_preference` returns (S3 plan, 「land_preference: S3 owes this signature」)."""

    status: Literal["active", "pending", "refused"]
    item_id: str | None                 # active: spec item id / profile line key
    pending_action_id: str | None       # pending: the ledger draft row (its card)
    reason: str | None


class LandPreference(Protocol):
    """S3's `land_preference(repo, snap, *, kind, text, provenance, evidence, scope, section,
    replaces, pending, same_as, history_policy, session_id, now, new_id, commit_extra,
    idempotency_key)`;
    the keywords below are the ones this lander passes."""

    def __call__(self, repo: Any, snap: Any, *, kind: Literal["teaching", "about_me"],
                 text: str, provenance: Provenance, evidence: Sequence[EvidenceRef],
                 scope: MemoryScope, session_id: str | None, idempotency_key: str,
                 same_as: str | None, now: datetime,
                 new_id: Callable[[str], str]) -> S3Landed: ...


KINDS: dict[str, Literal["teaching", "about_me"]] = {"preference": "teaching",
                                                     "about_me": "about_me"}


class S3PreferenceLander:
    """S3's `land_preference` over the wiki repo, the snapshot read fresh per call. pending →
    Landed(pending_action_id; a preference also its pending spec item as `spec_item_id`); active (S3 adopted it: not expected for explicit_history) →
    Landed with the item as `spec_item_id` (teaching) or `profile_ref` {"key"} (about_me);
    refused → Refused(reason). Any other failure (no wiki yet, a write conflict, the database) →
    Deferred: kept and tried again, never lost or forced."""

    def __init__(self, land: LandPreference, repo: "Repo", clock: Callable[[], datetime],
                 new_id: Callable[[str], str]):
        self._land, self._repo, self._clock, self._new_id = land, repo, clock, new_id

    def land(self, user_id, candidate, *, session_id, provenance="explicit_history",
             idempotency_key, same_as=None, page_id=None) -> Landed | Refused | Deferred:
        from evowiki.memory.consolidate import candidate_scope

        scope = candidate_scope(candidate, page_id)
        if scope is None:
            return Refused("bad_scope")
        try:
            r = self._land(self._repo, self._repo.load(user_id), kind=KINDS[candidate.kind],
                           text=candidate.content, provenance=provenance,
                           evidence=evidence_refs(candidate, session_id), scope=scope,
                           session_id=None,              # the card goes to his next chat
                           idempotency_key=idempotency_key, same_as=same_as,
                           now=self._clock(),
                           new_id=self._new_id)
        except Exception:  # broad on purpose: the candidate waits, it is never lost or forced
            log.warning("landing preference %s for %s failed", idempotency_key, user_id,
                        exc_info=True)
            return Deferred("land_failed")
        if r.status == "refused":
            return Refused(r.reason or "refused")
        if r.status == "pending":         # a teaching item is already in the spec, pending
            return Landed(spec_item_id=r.item_id if candidate.kind == "preference" else None,
                          pending_action_id=r.pending_action_id)
        if candidate.kind == "preference":
            return Landed(spec_item_id=r.item_id)
        return Landed(profile_ref={"key": r.item_id})


class RuleGuard(Protocol):
    def check(self, user_id: str, text: str, scope: MemoryScope) -> str | Same | None:
        """None: the text may go on; `Same`: it may go on as that existing rule; else why it is
        dropped ("blocked_rule" / "needs_confirm")."""
        ...


class NullRuleGuard:
    """Nothing is blocked (tests; no wiki to check against)."""

    def check(self, user_id, text, scope) -> str | Same | None:
        return None


class AutoRuleGuard:
    """`rules.guard.check_auto_rule(actor="system")` over the user's current wiki state (a rule's
    scope is "any": the spec has no page / concept scope). `judge(user_id)`: the semantic judge
    metered to that user (None → anything close is needs_confirm). A user without a wiki has no
    rules: nothing to compare, the text goes on. Any other failure propagates (the caller's run
    fails and is tried again; never a silent pass)."""

    def __init__(self, repo: "Repo", judge: Callable[[str], "RuleJudge | None"]):
        self._repo, self._judge = repo, judge

    def check(self, user_id, text, scope) -> str | Same | None:
        from evowiki.rules.guard import check_auto_rule

        try:
            state = self._repo.load(user_id).state
        except LookupError:
            return None
        r = check_auto_rule(state, text, "any", None, actor="system", judge=self._judge(user_id))
        if r.outcome == "block":
            return "blocked_rule"
        if r.outcome == "confirm":
            return "needs_confirm"
        return Same(r.same_as[0]) if r.same_as else None
