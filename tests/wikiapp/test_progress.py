"""One source of progress (usability reset item 1, wikiapp.progress): page states, a goal done only
when every block is, 今天 and the end of a page agree. Acceptance cases from the 2026-10-27 Codex
audits: U2, D04, D05, D22, D25, D38–D40. Scripted LLM only."""

from datetime import timedelta

import pytest

from evowiki.belief.aspects import AspectKView, Declare
from evowiki.goals.blocks import Block
from evowiki.goals.contracts import Band, Observation
from evowiki.goals.observe import SELF_DEFAULT_STRENGTH
from evowiki.goals.path_edit import MoveEdit
from evowiki.wikiapp import progress
from evowiki.wikiapp.events import PageSaved, PagesTrashed, WikiEvent, WritingMark
from evowiki.wikiapp.goal import _taught, create_goal_from_blocks, enter_region, stop_draft
from evowiki.wikiapp.kwrite import observation_events, page_spec
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.page import mark_done
from evowiki.wikiapp.page_run_store import PageRunRow
from evowiki.wikiapp.path_edits import commit_path_edit
from evowiki.wikiapp.paths import next_path_page, path_page_done
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict
from tests.wikiapp.goal_script import auto_probe, push_no_question, push_pages
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_state import page as a_page
from tests.wikiapp.verify import verify_prereqs

BLOCKS = tuple(Block(name=f"第{i}块", covers=(f"要点{i}",), target=i == 6) for i in range(1, 7))


@pytest.fixture
def env():
    s = Script()
    auto_probe(s)
    return MemoryRepo(["u1"]), s, Ids()


def gw(env):
    return CountingGateway(env[1].gateway())


def snap(env):
    return env[0].load("u1")


def prog(env, runs=None, *, page_id=None):
    sn = snap(env)
    kv = AspectKView(sn.k, NOW)
    if page_id is not None:
        return progress.page_progress(sn.state, kv, runs or {}, page_id)
    return progress.progress(sn.state, kv, runs or {})


def write_body(env, page_id):
    """The page gets its written body (what the organize stream commits)."""
    repo, _, ids = env
    sn = snap(env)
    rec = sn.state.pages[page_id]
    repo.commit(sn, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(
        record=rec.model_copy(update={"page": a_page(page_id)}), action="organized"))]))


def run(page_id, status, *, section=None, of=None, failure=None) -> PageRunRow:
    return PageRunRow(run_id=f"page:u1:{page_id}:1", user_id="u1", page_id=page_id, n=1,
                      kind="organize", origin="open", job={}, status=status, started_at=NOW,
                      progress=None if section is None else {"section": section, "of": of},
                      failure=failure)


def goal6(env):
    """A six-block goal (chat-first blocks) with its opening diagnosis stopped."""
    repo, _, ids = env
    res = create_goal_from_blocks(repo, gw(env), "u1", "我想学六块", BLOCKS, now=NOW,
                                  new_id=ids, title="六块")
    stop_draft(repo, gw(env), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    return res.draft.map_id


def enter(env, map_id, region, *pages):
    """Enters a block and opens the point the pages are for (spec 2026-10-28: entering splits one
    level and plans nothing; opening a point plans its path). The block splits into the pages'
    concepts (plus one part K shows known when there is only one: a split needs 2 parts); the
    last concept needs the others first, so opening it plans ONE path over all of them, hung on
    that point — no question, the scripted pages."""
    repo, s, ids = env
    want = list(dict.fromkeys(c for p in pages for c in p))
    blocks = [{"name": f"{region}-{i}", "summary": "s", "page": True} for i in range(len(want))]
    if len(want) == 1:
        blocks.append({"name": f"{region}-已会", "summary": "s", "page": True})
    s.push("goals.intake", {"title": "x", "blocks": blocks})
    shown = enter_region(repo, gw(env), "u1", map_id, region, now=NOW, new_id=ids)
    assert shown.map_id == map_id and shown.path_id is None
    kids = [n.id for n in snap(env).state.maps[map_id].children(region)]
    assert kids[:len(want)] == want
    if len(want) == 1:                                   # the filler part: K shows it known
        sn = snap(env)
        repo.commit(sn, Commit(aspect=[Declare(event_id=ids("aev"), ts=NOW, cell=f"{kids[1]}.{a}",
                                               value="know") for a in sn.k.aspects(kids[1])]))
    else:
        verify_prereqs(repo, "u1", [(c, want[-1]) for c in want[:-1]], now=NOW, new_id=ids)
        push_pages(s, *pages)
    push_no_question(s)
    return enter_region(repo, gw(env), "u1", map_id, want[-1], now=NOW, new_id=ids)


def test_a_new_goal_waits_for_its_first_block(env):
    map_id = goal6(env)
    p = prog(env)
    (g,) = p.goals
    assert (g.goal_id, g.kind, g.state) == (map_id, "map", "not_started")
    assert [b.state for b in g.blocks] == ["not_started"] * 6
    assert (p.today.kind, p.today.region_id) == ("enter_block", "c1")


def test_u2_one_block_done_is_not_the_goal_done(env):
    repo, _, ids = env
    map_id = goal6(env)
    out = enter(env, map_id, "c1", ["c7"])
    first = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
    write_body(env, first.page_id)
    path_page_done(repo, "u1", out.path_id, 1, now=NOW, new_id=ids)
    p = prog(env)
    (g,) = p.goals
    assert g.state == "learning" and [b.state for b in g.blocks][:2] == ["done", "not_started"]
    assert (p.today.kind, p.today.map_id, p.today.region_id) == ("enter_block", map_id, "c2")


def test_d05_today_follows_the_page_being_written(env):
    repo, _, ids = env
    map_id = goal6(env)
    out = enter(env, map_id, "c1", ["c7"], ["c8"])
    first = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
    # page 1 has a record but no body: planned, then writing (with how far), then failed
    assert prog(env).today.kind == "start_page" and prog(env).today.page_id == first.page_id
    writing = prog(env, {first.page_id: run(first.page_id, "writing", section=2, of=5)})
    assert writing.today.kind == "writing" and writing.today.index == 1
    assert writing.today.progress.section == 2 and writing.today.progress.of == 5
    assert writing.goals[0].state == "learning" and writing.goals[0].pages_done == 0
    failed = prog(env, {first.page_id: run(first.page_id, "failed", failure="llm_failed")})
    assert (failed.today.kind, failed.today.failure) == ("failed", "llm_failed")
    write_body(env, first.page_id)
    assert (prog(env).today.kind, prog(env).today.page_id) == ("read", first.page_id)


def test_d04_d39_an_unwritten_page_is_not_taught_and_cannot_be_finished(env):
    repo, _, ids = env
    map_id = goal6(env)
    out = enter(env, map_id, "c1", ["c7"])
    first = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
    assert _taught(snap(env).state) == {}
    with pytest.raises(WikiConflict, match="还没写好"):
        path_page_done(repo, "u1", out.path_id, 1, now=NOW, new_id=ids)
    with pytest.raises(WikiConflict, match="还没写好"):
        mark_done(repo, "u1", first.page_id, now=NOW, new_id=ids)
    assert not snap(env).state.pages[first.page_id].done
    write_body(env, first.page_id)
    assert "c7" in _taught(snap(env).state)
    # a trashed page cannot be finished through either door
    sn = snap(env)
    repo.commit(sn, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                           body=PagesTrashed(page_ids=(first.page_id,)))]))
    with pytest.raises(WikiConflict, match="回收站"):
        mark_done(repo, "u1", first.page_id, now=NOW, new_id=ids)


def test_mastered_needs_a_body(env):
    repo, _, ids = env
    map_id = goal6(env)
    out = enter(env, map_id, "c1", ["c7"])
    first = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
    sn = snap(env)
    decls = [Declare(event_id=f"d_{a}", ts=NOW, cell=f"c7.{a}", value="know")
             for a in sn.k.aspects("c7")]
    repo.commit(sn, Commit(aspect=decls))
    later = NOW + timedelta(seconds=1)
    sn = snap(env)
    assert progress.page_state(sn.state, AspectKView(sn.k, later), first.page_id, None) == "planned"
    write_body(env, first.page_id)
    sn = snap(env)
    assert progress.page_state(sn.state, AspectKView(sn.k, later), first.page_id,
                               None) == "mastered"


def test_d40_a_page_moved_last_does_not_hide_earlier_rows(env):
    repo, _, ids = env
    map_id = goal6(env)
    out = enter(env, map_id, "c1", ["c7"], ["c8"], ["c9"])
    first = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
    write_body(env, first.page_id)
    key = snap(env).state.paths[out.path_id].path.pages[0].page_id
    commit_path_edit(repo, "u1", out.path_id, [MoveEdit(page_key=key, to_index=2)], now=NOW,
                     new_id=ids, source="user")
    end = prog(env, page_id=first.page_id)
    assert end.state == "written" and end.after.kind == "start_page" and end.after.index == 2
    path_page_done(repo, "u1", out.path_id, 1, now=NOW, new_id=ids)
    p = prog(env)
    assert p.goals[0].blocks[0].state == "learning"
    assert (p.today.kind, p.today.index) == ("start_page", 2)


def test_the_end_of_a_page_and_today_agree(env):
    repo, _, ids = env
    map_id = goal6(env)
    out = enter(env, map_id, "c1", ["c7"], ["c8"])
    first = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
    write_body(env, first.page_id)
    end = prog(env, page_id=first.page_id)
    assert end.goal_id == map_id and (end.after.kind, end.after.index) == ("start_page", 2)
    path_page_done(repo, "u1", out.path_id, 1, now=NOW, new_id=ids)
    assert prog(env).today == end.after
    second = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
    write_body(env, second.page_id)
    last = prog(env, page_id=second.page_id)        # the block's last page: next block, not done
    assert (last.after.kind, last.after.region_id) == ("enter_block", "c2")


def test_a_goal_is_done_only_when_every_block_is(env):
    repo, _, ids = env
    map_id = goal6(env)
    for n, region in enumerate(("c1", "c2", "c3", "c4", "c5", "c6")):
        out = enter(env, map_id, region, [f"c{7 + 2 * n}"])     # each block: its point + a filler
        pg = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
        write_body(env, pg.page_id)
        if region != "c6":
            path_page_done(repo, "u1", out.path_id, 1, now=NOW, new_id=ids)
            assert prog(env).goals[0].state == "learning"
    assert prog(env, page_id=pg.page_id).after.kind == "goal_done"
    path_page_done(repo, "u1", out.path_id, 1, now=NOW, new_id=ids)
    p = prog(env)
    assert p.goals[0].state == "done" and p.goals[0].step.kind == "goal_done"
    assert p.today.kind == "none"


def test_d25_trashed_pages_are_not_planning_context(env):
    repo, _, ids = env
    map_id = goal6(env)
    out = enter(env, map_id, "c1", ["c7"], ["c8"])
    a = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
    b = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
    write_body(env, a.page_id)
    write_body(env, b.page_id)
    assert [o.page_id for o in page_spec(snap(env).state, snap(env).k, b.page_id,
                                         bands_only=True).other_pages] == [a.page_id]
    repo.commit(snap(env), Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                                  body=PagesTrashed(page_ids=(a.page_id,)))]))
    assert page_spec(snap(env).state, snap(env).k, b.page_id, bands_only=True).other_pages == ()
    # a trashed row is out of the way: the path goes on without it
    p = prog(env)
    assert p.goals[0].blocks[0].pages_total == 1 and p.today.page_id == b.page_id


def test_t3_a_page_being_written_is_not_taught_read_or_done(env):
    """Stage 4 T3: a run commits the outline and each section as it writes them, under a
    WritingMark. Until its final commit the page is writing / failed / planned (by its run), never
    taught, finishable or mastered, and the next step stays on it."""
    repo, _, ids = env
    map_id = goal6(env)
    out = enter(env, map_id, "c1", ["c7"])
    pid = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids).page_id
    sn = snap(env)
    part = sn.state.pages[pid].model_copy(update={
        "page": a_page(pid), "writing": WritingMark(job_id=f"page:u1:{pid}:1")})
    repo.commit(sn, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(
        record=part, action="section_written"))]))
    sn = snap(env)
    decls = [Declare(event_id=f"d_{a}", ts=NOW, cell=f"c7.{a}", value="know")
             for a in sn.k.aspects("c7")]
    repo.commit(sn, Commit(aspect=decls))            # K: every target cell known
    later = NOW + timedelta(seconds=1)
    sn = snap(env)
    kv = AspectKView(sn.k, later)
    assert progress.page_state(sn.state, kv, pid, None) == "planned"
    assert progress.page_state(sn.state, kv, pid, run(pid, "writing")) == "writing"
    assert progress.page_state(sn.state, kv, pid, run(pid, "failed")) == "failed"
    assert _taught(sn.state) == {}
    with pytest.raises(WikiConflict, match="还没写好"):
        mark_done(repo, "u1", pid, now=NOW, new_id=ids)
    with pytest.raises(WikiConflict, match="还没写好"):
        path_page_done(repo, "u1", out.path_id, 1, now=NOW, new_id=ids)
    p = prog(env, {pid: run(pid, "writing", section=1, of=3)})
    assert (p.today.kind, p.today.page_id) == ("writing", pid)
    assert p.goals[0].pages_done == 0
    assert prog(env, {pid: run(pid, "writing")}, page_id=pid).state == "writing"
    # the run's final commit clears the mark: written, taught
    sn = snap(env)
    done = sn.state.pages[pid].model_copy(update={"writing": None})
    repo.commit(sn, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(
        record=done, action="organized"))]))
    assert "c7" in _taught(snap(env).state)
    sn = snap(env)
    assert progress.page_state(sn.state, AspectKView(sn.k, later), pid, None) == "mastered"


def _observe(env, cell, polarity="+", strength=1.0, kind="quiz", n=3):
    repo, _, ids = env
    obs = [Observation(cell=cell, polarity=polarity, strength=strength, kind=kind,
                       evidence_ref=f"t:{cell}:{polarity}:{i}", quote="会") for i in range(n)]
    repo.commit(snap(env), Commit(aspect=observation_events(
        obs, ts=NOW, new_id=ids, note_text=lambda o: "x")))


def test_a_block_is_known_only_when_k_has_every_cell_learned(env):
    """The `me` shape (a chat-first block that is a K concept itself, no members): term right in a
    quiz, purpose only self-reported (shaky) → not known, 今天 is entering it. Once K has every
    cell likely+ it is known (今天 moves on); the map never stores it."""
    map_id = goal6(env)
    _observe(env, "c1.term")
    _observe(env, "c1.purpose", strength=SELF_DEFAULT_STRENGTH, kind="self", n=1)
    sn = snap(env)
    assert AspectKView(sn.k, NOW).cell("c1.purpose").band == Band.SHAKY
    p = prog(env)
    assert p.goals[0].blocks[0].state == "not_started" and p.today.region_id == "c1"
    _observe(env, "c1.purpose")
    p = prog(env)
    assert p.goals[0].blocks[0].state == "known" and p.today.region_id == "c2"
    assert all(n.state != "known" for n in snap(env).state.maps[map_id].nodes)
