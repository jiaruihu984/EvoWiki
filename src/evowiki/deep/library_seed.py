"""Seed data for the component library: `library_seed.json` (authored metadata, html by repo-relative
path) → `SeedComponent` → `register_seed`, which registers each one through `library.register`, so a
seed component whose render check fails is reported and never stored.

`library_seed.json` lists fixed-content components inline (`components`) and configurable ones as
files (`component_files`: repo-relative paths to one `SeedComponent` JSON each, kept next to its HTML in
`src/evowiki/deep/components/`)."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evowiki.deep.components import ConfigInvalid, SchemaError, localized_html
from evowiki.deep.contracts import Probe, RenderCheck, RenderChecker
from evowiki.deep.library import ComponentLibrary, ComponentRejected, MemoryLibrary, register
from evowiki.i18n import current_language

SEED_FILE = Path(__file__).with_name("library_seed.json")
REPO_ROOT = Path(__file__).resolve().parents[3]


class SeedComponent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    component_id: str
    name: str
    description: str
    learning_question: str
    params: dict[str, str] = Field(default_factory=dict)
    html_path: str                       # repo-relative, forward slashes
    height: int
    probes: tuple[Probe, ...]
    source: str = ""
    scenarios: tuple[str, ...] = ()
    config_schema: dict[str, Any] | None = None
    example_configs: tuple[dict[str, Any], ...] = ()


class SeedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = ""
    components: tuple[SeedComponent, ...] = ()
    component_files: tuple[str, ...] = ()          # repo-relative SeedComponent JSON files


def load_component(path: Path) -> SeedComponent:
    return SeedComponent.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_seed(path: Path = SEED_FILE, root: Path = REPO_ROOT) -> tuple[SeedComponent, ...]:
    f = SeedFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return (*f.components, *(load_component(root / p) for p in f.component_files))


VENDOR_DIR = Path(__file__).with_name("components") / "vendor"
# `<script data-vendor="cytoscape"></script>` in a component's HTML: the vendored library
# (components/vendor/<name>.min.js, its licence next to it) is inlined there, so a placed component
# stays one self-contained, offline file (AUTHORING rule 5) while the repo keeps the library once.
_VENDOR = re.compile(r'<script data-vendor="([a-z0-9-]+)"></script>')


def inline_vendor(html: str) -> str:
    """`html` with every vendored-library placeholder replaced by the library inline; FileNotFoundError
    for an unknown library (a component never ships without its library)."""
    def lib(m: re.Match[str]) -> str:
        code = (VENDOR_DIR / f"{m.group(1)}.min.js").read_text(encoding="utf-8")
        return "<script>" + code.replace("</script", "<\\/script") + "</script>"
    return _VENDOR.sub(lib, html)


def seed_html(c: SeedComponent, root: Path = REPO_ROOT, *, language: str | None = None) -> str:
    html = inline_vendor((root / c.html_path).read_text(encoding="utf-8"))
    if f'data-evowiki-locale="{c.component_id}"' in html:
        messages = json.loads(Path(__file__).with_name("seed_messages.json").read_text(encoding="utf-8"))
        resource = json.dumps(messages[c.component_id], ensure_ascii=False).replace("</", "<\\/")
        script = Path(__file__).with_name("seed_locale.js").read_text(encoding="utf-8")
        html = html.replace(f'<script data-evowiki-locale="{c.component_id}"></script>',
                            '<script type="application/json" id="evowiki-system-messages">' + resource
                            + '</script><script data-evowiki-locale="' + c.component_id + '">'
                            + script + '</script>')
    return localized_html(html, language or current_language())


@dataclass(frozen=True)
class SeedResult:
    component_id: str
    registered: bool
    check: RenderCheck | None
    error: str = ""


def register_seed(lib: ComponentLibrary | None, checker: RenderChecker,
                  components: Sequence[SeedComponent] | None = None,
                  root: Path = REPO_ROOT) -> list[SeedResult]:
    """Render-checks every seed component and stores the passing ones in `lib`. `lib=None` is a dry
    run: the checks run, nothing is stored. A failing component is reported, never stored."""
    out = []
    target = lib if lib is not None else MemoryLibrary()
    for c in load_seed() if components is None else components:
        try:
            comp = register(target, component_id=c.component_id, name=c.name,
                            description=c.description, learning_question=c.learning_question,
                            html=seed_html(c, root), height=c.height, probes=c.probes,
                            checker=checker, params=c.params, source=c.source,
                            scenarios=c.scenarios, config_schema=c.config_schema,
                            example_configs=c.example_configs)
            out.append(SeedResult(c.component_id, True, comp.check))
        except ComponentRejected as exc:
            out.append(SeedResult(c.component_id, False, exc.check, str(exc)))
        except (SchemaError, ConfigInvalid) as exc:
            out.append(SeedResult(c.component_id, False, None, str(exc)))
    return out
