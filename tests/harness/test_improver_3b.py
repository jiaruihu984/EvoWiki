"""The improver sees and proposes structural keys (harness RSI phase 3b, Task 5): read_pack lists
every key diff.refuse lets it change, with kind / options / approval / hook schema; propose takes
any JSON value and hands refuse's plain reason back; the prompt is improver@v5 (was v1 text with step 5
rewritten for the smoke check). Scripted model, fake writer: nothing reaches a real provider."""

import re
from dataclasses import replace

import pytest

from evowiki.deep.library import MemoryLibrary
from evowiki.harness import improver as improver_mod
from evowiki.harness import improver_prompt
from evowiki.harness.diff import KeyOp, PackDiff, refuse
from evowiki.harness.improver import ImproverRun, component_infos, run_improver
from evowiki.harness.keys import (
    HOOK_MAX_TOOL_CALLS,
    HOOK_TEXT_MAX,
    HOOKS,
    LOCKED_TOOLS,
    TOGGLE_TOOLS,
)
from evowiki.harness.pack import EMPTY
from evowiki.service.harness_defaults import defaults
from tests.deep.fakes import component
from tests.deep.test_components import CFG, configurable
from tests.harness.test_improver import CASES, Improver, deps, mturn, tc
from tests.harness.test_round import Env, FakeImprover
from tests.runtime.helpers import make_user

MORE = re.compile(r"\n（还有 \d+ 字，用 offset=(\d+) 接着读）$")
KEY_ROW = re.compile(r"^- (H\d+\.[^｜]+)｜", re.MULTILINE)
STEP = {"at": "deep.before_write", "kind": "delegate", "tier": "good", "tools": ["read_k"],
        "instruction": "先读这一段要用到的前置概念，列出学习者还不会的。",
        "output_to": "context.notes", "max_tool_calls": 3}
NONE = mturn(tc("compose", 9, decision="none", ops=[], spec_ops=[], citations=["signal:sig1"],
                rationale="只是看看"))


def _pack(pool, dim=None, forged=None, library=None) -> str:
    d = replace(deps(pool, Improver()), forged=forged or {},
                components=component_infos(library))
    run = ImproverRun("u1", CASES, d)
    base = {} if dim is None else {"dim": dim}
    out, offset = "", 0
    while True:                         # the whole text, page by page
        part = run.read_pack({**base, "offset": offset})
        m = MORE.search(part)
        if m is None:
            return out + part
        out += part[:m.start()]
        offset = int(m.group(1))


def _propose(pool, *ops) -> list[str]:
    """Each op proposed alone as the specific candidate; the tool result text of each."""
    make_user(pool, "u1")
    imp = Improver(*(mturn(tc("propose", i, kind="specific", ops=[op], spec_ops=[],
                              rationale="执行记录显示没读前置概念", citations=["signal:sig1"]))
                     for i, op in enumerate(ops, 1)), NONE)
    run_improver("u1", CASES, deps=deps(pool, imp))
    return [r[0] for r in imp.results[1:len(ops) + 1]]


# --- read_pack -----------------------------------------------------------------------------------

def test_read_pack_h10_lists_the_deep_tiers_with_options_and_approval(pool):
    make_user(pool, "u1")
    imp = Improver(mturn(tc("read_pack", 1, dim="H10")), NONE)
    run_improver("u1", CASES, deps=deps(pool, imp))
    (text,) = imp.results[1]
    assert "H10.deep.write.tier" in text and "deep" in text and "good" in text
    assert "要你批准" in text
    assert "assistant.turn" not in text and "bot.plan" not in text and "forge.code" not in text
    assert ("- H10.deep.write.tier｜写页面用哪一档模型｜来源 默认｜现在：（未设，用默认）｜"
            "默认：\"deep\"｜只能是 deep、good 之一｜要你批准") in text
    assert "- H10.deep.write.thinking｜写页面要不要深度思考｜来源 默认｜现在：（未设，用默认）｜默认：（没有固定值）｜true 或 false｜要你批准" in text


def test_every_listed_key_is_one_refuse_lets_through(pool):
    text = _pack(pool, forged={"deep": ("check_balanced",), "assistant": ("zz_lookup",)})
    names = KEY_ROW.findall(text)
    assert names
    for n in names:
        # an assistant tool can only be switched off (rule R1): its H3 row is checked with False
        value = False if n.startswith("H3.assistant.") else None
        assert refuse(PackDiff((KeyOp(n, value),)), EMPTY, {}, defaults=defaults()) is None, n
    dims = {n.split(".")[0] for n in names}
    assert dims == {"H1", "H2", "H3", "H4", "H5", "H8", "H9", "H10", "H11", "H12"}
    for gone in ("H6.", "H4.forge", "H10.forge", "H12.patch", ".bot.", "zz_lookup",
                 "H9.assistant", "H10.assistant", "H5.assistant.context_budget",
                 "H5.assistant.top_k_pages", "H12.tool.assistant"):
        assert not any(gone in n for n in names), gone


def test_read_pack_lists_exactly_the_measured_assistant_keys(pool):
    """Harness 3b follow-up 3 (rule R1): the assistant keys read_pack lists are exactly those an
    assistant replay measures: H1.assistant, the built-in tools' H2 texts, H3 switch-offs, H4
    max_tool_calls and the two H5 elide keys."""
    from evowiki.harness.diff import ASSISTANT_MEASURED
    from evowiki.harness.tool_names import TOOLS
    text = _pack(pool)
    names = set(KEY_ROW.findall(text))
    listed = {n for n in names if n.split(".")[1] == "assistant" or n == "H1.assistant"}
    h2 = {n for n in listed if n.startswith("H2.")}
    assert {n.split(".")[2] for n in h2} == set(TOOLS["assistant"])
    assert listed - h2 == ({"H1.assistant"} | set(ASSISTANT_MEASURED)
                           | {f"H3.assistant.{t}" for t in TOGGLE_TOOLS["assistant"]})
    assert "- H3.assistant.web_search｜" in text and "只能填 false" in text


def test_structural_rows_carry_kind_options_approval_and_the_hook_schema(pool):
    text = _pack(pool)
    assert "H3 工具开关（写页面能关掉的工具：" + "、".join(TOGGLE_TOOLS["deep"]) in text
    assert "、".join(LOCKED_TOOLS["deep"]) + " 一直开着" in text
    assert ("- H3.deep.read_k｜写页面：能不能用 read_k｜来源 默认｜现在：（未设，用默认）｜默认：true｜"
            "true 或 false｜要你批准") in text
    assert "- H8.deep.spec.write｜" in text and "列表，从 items、exemplar 里选 0–2 项｜自动" in text
    assert "- H8.deep.evoui_examples｜" in text and '"block": 交互块}｜自动' in text
    hook = HOOKS["deep.before_write"]
    assert (f'"at": "deep.before_write", "kind": "delegate", "tier": fast、good、deep 之一, '
            f'"tools": [从 {"、".join(hook.tools)} 里选], "instruction": 说明（最多 {HOOK_TEXT_MAX} 字）, '
            f'"output_to": "context.notes", "max_tool_calls": 0–{HOOK_MAX_TOOL_CALLS}}}｜要你批准') in text
    assert '- H11.deep.after_write｜' in text and '"kind": "self_check"' in text
    assert "H9.assistant" not in text
    # non-structural rows keep their phase-2 form
    assert "- H4.deep.max_tool_calls｜写页面：每轮最多调用几次工具｜来源 默认｜现在：（未设，用默认）｜默认：80\n" in text


def test_forged_tools_are_listed_only_when_given_and_only_for_improver_agents(pool):
    without = _pack(pool)
    assert "H12" not in "".join(KEY_ROW.findall(without)) and "自己造的工具" not in without
    text = _pack(pool, forged={"deep": ("check_balanced", "read_k"), "assistant": ("zz_lookup",)})
    assert ("- H3.deep.check_balanced｜写页面：用自己造的工具 check_balanced｜来源 默认｜"
            "现在：（未设，用默认）｜默认：false｜true 或 false｜自动") in text
    assert ('- H12.tool.deep.check_balanced｜自己造的工具｜来源 默认｜现在：（未设，用默认）｜'
            '默认：（没有固定值）｜{"candidate_id": "fc_…"}｜自动') in text
    # the opt-in sits under the H3 block (before H8), the record in its own H12 block at the end
    assert (text.index("H3 工具开关") < text.index("- H3.deep.check_balanced")
            < text.index("- H8.deep.spec.plan") < text.index("- H12.tool.deep.check_balanced"))
    assert "zz_lookup" not in text                       # assistant is not an improver agent
    assert "H12.tool.deep.read_k" not in text         # a built-in is never a forged tool
    h12 = _pack(pool, "H12", forged={"deep": ("check_balanced",)})
    assert "H12.tool.deep.check_balanced" in h12 and "H3.deep.check_balanced" not in h12
    assert "现在没有你能改的设置" in _pack(pool, "H12")


def test_component_keys_come_from_the_library(pool):
    lib = MemoryLibrary([component("pair-demo"), configurable()])
    infos = component_infos(lib)
    assert infos["pair-demo"].config is None and infos["pair-demo"].description == "点按钮看计数加一"
    assert '"title": "复制 DNA"' in (infos["steps-demo"].config or "")
    assert component_infos(None) == {}
    text = _pack(pool, "H8", library=lib)
    assert ("- H8.component.pair-demo.note｜交互组件：什么时候用｜来源 默认｜现在：（未设，用默认）｜"
            "默认：\"点按钮看计数加一\"｜文字，最多 400 字｜自动") in text
    assert "H8.component.pair-demo.presets" not in text          # fixed content: no presets
    assert "- H8.component.steps-demo.note｜" in text
    assert ("- H8.component.steps-demo.presets｜交互组件：新的预设｜来源 默认｜现在：（未设，用默认）｜"
            '默认：[]｜列表，每项 {"name": 名字（最多 40 字）, "use": 什么时候用（最多 200 字）, '
            '"config": 按下面这个组件的配置格式填的对象}｜要你批准') in text
    assert "  steps-demo 的配置格式（JSON Schema）：" in text
    # every listed component key passes refuse; none without a library
    names = [n for n in KEY_ROW.findall(text) if n.startswith("H8.component.")]
    assert len(names) == 3
    for n in names:
        assert refuse(PackDiff((KeyOp(n, None),)), EMPTY, {}, defaults=defaults()) is None, n
    assert "H8.component." not in _pack(pool, "H8")


@pytest.mark.parametrize("with_library", [True, False])
def test_the_round_passes_the_library_components_to_the_improver(pool, with_library):
    lib = MemoryLibrary([component("pair-demo"), configurable()]) if with_library else None
    imp = FakeImprover(None, decision="none")
    env = Env(pool, improver=imp)
    env.deps = replace(env.deps, library=lib)
    env.run()
    (seen,) = imp.seen
    assert set(seen.components) == ({"pair-demo", "steps-demo"} if with_library else set())
    if with_library:
        assert seen.components["steps-demo"].config and seen.components["pair-demo"].config is None


# --- propose -------------------------------------------------------------------------------------

def test_propose_takes_structural_values_and_hands_back_refuse_reasons(pool):
    bad_step = {**STEP, "tier": "deep", "tools": ["write_block"]}
    preset = {"name": "二分查找", "use": "讲算法一步步怎么缩小范围时", "config": CFG}
    ok, toggle, bad, tier, think, pre, note = _propose(
        pool,
        {"name": "H9.deep.before_write", "value": [STEP]},
        {"name": "H10.assistant.turn.tier", "value": "good"},    # R1: a replay cannot measure it
        {"name": "H9.deep.before_write", "value": [bad_step]},
        {"name": "H10.deep.write.tier", "value": "good"},
        {"name": "H10.deep.write.thinking", "value": True},
        {"name": "H8.component.steps-demo.presets", "value": [preset]},
        {"name": "H8.component.pair-demo.note", "value": "要让学习者亲手试配对规则时用"})
    assert ok.startswith("已记录针对性候选：1 个设置改动")
    assert "重放测不出这个设置的效果" in toggle
    assert bad == "这个改动不能用：H9.deep.before_write 第 1 步：这里不能用工具 write_block"
    assert tier.startswith("已记录") and think.startswith("已记录")
    assert pre.startswith("已记录") and note.startswith("已记录")


def test_the_op_schema_takes_any_json_value():
    value = improver_mod._OP_SCHEMA["items"]["properties"]["value"]
    assert "type" not in value and value["description"]
    assert improver_mod._KeyOpIn(name="H3.deep.read_k", value=False).value is False
    assert improver_mod._KeyOpIn(name="H1.write", value="  多举例 ").value == "多举例"


# --- prompt --------------------------------------------------------------------------------------

def test_the_prompt_is_v5_and_covers_both_agents_and_code(pool):
    p = improver_prompt
    assert p.PROMPT_VERSION == "improver@v5" and p.PURPOSE == "rsi.h.improve"
    for word in ("陪读助手", "写页面", "propose_patch", "评测集", "某一条记忆", "H10", "只是数据"):
        assert word in p.SYSTEM, word
    for absent in ("deepseek", "anthropic", "claude", "sonnet", "opus"):
        assert absent not in p.SYSTEM.lower(), absent
    make_user(pool, "u1")
    imp = Improver(NONE)
    run_improver("u1", CASES, deps=deps(pool, imp))
    req = imp.requests[0]
    assert req.system == p.SYSTEM and req.prompt_version == "improver@v5"
    (pack_tool,) = [t for t in req.tools if t.name == "read_pack"]
    assert pack_tool.parameters["properties"]["dim"]["enum"] == [
        "H1", "H2", "H3", "H4", "H5", "H7", "H8", "H9", "H10", "H11", "H12"]


def test_read_pack_lists_the_materials_tools_with_their_default_texts(pool):
    """MF4: the assistant offers list_sources / read_source / search_source when the user has
    materials, so they are built-in tools: read_pack lists their H2 keys with the real default
    descriptions (`replay_assistant.builtin_specs`) and their H3 switch-offs."""
    from evowiki.harness.improver import assistant_tool_texts
    text = _pack(pool)
    texts = assistant_tool_texts()
    for name in ("H2.assistant.list_sources", "H2.assistant.read_source.source_id",
                 "H2.assistant.search_source.query"):
        row = next(ln for ln in text.splitlines() if ln.startswith(f"- {name}｜"))
        assert texts[name] and f"默认：\"{texts[name][:20]}" in row, row
    for tool in ("list_sources", "read_source", "search_source"):
        assert f"- H3.assistant.{tool}｜" in text
