"""H8 skills: the only way component presets and extra EvoUI examples enter a pack. Each one is
checked when it is stored, with the checks the writer's own tools run (place_component /
write_block + render_check), so a stored preset or example always renders. Any failure is a
HarnessInvalid naming the item (fail closed); nothing is stored. Never imported by deep/."""

from collections.abc import Sequence
from datetime import datetime

import psycopg
from pydantic import ValidationError

from evowiki.deep.components import ConfigInvalid, instantiate
from evowiki.deep.contracts import BlockChecker, InteractiveDraft, RenderChecker
from evowiki.deep.evoui import validate as validate_block
from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.library import LIBRARY_SECTION, ComponentLibrary
from evowiki.harness.pack import HarnessInvalid
from evowiki.harness.steps import Example, Preset, _reason, parse_examples, parse_presets
from evowiki.store import harness as store
from evowiki.store.harness import Source


def _check_preset(library: ComponentLibrary, checker: RenderChecker, component_id: str,
                  p: Preset) -> None:
    where = f"预设「{p.name}」"
    c = library.get(component_id)
    if c is None:
        raise HarnessInvalid(f"{where}：组件库里没有 {component_id}")
    if c.config_schema is None:
        raise HarnessInvalid(f"{where}：{component_id} 是固定内容的组件，不能加预设")
    try:
        html, probes, title, question = instantiate(c, p.config)
    except ConfigInvalid as e:
        raise HarnessInvalid(f"{where}：配置不合法") from e
    try:
        draft = InteractiveDraft(interactive_id=component_id, section_id=LIBRARY_SECTION,
                                 title=title, learning_question=question, html=html,
                                 height=c.height, probes=probes)
    except ValidationError as e:
        why = _reason(InteractiveDraft, e.errors(include_url=False)[0], own_text=False)
        raise HarnessInvalid(f"{where}：组件不合法：{why}") from e
    result = checker.check(draft)
    if not result.ok:
        raise HarnessInvalid(f"{where}：渲染检查没通过（{result.detail or '见控制台或页面报错'}）")


def put_presets(conn: psycopg.Connection, user_id: str, component_id: str,
                presets: Sequence[Preset], *, library: ComponentLibrary, checker: RenderChecker,
                source: Source, why: str, now: datetime) -> int | None:
    """Stores `H8.component.<id>.presets` after every preset's component exists and is
    configurable, its config validates and the render check on component + config passes (the
    call place_component makes)."""
    items = parse_presets([p.model_dump(mode="json") for p in presets])
    for p in items:
        _check_preset(library, checker, component_id, p)
    return store.set_value(conn, user_id, f"H8.component.{component_id}.presets",
                           [p.model_dump(mode="json") for p in items],
                           source=source, why=why, now=now)


def _check_example(checker: BlockChecker, i: int, e: Example) -> None:
    where = f"示例「{e.name}」"
    problems = validate_block(e.block)
    if problems:
        raise HarnessInvalid(f"{where}：块不合法：" + "；".join(problems[:8]))
    try:
        block = EvoUIBlock.model_validate(e.block)
    except ValidationError as err:
        why = _reason(EvoUIBlock, err.errors(include_url=False)[0], own_text=False)
        raise HarnessInvalid(f"{where}：块不合法：{why}") from err
    result = checker.check_block(f"example-{i}", block)       # the check_block path of render_all
    if not result.ok:
        raise HarnessInvalid(f"{where}：渲染检查没通过（{result.detail or '见控制台或页面报错'}）")


def put_examples(conn: psycopg.Connection, user_id: str, examples: Sequence[Example], *,
                 checker: BlockChecker, source: Source, why: str, now: datetime) -> int | None:
    """Stores `H8.deep.evoui_examples` after every block passes the EvoUI validator and the
    render check."""
    items = parse_examples([e.model_dump(mode="json") for e in examples])
    for i, e in enumerate(items, 1):
        _check_example(checker, i, e)
    return store.set_value(conn, user_id, "H8.deep.evoui_examples",
                           [e.model_dump(mode="json") for e in items],
                           source=source, why=why, now=now)
