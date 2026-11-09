"""`Repo` for page-writing workflows (spec 2026-10-14 §4, B1b): inside a durable workflow every read
and every commit is a checkpointed step, outside one every method is a plain delegate.

A `Snapshot` is not JSON, so a step records only the three numbers that identify it
(`wiki_seq`, `aspect_seq`, `infer_version`). The event logs are append-only, so on replay the body
gets the identical snapshot back from `inner.load_at(...)`; when the step just ran, the live
snapshot is reused (no second read). `commit` hands `once=<step key>` to the inner repo, so a step
that is retried after the commit landed returns what it recorded instead of writing twice or
raising a false `WikiConflict` (the exactly-once effect, `agent.effects`). Event lists are
returned as JSON and re-validated in the body."""

from collections.abc import Collection

from pydantic import TypeAdapter

from evowiki.agent.durable import in_workflow, step, step_key, step_ran
from evowiki.belief.aspects import AspectEvent, AspectEventAdapter
from evowiki.wikiapp.events import WikiEvent
from evowiki.wikiapp.repo import Commit, Repo, Snapshot

_ASPECTS: TypeAdapter = AspectEventAdapter


def _seqs(s: Snapshot) -> list[int]:
    return [s.wiki_seq, s.aspect_seq, s.infer_version]


class DurableRepo:
    def __init__(self, inner: Repo) -> None:
        self.inner = inner

    def _snapshot(self, user_id: str, name: str, make) -> Snapshot:
        live: list[Snapshot] = []

        def run() -> list[int]:
            live.append(make())
            return _seqs(live[0])

        seqs, ran = step_ran(name, run)
        if ran and live:
            return live[0]
        return self.inner.load_at(user_id, *seqs)

    def load(self, user_id: str) -> Snapshot:
        if not in_workflow():
            return self.inner.load(user_id)
        return self._snapshot(user_id, "repo.load", lambda: self.inner.load(user_id))

    def load_at(self, user_id: str, wiki_seq: int, aspect_seq: int,
                infer_version: int) -> Snapshot:
        return self.inner.load_at(user_id, wiki_seq, aspect_seq, infer_version)

    def commit(self, snap: Snapshot, c: Commit, *, once: str | None = None) -> Snapshot:
        if not in_workflow():
            return self.inner.commit(snap, c, once=once)
        return self._snapshot(snap.user_id, "repo.commit",
                              lambda: self.inner.commit(snap, c, once=step_key()))

    def aspect_events(self, user_id: str) -> list[AspectEvent]:
        if not in_workflow():
            return self.inner.aspect_events(user_id)
        raw = step("repo.aspect_events", lambda: [
            _ASPECTS.dump_python(e, mode="json") for e in self.inner.aspect_events(user_id)])
        return [_ASPECTS.validate_python(r) for r in raw]

    def wiki_events_of(self, user_id: str, types: Collection[str]) -> list[WikiEvent]:
        if not in_workflow():
            return self.inner.wiki_events_of(user_id, types)
        raw = step("repo.wiki_events_of", lambda: [
            e.model_dump(mode="json") for e in self.inner.wiki_events_of(user_id, types)])
        return [WikiEvent.model_validate(r) for r in raw]

    def wiki_events_since(self, user_id: str, types: Collection[str],
                          after_seq: int = 0) -> list[tuple[int, WikiEvent]]:
        if not in_workflow():
            return self.inner.wiki_events_since(user_id, types, after_seq)
        raw = step("repo.wiki_events_since", lambda: [
            [seq, e.model_dump(mode="json")]
            for seq, e in self.inner.wiki_events_since(user_id, types, after_seq)])
        return [(seq, WikiEvent.model_validate(r)) for seq, r in raw]
