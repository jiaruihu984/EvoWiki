"""Artifacts in Postgres (0091): a wiki commit registers in its own transaction as the app role,
a repeated save keeps the first row, a reference-only row gets its producer later, and the
session store registers answers / compactions."""

from datetime import UTC, datetime

from evowiki.assistant.lineage import answer_artifact, compaction_artifact
from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import SessionInfo
from evowiki.store.artifacts import (
    Artifact,
    Dependency,
    Lineage,
    artifacts_of,
    parents_of,
    put_lineage,
)
from evowiki.store.db import fetch_all
from evowiki.wikiapp.lineage import section_artifact
from evowiki.wikiapp.repo import PgRepo
from tests.runtime.helpers import make_user
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_review_flow import organized_learned_page

T = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


def test_pg_commit_registers_sections_with_their_parents(pool):
    make_user(pool, "u1")
    repo = PgRepo(pool)
    pid = organized_learned_page((repo, Script(), Ids()))
    page = repo.load("u1").state.pages[pid].page
    assert page is not None
    sid = section_artifact("u1", page, "s2")
    with pool.connection() as conn:
        rows = artifacts_of(conn, "u1", [sid])
        roles = {r for _, r in parents_of(conn, "u1", sid)}
        kinds = {r["kind"] for r in fetch_all(conn, "select distinct kind from artifact")}
    assert rows[sid]["kind"] == "section" and rows[sid]["ref"] == f"{pid}/s2"
    assert {"outline", "rule", "k_cell"} <= roles
    assert {"section", "outline", "check", "card", "rule", "k_cell"} <= kinds


def test_put_lineage_is_idempotent_and_fills_a_missing_producer(pool):
    make_user(pool, "u1")
    ref_only = Artifact("u1", "rule", "r1", "1", None, T)
    child = Artifact("u1", "section", "p/s", "k", "run-a", T)
    lin = Lineage((ref_only, child), (Dependency(child.artifact_id, ref_only.artifact_id, "rule"),))
    with pool.connection() as conn, conn.transaction():
        put_lineage(conn, lin)
        put_lineage(conn, lin)
        put_lineage(conn, Lineage((Artifact("u1", "rule", "r1", "1", "run-b", T),
                                   Artifact("u1", "section", "p/s", "k", "run-c", T))))
    with pool.connection() as conn:
        rows = artifacts_of(conn, "u1", [ref_only.artifact_id, child.artifact_id])
        deps = parents_of(conn, "u1", child.artifact_id)
    assert rows[ref_only.artifact_id]["produced_by_run"] == "run-b"   # filled once saved
    assert rows[child.artifact_id]["produced_by_run"] == "run-a"      # the first producer stays
    assert deps == [(ref_only.artifact_id, "rule")]


def test_pg_session_store_registers_answers_and_chains_compactions(pool):
    make_user(pool, "u1")
    store = PgSessionStore(pool)
    store.create(SessionInfo(session_id="s1", user_id="u1", page_id=None, note_concept_id=None,
                             created_at=T))
    store.append("s1", "e1", "assistant", {"message_id": "m1", "text": "答"}, T)
    store.append("s1", "c1", "compaction", {"summary": "一"}, T)
    store.append("s1", "c2", "compaction", {"summary": "二"}, T)
    store.append("s1", "c2", "compaction", {"summary": "二"}, T)          # a retry: nothing new
    with pool.connection() as conn:
        assert answer_artifact("u1", "m1") in artifacts_of(conn, "u1", [answer_artifact("u1", "m1")])
        assert parents_of(conn, "u1", compaction_artifact("u1", "c2")) == [
            (compaction_artifact("u1", "c1"), "previous")]
