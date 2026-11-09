"""Forgetting a session deletes the lineage rows of its answers and compaction summaries
(observability 0091 `artifact` / `artifact_dependency`; memory/forget.py `forget_artifacts`):
the session's own, and those of its tainted turns and voided compactions; pages' artifacts stay.
Before 0091 is merged the test creates the two tables from its DDL. Postgres; fakes; no model
call."""

import pytest

from tests.memory.forget_world import T0, build

DDL = """
create table if not exists artifact (
  artifact_id text primary key, user_id text not null references app_user (user_id),
  kind text not null, ref text not null, version_key text not null, produced_by_run text,
  created_at timestamptz not null, unique (user_id, kind, ref, version_key));
create table if not exists artifact_dependency (
  child_id text not null references artifact (artifact_id) on delete cascade,
  parent_id text not null references artifact (artifact_id) on delete cascade,
  role text not null, primary key (child_id, parent_id, role), check (child_id <> parent_id));
grant select, insert, update, delete on artifact, artifact_dependency to evowiki_app;
"""


@pytest.fixture
def w(pool, tmp_path, admin_pool):
    with admin_pool.connection() as conn:
        conn.execute(DDL)
    return build(pool, tmp_path)


def test_answer_and_compaction_artifacts_go_page_ones_stay(w):
    rows = [("art_a1", "answer", "a1-ans"), ("art_b2", "answer", "b2-ans"),
            ("art_c2", "answer", "c2-ans"), ("art_b1", "answer", "b1-ans"),
            ("art_comp", "compaction", "B-compaction"), ("art_sec", "section", "p1/s1")]
    with w.pool.connection() as conn:
        for aid, kind, ref in rows:
            conn.execute("insert into artifact (artifact_id, user_id, kind, ref, version_key, "
                         "created_at) values (%s, 'u1', %s, %s, 'h', %s)", (aid, kind, ref, T0))
        conn.execute("insert into artifact_dependency values ('art_sec', 'art_a1', 'source'), "
                     "('art_sec', 'art_b1', 'source')")
        # the session store registered the world's real answers / compactions too (0091)
        before = conn.execute("select count(*) as n from artifact").fetchone()["n"]
    out = w.service.forget_session("u1", "A", requested_by="test")
    assert out.state == "committed", out
    with w.pool.connection() as conn:
        left = {r["artifact_id"] for r in conn.execute("select artifact_id from artifact")}
        edges = conn.execute("select child_id, parent_id from artifact_dependency").fetchall()
        audit = {r["step"]: r["rows"] for r in conn.execute(
            "select step, rows from forget_audit where request_id = %s", (out.request_id,))}
    mine = {aid for aid, _, _ in rows}
    assert left & mine == {"art_b1", "art_sec"}
    assert [e for e in edges if e["child_id"] in mine] == [
        {"child_id": "art_sec", "parent_id": "art_b1"}]
    assert audit["artifact"] == before - len(left) and audit["artifact"] >= 4
    assert audit["artifact_dependency"] >= 1
