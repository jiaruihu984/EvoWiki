"""The teaching-spec registry (policy track "B" = θ_rep,u versions) as a Protocol, and an in-memory
version for tests and walkthrough scripts. The harness loop reads and commits through it
(`harness.round`, `harness.change_source`, `harness.fast_check`, `harness.observe`); the real one is
`wikiapp.spec_store.RepoSpecRegistry` over R1's teaching-spec log."""

from collections import defaultdict
from datetime import datetime
from typing import Protocol

from evowiki.teach.contracts import PolicyVersion, TeachingSpec
from evowiki.teach.rule_authority import settle_items


class SpecRegistry(Protocol):
    def current(self, user_id: str) -> PolicyVersion: ...

    def get(self, user_id: str, version: int) -> PolicyVersion: ...

    def commit(self, user_id: str, spec: TeachingSpec, *, patch_id: str | None,
               at: datetime, undo: bool = False, actor: str = "rsi") -> PolicyVersion:
        """Save `spec` (derived from version `spec.version`) as the next version; raises when the
        stored spec moved past `spec.version`. History is never rewritten: a rollback (`undo`) is a
        new version too. `actor`: who makes the change (rule provenance contract §1.3; "rsi" unless
        it is the user's own undo) — `rules.authority.RuleAuthorityError` when it may not."""
        ...


class MemoryRegistry:
    """In-memory `SpecRegistry` (not for production: nothing persists)."""

    def __init__(self) -> None:
        self.rows: dict[str, list[PolicyVersion]] = defaultdict(list)

    def seed(self, user_id: str, spec: TeachingSpec, at: datetime) -> PolicyVersion:
        return self.commit(user_id, spec, patch_id=None, at=at)

    def current(self, user_id: str) -> PolicyVersion:
        return self.rows[user_id][-1]

    def get(self, user_id: str, version: int) -> PolicyVersion:
        return next(r for r in self.rows[user_id] if r.version == version)

    def commit(self, user_id: str, spec: TeachingSpec, *, patch_id: str | None,
               at: datetime, undo: bool = False, actor: str = "rsi") -> PolicyVersion:
        """The same checkpoint as the wiki's (`rules.authority.check`, no decisions here); the
        first row (the seed) is not checked."""
        if self.rows[user_id]:
            prev = self.rows[user_id][-1].spec
            assert prev is not None
            spec = settle_items(prev, spec, actor=actor, undo=undo)[0]  # type: ignore[arg-type]
        v = len(self.rows[user_id]) + 1
        row = PolicyVersion(track="B", version=v, at=at, patch_id=patch_id,
                            spec=spec.model_copy(update={"version": v}))
        self.rows[user_id].append(row)
        return row
