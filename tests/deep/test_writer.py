"""The single page writer (2026-10-10): a page written from its outline (every non-check section required,
streamed through `on_change`), the writer fallback (Claude → DeepSeek in the same workspace, reported),
cancel, and budget errors that never fall back. Scripted gateways only."""

from threading import Event

import pytest

from evowiki.deep.agent import RESUME_NOTE
from evowiki.deep.writer import PageWriter, WriteFailed
from evowiki.goals.fold import Page, states_for
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.providers import ProviderTransientError
from evowiki.llm.usage import BudgetExceededError
from evowiki.wikiapp.refine_apply import refine_input, stream_body
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import MemoryRepo
from tests.deep.fakes import DeepScript, FakeChecker, call, finish, library, section, turn
from tests.deep.test_evoui_parity import BASE as BLOCK
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized

DEEPSEEK = ModelRef(provider="deepseek", model="deepseek-v4-pro")
FALLBACK = GatewayConfig(generator=DEEPSEEK, verifier=DEEPSEEK, allow_single_provider=True)


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def outline_input(env, pid):
    """The organized page's outline with no texts: what a fresh page write starts from."""
    snap = env[0].load("u1")
    page = snap.state.pages[pid].page
    fresh = Page(outline=page.outline, states=states_for(page.outline), texts={},
                 snapshot=page.snapshot, unresolved_conflicts=(), conflicts=())
    return refine_input(snap, pid, NOW, WikiSpecSource(env[0]), page=fresh, mode="page")


def writer(ds: DeepScript, fallback=None, meter=None) -> PageWriter:
    llm = ds.llm(meter)
    if fallback is not None:
        from dataclasses import replace
        llm = replace(llm, fallback=fallback)
    return PageWriter(llm=llm, checker=FakeChecker(), library=library())


def write_all(i: int = 1) -> list[dict]:
    return [turn(section("s1", i, role="organizer", depth="quick", heading="从你会的出发")),
            turn(section("s2", i)),
            turn(section("s3", i, role="recap", depth="quick", heading="标准差提醒")),
            turn(finish(i))]


def test_page_mode_requires_every_non_check_section_and_streams_in_order(env):
    pid = organized(env)
    inp = outline_input(env, pid)
    assert inp.required == ("s1", "s2", "s3") and not inp.page.texts
    seen = []
    early = [turn(section("s1", heading="从你会的出发", role="organizer", depth="quick")),
             turn(finish(1))]                        # finish before s2/s3: refused
    ds = DeepScript().plan().write(*early, *write_all(2)[1:])
    res = writer(ds).write(inp, on_change=lambda ws, sid: seen.append((sid, stream_body(ws, sid))))
    assert [s.section_id for s in res.outcome.draft.sections] == ["s1", "s2", "s3"]
    assert [sid for sid, _ in seen] == ["s1", "s2", "s3"]
    refused = next(m.text for r in ds.requests if r.purpose == "deep.write" for m in r.messages
                   if m.role == "tool" and "还没写" in m.text and "提交" in m.text)
    assert "s2" in refused and "s3" in refused
    assert res.model == "anthropic:claude-sonnet-5-5" and res.fallback_reason == ""
    # plan_page is a write turn of its own (no deep.plan call): 6 priced turns
    assert res.cost_usd == pytest.approx(6 * 0.007) and res.calls == 6
    assert ds.count("deep.plan") == 0
    first = next(r for r in ds.requests if r.purpose == "deep.write").messages[0].text
    assert "这些段都要写，按这个顺序：s1, s2, s3" in first and "开始吧：先调用 plan_page。" in first
    assert "这个学习者的表达参数" in first and "骨架" not in first


def test_a_block_streams_inline_once_written(env):
    pid = organized(env)
    seen = []
    body = "先看配对。\n\n{{block:pair}}\n"
    ds = DeepScript().plan().write(
        turn(section("s1", role="organizer", depth="quick")),
        turn(section("s2", body=body)),
        turn(call("write_block", block_id="pair", section_id="s2", why="动手看配对", block=BLOCK)),
        turn(section("s3", role="recap", depth="quick")), turn(finish()))
    writer(ds).write(outline_input(env, pid),
                     on_change=lambda ws, sid: seen.append((sid, stream_body(ws, sid))))
    s2 = [b for sid, b in seen if sid == "s2"]
    assert len(s2) == 2                                   # the text, then the block on it
    assert "{{block:" not in s2[0] and "```evoui" not in s2[0] and "先看配对" in s2[0]
    assert "```evoui" in s2[1] and "{{block:" not in s2[1]


def test_read_page_shows_what_the_writer_just_wrote(env):
    pid = organized(env)
    ds = DeepScript().plan().write(
        turn(section("s1", role="organizer", depth="quick")),
        turn(call("read_page", section_id="s1")), *write_all(2)[1:])
    writer(ds).write(outline_input(env, pid))
    read = next(m.text for r in ds.requests if r.purpose == "deep.write" for m in r.messages
                if m.role == "tool" and "s1 精修正文" in m.text)
    assert "还没写" not in read


def test_unavailable_primary_falls_back_in_the_same_workspace(env):
    pid = organized(env)
    notes = []
    ds = DeepScript().plan().write(
        turn(section("s1", role="organizer", depth="quick")),
        ProviderTransientError("429 rate limited"),          # Claude goes away mid-page
        turn(section("s2")), turn(section("s3", role="recap", depth="quick")), turn(finish()))
    res = writer(ds, FALLBACK).write(outline_input(env, pid),
                                     on_writer=lambda m, fb, why: notes.append((m, fb, why)))
    assert [s.section_id for s in res.outcome.draft.sections] == ["s1", "s2", "s3"]
    assert notes[0] == ("anthropic:claude-sonnet-5-5", False, "")
    assert notes[1][:2] == ("deepseek:deepseek-v4-pro", True) and "429" in notes[1][2]
    assert res.model == "deepseek:deepseek-v4-pro" and "429" in res.fallback_reason
    served = [p for p, purpose in ds.served if purpose == "deep.write"]
    # plan_page and s1 on Claude, the 429, then DeepSeek in the same workspace (plan kept)
    assert served[:3] == ["anthropic"] * 3 and set(served[3:]) == {"deepseek"}
    resumed = [r for r in ds.requests if r.purpose == "deep.write"][3].messages[0].text
    assert "规划（已定，按它写）" in resumed and "先调用 plan_page" not in resumed
    assert RESUME_NOTE in resumed and "s1 精修正文" in resumed   # the draft is shown again
    assert "EvoUI 索引" in resumed


def test_no_fallback_left_fails_closed(env):
    pid = organized(env)
    ds = DeepScript().plan().write(ProviderTransientError("503"))
    with pytest.raises(WriteFailed) as e:
        writer(ds).write(outline_input(env, pid))
    assert e.value.failure == "llm_unavailable" and e.value.model == "anthropic:claude-sonnet-5-5"


def test_budget_never_falls_back(env):
    pid = organized(env)

    class Spent:
        def check(self, account):
            raise BudgetExceededError(account, "day", 1.0)

        def record(self, *a, **k):
            raise AssertionError("nothing may be billed")
    ds = DeepScript()
    with pytest.raises(WriteFailed) as e:
        writer(ds, FALLBACK, meter=Spent()).write(outline_input(env, pid))
    assert e.value.failure == "budget_exceeded" and ds.served == []


def test_cancel_stops_before_the_next_call(env):
    pid = organized(env)
    stop = Event()
    ds = DeepScript().plan().write(turn(section("s1", role="organizer", depth="quick")),
                                   *write_all()[1:])

    def cancel_after_first(ws, sid):
        stop.set()
    with pytest.raises(WriteFailed) as e:
        writer(ds).write(outline_input(env, pid), on_change=cancel_after_first, cancel=stop)
    assert e.value.failure == "cancelled"
    assert ds.count("deep.write") == 2                 # plan_page, then s1


def test_english_writer_carries_output_language_after_chinese_reference_and_tool_frames(env):
    from dataclasses import replace

    from evowiki.goals.language import language_rule

    pid = organized(env)
    inp = replace(outline_input(env, pid), language="en")
    english = [turn(section("s1", heading="Start with a familiar example", role="line", depth="quick",
                            cue="Read the example", answer="A familiar example", body="Start with an example you know.")),
               turn(section("s2", heading="Follow the calculation", cue="Compare the values",
                            answer="The values are equal", body="Follow each calculation step.")),
               turn(section("s3", heading="Keep the key idea", role="recap", depth="quick",
                            cue="Remember the key idea", answer="Equal values", body="Keep the main relationship in mind.")),
               turn(finish())]
    ds = DeepScript().plan().write(*english)
    result = writer(ds).write(inp)
    assert [section.body for section in result.outcome.draft.sections] == [
        "Start with an example you know.", "Follow each calculation step.", "Keep the main relationship in mind."]
    assert result.calls == 5  # same planning/writing calls; the language directive is not a new model step
    requests = [request for request in ds.requests if request.purpose == "deep.write"]
    assert len(requests) == 5
    for request in requests:
        directive = request.messages[-1]
        assert directive.role == "user" and language_rule("en") in directive.text
        assert "outline and section headings" in directive.text
        assert "HTML/EvoUI" in directive.text and "original source quotations" in directive.text
        assert request.system == requests[0].system
        assert language_rule("en") not in request.system
    assert any(message.role == "tool" for message in requests[-1].messages[:-1])
    assert "这些段都要写" in requests[0].messages[0].text  # developer-language frame is not mistaken for output language
