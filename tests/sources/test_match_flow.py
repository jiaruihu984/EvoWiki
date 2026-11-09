"""The match workflow body (`sources.match_flow.run_match`) in a virtual workflow: real Postgres,
HashingEmbedder, a scripted JsonLLM. No model is ever called."""

from datetime import UTC, datetime

import pytest

from evowiki.agent import durable
from evowiki.goals.contracts import OutlineSection, PageOutline, SectionRole, SectionText
from evowiki.goals.fold import Page
from evowiki.llm.gateway import LLMParseError
from evowiki.llm.usage import Account, BudgetExceededError
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import match_flow, store
from evowiki.sources.document import Block, DocPage, SourceDocument
from evowiki.sources.match import MatchPlan, MatchVerdict
from evowiki.sources.match_flow import MatchDeps, run_match, start_match, workflow_id
from evowiki.wikiapp.events import (
    PageRecord,
    PageSaved,
    PageSource,
    PagesTrashed,
    SourceMatched,
    WikiEvent,
)
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.sources._fakes import FakeLLM
from tests.sources.test_importer import (
    add_source,
    env,  # noqa: F401  (the import fixture)
    run,
)

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
MATERIAL = (b"# Mean\n\nThe mean is the sum of the values divided by how many values there are. "
            b"The mean of 2 4 6 is 4.\n\n# Median\n\nThe median is the middle value when the "
            b"values are sorted. The median of 1 3 9 is 3.\n")


def record(pid: str, title: str, body: str) -> PageRecord:
    outline = PageOutline(page_id=pid, goal="g", concepts=("c1",), targets=("c1",), sections=(
        OutlineSection(id="s1", heading=title, role=SectionRole.EXPLAIN),))
    page = Page(outline=outline, states={},
                texts={"s1": SectionText(section_id="s1", body=body, cache_key=pid)})
    return PageRecord(page_id=pid, title=title, goal="g", concepts=("c1",),
                      targets=("c1",), page=page, created_at=NOW)


def save(repo: PgRepo, uid: str, *bodies, trashed=()) -> None:
    snap = repo.load(uid)
    evs = [WikiEvent(event_id=f"ev-{uid}-{i}", ts=NOW, body=b) for i, b in enumerate(bodies)]
    if trashed:
        evs.append(WikiEvent(event_id=f"ev-{uid}-t", ts=NOW, body=PagesTrashed(page_ids=trashed)))
    repo.commit(snap, Commit(wiki=evs))


@pytest.fixture
def flow(env):  # noqa: F811
    pool, _box, import_deps = env
    repo = PgRepo(pool)
    save(repo, "u1",
         PageSaved(record=record("pmean", "Mean", "the mean is the sum of the values divided by "
                                 "how many values there are"), action="done"),
         PageSaved(record=record("pmed", "Median", "the median is the middle of the sorted "
                                 "values"), action="done"),
         PageSaved(record=record("pbio", "Photosynthesis", "plants turn light into sugar with "
                                 "chlorophyll"), action="done"),
         PageSaved(record=record("pold", "Mean again", "the mean is the sum of the values"),
                   action="done"),
         PageSaved(record=PageRecord(page_id="pnote", title="Mean notes", goal="g",
                                     concepts=(), targets=(), created_at=NOW),
                   action="add"),
         trashed=("pold",))
    add_source(pool, "s1", MATERIAL, kind="md")
    assert run(import_deps(), "s1")[-1][0] == "done"
    box = {"llm": None, "notified": [], "import_deps": import_deps}
    deps = MatchDeps(pool=pool, repo=repo, embedder=HashingEmbedder(),
                     llm_for=lambda _u: box["llm"], clock=lambda: NOW,
                     notify=box["notified"].append)
    return pool, repo, deps, box


def go(deps, uid="u1", sid="s1") -> str:
    with durable.virtual_workflow(workflow_id(uid, sid)):
        return run_match(uid, sid, deps)


def matched(repo, uid="u1") -> list[SourceMatched]:
    return [e.body for e in repo.wiki_events_of(uid, ["source_matched"])]


def use(pool, u, sid="s1"):
    with pool.connection() as conn:
        store.add_use(conn, "u1", sid, u, now=NOW)


def test_section_vectors_are_read_under_the_embedder_tag(flow):
    pool, _repo, deps, _box = flow
    e = deps.embedder
    with pool.connection() as conn:
        vecs = store.section_vectors(conn, "u1", "s1", (e.id, e.model_version, e.dim))
        secs = store.sections(conn, "u1", "s1")
        assert store.section_vectors(conn, "u1", "s1", ("other", e.model_version, e.dim)) == {}
        assert store.section_vectors(conn, "u2", "s1", (e.id, e.model_version, e.dim)) == {}
    assert set(vecs) == {s.section_id for s in secs} and all(len(v) == e.dim for v in vecs.values())


def test_a_supplement_records_the_judged_pages_and_wakes_the_bot(flow):
    _pool, repo, deps, box = flow
    box["llm"] = llm = FakeLLM(MatchPlan(verdicts=[MatchVerdict(n=1, sections=[1], why="多了例子"),
                                                   MatchVerdict(n=2, sections=[])]))
    assert go(deps) == "matched"
    (ev,) = matched(repo)
    assert ev.source_id == "s1" and ev.title == "s1"
    assert [p.page_id for p in ev.pages] == ["pmean"] and ev.pages[0].why == "多了例子"
    assert ev.pages[0].section_ids                       # the material's sections behind it
    assert box["notified"] == ["u1"]
    ((_r, purpose, prompt),) = llm.calls
    assert purpose == "sources.match"
    # trashed pages and the user's own notes (no EvoWiki page) are never candidates
    assert "Mean again" not in prompt and "Mean notes" not in prompt
    assert "Photosynthesis" not in prompt                # below the vectors' first cut
    # a second run of the same source records nothing more
    box["llm"] = FakeLLM()
    assert go(deps) == "skipped"
    assert len(matched(repo)) == 1 and box["notified"] == ["u1"]


def test_a_supplement_use_is_matched_too(flow):
    pool, repo, deps, box = flow
    use(pool, "supplement")
    use(pool, "ask")
    box["llm"] = FakeLLM(MatchPlan(verdicts=[]))
    assert go(deps) == "matched"
    assert [e.pages for e in matched(repo)] == [()]


def test_pages_the_material_was_added_for_or_written_from_are_not_candidates(flow):
    """A revise from pasted text names its page; a page written from the material cites it:
    neither is offered again for the same material."""
    pool, repo, deps, box = flow
    with pool.connection() as conn:
        store.add_use(conn, "u1", "s1", "supplement", page_id="pmean", now=NOW)
    rec = record("pmed", "Median", "the median is the middle of the sorted values")
    cited = rec.model_copy(update={"sources": (PageSource(source_id="s1", title="s1"),)})
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id="ev-u1-cited", ts=NOW, body=PageSaved(record=cited, action="done"))]))
    box["llm"] = llm = FakeLLM(MatchPlan(verdicts=[]))
    assert go(deps) == "matched"
    for _r, _purpose, prompt in llm.calls:
        assert "Mean" not in prompt and "Median" not in prompt


@pytest.mark.parametrize("u", ["learn", "ask", "understand", "exam"])
def test_other_uses_are_not_matched(flow, u):
    pool, repo, deps, box = flow
    use(pool, u)
    box["llm"] = llm = FakeLLM()
    assert go(deps) == "skipped"
    assert matched(repo) == [] and box["notified"] == [] and llm.calls == []


def test_no_candidate_above_the_line_records_an_empty_match_without_a_call(flow):
    pool, repo, deps, box = flow
    add_source(pool, "s2", b"# Volcano\n\nlava magma eruption crater ash\n", kind="md")
    assert run(box["import_deps"](), "s2")[-1][0] == "done"
    box["llm"] = llm = FakeLLM()
    assert go(deps, sid="s2") == "matched"
    assert [e.pages for e in matched(repo)] == [()] and llm.calls == []
    assert box["notified"] == ["u1"]


@pytest.mark.parametrize("fail", [LLMParseError("not json"),
                                  BudgetExceededError(Account(user_id="u1", group="other"), "day", 1.0)])
def test_a_failed_judgement_records_nothing(flow, fail):
    _pool, repo, deps, box = flow
    box["llm"] = FakeLLM(fail)
    assert go(deps) == "failed"
    assert matched(repo) == [] and box["notified"] == []


def test_without_a_model_nothing_is_recorded(flow):
    _pool, repo, deps, box = flow
    assert go(deps) == "failed"
    assert matched(repo) == [] and box["notified"] == []


def test_no_section_vectors_under_the_embedder_fails_without_an_event(flow, caplog):
    import dataclasses
    _pool, repo, deps, box = flow
    box["llm"] = llm = FakeLLM()
    other = dataclasses.replace(deps, embedder=HashingEmbedder(dim=64))   # another tag
    with caplog.at_level("WARNING", logger=match_flow.log.name):
        assert go(other) == "failed"
    assert "no section vectors" in caplog.text
    assert matched(repo) == [] and box["notified"] == [] and llm.calls == []
    box["llm"] = FakeLLM(MatchPlan(verdicts=[]))
    assert go(deps) == "matched"                          # nothing recorded: a later run still can


def test_a_deleted_source_is_not_matched(flow):
    pool, repo, deps, box = flow
    with pool.connection() as conn:
        store.soft_delete(conn, "u1", "s1", NOW)
    box["llm"] = FakeLLM()
    assert go(deps) == "skipped" and matched(repo) == []


def test_start_match_starts_only_for_supplements(flow, monkeypatch):
    pool, _repo, deps, _box = flow
    started: list[str] = []
    monkeypatch.setattr(match_flow, "_start", lambda wid, u, s: started.append(wid))
    match_flow.install(deps)
    try:
        start_match("u1", "s1")                          # no use yet: a supplement
        use(pool, "learn")
        start_match("u1", "s1")
        add_source(pool, "s3", b"x", kind="text")
        use(pool, "supplement", "s3")
        start_match("u1", "s3")
    finally:
        match_flow.uninstall()
    assert started == ["source_match:u1:s1", "source_match:u1:s3"]


def test_start_match_never_raises(monkeypatch):
    match_flow.uninstall()
    start_match("u1", "s1")                              # no deps installed: logged only


def test_the_import_asks_for_a_match_once_it_is_done(env):  # noqa: F811
    pool, _box, import_deps = env
    calls: list[tuple[str, str]] = []
    add_source(pool, "s1", MATERIAL, kind="md")
    items = run(import_deps(on_ready=lambda u, s: calls.append((u, s))), "s1")
    assert items[-1][0] == "done" and calls == [("u1", "s1")]
    # a hook that fails does not fail the import
    add_source(pool, "s2", MATERIAL + b" ", kind="md")

    def boom(_u, _s):
        raise RuntimeError("no DBOS")
    assert run(import_deps(on_ready=boom), "s2")[-1][0] == "done"


def _hook_threads_done(timeout: float = 5.0) -> None:
    import threading
    import time
    end = time.monotonic() + timeout
    while any(t.name == "source-match-use" for t in threading.enumerate()):
        assert time.monotonic() < end, "the hook's thread did not end"
        time.sleep(0.05)


def test_a_supplement_use_starts_the_match_once_committed(flow, monkeypatch):
    """C's 资料加入 hook: a supplement use on a material whose import is done starts the match,
    but only after the use's transaction committed (a workflow reading the uses earlier could
    end for good under its fixed id)."""
    from evowiki.sources import events
    pool, _repo, deps, _box = flow
    use(pool, "learn")                                   # not matched on its own
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(match_flow, "start_match", lambda u, s: started.append((u, s)))
    match_flow.install(deps)
    try:
        with pool.connection() as conn:
            events.source_added(conn, "u1", "s1", "supplement", NOW)
            import time
            time.sleep(0.3)
            assert started == []                         # not committed yet
        _hook_threads_done()
        assert started == [("u1", "s1")]
        # a material still importing: its own on_ready starts the match later, not the hook
        add_source(pool, "s3", b"x", kind="text")
        with pool.connection() as conn:
            events.source_added(conn, "u1", "s3", "supplement", NOW)
        _hook_threads_done()
        # other uses start nothing
        with pool.connection() as conn:
            events.source_added(conn, "u1", "s1", "exam", NOW)
        _hook_threads_done()
    finally:
        match_flow.uninstall()
    assert started == [("u1", "s1")]


def test_a_rolled_back_supplement_use_starts_nothing(flow, monkeypatch, caplog):
    from evowiki.sources import events
    pool, _repo, deps, _box = flow
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(match_flow, "start_match", lambda u, s: started.append((u, s)))
    monkeypatch.setattr(match_flow, "USE_WAIT_S", 0.3)
    match_flow.install(deps)
    try:
        with pytest.raises(RuntimeError), pool.connection() as conn:
            events.source_added(conn, "u1", "s1", "supplement", NOW)
            raise RuntimeError("the caller failed after the use")
        _hook_threads_done()
    finally:
        match_flow.uninstall()
    assert started == [] and "never became visible" in caplog.text


def test_without_a_match_runtime_the_hook_does_nothing(flow):
    from evowiki.sources import events
    pool, *_ = flow
    match_flow.uninstall()
    with pool.connection() as conn:
        events.source_added(conn, "u1", "s1", "supplement", NOW)
    _hook_threads_done(0.5)


def test_matched_sections_carry_their_locators(flow):
    pool, repo, deps, box = flow
    with pool.connection() as conn:
        secs = store.sections(conn, "u1", "s1")
        for s in secs:
            conn.execute("update source_section set page_from = 1 where section_id = %s",
                         (s.section_id,))
        store.put_document(conn, "u1", "s1", SourceDocument(
            kind="pdf", backend="mineru", version="4", pages=(DocPage(1),),
            blocks=tuple(Block(f"p1-b{i}", 1, "heading", s.title)
                         for i, s in enumerate(secs))), NOW)
    box["llm"] = FakeLLM(MatchPlan(verdicts=[MatchVerdict(n=1, sections=[1, 2], why="例子")]))
    assert go(deps) == "matched"
    (ev,) = matched(repo)
    page = ev.pages[0]
    assert len(page.locators) == len(page.section_ids) == 2
    ids = {s.title: f"p1-b{i}" for i, s in enumerate(secs)}
    assert {loc["block_id"] for loc in page.locators} <= set(ids.values())
    assert all(loc["source_id"] == "s1" and loc["page"] == 1 for loc in page.locators)
