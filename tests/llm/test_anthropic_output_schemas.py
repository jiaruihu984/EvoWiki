"""Every structured-output schema the product sends through the gateway must be one Anthropic
structured outputs accept (`output_config.format`, after `providers.strict_schema`): every object
node closed (`additionalProperties: false`, with `properties`), no other `additionalProperties`
value, no recursive `$ref`, every property typed. Live 2026-10-20: the Gate judge's
`PairOut.missing_prereq: dict[...]` (an object whose additionalProperties is a schema) was an
HTTP 400 and the whole RSI-B round failed.

The schemas are found by scanning the source: every BaseModel class passed to a call whose first
argument is the model role ("generator" / "verifier" / a `role` variable), i.e.
`gateway.call(role, request, Schema)`, `MeteredLLM.call(...)` and the `JsonLLM` wrappers
`llm(role, purpose, prompt, Schema)`. Schemas built at run time are listed in `_BUILT`."""

import ast
import importlib
import pathlib

from pydantic import BaseModel

from evowiki.llm.providers import _typeless, strict_schema

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
ROLES = ("generator", "verifier")


def _built() -> list[type[BaseModel]]:
    """Schemas built at run time, over the real seed library: the understanding's reading (component enum)
    and every configurable component's own config call."""
    from evowiki.deep.library_seed import load_seed
    from evowiki.wikiapp import component_fill, understand
    seeds = [c for c in load_seed() if c.config_schema]
    ctx = understand.Context(page_id="p", title="t", sections=(), concepts={}, bands=(), rules=(),
                             components={c.component_id: c.name for c in seeds},
                             schemas={c.component_id: c.config_schema for c in seeds})
    return [understand.reply_schema(ctx),
            *(component_fill.reply_schema(c.component_id, c.config_schema) for c in seeds)]


def _is_role(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in ROLES
    if isinstance(node, ast.Name):
        return node.id == "role"
    return isinstance(node, ast.Attribute) and node.attr.lstrip("_") == "role"


def output_schemas() -> dict[str, type[BaseModel]]:
    found: dict[str, type[BaseModel]] = {}
    for path in sorted((SRC / "evowiki").rglob("*.py")):
        parts = path.relative_to(SRC).with_suffix("").parts
        if parts[-1] == "__main__":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and n.args and _is_role(n.args[0])]
        if not calls:
            continue
        mod = importlib.import_module(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
        for call in calls:
            for arg in [*call.args[1:], *(k.value for k in call.keywords)]:
                obj = getattr(mod, arg.id, None) if isinstance(arg, ast.Name) else None
                if isinstance(obj, type) and issubclass(obj, BaseModel):
                    found[f"{obj.__module__}.{obj.__qualname__}"] = obj
    for m in _built():
        found[f"{m.__module__}.{m.__qualname__} (built)"] = m
    return found


def problems(schema: dict) -> list[str]:
    out: list[str] = []

    def visit(node, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                if node.get("additionalProperties") is not False:
                    out.append(f"{path}: object without additionalProperties: false")
                if not isinstance(node.get("properties"), dict):
                    out.append(f"{path}: object without properties")
                else:
                    out.extend(f"{path}/properties/{k}: untyped" for k, v in node["properties"].items()
                               if isinstance(v, dict) and _typeless(v))
            elif "additionalProperties" in node:
                out.append(f"{path}: additionalProperties outside an object")
            for k, v in node.items():
                visit(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                visit(v, f"{path}[{i}]")

    visit(schema, "")
    defs = schema.get("$defs", {})

    def refs(node) -> set[str]:
        if isinstance(node, dict):
            r = {node["$ref"].rsplit("/", 1)[-1]} if isinstance(node.get("$ref"), str) else set()
            return r.union(*(refs(v) for v in node.values()))
        if isinstance(node, list):
            return set().union(*(refs(v) for v in node))
        return set()

    graph = {name: refs(d) for name, d in defs.items()}

    def cyclic(name: str, seen: tuple[str, ...]) -> bool:
        return name in seen or any(cyclic(n, (*seen, name)) for n in graph.get(name, ()))

    out.extend(f"$defs/{n}: recursive" for n in defs if cyclic(n, ()))
    return out


def test_the_scan_finds_the_known_output_schemas():
    names = set(output_schemas())
    for known in ("evowiki.wikiapp.understand.UnderstandReply (built)",
                  "evowiki.goals.blocks._BlocksOut", "evowiki.goals.path.DraftPath",
                  "evowiki.wikiapp.graph_groups.GroupNamesReply"):
        assert known in names, known
    assert len(names) >= 40


def test_every_output_schema_is_accepted_by_anthropic_structured_outputs():
    bad = {name: p for name, m in output_schemas().items()
           if (p := problems(strict_schema(m.model_json_schema())))}
    assert not bad, bad


MAX_UNIONS = 16       # Anthropic: at most 16 union-typed parameters (anyOf / type arrays) per request


def unions(schema: dict) -> int:
    n = 0

    def visit(node) -> None:
        nonlocal n
        if isinstance(node, dict):
            if "anyOf" in node or isinstance(node.get("type"), list):
                n += 1
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)
    visit(schema)
    return n


def test_every_output_schema_has_at_most_16_union_typed_parameters():
    """feedback-understand@v11 put the whole library's configs in one reading (60 unions): a 400 on Anthropic.
    A component's config is a call of its own now (wikiapp.component_fill)."""
    over = {name: n for name, m in output_schemas().items()
            if (n := unions(strict_schema(m.model_json_schema()))) > MAX_UNIONS}
    assert not over, over


def test_the_check_catches_an_open_map_and_recursion():
    class Open(BaseModel):
        counts: dict[str, int]

    class Node(BaseModel):
        children: list["Node"] = []

    assert problems(strict_schema(Open.model_json_schema()))
    assert any("recursive" in p for p in problems(strict_schema(Node.model_json_schema())))
