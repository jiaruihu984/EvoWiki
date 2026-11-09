"""The interactive component library (B5; spec the product specification §4.1).

A global catalogue of interactives that already passed the render check. Deep refine and the feedback
understanding call may only *place* a component from here by id — they never write free HTML. A
component enters the library only through `register`, which runs the render check itself and refuses
(`ComponentRejected`) anything that does not pass; `LibraryComponent` itself also refuses a failing
check, so a failing component cannot be stored by any route.

A configurable component (`config_schema` set) is registered only when its schema is in the supported
subset and every example config validates and passes the render check on component + config
(`deep.components`). `catalog_text` is the listing given to prompts: one line per component with what
it fits and, for a configurable one, the shape of its config."""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from evowiki.deep.components import check_schema, instantiate
from evowiki.deep.contracts import (
    InteractiveDraft,
    LibraryComponent,
    Probe,
    RenderCheck,
    RenderChecker,
)
from evowiki.harness.steps import Preset
from evowiki.i18n import current_language, tr

LIBRARY_SECTION = "library"       # InteractiveDraft needs a section id; a library component has none


class ComponentLibrary(Protocol):
    def list(self) -> list[LibraryComponent]: ...

    def get(self, component_id: str) -> LibraryComponent | None: ...

    def put(self, component: LibraryComponent) -> None:
        """Adds or replaces (same id) a component."""
        ...


class MemoryLibrary:
    def __init__(self, components: Sequence[LibraryComponent] = ()):
        self.rows: dict[str, LibraryComponent] = {}
        for c in components:
            self.put(c)

    def list(self) -> list[LibraryComponent]:
        return [self.rows[k] for k in sorted(self.rows)]

    def get(self, component_id: str) -> LibraryComponent | None:
        return self.rows.get(component_id)

    def put(self, component: LibraryComponent) -> None:
        self.rows[component.component_id] = component


class ComponentRejected(ValueError):
    """The render check of a would-be component did not pass; nothing was stored."""

    def __init__(self, check: RenderCheck):
        super().__init__(f"component {check.interactive_id} failed the render check: "
                         f"{check.detail or 'see console/page errors'}")
        self.check = check


def _check(checker: RenderChecker, component_id: str, name: str, learning_question: str,
           html: str, height: int, probes: Sequence[Probe]) -> RenderCheck:
    draft = InteractiveDraft(interactive_id=component_id, section_id=LIBRARY_SECTION, title=name,
                             learning_question=learning_question, html=html, height=height,
                             probes=tuple(probes))
    result = checker.check(draft)
    if not result.ok or result.interactive_id != component_id:
        raise ComponentRejected(result)
    return result


def register(lib: ComponentLibrary, *, component_id: str, name: str, description: str,
             learning_question: str, html: str, height: int, probes: Sequence[Probe],
             checker: RenderChecker, params: Mapping[str, str] | None = None,
             source: str = "", scenarios: Sequence[str] = (),
             config_schema: Mapping[str, Any] | None = None,
             example_configs: Sequence[Mapping[str, Any]] = ()) -> LibraryComponent:
    """Render-checks the component and stores it only if the check passes (ComponentRejected
    otherwise; ValueError/ValidationError for malformed fields; SchemaError / ConfigInvalid for a
    schema outside the subset or an example that does not fit it). A configurable component is
    checked once per example config, on component + that config."""
    base: dict[str, Any] = {
        "component_id": component_id, "name": name, "description": description,
        "learning_question": learning_question, "params": dict(params or {}), "html": html,
        "height": height, "probes": tuple(probes), "source": source,
        "scenarios": tuple(scenarios)}
    if config_schema is None:
        result = _check(checker, component_id, name, learning_question, html, height, probes)
        component = LibraryComponent(**base, check=result)
    else:
        check_schema(config_schema)
        if len(example_configs) < 2:
            raise ValueError("a configurable component needs at least 2 example configs")
        draft = LibraryComponent.model_construct(**base, config_schema=dict(config_schema))
        checks = []
        for cfg in example_configs:
            html_i, probes_i, title, question = instantiate(draft, cfg)
            checks.append(_check(checker, component_id, title, question, html_i, height, probes_i))
        component = LibraryComponent(**base, check=checks[0], config_schema=dict(config_schema),
                                     example_configs=tuple(dict(c) for c in example_configs),
                                     example_checks=tuple(checks))
    lib.put(component)
    return component


def _line(c: LibraryComponent, description: str) -> str:
    lang = current_language()
    if lang == "en":
        line = f"{c.component_id}: {tr(c.name, lang=lang)} — {tr(description, lang=lang)}"
        if c.scenarios:
            line += " (Topics: " + ", ".join(tr(s, lang=lang) for s in c.scenarios) + ")"
        if c.params:
            line += " (Controls: " + "; ".join(f"{tr(k, lang=lang)}: {tr(v, lang=lang)}"
                                               for k, v in c.params.items()) + ")"
        return line + (" [Configurable]" if c.config_schema is not None
                       else " [Fixed content: this topic only; omit config]")
    line = f"{c.component_id}：{c.name} — {description}"
    if c.scenarios:
        line += "（适用：" + "、".join(c.scenarios) + "）"
    if c.params:
        line += "（可调：" + "；".join(f"{k}：{v}" for k, v in c.params.items()) + "）"
    if c.config_schema is not None:
        line += " [可配置]"
    else:
        line += " [固定内容：只讲这一个主题，不用给 config]"
    return line


def catalog_line(c: LibraryComponent) -> str:
    """`id：name — description（适用：…）（可调：…）` and, for a configurable component,
    `[可配置]` (its config's schema is the tool's: `deep.tools.place_component_schema`); a
    fixed-content one is marked `[固定内容]`."""
    return _line(c, c.description)


def _with_extras(c: LibraryComponent, note: str | None, presets: Sequence[Preset]) -> str:
    """The catalog line with the user's usage note instead of the description (H8) and one indented
    line per preset (H8)."""
    lines = [_line(c, note if note else c.description)]
    lines += [f"  预设「{p.name}」（{p.use}）：config "
              f"{json.dumps(p.config, ensure_ascii=False, separators=(',', ':'))}" for p in presets]
    return "\n".join(lines)


def catalog_text(lib: ComponentLibrary | Sequence[LibraryComponent],
                 notes: Mapping[str, str] | None = None,
                 presets: Mapping[str, Sequence[Preset]] | None = None) -> str:
    """One line per component: `id：name — description（可调：params）`; "" for an empty library.
    `notes` (component id → usage note) replace that component's description; each of `presets`
    adds an indented line under its component. Both empty/None: the plain listing."""
    comps = lib if isinstance(lib, Sequence) else lib.list()
    if not notes and not presets:
        return "\n".join(catalog_line(c) for c in comps)
    return "\n".join(_with_extras(c, (notes or {}).get(c.component_id),
                                  (presets or {}).get(c.component_id, ())) for c in comps)
