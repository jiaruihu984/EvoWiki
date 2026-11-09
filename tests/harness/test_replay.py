"""A one-section replay with a candidate pack and spec (design 2026-10-21 §5.4) on a real `pool`: it
writes nothing but its own trace, the scripted writer sees the candidate, and the snapshot is the
recorded run's own (`Repo.load_at`), not the page as it is now. Scripted providers only."""

from dataclasses import replace
from datetime import timedelta

import pytest

from evowiki.deep.agent import SnapshotSeqs
from evowiki.deep.contracts import RefineScope
from evowiki.deep.store import PgAssetStore, PgJobStore
from evowiki.harness.diff import KeyOp, PackDiff
from evowiki.harness.pack import EMPTY, HarnessPack
from evowiki.harness.replay import Case, ReplayDeps, ReplayResult, candidate_spec, replay
from evowiki.llm.chat import ChatRequest
from evowiki.store import trace
from evowiki.teach.contracts import SpecPatchOp
from evowiki.wikiapp.events import PageSaved
from evowiki.wikiapp.refine_service import DeepService
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import PgRepo
from tests.deep.fakes import (
    RULES,
    DeepScript,
    FakeChecker,
    call,
    finish,
    interactive,
    library,
    section,
    self_check,
    simple_round,
    turn,
)
from tests.runtime.helpers import make_user
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import commit_wiki, organized

TABLES = ("wiki_event", "refine_job", "wiki_asset", "agent_action", "harness_value",
          "harness_version")
H1 = "候选：每个符号第一次出现时先用一句话说它是什么。"
H2 = "候选：写一段正文；一次只写一段。"
ITEM = "候选规则：先给一个能看到结果的小例子，再讲定义。"


class World:
    def __init__(self, pool):
        make_user(pool, "u1")
        self.pool, self.repo, self.ids, self.s = pool, PgRepo(pool), Ids(), Script()
        self.pid = organized((self.repo, self.s, self.ids))

    def service(self, ds: DeepScript) -> DeepService:
        llm = replace(ds.llm(), trace=trace.PgTraceSink(self.pool, lambda: NOW))
        return DeepService(repo=self.repo, jobs=PgJobStore(self.pool),
                           assets=PgAssetStore(self.pool), specs=WikiSpecSource(self.repo),
                           checker=FakeChecker(), llm=llm, clock=lambda: NOW, new_id=self.ids,
                           library=library())

    def counts(self) -> dict[str, int]:
        with self.pool.connection() as conn:
            return {t: conn.execute(f"select count(*) as n from {t}").fetchone()["n"] for t in TABLES}

    def recorded(self, ds: DeepScript, sections: tuple[str, ...] = ()) -> Case:
        """A normal (committed) refine of the page (of `sections` only, when given), traced; the
        case replays its s2."""
        svc = self.service(ds)
        res = svc.run("u1", svc.start("u1", self.pid, "user"),
                      RefineScope(page_id=self.pid, sections=sections))
        assert res.job.stage == "done", res.job.failure_detail
        with self.pool.connection() as conn:
            (run,) = trace.deep_runs_for_page(conn, "u1", self.pid, before=NOW + timedelta(1))
        inp = run["input"]
        return Case(case_id="c1", run_id=run["run_id"], page_id=self.pid, section_id="s2",
                    signal_ids=("sig1",), anchor=False, snapshot=SnapshotSeqs(**inp["snapshot"]),
                    mode=inp["mode"], instruction=inp["instruction"], user_id="u1")

    def spec(self, case: Case):
        s = case.snapshot
        return WikiSpecSource(self.repo).spec(
            self.repo.load_at("u1", s.wiki_seq, s.aspect_seq, s.infer_version))

    def replay(self, case: Case, diff: PackDiff, ds: DeepScript, pack=EMPTY) -> ReplayResult:
        return replay(case, diff, base_pack=pack, base_spec=self.spec(case),
                      deps=ReplayDeps(deep=self.service(ds), repo=self.repo))

    def first_request(self, run_id: str) -> ChatRequest:
        with self.pool.connection() as conn:
            return trace.request_at(conn, run_id, 1)


def log_writer_models(pool, log):
    """`ReplayDeps.writer_models` for a scripted deep tier whose calls are in its in-memory `log`
    (not llm_call): the run's traced model steps, looked up there."""
    from evowiki.llm.models import ModelRef
    from evowiki.store.db import fetch_all

    def models(run_id: str) -> tuple[ModelRef, ...]:
        with pool.connection() as conn:
            rows = fetch_all(conn, "select call_id from agent_step where run_id = %s "
                                   "and kind = 'model' order by seq", (run_id,))
        by_id = {c.id: c for c in log.calls()}
        calls = [by_id[r["call_id"]] for r in rows if r["call_id"] in by_id]
        return tuple(dict.fromkeys(ModelRef(provider=c.provider, model=c.model)
                                   for c in calls if c.purpose == "deep.write"))
    return models


@pytest.fixture
def world(pool):
    return World(pool)


def candidate() -> PackDiff:
    return PackDiff(ops=(KeyOp("H1.write", H1), KeyOp("H2.deep.write_section", H2)),
                    spec_ops=(SpecPatchOp(op="add_item", item_id="h1", text=ITEM),))


def candidate_round(i: int, *, with_interactive: bool = False) -> list[dict]:
    """`simple_round` under the candidate spec: finish carries a self-check line for its new item."""
    body = "s2 精修正文。\n\n{{interactive:pair-demo}}\n" if with_interactive else None
    first = [call("read_spec", i, part="items"),
             section("s2", i, **({"body": body} if body else {}))]
    if with_interactive:
        first.append(interactive(i=i))
    return [turn(*first), turn(finish(i, self_check(rules=(*RULES, "h1"))))]


def writes(ds: DeepScript) -> list[ChatRequest]:
    return [r for r in ds.requests if isinstance(r, ChatRequest) and r.purpose == "deep.write"]


def test_a_replay_writes_nothing_but_its_own_trace(world):
    ds = DeepScript().plan().write(*simple_round()).plan().write(
        *candidate_round(2, with_interactive=True))
    case = world.recorded(ds)
    before = world.counts()
    res = world.replay(case, candidate(), ds)
    assert world.counts() == before          # no page, section log, job, asset, action or pack row
    assert res.case_id == "c1" and "s2 精修正文" in res.text and "A 对 T，G 对 C" in res.text
    assert res.writer_provider == "anthropic"
    assert res.cost > 0                       # the replay's own priced calls (Sonnet turns)
    assert res.run_id.startswith(trace.REPLAY_RUN_PREFIX) and res.run_id != case.run_id
    with world.pool.connection() as conn:
        run = trace.run(conn, res.run_id)
        assert run["input"]["job"]["trigger"] == "harness_replay"
        assert run["input"]["scope"] == {"sections": ["s2"], "dry_run": True}
        assert run["finished_at"] is not None
        # a replay is never a case source: only the recorded run is listed for the page
        listed = trace.deep_runs_for_page(conn, "u1", case.page_id, before=NOW + timedelta(1))
    assert [r["run_id"] for r in listed] == [case.run_id]


def test_the_writer_sees_the_candidate_pack_and_spec(world):
    ds = DeepScript().plan().write(*simple_round()).plan().write(*candidate_round(2))
    case = world.recorded(ds)
    n = len(writes(ds))
    world.replay(case, candidate(), ds)
    first = writes(ds)[n]
    assert H1 in first.system                                          # H1
    (ws,) = [t for t in first.tools if t.name == "write_section"]
    assert ws.description == H2                                        # H2
    assert ITEM in first.messages[0].text                              # H7 (the spec copy)
    assert H1 not in writes(ds)[0].system                              # the recorded run had none


def test_the_writer_runs_under_the_candidates_cap(world):
    """H4.deep.max_tool_calls=10: the 11th call of a round (finish) is refused, so the round ends
    without a draft and a second round is written."""
    reads = [call("read_spec", i, part="items") for i in range(1, 10)]
    capped = [turn(*reads, section("s2", 20), finish(21))]
    ds = DeepScript().plan().write(*simple_round()).plan().write(*capped, *simple_round(3))
    case = world.recorded(ds)
    n = len(writes(ds))
    res = world.replay(case, PackDiff(ops=(KeyOp("H4.deep.max_tool_calls", 10),)), ds)
    rounds = writes(ds)[n:]
    assert len(rounds) == 4                  # round 0 (plan_page, capped), round 1 (two turns)
    assert "上一轮没有调用 finish" in rounds[2].messages[0].text
    assert "s2 精修正文" in res.text


def test_an_empty_diff_sends_the_recorded_request_except_the_scope_note(world):
    ds = DeepScript().plan().write(*simple_round()).plan().write(*simple_round(2))
    case = world.recorded(ds)
    res = world.replay(case, PackDiff(), ds)
    old, new = world.first_request(case.run_id), world.first_request(res.run_id)
    assert new.system == old.system and new.tools == old.tools
    note = "这次只改写这些段：s2（可以插入新段）"
    assert note not in old.messages[0].text and note in new.messages[0].text
    assert new.messages[0].text.replace("\n\n" + note, "") == old.messages[0].text


def test_a_reference_replay_draws_a_fresh_sample_not_the_recorded_one(world):
    """The recorded run was a one-section rewrite of s2, so the empty-diff replay sends exactly
    its requests; the replay's salt still keeps the recorded plan and turns out of it."""
    ds = DeepScript().plan().write(*simple_round())
    case = world.recorded(ds, sections=("s2",))
    assert ds.pending() == {}
    fresh = [turn(call("read_spec", 2, part="items"), section("s2", 2, body="重放的新正文。")),
             turn(finish(2))]
    ds.plan().write(*fresh)
    res = world.replay(case, PackDiff(), ds)
    assert ds.pending() == {}                       # a new plan and both turns were drawn
    assert ds.count("deep.write") == 6 and "重放的新正文。" in res.text     # plan_page + 2, twice
    assert world.first_request(res.run_id) == world.first_request(case.run_id)
    assert res.cost > 0


def test_the_replay_reads_the_runs_snapshot_not_the_page_now(world):
    ds = DeepScript().plan().write(*simple_round()).plan().write(*simple_round(2)).plan().write(
        *simple_round(3))
    case = world.recorded(ds)
    first = world.replay(case, PackDiff(), ds)
    rec = world.repo.load("u1").state.pages[case.page_id]
    commit_wiki((world.repo, world.s, world.ids),
                PageSaved(record=rec.model_copy(update={"page": None}), action="rewritten"))
    assert world.repo.load("u1").state.pages[case.page_id].page is None
    again = world.replay(case, PackDiff(), ds)
    assert again.run_id != first.run_id
    assert world.first_request(again.run_id) == world.first_request(first.run_id)
    with pytest.raises(LookupError):         # a section the snapshot does not have
        world.replay(replace(case, section_id="s9"), PackDiff(), ds)
    now = world.repo.load("u1")              # the snapshot after the page was emptied
    with pytest.raises(LookupError):
        world.replay(replace(case, snapshot=SnapshotSeqs(now.wiki_seq, now.aspect_seq,
                                                         now.infer_version)), PackDiff(), ds)


def test_an_unloadable_snapshot_raises(world):
    ds = DeepScript().plan().write(*simple_round())
    case = world.recorded(ds)
    with pytest.raises(LookupError):
        world.replay(replace(case, user_id="nobody"), PackDiff(), ds)


def test_candidate_spec_reuses_the_rsib_ops_on_a_copy(world):
    case_spec = WikiSpecSource(world.repo).spec(world.repo.load("u1"))
    out = candidate_spec(case_spec, candidate().spec_ops)
    assert [i.text for i in out.items][-1] == ITEM and out.version == case_spec.version
    assert ITEM not in [i.text for i in case_spec.items]
    assert candidate_spec(case_spec, ()) is case_spec


def test_the_pack_is_the_base_with_the_diff(world):
    ds = DeepScript().plan().write(*simple_round()).plan().write(*simple_round(2))
    case = world.recorded(ds)
    n = len(writes(ds))
    base = HarnessPack(3, {"H1.write": "基础的写法。", "H2.deep.write_section": "基础说明。"})
    world.replay(case, PackDiff(ops=(KeyOp("H2.deep.write_section", None),)), ds, pack=base)
    first = writes(ds)[n]
    assert "基础的写法。" in first.system
    (ws,) = [t for t in first.tools if t.name == "write_section"]
    (default,) = [t for t in writes(ds)[0].tools if t.name == "write_section"]
    assert ws.description == default.description     # reset to the default (the recorded run's)


def test_a_section_the_writer_added_replays_from_the_page_its_job_saved(world):
    """The recorded refine writes s2 and adds s2x after it; a signal on s2x gets a case built on
    the page that refine saved (`harness.cases`), and its replay rewrites s2x in the sandbox."""
    from evowiki.harness.cases import select_cases
    from evowiki.teach.contracts import Signal

    added = section("s2x", 1, after="s2", heading="新加的一段", body="s2x 原来的正文。")
    first = [turn(call("read_spec", 1, part="items"), section("s2", 1), added), turn(finish(1))]
    again = [turn(call("read_spec", 2, part="items"), section("s2x", 2, body="s2x 重放的新正文。")),
             turn(finish(2))]
    ds = DeepScript().plan().write(*first).plan().write(*again)
    recorded = world.recorded(ds)
    with world.pool.connection() as conn:
        run = trace.run(conn, recorded.run_id)
    assert run["input"]["job_id"].startswith("rf")          # the refine's own job id is traced
    sig = Signal(signal_id="sig1", kind="page_feedback", at=NOW + timedelta(hours=1),
                 page_id=world.pid, section_id="s2x", chips=("unclear",))
    with world.pool.connection() as conn:
        cs = select_cases(conn, "u1", [sig], world.spec(recorded), now=NOW + timedelta(hours=2),
                          repo=world.repo)
    assert cs.unusable == ()
    (case,) = cs.problem
    (seq,) = [q for q, ev in world.repo.wiki_events_since("u1", {"page_saved"})
              if ev.body.record.written and ev.body.record.written.job_id == run["input"]["job_id"]]
    assert (case.run_id, case.mode, case.page_seq) == (recorded.run_id, "rewrite", seq)
    assert case.page["texts"]["s2x"]["body"] == "s2x 原来的正文。"
    before = world.counts()
    res = world.replay(case, PackDiff(), ds)
    assert world.counts() == before
    assert "s2x 重放的新正文。" in res.text
    with world.pool.connection() as conn:
        rp = trace.run(conn, res.run_id)
    assert rp["input"]["scope"] == {"sections": ["s2x"], "dry_run": True}
    assert rp["input"]["mode"] == "rewrite" and "s2x" in rp["input"]["page"]["texts"]
    assert rp["input"]["snapshot"] == run["input"]["snapshot"]     # the run's own snapshot
