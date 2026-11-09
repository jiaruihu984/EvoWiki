"""Wiki-app persistence with optimistic concurrency (D2), scoped per record. A flow loads a
`Snapshot` (wiki state, aspect K and both log seqs), calls the LLM outside any lock, then
`commit`s under the user's writer lock. Every wiki event writes some records (`scopes`: a page, a
path, a goal map, a draft, the ledger, the graph, ...). The commit conflicts (`WikiConflict`, 409,
nothing written) iff a wiki event that landed since the snapshot writes a record the commit's own
wiki events write, or one the flow declared it read (`Commit.depends`; `PAGE_LIST`: a page was
created since). `ANY` ("*", e.g.
`WikiStarted`) overlaps every non-empty scope set. `BENIGN_WIKI_EVENTS` (a GET recording
`page_opened`, a `usage_added`, a `signal_recorded`, `source_matched`) write no record, so they
never conflict, and a commit whose own events are all benign never conflicts either. Aspect (K)
events are appended evidence: K events that landed since the snapshot conflict only with a commit
that was computed on K - one that writes wiki records together with its own K events (a K-change
pass: feedback, 「这部分我懂」, a quiz's page patch) or that declared `K_SCOPE` in `depends` (e.g. a
fold computed from K bands); a pure K append (an observation) or a wiki write without K events
(a page written, 学完, 下一页) goes on top. On no conflict the commit appends after everything that landed; the returned state replays the wiki
events in between and the returned K is built from the whole aspect log. Aspect and wiki events of
one commit land together or not at all.

K is built with the user's θ_infer,u (RSI track A, `evowiki.teach`): `ASPECT_CONFIG_V1` with the
current track-A version's infer. A user with no version gets `ASPECT_CONFIG_V1` itself, so K is
exactly what it was before RSI. PgRepo reads the version in the same transaction as the logs.

K inference follows only the node graph's verified prerequisites (relation layers, user ruling
2026-10-14): every K this module builds — a load, and the K a commit returns — is replayed with
`trusted = state.graph.trusted_pairs()` of the wiki state it goes with (`inference_trust`). K keeps
every stored prereq (it cannot drop one); the filter is applied at replay, never written. An
unverified prerequisite (from intake, a split, a merge, the legacy conversion) therefore feeds no
inference; a user without graph nodes has no trusted pair at all."""

import dataclasses
import functools
import hashlib
import inspect
import json
import logging
import threading
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg_pool import ConnectionPool

from evowiki.agent.effects import EffectConflict, apply_once, payload_hash
from evowiki.belief.aspects import (
    AspectConfig,
    AspectEvent,
    AspectK,
    AspectKView,
    TrustedPrereqs,
)
from evowiki.runtime.aspect_beliefs import load_aspects, record_aspect_events, replay_aspects
from evowiki.store.artifacts import MemoryArtifacts, current_run_id, put_lineage
from evowiki.store.aspects import AspectEventConflict, last_aspect_seq
from evowiki.store.aspects import aspect_events as stored_aspect_events
from evowiki.store.locks import user_writer
from evowiki.store.rsi import current_infer, current_infer_version, infer_at
from evowiki.store.users import UnknownUser, get_user
from evowiki.store.wiki import (
    WikiEventConflict,
    append_wiki_events,
    last_wiki_seq,
    latest_graph_action,
    put_wiki_snapshot,
    wiki_events,
    wiki_events_after,
    wiki_events_of_type,
    wiki_snapshot,
)
from evowiki.teach.contracts import InferParams
from evowiki.wikiapp.events import (
    ConceptsNamed,
    DomainsGrouped,
    DraftClosed,
    DraftCreated,
    FeedbackApplied,
    FeedbackUndone,
    FollowupResolved,
    FollowupScheduled,
    GoalMapSaved,
    GoalSettingsSet,
    GraphSaved,
    GroupsNamed,
    GuidelineAdded,
    GuidelineProposalClosed,
    GuidelineProposalRejected,
    GuidelineProposed,
    GuidelineRemoved,
    LanguageSet,
    PageFitSaved,
    PageMarkedStale,
    PageRewriteFailed,
    PageSaved,
    PagesStaleSet,
    PagesTrashed,
    PathSaved,
    ProbeAnswered,
    ReadingSaved,
    RefineSettingsSet,
    ReviewCardHidden,
    ReviewCardRestored,
    ReviewFirstAnswered,
    ReviewPostponed,
    ReviewSettingsSet,
    RoughMarked,
    RuleDecided,
    SectionLogsSaved,
    SeriesUpdated,
    TeachingSpecSaved,
    WikiEvent,
    WikiStarted,
)
from evowiki.wikiapp.lineage import commit_lineage
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from evowiki.wikiapp.state import ASPECT_CONFIG_VERSION, WikiState, apply_event, replay

log = logging.getLogger(__name__)
CONFLICT_DETAIL = "数据刚被另一个请求改过，请重试"


class WikiConflict(RuntimeError):
    """The user's logs moved since the snapshot was loaded (409; the client retries)."""

    def __init__(self, message: str = CONFLICT_DETAIL, *, public_message: str | None = None,
                 code: str = "wiki_conflict", params: dict[str, object] | None = None):
        super().__init__(message)
        self.public_message, self.error_code, self.params = public_message, code, params or {}


def k_config(infer: InferParams) -> AspectConfig:
    """The aspect config K is built with for a user on θ_infer,u = `infer`; the default infer is
    `ASPECT_CONFIG_V1` itself (identical K to before RSI)."""
    return ASPECT_CONFIG_V1 if infer == ASPECT_CONFIG_V1.infer else ASPECT_CONFIG_V1.with_infer(infer)


def inference_trust(state: WikiState) -> TrustedPrereqs:
    """The (prereq, concept) pairs K inference may follow for this wiki state: the graph's
    verified prerequisites (module doc)."""
    return state.graph.trusted_pairs()


class WikiInvalid(ValueError):
    """A wiki request the flow refuses (422), e.g. answers that do not fit the draft's questions.
    Its own type so only wiki routes map ValueError-like failures to 422."""

    def __init__(self, message: str = "", *, public_message: str | None = None,
                 code: str = "wiki_invalid", params: dict[str, object] | None = None):
        super().__init__(message)
        self.public_message, self.error_code, self.params = public_message, code, params or {}


@dataclass(frozen=True)
class Snapshot:
    """Read-only by convention: never mutate `k` (commit builds a new one)."""

    user_id: str
    state: WikiState
    k: AspectK
    wiki_seq: int
    aspect_seq: int
    infer_version: int = 1       # θ_infer,u version (R2 registry, track A) K was built with

    def kview(self, now: datetime) -> AspectKView:
        return AspectKView(self.k, now)


@dataclass
class Commit:
    """`extra`: writes that must land with these events or not at all (deep refine's assets).
    Each gets the commit's connection (None in MemoryRepo) after the events are validated. They
    ride on the events: a commit without events runs none of them. `depends`: scopes (`scopes`)
    of records the flow read to compute its events but does not write; a write to one of them
    since the snapshot conflicts like a write to its own records; `K_SCOPE` there: the events
    were computed on K, so K events landed since conflict too (module doc)."""

    aspect: list[AspectEvent] = field(default_factory=list)
    wiki: list[WikiEvent] = field(default_factory=list)
    extra: list[Callable[[Any], None]] = field(default_factory=list)
    depends: frozenset[str] = frozenset()

    def empty(self) -> bool:
        return not self.aspect and not self.wiki


def commit_payload(c: Commit) -> dict:
    """What an exactly-once commit is keyed on: the events. `extra` writers are not part of it
    (they are derived from the same draft as the events)."""
    return {"aspect": [e.model_dump(mode="json") for e in c.aspect],
            "wiki": [e.model_dump(mode="json") for e in c.wiki]}


# wiki events that do not change what an in-flight flow computed from: they write no record
# (`scopes` is empty), so they never conflict (M4)
BENIGN_WIKI_EVENTS = frozenset({"page_opened", "usage_added", "signal_recorded",
                                "source_matched"})

ANY = "*"      # the scope of an event that rewrites shared state: overlaps every scope
K_SCOPE = "k"  # in `Commit.depends`: the commit was computed on K (the aspect log must not move)
# in `Commit.depends`: the commit was computed on the set of pages (a curate): a page created since
# conflicts. Only a dependency: pages created concurrently do not conflict with each other
PAGE_LIST = "page_list"


def _page(pid: str) -> str:
    return f"page:{pid}"


def scopes(ev: WikiEvent) -> frozenset[str]:
    """The records `ev` writes, for the commit's conflict check (module doc). Whole-record
    snapshots (a page, a path, a map, the ledger, the graph, a setting) are one scope each: two
    writers of one would lose an update. Events that rewrite state across records, or that this
    table does not know, are `ANY` (fail closed)."""
    b = ev.body
    if b.type in BENIGN_WIKI_EVENTS:
        return frozenset()
    match b:
        case PageSaved():
            return frozenset({_page(b.record.page_id)})
        case PagesTrashed():
            return frozenset(_page(p) for p in b.page_ids)
        case SectionLogsSaved():          # written with the page_saved that produced them
            return frozenset(_page(g.page_id) for g in b.logs) or frozenset({ANY})
        case SeriesUpdated():             # merged onto the path's note at replay, in any order
            return frozenset({_page(b.page_id)})
        case FeedbackApplied():
            return frozenset({_page(b.page_id), f"feedback:{b.feedback_id}"})
        case FeedbackUndone():
            return frozenset({f"feedback:{b.feedback_id}"})
        case PageMarkedStale() | PageRewriteFailed():
            return frozenset({f"marks:{b.page_id}"})
        case PathSaved():
            return frozenset({f"path:{b.record.path.path_id}"})
        case GoalMapSaved():
            return frozenset({f"map:{b.record.map_id}"})
        case PageFitSaved():
            return frozenset({f"fit:{b.record.map_id}:{b.record.node_id}"})
        case ReadingSaved():
            return frozenset({f"reading:{b.record.reading_id}"})
        case GoalSettingsSet():
            return frozenset({f"goal:{b.settings.goal_id}"})
        case DraftCreated():
            return frozenset({f"draft:{b.draft.draft_id}"})
        case DraftClosed() | ProbeAnswered() | RoughMarked():
            return frozenset({f"draft:{b.draft_id}"})
        case ConceptsNamed():
            return frozenset(f"name:{n.concept_id}" for n in b.names) or frozenset({ANY})
        case DomainsGrouped():
            return frozenset({"domains"})
        case GroupsNamed():
            return frozenset(f"group:{g.key}" for g in b.names) or frozenset({ANY})
        case GraphSaved():
            return frozenset({"graph"})
        case LanguageSet():
            return frozenset({"language"})
        case ReviewSettingsSet():
            return frozenset({"review_settings"})
        case RefineSettingsSet():
            return frozenset({"refine_settings"})
        case TeachingSpecSaved():
            return frozenset({"spec"})
        case RuleDecided():               # in the spec's commit; the checkpoint reads them
            return frozenset({"spec"})
        case PagesStaleSet():
            return frozenset({"stale"})
        case (GuidelineAdded() | GuidelineRemoved() | GuidelineProposed()
              | GuidelineProposalClosed() | GuidelineProposalRejected()):
            return frozenset({"guidelines"})
        case ReviewFirstAnswered():
            return frozenset({f"review_first:{b.node_id}"})
        case ReviewCardHidden() | ReviewCardRestored():
            return frozenset({f"card:{b.card_id}"})
        case ReviewPostponed():
            return frozenset(f"postpone:{c}" for c in b.until)
        case FollowupScheduled():
            return frozenset({f"followup:{b.followup.followup_id}"})
        case FollowupResolved():
            return frozenset({f"followup:{b.followup_id}"})
        case _:     # WikiStarted, PageMarksCleared (every page of a job), anything new
            return frozenset({ANY})


def _overlap(landed: frozenset[str], own: frozenset[str]) -> bool:
    if not landed or not own:
        return False
    return ANY in landed or ANY in own or not landed.isdisjoint(own)


def _own_scopes(snap: Snapshot, c: Commit) -> frozenset[str]:
    """What the commit writes (plus what it declared it read); `ANY` when it starts the wiki."""
    own = frozenset().union(*(scopes(e) for e in c.wiki), c.depends)
    if not snap.state.started and not c.empty():
        own |= {ANY}
    return own


def _landed(state: WikiState, between: Sequence[WikiEvent]) -> frozenset[str]:
    """The scopes the events since the snapshot wrote, plus `PAGE_LIST` when one created a page
    the snapshot did not have."""
    out = frozenset().union(*(scopes(e) for e in between))
    if any(isinstance(e.body, PageSaved) and e.body.record.page_id not in state.pages
           for e in between):
        out |= {PAGE_LIST}
    return out


def _check(snap: Snapshot, c: Commit, between: Sequence[WikiEvent], k_moved: bool) -> None:
    """WikiConflict iff an event that landed since the snapshot writes a record the commit
    writes or depends on, or K moved under a commit computed on K (module doc)."""
    landed = _landed(snap.state, between)
    own = _own_scopes(snap, c)
    if _overlap(landed, own):
        raise WikiConflict()
    if k_moved and (K_SCOPE in c.depends
                    or (c.aspect and any(scopes(e) for e in c.wiki))):
        raise WikiConflict()


# Called after a PgRepo commit that saved a changed graph (`graph_saved`, any action but
# `relayout`: positions only, the same groups) with (user_id, the state after the commit), once
# the transaction is committed. The service sets it (`group_naming.register`: enqueue the naming
# of the groups that have no name); None (scripts, tests, replay): nothing. Guarded: a failing
# listener is logged, a commit that landed is never reported as failed.
GraphListener = Callable[[str, WikiState], None]
_graph_listener: GraphListener | None = None


def listen_graph(fn: GraphListener | None) -> None:
    global _graph_listener
    _graph_listener = fn


def _graph_changed(c: Commit) -> bool:
    return any(e.body.type == "graph_saved" and e.body.action != "relayout"  # type: ignore[union-attr]
               for e in c.wiki)


def _after_commit(out: "Snapshot", c: Commit) -> None:
    fn = _graph_listener
    if fn is None or not _graph_changed(c):
        return
    try:
        fn(out.user_id, out.state)
    except Exception:
        log.exception("the graph listener failed after a commit of %s", out.user_id)


class Repo(Protocol):
    def load(self, user_id: str) -> Snapshot: ...            # raises LookupError for unknown user

    def aspect_events(self, user_id: str) -> list[AspectEvent]: ...  # LookupError if unknown

    def wiki_events_of(self, user_id: str, types: Collection[str]) -> list[WikiEvent]: ...
    # the user's stored wiki events of these body types, in log order (LookupError if unknown);
    # e.g. every `teaching_spec_saved` to read back a full old teaching-spec version

    def wiki_events_since(self, user_id: str, types: Collection[str],
                          after_seq: int = 0) -> list[tuple[int, WikiEvent]]: ...
    # (seq, event) of the user's stored wiki events of these body types with seq > after_seq, in
    # seq order. seq is the commit order: a commit's events get seqs above every event committed
    # before it (LookupError if unknown); e.g. the signals read API pages by it

    def load_at(self, user_id: str, wiki_seq: int, aspect_seq: int,
                infer_version: int) -> Snapshot: ...
    # the snapshot as it was at those seqs (the logs are append-only, so this is exact)

    def commit(self, snap: Snapshot, c: Commit, *, once: str | None = None) -> Snapshot: ...


def commit_signal(repo: Repo, user_id: str, signal: WikiEvent) -> None:
    """Records a click whose action failed (LLM, budget, spec, commit conflict): the signal alone, in
    its own small commit on a fresh snapshot; a conflict there is dropped."""
    try:
        repo.commit(repo.load(user_id), Commit(wiki=[signal]))
    except WikiConflict:
        pass
    # with `once`, the commit is an effect keyed `once` (payload `commit_payload(c)`): an already
    # applied key returns the snapshot at the recorded seqs without the version check, the same
    # key with other events is `EffectConflict`.
    # WikiConflict iff a wiki event since the snapshot writes a record the commit writes or
    # depends on (`scopes`); everything else that landed is kept (the returned snapshot has it)


def _utc(ev: WikiEvent) -> WikiEvent:
    """Envelope ts in UTC, as it comes back from timestamptz, so the returned snapshot equals
    a later load byte for byte."""
    return ev if ev.ts.tzinfo is UTC else ev.model_copy(update={"ts": ev.ts.astimezone(UTC)})


def _wiki_batch(snap: Snapshot, c: Commit) -> list[WikiEvent]:
    """The commit's wiki events, with `WikiStarted` first on the user's first non-empty commit."""
    evs = [_utc(e) for e in c.wiki]
    if not snap.state.started and not c.empty():
        ts = c.wiki[0].ts if c.wiki else c.aspect[0].ts
        evs.insert(0, WikiEvent(event_id=f"wiki_started:{snap.user_id}",
                                ts=ts.astimezone(UTC),
                                body=WikiStarted(aspect_config=ASPECT_CONFIG_VERSION)))
    return evs


def _advance(state: WikiState, evs: Sequence[WikiEvent]) -> WikiState:
    for e in evs:
        state = apply_event(state, e)
    return state


def _row(ev: WikiEvent) -> tuple[str, datetime, str, dict]:
    return ev.event_id, ev.ts, ev.body.type, ev.body.model_dump(mode="json")


def _from_row(event_id: str, occurred_at: datetime, payload: dict) -> WikiEvent:
    return WikiEvent.model_validate({"event_id": event_id, "ts": occurred_at, "body": payload})


class MemoryRepo:
    """Lists per user; for unit tests and the replay comparison. Mirrors PgRepo: event ids are
    unique across users, an identical retry of a stored aspect event is skipped, and a batch is
    validated in full before anything is stored. `infer_for(user_id)`: the user's θ_infer,u
    (e.g. `registry.infer_for` over a MemoryRsiStore); None = the default for everyone."""

    def __init__(self, users: Iterable[str] = (),
                 infer_for: Callable[[str], InferParams] | None = None,
                 infer_version_for: Callable[[str], int] | None = None):
        self._infer_for = infer_for
        self._infer_version_for = infer_version_for
        self._aspect: dict[str, list[AspectEvent]] = {u: [] for u in users}
        self._wiki: dict[str, list[WikiEvent]] = {u: [] for u in self._aspect}
        self._aspect_ids: dict[str, tuple[str, AspectEvent]] = {}
        self._wiki_ids: set[str] = set()
        self._effects: dict[str, tuple[str, dict]] = {}
        self.artifacts = MemoryArtifacts()    # what the commits saved (lineage, 0091)

    @classmethod
    def of_logs(cls, user_id: str, wiki: Sequence[WikiEvent], aspect: Sequence[AspectEvent], *,
                infer: InferParams | None = None, infer_version: int = 1) -> "MemoryRepo":
        """A repo holding one user's logs exactly as given: events another repo already stored
        (e.g. a PgRepo user's logs up to a snapshot, for a DB-free harness replay), so nothing is
        re-validated and no `WikiStarted` is added. K is built with `infer` (None = the
        default) and loads report `infer_version`."""
        repo = cls([user_id], infer_for=None if infer is None else (lambda _u: infer),
                   infer_version_for=lambda _u: infer_version)
        repo._wiki[user_id].extend(wiki)
        repo._aspect[user_id].extend(aspect)
        repo._wiki_ids.update(e.event_id for e in wiki)
        repo._aspect_ids.update((e.event_id, (user_id, e)) for e in aspect)
        return repo

    def add_user(self, user_id: str) -> None:
        self._aspect.setdefault(user_id, [])
        self._wiki.setdefault(user_id, [])

    def _require(self, user_id: str) -> None:
        if user_id not in self._aspect:
            raise UnknownUser(f"unknown user {user_id}")

    def wiki_log(self, user_id: str) -> list[WikiEvent]:
        self._require(user_id)
        return list(self._wiki[user_id])

    def aspect_log(self, user_id: str) -> list[AspectEvent]:
        self._require(user_id)
        return list(self._aspect[user_id])

    aspect_events = aspect_log

    def wiki_events_of(self, user_id: str, types: Collection[str]) -> list[WikiEvent]:
        self._require(user_id)
        return [e for e in self._wiki[user_id] if e.body.type in types]

    def wiki_events_since(self, user_id: str, types: Collection[str],
                          after_seq: int = 0) -> list[tuple[int, WikiEvent]]:
        self._require(user_id)
        return [(seq, e) for seq, e in enumerate(self._wiki[user_id], start=1)
                if seq > after_seq and e.body.type in types]

    def latest_graph_action(self, user_id: str) -> str | None:
        self._require(user_id)
        last = next((e for e in reversed(self._wiki[user_id]) if e.body.type == "graph_saved"),
                    None)
        return None if last is None else last.body.action    # type: ignore[union-attr]

    def _config(self, user_id: str) -> AspectConfig:
        return ASPECT_CONFIG_V1 if self._infer_for is None else k_config(self._infer_for(user_id))

    def load(self, user_id: str) -> Snapshot:
        self._require(user_id)
        state = replay(self._wiki[user_id])
        return Snapshot(user_id, state,
                        replay_aspects(self._config(user_id), self._aspect[user_id],
                                       trusted=inference_trust(state)),
                        len(self._wiki[user_id]), len(self._aspect[user_id]),
                        self._infer_version(user_id))

    def head(self, user_id: str) -> tuple[int, int, int]:
        """(wiki seq, aspect seq, infer version) a `load` now would report, without replaying."""
        self._require(user_id)
        return len(self._wiki[user_id]), len(self._aspect[user_id]), self._infer_version(user_id)

    def load_at(self, user_id: str, wiki_seq: int, aspect_seq: int,
                infer_version: int) -> Snapshot:
        """Memory has no policy versions: any `infer_version` is accepted and stored."""
        self._require(user_id)
        state = replay(self._wiki[user_id][:wiki_seq])
        return Snapshot(user_id, state,
                        replay_aspects(self._config(user_id), self._aspect[user_id][:aspect_seq],
                                       trusted=inference_trust(state)),
                        wiki_seq, aspect_seq, infer_version)

    def _infer_version(self, user_id: str) -> int:
        return 1 if self._infer_version_for is None else self._infer_version_for(user_id)

    def commit(self, snap: Snapshot, c: Commit, *, once: str | None = None) -> Snapshot:
        uid = snap.user_id
        self._require(uid)
        if once is None:
            return self._commit(snap, c)
        h = payload_hash(commit_payload(c))
        done = self._effects.get(once)
        if done is not None:
            if done[0] != h:
                raise EffectConflict(f"idempotency key {once!r} was used for a different payload")
            return self.load_at(uid, **done[1])
        out = self._commit(snap, c)
        self._effects[once] = (h, {"wiki_seq": out.wiki_seq, "aspect_seq": out.aspect_seq,
                                   "infer_version": out.infer_version})
        return out

    def _commit(self, snap: Snapshot, c: Commit) -> Snapshot:
        uid = snap.user_id
        if len(self._wiki[uid]) < snap.wiki_seq or len(self._aspect[uid]) < snap.aspect_seq:
            raise WikiConflict()
        between = self._wiki[uid][snap.wiki_seq:]
        k_moved = len(self._aspect[uid]) != snap.aspect_seq
        _check(snap, c, between, k_moved)
        moved = bool(between) or k_moved
        if between:      # unrelated events landed since the snapshot: build on top of them
            snap = dataclasses.replace(snap, state=_advance(snap.state, between),
                                       wiki_seq=len(self._wiki[uid]))
        if c.empty():
            if not moved:
                return snap
            return dataclasses.replace(        # K of the whole aspect log, for the new state
                snap, k=replay_aspects(self._config(uid), self._aspect[uid],
                                       trusted=inference_trust(snap.state)),
                aspect_seq=len(self._aspect[uid]), infer_version=self._infer_version(uid))
        evs = _wiki_batch(snap, c)
        state = _advance(snap.state, evs)        # K below is built for the state after the commit
        # aspect: the rules of record_aspect_events (validate all, then append)
        k = replay_aspects(self._config(uid), self._aspect[uid], trusted=inference_trust(state))
        fresh: list[AspectEvent] = []
        for ev in c.aspect:
            old = self._aspect_ids.get(ev.event_id)
            if old is not None:
                if old != (uid, ev):
                    raise AspectEventConflict(
                        f"aspect event {ev.event_id} reused with different content")
                continue
            k.apply(ev)
            fresh.append(ev)
        # wiki: the rules of append_wiki_events
        ids = [e.event_id for e in evs]
        if len(set(ids)) != len(ids):
            raise WikiEventConflict("wiki event ids repeated within one batch")
        if taken := sorted(set(ids) & self._wiki_ids):
            raise WikiEventConflict(f"wiki event ids already stored: {taken}")
        lineage = commit_lineage(uid, snap.state, evs, run_id=current_run_id())
        for write in c.extra:
            write(None)
        self.artifacts.put(lineage)
        for ev in fresh:
            self._aspect[uid].append(ev)
            self._aspect_ids[ev.event_id] = (uid, ev)
        self._wiki[uid].extend(evs)
        self._wiki_ids.update(ids)
        return Snapshot(uid, state, k, len(self._wiki[uid]), len(self._aspect[uid]),
                        self._infer_version(uid))


@functools.cache
def reducer_fingerprint() -> str:
    """What a cached wiki state was built by: the state's schema and the reducer's source. A
    snapshot stored by other code (a field added, an event applied differently) is not used."""
    from evowiki.wikiapp import state as state_module
    src = json.dumps(WikiState.model_json_schema(), sort_keys=True) + inspect.getsource(state_module)
    return hashlib.sha256(src.encode()).hexdigest()[:16]


def _cached_state(conn: Any, user_id: str) -> WikiState:
    """The user's wiki state: the cached snapshot (spec 2026-10-28-current-state, option B) and
    the events after it; the whole log when there is no usable snapshot."""
    cached = wiki_snapshot(conn, user_id)
    if cached is not None and cached.reducer == reducer_fingerprint():
        try:
            base = WikiState.model_validate(cached.state)
        except ValueError:
            log.warning("wiki snapshot of %s does not load; replaying the log", user_id,
                        exc_info=True)
        else:
            return _advance(base, [_from_row(e.event_id, e.occurred_at, e.payload)
                                   for e in wiki_events_after(conn, user_id, cached.wiki_seq)])
    return replay(_from_row(s.event_id, s.occurred_at, s.payload)
                  for s in wiki_events(conn, user_id))


def load_in(conn: Any, user_id: str) -> Snapshot:
    """`PgRepo.load` on a connection the caller owns: the caller opens the transaction (and picks
    its isolation level; `PgRepo.load` uses repeatable read), so everything it reads afterwards
    on `conn` stays consistent with this snapshot. UnknownUser for an unknown user."""
    if get_user(conn, user_id) is None:
        raise UnknownUser(f"unknown user {user_id}")
    state = _cached_state(conn, user_id)
    k = load_aspects(conn, user_id, k_config(current_infer(conn, user_id)),
                     trusted=inference_trust(state))
    return Snapshot(user_id, state, k, last_wiki_seq(conn, user_id),
                    last_aspect_seq(conn, user_id), current_infer_version(conn, user_id))


class PgRepo:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def aspect_events(self, user_id: str) -> list[AspectEvent]:
        with self.pool.connection() as conn:
            if get_user(conn, user_id) is None:
                raise UnknownUser(f"unknown user {user_id}")
            return [s.event for s in stored_aspect_events(conn, user_id)]

    def wiki_events_of(self, user_id: str, types: Collection[str]) -> list[WikiEvent]:
        with self.pool.connection() as conn:
            if get_user(conn, user_id) is None:
                raise UnknownUser(f"unknown user {user_id}")
            return [_from_row(s.event_id, s.occurred_at, s.payload)
                    for s in wiki_events_of_type(conn, user_id, sorted(types))]

    def wiki_events_since(self, user_id: str, types: Collection[str],
                          after_seq: int = 0) -> list[tuple[int, WikiEvent]]:
        with self.pool.connection() as conn:
            if get_user(conn, user_id) is None:
                raise UnknownUser(f"unknown user {user_id}")
            return [(s.seq, _from_row(s.event_id, s.occurred_at, s.payload))
                    for s in wiki_events_of_type(conn, user_id, sorted(types),
                                                 after_seq=after_seq)]

    def latest_graph_action(self, user_id: str) -> str | None:
        with self.pool.connection() as conn:
            if get_user(conn, user_id) is None:
                raise UnknownUser(f"unknown user {user_id}")
            return latest_graph_action(conn, user_id)

    def load(self, user_id: str) -> Snapshot:
        with self.pool.connection() as conn, conn.transaction():
            # one repeatable-read transaction: both logs and both seqs are one consistent view
            conn.execute("set transaction isolation level repeatable read")
            return load_in(conn, user_id)

    def head(self, user_id: str) -> tuple[int, int, int]:
        """(wiki seq, aspect seq, infer version) a `load` now would report, without replaying
        (three small reads; `TurnRepo` checks its snapshot with it)."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("set transaction isolation level repeatable read")
            if get_user(conn, user_id) is None:
                raise UnknownUser(f"unknown user {user_id}")
            return (last_wiki_seq(conn, user_id), last_aspect_seq(conn, user_id),
                    current_infer_version(conn, user_id))

    def load_at(self, user_id: str, wiki_seq: int, aspect_seq: int,
                infer_version: int) -> Snapshot:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("set transaction isolation level repeatable read")
            if get_user(conn, user_id) is None:
                raise UnknownUser(f"unknown user {user_id}")
            state = replay(_from_row(s.event_id, s.occurred_at, s.payload)
                           for s in wiki_events(conn, user_id, upto_seq=wiki_seq))
            k = load_aspects(conn, user_id, k_config(infer_at(conn, user_id, infer_version)),
                             upto_seq=aspect_seq, trusted=inference_trust(state))
            return Snapshot(user_id, state, k, wiki_seq, aspect_seq, infer_version)

    def commit(self, snap: Snapshot, c: Commit, *, once: str | None = None) -> Snapshot:
        uid = snap.user_id
        built: list[Snapshot] = []
        recorded: dict = {}
        with user_writer(self.pool, uid) as tx:
            if get_user(tx.conn, uid) is None:
                raise UnknownUser(f"unknown user {uid}")
            if once is None:
                built.append(self._commit(tx, snap, c))
            else:
                def fn() -> dict:
                    out = self._commit(tx, snap, c)
                    built.append(out)
                    return {"wiki_seq": out.wiki_seq, "aspect_seq": out.aspect_seq,
                            "infer_version": out.infer_version}

                # the key lookup comes before the version check: a re-run after the commit was
                # applied returns what it recorded, not a false WikiConflict
                recorded = apply_once(tx.conn, once, commit_payload(c), fn)
        if not built:                   # an applied `once` replayed: nothing new was written
            return self.load_at(uid, **recorded)
        _after_commit(built[0], c)      # after the transaction: a job started now sees the commit
        return built[0]

    def _commit(self, tx, snap: Snapshot, c: Commit) -> Snapshot:
        uid = snap.user_id
        aspect_seq = last_aspect_seq(tx.conn, uid)
        if aspect_seq < snap.aspect_seq:
            raise WikiConflict()
        moved = k_moved = aspect_seq != snap.aspect_seq
        between: list[WikiEvent] = []
        if last_wiki_seq(tx.conn, uid) != snap.wiki_seq:
            rows = wiki_events_after(tx.conn, uid, snap.wiki_seq)
            if not rows:
                raise WikiConflict()
            between = [_from_row(e.event_id, e.occurred_at, e.payload) for e in rows]
        _check(snap, c, between, k_moved)
        if between:
            snap = dataclasses.replace(snap, wiki_seq=rows[-1].seq,
                                       state=_advance(snap.state, between))
            moved = True
        if c.empty():
            if not moved:
                return snap
            return dataclasses.replace(        # K of the whole aspect log, for the new state
                snap, k=load_aspects(tx.conn, uid, k_config(current_infer(tx.conn, uid)),
                                     trusted=inference_trust(snap.state)),
                aspect_seq=aspect_seq, infer_version=current_infer_version(tx.conn, uid))
        evs = _wiki_batch(snap, c)
        state = _advance(snap.state, evs)   # before the append: a bad event writes nothing
        lineage = commit_lineage(uid, snap.state, evs, run_id=current_run_id())
        k = record_aspect_events(tx, k_config(current_infer(tx.conn, uid)), c.aspect,
                                 trusted=inference_trust(state))
        seqs = append_wiki_events(tx, [_row(e) for e in evs])
        if seqs:                            # the cache moves with the log, in this transaction
            put_wiki_snapshot(tx.conn, uid, seqs[-1], reducer_fingerprint(),
                              state.model_dump(mode="json"))
        put_lineage(tx.conn, lineage)       # what the commit saved, in its transaction (0091)
        # The account language is a projection, committed with its authoritative wiki event.
        if any(isinstance(e.body, LanguageSet) for e in evs):
            tx.conn.execute("update app_user set lang=%s where user_id=%s",
                            (state.language, uid))
        for write in c.extra:
            write(tx.conn)
        return Snapshot(uid, state, k, seqs[-1] if seqs else snap.wiki_seq,
                        last_aspect_seq(tx.conn, uid), current_infer_version(tx.conn, uid))


class TurnRepo:
    """A `Repo` that loads a user's snapshot once while a turn of theirs is open (`turn`): the
    assistant's ports (data, tools, observer, action tools) all read one wiki per turn instead of
    replaying both logs on every read. Before a remembered snapshot is handed out, the inner
    repo's `head` (three small reads) must still match its seqs, so a write that landed meanwhile -
    the turn's own action tools write through other repos - is seen (one fresh load). A commit
    through it remembers the snapshot the commit returned; a `WikiConflict` forgets it. Outside a
    turn of that user, and over an inner repo without `head`, it only delegates: nothing outlives
    the user's last open turn."""

    def __init__(self, inner: Repo):
        self.inner = inner
        self._lock = threading.Lock()
        self._open: dict[str, int] = {}
        self._memo: dict[str, Snapshot] = {}
        self._head: Callable[[str], tuple[int, int, int]] | None = getattr(inner, "head", None)

    @contextmanager
    def turn(self, user_id: str) -> Iterator[None]:
        with self._lock:
            self._open[user_id] = self._open.get(user_id, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                n = self._open.pop(user_id) - 1
                if n:
                    self._open[user_id] = n
                else:
                    self._memo.pop(user_id, None)

    def _remember(self, snap: Snapshot) -> None:
        with self._lock:
            if snap.user_id in self._open:
                self._memo[snap.user_id] = snap

    def load(self, user_id: str) -> Snapshot:
        with self._lock:
            open_ = user_id in self._open
            snap = self._memo.get(user_id)
        if not open_ or self._head is None:
            return self.inner.load(user_id)
        if snap is not None and self._head(user_id) == (snap.wiki_seq, snap.aspect_seq,
                                                        snap.infer_version):
            return snap
        snap = self.inner.load(user_id)
        self._remember(snap)
        return snap

    def commit(self, snap: Snapshot, c: Commit, *, once: str | None = None) -> Snapshot:
        try:
            out = self.inner.commit(snap, c, once=once)
        except WikiConflict:
            with self._lock:
                self._memo.pop(snap.user_id, None)
            raise
        self._remember(out)
        return out

    def load_at(self, user_id: str, wiki_seq: int, aspect_seq: int,
                infer_version: int) -> Snapshot:
        return self.inner.load_at(user_id, wiki_seq, aspect_seq, infer_version)

    def aspect_events(self, user_id: str) -> list[AspectEvent]:
        return self.inner.aspect_events(user_id)

    def wiki_events_of(self, user_id: str, types: Collection[str]) -> list[WikiEvent]:
        return self.inner.wiki_events_of(user_id, types)

    def wiki_events_since(self, user_id: str, types: Collection[str],
                          after_seq: int = 0) -> list[tuple[int, WikiEvent]]:
        return self.inner.wiki_events_since(user_id, types, after_seq)

    def __getattr__(self, name: str) -> Any:     # e.g. latest_graph_action, head
        if name == "inner":                       # not set yet (copy / unpickle): no recursion
            raise AttributeError(name)
        return getattr(self.inner, name)
