"""The component library (B5): register only after a passing render check, catalog text, the Postgres
store (migration 0027) and the seed file (its metadata, and the real render check when Chromium is
installed)."""

import pytest
from pydantic import ValidationError

from evowiki.deep.contracts import LibraryComponent, Probe, RenderCheck
from evowiki.deep.library import (
    ComponentRejected,
    MemoryLibrary,
    catalog_text,
    register,
)
from evowiki.deep.library_seed import REPO_ROOT, load_seed, register_seed, seed_html
from evowiki.deep.render import PlaywrightChecker
from evowiki.deep.store import PgLibrary
from tests.deep.fakes import HTML, FakeChecker, component

PROBES = (Probe(target="#b", action="click", expect_change="#out"),)


def reg(lib, checker, cid="pair-demo", **kw):
    return register(lib, component_id=cid, name="拼一条链", description="点按钮看计数",
                    learning_question="对面放什么？", html=HTML, height=480, probes=PROBES,
                    checker=checker, params={"加一": "计数加一"}, **kw)


def test_register_stores_only_after_a_passing_check():
    lib, checker = MemoryLibrary(), FakeChecker()
    c = reg(lib, checker, source="test")
    assert checker.checked == ["pair-demo"] and c.check.ok and c.source == "test"
    assert lib.get("pair-demo") == c and lib.list() == [c]


def test_a_failing_component_is_never_stored():
    lib = MemoryLibrary()
    with pytest.raises(ComponentRejected, match="按钮没反应"):
        reg(lib, FakeChecker(lambda d: "按钮没反应"))
    assert lib.list() == [] and lib.get("pair-demo") is None


def test_a_checker_answering_for_another_id_is_refused():
    class Wrong:
        def check(self, d):
            return RenderCheck(interactive_id="other", ok=True)
    lib = MemoryLibrary()
    with pytest.raises(ComponentRejected):
        reg(lib, Wrong())
    assert lib.list() == []


def test_malformed_fields_are_refused_before_any_check():
    checker = FakeChecker()
    with pytest.raises(ValidationError):
        reg(MemoryLibrary(), checker, cid="Bad_ID")
    assert checker.checked == []


def test_library_component_itself_refuses_a_failing_check():
    with pytest.raises(ValidationError):
        LibraryComponent(**{**component().model_dump(),
                            "check": RenderCheck(interactive_id="pair-demo", ok=False)})


def test_catalog_text_one_line_per_component():
    lib = MemoryLibrary([component("a-one"), component("b-two")])
    lines = catalog_text(lib).split("\n")
    fixed = " [固定内容：只讲这一个主题，不用给 config]"
    assert lines == ["a-one：拼一条链 — 点按钮看计数加一（可调：加一：点一下计数加一）" + fixed,
                     "b-two：拼一条链 — 点按钮看计数加一（可调：加一：点一下计数加一）" + fixed]
    assert catalog_text(MemoryLibrary()) == ""
    bare = component().model_copy(update={"params": {}})
    assert catalog_text([bare]) == "pair-demo：拼一条链 — 点按钮看计数加一" + fixed


# ---- Postgres ----

def test_pg_library_roundtrip_and_replace(pool):
    with pool.connection() as conn:
        conn.execute("delete from component_library")
    lib = PgLibrary(pool)
    assert lib.list() == [] and lib.get("pair-demo") is None
    a, b = component("pair-demo"), component("zeta-demo")
    lib.put(b)
    lib.put(a)
    assert [c.component_id for c in lib.list()] == ["pair-demo", "zeta-demo"]
    assert lib.get("pair-demo") == a
    newer = a.model_copy(update={"name": "改名"})
    lib.put(newer)
    assert lib.get("pair-demo").name == "改名" and len(lib.list()) == 2


def test_the_table_refuses_a_row_whose_check_did_not_pass(pool):
    import psycopg
    body = component().model_dump(mode="json")
    body["check"]["ok"] = False
    with pytest.raises(psycopg.errors.CheckViolation), pool.connection() as conn:
        conn.execute("insert into component_library (component_id, body) values (%s, %s::jsonb)",
                     ("pair-demo", __import__("json").dumps(body)))


# ---- the seed ----

def test_seed_metadata_is_complete_and_excludes_the_teaching_bug():
    seed = load_seed()
    ids = [c.component_id for c in seed]
    assert len(set(ids)) == len(ids) >= 5
    assert not any("run1-base" in c.html_path for c in seed)
    for c in seed:
        assert (REPO_ROOT / c.html_path).is_file(), c.html_path
        assert c.params and c.probes and 400 <= c.height <= 1600
        html = seed_html(c)
        for p in c.probes:        # the result region is a real element of the html
            assert p.expect_change.startswith("#") and p.expect_change[1:] in html, p


def test_seed_registers_with_a_passing_checker_and_skips_failing_ones():
    seed = load_seed()
    lib = MemoryLibrary()
    bad = seed[0].component_id
    res = register_seed(lib, FakeChecker(lambda d: "坏了" if d.interactive_id == bad else None),
                        seed)
    assert [r.registered for r in res] == [False] + [True] * (len(seed) - 1)
    assert lib.get(bad) is None and len(lib.list()) == len(seed) - 1
    assert "坏了" in res[0].error
    dry = register_seed(None, FakeChecker(), seed)            # dry run: checks only
    assert all(r.registered for r in dry)


def test_seed_passes_the_real_render_check():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (uv sync --extra deep)")
    try:
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium not available ({type(exc).__name__})")
    lib = MemoryLibrary()
    res = register_seed(lib, PlaywrightChecker())
    assert [r.error for r in res if not r.registered] == []
    assert len(lib.list()) == len(load_seed())
