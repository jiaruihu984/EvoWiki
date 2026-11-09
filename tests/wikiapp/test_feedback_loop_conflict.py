"""Phase 3b follow-up 2: the understood-feedback flows (page feedback, 「这部分我懂」) retry once on a
fresh snapshot when a background curate commits between their load and their commit, and never
repeat a model call (scripted gateway, MemoryRepo, no network).

The no-conflict goldens (`feedback_loop_conflict_golden.json`) were dumped from the flows before
the retry change: with no conflict the events and outputs stay byte-identical. To regenerate
after an intended event change (check the diff first), run once from the repo root with the
PYTHONPATH pytest uses:

    from tests.wikiapp.test_feedback_loop_conflict import *
    GOLDEN.write_text(json.dumps({n: dump((MeddleRepo(["u1"]), Script(), Ids()), n)[0]
                                  for n in SCENARIOS}, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", encoding="utf-8")
"""

import dataclasses
import json
from pathlib import Path

import pytest

from evowiki.belief.aspects import Declare
from evowiki.deep.store import MemoryAssetStore
from evowiki.teach.contracts import PageFeedbackIn
from evowiki.wikiapp.events import PageMarksCleared, PageSaved, WikiEvent
from evowiki.wikiapp.feedback_loop import READ_TRIES, page_feedback_loop
from evowiki.wikiapp.kchange import feedback_know
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import PageChanged
from evowiki.wikiapp.repo import BENIGN_WIKI_EVENTS, Commit, MemoryRepo, WikiConflict
from evowiki.wikiapp.understand import PURPOSE
from tests.wikiapp.fake_writer import WRITE, writing
from tests.wikiapp.script import Script
from tests.wikiapp.test_feedback_loop import LAB, TEXT, answer
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_kchange import rewrite
from tests.wikiapp.test_organize import add_page, text
from tests.wikiapp.test_page import LINE, miss, organized

GOLDEN = Path(__file__).with_name("feedback_loop_conflict_golden.json")


class MeddleRepo(MemoryRepo):
    """Before each of the next `meddles` commits that carry K or non-benign wiki events, a
    curate-like commit lands first: `meddle(repo)` (default: a PageSaved on another page)."""

    def __init__(self, users):
        super().__init__(users)
        self.meddles = 0
        self.meddle = None
        self._inside = False
        self.loads = 0
        self.forget: set[str] = set()      # concepts K "no longer has" in loads (from now on)

    def load(self, user_id):
        self.loads += 1
        snap = super().load(user_id)
        if not self.forget:
            return snap
        k = snap.k.copy()
        for cid in self.forget:
            k._concepts.pop(cid, None)
            for cell in [c for c in k._cells if c.startswith(f"{cid}.")]:
                del k._cells[cell]
        return dataclasses.replace(snap, k=k)

    def commit(self, snap, c, *, once=None):
        real = c.aspect or any(e.body.type not in BENIGN_WIKI_EVENTS for e in c.wiki)
        if real and self.meddles and not self._inside:
            self.meddles -= 1
            self._inside = True
            try:
                self.meddle(self)
            finally:
                self._inside = False
        return super().commit(snap, c, once=once)


def other_page(env) -> str:
    """A second page; the meddle toggles its `done` (a curate's PageSaved on ANOTHER page). A save
    of another page alone no longer conflicts (scoped commits, `repo.scopes`), so the meddle also
    commits a `PageMarksCleared` of no job: it changes nothing but its scope is ANY, so the flow's
    commit still conflicts and its retry is what these tests exercise."""
    repo, _, ids = env
    pid2 = add_page(repo, ids)

    def meddle(r):
        snap = r.load("u1")
        rec = snap.state.pages[pid2]
        r.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(
            record=rec.model_copy(update={"done": not rec.done}), action="curated")),
            WikiEvent(event_id=ids("wev"), ts=NOW, body=PageMarksCleared(job_id="h_none"))]))
    repo.meddle = meddle
    return pid2


def rewrite_text(env, pid, sid, body="curate 改写了这一段"):
    """A meddle that rewrites section `sid` of page `pid` (a curate rewrote this page)."""
    _, _, ids = env

    def meddle(r):
        snap = r.load("u1")
        rec = snap.state.pages[pid]
        t = rec.page.texts[sid]
        page = rec.page.model_copy(update={"texts": {**rec.page.texts,
                                                     sid: t.model_copy(update={"body": body})}})
        r.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(
            record=rec.model_copy(update={"page": page}), action="curated"))]))
    return meddle


@pytest.fixture
def env():
    return MeddleRepo(["u1"]), Script(), Ids()


def _gw(env):
    return CountingGateway(env[1].gateway(), "u1")


def run_fb(env, pid, components=(LAB,)):
    repo, _, ids = env
    store = MemoryAssetStore()
    out = page_feedback_loop(repo, _gw(env), "u1", pid, PageFeedbackIn(text=TEXT, chips=("unclear",)),
                             now=NOW, new_id=ids, components=components, assets=store)
    return out, store


def run_know(env, pid, sid="s2"):
    repo, _, ids = env
    return feedback_know(repo, _gw(env), "u1", pid, sid, now=NOW, new_id=ids)


# ---- scenarios (also used to dump the goldens) ----

def page_component(env):
    """Flow 1: the plan patches s2 and places a library component (1 call: understanding)."""
    pid = organized(env)
    env[1].push(PURPOSE, answer())
    return pid, lambda: run_fb(env, pid)[0]


def page_missing_patch(env):
    """Flow 1: 「不会」 on c2 and the K-change pass hands s3 to the writer (1 call: the
    understanding; the K-change pass makes none)."""
    pid = organized(env, LINE)
    env[1].push(PURPOSE, answer(missing=["c2"], page={"summary": "无", "sections": []}))
    return pid, lambda: run_fb(env, pid, components=())[0]


def know_plain(env):
    """Flow 2: 「这部分我懂」 on s2, 0 calls."""
    pid = organized(env)
    return pid, lambda: run_know(env, pid)


def know_patch(env):
    """Flow 2: 「这部分我懂」 on s2 while s3 conflicts with K: the conflict is recorded (0 calls)."""
    pid = organized(env, LINE)
    miss(env, "c2.term")
    return pid, lambda: run_know(env, pid)


SCENARIOS = {"page_component": page_component, "page_missing_patch": page_missing_patch,
             "know_plain": know_plain, "know_patch": know_patch}
CALLS = {"page_component": 1, "page_missing_patch": 1, "know_plain": 0, "know_patch": 0}


def dump(env, name) -> tuple[dict, int]:
    """What a scenario leaves (its output, the wiki and K events it committed: the golden), and
    the model calls it made."""
    repo = env[0]
    _pid, go = SCENARIOS[name](env)
    wseq, aseq = len(repo.wiki_log("u1")), len(repo.aspect_log("u1"))
    calls = env[1].calls()
    out = go()
    return ({"out": out.model_dump(mode="json"),
             "wiki": [e.model_dump(mode="json") for e in repo.wiki_log("u1")[wseq:]],
             "aspect": [e.model_dump(mode="json") for e in repo.aspect_log("u1")[aseq:]]},
            env[1].calls() - calls)


# ---- no conflict: identical to before ----

@pytest.mark.parametrize("name", list(SCENARIOS))
def test_no_conflict_events_and_output_are_unchanged(env, name):
    got, made = dump(env, name)
    assert made == CALLS[name]
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))[name]
    assert json.dumps(got, sort_keys=True, ensure_ascii=False) == json.dumps(
        want, sort_keys=True, ensure_ascii=False)


# ---- one conflict: retried on a fresh snapshot, no model call repeated ----

@pytest.mark.parametrize("name", list(SCENARIOS))
def test_one_curate_on_another_page_is_retried_without_a_model_call(env, name):
    repo, s, _ = env
    pid, go = SCENARIOS[name](env)
    pid2 = other_page(env)
    repo.meddles = 1
    calls = s.calls()
    done_before = repo.load("u1").state.pages[pid2].done
    out = go()
    assert s.calls() - calls == CALLS[name]                    # exactly the first attempt's
    st = repo.load("u1").state
    assert st.pages[pid2].done is not done_before              # the curate's commit is kept
    if name.startswith("page"):
        assert out.understood and out.feedback_id in st.feedbacks
        assert out.llm_calls == CALLS[name]
    else:
        assert out.section.folded and out.change.llm_calls == CALLS[name]
    if name == "page_missing_patch":                            # he named it: s3 waits for the writer
        assert st.pages[pid].changed.get("s3") == "rewritten"
    if name == "know_patch":
        assert out.change.rewritten == []


def test_a_retried_page_feedback_writes_the_same_page_as_without_conflict(env):
    """The retry's page equals the no-conflict page (ids aside): same texts and component."""
    repo, _, _ = env
    pid, go = page_component(env)
    other_page(env)
    repo.meddles = 1
    out = go()
    rec = repo.load("u1").state.pages[pid]
    assert [x.section_id for x in out.sections] == ["s2"]
    assert rec.page.texts["s2"].body.endswith("标准差就是“典型的偏离有多大”。")
    assert out.component is not None and out.component.component_id == "variance-dots"


# ---- two conflicts: 「这部分我懂」 still 409; a page feedback is read again on the fresh page ----

@pytest.mark.parametrize("name", ["know_plain", "know_patch"])
def test_two_conflicts_raise_without_a_model_call_repeated(env, name):
    repo, s, _ = env
    pid, go = SCENARIOS[name](env)
    other_page(env)
    repo.meddles = 2
    calls = s.calls()
    before = repo.load("u1").state.pages[pid]
    with pytest.raises(WikiConflict):
        go()
    assert s.calls() - calls == CALLS[name]
    assert repo.load("u1").state.pages[pid] == before          # nothing of the flow committed


def test_page_feedback_after_two_conflicts_is_read_again_on_the_fresh_page(env):
    repo, s, _ = env
    _pid, go = page_component(env)
    other_page(env)
    repo.meddles = 2
    out = go()
    assert out.understood and out.feedback_id in repo.load("u1").state.feedbacks
    # the page the reading is about did not change: the same request is served from the call log
    assert s.calls(PURPOSE) == 1


def test_page_feedback_that_keeps_conflicting_is_409_after_the_last_reading(env):
    repo, s, _ = env
    pid, go = page_component(env)
    other_page(env)
    repo.meddles = 2 * READ_TRIES
    loads = repo.loads
    before = repo.load("u1").state.pages[pid]
    with pytest.raises(WikiConflict):
        go()
    assert s.calls(PURPOSE) == 1                                   # replayed, never paid again
    assert repo.loads - loads > 2 * READ_TRIES                     # every reading ran on a fresh load
    assert repo.load("u1").state.pages[pid].page == before.page   # nothing of the flow committed


# ---- the target section changed: the feedback is read again against it ----

def test_page_feedback_on_a_section_a_curate_rewrote_is_read_again(env):
    repo, s, _ = env
    pid, go = page_component(env)
    s.push(PURPOSE, answer())
    repo.meddle, repo.meddles = rewrite_text(env, pid, "s2"), 1
    out = go()
    assert out.understood and s.calls(PURPOSE) == 2
    rec = repo.load("u1").state.pages[pid]
    assert rec.page.texts["s2"].body.startswith("curate 改写了这一段")   # the curate's text is kept
    assert rec.page.texts["s2"].body.endswith("标准差就是“典型的偏离有多大”。")


def test_know_on_a_section_a_curate_rewrote_is_page_changed(env):
    repo, _, _ = env
    pid, go = know_plain(env)
    repo.meddle, repo.meddles = rewrite_text(env, pid, "s2"), 1
    seq = len(repo.aspect_log("u1"))
    with pytest.raises(PageChanged):
        go()
    assert len(repo.aspect_log("u1")) == seq                   # no +0.6 observations
    assert not repo.load("u1").state.pages[pid].page.state("s2").folded


def test_know_after_another_section_changed_recomputes_with_zero_calls(env):
    """No model call on the first attempt: the K-change pass is recomputed on the fresh page."""
    repo, s, _ = env
    pid, go = know_plain(env)
    repo.meddle, repo.meddles = rewrite_text(env, pid, "s3"), 1
    calls = s.calls()
    out = go()
    rec = repo.load("u1").state.pages[pid]
    assert out.section.folded and s.calls() == calls and out.change.llm_calls == 0
    assert rec.page.texts["s3"].body == "curate 改写了这一段" and rec.page.state("s2").folded


# ---- R2: the K-change pass makes no model call, so a retry recomputes it ----

def test_page_feedback_on_a_changed_page_recomputes_the_k_change_pass(env):
    """Another section changed under a 「不会」 feedback: the K-change pass is recomputed on the
    fresh page (0 calls); the reading is not repeated."""
    repo, s, _ = env
    pid, go = page_missing_patch(env)
    repo.meddle, repo.meddles = rewrite_text(env, pid, "s1"), 1       # not a target section
    out = go()
    assert out.understood and s.calls(PURPOSE) == 1
    rec = repo.load("u1").state.pages[pid]
    assert rec.page.texts["s1"].body == "curate 改写了这一段" and rec.changed.get("s3") == "rewritten"


def test_know_on_a_changed_page_recomputes_the_k_change_pass(env):
    repo, s, _ = env
    pid, go = know_patch(env)
    repo.meddle, repo.meddles = rewrite_text(env, pid, "s1"), 1
    calls = s.calls()
    out = go()
    assert s.calls() == calls and out.section.folded and out.change.rewritten == []


def k_meddle(env, cell="c1.procedure"):
    """A meddle that commits a K event (a curate's topic → K)."""
    _, _, ids = env

    def meddle(r):
        r.commit(r.load("u1"), Commit(aspect=[Declare(event_id=ids("aev"), ts=NOW, cell=cell,
                                                      value="know")]))
    return meddle


def test_k_moved_under_know_with_a_conflict_is_recomputed(env):
    repo, s, _ = env
    _pid, go = know_patch(env)
    repo.meddle, repo.meddles = k_meddle(env), 1
    calls = s.calls()
    out = go()
    assert s.calls() == calls and out.change.llm_calls == 0 and out.change.rewritten == []


def test_k_moved_under_know_without_a_call_is_recomputed(env):
    repo, s, _ = env
    pid, go = know_plain(env)
    repo.meddle, repo.meddles = k_meddle(env), 1
    calls = s.calls()
    out = go()
    assert s.calls() == calls and out.change.llm_calls == 0 and out.section.folded
    assert repo.load("u1").state.pages[pid].page.state("s2").folded


def test_a_concept_gone_from_k_on_retry_gets_no_dont(env):
    """「不会」 is re-read against the fresh K: a concept K no longer has gets no cells and no
    Declare."""
    repo, _, _ = env
    _pid, go = page_missing_patch(env)
    other_page(env)
    meddle = repo.meddle                           # a curate on another page, then K forgets c2

    def forget(r):
        meddle(r)
        r.forget = {"c2"}
    repo.meddle, repo.meddles = forget, 1
    aseq = len(repo.aspect_log("u1"))
    out = go()
    assert [(m.concept_id, m.cells) for m in out.missing] == [("c2", [])]
    assert len(repo.aspect_log("u1")) == aseq


# ---- 「再讲深一点」: retried on a fresh snapshot, the K-change pass calls nothing ----

def test_deeper_retried_after_another_page_changed_makes_no_extra_call(env):
    repo, s, _ = env
    pid = organized(env, LINE)                     # s3 is a line on c2.term
    miss(env, "c2.term")                # K moved; the page was not brought up to date
    s.push(WRITE, text("s2", "z 讲深一点"))
    pid2 = other_page(env)
    repo.meddles = 1
    items, gw = rewrite(env, pid, "s2", "deeper", kit=writing(env))
    assert [n for n, _ in items][-2:] == ["change", "done"]
    assert gw.calls == 0                           # the writer's round only
    change = items[-2][1]
    assert change.rewritten == ["s2"] and change.llm_calls == 1
    st = repo.load("u1").state
    assert st.pages[pid].page.texts["s2"].body == "z 讲深一点" and st.pages[pid2].done
