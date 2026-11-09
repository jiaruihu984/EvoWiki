"""Stage 4a (single writer): a K change writes no section text and no outline (the goals.patch step
is gone, 2026-10-28). The sections it hands to the page writer wait for it (`stale.patch_pending`,
derived from the `patch` SectionLogs); a later write of a section ends its wait; the scoped writes are retried like the other lazy rewrites
(`stale.patch_due`: at most MAX_FAILURES failures on the same waiting set, once per RETRY_AFTER)."""

from datetime import timedelta

from evowiki.belief.aspects import Declare
from evowiki.teach.contracts import SectionLog
from evowiki.wikiapp.events import PageRewriteFailed, PagesTrashed, SectionLogsSaved
from evowiki.wikiapp.kchange import k_changed
from evowiki.wikiapp.kwrite import apply_in_memory
from evowiki.wikiapp.page import open_page_flow
from evowiki.wikiapp.repo import Commit
from evowiki.wikiapp.stale import MAX_FAILURES, RETRY_AFTER, patch_due, patch_pending
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW
from tests.wikiapp.test_page import (  # noqa: F401
    LINE,
    commit_wiki,
    env,
    gw_of,
    organized,
    sec,
)


def _patched(env):  # noqa: F811
    """The LINE page opened after page feedback named c2.term as missing: s3 (a line on it) waits for
    the writer."""
    repo, _, ids = env
    pid = organized(env, LINE)
    before = repo.load("u1").state.pages[pid].page
    snap = repo.load("u1")
    aspect = [Declare(event_id=ids("aev"), ts=NOW, cell="c2.term", value="dont")]
    kc = k_changed(snap, apply_in_memory(snap, aspect), pid, gw_of(env), skip=None, now=NOW,
                   new_id=ids, action="declare", declared_too=True)
    repo.commit(snap, Commit(aspect=aspect, wiki=kc.wiki))
    out = open_page_flow(repo, gw_of(env), "u1", pid, now=NOW, new_id=ids)
    return pid, before, out


def _state(env):  # noqa: F811
    return env[0].load("u1").state


def _log(pid, sid, producer):
    return SectionLog(page_id=pid, section_id=sid, at=NOW, producer=producer, role="recap",
                      spec_version=1, infer_version=1)


def test_a_handed_section_keeps_its_old_text_and_waits_for_the_writer(env):  # noqa: F811
    pid, before, out = _patched(env)
    assert out.llm_calls == 0                                    # no call of its own
    s3 = sec(out, "s3")
    assert s3.changed == "rewritten" and s3.role.value == "line" and not s3.folded
    assert s3.body == before.texts["s3"].body                    # still readable
    st = _state(env)
    assert st.section_logs[pid]["s3"].producer == "patch"
    assert patch_pending(st, pid) == ("s3",)
    assert patch_due(st, pid, NOW)


def test_a_later_write_ends_the_wait_without_another_event(env):  # noqa: F811
    pid, _before, _out = _patched(env)
    commit_wiki(env, SectionLogsSaved(logs=(_log(pid, "s3", "refine"),)))
    st = _state(env)
    assert patch_pending(st, pid) == () and not patch_due(st, pid, NOW)


def test_a_trashed_or_unorganized_page_never_waits(env):  # noqa: F811
    pid, _before, _out = _patched(env)
    assert patch_pending(_state(env), "nope") == ()
    commit_wiki(env, PagesTrashed(page_ids=(pid,), trashed=True))
    assert patch_pending(_state(env), pid) == () and not patch_due(_state(env), pid, NOW)


def _failed(env, pid, sections, at=NOW):  # noqa: F811
    from evowiki.wikiapp.events import WikiEvent
    from evowiki.wikiapp.repo import Commit
    repo, _, ids = env
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=ids("wev"), ts=at, body=PageRewriteFailed(
            page_id=pid, spec_version=1, failure="no_draft", kind="patch",
            sections=sections))]))


def test_failed_writes_are_retried_once_a_day_and_stop_after_two(env):  # noqa: F811
    pid, _before, _out = _patched(env)
    _failed(env, pid, ("s3",))
    st = _state(env)
    assert st.patch_failures[pid].failures == 1
    assert not patch_due(st, pid, NOW + timedelta(hours=1))      # failed once, not a day ago
    assert patch_due(st, pid, NOW + RETRY_AFTER)
    _failed(env, pid, ("s3",), at=NOW + RETRY_AFTER)
    st = _state(env)
    assert st.patch_failures[pid].failures == MAX_FAILURES
    assert not patch_due(st, pid, NOW + 10 * RETRY_AFTER)        # stopped: still waiting
    assert patch_pending(st, pid) == ("s3",)
    # the rsi / reorder marks are untouched by a patch failure
    assert st.stale_marks == {} and st.reorder_marks == {}


def test_a_new_patch_re_arms_the_retries(env):  # noqa: F811
    pid, _before, _out = _patched(env)
    for _ in range(MAX_FAILURES):
        _failed(env, pid, ("s3",))
    assert not patch_due(_state(env), pid, NOW + 10 * RETRY_AFTER)
    # another K change leaves another section waiting: the failures were on another set
    commit_wiki(env, SectionLogsSaved(logs=(_log(pid, "s2", "patch"),)))
    st = _state(env)
    assert patch_pending(st, pid) == ("s2", "s3")
    assert patch_due(st, pid, NOW)
    _failed(env, pid, ("s2", "s3"))
    assert _state(env).patch_failures[pid].failures == 1         # counted afresh


def test_check_sections_never_wait(env):  # noqa: F811
    """A patch log on a check section is no prose to write."""
    pid, _before, _out = _patched(env)
    assert _state(env).pages[pid].page.outline.sections[-1].role.value == "check"    # s4
    commit_wiki(env, SectionLogsSaved(logs=(_log(pid, "s4", "patch"),)))
    assert patch_pending(_state(env), pid) == ("s3",)


def test_script_is_empty_after_a_k_change(env):  # noqa: F811
    """No answer is consumed by a K change (single writer: the writer's scoped round writes)."""
    _patched(env)
    s: Script = env[1]
    assert s.pending() == {}
