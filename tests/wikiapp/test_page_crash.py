"""Crash tests for durable page writing (B1b spec §reliability, Task 11): real process deaths
(os._exit) in a child, then a second child that recovers the pending workflow. The model is scripted;
no network. Each test: the crash child exits 1, the resume child exits 0, then the log (one line per
provider call, per checker call, per stream item) and the database say what ran how often."""
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import psycopg
import pytest

from evowiki.deep.store import PgAssetStore
from evowiki.wikiapp.page_run_store import PgPageRunStore
from evowiki.wikiapp.page_runs import InlinePageRuns
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.repo import PgRepo
from tests.pg import drop_database, fresh_database
from tests.runtime.helpers import make_user
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page_runs import path_with_first_page

ROOT = Path(__file__).resolve().parents[2]


def run_child(scenario: str, mode: str, db: str, log: Path, page_id: str,
              crash_at: str = "") -> int:
    env = {**os.environ, "CRASH_AT": crash_at, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, "-m", "tests.wikiapp.page_crash_child", scenario, mode,
                           db, str(log), page_id], cwd=ROOT, env=env, timeout=240,
                          check=False).returncode


def lines(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines()


def items(log: Path) -> list[dict]:
    return [json.loads(ln[5:]) for ln in lines(log) if ln.startswith("item ")]


def seed(pool):
    """((repo, script, ids), path id, page 1) — page 1 is a plain, unorganized wiki page."""
    make_user(pool, "u1")
    env = (PgRepo(pool), Script(), Ids())
    path_id, p1 = path_with_first_page(env)
    return env, path_id, p1


@pytest.fixture
def seeded(db_url, pool):
    return seed(pool)


@pytest.fixture
def reference_items(pg_admin_url, tmp_path):
    """The stream of the same page written without a crash, in a database of its own."""
    from evowiki.store.db import make_pool
    url, name = fresh_database(pg_admin_url)
    pool = make_pool(url, min_size=1, max_size=4)
    try:
        _, _, p1 = seed(pool)
        log = tmp_path / "reference"
        assert run_child("organize", "crash", url, log, p1) == 0     # nothing crashes
        return items(log), p1
    finally:
        pool.close()
        drop_database(pg_admin_url, name)


def count(db: str, sql: str, *params) -> int:
    with psycopg.connect(db) as c:
        return c.execute(sql, params).fetchone()[0]


def canonical(its: list[dict]) -> list[dict]:
    """Stream items with the json inside ```evoui fences re-dumped with sorted keys."""
    def fix(m):
        if not m.group(1).lstrip().startswith("{"):      # an `asset: <id>` fence (a written page)
            return m.group(0)
        dumped = json.dumps(json.loads(m.group(1)), sort_keys=True, ensure_ascii=False)
        return "```evoui\n" + dumped + "\n```"
    out = json.loads(json.dumps(its))
    for it in out:
        body = it["data"].get("body")
        if isinstance(body, str):
            it["data"]["body"] = re.sub(r"```evoui\n(.*?)\n```", fix, body, flags=re.DOTALL)
    return out


def assert_clean_stream(log: Path) -> list[str]:
    """One outline, one done (last). (Equality with an uninterrupted run shows nothing is re-emitted.)"""
    its = items(log)
    names = [i["event"] for i in its]
    assert names.count("outline") == 1 and names[:3] == ["planning", "outline", "writer"]
    assert names.count("done") == 1 and names[-1] == "done"
    return names


def test_crash_mid_write_repeats_only_the_inflight_call(seeded, db_url, pool, tmp_path,
                                                        reference_items):
    env, _, p1 = seeded
    log = tmp_path / "log"
    run_id = f"page:u1:{p1}:1"
    assert run_child("organize", "crash", db_url, log, p1, "deep.write:3") == 1
    assert run_child("organize", "resume", db_url, log, p1) == 0
    ls = lines(log)
    served = Counter(ln for ln in ls if ln.startswith("provider "))
    # model calls whose step was archived are not asked again; only the call that died is
    # (turn 1 is plan_page: no model call runs before the writer's loop)
    assert not any(k.startswith(("provider goals.organize", "provider deep.plan")) for k in served)
    assert [served[f"provider deep.write {n}"] for n in (1, 2, 3, 4, 5)] == [1, 1, 2, 1, 1]
    assert [ln for ln in ls if ln.startswith("check ")] == ["check proj"]    # not re-checked
    assert "result done" in ls
    rec = env[0].load("u1").state.pages[p1]
    assert rec.page is not None and set(rec.page.texts) == {"s1", "s2", "s3"}
    # five write turns, each recorded once (the crashed one never reached the log)
    assert count(db_url, "select count(*) from llm_attempt where purpose = 'deep.write'") == 5
    assert_clean_stream(log)
    ref, ref_page = reference_items
    assert ref_page == p1 and items(log) == ref          # the very stream of an uninterrupted run
    row = PgPageRunStore(pool).get(run_id)
    assert row.status == "done" and row.finished_at is not None


def test_crash_after_llm_call_row_is_served_from_llm_call_without_a_second_call(
        seeded, db_url, pool, tmp_path, reference_items):
    env, _, p1 = seeded
    log = tmp_path / "log"
    run_id = f"page:u1:{p1}:1"
    crash = "after_llm_call:deep.write:2"
    assert run_child("organize", "crash", db_url, log, p1, crash) == 1
    assert count(db_url, "select count(*) from llm_call where purpose = 'deep.write'") == 2
    assert run_child("organize", "resume", db_url, log, p1) == 0
    ls = lines(log)
    assert f"crash {crash}" in ls and "result done" in ls
    served = Counter(ln for ln in ls if ln.startswith("provider "))
    # the answer was recorded before the crash: the re-executed step reads llm_call, no provider call
    assert [served[f"provider deep.write {n}"] for n in (1, 2, 3, 4, 5)] == [1] * 5
    # one accepted call row and one attempt per write turn: nothing was paid twice; the re-executed
    # step served from the log is a logical call of its own (cost 0, served_from the recorded one)
    assert count(db_url, "select count(*) from llm_call where purpose = 'deep.write' "
                         "and served_from is null") == 5
    assert count(db_url, "select count(*) from llm_call where purpose = 'deep.write' "
                         "and served_from is not null and cost = 0") == 1
    assert count(db_url, "select count(*) from llm_attempt where purpose = 'deep.write'") == 5
    organized = [e for e in env[0].wiki_events_of("u1", ["page_saved"])
                 if e.body.action == "organized" and e.body.record.page_id == p1]
    assert len(organized) == 1
    ref, ref_page = reference_items
    assert ref_page == p1
    # The replayed call comes back from jsonb (keys reordered inside the evoui block) and, served
    # from llm_call, adds nothing to the page meter: the call paid in the dead process is not
    # re-added, so the meter is exactly that one call short. Everything else equals the reference.
    got, want = canonical(items(log)), canonical(ref)
    [got_done], [want_done] = got[-1:], want[-1:]
    assert got_done["data"].pop("cost_usd") == pytest.approx(want_done["data"].pop("cost_usd") * 4 / 5)
    assert got == want
    row = PgPageRunStore(pool).get(run_id)
    assert row.status == "done" and row.finished_at is not None


def test_crash_after_commit_commits_once(seeded, db_url, pool, tmp_path, reference_items):
    env, _, p1 = seeded
    log = tmp_path / "log"
    run_id = f"page:u1:{p1}:1"
    assert run_child("organize", "crash", db_url, log, p1, "after_commit") == 1
    assert run_child("organize", "resume", db_url, log, p1) == 0
    ls = lines(log)
    assert "crash after_commit" in ls and "result done" in ls
    # the first commit is the outline's, inside plan_page's tool call (T3): the crash came right
    # after the plan_page turn, before any section was written
    assert ls.index("returned deep.write 1") < ls.index("crash after_commit") < ls.index(
        "provider deep.write 2")
    saved = [e.body.action for e in env[0].wiki_events_of("u1", ["page_saved"])
             if e.body.record.page_id == p1]
    # the re-executed commit step reused its key: the outline is stored once, then s1–s3, then
    # the final commit
    assert saved == ["created", "outlined", "section_written", "section_written",
                     "section_written", "organized"]
    assert count(db_url, "select count(*) from effect_applied where position(%s in key) = 1",
                 run_id + ":") == 5
    assert count(db_url, "select count(*) from wiki_asset where user_id = 'u1' and page_id = %s",
                 p1) == 1
    [meta] = PgAssetStore(pool).for_page("u1", p1)
    rec = env[0].load("u1").state.pages[p1]
    assert rec.page is not None and rec.written.assets == (meta.asset_id,)    # the page references it
    assert meta.kind == "evoui" and meta.section_id == "s2"
    # no provider call and no render check repeated after the commit
    served = Counter(ln for ln in ls if ln.startswith(("provider ", "check ")))
    assert set(served.values()) == {1}
    assert_clean_stream(log)
    ref, ref_page = reference_items
    assert ref_page == p1 and items(log) == ref
    row = PgPageRunStore(pool).get(run_id)
    assert row.status == "done" and row.finished_at is not None


def test_idle_write_commits_its_page_at_once_with_its_interactive(seeded, db_url, pool, tmp_path):
    env, path_id, p1 = seeded
    repo, s, ids = env
    from tests.wikiapp.test_k1_replay import PAGE1
    from tests.wikiapp.test_page_runs import script_outline, stream
    script_outline(s, PAGE1)
    list(stream(env, p1, InlinePageRuns()))                  # page 1 written in-process
    p2 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    log = tmp_path / "log"
    assert run_child("idle", "crash", db_url, log, p2) == 0     # the Bot's write runs to done
    store = PgPageRunStore(pool)
    row = store.get(f"page:u1:{p2}:1")
    assert row.origin == "idle" and row.status == "done" and row.began_at is not None
    assert row.llm_calls is not None and row.cost_usd is not None and row.failure is None
    rec = repo.load("u1").state.pages[p2]
    assert rec.page is not None and set(rec.page.texts) == {"s1", "s2"}   # committed, not held
    assert count(db_url, "select count(*) from wiki_asset where user_id = 'u1' and page_id = %s",
                 p2) == 1
    saved = [e.body.action for e in repo.wiki_events_of("u1", ["page_saved"])
             if e.body.record.page_id == p2]
    assert saved == ["created", "outlined", "section_written", "section_written", "organized"]
    # the page is written ahead: 「下一页」 hands it back, nothing is written again
    out = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids, runs=store)
    assert out.prefetched and out.page_id == p2


def test_idle_write_crashing_after_its_commit_commits_once(seeded, db_url, pool, tmp_path):
    env, path_id, p1 = seeded
    repo, s, ids = env
    from tests.wikiapp.test_k1_replay import PAGE1
    from tests.wikiapp.test_page_runs import script_outline, stream
    script_outline(s, PAGE1)
    list(stream(env, p1, InlinePageRuns()))
    p2 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    log = tmp_path / "log"
    assert run_child("idle", "crash", db_url, log, p2, "after_commit") == 1
    assert run_child("idle", "resume", db_url, log, p2) == 0
    ls = lines(log)
    assert "crash after_commit" in ls and "result done" in ls
    saved = [e.body.action for e in repo.wiki_events_of("u1", ["page_saved"])
             if e.body.record.page_id == p2]
    # the commit step is not repeated (the crash came after the outline's commit)
    assert saved == ["created", "outlined", "section_written", "section_written", "organized"]
    row = PgPageRunStore(pool).get(f"page:u1:{p2}:1")
    assert row.status == "done" and row.origin == "idle"
