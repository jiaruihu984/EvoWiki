"""Configurable library components (B5, component-library): schema subset, config validation, injection.

A configurable component is one generic interaction whose content comes from a config. Its
`config_schema` must stay inside the JSON-Schema subset Anthropic structured outputs accept
(`check_schema`), so the same schema can be handed to a model as-is. Every object in it is closed
(`additionalProperties: false`) and lists all of its properties in `required` (optional values are
written as a `null` union), as structured outputs require.

`validate_config` checks a config against a schema plus generic size caps (the subset has no length or
numeric bounds, so components clamp numbers themselves). `instantiate` puts the config into the
component HTML as `<script type="application/json" id="evowiki-config">` and resolves `{{config.path}}`
references in the component's probes. Pure: no IO."""

import json
import math
import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from evowiki.deep.contracts import CONFIG_ELEMENT_ID, LibraryComponent, Probe
from evowiki.i18n import current_language

ALLOWED_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum",
                          "const", "description", "title", "anyOf", "minItems"})
TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
MAX_STRING = 600            # chars of any one string in a config
MAX_ARRAY = 60              # items of any one array
MAX_CONFIG_BYTES = 24_000   # the whole config as JSON
REQUIRED_TOP = ("title", "question")      # every configurable component shows these
_REF = re.compile(r"\{\{config\.([^{}]+)\}\}")


class SchemaError(ValueError):
    """A config schema outside the supported subset."""


class ConfigInvalid(ValueError):
    """A config that does not fit its component's schema (the component is not placed)."""

    def __init__(self, errors: Sequence[str]):
        super().__init__("; ".join(errors[:8]))
        self.errors = tuple(errors)


# ---- schema subset ----

def _types(node: Mapping[str, Any]) -> tuple[str, ...]:
    t = node.get("type")
    return (t,) if isinstance(t, str) else tuple(t) if isinstance(t, list) else ()


def check_schema(schema: Mapping[str, Any]) -> None:
    """SchemaError unless `schema` is in the supported subset and its top level is an object with
    the string properties `title` and `question`."""
    problems: list[str] = []

    def visit(node: Any, where: str) -> None:
        if not isinstance(node, dict):
            problems.append(f"{where}: not an object")
            return
        if bad := sorted(set(node) - ALLOWED_KEYS):
            problems.append(f"{where}: unsupported keys {bad}")
        if "anyOf" in node:
            for i, sub in enumerate(node["anyOf"]):
                visit(sub, f"{where}.anyOf[{i}]")
            return
        types = _types(node)
        if not types and "enum" not in node and "const" not in node:
            problems.append(f"{where}: no type")
        if set(types) - TYPES:
            problems.append(f"{where}: unknown type {types}")
        if node.get("minItems", 0) not in (0, 1):
            problems.append(f"{where}: minItems may only be 0 or 1")
        if "object" in types:
            props = node.get("properties")
            if not isinstance(props, dict) or not props:
                problems.append(f"{where}: object without properties")
                return
            if node.get("additionalProperties") is not False:
                problems.append(f"{where}: objects must set additionalProperties: false")
            if sorted(node.get("required", [])) != sorted(props):
                problems.append(f"{where}: every property must be required (use a null union)")
            for k, v in props.items():
                visit(v, f"{where}.{k}")
        if "array" in types:
            if "items" not in node:
                problems.append(f"{where}: array without items")
            else:
                visit(node["items"], f"{where}[]")

    visit(schema, "$")
    if not problems:
        props = schema.get("properties", {})
        if _types(schema) != ("object",):
            problems.append("$: the top level must be an object")
        for k in REQUIRED_TOP:
            if _types(props.get(k, {})) != ("string",):
                problems.append(f"$: needs a string property {k!r}")
    if problems:
        raise SchemaError("; ".join(problems))


# ---- validation ----

def _is(t: str, v: Any) -> bool:
    match t:
        case "object":
            return isinstance(v, dict)
        case "array":
            return isinstance(v, list)
        case "string":
            return isinstance(v, str)
        case "integer":
            return isinstance(v, int) and not isinstance(v, bool)
        case "number":
            return isinstance(v, int | float) and not isinstance(v, bool)
        case "boolean":
            return isinstance(v, bool)
        case "null":
            return v is None
    return False


def _errors(node: Mapping[str, Any], v: Any, where: str) -> list[str]:
    if "anyOf" in node:
        options = [_errors(sub, v, where) for sub in node["anyOf"]]
        return [] if any(not o for o in options) else [f"{where}: matches none of anyOf"]
    if "const" in node and v != node["const"]:
        return [f"{where}: must be {node['const']!r}"]
    if "enum" in node and v not in node["enum"]:
        return [f"{where}: must be one of {node['enum']}"]
    types = _types(node)
    if types and not any(_is(t, v) for t in types):
        return [f"{where}: expected {'/'.join(types)}"]
    out: list[str] = []
    if isinstance(v, str) and len(v) > MAX_STRING:
        out.append(f"{where}: longer than {MAX_STRING} characters")
    if isinstance(v, float) and not math.isfinite(v):
        out.append(f"{where}: not a number")
    if isinstance(v, list):
        if len(v) < node.get("minItems", 0):
            out.append(f"{where}: needs at least {node['minItems']} item")
        if len(v) > MAX_ARRAY:
            out.append(f"{where}: more than {MAX_ARRAY} items")
        for i, x in enumerate(v[:MAX_ARRAY]):
            out += _errors(node.get("items", {}), x, f"{where}[{i}]")
    if isinstance(v, dict) and "object" in types:
        props = node.get("properties", {})
        out += [f"{where}: unknown key {k!r}" for k in v if k not in props]
        out += [f"{where}: missing {k!r}" for k in node.get("required", []) if k not in v]
        for k, sub in props.items():
            if k in v:
                out += _errors(sub, v[k], f"{where}.{k}")
    return out


def validate_config(schema: Mapping[str, Any], config: Any) -> dict[str, Any]:
    """`config` as a dict when it fits `schema` and the size caps; ConfigInvalid otherwise."""
    errors = _errors(schema, config, "config")
    if not errors and len(json.dumps(config, ensure_ascii=False)) > MAX_CONFIG_BYTES:
        errors.append(f"config: larger than {MAX_CONFIG_BYTES} bytes")
    if not errors:
        for k in REQUIRED_TOP:
            if not str(config.get(k, "")).strip():
                errors.append(f"config.{k}: must not be empty")
    if errors:
        raise ConfigInvalid(errors)
    return dict(config)


# ---- graded components ----

def graded_config(html: str) -> dict[str, Any] | None:
    """The config a placed component carries, when it holds an answer key (`targets` with ids, `items` each
    with an `id` and an `answer`: the server grades the reader's placements against it,
    `wikiapp.block_results`); None for a component without right and wrong, or HTML without a config."""
    m = re.search(r'<script type="application/json" id="' + CONFIG_ELEMENT_ID + r'">(.*?)</script>',
                  html, flags=re.DOTALL)
    if m is None:
        return None
    try:
        config = json.loads(m.group(1))
    except ValueError:
        return None
    return config if _answer_key(config) else None


def _answer_key(config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    targets, items = config.get("targets"), config.get("items")
    return (isinstance(targets, list) and isinstance(items, list) and bool(items)
            and all(isinstance(t, dict) and isinstance(t.get("id"), str) for t in targets)
            and all(isinstance(i, dict) and isinstance(i.get("id"), str) and isinstance(i.get("answer"), str)
                    for i in items))


def cell_problems(config: Any, page_cells: Collection[str]) -> list[str]:
    """What is wrong with a graded config's `items[].cell` on a page with these K cells: a reader's answer
    becomes K evidence only through its item's cell, so with cells to name every item names one of them.
    [] for a component without an answer key, and on a page without cells."""
    if not page_cells or not _answer_key(config):
        return []
    items = config["items"]
    if foreign := sorted({i["cell"] for i in items if i.get("cell") and i["cell"] not in page_cells}):
        return [f"items[].cell 只能用这一页的格子（{', '.join(sorted(page_cells))}），不能用 {', '.join(foreign)}"]
    if empty := [i["id"] for i in items if not i.get("cell")]:
        return [f"items 里 {', '.join(empty)} 没有写 cell：每一项写它考的格子（{', '.join(sorted(page_cells))}）"]
    return []


# ---- placing ----

def config_script(config: Mapping[str, Any]) -> str:
    """The injected element. Every `<` is written as the JSON escape `\\u003c`, so no `</script>` or
    `<!--` inside a config string can end or open anything in the document."""
    text = json.dumps(config, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/json" id="{CONFIG_ELEMENT_ID}">{text}</script>'


# The page's height handshake (the plugin's embed-block frameHeightOf): the frame fits the component,
# not the height the writer guessed (a live page had 230 px of empty frame under the component).
FRAME_HEIGHT = ("<script>new ResizeObserver(function(){parent.postMessage({evowikiFrameHeight:"
                "Math.ceil(document.body.getBoundingClientRect().height)},\"*\")})"
                ".observe(document.body)</script>")


def inject(html: str, config: Mapping[str, Any]) -> str:
    """`html` with the config element placed before its first `<script` (so the component's own
    script can read it), else before `</body>`, else at the end. A config element the component
    already carries (its built-in default) is replaced."""
    tag = config_script(config)
    html = re.sub(r'<script type="application/json" id="' + CONFIG_ELEMENT_ID + r'">.*?</script>',
                  "", html, count=1, flags=re.DOTALL)
    html = html.replace("</body>", FRAME_HEIGHT + "</body>", 1)
    for marker in ("<script", "</body>"):
        i = html.find(marker)
        if i >= 0:
            return html[:i] + tag + html[i:]
    return html + tag


def _lookup(config: Any, path: str) -> Any:
    cur = config
    for part in re.findall(r"[^.\[\]]+", path):
        if isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise ConfigInvalid([f"probe refers to config.{path}, which is not in the config"])
    return cur


def resolve_probes(probes: Sequence[Probe], config: Any) -> tuple[Probe, ...]:
    """Probes with `{{config.a.b[0]}}` replaced by the config's value (ConfigInvalid if missing)."""
    def fill(s: str | None) -> str | None:
        return None if s is None else _REF.sub(lambda m: str(_lookup(config, m.group(1))), s)
    return tuple(p.model_copy(update={"target": fill(p.target), "value": fill(p.value),
                                      "expect_change": fill(p.expect_change)}) for p in probes)


def instantiate(c: LibraryComponent, config: Mapping[str, Any] | None, *, language: str | None = None
                ) -> tuple[str, tuple[Probe, ...], str, str]:
    """(html, probes, title, learning_question) of `c` placed with `config`. A fixed-content
    component takes no config (ConfigInvalid if one is given); a configurable one needs a valid one."""
    if c.config_schema is None:
        if config:
            raise ConfigInvalid([f"{c.component_id} is a fixed-content component; give no config"])
        return localized_html(c.html, language or current_language()), c.probes, c.name, c.learning_question
    if config is None:
        raise ConfigInvalid([f"{c.component_id} needs a config"])
    cfg = validate_config(c.config_schema, config)
    return (localized_html(inject(c.html, cfg), language or current_language()), resolve_probes(c.probes, cfg), cfg["title"][:120],
            cfg["question"][:300])



def localized_html(html: str, language: str) -> str:
    """Bind explicit locale only on our managed seed artifacts; never inject into outside pages."""
    if "data-evowiki-locale=" not in html:
        return html
    lang = "en" if language == "en" else "zh"
    return re.sub(r'(<html\b[^>]*\blang=)["\'][^"\']*["\']',
                  lambda m: m.group(1) + '"' + lang + '"', html, count=1, flags=re.IGNORECASE)
