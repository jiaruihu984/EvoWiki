"""Configurable library components: the schema subset, config validation, injection, probe references,
registration (every example render-checked on component + config) and placing one from deep refine.
No browser (FakeChecker) except the one real-browser test at the end."""

import json

import pytest

from evowiki.deep.components import (
    ConfigInvalid,
    SchemaError,
    cell_problems,
    check_schema,
    config_script,
    graded_config,
    inject,
    instantiate,
    resolve_probes,
    validate_config,
)
from evowiki.deep.contracts import CONFIG_ELEMENT_ID, LibraryComponent, Probe, RenderCheck
from evowiki.deep.library import ComponentRejected, MemoryLibrary, catalog_text, register
from evowiki.deep.library_seed import REPO_ROOT, load_component, seed_html
from tests.deep.fakes import FakeChecker, component

DIST = REPO_ROOT / "src/evowiki/deep/components/distribution-line.json"

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "question", "steps", "note"],
    "properties": {
        "title": {"type": "string"}, "question": {"type": "string"},
        "steps": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "text"],
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}},
        "note": {"type": ["string", "null"]}}}
CFG = {"title": "复制 DNA", "question": "每一步变了什么？",
       "steps": [{"id": "s-a", "text": "拉开"}, {"id": "s-b", "text": "配对"}], "note": None}
HTML = ("<!doctype html><meta charset=utf-8><p id=t></p><button id=next>下一步</button><p id=out>0</p>"
        "<script>const c=JSON.parse(document.getElementById('evowiki-config').textContent);"
        "t.textContent=c.title;let i=0;next.onclick=()=>{out.textContent=c.steps[++i%c.steps.length].text}"
        "</script>")
PROBES = (Probe(target="#next", action="click", expect_change="#out"),)


# ---- schema subset ----

def test_the_reference_component_schema_is_in_the_subset():
    c = load_component(DIST)
    check_schema(c.config_schema)
    for cfg in c.example_configs:
        assert validate_config(c.config_schema, cfg) == cfg


@pytest.mark.parametrize("change, why", [
    (lambda s: s["properties"]["steps"].update(maxItems=5), "unsupported keys"),
    (lambda s: s["properties"]["title"].update(maxLength=5), "unsupported keys"),
    (lambda s: s["properties"]["steps"].update(minItems=2), "minItems"),
    (lambda s: s.update(required=["title", "question", "steps"]), "every property must be required"),
    (lambda s: s.pop("additionalProperties"), "additionalProperties"),
    (lambda s: s["properties"].pop("question") and s["required"].remove("question"), "question"),
    (lambda s: s["properties"]["steps"].pop("items"), "array without items"),
])
def test_schemas_outside_the_subset_are_refused(change, why):
    bad = json.loads(json.dumps(SCHEMA))
    change(bad)
    with pytest.raises(SchemaError, match=why):
        check_schema(bad)


# ---- validation ----

@pytest.mark.parametrize("cfg, why", [
    ({**CFG, "steps": "拉开"}, "expected array"),
    ({**CFG, "extra": 1}, "unknown key"),
    ({k: v for k, v in CFG.items() if k != "note"}, "missing 'note'"),
    ({**CFG, "steps": []}, "at least 1"),
    ({**CFG, "title": "  "}, "must not be empty"),
    ({**CFG, "note": "长" * 700}, "longer than"),
    ({**CFG, "steps": [{"id": "x", "text": "t"}] * 61}, "more than 60"),
    ({**CFG, "note": 3}, "expected string/null"),
])
def test_invalid_configs_are_refused(cfg, why):
    with pytest.raises(ConfigInvalid, match=why):
        validate_config(SCHEMA, cfg)


def test_numbers_and_anyof():
    s = {"type": "object", "additionalProperties": False, "required": ["title", "question", "x", "o"],
         "properties": {"title": {"type": "string"}, "question": {"type": "string"},
                        "x": {"type": "number"},
                        "o": {"anyOf": [{"type": "null"}, {"type": "integer"}]}}}
    base = {"title": "t", "question": "q", "x": 1.5, "o": None}
    validate_config(s, base)
    validate_config(s, {**base, "o": 3})
    for bad in ({**base, "x": True}, {**base, "x": float("nan")}, {**base, "o": 1.5}):
        with pytest.raises(ConfigInvalid):
            validate_config(s, bad)


# ---- injection and probes ----

def test_inject_puts_the_config_before_the_first_script_and_replaces_a_default():
    html = inject(HTML, CFG)
    i, j = html.index(f'id="{CONFIG_ELEMENT_ID}"'), html.index("<script>const")
    assert i < j
    again = inject(html, {**CFG, "title": "新"})
    assert again.count(f'id="{CONFIG_ELEMENT_ID}"') == 1 and '"新"' in again


def test_config_text_cannot_close_its_script():
    tag = config_script({**CFG, "title": "</script><script>alert(1)</script><!--"})
    body = tag[tag.index(">") + 1:tag.rindex("</script>")]
    assert "<" not in body
    assert json.loads(body)["title"] == "</script><script>alert(1)</script><!--"


def test_probes_may_reference_the_config():
    probes = (Probe(target="[data-step={{config.steps[1].id}}]", action="click",
                    expect_change="#out"),)
    assert resolve_probes(probes, CFG)[0].target == "[data-step=s-b]"
    with pytest.raises(ConfigInvalid, match="steps.5"):
        resolve_probes((probes[0].model_copy(update={"target": "{{config.steps.5.id}}"}),), CFG)


# ---- the contract and registration ----

def configurable(**kw) -> LibraryComponent:
    ok = RenderCheck(interactive_id="steps-demo", ok=True, probes_passed=1, probes_total=1)
    base = {"component_id": "steps-demo", "name": "分步过程", "description": "一步步看",
            "html": HTML, "height": 480, "learning_question": "每一步变了什么？", "probes": PROBES,
            "check": ok, "config_schema": SCHEMA,
            "example_configs": (CFG, {**CFG, "title": "二分查找"}), "example_checks": (ok, ok),
            "scenarios": ("DNA 复制", "算法")}
    return LibraryComponent(**{**base, **kw})


def test_contract_needs_two_passing_examples():
    configurable()
    bad = RenderCheck(interactive_id="steps-demo", ok=False)
    for kw in ({"example_configs": (CFG,)}, {"example_checks": (configurable().check, bad)},
               {"config_schema": None}):
        with pytest.raises(ValueError):
            configurable(**kw)


def test_register_checks_every_example_on_component_plus_config():
    lib, checker = MemoryLibrary(), FakeChecker()
    seen = []
    checker.fail = lambda d: seen.append(d.html) or None
    c = register(lib, component_id="steps-demo", name="分步过程", description="一步步看",
                 learning_question="每一步变了什么？", html=HTML, height=480, probes=PROBES,
                 checker=checker, config_schema=SCHEMA,
                 example_configs=(CFG, {**CFG, "title": "二分查找"}), scenarios=("算法",))
    assert len(seen) == 2 and '"复制 DNA"' in seen[0] and '"二分查找"' in seen[1]
    assert c.configurable and len(c.example_checks) == 2 and lib.get("steps-demo") == c


def test_register_refuses_a_failing_example_a_bad_schema_or_a_bad_example():
    def reg(**kw):
        args = {"component_id": "steps-demo", "name": "分步过程", "description": "d",
                "learning_question": "q", "html": HTML, "height": 480, "probes": PROBES,
                "checker": FakeChecker(), "config_schema": SCHEMA,
                "example_configs": (CFG, {**CFG, "title": "二分查找"})}
        return register(MemoryLibrary(), **{**args, **kw})
    with pytest.raises(ComponentRejected):
        reg(checker=FakeChecker(lambda d: "坏了" if "二分查找" in d.html else None))
    with pytest.raises(SchemaError):
        reg(config_schema={**SCHEMA, "additionalProperties": True})
    with pytest.raises(ConfigInvalid):
        reg(example_configs=(CFG, {**CFG, "steps": 1}))
    with pytest.raises(ValueError, match="at least 2"):
        reg(example_configs=(CFG,))


def test_instantiate_fixed_and_configurable():
    fixed = component()
    assert instantiate(fixed, None)[0] == fixed.html
    with pytest.raises(ConfigInvalid, match="fixed-content"):
        instantiate(fixed, CFG)
    c = configurable()
    html, _, title, question = instantiate(c, CFG)
    assert CONFIG_ELEMENT_ID in html and (title, question) == ("复制 DNA", "每一步变了什么？")
    with pytest.raises(ConfigInvalid, match="needs a config"):
        instantiate(c, None)


def test_catalog_lists_scenarios_without_a_config_shape():
    """The config's format is the tool's JSON Schema (`place_component_schema`), not a prompt line."""
    line = catalog_text([configurable()])
    assert line.startswith("steps-demo：分步过程 — 一步步看（适用：DNA 复制、算法）")
    assert line.endswith("[可配置]")
    assert "string?" not in line and "steps:" not in line


# ---- placing from deep refine ----

def test_refine_places_a_configurable_component_with_its_config(env):
    from tests.deep.fakes import DeepScript, finish, interactive, section, turn
    from tests.deep.test_service import record, refine, service
    from tests.wikiapp.test_page import organized

    pid = organized(env)
    body = "s2 正文\n{{interactive:steps-demo}}\n{{interactive:steps-two}}"
    bad = interactive("steps-demo", config={**CFG, "steps": "x"})
    good = interactive("steps-demo", i=2, config=CFG)
    second = interactive("steps-demo", i=3, config={**CFG, "title": "二分查找"},
                         placement_id="steps-two")
    ds = DeepScript().plan().write(turn(section("s2", body=body), bad), turn(good, second),
                                   turn(finish()))
    svc = service(env, ds, lib=MemoryLibrary([configurable()]))
    res = refine(svc, pid)
    assert res.job.stage == "done" and len(res.job.interactives) == 2
    second_req = [r for r in ds.requests if r.purpose == "deep.write"][2]   # after plan_page
    err = next(m.text for m in second_req.messages if m.role == "tool" and m.is_error)
    assert "config 不合法，没有放置" in err
    first = next(r for r in ds.requests if r.purpose == "deep.write")
    assert "[可配置] 的要给 config，格式见它的 config 参数" in first.messages[0].text
    place = next(t for t in first.tools if t.name == "place_component")
    assert place.parameters["properties"]["config"]["anyOf"][1] == {**SCHEMA, "title": "steps-demo"}
    titles = sorted(a.meta.title for a in svc.assets.rows.values())
    assert titles == ["二分查找", "复制 DNA"]
    for a in svc.assets.rows.values():
        assert CONFIG_ELEMENT_ID in a.html and a.meta.section_id == "s2"
    assert "steps-two" in record(env, pid).page.texts["s2"].body


@pytest.fixture
def env():
    from evowiki.wikiapp.repo import MemoryRepo
    from tests.wikiapp.script import Script
    from tests.wikiapp.test_goal import Ids
    return MemoryRepo(["u1"]), Script(), Ids()


# ---- the real browser ----

def test_the_reference_component_passes_with_both_examples_in_chromium():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium not available ({type(exc).__name__}); uv run playwright install chromium")
    from evowiki.deep.render import PlaywrightChecker
    c = load_component(DIST)
    comp = register(MemoryLibrary(), component_id=c.component_id, name=c.name,
                    description=c.description, learning_question=c.learning_question,
                    html=seed_html(c), height=c.height, probes=c.probes,
                    checker=PlaywrightChecker(), params=c.params, scenarios=c.scenarios,
                    config_schema=c.config_schema, example_configs=c.example_configs)
    assert all(x.ok and x.probes_passed == x.probes_total for x in comp.example_checks)


SORT_CONFIG = {"targets": [{"id": "a"}], "items": [{"id": "1", "answer": "a", "cell": "c1.term"},
                                                   {"id": "2", "answer": "a", "cell": None}]}


def test_graded_config_only_for_an_answer_key():
    assert graded_config(inject("<p>x</p><script>1</script>", SORT_CONFIG)) == SORT_CONFIG
    assert graded_config(inject("<p>x</p><script>1</script>", CFG)) is None
    assert graded_config("<p>no config</p>") is None


def test_a_graded_config_names_this_pages_cells():
    assert cell_problems(CFG, {"c1.term"}) == []                       # no answer key
    assert cell_problems(SORT_CONFIG, ()) == []                        # a page without cells
    assert "2 没有写 cell" in cell_problems(SORT_CONFIG, {"c1.term"})[0]
    assert "不能用 c1.term" in cell_problems(SORT_CONFIG, {"c2.term"})[0]


def test_sort_match_reports_each_items_first_placement_only_in_chromium():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium not available ({type(exc).__name__}); uv run playwright install chromium")
    c = load_component(REPO_ROOT / "src/evowiki/deep/components/sort-match.json")
    html = inject(seed_html(c), c.example_configs[0])
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        listen = "<script>window.got=[];addEventListener('message',e=>{if(e.data.evowikiComponentItem)got.push(e.data.evowikiComponentItem)})</script>"
        page.set_content(html.replace("<body>", "<body>" + listen, 1))

        def put(i, k):
            page.click(f"[data-probe=item-{i}]")
            page.click(f"[data-probe=cat-{k}]")

        put(0, 1)                     # wrong
        put(0, 0)                     # the retry
        put(1, 1)
        page.click("#reset")
        put(0, 1)                     # after a reset: still the same item
        page.click("#show")           # the rest were not tried
        page.wait_for_timeout(50)
        got = page.evaluate("got")
        b.close()
    assert got[:2] == [{"item": "1", "target": "d"}, {"item": "2", "target": "d"}]
    assert got[2:] == [{"item": str(i), "target": None} for i in range(3, 8)]
