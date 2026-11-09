"""The teaching spec, signals and section logs over the real stores, for the harness loop
(`service.rsi_runs._harness`, `harness.cases`, `service.deps.harness_change_sources`):

| reader | implementation |
|---|---|
| signals | R1 `wikiapp.signals.all_signals` (wiki log + assistant selections), filtered by `since` |
| section logs | R1 `section_logs_saved` events (full history, not only the latest per section) |
| `teach.spec_registry.SpecRegistry` | R1 teaching spec: `teachspec.effective_spec` (virtual v1 when nothing is stored), old versions via `teach.read_version`, writes via `teach.commit_spec` with `base_version` |

θ_rep,u versions live in R1's spec log (the single write path), so policy track B has no rows in
R2's `rsi_policy_version`."""

from collections.abc import Collection, Sequence
from datetime import UTC, datetime

from evowiki.assistant.session import SessionStore
from evowiki.rules.authority import Actor
from evowiki.teach.contracts import PolicyVersion, SectionLog, Signal, TeachingSpec
from evowiki.wikiapp.events import SectionLogsSaved
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Repo, Snapshot
from evowiki.wikiapp.signals import all_signals
from evowiki.wikiapp.teach import commit_spec, read_version
from evowiki.wikiapp.teachspec import effective_spec

LOGS_EVENT = SectionLogsSaved.model_fields["type"].default
VIRTUAL_AT = datetime(1970, 1, 1, tzinfo=UTC)


class RepoSignals:
    def __init__(self, repo: Repo, sessions: SessionStore | None = None):
        self.repo, self.sessions = repo, sessions

    def signals(self, user_id: str, *, since: datetime | None = None) -> Sequence[Signal]:
        out = all_signals(self.repo, user_id, sessions=self.sessions)
        return [s for s in out if since is None or s.at >= since]


class RepoSectionLogs:
    def __init__(self, repo: Repo):
        self.repo = repo

    def section_logs(self, user_id: str, page_ids: Collection[str]) -> Sequence[SectionLog]:
        return [lg for ev in self.repo.wiki_events_of(user_id, (LOGS_EVENT,))
                if isinstance(ev.body, SectionLogsSaved)
                for lg in ev.body.logs if lg.page_id in page_ids]


class RepoSpecRegistry:
    """Policy track "B" (θ_rep,u) = R1's teaching-spec versions (`teach.spec_registry.SpecRegistry`). `commit` takes a spec derived
    from the version it was read at (`spec.version`) and saves it as the next version; if the spec
    moved meanwhile, R1 raises WikiConflict (nothing is written)."""

    def __init__(self, repo: Repo, new_id: Ids):
        self.repo, self.new_id = repo, new_id

    def current(self, user_id: str) -> PolicyVersion:
        snap = self.repo.load(user_id)
        return self._row(snap, effective_spec(snap.state))

    def get(self, user_id: str, version: int) -> PolicyVersion:
        snap = self.repo.load(user_id)
        return self._row(snap, read_version(self.repo, snap, version))

    def commit(self, user_id: str, spec: TeachingSpec, *, patch_id: str | None,
               at: datetime, undo: bool = False, actor: Actor = "rsi") -> PolicyVersion:
        """`actor` "rsi" (the harness loop's changes and rollbacks) unless it is the user's own undo: the whole
        spec is checked item by item against the current one (`commit_spec`)."""
        snap = self.repo.load(user_id)
        out = commit_spec(self.repo, snap,
                          lambda base: spec.model_copy(update={"version": base.version + 1}),
                          actor=actor, undo=undo, action="rsi_undo" if undo else "rsi",
                          origin=patch_id,
                          now=at,
                          new_id=self.new_id, base_version=spec.version)
        return self._row(out, effective_spec(out.state), patch_id=patch_id)

    @staticmethod
    def _row(snap: Snapshot, spec: TeachingSpec, patch_id: str | None = None) -> PolicyVersion:
        meta = next((m for m in snap.state.spec_versions if m.version == spec.version), None)
        at = meta.at if meta is not None else VIRTUAL_AT         # the never-saved virtual v1
        return PolicyVersion(track="B", version=spec.version, at=at,
                             patch_id=patch_id if patch_id else (meta.origin if meta else None),
                             spec=spec)
