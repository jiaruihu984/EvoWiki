"""Task 3 of assistant-actions: the ledger executors of the assistant's page / path / goal actions
(`bot.executors_assistant`) on real Postgres with a seeded path and FAKE write requests / practice
writer / scripted LLM - nothing here calls a model."""

from datetime import UTC, date, datetime

import pytest

from evowiki.bot.actions import STALE_DETAIL, ActionLedger, ActionRefused, NotDecidable, can_undo
from evowiki.bot.executors_assistant import (
    NO_BLOCKS,
    SCOPES,
    SUMMARY_GOAL,
    action_level,
    path_fingerprint,
    register_assistant_executors,
    revise_pages,
)
from evowiki.bot.executors_profile import FORGET_KIND
from evowiki.bot.executors_profile import KIND as PROFILE_FACT
from evowiki.bot.practice import DeepPracticeWriter
from evowiki.bot.settings import BotSettings
from evowiki.deep.store import MemoryAssetStore
from evowiki.deep.writer import PageWriter
from evowiki.goals.path_edit import AddEdit, MoveEdit
from evowiki.store import bot as store
from evowiki.wikiapp.events import PageSaved, PagesTrashed, PathSaved, WikiEvent, WritingMark
from evowiki.wikiapp.goal import page_events_for
from evowiki.wikiapp.kwrite import page_spec
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import Writing
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.path_order import shown
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.review import deadline_days
from tests.deep.fakes import PRACTICE_BLOCK, DeepScript, FakeChecker, interactive, library, turn
from tests.deep.fakes import call as tool_call
from tests.runtime.helpers import make_user
from tests.wikiapp.partial_write_check import assert_partial_write
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import GOAL
from tests.wikiapp.test_page import organized
from tests.wikiapp.test_path_edits import NOW, PATH_ID, Ids, seed_events

TZ = BotSettings().timezone              # the user's Bot zone (Asia/Shanghai, the default)

P1, P2, P3 = (f"{PATH_ID}-p{i}" for i in (1, 2, 3))
ALL_ASK = BotSettings(levels={s: "ask" for s in ("page_body", "new_page", "delete_merge",
                                                 "structure", "path")})
_n = iter(range(100_000))


class Fakes:
    """Write requests and cancels, recorded."""

    def __init__(self):
        self.requests: list[list[str]] = []
        self.cancels: list[list[str]] = []
        self.fail_request = False

    def request_write(self, user_id, page_ids):
        if self.fail_request:
            raise RuntimeError("queue down")
        self.requests.append(list(page_ids))
        return [f"run-{p}" for p in page_ids]

    def cancel_writes(self, user_id, page_ids):
        self.cancels.append(list(page_ids))


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    ids, fakes = Ids(), Fakes()
    repo = PgRepo(pool)
    aspect, wiki = seed_events(ids)
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    with pool.connection() as conn:
        store.put_settings(conn, "u1", ALL_ASK, NOW)
    ledger = ActionLedger(pool, lambda: NOW, ids, {})
    scripts: dict = {"s": Script()}
    assets, checker = MemoryAssetStore(), FakeChecker()

    def gateway(uid):
        return CountingGateway(scripts["s"].gateway())
    ds = DeepScript()                     # the page writer's practice runs (scripted deep.write)
    writing = Writing(writer=PageWriter(llm=ds.llm(), checker=checker, library=library()),
                      specs=WikiSpecSource(repo), assets=assets)
    practice = DeepPracticeWriter(repo, lambda: writing, assets, lambda: NOW, ids)
    ex = register_assistant_executors(
        ledger, repo, lambda: NOW, ids, request_write=fakes.request_write,
        cancel_writes=fakes.cancel_writes, gateway=gateway, pool=pool, practice=practice)
    return type("Env", (), {"pool": pool, "repo": repo, "ids": ids, "fakes": fakes,
                            "ledger": ledger, "ex": ex, "scripts": scripts, "assets": assets,
                            "checker": checker, "practice": practice, "ds": ds})


def go(env, kind, content, *, draft=None, scope=None, seen=True, **kw):
    """Proposes and approves. A path action gets `path_seen` (what Task 5's tools will put there)
    unless `seen=False`."""
    if seen and "path_seen" not in content and content.get("path_id") in state(env).paths \
            and kind in ("page_split", "page_new", "path_edit"):
        content = {**content, "path_seen": path_fingerprint(state(env).paths[content["path_id"]])}
    row = env.ledger.propose("u1", kind=kind, scope=scope or SCOPES[kind][0],
                             source="assistant", summary=kind, why="", content=content,
                             key=f"{kind}:{next(_n)}", **kw)
    return env.ledger.approve("u1", row.action_id, draft=draft)


def state(env):
    return env.repo.load("u1").state


def row_keys(env):
    return [i.key for i in shown(state(env).paths[PATH_ID])]


def titles(env):
    return [i.title for i in shown(state(env).paths[PATH_ID])]


def parts(*specs):
    return [{"key": f"k{i}", **s} for i, s in enumerate(specs)]


A = {"title": "抽样的随机性", "covers": "同一总体每次抽样不同"}
B = {"title": "样本均值的分布", "node_id": "n5"}


# --- levels -------------------------------------------------------------------------------

def test_the_strictest_scope_decides_the_level(env):
    with env.pool.connection() as conn:
        store.put_settings(conn, "u1", BotSettings(levels={
            "new_page": "auto_undo", "structure": "ask", "delete_merge": "auto_undo"}), NOW)
    assert action_level(env.ledger, "u1", "page_split") == ("ask", "structure")
    assert action_level(env.ledger, "u1", "page_new") == ("auto_undo", "new_page")
    assert action_level(env.ledger, "u1", "page_merge") == ("auto_undo", "delete_merge")
    with env.pool.connection() as conn:
        store.put_settings(conn, "u1", BotSettings(levels={
            "new_page": "ask", "structure": "auto_undo"}), NOW)
    assert action_level(env.ledger, "u1", "page_split") == ("ask", "new_page")
    assert set(SCOPES) - {PROFILE_FACT, FORGET_KIND, "memory_forget"} == set(env.ex)     # those two: registered by service/deps


# --- page_split ---------------------------------------------------------------------------

def test_split_replaces_the_row_and_requests_no_write(env):
    out = go(env, "page_split", {"path_id": PATH_ID, "page_key": P2, "pages": parts(A, B)})
    assert out.status == "executed", out.detail
    assert row_keys(env)[0] == P1 and row_keys(env)[3] == P3 and len(row_keys(env)) == 4
    assert titles(env)[1:3] == ["抽样的随机性", "样本均值的分布"]
    rec = state(env).paths[PATH_ID]
    new_ids = out.result["new_pages"]
    assert [rec.page_ids[next(n for n, p in enumerate(rec.path.pages) if p.page_id == i.key)]
            for i in shown(rec)[1:3]] == new_ids
    assert env.fakes.requests == []                           # created, not written: written on open
    assert "run_ids" not in out.result
    assert {state(env).pages[p].title for p in new_ids} == {A["title"], B["title"]}
    assert state(env).pages[new_ids[1]].targets == ("n5",)
    # the title-only part got a node of its own
    assert state(env).graph.node(state(env).pages[new_ids[0]].targets[0]).name == A["title"]
    assert can_undo(env.ledger.list("u1", status="executed")[0])


def test_split_undo_restores_the_row_cancels_writes_and_trashes_the_new_pages(env):
    out = go(env, "page_split", {"path_id": PATH_ID, "page_key": P2, "pages": parts(A, B)})
    new_ids = out.result["new_pages"]
    undone = env.ledger.undo("u1", out.action_id)
    assert undone.status == "undone"
    assert row_keys(env) == [P1, P2, P3]
    assert env.fakes.cancels == [new_ids]
    assert state(env).trashed_pages == frozenset(new_ids)


def test_split_undo_is_refused_when_a_later_path_edit_used_a_new_row(env):
    out = go(env, "page_split", {"path_id": PATH_ID, "page_key": P2, "pages": parts(A, B)})
    new_key = row_keys(env)[1]
    go(env, "path_edit", {"path_id": PATH_ID, "op": "reorder", "pages": [{"key": new_key}]})                                      # (already first? moves it)
    with pytest.raises(NotDecidable, match="后面的路径修改"):
        env.ledger.undo("u1", out.action_id)
    assert row_keys(env)[0] == new_key and env.fakes.cancels == []


def test_split_undo_is_refused_when_a_new_page_is_finished(env):
    out = go(env, "page_split", {"path_id": PATH_ID, "page_key": P2, "pages": parts(A, B)})
    pid = out.result["new_pages"][0]
    snap = env.repo.load("u1")
    rec = snap.state.pages[pid].model_copy(update={"done": True})
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.ids("wev"), ts=NOW,
                                                 body=PageSaved(record=rec, action="done"))]))
    with pytest.raises(NotDecidable, match="已经学完"):
        env.ledger.undo("u1", out.action_id)


def test_split_of_a_row_the_path_no_longer_shows_is_stale(env):
    out = go(env, "page_split", {"path_id": PATH_ID, "page_key": "gone", "pages": parts(A)})
    assert out.status == "failed" and out.detail == STALE_DETAIL
    assert env.fakes.requests == [] and row_keys(env) == [P1, P2, P3]


def test_a_changed_page_makes_the_action_stale(env):
    snap = env.repo.load("u1")
    rec, evs = page_events_for(snap.state, snap.k, title="旧页", goal="g",
                               concepts=("n1",), targets=("n1",), now=NOW, new_id=env.ids,
                               user_id="u1")
    env.repo.commit(snap, Commit(wiki=evs))
    out = go(env, "page_new", {"mode": "page", "pages": [A]}, page_id=rec.page_id,
             page_version="an-older-fingerprint")
    assert out.status == "failed" and out.detail == STALE_DETAIL
    assert env.fakes.requests == []


def test_a_write_request_that_fails_is_reported_not_swallowed(env):
    env.fakes.fail_request = True
    out = go(env, "page_new", {"mode": "page", "path_id": PATH_ID, "pages": parts(A)})
    assert out.status == "executed"                           # the structure is committed
    assert "queue down" in out.result["write_error"] and out.result["run_ids"] == []
    assert len(row_keys(env)) == 4


def test_a_split_never_asks_the_queue_even_when_it_is_down(env):
    env.fakes.fail_request = True
    out = go(env, "page_split", {"path_id": PATH_ID, "page_key": P2, "pages": parts(A)})
    assert out.status == "executed" and "write_error" not in out.result
    assert env.fakes.requests == [] and len(row_keys(env)) == 3


# --- revise (drafts) ----------------------------------------------------------------------

def test_draft_reorder_drop_and_add(env):
    draft = {"pages": [{"key": "k1"}, {"key": "k0", "covers": "改过的范围"}, {"title": "补一页"}]}
    pages = parts(A, B, {"title": "被去掉的"})
    out = go(env, "page_split", {"path_id": PATH_ID, "page_key": P2, "pages": pages, "confirm": True},
             draft=draft)
    assert out.status == "executed", out.detail
    assert titles(env)[1:4] == ["样本均值的分布", "抽样的随机性", "补一页"]
    assert [p["title"] for p in out.content["pages"]] == ["样本均值的分布", "抽样的随机性", "补一页"]
    assert out.content["pages"][1]["covers"] == "改过的范围"
    assert env.fakes.requests == [] and len(out.result["new_pages"]) == 3


@pytest.mark.parametrize("draft", [
    {"pages": []},                                            # nothing left
    {"pages": [{"key": "nope"}]},                             # unknown key
    {"pages": [{"key": "k0"}, {"key": "k0"}]},                # the same page twice
    {"pages": [{"title": "  "}]},                             # a new page needs a title
    {"pages": [{"key": "k0", "covers": 3}]},                  # wrong type
    {"pages": [{"key": "k0", "extra": 1}]},                   # unknown field
    {"pages": "x"}, {}, [],                                  # not a page list at all
])
def test_a_bad_draft_is_refused_and_nothing_is_written(env, draft):
    out = go(env, "page_split", {"path_id": PATH_ID, "page_key": P2, "pages": parts(A, B)},
             draft=draft)
    assert out.status == "failed" and out.detail
    assert row_keys(env) == [P1, P2, P3] and env.fakes.requests == []


def test_revise_pages_keeps_the_rest_of_the_content_and_unique_new_keys():
    content = {"path_id": "p", "pages": [{"key": "new-0", "title": "甲"}], "confirm": True}
    out = revise_pages(content, {"pages": [{"key": "new-0"}, {"title": "乙"}]}, allow_new=True)
    assert out["path_id"] == "p" and out["confirm"] is True
    assert [p["key"] for p in out["pages"]] == ["new-0", "new-1"]
    with pytest.raises(ActionRefused):
        revise_pages(content, {"pages": [{"title": "乙"}]}, allow_new=False)


# --- page_new -----------------------------------------------------------------------------

def test_page_new_in_the_path_at_an_index_and_undo_removes_it(env):
    out = go(env, "page_new", {"mode": "page", "path_id": PATH_ID, "index": 0, "pages": [B]})
    assert out.status == "executed", out.detail
    assert titles(env)[0] == "样本均值的分布" and len(row_keys(env)) == 4
    [pid] = out.result["new_pages"]
    assert env.fakes.requests == [[pid]]
    env.ledger.undo("u1", out.action_id)
    assert row_keys(env) == [P1, P2, P3] and state(env).trashed_pages == {pid}
    assert env.fakes.cancels == [[pid]]


def test_page_new_for_a_node_the_path_already_shows_is_refused(env):
    out = go(env, "page_new", {"mode": "page", "path_id": PATH_ID,
                               "pages": [{"title": "第 1 页", "node_id": "n1"}]})
    assert out.status == "failed" and "已经有页" in out.detail
    assert env.fakes.requests == []


def test_cheat_sheet_is_a_summary_mode_page(env):
    out = go(env, "page_new", {"mode": "summary", "pages": [
        {"title": "统计推断速查", "covers": "样本、抽样分布、置信区间",
         "concepts": ["n1", "n2", "n3"]}]})
    assert out.status == "executed", out.detail
    [pid] = out.result["new_pages"]
    rec = state(env).pages[pid]
    assert rec.goal.startswith(SUMMARY_GOAL) and "置信区间" in rec.goal
    assert rec.concepts == ("n1", "n2", "n3") and rec.targets == ()
    assert row_keys(env) == [P1, P2, P3]                      # not on a path
    assert env.fakes.requests == [[pid]]
    snap = env.repo.load("u1")                                # the writer's spec, on a page with no target node
    assert page_spec(snap.state, snap.k, pid, user_id="u1").targets == ("n1", "n2", "n3")
    env.ledger.undo("u1", out.action_id)
    assert state(env).trashed_pages == {pid}


def test_page_new_with_an_unknown_mode_or_node_is_refused(env):
    assert go(env, "page_new", {"mode": "poem", "pages": [A]}).status == "failed"
    out = go(env, "page_new", {"mode": "page", "pages": [{"title": "x", "node_id": "zz"}]})
    assert out.status == "failed" and "zz" in out.detail


# --- page_merge ---------------------------------------------------------------------------

def _two_pages(env):
    snap = env.repo.load("u1")
    made = []
    for title, cid in (("页甲", "n1"), ("页乙", "n2")):
        rec, evs = page_events_for(snap.state, snap.k, title=title, goal="g",
                                   concepts=(cid,), targets=(cid,), now=NOW, new_id=env.ids,
                                   user_id="u1")
        env.repo.commit(snap, Commit(wiki=evs))
        snap = env.repo.load("u1")
        made.append(rec.page_id)
    return made


def _written(env):
    """A page record with a written body (scripted organize, no model)."""
    pid = organized((env.repo, Script(), env.ids))
    return state(env).pages[pid]


def test_merge_creates_the_new_page_unwritten_and_trashes_the_originals_when_it_is_written(env):
    a, b = _two_pages(env)
    out = go(env, "page_merge", {"title": "合并页", "pages": [{"key": a}, {"key": b}]})
    assert out.status == "executed", out.detail
    new = out.result["new_page"]
    st = state(env)
    assert st.pages[new].concepts == ("n1", "n2") and not st.trashed_pages
    assert env.fakes.requests == []                           # written when the learner opens it
    ex = env.ex["page_merge"]
    row = env.ledger.list("u1", status="executed")[0]
    assert ex.settle("u1", row) is False                      # not written yet: originals stay
    body = _written(env).page
    snap = env.repo.load("u1")
    written = snap.state.pages[new].model_copy(update={"page": body})
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.ids("wev"), ts=NOW, body=PageSaved(
        record=written, action="organized"))]))
    row = env.ledger.list("u1", status="executed")[0]
    assert ex.settle("u1", row) is True and state(env).trashed_pages == {a, b}
    row = env.ledger.list("u1", status="executed")[0]
    assert ex.settle("u1", row) is False                      # once
    undone = env.ledger.undo("u1", out.action_id)
    assert undone.status == "undone" and state(env).trashed_pages == {new}


def test_a_part_written_merged_page_does_not_settle(env):
    """Stage 4: a merge's first write commits its sections as it goes, under a WritingMark. Until
    the run's final commit clears the mark the merged page is not written: the originals stay."""
    a, b = _two_pages(env)
    out = go(env, "page_merge", {"title": "合并页", "pages": [{"key": a}, {"key": b}]})
    new = out.result["new_page"]
    ex = env.ex["page_merge"]
    body = _written(env).page
    snap = env.repo.load("u1")
    part = snap.state.pages[new].model_copy(update={
        "page": body, "writing": WritingMark(job_id=f"page:u1:{new}:1")})
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.ids("wev"), ts=NOW, body=PageSaved(
        record=part, action="section_written"))]))
    row = env.ledger.list("u1", status="executed")[0]
    assert ex.settle("u1", row) is False and not state(env).trashed_pages
    snap = env.repo.load("u1")
    done = snap.state.pages[new].model_copy(update={"writing": None})
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.ids("wev"), ts=NOW, body=PageSaved(
        record=done, action="organized"))]))
    row = env.ledger.list("u1", status="executed")[0]
    assert ex.settle("u1", row) is True and state(env).trashed_pages == {a, b}


def test_merge_undo_before_it_is_written_cancels_and_removes_the_new_page(env):
    a, b = _two_pages(env)
    out = go(env, "page_merge", {"title": "合并页", "pages": [{"key": a}, {"key": b}]})
    new = out.result["new_page"]
    env.ledger.undo("u1", out.action_id)
    assert env.fakes.cancels == [[new]] and state(env).trashed_pages == {new}


def test_merge_needs_two_existing_pages_and_a_title(env):
    a, _ = _two_pages(env)
    assert go(env, "page_merge", {"title": "t", "pages": [{"key": a}]}).status == "failed"
    out = go(env, "page_merge", {"title": "t", "pages": [{"key": a}, {"key": "pg-gone"}]})
    assert out.status == "failed" and out.detail == STALE_DETAIL
    assert go(env, "page_merge", {"title": " ", "pages": [{"key": a}, {"key": a}]}).status == "failed"


def _write_new(env, new, body=None):
    """The merged page's write committed (scripted organize)."""
    body = body or _written(env).page
    snap = env.repo.load("u1")
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.ids("wev"), ts=NOW, body=PageSaved(
        record=snap.state.pages[new].model_copy(update={"page": body}), action="organized"))]))


def _on_path(env, a, b):
    """a and b are the wiki pages of the path's rows p1 and p2."""
    snap = env.repo.load("u1")
    rec = snap.state.paths[PATH_ID].model_copy(update={"page_ids": (a, b, None)})
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.ids("wev"), ts=NOW,
                                                 body=PathSaved(record=rec, action="user_edit"))]))


def _merge(env, a, b):
    snap = env.repo.load("u1")
    return go(env, "page_merge", {"title": "合并页", "pages": [{"key": a}, {"key": b}],
                                  "versions": {p: page_fingerprint(snap.state.pages[p])
                                               for p in (a, b)}})


def test_the_merge_job_carries_the_originals_text_and_versions(env):
    a = _written(env).page_id
    (b,) = _two_pages(env)[:1]
    out = _merge(env, a, b)
    st = state(env)
    froms = st.pages[out.result["new_page"]].merged_from
    assert [(m.page_id, m.version) for m in froms] == [
        (a, page_fingerprint(st.pages[a])), (b, page_fingerprint(st.pages[b]))]
    assert "z 分数怎么读" in froms[0].text


def test_an_original_edited_while_the_merge_waited_stops_it_and_keeps_both(env):
    a, b = _two_pages(env)
    out = _merge(env, a, b)
    new = out.result["new_page"]
    body = _written(env).page
    snap = env.repo.load("u1")                               # the learner edits a meanwhile
    edited = snap.state.pages[a].model_copy(update={"page": body})
    env.repo.commit(env.repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=env.ids("wev"), ts=NOW, body=PageSaved(record=edited, action="organized"))]))
    _write_new(env, new, body)
    ex = env.ex["page_merge"]
    assert ex.settle("u1", env.ledger.get("u1", out.action_id)) is True
    assert not state(env).trashed_pages                       # nothing moved
    row = env.ledger.get("u1", out.action_id)
    assert row.status == "executed" and "「页甲」又改过" in row.detail and can_undo(row)
    assert ex.settle("u1", row) is False                      # stopped once, not retried
    env.ledger.undo("u1", out.action_id)
    assert state(env).trashed_pages == {new}                  # the learner chose the originals


def test_a_settled_merge_replaces_the_originals_rows_and_undo_restores_them(env):
    a, b = _two_pages(env)
    _on_path(env, a, b)
    out = _merge(env, a, b)
    new = out.result["new_page"]
    assert row_keys(env) == [P1, P2, P3]                      # not written yet: the path waits
    _write_new(env, new)
    assert env.ex["page_merge"].settle("u1", env.ledger.get("u1", out.action_id))
    st = state(env)
    rec = st.paths[PATH_ID]
    items = shown(rec)
    assert st.trashed_pages == {a, b}
    assert len(items) == 2 and items[0].title == "合并页" and items[1].key == P3
    assert rec.page_ids[[p.page_id for p in rec.path.pages].index(items[0].key)] == new
    assert items[0].node_ids == ("n1",)                       # the first original's row's node
    env.ledger.undo("u1", out.action_id)
    st = state(env)
    assert row_keys(env) == [P1, P2, P3] and st.trashed_pages == {new}
    assert st.paths[PATH_ID].page_ids[:2] == (a, b)


# --- page_practice ------------------------------------------------------------------------

def _block_run(bid="kana", sid="s2", i=1) -> list[dict]:
    """The writer's practice run as scripted: one block on `sid`, then its submit."""
    return [turn(tool_call("write_block", i, block_id=bid, section_id=sid, why="看懂：z 随 x 怎么变",
                      block=PRACTICE_BLOCK)),
            turn(tool_call("finish", i, interactive_id=bid))]


def _body(env, page_id, sid="s2"):
    return state(env).pages[page_id].page.texts[sid].body


def _drafted(env, page_id, kind="interactive", covers="z 分数", run=None):
    """The practice composed now (as `add_practice` does when it proposes): the row's content."""
    env.ds.write(*(run or _block_run()))
    d = env.practice.compose("u1", page_id, kind, covers, f"k:{next(_n)}")
    return {"page_id": page_id, "kind": kind, "covers": covers,
            "draft": d.model_dump(mode="json")}, d


def test_compose_checks_the_block_and_writes_nothing(env):
    a = _written(env).page_id
    before = _body(env, a)
    _, d = _drafted(env, a)
    assert d.section_id == "s2" and d.check.ok and d.block_id.startswith("practice-")
    assert d.why == "看懂：z 随 x 怎么变" and d.block is not None and d.asset is None
    assert d.base_version == page_fingerprint(state(env).pages[a])
    assert d.cost_usd == pytest.approx(0.014)                 # the run's two writer turns
    assert env.checker.checked == ["kana"]                     # checked once, when it was placed
    assert _body(env, a) == before and env.assets.rows == {}      # nothing written yet


def test_practice_writes_the_drafted_block_in_one_commit_and_undo_removes_it(env):
    a = _written(env).page_id
    before = _body(env, a)
    content, d = _drafted(env, a)
    calls = len(env.ds.served)
    out = go(env, "page_practice", content, page_id=a, page_version=d.base_version)
    assert out.status == "executed", out.detail
    assert len(env.ds.served) == calls                              # confirming calls no model
    body = _body(env, a)
    assert body.startswith(before) and "```evoui" in body          # the block is on the page ...
    assert out.undo["block"]["fence"] in body and out.result["block_id"] == d.block_id
    [asset] = env.assets.rows.values()                              # ... and stored as an asset
    assert asset.meta.kind == "evoui" and asset.meta.page_id == a
    assert asset.meta.interactive_id == d.block_id and asset.meta.check == d.check
    assert state(env).pages[a].changed["s2"] == "inserted"
    assert_partial_write(env.repo, a, "practice", {"s2"}, producer="edit")
    undone = env.ledger.undo("u1", out.action_id)
    assert undone.status == "undone" and _body(env, a) == before


def test_a_component_practice_is_stored_when_drafted_and_embedded_on_confirm(env):
    a = _written(env).page_id
    run = [turn(interactive("pair-demo", "s2")), turn(tool_call("finish", interactive_id="pair-demo"))]
    content, d = _drafted(env, a, "interactive", "点一点", run)
    assert d.component_id == "pair-demo" and d.block is None and d.asset is not None
    [asset] = env.assets.rows.values()                              # the page shows the draft from it
    assert asset.meta.kind == "html" and asset.meta.asset_id == d.asset.asset_id
    out = go(env, "page_practice", content, page_id=a, page_version=d.base_version)
    assert out.status == "executed", out.detail
    assert d.asset.asset_id in _body(env, a)


def test_a_run_without_a_usable_interactive_drafts_nothing(env):
    """No submit in any round, or a block that never passes its render check: nothing is drafted
    and the learner gets plain words."""
    from evowiki.bot.practice import FAILED
    a = _written(env).page_id
    env.ds.write(*[turn(text="好了")] * 9)
    with pytest.raises(ActionRefused) as e:
        env.practice.compose("u1", a, "card", "不交", "k:none")
    assert e.value.detail == FAILED
    env.checker.fail_block = lambda bid, b: "overflow at 375"
    for r in range(3):           # placed, refused at submit, then the round runs out
        env.ds.write(*_block_run(i=r), *[turn(text="好了")] * 3)
    with pytest.raises(ActionRefused, match="请切换模型重试") as e:
        env.practice.compose("u1", a, "card", "渲染", "k:render")
    assert e.value.detail == FAILED and "overflow" not in e.value.detail
    assert env.assets.rows == {}


def test_a_row_without_a_readable_draft_is_refused(env):
    a = _written(env).page_id
    before = _body(env, a)
    out = go(env, "page_practice", {"page_id": a, "kind": "card"})
    assert out.status == "failed" and "草稿" in out.detail
    content, _ = _drafted(env, a)
    out = go(env, "page_practice", {**content, "page_id": "pg-other"})
    assert out.status == "failed"
    assert _body(env, a) == before and env.assets.rows == {}


def _edit(env, page_id, sid="s2", body=None, heading=None):
    """The learner edits the page after the draft: a section's text and / or its heading."""
    snap = env.repo.load("u1")
    rec = snap.state.pages[page_id]
    page = rec.page
    if body is not None:
        page = page.model_copy(update={"texts": {**page.texts, sid: page.texts[sid].model_copy(
            update={"body": body})}})
    if heading is not None:
        secs = tuple(s.model_copy(update={"heading": heading}) if s.id == sid else s
                     for s in page.outline.sections)
        page = page.model_copy(update={"outline": page.outline.model_copy(update={"sections": secs})})
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.ids("wev"), ts=NOW, body=PageSaved(
        record=rec.model_copy(update={"page": page}), action="edited"))]))


def test_practice_on_a_changed_page_is_re_anchored_and_undo_after_an_edit_is_refused(env):
    """An added practice is a local insertion: the page changed since the draft (even its own
    section's text), it still goes at the end of its section as the page is now."""
    a = _written(env).page_id
    content, d = _drafted(env, a, "card", "过时")
    _edit(env, a, body="改写过了")
    out = go(env, "page_practice", content, page_id=a, page_version=d.base_version)
    assert out.status == "executed", out.detail
    assert _body(env, a).startswith("改写过了\n\n```evoui")
    _edit(env, a, body="又改了一次")
    with pytest.raises(NotDecidable, match="又改过"):
        env.ledger.undo("u1", out.action_id)


def test_a_draft_whose_section_was_renamed_is_stale_and_redraft_composes_it_again(env):
    """A renamed section is another section: 确认 refuses (stale, nothing written); 重试
    (`ActionLedger.redraft`) composes the same request against the page as it is now, on the same
    row, which then confirms."""
    a = _written(env).page_id
    content, d = _drafted(env, a, "card", "改前")
    row = env.ledger.propose("u1", kind="page_practice", scope=SCOPES["page_practice"][0],
                             source="assistant", summary="p", why="", content=content,
                             key=f"page_practice:{next(_n)}", page_id=a, page_version=d.base_version)
    _edit(env, a, heading="换了个标题")
    out = env.ledger.approve("u1", row.action_id)
    assert out.status == "failed" and out.detail == STALE_DETAIL
    assert "```evoui" not in _body(env, a) and env.assets.rows == {}
    env.ds.write(*_block_run())
    again = env.ledger.redraft("u1", row.action_id)
    assert again.action_id == row.action_id and again.status == "proposed" and again.detail == ""
    assert again.content["draft"]["section_heading"] == "换了个标题"
    assert again.page_version == page_fingerprint(state(env).pages[a])
    assert env.ledger.approve("u1", row.action_id).status == "executed"
    with pytest.raises(NotDecidable):
        env.ledger.redraft("u1", row.action_id)             # decided: nothing to compose again


def test_practice_on_an_unwritten_page_or_a_bad_kind_is_refused(env):
    a, _ = _two_pages(env)
    with pytest.raises(ActionRefused, match="还没写好"):
        env.practice.compose("u1", a, "card", "", "k:unwritten")
    assert go(env, "page_practice", {"page_id": a, "kind": "dance"}).status == "failed"
    assert env.assets.rows == {}


# --- path_edit ----------------------------------------------------------------------------

def test_reorder_brings_the_listed_rows_first_and_undo_restores_the_order(env):
    out = go(env, "path_edit", {"path_id": PATH_ID, "op": "reorder",
                                "pages": [{"key": P3}, {"key": P1}]})
    assert out.status == "executed", out.detail
    assert row_keys(env) == [P3, P1, P2]
    assert env.fakes.requests == []                           # no body is written for a reorder
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    assert row_keys(env) == [P1, P2, P3]


def test_postpone_moves_rows_after_another_row_or_to_the_end(env):
    out = go(env, "path_edit", {"path_id": PATH_ID, "op": "postpone",
                                "pages": [{"key": P1}], "after": P2})
    assert row_keys(env) == [P2, P1, P3]
    env.ledger.undo("u1", out.action_id)
    assert row_keys(env) == [P1, P2, P3]
    go(env, "path_edit", {"path_id": PATH_ID, "op": "postpone", "pages": [{"key": P1}]})
    assert row_keys(env) == [P2, P3, P1]


def test_path_edit_draft_drops_a_row_from_the_move(env):
    out = go(env, "path_edit", {"path_id": PATH_ID, "op": "reorder",
                                "pages": [{"key": P3}, {"key": P2}]},
             draft={"pages": [{"key": P2}]})
    assert out.status == "executed" and row_keys(env) == [P2, P1, P3]
    # a draft cannot add a page to a path edit
    out = go(env, "path_edit", {"path_id": PATH_ID, "op": "reorder", "pages": [{"key": P1}]}, draft={"pages": [{"title": "新"}]})
    assert out.status == "failed"


def test_path_edit_refusals_and_noop(env):
    noop = go(env, "path_edit", {"path_id": PATH_ID, "op": "reorder", "pages": [{"key": P1}]})
    assert noop.status == "failed" and "已经是这个顺序" in noop.detail
    gone = go(env, "path_edit", {"path_id": PATH_ID, "op": "reorder", "pages": [{"key": "x"}]})
    assert gone.status == "failed" and gone.detail == STALE_DETAIL
    assert go(env, "path_edit", {"path_id": "nope", "op": "reorder", "pages": [{"key": P1}]}).status == "failed"
    assert go(env, "path_edit", {"path_id": PATH_ID, "op": "shuffle", "pages": [{"key": P1}]}).status == "failed"


def test_reorder_undo_is_refused_after_a_later_edit_of_a_moved_row(env):
    out = go(env, "path_edit", {"path_id": PATH_ID, "op": "reorder", "pages": [{"key": P3}]})
    go(env, "path_edit", {"path_id": PATH_ID, "op": "postpone", "pages": [{"key": P3}]})
    with pytest.raises(NotDecidable, match="后面的路径修改"):
        env.ledger.undo("u1", out.action_id)


def test_path_edit_hooks_see_the_assistant_as_the_source(env):
    from evowiki.wikiapp.path_edits import off_path_changed, on_path_changed
    seen = []

    def hook(user_id, path_id, before, after, source):
        seen.append(source)
    on_path_changed(hook)
    try:
        go(env, "path_edit", {"path_id": PATH_ID, "op": "reorder", "pages": [{"key": P3}]})
    finally:
        off_path_changed(hook)
    assert seen == ["assistant"]


# --- goals --------------------------------------------------------------------------------

def test_deadline_is_stored_and_undone_and_pulls_the_review_horizon(env):
    out = go(env, "goal_deadline", {"goal_id": PATH_ID, "deadline": "2026-11-03"})
    assert out.status == "executed", out.detail
    st = state(env)
    assert st.goal_settings[PATH_ID].deadline == date(2026, 11, 3)
    days = deadline_days(st, NOW, TZ)
    assert set(days) == {"n1", "n2", "n3", "n4"} and 21.0 < days["n1"] < 22.0
    # D16: the date ends at 23:59:59 in the user's zone (Shanghai: 15:59:59 UTC), not in UTC
    assert deadline_days(st, datetime(2026, 11, 3, 15, 59, 58, tzinfo=UTC), TZ)["n1"] > 0
    assert deadline_days(st, datetime(2026, 11, 3, 16, 0, tzinfo=UTC), TZ) == {}  # past: no effect
    assert set(deadline_days(st, datetime(2026, 11, 3, 16, 0, tzinfo=UTC), "UTC")) == {
        "n1", "n2", "n3", "n4"}
    with pytest.raises(KeyError):                 # an unknown zone fails, it is never guessed
        deadline_days(st, NOW, "Mars/Base")
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    assert state(env).goal_settings[PATH_ID].deadline is None and deadline_days(state(env), NOW, TZ) == {}


def test_deadline_refusals(env):
    for goal_id, deadline in ((PATH_ID, "2026-10-01"), (PATH_ID, "soon"), ("nope", "2026-12-01")):
        out = go(env, "goal_deadline", {"goal_id": goal_id, "deadline": deadline})
        assert out.status == "failed" and out.detail
    first = go(env, "goal_deadline", {"goal_id": PATH_ID, "deadline": "2026-12-01"})
    assert go(env, "goal_deadline", {"goal_id": PATH_ID, "deadline": "2026-12-01"}).status == "failed"
    go(env, "goal_deadline", {"goal_id": PATH_ID, "deadline": "2026-12-05"})
    with pytest.raises(NotDecidable, match="又改过"):
        env.ledger.undo("u1", first.action_id)


def _parked(env):
    from evowiki.store import bot_goals
    with env.pool.connection() as conn:
        return set(bot_goals.parked(conn, "u1"))


def test_pause_and_resume_use_the_bots_parked_goal_state(env):
    from evowiki.bot.facts import load_facts
    from evowiki.bot.goals import list_goals
    out = go(env, "goal_pause", {"goal_id": PATH_ID, "paused": True})
    assert out.status == "executed", out.detail
    assert _parked(env) == {PATH_ID}                       # the same table GET /wiki/bot/goals reads
    [g] = [g for g in list_goals(env.pool, "u1", now=NOW) if g.path_id == PATH_ID]
    assert g.parked
    with env.pool.connection() as conn:                    # B5's rule: a parked goal has no card
        assert load_facts(conn, "u1", now=NOW, settings=BotSettings()).goals == ()
    again = go(env, "goal_pause", {"goal_id": PATH_ID, "paused": True})
    assert again.status == "failed" and "已经是暂停" in again.detail
    resumed = go(env, "goal_pause", {"goal_id": PATH_ID, "paused": False})
    assert resumed.status == "executed" and _parked(env) == set()
    with pytest.raises(NotDecidable, match="又改过"):          # the pause was lifted since
        env.ledger.undo("u1", out.action_id)
    assert env.ledger.undo("u1", resumed.action_id).status == "undone"
    assert _parked(env) == {PATH_ID}
    assert env.ledger.undo("u1", go(env, "goal_pause", {"goal_id": PATH_ID, "paused": False}).action_id
                           ).status == "undone"
    assert _parked(env) == {PATH_ID}                       # undo of a resume parks again


# a goal_new row's blocks as the chat's draft stores them (assistant/tools/actions._blocks_content)
Z_BLOCKS = [{"key": "b1", "name": "z 分数", "covers": ["计算", "解读"], "target": True},
            {"key": "b2", "name": "标准差", "covers": ["离散程度"]}]
BOOK_BLOCKS = [{"key": "b1", "name": "向量加法", "covers": ["加法"], "target": True,
                "sections": ["§1.1"]},
               {"key": "b2", "name": "数乘", "covers": ["数乘"], "sections": ["§1.2"]}]
BY_BOOK = "按《线性代数讲义》学"


def test_goal_new_commits_with_the_effect_and_undo_archives_it(env):
    s = Script()
    env.scripts["s"] = s
    from tests.wikiapp.goal_script import auto_probe
    auto_probe(s)
    out = go(env, "goal_new", {"goal": GOAL, "words": ["我会算 z 分数"], "blocks": Z_BLOCKS})
    assert out.status == "executed", out.detail
    assert out.result["open"] and out.result["draft_id"] and s.calls("goals.intake") == 0
    draft_id = out.result["draft_id"]
    assert draft_id in state(env).drafts and out.undo == {"draft_id": draft_id}
    undone = env.ledger.undo("u1", out.action_id)
    assert undone.status == "undone"
    st = state(env)
    assert draft_id in st.closed_drafts and any(g.archived for g in st.goal_settings.values())


@pytest.mark.parametrize("has_draft", [True, False])
def test_goal_new_undo_archives_later_paths_and_cancels_their_writes(env, has_draft):
    from evowiki.goals.regions import GoalMap, MapNode
    from evowiki.wikiapp.events import DraftClosed, GoalMapSaved
    from tests.wikiapp.goal_script import auto_probe

    auto_probe(env.scripts["s"])
    out = go(env, "goal_new", {"goal": GOAL, "blocks": Z_BLOCKS})
    draft_id = out.result["draft_id"]
    # Diagnosis finishes and a child path is planned after the original action.
    m = GoalMap(map_id="later-map", goal=GOAL, created_at=NOW, nodes=(
        MapNode(id="parent", kind="region", name="父块"),
        MapNode(id="child", kind="concept", name="子块", parent="parent", path_id=PATH_ID),))
    from evowiki.wikiapp.paths import next_path_page
    page_id = next_path_page(env.repo, "u1", PATH_ID, now=NOW, new_id=env.ids).page_id
    before = state(env).paths[PATH_ID]
    env.repo.commit(env.repo.load("u1"), Commit(wiki=[
        WikiEvent(event_id=env.ids("wev"), ts=NOW, body=GoalMapSaved(record=m, action="planned")),
        WikiEvent(event_id=env.ids("wev"), ts=NOW, body=DraftClosed(
            draft_id=draft_id, outcome="answered", map_id=m.map_id)),
    ]))
    if not has_draft:
        with env.pool.connection() as conn:
            store.set_action_undo(conn, out.action_id, {"draft_id": "no-draft", "map_id": m.map_id})
    undone = env.ledger.undo("u1", out.action_id)
    assert undone.status == "undone", undone.detail
    st = state(env)
    assert st.goal_settings[m.map_id].archived and st.goal_settings[PATH_ID].archived
    assert env.fakes.cancels == [[page_id]]
    assert st.paths[PATH_ID] == before
    assert page_id in st.pages and page_id not in st.trashed_pages


def test_goal_new_without_blocks_or_a_reading_is_refused(env):
    """A row from before chat-first blocks: no S1 at confirm any more (spec 2026-10-28 §2.8)."""
    before = set(state(env).drafts)
    out = go(env, "goal_new", {"goal": GOAL})
    assert out.status == "failed" and out.detail == NO_BLOCKS
    assert set(state(env).drafts) == before


def test_goal_new_without_a_gateway_is_refused(pool):
    make_user(pool, "u1")
    ids = Ids()
    repo = PgRepo(pool)
    with pool.connection() as conn:
        store.put_settings(conn, "u1", ALL_ASK, NOW)
    ledger = ActionLedger(pool, lambda: NOW, ids, {})
    fakes = Fakes()
    register_assistant_executors(ledger, repo, lambda: NOW, ids, pool=pool,
                                 request_write=fakes.request_write,
                                 cancel_writes=fakes.cancel_writes, gateway=lambda uid: None)
    row = ledger.propose("u1", kind="goal_new", scope="structure", source="assistant",
                         summary="s", why="", content={"goal": "学点东西", "blocks": Z_BLOCKS},
                         key="g1")
    out = ledger.approve("u1", row.action_id)
    assert out.status == "failed" and "模型" in out.detail


def _material(env, status="ready", sid="s1"):
    """A material of u1 whose sections carry the labels §1.1 / §1.2 (tests/sources/test_store)."""
    from dataclasses import replace

    from evowiki.sources import store as source_store
    from tests.sources.test_store import _sec, _src
    secs = [replace(_sec("sec11", 0), title="1.1 向量加法"), replace(_sec("sec12", 1), title="1.2 数乘")]
    with env.pool.connection() as conn:
        source_store.insert_source(conn, _src(sid, status=status, title="线性代数讲义",
                                              toc_mode="headings"), b"x", "text/markdown")
        source_store.replace_sections(conn, "u1", sid, secs)
    return sid


def test_goal_new_with_a_material_writes_the_links_and_use_in_its_commit(env):
    from evowiki.sources import store as source_store
    from tests.wikiapp.goal_script import auto_probe
    sid = _material(env)
    s = Script()
    env.scripts["s"] = s
    auto_probe(s)
    out = go(env, "goal_new", {"goal": BY_BOOK, "source_ids": [sid], "blocks": BOOK_BLOCKS})
    assert out.status == "executed", out.detail
    assert s.calls("goals.intake") == 0                     # the blocks as confirmed: no S1
    draft = state(env).drafts[out.result["draft_id"]]
    assert draft.sources == (sid,) and draft.source_use == "learn"
    with env.pool.connection() as conn:
        links = source_store.links_for_sources(conn, "u1", [sid])
        uses = source_store.uses_of(conn, "u1", sid)
    assert {(x.role, x.by) for x in links} == {("teaches", "blocks")}   # stage 3: block links
    assert [u["use"] for u in uses] == ["learn"]


def _attached(env, sid="s1"):
    """`_material`, attached in a chat only (use ask): its first learning use needs the full
    import (sources.uses.needs_full_run)."""
    from evowiki.sources import store as source_store
    _material(env, sid=sid)
    with env.pool.connection() as conn:
        source_store.add_use(conn, "u1", sid, "ask", session_id="chat1", now=NOW)
        source_store.set_progress(conn, "u1", sid, {"end": "done", "mode": "ask"})
    return sid


class _Runs:
    def __init__(self, fail=False):
        self.jobs, self.fail = [], fail

    def start(self, job):
        if self.fail:
            raise RuntimeError("no runner")
        self.jobs.append(job)
        return f"source:{job.user_id}:{job.source_id}:{job.gen}"


def _goal_from_attachment(env, runs):
    from tests.wikiapp.goal_script import auto_probe
    sid = _attached(env)
    env.ex["goal_new"]._runs = lambda: runs
    s = Script()
    env.scripts["s"] = s
    auto_probe(s)
    return sid, go(env, "goal_new", {"goal": BY_BOOK, "source_ids": [sid], "blocks": BOOK_BLOCKS})


def test_goal_new_from_a_chat_attachment_starts_its_full_import(env):
    from evowiki.sources import store as source_store
    runs = _Runs()
    sid, out = _goal_from_attachment(env, runs)
    assert out.status == "executed", out.detail
    assert "note" not in out.result
    assert [(j.source_id, j.gen, j.mode) for j in runs.jobs] == [(sid, 1, "full")]
    with env.pool.connection() as conn:
        uses = source_store.uses_of(conn, "u1", sid)
    assert sorted(u["use"] for u in uses) == ["ask", "learn"]


def test_goal_new_whose_full_import_cannot_start_is_executed_with_a_note(env):
    from evowiki.sources import store as source_store
    from evowiki.sources.uses import MSG_NOT_STARTED
    sid, out = _goal_from_attachment(env, _Runs(fail=True))
    assert out.status == "executed", out.detail                 # the goal is made
    assert out.result["draft_id"] in state(env).drafts
    assert out.result["note"] == "目标建好了，但资料没能开始整理"
    with env.pool.connection() as conn:
        row = source_store.get_source(conn, "u1", sid)
    assert row.progress["end"] == "error" and row.progress["detail"] == MSG_NOT_STARTED


def test_goal_new_with_a_material_still_reading_fails_with_the_routes_text(env):
    from evowiki.sources import store as source_store
    sid = _material(env, status="reading")
    s = Script()
    env.scripts["s"] = s
    before = set(state(env).drafts)
    out = go(env, "goal_new", {"goal": "学向量", "source_ids": [sid], "blocks": BOOK_BLOCKS})
    assert out.status == "failed" and out.detail == "资料还在读，读完再开始"
    assert s.calls() == 0 and set(state(env).drafts) == before
    with env.pool.connection() as conn:
        assert source_store.uses_of(conn, "u1", sid) == []


def test_goal_new_with_an_unknown_material_fails_and_writes_nothing(env):
    before = set(state(env).drafts)
    out = go(env, "goal_new", {"goal": BY_BOOK, "source_ids": ["nope"], "blocks": BOOK_BLOCKS})
    assert out.status == "failed" and out.detail == "找不到这份资料"
    assert set(state(env).drafts) == before


def test_goal_new_reports_an_unusable_material_before_the_model_check(pool):
    """Final review #13: no gateway AND a material still being read → the material's line."""
    make_user(pool, "u1")
    ids = Ids()
    with pool.connection() as conn:
        store.put_settings(conn, "u1", ALL_ASK, NOW)
    ledger = ActionLedger(pool, lambda: NOW, ids, {})
    fakes = Fakes()
    register_assistant_executors(ledger, PgRepo(pool), lambda: NOW, ids, pool=pool,
                                 request_write=fakes.request_write,
                                 cancel_writes=fakes.cancel_writes, gateway=lambda uid: None)
    env = type("E", (), {"pool": pool})()
    sid = _material(env, status="reading")
    row = ledger.propose("u1", kind="goal_new", scope="structure", source="assistant",
                         summary="s", why="",
                         content={"goal": "学", "source_ids": [sid], "blocks": BOOK_BLOCKS},
                         key="g1")
    out = ledger.approve("u1", row.action_id)
    assert out.status == "failed" and out.detail == "资料还在读，读完再开始"


def test_load_materials_with_no_ids_opens_no_connection():
    """Final review #12: no material, no pool needed."""
    from evowiki.wikiapp.goal_sources import load_materials
    assert load_materials(None, "u1", []) == []


# --- state folds --------------------------------------------------------------------------

def test_new_events_fold_into_the_state_and_survive_a_json_round_trip(env):
    from pydantic import TypeAdapter

    from evowiki.wikiapp.events import GoalSettings, GoalSettingsSet
    from evowiki.wikiapp.state import apply_event

    ta = TypeAdapter(list[WikiEvent])
    evs = [WikiEvent(event_id="e1", ts=NOW, body=GoalSettingsSet(settings=GoalSettings(
               goal_id="g", skippable=("k",), deadline=date(2026, 12, 1)))),
           WikiEvent(event_id="e2", ts=NOW, body=PagesTrashed(page_ids=("a", "b"))),
           WikiEvent(event_id="e3", ts=NOW, body=PagesTrashed(page_ids=("a",), trashed=False))]
    st = state(env)
    for ev in ta.validate_json(ta.dump_json(evs)):
        st = apply_event(st, ev)
    assert st.goal_settings["g"].deadline == date(2026, 12, 1) and st.goal_settings["g"].skippable
    assert st.trashed_pages == {"b"}


# --- fix round 1 ----------------------------------------------------------------------------

def _know(env, *concepts):
    from evowiki.belief.aspects import Declare
    env.repo.commit(env.repo.load("u1"), Commit(aspect=[
        Declare(event_id=env.ids("aev"), ts=NOW, cell=f"{c}.{a}", value="know")
        for c in concepts for a in ("term", "purpose")]))


def test_a_deadline_keeps_the_stored_order_known_rows_show_skippable_and_undo_restores(env):
    # skill design A7: a deadline is about time only; 可以跳过 comes from K, the row stays enterable
    from evowiki.wikiapp.paths import next_path_page, path_items
    _know(env, "n1")
    out = go(env, "goal_deadline", {"goal_id": PATH_ID, "deadline": "2026-11-03"})
    assert out.status == "executed", out.detail
    assert row_keys(env) == [P1, P2, P3] and "reordered" not in out.result
    snap = env.repo.load("u1")
    items = path_items(snap.state, snap.state.paths[PATH_ID], snap.k, NOW)
    assert [(i.key, i.skippable, i.skipped) for i in items] == [
        (P1, True, False), (P2, False, False), (P3, False, False)]
    assert snap.state.goal_settings[PATH_ID].skippable == ()           # nothing stored for it
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    snap = env.repo.load("u1")
    assert row_keys(env) == [P1, P2, P3]
    assert snap.state.goal_settings[PATH_ID].deadline is None
    assert deadline_days(snap.state, NOW, TZ) == {}                      # the review horizon is back
    # the learner can still choose the skippable row: 「下一页」 starts it
    started = next_path_page(env.repo, "u1", PATH_ID, now=NOW, new_id=env.ids)
    assert started.index == 1
    snap = env.repo.load("u1")
    after = path_items(snap.state, snap.state.paths[PATH_ID], snap.k, NOW)
    assert after[0].page_id == started.page_id and not after[0].skippable   # started: no hint


def test_the_learners_own_order_stays_when_k_changes_and_a_deadline_is_set(env):
    from evowiki.wikiapp.path_edits import commit_path_edit
    commit_path_edit(env.repo, "u1", PATH_ID,
                     [AddEdit(page_key="u-mine", title="我自己的", node_id=None),
                      MoveEdit(page_key="u-mine", to_index=0), MoveEdit(page_key=P3, to_index=1)],
                     now=NOW, new_id=env.ids, source="user")
    order = ["u-mine", P3, P1, P2]
    assert row_keys(env) == order
    _know(env, "n1", "n2", "n3", "n4")
    assert row_keys(env) == order
    go(env, "goal_deadline", {"goal_id": PATH_ID, "deadline": "2026-11-03"})
    assert row_keys(env) == order


def test_deadline_undo_keeps_a_later_order_change(env):
    out = go(env, "goal_deadline", {"goal_id": PATH_ID, "deadline": "2026-11-03"})
    go(env, "path_edit", {"path_id": PATH_ID, "op": "reorder", "pages": [{"key": P3}]})
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    assert row_keys(env) == [P3, P1, P2]
    assert state(env).goal_settings[PATH_ID].deadline is None


def test_a_big_goal_setting_is_fanned_out_to_its_paths(env):
    from evowiki.wikiapp.path_edits import map_paths
    from tests.wikiapp.test_path_edits import MAP_ID
    assert map_paths(state(env), MAP_ID) == [PATH_ID]
    out = go(env, "goal_deadline", {"goal_id": MAP_ID, "deadline": "2026-11-03"})
    assert out.status == "executed" and out.result["paths"] == [PATH_ID]
    assert state(env).goal_settings[PATH_ID].deadline == date(2026, 11, 3)
    assert set(deadline_days(state(env), NOW, TZ)) == {"n1", "n2", "n3", "n4"}
    env.ledger.undo("u1", out.action_id)
    paused = go(env, "goal_pause", {"goal_id": MAP_ID, "paused": True})
    assert paused.status == "executed" and _parked(env) == {PATH_ID}
    assert env.ledger.undo("u1", paused.action_id).status == "undone"
    assert _parked(env) == set()


def test_a_big_goal_without_a_path_is_refused_not_ignored(env):
    from evowiki.goals.regions import GoalMap, MapNode
    from evowiki.wikiapp.events import GoalMapSaved
    m = GoalMap(map_id="map-empty", goal="空", created_at=NOW, nodes=(
        MapNode(id="r2", kind="region", name="空区", state="collapsed"),))
    env.repo.commit(env.repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=env.ids("wev"), ts=NOW, body=GoalMapSaved(record=m, action="planned"))]))
    for kind, content in (("goal_deadline", {"goal_id": "map-empty", "deadline": "2026-11-03"}),
                          ("goal_pause", {"goal_id": "map-empty", "paused": True})):
        out = go(env, kind, content)
        assert out.status == "failed" and "还没有开始学" in out.detail


def test_undone_split_and_new_page_leave_the_graph_as_before(env):
    ids_before = [n.id for n in state(env).graph.nodes]
    out = go(env, "page_split", {"path_id": PATH_ID, "page_key": P2, "pages": parts(A, B)})
    assert len(state(env).graph.nodes) == len(ids_before) + 1 and len(out.undo["nodes"]) == 1
    env.ledger.undo("u1", out.action_id)
    assert [n.id for n in state(env).graph.nodes] == ids_before
    new = go(env, "page_new", {"mode": "page", "path_id": PATH_ID, "pages": [A]})
    assert len(new.undo["nodes"]) == 1 and len(state(env).graph.nodes) == len(ids_before) + 1
    env.ledger.undo("u1", new.action_id)
    assert [n.id for n in state(env).graph.nodes] == ids_before and row_keys(env) == [P1, P2, P3]


def test_undo_is_refused_when_a_created_node_is_used_elsewhere(env):
    out = go(env, "page_new", {"mode": "page", "path_id": PATH_ID, "pages": [A]})
    node = out.undo["nodes"][0]
    go(env, "page_new", {"mode": "page", "pages": [{"title": "借用", "node_id": node}]})
    with pytest.raises(NotDecidable, match="别处用到"):
        env.ledger.undo("u1", out.action_id)
    assert len(row_keys(env)) == 4


def test_merge_undo_restores_only_what_this_action_trashed(env):
    a, b = _two_pages(env)
    out = go(env, "page_merge", {"title": "合并页", "pages": [{"key": a}, {"key": b}]})
    new = out.result["new_page"]
    env.repo.commit(env.repo.load("u1"), Commit(wiki=[WikiEvent(    # the learner trashed b meanwhile
        event_id=env.ids("wev"), ts=NOW, body=PagesTrashed(page_ids=(b,)))]))
    body = _written(env).page
    snap = env.repo.load("u1")
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.ids("wev"), ts=NOW, body=PageSaved(
        record=snap.state.pages[new].model_copy(update={"page": body}), action="organized"))]))
    assert env.ex["page_merge"].settle("u1", env.ledger.list("u1", status="executed")[0])
    assert state(env).trashed_pages == {a, b}
    env.ledger.undo("u1", out.action_id)
    assert state(env).trashed_pages == {b, new}                       # b stays where the learner put it


def test_path_edit_is_stale_when_the_path_changed_since_the_proposal(env):
    from evowiki.bot.executors_assistant import path_fingerprint
    from evowiki.wikiapp.path_edits import commit_path_edit
    seen = path_fingerprint(state(env).paths[PATH_ID])
    commit_path_edit(env.repo, "u1", PATH_ID, [MoveEdit(page_key=P3, to_index=0)],
                     now=NOW, new_id=env.ids, source="user")
    content = {"path_id": PATH_ID, "op": "reorder", "pages": [{"key": P1}], "path_seen": seen}
    out = go(env, "path_edit", content)
    assert out.status == "failed" and out.detail == STALE_DETAIL
    fresh = {**content, "path_seen": path_fingerprint(state(env).paths[PATH_ID])}
    assert go(env, "path_edit", fresh).status == "executed"


def test_an_add_dropped_for_a_node_already_on_the_path_creates_no_page(env):
    n_pages = len(state(env).pages)
    out = go(env, "page_new", {"mode": "page", "path_id": PATH_ID, "pages": [
        {"title": "第 1 页", "node_id": "n1"}, B, {"title": "又是它", "node_id": "n5"}]})
    assert out.status == "executed", out.detail
    assert len(out.result["new_pages"]) == 1 and len(state(env).pages) == n_pages + 1
    assert env.fakes.requests == [out.result["new_pages"]]    # page_new: an explicit ask, written now


def test_made_pages_are_keyed_by_draft_key_with_their_path_position(env):
    out = go(env, "page_new", {"mode": "page", "path_id": PATH_ID, "index": 0, "pages": [
        {"key": "d1", "title": "第 1 页", "node_id": "n1"}, {"key": "d2", **B}, {"key": "d3", "title": "又是它", "node_id": "n5"}]})
    assert out.status == "executed", out.detail
    # d1's node is on the path already (no page made); d3 repeats d2's node: only d2 made a page
    assert [(m["key"], m["n"]) for m in out.result["made"]] == [("d2", 1)]
    assert [m["page_id"] for m in out.result["made"]] == out.result["new_pages"]
    split = go(env, "page_split", {"path_id": PATH_ID, "page_key": P2, "pages": parts(A, B)})
    assert [(m["key"], m["n"]) for m in split.result["made"]] == [("k0", 3), ("k1", 4)]


def test_a_merge_records_the_made_page_with_the_originals_folder(env):
    # see test_merge_writes_the_new_page_first...: the same setup
    pids = _two_pages(env)
    out = go(env, "page_merge", {"title": "合并", "pages": [{"key": p} for p in pids]})
    assert out.status == "executed", out.detail
    [m] = out.result["made"]
    assert m["page_id"] == out.result["new_page"] and m["key"] is None and m["n"] is None
    assert m["folder"] == state(env).pages[pids[0]].folder


def test_a_path_action_without_path_seen_is_refused(env):
    for kind, content in (
            ("page_split", {"path_id": PATH_ID, "page_key": P2, "pages": parts(A)}),
            ("page_new", {"mode": "page", "path_id": PATH_ID, "pages": [A]}),
            ("path_edit", {"path_id": PATH_ID, "op": "reorder", "pages": [{"key": P3}]})):
        out = go(env, kind, content, seen=False)
        assert out.status == "failed" and "没有记下提议时的路径" in out.detail, kind
    assert row_keys(env) == [P1, P2, P3] and env.fakes.requests == []


def test_path_actions_use_the_path_scope_and_the_router_refuses_unknown_ops(env):
    from evowiki.bot.executors_assistant import PathEditRouter
    assert SCOPES["path_edit"] == ("path",) and "path" in SCOPES["goal_deadline"]
    with env.pool.connection() as conn:
        store.put_settings(conn, "u1", BotSettings(levels={"path": "ask", "structure": "auto_undo"}),
                           NOW)
    assert action_level(env.ledger, "u1", "path_edit") == ("ask", "path")
    assert action_level(env.ledger, "u1", "goal_deadline") == ("ask", "path")
    router = PathEditRouter(env.ex["path_edit"], object())
    row = type("R", (), {"content": {"op": "shuffle"}})()
    for call in (lambda: router.execute("u1", row, None), lambda: router.undo("u1", row)):
        with pytest.raises(ActionRefused, match="不认识"):
            call()


# --- the row's question (`covers` of the assistant's page spec) ----------------------------

def test_split_and_new_page_rows_keep_the_question_and_the_new_node_has_no_definition(env):
    q = "同样的总体为什么每次抽出来不一样"
    out = go(env, "page_split", {"path_id": PATH_ID, "page_key": P2, "pages": parts({"title": "抽样的随机性", "covers": q}, B)})
    assert out.status == "executed", out.detail
    rows = shown(state(env).paths[PATH_ID])
    assert [i.question for i in rows[1:3]] == [q, ""]
    nid = rows[1].node_ids[0]
    assert state(env).graph.node(nid).definition == ""          # the question is not a definition
    q2 = "多抽几次，平均数会怎么跳"
    out = go(env, "page_new", {"mode": "page", "path_id": PATH_ID, "index": 0,
                               "pages": [{"title": "新的一页", "covers": q2}]})
    assert out.status == "executed", out.detail
    assert shown(state(env).paths[PATH_ID])[0].question == q2


def test_goal_limits_replace_the_record_and_undo_puts_them_back(env):
    """S1's goal record: `goal_limits` replaces the map's limits (one GoalMapSaved "edited");
    undo puts the old list back, refused once they were changed again."""
    from evowiki.wikiapp.events import GoalMapSaved, WikiEvent
    m = next(iter(state(env).maps.values()), None)
    if m is None:
        from evowiki.goals.regions import GoalMap, MapNode
        m = GoalMap(map_id="map-l", goal="学统计", created_at=NOW,
                    nodes=(MapNode(id="b1", kind="region", name="一块"),))
    m = m.model_copy(update={"limits": ("尽快读到第一页",)})
    env.repo.commit(env.repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=env.ids("wev"), ts=NOW, body=GoalMapSaved(record=m, action="planned"))]))
    now = {"level": "auto_undo", "approved_now": True}     # as set_goal_limits records it
    out = go(env, "goal_limits", {"goal_id": m.map_id, "limits": ["不急", " "]}, **now)
    assert out.status == "executed", out.detail
    assert state(env).maps[m.map_id].limits == ("不急",)
    assert env.ledger.undo("u1", out.action_id).status == "undone"
    assert state(env).maps[m.map_id].limits == ("尽快读到第一页",)
    again = go(env, "goal_limits", {"goal_id": m.map_id, "limits": ["只用 Windows"]}, **now)
    go(env, "goal_limits", {"goal_id": m.map_id, "limits": []}, **now)
    with pytest.raises(NotDecidable, match="之后又改过了"):
        env.ledger.undo("u1", again.action_id)
