"""Deep refine end to end over MemoryRepo with a scripted Gateway and a fake browser: the agent loop,
the rewrite cap, fail-closed paths, page_changed, the page budget and dry_run."""

import pytest

from evowiki.deep.agent import TRUNCATED_NOTE
from evowiki.deep.contracts import BUILTIN_REVIEW_RULES, RefineScope, RefineSettings
from evowiki.deep.store import MemoryAssetStore, MemoryJobStore
from evowiki.goals.language import language_rule
from evowiki.llm.usage import BudgetExceededError
from evowiki.wikiapp.events import PageSaved, RefineSettingsSet
from evowiki.wikiapp.page import open_page_flow, set_fold
from evowiki.wikiapp.refine_service import DeepService, JobRunning
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import MemoryRepo
from tests.deep.fakes import (
    RULES,
    DeepScript,
    FakeChecker,
    call,
    component,
    finish,
    interactive,
    library,
    library_empty,
    section,
    self_check,
    simple_round,
    turn,
)
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import commit_wiki, declare, organized


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def service(env, ds: DeepScript, checker=None, *, llm="scripted", meter=None,
            lib="default") -> DeepService:
    repo, _, ids = env
    return DeepService(repo=repo, jobs=MemoryJobStore(), assets=MemoryAssetStore(),
                       specs=WikiSpecSource(repo), checker=checker or FakeChecker(),
                       llm=ds.llm(meter) if llm == "scripted" else llm,
                       clock=lambda: NOW, new_id=ids,
                       library=library() if lib == "default" else lib)


def refine(svc: DeepService, pid: str, **scope):
    job = svc.start("u1", pid, "user")
    return svc.run("u1", job, RefineScope(page_id=pid, **scope) if scope else None)


def record(env, pid):
    return env[0].load("u1").state.pages[pid]


# ---- happy path ----

def test_plan_write_self_check_render_commit(env):
    pid = organized(env)
    set_fold(env[0], "u1", pid, "s2", True, now=NOW, new_id=env[2])     # the user folded s2
    ds = DeepScript().plan().write(*simple_round(with_interactive=True))
    svc = service(env, ds)
    res = svc.run("u1", svc.start("u1", pid, "user"))

    job = res.job
    assert (job.stage, job.failure, job.round) == ("done", None, 1)
    assert job.changed_sections == ("s2",) and len(job.interactives) == 1
    assert job.cost_usd == pytest.approx(3 * 0.007)          # plan_page + two write turns
    assert job.model == "anthropic:claude-sonnet-5-5" and job.spec_version == 1
    stages = [j.stage for u, j in svc.jobs.rows]
    assert stages == ["queued", "queued", "write", "render", "commit", "done"]   # no plan stage
    assert ds.count("deep.review") == 0 and ("anthropic", "deep.write") in ds.served
    assert [f.rule for f in job.self_check] == list(RULES) and all(f.ok for f in job.self_check)

    rec = record(env, pid)
    page = rec.page
    asset_id = job.interactives[0]
    body = page.texts["s2"].body
    assert "{{interactive:" not in body
    assert f"```embedded-html\ntype: embedded-html\nid: pair-demo\nsrc: assets/{asset_id}.html" in body
    assert page.texts["s2"].cue == "A 对 T，G 对 C"
    assert page.state("s2").folded and page.state("s2").fold_locked       # fold kept
    s2 = next(s for s in page.outline.sections if s.id == "s2")
    assert s2.heading == "配对规则怎么来" and s2.cells == ("c1.procedure", "c1.interpret")
    s4 = next(s for s in page.outline.sections if s.id == "s4")
    assert s4.check is not None                                           # check untouched
    assert rec.changed == {"s2": "rewritten"}
    assert rec.written.job_id == job.job_id and rec.written.sections == ("s2",)
    assert rec.written.kind == "rewrite" and rec.written.cost_usd == pytest.approx(job.cost_usd)

    log = env[0].load("u1").state.section_log(pid, "s2")          # R1 structured log
    assert (log.producer, log.spec_version, log.cells) == ("refine", 1,
                                                           ("c1.procedure", "c1.interpret"))
    assert log.spec_items == RULES[len(BUILTIN_REVIEW_RULES):]
    for r in ds.requests:                                          # language rule on every prompt
        assert language_rule("zh") in r.messages[-1].text
    spec_read = next(m.text for r in ds.requests if r.purpose == "deep.write"
                     for m in r.messages if m.role == "tool" and "样板页" in m.text)
    assert "讲法样板" in spec_read                            # bundled exemplar (v1 has none)

    a = svc.assets.get("u1", asset_id)
    assert a.meta.section_id == "s2" and a.meta.check.ok and a.html.startswith("<!doctype")
    out = svc.out("u1", pid)
    assert out.job == job and [m.asset_id for m in out.assets] == [asset_id]


def test_page_out_shows_the_writer_and_the_changed_sections(env):
    pid = organized(env)
    ds = DeepScript().plan().write(*simple_round())
    job = refine(service(env, ds), pid).job
    out = open_page_flow(env[0], None, "u1", pid, now=NOW, new_id=env[2])
    assert out.writer.model == "anthropic:claude-sonnet-5-5" and out.writer.fallback_reason is None
    assert out.writer.cost_usd == pytest.approx(job.cost_usd)
    assert next(s for s in out.sections if s.id == "s2").changed == "rewritten"


def test_insert_new_section_after_existing(env):
    pid = organized(env)
    new = section("n1", heading="动手拼一次", after="s2", role="explain", depth="quick",
                  cue="拼一次", body="新段正文", cells=["c1.procedure"])
    ds = DeepScript().plan().write(turn(new), turn(finish()))
    res = refine(service(env, ds), pid)
    assert res.job.stage == "done"
    ids = [s.id for s in record(env, pid).page.outline.sections]
    assert ids == ["s1", "s2", "n1", "s3", "s4"]
    assert record(env, pid).changed == {"n1": "inserted"}


# ---- the plan's `known` comes from K ----

def test_known_is_written_from_k_not_by_the_model(env):
    pid = organized(env)
    declare(env, "know", "c2.term")
    ds = DeepScript().plan().write(*simple_round())
    res = refine(service(env, ds), pid)
    known = res.draft.plan.known
    assert known.splitlines()[0] == "确定会：标准差·术语 / 定义"       # the only known cell
    # D10: neither the goal's background nor the organizer's own 画像 reaches the writer as his
    # words; what he said about himself comes only from the profile block (the gateway)
    assert "画像" not in known and "我会算 z 分数" not in known
    assert "z 分数" not in known.splitlines()[0]                      # 模糊 / 没证据 cells stay out
    first = next(r for r in ds.requests if r.purpose == "deep.write").messages[0].text
    assert known in first                                   # what the plan's known will be


def test_the_writer_gets_every_page_concept_scope_from_the_graph_and_k(env):
    """The scope the goal's split wrote (the node's definition) and the concept's layers in K
    reach the writer (audit D09)."""
    from dataclasses import replace

    from evowiki.deep.agent import scope_block
    from evowiki.goals.scope import concept_scope
    from evowiki.wikiapp.refine_apply import refine_input
    pid = organized(env)
    snap = env[0].load("u1")
    cid = snap.state.pages[pid].concepts[0]
    defined = "只讲定义和几何意义，不讲怎么算。"
    graph = snap.state.graph.model_copy(update={"nodes": tuple(
        n.model_copy(update={"definition": defined}) if n.id == cid else n
        for n in snap.state.graph.nodes)})
    snap = replace(snap, state=snap.state.model_copy(update={"graph": graph}))
    inp = refine_input(snap, pid, NOW, WikiSpecSource(env[0]))
    line = concept_scope(snap.state.names[cid].name, defined, snap.kview(NOW).aspects(cid))
    assert f"- {line}" in scope_block(inp)[0].splitlines()
    # on a page written from materials the definition stays out, the layers stay
    with_src = replace(inp, sources=(object(),))
    assert defined not in scope_block(with_src)[0] and "讲的层面：" in scope_block(with_src)[0]
    # the stored page (no definition in this fixture) gets its concepts' layers
    block = scope_block(refine_input(env[0].load("u1"), pid, NOW, WikiSpecSource(env[0])))[0]
    assert "讲的层面：" in block
    ds = DeepScript().plan().write(*simple_round())
    refine(service(env, ds), pid)
    write = next(r for r in ds.requests if r.purpose == "deep.write")
    assert block in write.messages[0].text


# ---- tools refuse what refine may not touch ----

def test_check_section_and_foreign_cells_are_refused_to_the_model(env):
    pid = organized(env)
    ds = DeepScript().plan().write(
        turn(section("s4", heading="改题"), section("s3", cells=["c9.term"])),
        turn(*simple_round()[0]["tool_calls"]), turn(finish()))
    res = refine(service(env, ds), pid)
    assert res.job.stage == "done"
    second = [r for r in ds.requests if r.purpose == "deep.write"][2]   # after plan_page + the writes
    errors = [m.text for m in second.messages if m.role == "tool" and m.is_error]
    assert any("确认题段不能" in e for e in errors) and any("不属于本页概念" in e for e in errors)
    assert record(env, pid).changed == {"s2": "rewritten"}


def test_scope_limits_rewrites(env):
    pid = organized(env)
    ds = DeepScript().plan().write(turn(section("s1")), turn(section("s2", 2)),
                                   turn(finish()))
    res = refine(service(env, ds), pid, sections=("s2",))
    assert res.job.stage == "done" and res.job.changed_sections == ("s2",)


# ---- self-check (recorded, not a gate) and rounds ----

def test_self_check_is_recorded_and_never_blocks(env):
    pid = organized(env)
    # a failing self-check gets one more round (must-fix = the failing items); a second failing
    # self-check commits anyway
    ds = DeepScript().plan().write(turn(section("s2")), turn(finish(checks=self_check(ok=False))),
                                   turn(section("s2", 2)),
                                   turn(finish(2, checks=self_check(ok=False))))
    res = refine(service(env, ds), pid)
    assert (res.job.stage, res.job.round) == ("done", 2)          # a failing self-check commits
    bad = [f for f in res.job.self_check if not f.ok]
    assert [f.rule for f in bad] == ["coherence"] and "已改" in bad[0].note
    assert [f.rule for f in res.job.self_check] == list(RULES)
    assert record(env, pid).written is not None
    assert ds.count("deep.review") == 0 and ds.pending() == {}


def test_finish_needs_one_self_check_line_per_rule(env):
    pid = organized(env)
    partial = self_check(rules=RULES[:3])
    ds = DeepScript().plan().write(turn(section("s2")), turn(finish(checks=partial)),
                                   turn(finish(2)))
    res = refine(service(env, ds), pid)
    assert res.job.stage == "done" and res.job.round == 1
    third = [r for r in ds.requests if r.purpose == "deep.write"][3]
    err = next(m.text for m in third.messages if m.role == "tool" and m.is_error)
    assert "自查还缺这些规则" in err and RULES[3] in err


def test_the_write_prompt_lists_the_self_check_rules_and_the_interactive_rule(env):
    pid = organized(env)
    ds = DeepScript().plan().write(*simple_round())
    refine(service(env, ds), pid)
    first = next(r for r in ds.requests if r.purpose == "deep.write")
    text = first.messages[0].text
    assert "自查清单" in text and all(f"- {rule}:" in text for rule in RULES)
    assert "按需要做交互" in text                                     # interactive_bias 1
    assert "四、交互" in first.system


def test_a_placement_reports_its_render_check_and_a_fixed_one_is_kept(env):
    """T4: place_component runs the render check and says how it went; the writer fixes it in
    the same round (here: the check is run again), no extra round."""
    pid = organized(env)
    runs = []

    def flaky(d):                       # the component fails its first check only
        runs.append(d.interactive_id)
        return "页面异常" if len(runs) == 1 else None
    checker = FakeChecker(flaky)
    body = "s2 正文\n{{interactive:pair-demo}}"
    ds = (DeepScript().plan()
          .write(turn(section("s2", body=body), interactive()),
                 turn(call("render_check", 2, interactive_id="pair-demo")), turn(finish())))
    res = refine(service(env, ds, checker), pid)
    assert (res.job.stage, res.job.round, res.job.dropped) == ("done", 1, ())
    assert len(res.job.interactives) == 1 and runs == ["pair-demo", "pair-demo"]
    placed = next(m for r in ds.requests if r.purpose == "deep.write" for m in r.messages
                  if m.role == "tool" and "已放置组件" in m.text)
    assert placed.is_error and "没通过" in placed.text and "页面异常" in placed.text


def test_an_interactive_that_still_fails_at_finish_is_dropped_without_another_round(env):
    """T4: the render gate drops what still fails (DroppedInteractive) and commits the text; it
    never costs a must-fix round."""
    pid = organized(env)
    checker = FakeChecker(lambda d: "按钮点了没反应")
    ds = DeepScript().plan().write(*simple_round(1, with_interactive=True))
    svc = service(env, ds, checker)
    res = refine(svc, pid)
    assert (res.job.stage, res.job.failure, res.job.round) == ("done", None, 1)
    [d] = res.job.dropped
    assert (d.interactive_id, d.section_id) == ("pair-demo", "s2") and "按钮点了没反应" in d.reason
    assert res.job.interactives == () and not svc.assets.rows
    body = record(env, pid).page.texts["s2"].body
    assert "interactive" not in body and body.startswith("s2 精修正文")
    assert ds.pending() == {}


def test_agent_reuses_its_own_render_check(env):
    pid = organized(env)
    checker = FakeChecker()
    first = simple_round(with_interactive=True)
    ds = DeepScript().plan().write(first[0], turn(call("render_check", interactive_id="pair-demo")),
                                   first[1])
    assert refine(service(env, ds, checker), pid).job.stage == "done"
    assert checker.checked == ["pair-demo"]                 # the render stage reused it


def test_model_that_never_finishes_fails_closed_and_keeps_its_last_draft(env):
    pid = organized(env)
    before = record(env, pid)
    talk = turn(text="我写完了")
    ds = DeepScript().plan().write(turn(section("s2")), *[talk] * 9)
    svc = service(env, ds)
    res = refine(svc, pid)
    assert (res.job.stage, res.job.failure) == ("failed", "no_draft")
    assert "finish" in res.job.failure_detail
    assert record(env, pid) == before                                   # skeleton kept
    out = svc.out("u1", pid)
    assert [s.section_id for s in out.last_draft.sections] == ["s2"]
    assert res.last_draft == out.last_draft


def test_a_new_job_hides_the_old_last_draft(env):
    pid = organized(env)
    ds = DeepScript().plan().write(turn(section("s2")), *[turn(text="?")] * 9)
    svc = service(env, ds)
    refine(svc, pid)
    assert svc.out("u1", pid).last_draft is not None
    svc.llm = DeepScript().plan().write(*simple_round(9)).llm()   # fresh log: nothing replays
    refine(svc, pid)
    out = svc.out("u1", pid)
    assert out.job.stage == "done" and out.last_draft is None


# ---- draft order ----

def test_draft_is_in_final_page_order_not_write_order(env):
    pid = organized(env)
    late = section("n1", 1, heading="新插入", after="s1", role="explain", depth="quick",
                   cue="插一段", body="插在 s1 后", cells=["c1.procedure"])
    ds = DeepScript().plan().write(turn(section("s3", heading="提醒")), turn(section("s2")),
                                   turn(late), turn(finish()))
    res = refine(service(env, ds), pid)
    assert [s.section_id for s in res.draft.sections] == ["n1", "s2", "s3"]
    assert [s.id for s in record(env, pid).page.outline.sections] == ["s1", "n1", "s2", "s3", "s4"]


def test_page_order_places_chains_and_shared_anchors():
    from evowiki.deep.contracts import RefinedSection
    from evowiki.deep.tools import page_order

    def new(sid, after):
        return RefinedSection(section_id=sid, after=after, heading=sid, role="explain",
                              depth="quick", cue=sid, body=sid)
    written = {"b": new("b", "a"), "x": new("x", "s2"), "a": new("a", "s1"),
               "y": new("y", "s2"), "t1": new("t1", None), "t2": new("t2", None)}
    assert page_order(["s1", "s2"], written) == ["t1", "t2", "s1", "a", "b", "s2", "x", "y"]


def test_old_reads_and_write_bodies_are_stubbed_in_the_history(env):
    from evowiki.deep.tools import READ_STUB, WRITE_STUB
    pid = organized(env)
    long = "长正文。" * 200
    reads = [turn(call("read_spec", i, part="exemplar", offset=0)) for i in range(2, 6)]
    ds = (DeepScript().plan()
          .write(turn(call("read_spec", 1, part="exemplar"), section("s2", body=long)), *reads,
                 turn(finish()))
          )
    assert refine(service(env, ds), pid).job.stage == "done"
    last = [r for r in ds.requests if r.purpose == "deep.write"][-1]
    first_reply, first_result = last.messages[3], last.messages[4]   # after plan_page's call / result
    assert first_reply.tool_calls[1].args["body"] == WRITE_STUB
    assert first_reply.tool_calls[1].args["cue"] == "A 对 T，G 对 C"          # short args kept
    assert first_result.text == READ_STUB
    assert last.messages[-1].text != READ_STUB                                # recent read kept
    assert record(env, pid).page.texts["s2"].body == long                     # the draft is whole


def cut() -> Exception:
    from evowiki.llm.providers import ProviderEmptyOutputError
    return ProviderEmptyOutputError("truncated at max_tokens", status="truncated",
                                    input_tokens=1000, output_tokens=32768)


def test_a_truncated_write_reply_is_retried_smaller_once(env):
    pid = organized(env)
    ds = DeepScript().plan().write(cut(), *simple_round())
    res = refine(service(env, ds), pid)
    assert res.job.stage == "done"
    _plan, first, retry, after, *_ = [r for r in ds.requests if r.purpose == "deep.write"]
    # deep@v16: the note is a user message after the history, the system prompt never changes
    # (prompt caching), and the note stays in the history once added (append-only)
    assert first.system == retry.system == after.system
    assert TRUNCATED_NOTE not in first.system
    # The language footer is request-only; retry notes belong to durable history.
    assert first.messages[-1] == retry.messages[-1] == after.messages[-1]
    first_history, retry_history = first.messages[:-1], retry.messages[:-1]
    assert retry_history[:-1] == first_history
    assert (retry_history[-1].role, retry_history[-1].text) == ("user", TRUNCATED_NOTE)
    assert after.messages[:len(retry_history)] == retry_history
    assert res.job.cost_usd > 32768 * 10 / 1e6                # the cut reply was still billed


def test_a_second_truncated_reply_fails_closed(env):
    pid = organized(env)
    ds = DeepScript().plan().write(cut(), cut())
    res = refine(service(env, ds), pid)
    assert (res.job.failure, res.job.failure_detail[:11]) == ("llm_unavailable", "stage=write")
    assert record(env, pid).written.kind == "page"   # the organize write only


# ---- fail closed on the LLM ----

def test_no_deep_model_is_llm_unavailable(env):
    pid = organized(env)
    res = refine(service(env, DeepScript(), llm=None), pid)
    assert (res.job.stage, res.job.failure) == ("failed", "llm_unavailable")


def test_unreachable_provider_is_llm_unavailable(env):
    pid = organized(env)
    ds = DeepScript()
    llm = ds.llm()
    svc = service(env, ds, llm=llm.__class__(**{**llm.__dict__, "providers": {}}))
    res = refine(svc, pid)
    assert (res.job.failure, res.job.failure_detail[:11]) == ("llm_unavailable", "stage=write")


# ---- page_changed ----

def test_page_changed_during_refine_writes_nothing(env):
    pid = organized(env)
    repo = env[0]
    ds = DeepScript().plan().write(*simple_round(with_interactive=True))

    def someone_rewrites_s1():
        ds.before_write = None
        rec = record(env, pid)
        texts = {**rec.page.texts, "s1": rec.page.texts["s1"].model_copy(update={"body": "别人改的"})}
        new = rec.model_copy(update={"page": rec.page.model_copy(update={"texts": texts})})
        commit_wiki(env, PageSaved(record=new, action="rewritten"))

    ds.before_write = someone_rewrites_s1
    svc = service(env, ds)
    res = refine(svc, pid)
    assert (res.job.stage, res.job.failure) == ("failed", "page_changed")
    assert res.job.failure_detail.startswith("stage=commit")
    rec = record(env, pid)
    assert rec.page.texts["s1"].body == "别人改的" and rec.written.kind == "page"
    assert not svc.assets.rows
    assert repo.load("u1").state.pages[pid].changed != {"s2": "rewritten"}


def test_a_fold_during_refine_is_not_a_page_change(env):
    pid = organized(env)
    ds = DeepScript().plan().write(*simple_round())

    def user_folds_s3():
        ds.before_write = None
        set_fold(env[0], "u1", pid, "s3", True, now=NOW, new_id=env[2])

    ds.before_write = user_folds_s3
    res = refine(service(env, ds), pid)
    assert res.job.stage == "done"
    page = record(env, pid).page
    assert page.state("s3").folded and page.texts["s2"].body.startswith("s2 精修正文")


# ---- budget ----

def test_page_limit_stops_the_job_at_its_stage(env):
    pid = organized(env)
    commit_wiki(env, RefineSettingsSet(settings=RefineSettings(per_page_limit_usd=0.05)))
    big = {**turn(call("read_spec", part="items")), "input_tokens": 20_000}   # $0.045 per turn
    ds = DeepScript().plan().write(big, {**big, "tool_calls": [call("read_spec", 2, part="items")]},
                                   *simple_round())
    svc = service(env, ds)
    res = refine(svc, pid)
    assert (res.job.stage, res.job.failure) == ("failed", "budget_exceeded")
    assert res.job.failure_detail.startswith("stage=write") and "这一页的花费到了上限" in \
        res.job.failure_detail
    # D26: each request is bounded by what is left: the write turn whose input alone (estimated,
    # at the dearest input rate) does not fit is refused before it is sent; nothing overshoots
    # (deep@v32: the first message is small enough that the plan turn fits; the big turn does not)
    assert res.job.cost_usd <= 0.05 and ds.count("deep.write") <= 1
    assert record(env, pid).written.kind == "page"   # the organize write only


def test_users_spending_limit_is_budget_exceeded(env):
    pid = organized(env)

    class Spent:
        def check(self, account):
            assert account.group == "page"
            raise BudgetExceededError(account, "day", 1.0)

        def record(self, *a, **k):
            raise AssertionError("nothing may be billed")

    res = refine(service(env, DeepScript(), meter=Spent()), pid)
    assert (res.job.failure, res.job.failure_detail[:11]) == ("budget_exceeded", "stage=write")


def test_spend_is_recorded_to_the_page_group(env):
    pid = organized(env)
    seen = []

    class Meter:
        def check(self, account):
            pass

        def record(self, account, purpose, usage, *, replayed):
            seen.append((account.group, purpose))

    ds = DeepScript().plan().write(*simple_round())
    refine(service(env, ds, meter=Meter()), pid)
    assert {g for g, _ in seen} == {"page"}
    assert [p for _, p in seen] == ["deep.write"] * 3        # plan_page is a write turn


# ---- dry_run ----

def test_dry_run_returns_the_draft_and_writes_nothing(env):
    pid = organized(env)
    before = record(env, pid)
    ds = DeepScript().plan().write(*simple_round(with_interactive=True))
    svc = service(env, ds)
    from evowiki.deep.contracts import RefineJob
    job = RefineJob(job_id="dry1", page_id=pid, trigger="harness_replay", stage="queued",
                    started_at=NOW)
    res = svc.run("u1", job, RefineScope(page_id=pid, dry_run=True))
    assert res.job.stage == "done" and res.draft is not None
    assert [s.section_id for s in res.draft.sections] == ["s2"]
    assert res.draft.interactives[0].interactive_id == "pair-demo"
    assert record(env, pid) == before
    assert svc.jobs.rows == [] and svc.assets.rows == {}


# ---- jobs ----

def test_one_job_per_page_at_a_time(env):
    pid = organized(env)
    ds = DeepScript().plan().write(*simple_round())
    svc = service(env, ds)
    job = svc.start("u1", pid, "user")
    with pytest.raises(JobRunning):
        svc.start("u1", pid, "user")
    svc.run("u1", job)
    assert svc.start("u1", pid, "user").stage == "queued"


def test_start_refuses_unknown_and_unorganized_pages(env):
    from tests.wikiapp.test_organize import page1
    svc = service(env, DeepScript())
    with pytest.raises(LookupError):
        svc.start("u1", "nope", "user")
    with pytest.raises(ValueError):
        svc.start("u1", page1(env), "user")


def test_an_unknown_spec_version_fails_without_writing(env):
    pid = organized(env)
    res = refine(service(env, DeepScript()), pid, spec_version=7, dry_run=True)
    assert res.job.failure == "internal" and "7" in res.job.failure_detail


def test_job_cost_is_every_priced_deep_call(env):
    """Every deep.write turn (plan_page's too) is metered once, and the job's cost is their sum."""
    pid = organized(env)
    seen = []

    class Meter:
        def check(self, account):
            pass

        def record(self, account, purpose, usage, *, replayed):
            seen.append((purpose, usage.cost_usd))

    ds = DeepScript().plan().write(*simple_round())
    res = refine(service(env, ds, llm=ds.llm(Meter())), pid)
    assert [p for p, _ in seen] == ["deep.write"] * 3
    assert all(c is not None and c > 0 for _, c in seen)
    assert res.job.cost_usd == pytest.approx(sum(c for _, c in seen))
    assert res.job.cost_usd == pytest.approx(3 * 0.007)


# ---- interactives come only from the library (B5) ----

def tool_names(ds) -> set[str]:
    first = next(r for r in ds.requests if r.purpose == "deep.write")
    return {t.name for t in first.tools}


def test_writer_sees_the_catalog_and_the_place_tool(env):
    pid = organized(env)
    ds = DeepScript().plan().write(*simple_round())
    refine(service(env, ds, lib=library("pair-demo", "other-demo")), pid)
    first = next(r for r in ds.requests if r.purpose == "deep.write")
    text = first.messages[0].text
    assert "pair-demo：拼一条链 — 点按钮看计数加一（可调：加一：点一下计数加一）" in text
    assert "other-demo：" in text and "交互组件库是空的" not in text
    assert "place_component" in tool_names(ds) and "write_interactive" not in tool_names(ds)


def test_empty_library_means_blocks_only_and_no_free_html(env):
    pid = organized(env)
    for lib in (library_empty(), None):
        ds = DeepScript().plan().write(*simple_round())
        res = refine(service(env, ds, lib=lib), pid)
        assert res.job.stage == "done" and res.job.interactives == ()
        first = next(r for r in ds.requests if r.purpose == "deep.write")
        assert "交互组件库是空的：需要交互时直接写 EvoUI 块" in first.messages[0].text
        names = tool_names(ds)      # no library component, never free HTML; a block is still possible
        assert names.isdisjoint({"place_component", "write_interactive"})
        assert {"write_block", "render_check"} <= names


def test_placing_an_unknown_component_is_refused_and_nothing_is_drafted(env):
    pid = organized(env)
    body = "s2 正文\n{{interactive:free-html}}"
    ds = DeepScript().plan().write(
        turn(section("s2", body=body), interactive("free-html")),
        turn(finish()),
        turn(call("write_interactive", 2, interactive_id="x", section_id="s2", title="t",
                  learning_question="q", html="<p>x</p>", height=480, probes=[])),
        turn(section("s2", 3), finish(3)))
    res = refine(service(env, ds), pid)
    msgs = [m.text for r in ds.requests if r.purpose == "deep.write" for m in r.messages
            if m.role == "tool"]
    assert any("组件库里没有 free-html" in m and "pair-demo" in m for m in msgs)
    assert any("正文引用了不存在的交互、块或图片：free-html" in m for m in msgs)  # finish refuses it too
    assert any("没有这个工具：write_interactive" in m for m in msgs)       # free HTML has no tool
    assert res.job.interactives == ()


def test_placed_component_keeps_the_library_html_and_goes_through_the_render_check(env):
    pid = organized(env)
    checker = FakeChecker()
    ds = DeepScript().plan().write(*simple_round(with_interactive=True))
    svc = service(env, ds, checker)
    res = refine(svc, pid)
    assert res.job.stage == "done" and checker.checked == ["pair-demo"]
    [asset] = svc.assets.rows.values()
    assert asset.html == component().html and asset.meta.title == "拼一条链"
    assert asset.meta.height == 480


def test_library_failure_fails_the_job_closed(env):
    class Broken:
        def list(self):
            raise RuntimeError("db down")
    pid = organized(env)
    res = refine(service(env, DeepScript(), lib=Broken()), pid)
    assert (res.job.stage, res.job.failure) == ("failed", "internal")


# ---- EvoUI blocks ----

def _block_round(i: int = 1, block=None) -> list[dict]:
    from tests.deep.test_evoui_parity import BASE
    body = "s2 精修正文。\n\n{{block:proj}}\n"
    return [turn(section("s2", i, body=body),
                 call("write_block", i, block_id="proj", section_id="s2",
                      why="他看不出角度和投影长度的关系", block=block or BASE)),
            turn(finish(i))]


def test_a_written_block_is_checked_stored_as_evoui_and_carried_as_a_fence(env):
    import json

    from tests.deep.test_evoui_parity import BASE
    pid = organized(env)
    checker = FakeChecker()
    ds = DeepScript().plan().write(*_block_round())
    svc = service(env, ds, checker)
    res = refine(svc, pid)
    assert res.job.stage == "done" and checker.checked == ["proj"]
    [asset_id] = res.job.interactives
    a = svc.assets.get("u1", asset_id)
    assert a.meta.kind == "evoui" and a.meta.interactive_id == "proj"
    assert json.loads(a.html)["title"] == BASE["title"]
    body = record(env, pid).page.texts["s2"].body
    assert "{{block:" not in body and f"```evoui\nasset: {asset_id}\n```" in body and "embedded-html" not in body
    first = next(r for r in ds.requests if r.purpose == "deep.write").messages[0].text
    assert "EvoUI 索引" in first and "read_ref" in first


def test_an_invalid_block_is_refused_with_the_validator_errors(env):
    from tests.deep.test_evoui_parity import BASE
    pid = organized(env)
    bad = {**BASE, "derived": {"x": "nope + 1"}}
    ds = DeepScript().plan().write(
        turn(call("write_block", 1, block_id="proj", section_id="s2", why="w", block=bad)),
        turn(section("s2"), finish()))
    res = refine(service(env, ds), pid)
    msgs = [m.text for r in ds.requests if r.purpose == "deep.write" for m in r.messages
            if m.role == "tool"]
    assert any("unknown name nope" in m for m in msgs)
    assert res.job.stage == "done" and res.job.interactives == ()


def test_a_broken_block_is_dropped_and_the_page_still_commits(env):
    pid = organized(env)
    checker = FakeChecker(fail_block=lambda bid, b: "控件拖了没反应")
    ds = DeepScript().plan().write(*_block_round(1))
    svc = service(env, ds, checker)
    res = refine(svc, pid)
    assert (res.job.stage, res.job.round) == ("done", 1) and ds.pending() == {}
    written = next(m for r in ds.requests if r.purpose == "deep.write" for m in r.messages
                   if m.role == "tool" and "已写入块" in m.text)
    assert written.is_error and "控件拖了没反应" in written.text      # said when it was placed
    [d] = res.job.dropped
    assert (d.interactive_id, d.section_id) == ("proj", "s2") and "控件拖了没反应" in d.reason
    assert res.job.interactives == () and not svc.assets.rows
    assert "block" not in record(env, pid).page.texts["s2"].body


def test_a_dropped_block_stays_out_of_the_self_check_round_and_its_text_wins(env):
    """Round 1: a broken block (dropped) and a self-check problem; round 2 fixes the text. The
    result is round 2's text without the block (the extra round does not bring it back, so it is
    not thrown away for the accepted draft)."""
    pid = organized(env)
    checker = FakeChecker(fail_block=lambda bid, b: "控件拖了没反应")
    first = _block_round(1)
    ds = DeepScript().plan().write(
        first[0], turn(finish(1, checks=self_check(ok=False))),
        turn(section("s2", 2, body="s2 第二轮改好的正文。"), finish(2)))
    svc = service(env, ds, checker)
    res = refine(svc, pid)
    assert (res.job.stage, res.job.round) == ("done", 2) and ds.pending() == {}
    assert [d.interactive_id for d in res.job.dropped] == []    # round 2 has no block at all
    assert res.job.interactives == () and not svc.assets.rows
    assert record(env, pid).page.texts["s2"].body.startswith("s2 第二轮改好的正文")
    assert checker.checked == ["proj", "proj"]                  # placed, then the round-1 gate


def test_a_block_quiz_may_only_name_this_pages_cells(env):
    import copy

    from tests.deep.test_evoui_parity import BASE
    pid = organized(env)
    bad = copy.deepcopy(BASE)
    bad["body"]["children"][6]["questions"][0]["cell"] = "zz.term"
    ds = DeepScript().plan().write(
        turn(call("write_block", 1, block_id="proj", section_id="s2", why="w", block=bad)),
        turn(section("s2"), finish()))
    refine(service(env, ds), pid)
    msgs = [m.text for r in ds.requests if r.purpose == "deep.write" for m in r.messages
            if m.role == "tool"]
    assert any("quiz、闪卡、填空和拼句的 cell 只能用这一页的格子" in m and "zz.term" in m for m in msgs)


# ---- as a workflow body (B1b) ----

def _spy_steps(monkeypatch, *, replay: list | None = None):
    """Records every step's name and output; with `replay`, the first len(replay) steps return
    those recorded outputs with ran=False (a recovered workflow) instead of running."""
    from evowiki.agent import durable
    from evowiki.wikiapp import durable_repo
    real = durable.step_ran
    names: list[str] = []
    outs: list = []

    def spy(name, fn):
        i = len(names)
        names.append(name)
        if replay is not None and i < len(replay):
            return replay[i], False
        value, ran = real(name, fn)
        outs.append(value)
        return value, ran
    monkeypatch.setattr(durable, "step_ran", spy)
    monkeypatch.setattr(durable_repo, "step_ran", spy)
    return names, outs


def test_a_durable_copy_run_equals_run_and_its_steps_are_named(env, monkeypatch):
    from evowiki.agent import durable
    pid = organized(env)
    ds = DeepScript().plan().write(*simple_round(with_interactive=True))
    svc = service(env, ds)
    names, _ = _spy_steps(monkeypatch)
    job = svc.start("u1", pid, "user")
    with durable.virtual_workflow("wf-refine"):
        res = svc.durable_copy().run("u1", job)

    assert (res.job.stage, res.job.failure) == ("done", None)
    assert names.count("repo.commit") == 1 and names.count("refine.save") >= 5
    assert "render.check" in names and "library.list" in names and "new_id" in names
    assert any(n.startswith("llm.converse:deep.write") for n in names)    # every model call a step
    assert [j.stage for _, j in svc.jobs.rows][-4:] == ["write", "render", "commit", "done"]

    # the same script through plain `run` gives the same result
    env2 = (type(env[0])(["u1"]), type(env[1])(), type(env[2])())
    pid2 = organized(env2)
    svc2 = service(env2, DeepScript().plan().write(*simple_round(with_interactive=True)))
    plain = svc2.run("u1", svc2.start("u1", pid2, "user"))
    for f in ("stage", "failure", "round", "changed_sections", "cost_usd", "model", "spec_version"):
        assert getattr(plain.job, f) == getattr(res.job, f)
    assert plain.draft == res.draft and len(plain.assets) == len(res.assets)


def test_a_replayed_durable_run_calls_neither_the_provider_nor_the_checker(env, monkeypatch):
    from evowiki.agent import durable
    pid = organized(env)
    ds = DeepScript().plan().write(*simple_round(with_interactive=True))
    checker = FakeChecker()
    svc = service(env, ds, checker)
    names, outs = _spy_steps(monkeypatch)
    job = svc.start("u1", pid, "user")
    with durable.virtual_workflow("wf-first"):
        first = svc.durable_copy().run("u1", job)
    assert first.job.stage == "done" and len(outs) == len(names)
    served, checked, saved, wiki_n = (list(ds.served), list(checker.checked), len(svc.jobs.rows),
                                      len(env[0].wiki_log("u1")))
    assert served and checked

    # recovery: the same body re-executes with every step recorded; nothing real runs again
    _spy_steps(monkeypatch, replay=outs)
    with durable.virtual_workflow("wf-first"):
        again = svc.durable_copy().run("u1", job)
    assert again.job == first.job and again.draft == first.draft
    assert ds.served == served and checker.checked == checked    # no provider / browser call
    assert len(svc.jobs.rows) == saved                           # no second job save
    assert len(env[0].wiki_log("u1")) == wiki_n                  # no second commit


def test_a_page_still_being_written_is_not_refined_or_taken_as_an_exemplar(env):
    """A page whose first write is running or stopped part-way (WritingMark) refuses a refine
    (POST refine → 409) and cannot be a teaching exemplar."""
    from evowiki.wikiapp.events import WritingMark
    from evowiki.wikiapp.refine_service import STILL_WRITING
    from evowiki.wikiapp.teach import page_exemplar
    from evowiki.wikiapp.teachspec import SpecInvalid
    pid = organized(env)
    rec = record(env, pid).model_copy(update={"writing": WritingMark(job_id="page:u1:x:1")})
    commit_wiki(env, PageSaved(record=rec, action="section_written"))
    svc = service(env, DeepScript())
    with pytest.raises(ValueError, match=STILL_WRITING):
        svc.start("u1", pid, "user")
    with pytest.raises(SpecInvalid):
        page_exemplar(env[0].load("u1").state, pid, "e1")
    from evowiki.wikiapp.repo import WikiConflict
    from evowiki.wikiapp.source_revise import revisable
    with pytest.raises(WikiConflict, match=STILL_WRITING):          # 按资料改 refuses it too
        revisable(env[0], None, None, "u1", pid, NOW)
