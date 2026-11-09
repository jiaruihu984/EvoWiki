"""Replay cases from feedback signals and exemplar pages: the run behind each signal, the caps, the
anchors, and every reason a signal is left out. Fake trace rows on the `pool` fixture; MemoryRepo."""

from datetime import timedelta

import pytest

from evowiki.content.models import Lang
from evowiki.deep.agent import SnapshotSeqs
from evowiki.harness import anchors
from evowiki.harness.cases import select_cases
from evowiki.store import trace
from evowiki.store.users import UserRow, create_user
from evowiki.teach.contracts import Exemplar, Signal, TeachingSpec
from evowiki.wikiapp.events import PageSaved, WriteMark
from evowiki.wikiapp.repo import MemoryRepo
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import commit_wiki, organized

T0 = NOW + timedelta(hours=2)
LATER = NOW + timedelta(days=1)


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def _user(pool):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))


def _seqs(repo) -> dict:
    s = repo.load("u1")
    return {"wiki_seq": s.wiki_seq, "aspect_seq": s.aspect_seq, "infer_version": s.infer_version}


def _run(pool, rid, page, *, at, snapshot, scope=None, stop="finished", mode="page",
         instruction="", given=None, old=False):
    """A finished deep write run started at `at`; `given`: the traced page it was given (Page JSON;
    None = the stored one); `old`: traced before the `page` / `domain` keys existed."""
    sink = trace.PgTraceSink(pool, clock=lambda: at)
    extra = {} if old else {"page": given, "domain": None}
    sink.start(rid, user_id="u1", agent="deep.write", subject=page, pack_version=0,
               input={"page_id": page, "title": "t", "round": 0, "must_fix": [], "snapshot": snapshot,
                      "mode": mode, "instruction": instruction, "scope": scope, "spec_version": 1,
                      **extra})
    sink.finish(rid, stop=stop, error=None)


def _sig(sid, page, section, *, chips=("unclear",), at=LATER, kind="page_feedback"):
    return Signal(signal_id=sid, kind=kind, at=at, page_id=page, section_id=section, chips=chips)


def _spec(*exemplars) -> TeachingSpec:
    return TeachingSpec(version=1, items=(), exemplars=tuple(exemplars))


def _select(pool, env, signals, spec=None, now=LATER + timedelta(hours=1)):
    with pool.connection() as conn:
        return select_cases(conn, "u1", signals, spec or _spec(), now=now, repo=env[0])


def _sections(env, pid) -> list[str]:
    rec = env[0].load("u1").state.pages[pid]
    return [s.id for s in rec.page.outline.sections]


def test_a_signal_gets_the_newest_run_before_it_and_its_snapshot(pool, env):
    _user(pool)
    pid = organized(env)
    sids = [s for s in _sections(env, pid) if s in env[0].load("u1").state.pages[pid].page.texts]
    snap = _seqs(env[0])
    _run(pool, "old", pid, at=T0, snapshot=snap, mode="rewrite", instruction="旧")
    _run(pool, "new", pid, at=T0 + timedelta(hours=1), snapshot=snap, mode="feedback",
         instruction="讲慢一点", scope={"sections": [], "dry_run": False})
    _run(pool, "after", pid, at=LATER + timedelta(minutes=5), snapshot=snap)   # after the signals
    out = _select(pool, env, [_sig("g1", pid, sids[0]), _sig("g2", pid, sids[0])])
    (case,) = out.problem
    assert case.case_id == f"case:new:{sids[0]}" and case.run_id == "new"
    assert case.signal_ids == ("g1", "g2") and not case.anchor
    assert case.snapshot == SnapshotSeqs(**snap) and case.user_id == "u1"
    assert (case.mode, case.instruction, case.page_id) == ("feedback", "讲慢一点", pid)
    assert out.unusable == ()


def _page_sections(env, pid) -> dict[str, str]:
    return dict(env[0].load("u1").state.pages[pid].page.texts)


def _cut(env, pid, sid):
    """The stored page without section `sid` (outline and text): the page a run had before its
    writer added `sid`."""
    page = env[0].load("u1").state.pages[pid].page
    return page.model_copy(update={
        "outline": page.outline.model_copy(update={"sections": tuple(
            s for s in page.outline.sections if s.id != sid)}),
        "texts": {k: v for k, v in page.texts.items() if k != sid}})


def _save(env, pid, job, sections, page=None) -> int:
    """A commit of the page as job `job` saved it (`page`, default the stored one; its WriteMark
    names `sections`); returns its wiki seq."""
    rec = env[0].load("u1").state.pages[pid]
    mark = WriteMark(job_id=job, at=NOW, model="m", sections=tuple(sections), kind="page")
    commit_wiki(env, PageSaved(record=rec.model_copy(update={
        "page": page if page is not None else rec.page, "written": mark}), action="refined"))
    return env[0].load("u1").wiki_seq


def test_a_section_the_writer_added_is_a_case_from_the_page_its_job_saved(pool, env):
    """The live case (§15 真实运行 2026-10-22): round 1 of a page write added s2, so no run's input
    has it; the case rewrites s2 on the run's snapshot with the page that write saved."""
    _user(pool)
    pid = organized(env)
    snap = _seqs(env[0])
    given = _cut(env, pid, "s2").model_copy(update={"texts": {}})      # a fresh outline
    job = f"page:u1:{pid}:1"
    _run(pool, f"{job}:write:0:w0", pid, at=T0, snapshot=snap, given=given.model_dump(mode="json"))
    _run(pool, f"{job}:write:1:w0", pid, at=T0 + timedelta(minutes=1), snapshot=snap,
         given=given.model_dump(mode="json"))
    _save(env, pid, "other-job", ["s1"])              # another job's save is not this run's
    seq = _save(env, pid, job, ["s1", "s2", "s3"])
    _save(env, pid, job, ["s1", "s2", "s3"])          # a later save carrying the same mark
    saved = env[0].load_at("u1", seq, snap["aspect_seq"], snap["infer_version"]).state.pages[pid]
    out = _select(pool, env, [_sig("a", pid, "s2"), _sig("b", pid, "s2"), _sig("c", pid, "s1")])
    assert out.unusable == ()
    added, kept = out.problem
    assert (added.section_id, added.signal_ids, added.run_id) == ("s2", ("a", "b"),
                                                                 f"{job}:write:1:w0")
    assert (added.mode, added.instruction, added.page_seq) == ("rewrite", "", seq)
    assert added.page == saved.page.model_dump(mode="json") and "s2" in added.page["texts"]
    assert added.snapshot == SnapshotSeqs(**snap)            # the run's own K and wiki
    # a section the run's input had keeps replaying from that input
    assert (kept.section_id, kept.mode, kept.page_seq) == ("s1", "page", None)
    assert kept.page == given.model_dump(mode="json")


def test_a_traced_job_id_finds_the_save_of_a_refine(pool, env):
    """A DeepService refine's WriteMark carries its RefineJob id, not the run id prefix: the run's
    traced `job_id` is what finds its save."""
    _user(pool)
    pid = organized(env)
    full = env[0].load("u1").state.pages[pid].page
    # the stored page at the run's snapshot has no s2: the refine added it
    commit_wiki(env, PageSaved(record=env[0].load("u1").state.pages[pid].model_copy(
        update={"page": _cut(env, pid, "s2")}), action="refined"))
    snap = _seqs(env[0])
    sink = trace.PgTraceSink(pool, clock=lambda: T0)
    rid = f"refine:{pid}:abc:write:0:w0"
    sink.start(rid, user_id="u1", agent="deep.write", subject=pid, pack_version=0,
               input={"page_id": pid, "snapshot": snap, "mode": "rewrite", "instruction": "",
                      "scope": {"sections": [], "dry_run": False}, "page": None, "domain": None,
                      "job_id": "rf_1"})
    sink.finish(rid, stop="finished", error=None)
    seq = _save(env, pid, "rf_1", ["s2"], page=full)
    (case,) = _select(pool, env, [_sig("a", pid, "s2")]).problem
    assert (case.run_id, case.page_seq, case.mode) == (rid, seq, "rewrite")
    assert case.page["texts"]["s2"] == full.texts["s2"].model_dump(mode="json")


def test_why_a_section_outside_the_runs_input_gives_no_case(pool, env, monkeypatch):
    _user(pool)
    pid = organized(env)
    snap = _seqs(env[0])
    job = f"page:u1:{pid}:1"
    _run(pool, f"{job}:write:0:w0", pid, at=T0, snapshot=snap, mode="rewrite",
         given=_cut(env, pid, "s2").model_dump(mode="json"))

    def why(section="s2"):
        out = _select(pool, env, [_sig("a", pid, section)])
        assert out.problem == ()
        return out.unusable

    # a check section is never the writer's
    assert why("s4") == (("a", "这一段是确认题，不是写作写的，不能重放"),)
    # the job saved the page but did not write s2 there
    _save(env, pid, job, ["s1"])
    assert why() == (("a", "那次写作没有写这一段"),)
    monkeypatch.setattr(env[0], "load_at", _failing_after(env[0].load_at, snap["wiki_seq"]))
    assert why() == (("a", "读不到那次写作存下的页面"),)
    monkeypatch.undo()


def test_a_section_added_after_the_run_or_removed_says_which(pool, env):
    _user(pool)
    pid = organized(env)
    snap = _seqs(env[0])
    full = env[0].load("u1").state.pages[pid].page
    job = f"page:u1:{pid}:1"
    _run(pool, f"{job}:write:0:w0", pid, at=T0, snapshot=snap, mode="rewrite",
         given=_cut(env, pid, "s2").model_dump(mode="json"))
    _save(env, pid, job, ["s1"], page=_cut(env, pid, "s2"))      # the job's page has no s2
    assert _select(pool, env, [_sig("a", pid, "s2")]).unusable == (
        ("a", "这一段不在那次写作的输入和结果里，现在页面上也已经没有了"),)
    _save(env, pid, "later", ["s2"], page=full)                  # someone else added it later
    assert _select(pool, env, [_sig("a", pid, "s2")]).unusable == (
        ("a", "这一段是那次写作之后才加上的，没有写它的写作记录"),)


def _failing_after(load_at, seq):
    def load(user_id, wiki_seq, aspect_seq, infer_version):
        if wiki_seq > seq:
            raise LookupError("gone")
        return load_at(user_id, wiki_seq, aspect_seq, infer_version)
    return load


def test_reasons_for_runs_that_are_not_finished_or_only_dry(pool, env):
    _user(pool)
    pid = organized(env)
    snap = _seqs(env[0])
    _run(pool, "capped", pid, at=T0, snapshot=snap, stop="tool_cap")
    assert _select(pool, env, [_sig("a", pid, "s1")]).unusable == (("a", "那几次写作都没有正常写完"),)
    _run(pool, "dry", pid, at=T0 + timedelta(minutes=1), snapshot=snap,
         scope={"sections": [], "dry_run": True})
    assert _select(pool, env, [_sig("a", pid, "s1")]).unusable == (("a", "那几次写作都是试写，没有存进页面"),)


def test_a_signal_on_no_page_says_so(pool, env):
    """`localize` never groups a page-less signal, so this is the case builder's own guard."""
    from evowiki.harness.cases import _problem
    _user(pool)
    unusable: list = []
    with pool.connection() as conn:
        assert _problem(conn, "u1", [_sig("b", None, None)], env[0], unusable) == ()
    assert unusable == [("b", "反馈没有落在某一页上")]


def test_runs_that_cannot_serve_a_signal_are_skipped_or_explained(pool, env):
    _user(pool)
    pid = organized(env)
    sid = next(iter(_page_sections(env, pid)))
    snap = _seqs(env[0])
    _run(pool, "bad-stop", pid, at=T0, snapshot=snap, stop="no_tool_calls")
    _run(pool, "dry", pid, at=T0 + timedelta(minutes=1), snapshot=snap,
         scope={"sections": [sid], "dry_run": True})
    _run(pool, "nosnap", pid, at=T0 + timedelta(minutes=2), snapshot=None)
    _run(pool, "other-section", pid, at=T0 + timedelta(minutes=3), snapshot=snap,
         scope={"sections": ["zz"], "dry_run": False})
    out = _select(pool, env, [_sig("g1", pid, sid)])
    assert out.problem == () and out.unusable == (("g1", "那几次写作都没有改到这一段"),)
    _run(pool, "ok", pid, at=NOW + timedelta(minutes=30), snapshot=snap)
    (case,) = _select(pool, env, [_sig("g1", pid, sid)]).problem
    assert case.run_id == "ok"          # skipped the newer unusable ones, not the run behind them


def test_unusable_reasons(pool, env):
    _user(pool)
    pid = organized(env)
    sid = next(iter(_page_sections(env, pid)))
    snap = _seqs(env[0])
    # no run at all
    assert _select(pool, env, [_sig("a", pid, sid)]).unusable == (("a", "找不到写这一段的记录"),)
    # only runs without a snapshot
    _run(pool, "nosnap", pid, at=T0, snapshot=None)
    assert _select(pool, env, [_sig("a", pid, sid)]).unusable == (("a", "那次写作没有留下当时的页面快照"),)
    # a snapshot from before the page existed
    empty = dict(snap, wiki_seq=0, aspect_seq=0)
    _run(pool, "early", pid, at=T0 + timedelta(minutes=1), snapshot=empty)
    assert _select(pool, env, [_sig("a", pid, sid)]).unusable == (("a", "那次写作时的快照里没有这一页"),)
    # the section is not in the page the run wrote against, and its job saved nothing
    _run(pool, "gone", pid, at=T0 + timedelta(minutes=2), snapshot=snap, mode="rewrite",
         given=_cut(env, pid, sid).model_dump(mode="json"))
    assert _select(pool, env, [_sig("a", pid, sid)]).unusable == ((
        "a", "这一段是那次写作中新加的（或者当时还没有正文），但那次写作的结果没有存进页面"),)
    # a first write traced before the page key: its outline is unknown, not "gone"
    _run(pool, "old-first", pid, at=T0 + timedelta(minutes=3), snapshot=snap, old=True)
    assert _select(pool, env, [_sig("a", pid, sid)]).unusable == (
        ("a", "那次写作用的提纲没有记下来（较早的记录），不能照原样重放"),)
    # an old refine (mode rewrite) still replays from its snapshot's stored page
    _run(pool, "old-refine", pid, at=T0 + timedelta(minutes=4), snapshot=snap, old=True,
         mode="rewrite")
    assert _select(pool, env, [_sig("a", pid, sid)]).problem[0].run_id == "old-refine"
    # a signal on the whole page: the first section localization traced it to
    out = _select(pool, env, [_sig("a", pid, None)])
    assert [c.signal_ids for c in out.problem] == [("a",)]


def test_signal_on_a_whole_page_joins_the_case_of_its_traced_section(pool, env):
    _user(pool)
    pid = organized(env)
    sid = next(iter(_page_sections(env, pid)))
    _run(pool, "r", pid, at=T0, snapshot=_seqs(env[0]))
    out = _select(pool, env, [_sig("a", pid, sid), _sig("b", pid, None)])
    assert [c.signal_ids for c in out.problem] == [("a", "b")] and not out.unusable


def test_more_signals_first_and_at_most_max_problem_cases(pool, env):
    _user(pool)
    pid = organized(env)
    sids = list(_page_sections(env, pid))
    assert len(sids) >= 3
    _run(pool, "r", pid, at=T0, snapshot=_seqs(env[0]))
    sigs = [_sig("x1", pid, sids[0]), _sig("y1", pid, sids[1]), _sig("y2", pid, sids[1]),
            _sig("y3", pid, sids[1]), _sig("z1", pid, sids[2]), _sig("z2", pid, sids[2])]
    out = _select(pool, env, sigs)
    assert [c.section_id for c in out.problem] == [sids[1], sids[2], sids[0]]
    assert out.problem[0].signal_ids == ("y1", "y2", "y3")
    assert len(out.problem) <= anchors.MAX_PROBLEM_CASES


def test_the_cap_cuts_the_weakest_sections(pool, env, monkeypatch):
    from evowiki.harness import cases
    monkeypatch.setattr(cases, "MAX_PROBLEM_CASES", 2)
    _user(pool)
    pid = organized(env)
    sids = list(_page_sections(env, pid))
    _run(pool, "r", pid, at=T0, snapshot=_seqs(env[0]))
    sigs = [_sig("x1", pid, sids[0]), _sig("y1", pid, sids[1]), _sig("y2", pid, sids[1]),
            _sig("z1", pid, sids[2]), _sig("z2", pid, sids[2]), _sig("z3", pid, sids[2])]
    out = _select(pool, env, sigs)
    assert [c.section_id for c in out.problem] == [sids[2], sids[1]]


def test_the_largest_group_is_the_finding(pool, env):
    _user(pool)
    pid = organized(env)
    sids = list(_page_sections(env, pid))
    _run(pool, "r", pid, at=T0, snapshot=_seqs(env[0]))
    sigs = [_sig("u1", pid, sids[0], chips=("unclear",)), _sig("u2", pid, sids[1], chips=("unclear",)),
            _sig("l1", pid, sids[2], chips=("too_long",))]
    out = _select(pool, env, sigs)
    assert {s for c in out.problem for s in c.signal_ids} == {"u1", "u2"}
    assert out.finding.startswith("讲得不清楚：")
    assert out.finding == _select(pool, env, sigs).finding          # stable for the same group
    assert _select(pool, env, []).finding == "" and _select(pool, env, []).problem == ()


def test_anchors_are_the_longest_section_of_each_exemplar_page(pool, env):
    _user(pool)
    pid = organized(env)
    texts = _page_sections(env, pid)
    longest = max(texts, key=lambda s: len(texts[s].body))
    snap = _seqs(env[0])
    _run(pool, "e-old", pid, at=T0, snapshot=snap, mode="rewrite")
    _run(pool, "e-new", pid, at=T0 + timedelta(hours=1), snapshot=snap, mode="feedback",
         instruction="好")
    ex = [Exemplar(exemplar_id="e1", kind="wiki_page", ref=pid, title="t", text="x"),
          Exemplar(exemplar_id="e2", kind="vault_note", ref="a/b.md", title="n", text="x"),
          Exemplar(exemplar_id="e3", kind="wiki_page", ref="nopage", title="n", text="x")]
    out = _select(pool, env, [], _spec(*ex))
    (a,) = out.anchors
    assert a.anchor and a.case_id == f"anchor:e-new:{longest}" and a.signal_ids == ()
    assert (a.page_id, a.section_id, a.mode, a.instruction) == (pid, longest, "feedback", "好")
    assert out.unusable == (("exemplar:nopage", "这个范例页没有可用的写作记录"),)


def test_anchors_are_capped_and_skip_problem_pages(pool, env):
    _user(pool)
    pid = organized(env)
    sid = next(iter(_page_sections(env, pid)))
    _run(pool, "r", pid, at=T0, snapshot=_seqs(env[0]))
    ex = Exemplar(exemplar_id="e1", kind="wiki_page", ref=pid, title="t", text="x")
    assert len(_select(pool, env, [], _spec(ex)).anchors) == 1
    # the exemplar page is already a problem page: no anchor from it
    out = _select(pool, env, [_sig("a", pid, sid)], _spec(ex))
    assert len(out.problem) == 1 and out.anchors == ()


def test_anchors_stop_at_max_exemplars(pool, env, monkeypatch):
    from evowiki.harness import cases
    monkeypatch.setattr(cases, "MAX_EXEMPLARS", 1)
    _user(pool)
    pid = organized(env)
    _run(pool, "r", pid, at=T0, snapshot=_seqs(env[0]))
    ex = [Exemplar(exemplar_id=f"e{i}", kind="wiki_page", ref=pid, title="t", text="x")
          for i in range(2)]
    assert len(_select(pool, env, [], _spec(*ex)).anchors) == 1


def test_an_exemplar_section_its_write_added_can_be_the_anchor(pool, env):
    _user(pool)
    pid = organized(env)
    texts = _page_sections(env, pid)
    longest = max(texts, key=lambda s: len(texts[s].body))
    snap = _seqs(env[0])
    job = f"page:u1:{pid}:1"
    given = _cut(env, pid, longest).model_copy(update={"texts": {}})
    _run(pool, f"{job}:write:0:w0", pid, at=T0, snapshot=snap, given=given.model_dump(mode="json"))
    seq = _save(env, pid, job, list(texts))
    ex = Exemplar(exemplar_id="e1", kind="wiki_page", ref=pid, title="t", text="x")
    (a,) = _select(pool, env, [], _spec(ex)).anchors
    assert (a.section_id, a.mode, a.page_seq) == (longest, "rewrite", seq)
    assert a.page["texts"][longest]["body"] == texts[longest].body


def test_why_an_exemplar_page_gives_no_anchor(pool, env):
    _user(pool)
    pid = organized(env)
    snap = _seqs(env[0])
    ex = Exemplar(exemplar_id="e1", kind="wiki_page", ref=pid, title="t", text="x")

    def why():
        out = _select(pool, env, [], _spec(ex))
        assert out.anchors == ()
        return out.unusable

    assert why() == ((f"exemplar:{pid}", "这个范例页没有可用的写作记录"),)
    # a rewrite of a page with no text yet, its job saved nothing
    blank = env[0].load("u1").state.pages[pid].page.model_copy(update={"texts": {}})
    _run(pool, "blank", pid, at=T0, snapshot=snap, mode="rewrite",
         given=blank.model_dump(mode="json"))
    assert why() == ((f"exemplar:{pid}", "这个范例页那次写作没有写出能重放的段"),)
    _run(pool, "early", pid, at=T0 + timedelta(minutes=1),
         snapshot=dict(snap, wiki_seq=0, aspect_seq=0))
    assert why() == ((f"exemplar:{pid}", "那次写作时的快照里没有这一页"),)


def _refine(pool, rid, pid, snap, *, job, sections, at=T0, dry=False):
    """A finished DeepService refine run scoped to `sections`, traced with its `job` id."""
    sink = trace.PgTraceSink(pool, clock=lambda: at)
    sink.start(rid, user_id="u1", agent="deep.write", subject=pid, pack_version=0,
               input={"page_id": pid, "snapshot": snap, "mode": "rewrite", "instruction": "",
                      "scope": {"sections": list(sections), "dry_run": dry}, "page": None,
                      "domain": None, "job_id": job})
    sink.finish(rid, stop="finished", error=None)


def test_a_section_a_scoped_run_inserted_is_a_case_from_its_saved_page(pool, env):
    """A refine scoped to s1 that inserted s2: s2 is outside the run's scope but in its job's
    saved WriteMark, so it is a case from that page, not "the runs did not touch it"."""
    _user(pool)
    pid = organized(env)
    full = env[0].load("u1").state.pages[pid].page
    commit_wiki(env, PageSaved(record=env[0].load("u1").state.pages[pid].model_copy(
        update={"page": _cut(env, pid, "s2")}), action="refined"))
    snap = _seqs(env[0])
    rid = f"refine:{pid}:x:write:0:w0"
    _refine(pool, rid, pid, snap, job="rf_2", sections=["s1"])
    assert _select(pool, env, [_sig("a", pid, "s2")]).unusable == (
        ("a", "那几次写作都没有改到这一段"),)            # nothing saved yet: truly untouched
    seq = _save(env, pid, "rf_2", ["s1", "s2"], page=full)
    out = _select(pool, env, [_sig("a", pid, "s2")])
    assert out.unusable == ()
    (case,) = out.problem
    assert (case.run_id, case.page_seq, case.mode, case.instruction) == (rid, seq, "rewrite", "")
    assert case.page["texts"]["s2"] == full.texts["s2"].model_dump(mode="json")


def test_a_later_save_that_copies_an_old_mark_is_not_the_runs_save(pool, env):
    """The job committed once before the run's snapshot; the run (same job id) never committed;
    a later save that only carries the old mark over is not what the run saved."""
    _user(pool)
    pid = organized(env)
    job = f"page:u1:{pid}:1"
    _save(env, pid, job, ["s1", "s2", "s3"])                    # the job's earlier commit
    snap = _seqs(env[0])
    _run(pool, f"{job}:write:0:w0", pid, at=T0, snapshot=snap, mode="rewrite",
         given=_cut(env, pid, "s2").model_dump(mode="json"))
    commit_wiki(env, PageSaved(record=env[0].load("u1").state.pages[pid], action="refined"))
    out = _select(pool, env, [_sig("a", pid, "s2")])
    assert out.problem == ()
    assert out.unusable == (
        ("a", "这一段是那次写作中新加的（或者当时还没有正文），但那次写作的结果没有存进页面"),)


def _revise(env, pid, action, sections):
    rec = env[0].load("u1").state.pages[pid]
    mark = WriteMark(job_id=f"revise:{action}", at=NOW, model="", sections=tuple(sections),
                     kind="rewrite")
    commit_wiki(env, PageSaved(record=rec.model_copy(update={"written": mark}), action="refined"))


def test_a_section_a_bot_revision_saved_says_so(pool, env):
    """A Bot page revision commits under revise:<action id>, which names no write run."""
    revised = ("a", ("这一段后来是按资料改写、经你同意后存进页面的；那次存页面时没有记下是哪次写作，"
                     "对不上写作记录，不能重放"))
    _user(pool)
    pid = organized(env)
    snap = _seqs(env[0])
    # the drafting run was a dry run
    _refine(pool, "draft", pid, snap, job="rf_d", sections=["s2"], dry=True)
    _revise(env, pid, "act1", ["s2"])
    assert _select(pool, env, [_sig("a", pid, "s2")]).unusable == (revised,)
    assert _select(pool, env, [_sig("a", pid, "s3")]).unusable == (
        ("a", "那几次写作都是试写，没有存进页面"),)
    # a saving run whose input lacked s2 and whose job saved nothing
    _run(pool, "w", pid, at=T0 + timedelta(minutes=1), snapshot=snap, mode="rewrite",
         given=_cut(env, pid, "s2").model_dump(mode="json"))
    assert _select(pool, env, [_sig("a", pid, "s2")]).unusable == (revised,)


def test_an_exemplar_whose_saved_page_cannot_be_read_says_so(pool, env, monkeypatch):
    _user(pool)
    pid = organized(env)
    snap = _seqs(env[0])
    job = f"page:u1:{pid}:1"
    _run(pool, f"{job}:write:0:w0", pid, at=T0, snapshot=snap, mode="rewrite",
         given=env[0].load("u1").state.pages[pid].page.model_dump(mode="json"))
    _save(env, pid, job, list(_page_sections(env, pid)))
    monkeypatch.setattr(env[0], "load_at", _failing_after(env[0].load_at, snap["wiki_seq"]))
    ex = Exemplar(exemplar_id="e1", kind="wiki_page", ref=pid, title="t", text="x")
    out = _select(pool, env, [], _spec(ex))
    assert out.anchors == ()
    assert out.unusable == ((f"exemplar:{pid}", "读不到那次写作存下的页面"),)
