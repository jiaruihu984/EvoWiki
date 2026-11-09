"""Prompt steps and guidance markers (plan 2026-10-21 参数 redesign, D1 / D2): the step registry
covers every product purpose, each step's guidance is marked in its prompts, markers vanish before
a request is hashed or sent, and a user's override replaces only its span."""

import inspect
import re
from datetime import UTC, datetime

from pydantic import BaseModel

from evowiki.assistant import compact as compact_mod
from evowiki.assistant import loop as loop_mod
from evowiki.deep import agent as deep_agent
from evowiki.deep import skill as write_skill
from evowiki.goals import path
from evowiki.goals.itemflaws import ITEM_RULES_ZH
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.prices import PriceTable
from evowiki.llm.prompt_steps import (
    MARK_CLOSE,
    STEPS,
    apply_marks,
    marked,
    open_mark,
    step_of,
    strip_marks,
)
from evowiki.llm.scripted import ScriptedChatProvider, ScriptedProvider
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl
from evowiki.service import engine, prompt_defaults
from evowiki.wikiapp import graph_groups
from tests.llm_cases.inventory import INVENTORY

ANY_MARK = re.compile(r"⟦g:[a-z0-9_]+⟧|⟦/g⟧")


# ---- registry ----

# Purposes with no 参数 step: the material-import prompts (table of contents, summary, glossary)
# run once per upload and are not user-tunable; they stay in INVENTORY for the prompt cases.
NOT_STEPPED = {"sources.toc", "sources.summary", "sources.glossary",
               "sources.exam", "sources.exam_attach",
               # the repair of a refused reading plan: its own fixed prompt, not one the user can edit
               "goals.reading.repair",
               # fixed slot-filling and yes/no prompts, nothing a user would tune
               "goals.rough_typed", "review.card_stale",
               # the rule guard's judge (rule provenance contract): never a step, so no H1 key
               # (and no user prompt override) reaches its prompt
               "teach.rule_conflict",
               # the conversation summary's fixed evaluator (evaluation/compact.py, protected):
               # never a step, so neither H1 nor a user override reaches its judge prompt
               "eval.compact.judge",
               # the page-writing evaluator's core (evaluation/page.py, protected), likewise
               "eval.page.judge",
               # cross-session memory's background calls (memory/): no step, no user override
               "assistant.consolidate", "memory.conflicts",
               # the page's visuals (deep/visual.py, deep/scene.py): inventoried on master, no step yet
               "deep.scene", "deep.visual", "deep.visual.decide", "deep.visual.recheck"}


def test_every_inventory_purpose_belongs_to_exactly_one_step():
    assert NOT_STEPPED <= set(INVENTORY)
    for purpose in set(INVENTORY) - NOT_STEPPED:
        owners = [s.key for s in STEPS if purpose in s.purposes]
        assert len(owners) == 1, (purpose, owners)
        assert step_of(purpose) is not None and step_of(purpose).key == owners[0]
    listed = [p for s in STEPS for p in s.purposes]
    assert sorted(listed) == sorted(set(INVENTORY) - NOT_STEPPED)   # no unknown purpose
    assert all(step_of(p) is None for p in NOT_STEPPED)
    assert len({s.key for s in STEPS}) == len(STEPS)
    assert len({s.name for s in STEPS}) == len(STEPS)
    assert all(s.description.strip() and len(s.description) >= 10 for s in STEPS)
    assert all(s.source in INVENTORY for s in STEPS)
    assert step_of("not.a.purpose") is None


# ---- every step's guidance is marked in a prompt it uses ----

def _src(*objs) -> str:
    """Source of builders too costly to call here (their state types are large); the marker
    sits in a literal of the returned prompt."""
    return "\n".join(inspect.getsource(o) for o in objs)


RENDERED = {
    "decompose": lambda: engine._enter(),
    "source_outline": lambda: engine._intake((engine.SOURCE,)),
    "source_reference": lambda: engine._intake((engine.SOURCE,), mode="reference"),
    "understand": lambda: engine._intake((engine.SOURCE,), "understand"),
    "blocks": lambda: engine._blocks(),
    "page_fit": lambda: engine._page_fit(),
    "reading_unit": lambda: engine._reading_unit(),
    "path": lambda: _src(path.path_prompt),
    "outline": lambda: engine._outline(),
    "write": lambda: engine._deep(deep_agent.WRITE_SYSTEM),
    "check": lambda: engine._outline(),
    "followup": lambda: engine._followup(),
    "card": lambda: engine._card_cue(),
    "assistant": lambda: loop_mod.SYSTEM,
    "compact": lambda: compact_mod.COMPACT_SYSTEM,
    "feedback": lambda: engine._understand(),
    "graph_group_naming": lambda: graph_groups.naming_request(()).prompt,
    "source_match": prompt_defaults._source_match,
}


def test_every_step_has_a_marked_prompt():
    assert set(RENDERED) == {s.key for s in STEPS}
    for key, render in RENDERED.items():
        text = render()
        assert open_mark(key) in text, key
        assert text.count(MARK_CLOSE) >= text.count("⟦g:"), key


def test_rendered_prompts_have_one_span_per_step_and_no_stray_markers():
    for key in ("decompose", "source_outline", "source_reference", "understand", "blocks",
                "page_fit", "reading_unit",
                "outline", "check", "followup", "card",
                "assistant", "compact", "feedback",
                "graph_group_naming", "source_match"):
        text = RENDERED[key]()
        for k, default in marked(text).items():
            assert default.strip(), (key, k)
        assert text.count("⟦g:") == text.count(MARK_CLOSE), key
        assert "⟦g:" not in strip_marks(text) and MARK_CLOSE not in strip_marks(text)


# Every real render of a marked prompt (several per step where a step has several prompts).
ALL_RENDERS = (
    engine._intake, engine._enter, RENDERED["source_outline"], RENDERED["source_reference"],
    RENDERED["understand"], engine._blocks, engine._page_fit, engine._reading_unit,
    engine._outline, lambda: engine._deep(deep_agent.WRITE_SYSTEM), engine._followup, engine._card_cue,
    engine._card_check, lambda: loop_mod.SYSTEM, lambda: compact_mod.COMPACT_SYSTEM,
    engine._understand,
    RENDERED["graph_group_naming"], RENDERED["source_match"])


def test_each_step_has_one_default_text_in_every_prompt_that_carries_it():
    """One override replaces the step's span in every prompt, so every prompt carrying the
    step's marker must have the same default there."""
    seen: dict[str, set[str]] = {}
    for render in ALL_RENDERS:
        text = render()
        for m in re.finditer(r"⟦g:([a-z0-9_]+)⟧(.*?)⟦/g⟧", text, re.DOTALL):
            seen.setdefault(m.group(1), set()).add(m.group(2))
    assert {"decompose", "check", "write"} <= set(seen)
    for key, defaults in seen.items():
        assert len(defaults) == 1, key
    # intake@v32: the user's blocks prompt carries no 拆成知识点 span; the one-level split does
    assert len(re.findall("⟦g:decompose⟧", engine._intake() + engine._enter())) == 1
    # the source step's span appears only when a material is attached, next to decompose's
    assert not re.search("⟦g:source_", engine._intake() + engine._enter())
    assert {"blocks", "source_outline"} <= set(marked(RENDERED["source_outline"]()))
    assert set(marked(RENDERED["source_reference"]())) == {"blocks", "source_reference"}
    assert set(marked(RENDERED["understand"]())) == {"understand", "blocks"}
    assert set(marked(engine._blocks())) == {"blocks"}


def test_the_outline_part_marks_outline_and_check_guidance():
    spans = marked(engine._outline())
    assert set(spans) == {"outline", "check"}
    assert spans["check"].startswith("出题规则（单选确认题）")
    assert spans["outline"].startswith("- 根据本页的学习目标和内容")
    # the tool and its format stay outside the guidance
    assert "plan_page" not in spans["outline"]


def test_the_writer_applies_the_outline_guidance_to_its_first_message():
    """The gateway applies guidance to system prompts only; the outline part sits in the first
    user message, so the writer applies the run's guidance there itself (marks always removed)."""
    from tests.goals.fakes_k import zscore_k, zscore_spec
    part = deep_agent.outline_part(zscore_spec(), zscore_k())
    out, applied = apply_marks(part, {"outline": "按我的方式排。"})
    assert applied and "按我的方式排。" in out and "根据本页的学习目标和内容" not in out
    assert not ANY_MARK.search(apply_marks(part, {})[0])


def test_engine_display_has_no_markers():
    for s in engine.steps():
        assert not ANY_MARK.search(engine.step_text(s)), s.step


def test_marked_constants_unmarked_equal_their_wording():
    # wrapping changed no wording: the stripped check rules are the rules the prompt had
    assert ITEM_RULES_ZH in strip_marks(write_skill.ref("assessment"))     # deep@v32: the check rules
    assert "⟦/g⟧" not in strip_marks(deep_agent.WRITE_SYSTEM)


# ---- apply_marks ----

TEXT = "角色行。\n规则：⟦g:a⟧先举例，再讲定义。⟦/g⟧\n⟦g:b⟧一次一件事。⟦/g⟧\n这些说法用 ⟦待查证：…⟧ 标出。\n只回复 JSON。"


def test_apply_marks_without_overrides_only_removes_the_markers():
    out, applied = apply_marks(TEXT, {})
    assert out == ANY_MARK.sub("", TEXT) and not applied
    assert out == strip_marks(TEXT)
    assert "⟦待查证：…⟧" in out                  # other ⟦…⟧ text is untouched
    assert apply_marks("没有标记", {"a": "x"}) == ("没有标记", False)


def test_apply_marks_replaces_only_the_overridden_span():
    out, applied = apply_marks(TEXT, {"a": "先讲直觉。", "other": "不在这里"})
    assert applied
    assert out == "角色行。\n规则：先讲直觉。\n一次一件事。\n这些说法用 ⟦待查证：…⟧ 标出。\n只回复 JSON。"
    out2, applied2 = apply_marks(TEXT, {"other": "不在这里"})
    assert out2 == strip_marks(TEXT) and not applied2


def test_marked_returns_default_guidance_per_step():
    assert marked(TEXT) == {"a": "先举例，再讲定义。", "b": "一次一件事。"}


# ---- gateway ----

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
ACC = Account(user_id="u1", group="page")


class _Meter:
    def check(self, account):
        pass

    def record(self, account, purpose, usage, *, replayed):
        pass


def _gateway(tmp_path, overrides, provider):
    return Gateway(CFG, CallLog(tmp_path / "calls.jsonl"), mode="record",
                   providers={"scripted": provider},
                   usage=UsageControl(meter=_Meter(), prices=PriceTable({})),
                   overrides=overrides, clock=lambda: datetime(2026, 10, 21, tzinfo=UTC))


def _request() -> LLMRequest:
    return LLMRequest(purpose="goals.intake", prompt_version="intake@v30",
                      subject="s", system="只回复 JSON。⟦g:decompose⟧系统里的规则。⟦/g⟧",
                      prompt="目标：x\n⟦g:decompose⟧1. 说具体。⟦/g⟧\n只回复 JSON。")


class _Out(BaseModel):
    text: str


def test_gateway_without_override_sends_the_unmarked_text(tmp_path):
    prov = ScriptedProvider("scripted", lambda m, r: '{"text": "好"}')
    seen: list[str] = []
    g = _gateway(tmp_path, lambda uid: seen.append(uid) or {}, prov)
    _, call = g.call("generator", _request(), _Out, account=ACC)
    sent = prov.calls[0]
    assert sent.prompt == "目标：x\n1. 说具体。\n只回复 JSON。"
    assert sent.system == "只回复 JSON。系统里的规则。"
    assert sent.prompt_version == call.prompt_version == "intake@v30"
    assert seen == ["u1"]


def test_gateway_without_account_never_reads_overrides(tmp_path):
    prov = ScriptedProvider("scripted", lambda m, r: '{"text": "好"}')
    g = Gateway(CFG, CallLog(tmp_path / "calls.jsonl"), mode="record",
                providers={"scripted": prov},
                overrides=lambda uid: (_ for _ in ()).throw(AssertionError("read")))
    g.call("generator", _request(), _Out)
    assert prov.calls[0].prompt == "目标：x\n1. 说具体。\n只回复 JSON。"


def test_gateway_applies_the_users_override_and_marks_the_version(tmp_path):
    prov = ScriptedProvider("scripted", lambda m, r: '{"text": "好"}')
    g = _gateway(tmp_path, lambda uid: {"decompose": "只说具体，不缩小范围。"} if uid == "u1" else {},
                 prov)
    _, call = g.call("generator", _request(), _Out, account=ACC)
    sent = prov.calls[0]
    assert sent.prompt == "目标：x\n只说具体，不缩小范围。\n只回复 JSON。"
    assert sent.system == "只回复 JSON。只说具体，不缩小范围。"
    assert sent.prompt_version == call.prompt_version == "intake@v30+user"
    # another user's call is not affected (and is a different recording)
    g.call("generator", _request(), _Out, account=Account(user_id="u2", group="page"))
    assert prov.calls[1].prompt == "目标：x\n1. 说具体。\n只回复 JSON。"
    assert prov.calls[1].prompt_version == "intake@v30"


def _chat_request() -> ChatRequest:
    stable = "共享的前缀⟦g:write⟧怎么写。⟦/g⟧"
    return ChatRequest(purpose="deep.write", prompt_version="deep@v18", subject="p",
                       system="你是作者。\n⟦g:write⟧- 先讲人话。⟦/g⟧\n用 write_section 写。",
                       messages=(ChatMessage(role="user", text=stable + "这一页的部分",
                                             stable_prefix=len(stable)),))


def test_converse_without_override_sends_the_unmarked_text(tmp_path):
    prov = ScriptedChatProvider("scripted", lambda m, r: ChatTurn(text="好"))
    g = _gateway(tmp_path, lambda uid: {}, prov)
    _, call = g.converse("generator", _chat_request(), account=ACC)
    sent = prov.chat_calls[0]
    assert sent.system == "你是作者。\n- 先讲人话。\n用 write_section 写。"
    assert sent.messages[0] == _chat_request().messages[0]      # messages are never processed
    assert sent.prompt_version == call.prompt_version == "deep@v18"


def test_converse_applies_the_users_override(tmp_path):
    prov = ScriptedChatProvider("scripted", lambda m, r: ChatTurn(text="好"))
    g = _gateway(tmp_path, lambda uid: {"write": "例子要能算出结果。"}, prov)
    _, call = g.converse("generator", _chat_request(), account=ACC)
    sent = prov.chat_calls[0]
    assert sent.system == "你是作者。\n例子要能算出结果。\n用 write_section 写。"
    assert sent.messages[0] == _chat_request().messages[0]      # messages are never processed
    assert sent.prompt_version == call.prompt_version == "deep@v18+user"


def test_real_prompt_through_the_gateway_is_todays_text(tmp_path):
    """A real marked prompt (S1's): the provider gets exactly the unmarked text."""
    prov = ScriptedProvider("scripted", lambda m, r: '{"text": "好"}')
    g = _gateway(tmp_path, lambda uid: {}, prov)
    text = engine._blocks()
    req = LLMRequest(purpose="goals.intake", prompt_version="v", subject="p",
                     system="只回复 JSON。", prompt=text)
    g.call("generator", req, _Out, account=ACC)
    assert prov.calls[0].prompt == ANY_MARK.sub("", text)
    assert not ANY_MARK.search(prov.calls[0].prompt)
