"""The writer's practice mode (stage 4 T5, `deep.agent.practice`): the assistant's add_practice adds ONE
interactive to a written page with the write round's tools, render check and fence — no plan, no outline,
no text. Scripted gateways only."""

from dataclasses import replace

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.deep import agent
from evowiki.deep.agent import PRACTICE_SYSTEM, PracticeAsk
from evowiki.deep.tools import SubmitTool, Workspace
from evowiki.deep.writer import PageWriter, WriteFailed
from evowiki.goals.contracts import SectionRole
from evowiki.llm.budget import JobBudget
from evowiki.wikiapp.refine_apply import refine_input
from evowiki.wikiapp.refine_spec import WikiSpecSource
from tests.deep.fakes import (
    PRACTICE_BLOCK,
    DeepScript,
    FakeChecker,
    call,
    interactive,
    library,
    turn,
)
from tests.deep.test_writer import env  # noqa: F401 - fixture
from tests.wikiapp.test_goal import NOW
from tests.wikiapp.test_page import organized

CTX = ToolContext(user_id="u1", session_id="t")


def practice_input(env, pid, kind="card", covers=""):  # noqa: F811
    snap = env[0].load("u1")
    inp = refine_input(snap, pid, NOW, WikiSpecSource(env[0]), mode="practice")
    return replace(inp, practice=PracticeAsk(kind=kind, covers=covers))


def writer(ds: DeepScript, checker=None) -> PageWriter:
    return PageWriter(llm=ds.llm(), checker=checker or FakeChecker(), library=library())


def block(bid="kana", sid="s2", i=1):
    return call("write_block", i, block_id=bid, section_id=sid, why="练熟：认读 10 个假名",
                block=PRACTICE_BLOCK)


def submit(iid="kana", i=1):
    return call("finish", i, interactive_id=iid)


def test_a_card_practice_writes_one_block_without_a_plan_and_submits_it(env):  # noqa: F811
    pid = organized(env)
    inp = practice_input(env, pid, "card", "あ行和か行")
    checker = FakeChecker()
    ds = DeepScript().write(turn(block()), turn(submit()))
    res = writer(ds, checker).practice(inp, budget=JobBudget())
    out = res.outcome
    assert out.block is not None and out.block.block_id == "kana" and out.interactive is None
    assert (out.section_id, out.why, out.rounds) == ("s2", "练熟：认读 10 个假名", 1)
    assert out.check.ok and checker.checked == ["kana"]    # checked when placed; the pass is reused
    assert ds.count("deep.write") == 2 and res.calls == 2
    first = ds.requests[0]
    assert first.purpose == "deep.write" and first.prompt_version == agent.PROMPT_VERSION
    assert first.system.startswith(PRACTICE_SYSTEM)
    names = {t.name for t in first.tools}
    assert names == {"read_k", "read_page", "read_spec", "read_ref", "draw_visual", "write_block", "render_check",
                     "finish"}
    msg = first.messages[0]
    page = env[0].load("u1").state.pages[pid].page
    assert page.texts["s2"].body[:80] in msg.text                  # the page's own text
    assert "闪卡" in msg.text and "あ行和か行" in msg.text           # what to add, what to cover
    assert "EvoUI 索引" in msg.text[:msg.stable_prefix]             # the write round's cached prefix
    assert "plan_page" not in msg.text and "write_section" not in msg.text


def test_an_interactive_may_place_a_library_component(env):  # noqa: F811
    pid = organized(env)
    ds = DeepScript().write(turn(interactive("pair-demo", "s2")), turn(submit("pair-demo")))
    out = writer(ds).practice(practice_input(env, pid, "interactive")).outcome
    assert out.interactive is not None and out.component_id == "pair-demo" and out.block is None
    assert out.section_id == "s2" and out.why == "让他亲手点一下，看见计数怎么变"
    assert "place_component" in {t.name for t in ds.requests[0].tools}
    assert "可用的交互组件" in ds.requests[0].messages[0].text


def test_a_round_without_a_submit_gets_another_with_that_as_a_must_fix(env):  # noqa: F811
    pid = organized(env)
    ds = DeepScript().write(turn(block()), *[turn(text="好了")] * 3, turn(submit()))
    out = writer(ds).practice(practice_input(env, pid, "card", "再来")).outcome
    assert out.rounds == 2 and out.block is not None
    from evowiki.deep.language import output_rule

    assert all(r.messages[-1].text == output_rule("zh") for r in ds.requests)
    rounds = [r.messages[:-1] for r in ds.requests if len(r.messages[:-1]) == 1]
    second = rounds[1][0].text
    assert "上一轮没有调用 finish" in second


def test_no_submit_in_any_round_fails_closed(env):  # noqa: F811
    pid = organized(env)
    ds = DeepScript().write(*[turn(text="好了")] * 9)
    with pytest.raises(WriteFailed) as e:
        writer(ds).practice(practice_input(env, pid, "card", "不交"))
    assert e.value.failure == "no_draft"


def test_the_page_job_budget_covers_a_practice_run(env):  # noqa: F811
    """The page's own budget (「每页最多花」, JobBudget) meters the run like any refine mode."""
    pid = organized(env)
    ds = DeepScript().write(turn(block()), turn(submit()))
    budget = JobBudget(1.0)
    res = writer(ds).practice(practice_input(env, pid, "card", "预算"), budget=budget)
    assert res.cost_usd == pytest.approx(0.014) and budget.spent == pytest.approx(0.014)
    ds = DeepScript().write(turn(block()), turn(submit()))
    with pytest.raises(WriteFailed) as e:          # the page's limit reached: the run stops
        writer(ds).practice(practice_input(env, pid, "card", "又一次"), budget=JobBudget(0.005))
    assert e.value.failure == "budget_exceeded"


def test_submit_takes_one_checked_interactive_on_a_written_non_check_section(env):  # noqa: F811
    pid = organized(env)
    page = env[0].load("u1").state.pages[pid].page
    ws = Workspace(page)
    ws.practice = True
    tool = SubmitTool(ws)
    assert tool.run({"interactive_id": "x"}, CTX).error
    checker = FakeChecker(fail_block=lambda bid, b: "overflow at 375")
    from evowiki.deep.tools import WriteBlockTool
    placed = WriteBlockTool(ws, checker).run(block()["args"], CTX)
    assert placed.error and "overflow" in placed.content and "{{block:" not in placed.content
    assert tool.run({"interactive_id": "kana"}, CTX).error and ws.submitted is None
    checks = [s.id for s in page.outline.sections if s.role is SectionRole.CHECK]
    if checks:                     # a check section never takes a practice
        WriteBlockTool(ws, FakeChecker()).run(block("onq", checks[0])["args"], CTX)
        assert tool.run({"interactive_id": "onq"}, CTX).error
    WriteBlockTool(ws, FakeChecker()).run(block()["args"], CTX)
    assert tool.run({"interactive_id": "kana"}, CTX).error is None and ws.submitted == "kana"
