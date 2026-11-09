"""A hand edit in the note becomes the page's text and locks the section (`wikiapp.hand_edit`): what is
written, what no system write can change afterwards, what 撤销 puts back, and that it is no complaint."""

import pytest

from evowiki.deep.contracts import RefineDraft, RefinedSection, RefinePlan
from evowiki.goals.contracts import Band, SectionRole
from evowiki.goals.fold import collapse_page, fold_pass, keep_locked
from evowiki.teach.localize import classes_of
from evowiki.teach.signal_kinds import polarity
from evowiki.wikiapp.events import HandEdited, PageSaved, SignalRecorded, UsageAdded, WikiEvent
from evowiki.wikiapp.hand_edit import (
    ALL_GONE,
    CHECK_EDIT,
    GONE,
    _applied,
    hand_edit_flow,
    undo_hand_edit,
)
from evowiki.wikiapp.page import set_pin
from evowiki.wikiapp.refine_apply import written_record
from evowiki.wikiapp.repo import Commit, WikiConflict, WikiInvalid
from evowiki.wikiapp.revise import revised_record
from evowiki.wikiapp.wiki_schemas import HandEditIn
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized
from tests.wikiapp.test_teach_generation import SpyRepo


@pytest.fixture
def env():
    return SpyRepo(["u1"]), Script(), Ids()


def kw(env):
    return {"now": NOW, "new_id": env[2]}


def rec(env, pid):
    return env[0].load("u1").state.pages[pid]


PLAN = RefinePlan(known="k", one_at_a_time=("a",), spoken_answer="s")


def rewritten(pid, sid, heading="系统", role="explain", body="系统重写。"):
    return RefinedSection(section_id=sid, heading=heading, role=role, depth="quick", cue="c", body=body)


def removed(env, pid, ids):
    from evowiki.wikiapp.page_version import text_fingerprint
    page = rec(env, pid).page
    return [{"section_id": sid, "fingerprint": text_fingerprint(
        next(s.heading for s in page.outline.sections if s.id == sid),
        page.texts[sid].body if sid in page.texts else "")} for sid in ids]


def edit(sid="s3", body="我改过的。", note="我改过的 [[链接]]。", **o):
    return HandEditIn(sections=[{"section_id": sid, "body": body, "note_body": note, **o}],
                      summary="【x】\n加：我改过的。")


def test_a_section_with_no_text_is_not_deleted_by_its_absence_from_the_note(env):
    """A run stopped part-way: the unwritten sections are not in the note. And 撤销 brings several deleted sections back in order."""
    pid = organized(env)
    repo = env[0]
    record = repo.load("u1").state.pages[pid]
    empty = record.model_copy(update={"page": record.page.model_copy(update={"texts": {k: v for k, v in record.page.texts.items() if k != "s3"}})})
    assert _applied(empty, HandEditIn(removed=removed(env, pid, ["s3"]), summary="x"), "he", NOW) is None
    ids = [s.id for s in record.page.outline.sections]
    out = hand_edit_flow(repo, "u1", pid, HandEditIn(removed=removed(env, pid, ["s2", "s3"]), summary="x"), **kw(env))
    undo_hand_edit(repo, "u1", pid, out.edit_id, now=NOW, new_id=env[2])
    assert [s.id for s in repo.load("u1").state.pages[pid].page.outline.sections] == ids


def test_the_edit_is_the_text_and_the_section_is_locked(env):
    pid = organized(env)
    old = rec(env, pid).page.texts["s3"]
    out = hand_edit_flow(env[0], "u1", pid, edit(), **kw(env))
    r = rec(env, pid)
    t = r.page.texts["s3"]
    assert (t.body, t.note_body) == ("我改过的。", "我改过的 [[链接]]。")
    assert r.page.state("s3").pinned and t.cue == old.cue != ""
    shown = next(s for s in out.page.sections if s.id == "s3")
    assert (shown.body, shown.note_body, shown.pinned) == ("我改过的。", "我改过的 [[链接]]。", True)
    assert out.page.hand_edit.sections == ["s3"] and out.page.hand_edit.can_undo
    assert "已写入你的改动" in out.page.hand_edit.summary
    bodies = [e.body for e in env[0].commits[-1].wiki]
    assert any(isinstance(b, HandEdited) for b in bodies)
    [sig] = [b.signal for b in bodies if isinstance(b, SignalRecorded)]
    assert (sig.kind, sig.section_id) == ("note_edit", "s3")
    [usage] = [b.record for b in bodies if isinstance(b, UsageAdded)]
    assert usage.action == "改了笔记"


def test_a_cue_he_typed_a_heading_and_a_fold_go_with_it(env):
    pid = organized(env)
    hand_edit_flow(env[0], "u1", pid, edit(heading="我的标题", cue="我的提示", folded=False), **kw(env))
    r = rec(env, pid)
    assert r.page.texts["s3"].cue == "我的提示"
    assert next(s for s in r.page.outline.sections if s.id == "s3").heading == "我的标题"
    assert not r.page.state("s3").folded


def test_a_card_stays_with_a_heading_edit_and_goes_with_a_new_text_or_cue(env):
    pid = organized(env)
    t = rec(env, pid).page.texts["s2"]
    assert t.cue and t.answer
    hand_edit_flow(env[0], "u1", pid, edit("s2", body=t.body, note=t.body, heading="我的标题"), **kw(env))
    assert (rec(env, pid).page.texts["s2"].cue, rec(env, pid).page.texts["s2"].answer) == (t.cue, t.answer)
    hand_edit_flow(env[0], "u1", pid, edit("s2", body=t.body, note=t.body, cue="我的折叠标题"), **kw(env))
    assert (rec(env, pid).page.texts["s2"].cue, rec(env, pid).page.texts["s2"].answer) == ("我的折叠标题", "")


def test_the_same_edit_again_changes_nothing(env):
    pid = organized(env)
    first = hand_edit_flow(env[0], "u1", pid, edit(), **kw(env))
    n = len(env[0].commits)
    again = hand_edit_flow(env[0], "u1", pid, edit(), **kw(env))
    assert first.edit_id is not None and again.edit_id is None and len(env[0].commits) == n


def test_it_is_no_complaint(env):
    pid = organized(env)
    hand_edit_flow(env[0], "u1", pid, edit(), **kw(env))
    [sig] = [e.body.signal for e in env[0].commits[-1].wiki if isinstance(e.body, SignalRecorded)]
    assert polarity(sig) == 0 and classes_of(sig) == []


def test_a_section_deleted_leaves_the_page_and_undo_brings_it_back_in_place(env):
    pid = organized(env)
    before = rec(env, pid).page
    ids = [s.id for s in before.outline.sections]
    out = hand_edit_flow(env[0], "u1", pid, HandEditIn(removed=removed(env, pid, ["s3"]), summary="删了"), **kw(env))
    r = rec(env, pid)
    assert "s3" not in [s.id for s in r.page.outline.sections] and "s3" not in r.page.texts
    assert "已删除" in out.page.hand_edit.summary
    undo_hand_edit(env[0], "u1", pid, out.edit_id, **kw(env))
    r = rec(env, pid)
    assert [s.id for s in r.page.outline.sections] == ids and r.page.texts == before.texts
    assert r.hand_edit is None


def test_refused_without_writing(env):
    pid = organized(env)
    n = len(env[0].commits)
    check = next(s.id for s in rec(env, pid).page.outline.sections if s.role is SectionRole.CHECK)
    every = [s.id for s in rec(env, pid).page.outline.sections]
    with pytest.raises(WikiConflict, match=GONE):
        hand_edit_flow(env[0], "u1", pid, edit(sid="nope"), **kw(env))
    with pytest.raises(WikiInvalid, match=CHECK_EDIT):
        hand_edit_flow(env[0], "u1", pid, edit(sid=check), **kw(env))
    with pytest.raises(WikiInvalid, match=ALL_GONE):
        hand_edit_flow(env[0], "u1", pid, HandEditIn(removed=removed(env, pid, every), summary="x"), **kw(env))
    with pytest.raises(LookupError):
        hand_edit_flow(env[0], "u1", "pg_nope", edit(), **kw(env))
    assert len(env[0].commits) == n


def test_undo_works_after_other_sections_changed_and_only_while_the_section_holds_the_edit(env):
    pid = organized(env)
    before = rec(env, pid).page
    out = hand_edit_flow(env[0], "u1", pid, edit(), **kw(env))
    # another section rewritten meanwhile
    r = rec(env, pid)
    draft = RefineDraft(page_id=pid, plan=PLAN, sections=(rewritten(pid, "s2"),))
    other = written_record(r, draft, [], job_id="j1", now=NOW)
    assert other.page.texts["s3"] == r.page.texts["s3"]
    snap = env[0].load("u1")
    env[0].commit(snap, Commit(wiki=[WikiEvent(event_id="e1", ts=NOW, body=PageSaved(record=other, action="x"))]))
    undo_hand_edit(env[0], "u1", pid, out.edit_id, **kw(env))
    r = rec(env, pid)
    assert r.page.texts["s3"] == before.texts["s3"] and not r.page.state("s3").pinned
    assert r.page.texts["s2"].body == "系统重写。"
    with pytest.raises(WikiConflict):                   # already undone
        undo_hand_edit(env[0], "u1", pid, out.edit_id, **kw(env))
    # a newer edit of the section: the older one cannot be undone over it
    first = hand_edit_flow(env[0], "u1", pid, edit(body="一", note="一"), **kw(env))
    hand_edit_flow(env[0], "u1", pid, edit(body="二", note="二"), **kw(env))
    with pytest.raises(WikiConflict):
        undo_hand_edit(env[0], "u1", pid, first.edit_id, **kw(env))
    with pytest.raises(LookupError):
        undo_hand_edit(env[0], "u1", pid, "he_nope", **kw(env))


# ---- what the lock holds against ----

def locked_page(env):
    pid = organized(env)
    hand_edit_flow(env[0], "u1", pid, edit(), **kw(env))
    return pid, rec(env, pid)


def test_a_rewrite_of_the_section_leaves_it_as_typed(env):
    pid, r = locked_page(env)
    draft = RefineDraft(page_id=pid, plan=PLAN, sections=(rewritten(pid, "s3"),))
    out = written_record(r, draft, [], job_id="j1", now=NOW)
    assert out.page.texts["s3"] == r.page.texts["s3"]
    assert next(s for s in out.page.outline.sections if s.id == "s3").heading != "系统"
    assert "s3" not in out.changed and "s3" not in out.written.sections


def test_a_whole_page_revision_and_a_new_outline_keep_it_in_place(env):
    pid, r = locked_page(env)
    taught = [s for s in r.page.outline.sections if s.role is not SectionRole.CHECK and s.id != "s3"]
    draft = RefineDraft(page_id=pid, plan=PLAN, sections=tuple(
        rewritten(pid, s.id, s.heading, s.role, "改写。") for s in taught))
    out = revised_record(r, draft, [], now=NOW, job_id="j2", model="")
    assert out.page.texts["s3"] == r.page.texts["s3"]
    ids = [s.id for s in r.page.outline.sections]
    assert [s.id for s in out.page.outline.sections].index("s3") == ids.index("s3")
    # a new outline with none of the old ids: the locked section stays near where it was
    fresh = r.page.outline.model_copy(update={"sections": tuple(
        s.model_copy(update={"id": f"n{i}"}) for i, s in enumerate(taught))})
    new = r.page.model_copy(update={"outline": fresh, "texts": {}, "states": {}})
    kept = keep_locked(r.page, new)
    assert kept.texts == {"s3": r.page.texts["s3"]} and "s3" in [s.id for s in kept.outline.sections]


def test_mastery_neither_folds_it_nor_collapses_it_and_unlocking_gives_it_back(env):
    pid, r = locked_page(env)

    class AllKnown:
        def cell(self, _c):
            return type("B", (), {"band": Band.KNOWN})()

    assert "s3" not in fold_pass(r.page.outline, r.page.states, AllKnown()).folded
    assert "s3" not in collapse_page(r.page.outline, r.page.states, AllKnown()).folded
    set_pin(env[0], "u1", pid, "s3", False, **kw(env))
    r2 = rec(env, pid)
    assert r2.page.texts["s3"].body == "我改过的。"
    assert "s3" in fold_pass(r2.page.outline, r2.page.states, AllKnown()).folded
    # and a later rewrite then replaces it, the note text with it
    draft = RefineDraft(page_id=pid, plan=PLAN, sections=(rewritten(pid, "s3"),))
    out = written_record(r2, draft, [], job_id="j3", now=NOW)
    assert (out.page.texts["s3"].body, out.page.texts["s3"].note_body) == ("系统重写。", None)


def test_an_edit_of_a_section_changed_on_the_server_since_is_refused_not_written(env):
    from evowiki.wikiapp.hand_edit import STALE
    from evowiki.wikiapp.page_version import text_fingerprint
    pid = organized(env)
    r = rec(env, pid)
    sec = next(x for x in r.page.outline.sections if x.id == "s3")
    seen = text_fingerprint(sec.heading, r.page.texts["s3"].body)
    draft = RefineDraft(page_id=pid, plan=PLAN, sections=(rewritten(pid, "s3", heading=sec.heading, role=sec.role.value),))
    other = written_record(r, draft, [], job_id="j1", now=NOW)
    env[0].commit(env[0].load("u1"), Commit(wiki=[WikiEvent(event_id="e1", ts=NOW, body=PageSaved(record=other, action="x"))]))
    n, current = len(env[0].commits), rec(env, pid).page.texts["s3"].body
    assert current != r.page.texts["s3"].body
    with pytest.raises(WikiConflict, match=STALE):
        hand_edit_flow(env[0], "u1", pid, edit(fingerprint=seen), **kw(env))
    assert len(env[0].commits) == n and rec(env, pid).page.texts["s3"].body == current
    now = text_fingerprint(sec.heading, current)          # an edit written from the current text goes in
    assert hand_edit_flow(env[0], "u1", pid, edit(fingerprint=now), **kw(env)).edit_id is not None
    assert hand_edit_flow(env[0], "u1", pid, edit(fingerprint=now), **kw(env)).edit_id is None   # a resend is no conflict


def test_stale_section_deletion_preserves_new_content(env):
    from evowiki.wikiapp.hand_edit import STALE
    from evowiki.wikiapp.page_version import text_fingerprint
    pid = organized(env)
    before = rec(env, pid).page
    section = next(s for s in before.outline.sections if s.id == "s3")
    seen = text_fingerprint(section.heading, before.texts["s3"].body)
    hand_edit_flow(env[0], "u1", pid, edit(body="NEWER CONTENT", note="NEWER CONTENT"), **kw(env))
    deletion = HandEditIn(removed=[{"section_id": "s3", "fingerprint": seen}], summary="delete")
    n = len(env[0].commits)
    with pytest.raises(WikiConflict, match=STALE):
        hand_edit_flow(env[0], "u1", pid, deletion, **kw(env))
    assert rec(env, pid).page.texts["s3"].body == "NEWER CONTENT"
    assert len(env[0].commits) == n


def test_current_deletion_retry_has_no_second_side_effect(env):
    from evowiki.wikiapp.page_version import text_fingerprint
    pid = organized(env)
    page = rec(env, pid).page
    section = next(s for s in page.outline.sections if s.id == "s3")
    deletion = HandEditIn(removed=[{"section_id": "s3", "fingerprint": text_fingerprint(section.heading, page.texts["s3"].body)}], summary="delete")
    first = hand_edit_flow(env[0], "u1", pid, deletion, **kw(env))
    n = len(env[0].commits)
    assert first.edit_id
    assert hand_edit_flow(env[0], "u1", pid, deletion, **kw(env)).edit_id is None
    assert len(env[0].commits) == n


def test_other_section_change_does_not_block_deleting_unchanged_section(env):
    pid = organized(env)
    deletion = HandEditIn(removed=removed(env, pid, ["s3"]), summary="delete")
    hand_edit_flow(env[0], "u1", pid, edit("s2", body="new other section", note="new other section"), **kw(env))
    hand_edit_flow(env[0], "u1", pid, deletion, **kw(env))
    page = rec(env, pid).page
    assert "s3" not in page.texts and page.texts["s2"].body == "new other section"
