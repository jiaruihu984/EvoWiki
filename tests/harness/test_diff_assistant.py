"""diff.refuse for assistant keys and H6 (harness 3b follow-up 3, rule R1): exactly the keys an
assistant-turn replay rebuilds are open; every other assistant key and every H6 key is refused."""

import pytest

from evowiki.harness.anchors import IMPROVER_AGENTS
from evowiki.harness.diff import (
    ASSISTANT_MEASURED,
    CLOSED,
    UNMEASURABLE,
    KeyOp,
    PackDiff,
    needs_ask,
    refuse,
)
from evowiki.harness.keys import KEYS, TOGGLE_TOOLS, key_of, needs_approval
from evowiki.harness.pack import HarnessPack
from evowiki.harness.tool_names import TOOLS
from evowiki.service.harness_defaults import defaults

D = defaults()
STEP = {"at": "assistant.before_answer", "kind": "delegate", "tier": "fast",
        "tools": ["read_page"], "instruction": "先查相关页面", "output_to": "context.notes",
        "max_tool_calls": 3}


def r(*ops, pack=None, sources=None):
    return refuse(PackDiff(tuple(ops)), pack or HarnessPack(0, {}), sources or {}, defaults=D)


def test_assistant_is_an_improver_agent():
    assert IMPROVER_AGENTS == frozenset({"deep", "assistant"})


ALLOWED = [
    KeyOp("H1.assistant", "先给答案，再问一个小问题。"),
    KeyOp("H1.assistant", None),
    KeyOp("H2.assistant.search_wiki", "在他的 Wiki 里找"),
    KeyOp("H2.assistant.search_wiki.query", "搜什么"),
    KeyOp("H2.assistant.web_search", None),
    *(KeyOp(f"H3.assistant.{t}", False) for t in TOGGLE_TOOLS["assistant"]),
    KeyOp("H4.assistant.max_tool_calls", 8),
    KeyOp("H4.assistant.max_tool_calls", 4),
    KeyOp("H4.assistant.max_tool_calls", None),
    KeyOp("H5.assistant.read_keep_calls", 5),
    KeyOp("H5.assistant.read_keep_calls", None),
    KeyOp("H5.assistant.read_stub_over", 3000),
    KeyOp("H5.assistant.read_stub_over", None),
]


@pytest.mark.parametrize("op", ALLOWED, ids=lambda o: f"{o.name}={o.value!r}")
def test_measured_assistant_keys_are_open(op):
    assert r(op) is None


UNMEASURED = [
    *(KeyOp(f"H5.assistant.{f}", v) for f, v in (
        ("context_budget", 4000), ("selection_max", 1500), ("paragraph_max", 2000),
        ("top_k_pages", 3))),
    KeyOp("H5.assistant.top_k_pages", None),
    *(KeyOp(k.name, None) for k in KEYS if k.dim == "H6"),
    *(KeyOp(k.name, k.lo) for k in KEYS if k.dim == "H6"),
    KeyOp("H9.assistant.before_answer", [STEP]),
    KeyOp("H9.assistant.before_answer", None),
    KeyOp("H10.assistant.turn.tier", "good"),
    KeyOp("H10.assistant.turn.tier", None),
    KeyOp("H10.assistant.turn.thinking", True),
]


@pytest.mark.parametrize("op", UNMEASURED, ids=lambda o: f"{o.name}={o.value!r}")
def test_unmeasured_assistant_and_h6_keys_are_refused(op):
    got = r(op)
    assert got is not None and UNMEASURABLE in got


def test_every_static_assistant_key_is_classified():
    """Every static key of the assistant (and H6) is either in the allowed list or refused as
    unmeasurable / closed: a new key cannot slip through unclassified."""
    for k in KEYS:
        if k.dim == "H1" or not (k.dim == "H6" or k.name.split(".")[1] == "assistant"):
            continue
        sample = False if k.kind == "bool" else k.lo if k.kind == "int" else None
        got = r(KeyOp(k.name, sample))
        if k.name in ASSISTANT_MEASURED or (k.dim == "H3" and sample is False):
            assert got is None, k.name
        else:
            assert got is not None, k.name


@pytest.mark.parametrize("op", [
    KeyOp("H3.assistant.web_search", True),            # switching a built-in tool on
    KeyOp("H3.assistant.web_search", None),            # back to the default = on
])
def test_switching_an_assistant_tool_on_is_refused(op):
    got = r(op)
    assert got is not None and "不能自动打开" in got


@pytest.mark.parametrize("op", [
    KeyOp("H3.assistant.a_tool", True),
    KeyOp("H3.assistant.a_tool", False),
    KeyOp("H12.tool.assistant.a_tool", {"candidate_id": "fc_a"}),
    KeyOp("H2.assistant.a_tool", "说明"),               # no default in the code: not measurable
    KeyOp("H2.assistant.a_tool.query", "说明"),
])
def test_forged_assistant_tools_stay_closed(op):
    assert CLOSED in r(op)


def test_h6_is_refused_on_assistant_grounds_not_as_an_out_of_range_dim():
    got = r(KeyOp("H6.keep_turns", 10))
    assert UNMEASURABLE in got and "不在可以自动改的范围内" not in got


def test_bot_keys_stay_closed():
    assert CLOSED in r(KeyOp("H4.bot.max_tool_calls", 3))
    assert CLOSED in r(KeyOp("H2.bot.suggest", "x"))


def test_h4_assistant_raise_limit_applies():
    # the rule, not today's default (20 sits at the key's upper bound): default 6 -> at most 9
    got = refuse(PackDiff((KeyOp("H4.assistant.max_tool_calls", 10),)), HarnessPack(0, {}), {},
                 defaults={**D, "H4.assistant.max_tool_calls": 6})
    assert "倍" in got


def test_user_set_assistant_key_is_never_touched():
    got = r(KeyOp("H2.assistant.search_wiki", "x"), sources={"H2.assistant.search_wiki": "user"})
    assert "你自己设的" in got


def test_assistant_h2_args_all_validate():
    for tool, targs in TOOLS["assistant"].items():
        assert r(KeyOp(f"H2.assistant.{tool}", "说明")) is None
        for a in targs:
            assert r(KeyOp(f"H2.assistant.{tool}.{a}", "说明")) is None


def test_approval_is_unchanged_for_assistant_structural_keys():
    for t in TOGGLE_TOOLS["assistant"]:
        assert needs_approval(f"H3.assistant.{t}")
    assert needs_approval("H9.assistant.before_answer")
    assert needs_approval("H10.assistant.turn.tier")
    assert needs_approval("H10.assistant.turn.thinking")
    assert needs_ask(PackDiff((KeyOp("H3.assistant.web_search", False),)))
    assert not needs_ask(PackDiff((KeyOp("H2.assistant.search_wiki", "x"),)))
    assert key_of("H4.assistant.max_tool_calls").approval == "auto"


@pytest.mark.parametrize("tool", ["list_sources", "read_source", "search_source"])
def test_the_materials_tools_are_builtin_and_open_under_r1(tool):
    """MF4: the assistant's materials tools are built-ins, so their H2 texts and their switch-off
    are open (R1), and switching them on stays refused."""
    assert tool in TOOLS["assistant"] and tool in TOGGLE_TOOLS["assistant"]
    assert r(KeyOp(f"H2.assistant.{tool}", "列出资料")) is None
    for arg in TOOLS["assistant"][tool]:
        assert r(KeyOp(f"H2.assistant.{tool}.{arg}", "说明")) is None
    assert r(KeyOp(f"H3.assistant.{tool}", False)) is None
    assert r(KeyOp(f"H3.assistant.{tool}", True)) is not None


def test_agents_touched_maps_each_op_to_the_cases_it_can_change():
    """#11: which case kinds the final probe replays for a diff."""
    from evowiki.harness.diff import agents_touched
    from evowiki.teach.contracts import SpecPatchOp

    def t(*ops, spec=()):
        return agents_touched(PackDiff(tuple(ops), tuple(spec)))
    deep, asst, both = frozenset({"deep"}), frozenset({"assistant"}), frozenset({"deep", "assistant"})
    assert t(KeyOp("H2.assistant.read_k", "x"), KeyOp("H3.assistant.web_search", False)) == asst
    assert t(KeyOp("H1.assistant", "x")) == asst and t(KeyOp("H1.compact", "x")) == asst
    assert t(KeyOp("H1.write", "x")) == deep and t(KeyOp("H4.deep.max_tool_calls", 90)) == deep
    assert t(KeyOp("H6.keep_budget", 2000)) == deep
    assert t(KeyOp("H8.component.c1.note", "x")) == deep
    assert t(KeyOp("H12.tool.deep.t1", {"candidate_id": "fc"})) == deep
    spec = SpecPatchOp.model_validate({"op": "add_item", "item_id": "i1", "scope": "any",
                                       "text": "先举例"})
    assert t(spec=(spec,)) == deep
    assert t(KeyOp("H1.write", "x"), KeyOp("H2.assistant.read_k", "y")) == both
    # 列大纲 is the writer's since stage 4 (its guidance sits in deep.write's first message)
    assert t(KeyOp("H1.outline", "x")) == deep
    assert t(KeyOp("H1.path", "x")) == both             # neither replay: replay everything
