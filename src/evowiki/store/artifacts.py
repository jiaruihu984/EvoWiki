"""Artifacts and their lineage (migration 0091; RSI observability plan 2026-10-29 §5 1b, ruling 3).

An artifact is one saved content version: `(kind, ref, version_key)` of one user. Its id is derived
from those four (`artifact_id`), so whoever shows a version can name it without a lookup — a page
view puts the id of each section it shows on the section, the client sends that id back with its
feedback (ruling 3: the version the user was shown, never the version current at submit time).

Rows are written only at save points, in the save's own transaction where it has one
(`wikiapp.lineage` from a wiki commit; material summaries / glossary; compaction entries). A
version saved again is the same artifact: the row keeps its first producer and creation time, a
dependency already there is kept. A parent named only by reference (a rule rev, a K cell's band,
a node as it is) is registered with no producer if it is not there yet, so the DAG never points
at nothing.

`produced_by_run` is the trace run that saved it (`current_run_id`, session A's `agent.trace`).

Ids pointing here (`Signal.target_artifact_id`, a selection's `artifact_id`, `produced_by_run`'s
run) have no foreign key: forgetting a session deletes its `answer` / `compaction` rows (memory
forget flow), so a reader that does not find an id treats it as「来源已删除」, never as an error."""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import psycopg
from psycopg_pool import ConnectionPool

from evowiki.store.db import fetch_all

ARTIFACT_PREFIX = "art_"
ID_HEX = 24
ARTIFACT_PATTERN = rf"^{ARTIFACT_PREFIX}[0-9a-f]{{{ID_HEX}}}$"     # for request schemas


def artifact_id(user_id: str, kind: str, ref: str, version_key: str) -> str:
    blob = f"{user_id}\x1f{kind}\x1f{ref}\x1f{version_key}".encode()
    return ARTIFACT_PREFIX + hashlib.sha256(blob).hexdigest()[:ID_HEX]


def is_artifact_id(value: str) -> bool:
    body = value[len(ARTIFACT_PREFIX):]
    return (value.startswith(ARTIFACT_PREFIX) and len(body) == ID_HEX
            and all(c in "0123456789abcdef" for c in body))


def text_key(*parts: str) -> str:
    """A content version key: sha256 of the parts (unit-separated), 32 hex."""
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def current_run_id() -> str | None:
    """The trace run this code runs in (session A's `agent.trace.current_run_id`); None outside a
    run, or before that interface exists."""
    try:
        from evowiki.agent.trace import current_run_id as run_id  # type: ignore[attr-defined]
    except ImportError:
        return None
    return run_id()


@dataclass(frozen=True)
class Artifact:
    user_id: str
    kind: str
    ref: str
    version_key: str
    produced_by_run: str | None
    created_at: datetime

    @property
    def artifact_id(self) -> str:
        return artifact_id(self.user_id, self.kind, self.ref, self.version_key)


@dataclass(frozen=True)
class Dependency:
    child_id: str
    parent_id: str
    role: str


@dataclass(frozen=True)
class Lineage:
    """What one save registers: its artifacts (parents named by reference included) and edges."""

    artifacts: tuple[Artifact, ...] = ()
    dependencies: tuple[Dependency, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.artifacts)


def _ordered(lin: Lineage) -> tuple[list[Artifact], list[Dependency]]:
    """Artifacts once each (a producer wins over a reference-only row of the same id) and edges
    once each, dropping self-edges."""
    arts: dict[str, Artifact] = {}
    for a in lin.artifacts:
        old = arts.get(a.artifact_id)
        if old is None or (old.produced_by_run is None and a.produced_by_run is not None):
            arts[a.artifact_id] = a
    deps = list(dict.fromkeys(d for d in lin.dependencies if d.child_id != d.parent_id))
    return list(arts.values()), deps


def put_lineage(conn: psycopg.Connection, lin: Lineage) -> None:
    """Writes `lin` on `conn` (the save's transaction). Idempotent: an artifact already stored keeps
    its row, except that a row registered by reference only gets the producer once one saves it."""
    arts, deps = _ordered(lin)
    if not arts:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "insert into artifact (artifact_id, user_id, kind, ref, version_key, produced_by_run, "
            "created_at) values (%s, %s, %s, %s, %s, %s, %s) "
            "on conflict (artifact_id) do update set produced_by_run = "
            "coalesce(artifact.produced_by_run, excluded.produced_by_run)",
            [(a.artifact_id, a.user_id, a.kind, a.ref, a.version_key, a.produced_by_run,
              a.created_at) for a in arts])
        if deps:
            cur.executemany(
                "insert into artifact_dependency (child_id, parent_id, role) values (%s, %s, %s) "
                "on conflict do nothing",
                [(d.child_id, d.parent_id, d.role) for d in deps])


def artifacts_of(conn: psycopg.Connection, user_id: str,
                 ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    rows = fetch_all(conn, "select * from artifact where user_id = %s and artifact_id = any(%s)",
                     (user_id, list(ids)))
    return {r["artifact_id"]: r for r in rows}


def parents_of(conn: psycopg.Connection, user_id: str,
               child_id: str) -> list[tuple[str, str]]:
    """(parent_id, role) of one artifact of the user, in a stable order."""
    rows = fetch_all(conn, "select d.parent_id, d.role from artifact_dependency d "
                           "join artifact a on a.artifact_id = d.child_id "
                           "where a.user_id = %s and d.child_id = %s "
                           "order by d.role, d.parent_id", (user_id, child_id))
    return [(r["parent_id"], r["role"]) for r in rows]


class ArtifactSink(Protocol):
    """Registers lineage at a save point that has no transaction of its own to share."""

    def put(self, lin: Lineage) -> None: ...


class PgArtifacts:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def put(self, lin: Lineage) -> None:
        if not lin:
            return
        with self.pool.connection() as conn, conn.transaction():
            put_lineage(conn, lin)


class MemoryArtifacts:
    """In-memory twin (tests, MemoryRepo): same first-producer-wins rule."""

    def __init__(self) -> None:
        self.rows: dict[str, Artifact] = {}
        self.deps: set[Dependency] = set()

    def put(self, lin: Lineage) -> None:
        arts, deps = _ordered(lin)
        for a in arts:
            old = self.rows.get(a.artifact_id)
            if old is None:
                self.rows[a.artifact_id] = a
            elif old.produced_by_run is None and a.produced_by_run is not None:
                self.rows[a.artifact_id] = Artifact(old.user_id, old.kind, old.ref,
                                                    old.version_key, a.produced_by_run,
                                                    old.created_at)
        missing = [d for d in deps if d.child_id not in self.rows or d.parent_id not in self.rows]
        if missing:
            raise ValueError(f"dependency on an artifact that is not registered: {missing[0]}")
        self.deps.update(deps)

    def of_kind(self, kind: str) -> list[Artifact]:
        return [a for a in self.rows.values() if a.kind == kind]

    def parents(self, child_id: str) -> list[tuple[str, str]]:
        return sorted(((d.parent_id, d.role) for d in self.deps if d.child_id == child_id),
                      key=lambda x: (x[1], x[0]))


NO_ARTIFACTS: ArtifactSink = type("_NoArtifacts", (), {"put": lambda self, lin: None})()


def merge(parts: Iterable[Lineage]) -> Lineage:
    arts: list[Artifact] = []
    deps: list[Dependency] = []
    for p in parts:
        arts += p.artifacts
        deps += p.dependencies
    return Lineage(tuple(arts), tuple(deps))
