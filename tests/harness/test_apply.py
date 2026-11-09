import pytest
from pydantic import BaseModel, Field

from evowiki.agent.compact import CompactPolicy
from evowiki.agent.spec import ElidePolicy, LoopLimits
from evowiki.agent.tool_helpers import spec as args_spec
from evowiki.agent.tools import ToolResult, ToolSpec
from evowiki.harness import apply
from evowiki.harness.apply import AssistantContext
from evowiki.harness.pack import EMPTY, HarnessInvalid, HarnessPack

BASE = LoopLimits(max_tool_calls=80, on_cap="stop", text_is_answer=False, max_nudges=2,
                  nudge_text="继续", max_truncated=1, truncated_text="少写", tool_error_max=2000)
EL = ElidePolicy(read_tools=frozenset({"read_page"}), write_tools=frozenset(), keep_turns=3,
                 stub_over=400, read_stub="r", write_stub="w")


class _T:
    def __init__(self):
        self.spec = ToolSpec(name="read_page", description="读页面", effect="read",
                             parameters={"type": "object", "properties": {
                                 "page_id": {"type": "string", "description": "页面 id"}}})

    def run(self, args, ctx):
        return ToolResult(content="ok")


def test_empty_pack_is_identity():
    assert apply.limits(EMPTY, "deep", BASE) is BASE
    assert apply.elide(EMPTY, "deep", EL) is EL
    p = CompactPolicy()
    assert apply.compact_policy(EMPTY, p) is p
    ctx = AssistantContext(1, 2, 3, 4)
    assert apply.assistant_context(EMPTY, ctx) is ctx
    assert apply.max_rewrites(EMPTY, 3) == 3
    tools = (_T(),)
    assert apply.describe(EMPTY, "deep", tools) is tools


def test_limits_replaced_only_where_set():
    out = apply.limits(HarnessPack(1, {"H4.deep.max_tool_calls": 100}), "deep", BASE)
    assert out.max_tool_calls == 100
    assert (out.max_nudges, out.nudge_text, out.on_cap) == (2, "继续", "stop")


def test_elide_assistant_keys():
    out = apply.elide(HarnessPack(1, {"H5.assistant.read_keep_calls": 5}), "assistant", EL)
    assert out.keep_turns == 5 and out.stub_over == 400


def test_assistant_context_replaces_one_key():
    base = AssistantContext(1000, 200, 300, 4)
    out = apply.assistant_context(HarnessPack(1, {"H5.assistant.top_k_pages": 6}), base)
    assert out == AssistantContext(1000, 200, 300, 6)


def test_max_rewrites_from_pack():
    assert apply.max_rewrites(HarnessPack(1, {"H4.deep.max_rewrites": 2}), 3) == 2


def test_incoherent_compaction_refused():
    p = HarnessPack(1, {"H6.keep_budget": 50000})          # > history_budget 20000
    with pytest.raises(HarnessInvalid):
        apply.compact_policy(p, CompactPolicy())


def test_describe_replaces_text_only():
    t = _T()
    p = HarnessPack(1, {"H2.deep.read_page": "读一页", "H2.deep.read_page.page_id": "要读的页"})
    (w,) = apply.describe(p, "deep", (t,))
    assert w.spec.description == "读一页"
    assert w.spec.parameters["properties"]["page_id"] == {"type": "string",
                                                          "description": "要读的页"}
    assert t.spec.description == "读页面"                    # original untouched
    assert w.run({}, None).content == "ok"


class _Inner(BaseModel):
    x: int = 0


class _Args(BaseModel):          # field names: arguments place_component has (harness/tool_names)
    why: str = Field(description="原文")
    config: _Inner = Field(default_factory=_Inner, description="嵌套")
    placement_id: str | None = Field(default=None, description="可省略")


class _Real:
    spec = args_spec("place_component", "真工具", _Args, "read", "draft")

    def run(self, args, ctx):
        return ToolResult(content="ok")


def test_describe_on_pydantic_schema_with_defs():
    """Model-built parameters keep top-level args under `properties` (nested models go to
    `$defs`); a description is set on the property whether it is a $ref or an anyOf."""
    assert "$defs" in _Real.spec.parameters
    p = HarnessPack(1, {"H2.deep.place_component.config": "新嵌套",
                        "H2.deep.place_component.placement_id": "新可选",
                        "H2.deep.place_component.why": "新原文"})
    (w,) = apply.describe(p, "deep", (_Real(),))
    props = w.spec.parameters["properties"]
    assert [props[k]["description"] for k in ("why", "config", "placement_id")] == [
        "新原文", "新嵌套", "新可选"]
    assert "$defs" in w.spec.parameters
    assert _Real.spec.parameters["properties"]["why"]["description"] == "原文"


def test_describe_skips_a_tool_this_run_does_not_offer():
    """place_component exists only with components: its key does not fail a run without it."""
    t = _T()
    p = HarnessPack(1, {"H2.deep.place_component": "放组件", "H2.deep.read_page": "读一页"})
    (w,) = apply.describe(p, "deep", (t,))
    assert w.spec.description == "读一页"
    assert apply.describe(HarnessPack(1, {"H2.deep.place_component": "放组件"}), "deep",
                          (t,)) == (t,)


def test_describe_unknown_arg_of_an_offered_tool_refused():
    # section_id is a registered read_page argument, but this run's read_page has no such argument
    with pytest.raises(HarnessInvalid, match="这个工具没有这个参数"):
        apply.describe(HarnessPack(1, {"H2.deep.read_page.section_id": "x"}), "deep", (_T(),))


def test_describe_name_outside_the_registry_refused():
    # a name no forged tool can have (the forge's reserved call_tool; an upper-case letter)
    with pytest.raises(HarnessInvalid, match="没有这个设置"):
        apply.describe(HarnessPack(1, {"H2.deep.call_tool": "x"}), "deep", (_T(),))
    with pytest.raises(HarnessInvalid, match="没有这个设置"):
        apply.describe(HarnessPack(1, {"H2.deep.X": "x"}), "deep", (_T(),))
    with pytest.raises(HarnessInvalid, match="没有这个设置"):
        apply.describe(HarnessPack(1, {"H2.deep.read_page.nope": "x"}), "deep", (_T(),))


def test_describe_a_possible_forged_tool_not_offered_is_skipped():
    """A pattern-valid name outside the built-ins may be a forged tool (P3): accepted, and skipped
    where it is applied when this run does not offer it."""
    t = _T()
    assert apply.describe(HarnessPack(1, {"H2.deep.nope": "x"}), "deep", (t,)) == (t,)


class _Pausing(_T):
    def __init__(self):
        super().__init__()
        self.spec = self.spec.model_copy(update={"name": "ask_user", "pauses": True})

    def resume(self, args, ctx, pause, reply):
        return ToolResult(content=f"答：{reply}")


def test_describe_keeps_a_pausing_tools_resume():
    # constraint 5 (2026-10-28): rewording ask_user must not take away its wait for the reply
    (w,) = apply.describe(HarnessPack(1, {"H2.assistant.ask_user": "问一句"}), "assistant",
                          (_Pausing(),))
    assert w.spec.description == "问一句" and w.spec.pauses
    assert hasattr(w, "resume") and w.resume({}, None, None, "好").content == "答：好"


def _contract(spec: ToolSpec) -> dict:
    """The spec without its H2-editable texts (the tool's and each argument's description)."""
    d = spec.model_dump()
    d.pop("description")
    for schema in (d["parameters"].get("properties") or {}).values():
        schema.pop("description", None)
    return d


def test_h2_texts_never_change_a_builtin_tools_contract():
    # every H2 key of every built-in assistant tool and argument set at once: only the texts move
    from evowiki.harness.improver import assistant_tool_texts
    from evowiki.harness.replay_assistant import builtin_specs

    class _S:
        def __init__(self, spec):
            self.spec = spec

        def run(self, args, ctx):
            return ToolResult(content="ok")

    specs = builtin_specs()
    pack = HarnessPack(1, {k: "改过的说明" for k in assistant_tool_texts()})
    out = apply.describe(pack, "assistant", tuple(_S(s) for s in specs.values()))
    assert len(out) == len(specs)
    for w in out:
        assert _contract(w.spec) == _contract(specs[w.spec.name])
        assert w.spec.description == "改过的说明"
