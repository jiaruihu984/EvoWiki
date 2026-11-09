"""H8 skills plumbing: catalog notes/presets, extra EvoUI examples, spec parts, and the
render-checked store-time writers (scripted checker, no browser)."""

from datetime import UTC, datetime

import pytest

from evowiki.content.models import Lang
from evowiki.deep.evoui import spec_for_prompt, spec_text, templates
from evowiki.deep.evoui import validate as validate_block
from evowiki.deep.library import MemoryLibrary, catalog_line, catalog_text
from evowiki.deep.tools import preread_spec, spec_part_text
from evowiki.harness import skills
from evowiki.harness.pack import HarnessInvalid
from evowiki.harness.steps import Example, Preset
from evowiki.store import harness as store
from evowiki.store.users import UserRow, create_user
from evowiki.teach.contracts import Exemplar, SpecItem, TeachingSpec
from tests.deep.fakes import FakeChecker, component
from tests.deep.test_components import CFG, configurable

T0 = datetime(2026, 10, 21, 1, 0, tzinfo=UTC)
FIXED = " [固定内容：只讲这一个主题，不用给 config]"
GOOD_CFG = {**CFG, "title": "转录"}
PRESET = Preset(name="转录", use="讲转录", config=GOOD_CFG)


def _user(pool, uid="u1"):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id=uid, background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))


def _teaching(exemplar: bool = True) -> TeachingSpec:
    items = (SpecItem(item_id="i1", text="先举例", origin="user_ui"),)
    ex = (Exemplar(exemplar_id="e1", kind="wiki_page", ref="p", title="样板", text="样板正文" * 10),)
    return TeachingSpec(version=2, items=items, exemplars=ex if exemplar else ())


# ---- catalog ----

def test_catalog_default_is_unchanged():
    lib = MemoryLibrary([component("a-one"), configurable()])
    plain = "\n".join(catalog_line(c) for c in lib.list())
    assert catalog_text(lib) == catalog_text(lib, None, None) == catalog_text(lib, {}, {}) == plain
    assert catalog_text([component("a-one")]).startswith("a-one：拼一条链 — 点按钮看计数加一（可调")


def test_a_note_replaces_the_description():
    lib = MemoryLibrary([component("a-one"), component("b-two")])
    lines = catalog_text(lib, {"a-one": "讲配对时用"}).split("\n")
    assert lines[0] == "a-one：拼一条链 — 讲配对时用（可调：加一：点一下计数加一）" + FIXED
    assert lines[1] == catalog_line(component("b-two"))


def test_a_preset_adds_an_indented_line():
    c = configurable()
    lines = catalog_text([c], None, {"steps-demo": [PRESET]}).split("\n")
    assert lines[0] == catalog_line(c)
    assert lines[1].startswith("  预设「转录」（讲转录）：config {") and '"title":"转录"' in lines[1]
    assert len(lines) == 2


# ---- EvoUI examples ----

def test_spec_for_prompt_extra():
    base = spec_for_prompt()
    assert spec_for_prompt(extra=()) == base
    ts = templates()
    assert base.endswith(f"### {ts[-1]['name']}（{ts[-1]['use']}）\n"
                         + __import__("json").dumps(ts[-1]["block"], ensure_ascii=False,
                                                    separators=(",", ":")))
    assert base.startswith(spec_text())
    extra = [{"name": "我的", "use": "用于我", "block": ts[0]["block"]}]
    got = spec_for_prompt(2, extra)
    assert got.startswith(spec_for_prompt(2)) and "### 我的（用于我）\n" in got
    assert got.index("### 我的") > got.index(f"### {ts[1]['name']}")


# ---- spec parts ----

def test_preread_spec_parts():
    t = _teaching()
    full = preread_spec(t)
    assert full == preread_spec(t, ("items", "exemplar"))
    assert full == spec_part_text(t, "items") + "\n\n" + spec_part_text(t, "exemplar")
    assert full.startswith("教学规范 v2（1 条）：") and "样板页 0《样板》开头：\n" in full
    assert preread_spec(t, ("items",)) == spec_part_text(t, "items") and "样板页 0《" not in preread_spec(t, ("items",))
    assert preread_spec(t, ("exemplar",)) == spec_part_text(t, "exemplar")
    assert preread_spec(t, ()) == ""
    no_ex = _teaching(False)
    assert preread_spec(no_ex) == spec_part_text(no_ex, "items") and spec_part_text(no_ex, "exemplar") == ""


# ---- store-time writers ----

def _stored(pool, key):
    with pool.connection() as conn:
        return store.load_pack(conn, "u1").values.get(key)


def test_put_presets_stores_on_a_passing_check(pool):
    _user(pool)
    lib, checker = MemoryLibrary([configurable()]), FakeChecker()
    with pool.connection() as conn:
        v = skills.put_presets(conn, "u1", "steps-demo", [PRESET], library=lib, checker=checker,
                               source="user", why="t", now=T0)
    assert v == 1 and checker.checked == ["steps-demo"]
    assert _stored(pool, "H8.component.steps-demo.presets")[0]["name"] == "转录"


def test_put_presets_refuses(pool):
    _user(pool)
    lib = MemoryLibrary([configurable(), component("fixed-one")])
    key = "H8.component.steps-demo.presets"

    def put(cid, preset, checker=None):
        with pool.connection() as conn:
            skills.put_presets(conn, "u1", cid, [preset], library=lib, checker=checker or FakeChecker(),
                               source="user", why="t", now=T0)

    failing = FakeChecker(fail=lambda d: "page error")
    with pytest.raises(HarnessInvalid, match="转录.*渲染检查没通过"):
        put("steps-demo", PRESET, failing)
    with pytest.raises(HarnessInvalid, match="转录.*没有 nope"):
        put("nope", PRESET)
    with pytest.raises(HarnessInvalid, match="固定内容"):
        put("fixed-one", PRESET)
    with pytest.raises(HarnessInvalid, match="转录.*：配置不合法$"):
        put("steps-demo", Preset(name="转录", use="x", config={"title": "只有标题"}))
    assert _stored(pool, key) is None


def test_put_examples(pool):
    _user(pool)
    block = templates()[0]["block"]
    ex = Example(name="我的", use="用于我", block=block)

    def put(examples, checker):
        with pool.connection() as conn:
            return skills.put_examples(conn, "u1", examples, checker=checker, source="user",
                                       why="t", now=T0)

    failing = FakeChecker(fail_block=lambda bid, b: "overflow")
    with pytest.raises(HarnessInvalid, match="我的.*渲染检查没通过"):
        put([ex], failing)
    bad = {**block, "body": {"kind": "nonsense"}}
    assert validate_block(bad)
    ok_checker = FakeChecker()
    with pytest.raises(HarnessInvalid, match="我的.*块不合法"):
        put([Example(name="我的", use="用于我", block=bad)], ok_checker)
    assert ok_checker.checked == [] and _stored(pool, "H8.deep.evoui_examples") is None
    assert put([ex], ok_checker) == 1 and ok_checker.checked == ["example-1"]
    assert _stored(pool, "H8.deep.evoui_examples")[0]["name"] == "我的"
