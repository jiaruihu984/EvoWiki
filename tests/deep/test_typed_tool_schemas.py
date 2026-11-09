"""The writer's typed tool schemas (fix/writer-typed-configs, live run 2026-10-28 on pg_nothink_0002):
place_component's `config` is each library component's own config_schema (as JSON Schema, a branch
per component titled with its id), finish's `self_check[].rule` an enum of the run's rule ids. No
LLM: the tool schemas are checked with a JSON Schema validator, the tools run on a workspace."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from evowiki.deep.components import ConfigInvalid, validate_config
from evowiki.deep.contracts import LibraryComponent
from evowiki.deep.library_seed import load_seed
from evowiki.deep.tools import FinishTool, PlaceComponentTool, Workspace, check_rules
from evowiki.llm.providers import deepseek_strict_schema
from evowiki.teach.contracts import TeachingSpec
from evowiki.teach.default_spec import default_spec

SEEDS = load_seed()
LIB = {c.component_id: LibraryComponent.model_construct(**c.model_dump()) for c in SEEDS}
CONFIGURABLE = [c for c in SEEDS if c.config_schema is not None]


def place_schema() -> dict:
    ws = Workspace(None, components=LIB)
    return PlaceComponentTool(ws).spec.parameters


def args(component_id: str, config) -> dict:
    return {"component_id": component_id, "section_id": "s4", "why": "动手看", "config": config}


def schema_errors(schema: dict, value) -> list[str]:
    return [e.message for e in Draft202012Validator(schema).iter_errors(value)]


def test_the_tool_schemas_are_valid_json_schema():
    Draft202012Validator.check_schema(place_schema())
    Draft202012Validator.check_schema(FinishTool(Workspace(None), "p", None,
                                                 check_rules(default_spec())).spec.parameters)


def test_component_id_is_the_librarys_enum_and_each_config_branch_is_titled_with_its_id():
    props = place_schema()["properties"]
    assert props["component_id"]["enum"] == list(LIB)
    titles = [b.get("title") for b in props["config"]["anyOf"]]
    assert titles == [None, *(c.component_id for c in CONFIGURABLE)]
    assert props["config"]["anyOf"][0] == {"type": "null"}
    assert "形状" not in json.dumps(props, ensure_ascii=False)


@pytest.mark.parametrize("c", CONFIGURABLE, ids=lambda c: c.component_id)
def test_every_components_examples_fit_its_tool_schema(c):
    """Per component: the tool schema accepts every example config, and so does the placement
    validation (`validate_config`) — the two agree."""
    schema = place_schema()
    assert len(c.example_configs) >= 2
    for cfg in c.example_configs:
        assert schema_errors(schema, args(c.component_id, cfg)) == []
        assert validate_config(c.config_schema, cfg) == cfg


def test_a_fixed_content_component_takes_null():
    fixed = next(c for c in SEEDS if c.config_schema is None)
    assert schema_errors(place_schema(), args(fixed.component_id, None)) == []


# the live run's scatter-line configs (llm_call rows of subject pg_nothink_0002): the model read
# `notes: ? or {...}` as "notes is an optional map" and wrote partial objects / an empty list
SCATTER = next(c for c in SEEDS if c.component_id == "scatter-line").example_configs[1]
REJECTED = {
    "partial notes": {"presets": [{"x": 182, "y": 70, "label": "U"}],
                      "notes": {"direction": "转到斜方向投影最散；转到垂直方向投影最挤。",
                                "correlation": "这些点沿斜线铺开，身高体重高度相关。"}},
    "empty presets, partial notes": {"presets": [],
                                     "notes": {"direction": "把方向线转到斜方向，看投影铺得多散。"}},
    "presets and notes left out": {"presets": None, "notes": None, "drop": ("presets", "notes")},
}


@pytest.mark.parametrize("name", REJECTED)
def test_the_live_runs_rejected_configs_are_outside_the_tool_schema(name):
    """Each config the run sent and had refused is also a schema error now: the schema the model reads
    says exactly what the placement validation checks (all four notes keys, null or 1+ presets)."""
    change = dict(REJECTED[name])
    cfg = {**copy.deepcopy(SCATTER), **{k: v for k, v in change.items() if k != "drop"}}
    for k in change.get("drop", ()):
        cfg.pop(k)
    assert schema_errors(place_schema(), args("scatter-line", cfg))
    with pytest.raises(ConfigInvalid):
        validate_config(LIB["scatter-line"].config_schema, cfg)


def test_the_live_runs_accepted_config_fits_the_schema():
    cfg = {**copy.deepcopy(SCATTER), "presets": None, "notes": None}
    assert schema_errors(place_schema(), args("scatter-line", cfg)) == []
    full = {**cfg, "notes": {"fit": None, "direction": "转到斜方向投影最散。", "correlation": None,
                             "covariance": None}}
    assert schema_errors(place_schema(), args("scatter-line", full)) == []


def test_an_empty_workspace_keeps_the_plain_args():
    """The harness builds the tools empty for their H2 texts (`improver.deep_tool_texts`)."""
    props = PlaceComponentTool(None).spec.parameters["properties"]     # type: ignore[arg-type]
    assert "enum" not in props["component_id"] and props["config"]["description"]


# ---- finish ----

def finish_tool(rules) -> FinishTool:
    return FinishTool(Workspace(None), "p", None, rules)


def rule_node(tool: FinishTool) -> dict:
    return tool.spec.parameters["$defs"]["SelfCheckArgs"]["properties"]["rule"]


def test_finish_rule_is_an_enum_of_the_runs_rules():
    rules = check_rules(default_spec())
    assert rule_node(finish_tool(rules))["enum"] == list(rules)


def test_the_enum_follows_the_spec():
    """Built from `check_rules(teaching)`, never a fixed list: a spec with other items gets them."""
    base = default_spec()
    item = base.items[0].model_copy(update={"item_id": "g_mine", "text": "我的规则"})
    spec = TeachingSpec(version=base.version + 1, items=(item,))
    enum = rule_node(finish_tool(check_rules(spec)))["enum"]
    assert "g_mine" in enum and base.items[1].item_id not in enum


def test_a_wrong_rule_id_is_a_schema_error():
    rules = check_rules(default_spec())
    schema = finish_tool(rules).spec.parameters
    ok = {"self_check": [{"rule": r, "ok": True} for r in rules]}
    assert schema_errors(schema, ok) == []
    mixed = {"self_check": [{"rule": "c01/d01", "ok": True}]}      # the live run's first try
    assert schema_errors(schema, mixed)


# ---- providers ----

def test_deepseek_strict_mode_can_express_both_tool_schemas():
    """The chat tools are sent non-strict on both providers; this keeps strict open: the schemas
    have no open map or recursive ref."""
    deepseek_strict_schema(place_schema())
    deepseek_strict_schema(finish_tool(check_rules(default_spec())).spec.parameters)


def test_place_component_refuses_a_graded_config_that_does_not_name_this_pages_cells():
    from evowiki.deep.library_seed import REPO_ROOT, load_component, seed_html
    from tests.deep.fakes import component
    c = load_component(REPO_ROOT / "src/evowiki/deep/components/sort-match.json")
    comp = component("sort-match").model_copy(update={"config_schema": c.config_schema, "html": seed_html(c)})
    ws = Workspace(None, page_cells=("c1.term",))
    ws.components = {"sort-match": comp}
    ws.unplanned = lambda: False
    config = c.example_configs[0]
    res = PlaceComponentTool(ws).run({"component_id": "sort-match", "section_id": "s1", "why": "y", "config": config}, None)
    assert "没有写 cell" in res.content
