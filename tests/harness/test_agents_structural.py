"""The deep page writer reads the structural pack keys (phase 3a Task 6): H3 tool toggles, H8 skills,
H9 before-write delegate notes, H11 after-write self-check must-fix lines and H10 step tiers /
thinking, through a per-tier gateway roster. An empty pack sends exactly today's requests.
Scripted providers only: no paid API."""

from dataclasses import replace

from evowiki.deep.agent import (
    NOTES_HEAD,
    RESUME_NOTE,
    UNROUTABLE_TEXT,
    RefineFailed,
    refine,
)
from evowiki.deep.contracts import RefineScope
from evowiki.deep.library import MemoryLibrary
from evowiki.deep.store import MemoryAssetStore, MemoryJobStore
from evowiki.harness.pack import EMPTY, HarnessPack
from evowiki.llm.models import THINKING_PARAM, GatewayConfig, ModelRef
from evowiki.llm.providers import ProviderTransientError
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import Account
from evowiki.wikiapp.refine_service import DeepService
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import MemoryRepo
from tests.deep.fakes import (
    DeepScript,
    FakeChecker,
    call,
    component,
    finish,
    library,
    section,
    self_check,
    turn,
)
from tests.deep.test_components import CFG, configurable
from tests.deep.test_writer import outline_input, write_all
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW
from tests.wikiapp.test_goal import Ids as WikiIds
from tests.wikiapp.test_page import organized

NO_SOURCE = object()          # the gateways are built without a harness source
DEEP_MODEL = "claude-sonnet-5-5"
FAST = GatewayConfig(generator=ModelRef(provider="deepseek", model="deepseek-v4-flash"),
                     verifier=ModelRef(provider="deepseek", model="deepseek-v4-flash"),
                     allow_single_provider=True)
GOOD = GatewayConfig(generator=ModelRef(provider="deepseek", model="deepseek-v4-pro"),
                     verifier=ModelRef(provider="deepseek", model="deepseek-v4-pro"),
                     allow_single_provider=True)
TIERS = {"fast": FAST, "good": GOOD}
EXEMPLAR = "样板页 0《"
EMPTY_LIBRARY = "交互组件库是空的"


class TierScript(DeepScript):
    """A DeepScript that also records the model each request was routed to."""

    def __init__(self) -> None:
        super().__init__()
        self.routed: list[tuple[str, str]] = []           # (model, purpose)

    def provider(self, name: str) -> ScriptedChatProvider:
        inner = super().provider(name)

        def chat(model, r):
            self.routed.append((model, r.purpose))
            return inner._chat(model, r)

        def complete(model, r):
            self.routed.append((model, r.purpose))
            return inner._complete(model, r)
        return ScriptedChatProvider(name, chat, complete)


def dumps(requests):
    return [r.model_dump() for r in requests]


def run(pack, script, *, tiers=TIERS, roster=True, lib="default", fallback=None, trace=None):
    """One refine of a fresh page under `pack`; (script, outcome, failure, writer notes)."""
    env = (MemoryRepo(["u1"]), Script(), WikiIds())
    inp = outline_input(env, organized(env))
    ds = script(TierScript().plan())
    llm = replace(ds.llm(), tiers=tiers, fallback=fallback)
    if pack is not NO_SOURCE:
        llm = replace(llm, harness=lambda uid: pack)
    rost, _ = llm.roster(None)
    notes = []
    try:
        out = refine(inp, rost["deep"][0], Account(user_id="u1", group="page"), FakeChecker(),
                     library=library() if lib == "default" else lib, fallbacks=rost["deep"][1:],
                     on_writer=lambda m, fb, why: notes.append((m, fb, why)),
                     roster=rost if roster else None,
                     **({"trace": trace} if trace is not None else {}))
        failed = None
    except RefineFailed as exc:
        out, failed = None, exc
    return ds, out, failed, notes


def two_rounds(ds):
    """Round 0's self-check names a problem, so round 1 runs with it as must-fix."""
    return ds.write(*write_all()[:-1], turn(finish(1, self_check(ok=False))), turn(finish(2)))


def writes(ds):
    return [r for r in ds.requests if r.purpose == "deep.write"]


def first_messages(ds):
    """Each round opens with its brief and a request-only language footer."""
    from evowiki.deep.language import output_rule

    histories = []
    for request in writes(ds):
        assert request.messages[-1].role == "user"
        assert request.messages[-1].text == output_rule("zh")
        histories.append(request.messages[:-1])
    return [history[0].text for history in histories if len(history) == 1]


def tool_names(ds):
    return {t.name for t in writes(ds)[0].tools}


# ---- (a) empty pack: byte-identical ----

def test_empty_pack_with_a_roster_sends_identical_requests():
    plain, o1, f1, n1 = run(NO_SOURCE, two_rounds, tiers=None, roster=False)
    empty, o2, f2, n2 = run(EMPTY, two_rounds)
    assert f1 is None and f2 is None and o1.rounds == o2.rounds == 2
    assert dumps(plain.requests) == dumps(empty.requests) and len(plain.requests) == 6
    assert plain.routed == empty.routed and n1 == n2
    assert len(o1.calls) == len(o2.calls) == 6


def test_empty_pack_through_the_service_sends_identical_requests():
    def dry(ds, tiers):
        repo, ids = MemoryRepo(["u1"]), WikiIds()
        pid = organized((repo, Script(), ids))
        llm = replace(ds.llm(), harness=lambda uid: EMPTY, tiers=tiers)
        svc = DeepService(repo=repo, jobs=MemoryJobStore(), assets=MemoryAssetStore(),
                          specs=WikiSpecSource(repo), checker=FakeChecker(), llm=llm,
                          clock=lambda: NOW, new_id=ids, library=library())
        job = svc.start("u1", pid, "user")
        return svc.run("u1", job, RefineScope(page_id=pid, dry_run=True))

    def script():
        return DeepScript().plan().write(turn(section("s2")), turn(finish()))
    a, b = script(), script()
    ra, rb = dry(a, None), dry(b, TIERS)
    assert ra.job.stage == rb.job.stage == "done"
    assert dumps(a.requests) == dumps(b.requests) and len(a.requests) == 3


# ---- (b) / (c) H3 tool toggles ----

def test_read_spec_off_leaves_it_out_of_the_tool_defs():
    ds, _, failed, _ = run(HarnessPack(1, {"H3.deep.read_spec": False}),
                           lambda s: s.write(*write_all()))
    assert failed is None
    assert "read_spec" not in tool_names(ds) and {"read_k", "read_page"} <= tool_names(ds)


def test_place_component_off_is_the_empty_library():
    on, _, _, _ = run(EMPTY, lambda s: s.write(*write_all()))
    assert "place_component" in tool_names(on) and EMPTY_LIBRARY not in first_messages(on)[0]
    ds, _, failed, _ = run(HarnessPack(1, {"H3.deep.place_component": False}),
                           lambda s: s.write(*write_all()))
    assert failed is None
    assert "place_component" not in tool_names(ds)
    assert EMPTY_LIBRARY in first_messages(ds)[0]


# ---- (d) / (e) H10 step tiers and thinking ----

def test_write_tier_good_routes_every_write_turn_plan_page_included():
    ds, _, failed, notes = run(HarnessPack(1, {"H10.deep.write.tier": "good"}),
                               lambda s: s.write(*write_all()))
    assert failed is None
    assert {m for m, p in ds.routed} == {"deepseek-v4-pro"}       # no separate plan call
    assert notes == [("deepseek:deepseek-v4-pro", False, "")]


def test_write_thinking_off_is_on_write_requests_only():
    ds, _, failed, _ = run(HarnessPack(1, {"H10.deep.write.thinking": False}),
                           lambda s: s.write(*write_all()))
    assert failed is None
    assert all(r.params[THINKING_PARAM] is False for r in writes(ds))
    assert all(r.params["round"] == 0 for r in writes(ds))


# ---- (f) H9 before-write delegate ----

DELEGATE = {"at": "deep.before_write", "kind": "delegate", "tier": "fast", "tools": ["read_k"],
            "instruction": "先读这一页的 K，列出他没把握的格子。", "output_to": "context.notes",
            "max_tool_calls": 2}


def test_before_write_delegate_runs_on_its_tier_and_its_notes_reach_every_round():
    ds, out, failed, _ = run(
        HarnessPack(1, {"H9.deep.before_write": [DELEGATE]}),
        lambda s: two_rounds(s).push("hook.deep.before_write",
                                     turn(call("read_k", concepts=["c1"])),
                                     turn(text="笔记：先讲配对，再讲例外。")))
    assert failed is None
    purposes = [r.purpose for r in ds.requests]
    hooks = [i for i, p in enumerate(purposes) if p == "hook.deep.before_write"]
    assert len(hooks) == 2 and hooks[-1] < purposes.index("deep.write")
    assert {m for m, p in ds.routed if p.startswith("hook.")} == {"deepseek-v4-flash"}
    hook_req = ds.requests[hooks[0]]
    assert [t.name for t in hook_req.tools] == ["read_k"]
    rounds = first_messages(ds)
    assert len(rounds) == 2
    assert all(NOTES_HEAD + "笔记：先讲配对，再讲例外。" in m for m in rounds)
    assert all(m.index(NOTES_HEAD) < m.index("页面大纲") for m in rounds)    # after the plan part
    assert sum(c.purpose == "hook.deep.before_write" for c in out.calls) == 2


def test_before_write_hook_naming_a_tool_turned_off_fails_before_any_call():
    pack = HarnessPack(1, {"H9.deep.before_write": [DELEGATE], "H3.deep.read_k": False})
    ds, _, failed, _ = run(pack, lambda s: s)
    assert failed is not None and failed.failure == "internal"
    assert "HarnessInvalid" in failed.detail and "read_k" in failed.detail
    assert ds.requests == []                                 # not even the first write turn


# ---- (g) H11 after-write self-check ----

CHECK = {"at": "deep.after_write", "kind": "self_check", "tier": "good", "tools": [],
         "instruction": "对照规划检查有没有漏讲的格子。", "output_to": "context.must_fix",
         "max_tool_calls": 0}


def test_after_write_self_check_problem_adds_a_must_fix_round():
    ds, out, failed, _ = run(
        HarnessPack(1, {"H11.deep.after_write": [CHECK]}),
        lambda s: s.write(*write_all(), turn(finish(2))).push(
            "hook.deep.after_write", turn(call("report", ok=False, notes=["x"]))))
    assert failed is None and out.rounds == 2
    assert ds.count("hook.deep.after_write") == 1          # not again after the extra round
    assert {m for m, p in ds.routed if p == "hook.deep.after_write"} == {"deepseek-v4-pro"}
    hook = next(r for r in ds.requests if r.purpose == "hook.deep.after_write")
    assert "s2 精修正文" in hook.messages[0].text                  # the draft is the brief
    fix_round = first_messages(ds)[1]
    assert "上一轮必须改的问题：" in fix_round and "- 自查：x" in fix_round


def test_after_write_check_naming_a_tool_turned_off_fails_before_any_call():
    check = {**CHECK, "tools": ["read_spec"], "max_tool_calls": 1}
    pack = HarnessPack(1, {"H11.deep.after_write": [check], "H3.deep.read_spec": False})
    ds, _, failed, _ = run(pack, lambda s: s)
    assert failed is not None and failed.failure == "internal"
    assert "HarnessInvalid" in failed.detail and "read_spec" in failed.detail
    assert ds.requests == []


def test_after_write_self_check_ok_adds_no_round():
    ds, out, failed, _ = run(
        HarnessPack(1, {"H11.deep.after_write": [CHECK]}),
        lambda s: s.write(*write_all()).push(
            "hook.deep.after_write", turn(call("report", ok=True, notes=[]))))
    assert failed is None and out.rounds == 1 and ds.count("deep.write") == 5
    assert ds.count("hook.deep.after_write") == 1


# ---- (g2) a failing H11 self-check keeps the accepted draft (ruled 2026-10-21) ----

def problem_draft(s):
    """Round 0 passes the render check and its own self-check names a problem: without the
    hook failure there would be one extra round."""
    return two_rounds(s)


def test_after_write_check_unavailable_keeps_the_accepted_draft(caplog):
    from tests.agent.test_loop_trace import Recorder
    sink = Recorder()
    with caplog.at_level("WARNING", logger="evowiki.deep.agent"):
        ds, out, failed, _ = run(
            HarnessPack(1, {"H11.deep.after_write": [CHECK]}),
            lambda s: problem_draft(s).push("hook.deep.after_write",
                                            ProviderTransientError("503 down")),
            trace=sink)
    assert failed is None and out.rounds == 1
    assert ds.count("deep.write") == 5 and ds.count("hook.deep.after_write") == 1
    assert len(first_messages(ds)) == 1                       # no extra write round
    warned = [r.getMessage() for r in caplog.records if "after_write self-check failed" in r.message]
    assert len(warned) == 1 and "503 down" in warned[0]
    hook_runs = [c[1] for c in sink.calls if c[0] == "start" and ":hook:after_write:" in c[1]]
    assert len(hook_runs) == 1
    finishes = [c for c in sink.calls if c[0] == "finish" and c[1] == hook_runs[0]]
    assert len(finishes) == 1 and finishes[0][2] is None and finishes[0][3]   # finished failed


def test_after_write_check_raising_harness_invalid_keeps_the_accepted_draft(caplog, monkeypatch):
    from evowiki.harness import apply
    from evowiki.harness.pack import HarnessInvalid

    def broken(step, **kw):
        raise HarnessInvalid("bad hook step")
    monkeypatch.setattr(apply, "run_hook", broken)
    with caplog.at_level("WARNING", logger="evowiki.deep.agent"):
        ds, out, failed, _ = run(HarnessPack(1, {"H11.deep.after_write": [CHECK]}), problem_draft)
    assert failed is None and out.rounds == 1 and ds.count("deep.write") == 5
    assert any("HarnessInvalid" in r.getMessage() and "bad hook step" in r.getMessage()
               for r in caplog.records)


def test_after_write_check_over_budget_still_fails_the_refine(monkeypatch):
    from evowiki.harness import apply
    from evowiki.llm.usage import BudgetExceededError

    def over(step, **kw):
        raise BudgetExceededError(Account(user_id="u1", group="page"), "day", 1.0)
    monkeypatch.setattr(apply, "run_hook", over)
    ds, out, failed, _ = run(HarnessPack(1, {"H11.deep.after_write": [CHECK]}), problem_draft)
    assert out is None and failed.failure == "budget_exceeded"
    assert ds.count("deep.write") == 5


def test_before_write_delegate_unavailable_still_fails_the_refine():
    ds, out, failed, _ = run(
        HarnessPack(1, {"H9.deep.before_write": [DELEGATE]}),
        lambda s: s.push("hook.deep.before_write", ProviderTransientError("503 down")))
    assert out is None and failed.failure == "llm_unavailable"
    assert ds.count("deep.write") == 0


# ---- (h) D2: a tier the roster lacks ----

def test_hook_tier_missing_from_the_roster_fails_before_any_call():
    ds, _, failed, _ = run(HarnessPack(1, {"H9.deep.before_write": [DELEGATE]}),
                           lambda s: s, tiers=None)
    assert isinstance(failed, RefineFailed) and failed.failure == "llm_unavailable"
    assert failed.detail == UNROUTABLE_TEXT
    assert ds.requests == []


def test_step_tier_missing_from_the_roster_is_the_same_plain_failure():
    ds, _, failed, _ = run(HarnessPack(1, {"H10.deep.write.tier": "good"}), lambda s: s,
                           tiers=None)
    assert failed.failure == "llm_unavailable" and failed.detail == UNROUTABLE_TEXT
    assert "deepseek" not in failed.detail and "good" not in failed.detail
    assert ds.count("deep.write") == 0


# ---- (i) / (j) H8 skills ----

def test_component_note_and_preset_show_in_the_catalog():
    lib = MemoryLibrary([component("pair-demo"), configurable()])
    preset = {"name": "复制", "use": "讲 DNA 复制", "config": CFG}
    pack = HarnessPack(1, {"H8.component.pair-demo.note": "讲配对时放它",
                           "H8.component.steps-demo.presets": [preset]})
    ds, _, failed, _ = run(pack, lambda s: s.write(*write_all()), lib=lib)
    assert failed is None
    first = first_messages(ds)[0]
    assert "讲配对时放它" in first and "点按钮看计数加一" not in first
    assert "预设「复制」（讲 DNA 复制）" in first


def test_preset_for_an_unknown_component_fails_the_refine():
    preset = {"name": "复制", "use": "讲 DNA 复制", "config": CFG}
    ds, _, failed, _ = run(HarnessPack(1, {"H8.component.ghost.presets": [preset]}),
                           lambda s: s)
    assert isinstance(failed, RefineFailed) and failed.failure == "internal"
    assert "ghost" in failed.detail and ds.requests == []


def test_write_spec_items_only_leaves_out_the_exemplar():
    plain, _, _, _ = run(EMPTY, lambda s: s.write(*write_all()))
    assert EXEMPLAR in first_messages(plain)[0]
    ds, _, failed, _ = run(HarnessPack(1, {"H8.deep.spec.write": ["items"]}),
                           lambda s: s.write(*write_all()))
    assert failed is None and EXEMPLAR not in first_messages(ds)[0]


# ---- (k) writer fallback with a roster ----

DEEPSEEK_PRO = GatewayConfig(generator=ModelRef(provider="deepseek", model="deepseek-v4-pro"),
                             verifier=ModelRef(provider="deepseek", model="deepseek-v4-pro"),
                             allow_single_provider=True)


def test_primary_failing_on_plan_page_falls_back_exactly_as_before():
    """The first write turn (plan_page) switches to the fallback and the rest stay on it; round 0
    is no resume (nothing written). Same requests as without a roster."""
    def script(s):
        s.queues["deep.write"].appendleft(ProviderTransientError("429 rate limited"))
        return s.write(*write_all())
    plain, _, f1, n1 = run(NO_SOURCE, script, tiers=None, roster=False, fallback=DEEPSEEK_PRO)
    ds, _, f2, n2 = run(EMPTY, script, fallback=DEEPSEEK_PRO)
    assert f1 is None and f2 is None and n1 == n2
    assert n2[0] == ("anthropic:claude-sonnet-5-5", False, "")
    assert n2[1][:2] == ("deepseek:deepseek-v4-pro", True) and "429" in n2[1][2]
    assert [p for p, _ in ds.served] == ["anthropic"] + ["deepseek"] * 5
    assert all(RESUME_NOTE not in m for m in first_messages(ds))
    assert dumps(plain.requests) == dumps(ds.requests)


# ---- PageWriter (the main page-write path) passes the roster ----

def page_write(pack, script, *, tiers=TIERS):
    from evowiki.deep.writer import PageWriter, WriteFailed
    env = (MemoryRepo(["u1"]), Script(), WikiIds())
    inp = outline_input(env, organized(env))
    ds = script(TierScript().plan())
    llm = replace(ds.llm(), tiers=tiers)
    if pack is not NO_SOURCE:
        llm = replace(llm, harness=lambda uid: pack)
    try:
        res, failed = PageWriter(llm=llm, checker=FakeChecker(), library=library()).write(inp), None
    except WriteFailed as exc:
        res, failed = None, exc
    return ds, res, failed


def test_page_writer_empty_pack_with_tiers_sends_identical_requests():
    plain, r1, f1 = page_write(NO_SOURCE, two_rounds, tiers=None)
    empty, r2, f2 = page_write(EMPTY, two_rounds)
    assert f1 is None and f2 is None and r1.calls == r2.calls == 6
    assert dumps(plain.requests) == dumps(empty.requests) and plain.routed == empty.routed
    assert r1.model == r2.model


def test_page_writer_routes_the_write_tier_and_a_fast_hook():
    pack = HarnessPack(1, {"H10.deep.write.tier": "good", "H9.deep.before_write": [DELEGATE]})
    ds, res, failed = page_write(pack, lambda s: s.write(*write_all()).push(
        "hook.deep.before_write", turn(text="笔记：先讲配对。")))
    assert failed is None and res.calls == 6
    assert [(m, p) for m, p in ds.routed if p != "deep.write"] == [
        ("deepseek-v4-flash", "hook.deep.before_write")]
    assert {m for m, p in ds.routed if p == "deep.write"} == {"deepseek-v4-pro"}
    assert NOTES_HEAD + "笔记：先讲配对。" in first_messages(ds)[0]


# ---- fix round 1: hooks share the writer fallback (R7); only the writing chain is reported ----

FLASH = GatewayConfig(generator=ModelRef(provider="deepseek", model="deepseek-v4-flash"),
                      verifier=ModelRef(provider="deepseek", model="deepseek-v4-flash"),
                      allow_single_provider=True)


def test_deep_tier_hook_switches_the_writer_fallback_and_the_writes_follow():
    from tests.agent.test_loop_trace import Recorder
    sink = Recorder()
    step = {**DELEGATE, "tier": "deep"}
    ds, _, failed, notes = run(
        HarnessPack(1, {"H9.deep.before_write": [step]}),
        lambda s: s.write(*write_all()).push(
            "hook.deep.before_write", ProviderTransientError("429 rate limited"),
            turn(text="笔记：先讲配对。")),
        fallback=DEEPSEEK_PRO, trace=sink)
    assert failed is None
    assert notes[0] == ("anthropic:claude-sonnet-5-5", False, "")
    assert notes[1][:2] == ("deepseek:deepseek-v4-pro", True) and "429" in notes[1][2]
    assert len(notes) == 2
    assert [p for p, purpose in ds.served if purpose == "hook.deep.before_write"] == [
        "anthropic", "deepseek"]
    assert {p for p, purpose in ds.served if purpose == "deep.write"} == {"deepseek"}
    assert all(RESUME_NOTE not in m for m in first_messages(ds))        # round 0: nothing written
    starts = [c[1] for c in sink.calls if c[0] == "start"]
    hook_ids = [r.split(":hook:")[1] for r in starts if ":hook:" in r]
    assert hook_ids == ["before_write:0", "before_write:0:w1"]
    assert [r.rsplit(":", 1)[1] for r in starts if ":write:" in r] == ["w1"]


def test_a_bad_stored_tier_is_the_packs_own_failure_not_d2():
    ds, _, failed, _ = run(HarnessPack(1, {"H10.deep.write.tier": "fast"}), lambda s: s)
    assert failed.failure == "internal" and "HarnessInvalid" in failed.detail
    assert ds.requests == []
