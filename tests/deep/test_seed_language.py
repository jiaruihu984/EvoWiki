import json

import pytest

from evowiki.deep.components import inject, resolve_probes
from evowiki.deep.library_seed import load_seed, seed_html
from evowiki.deep.render import _probe
from tests.deep.test_evoui_motion_nodes import browser  # noqa: F401

SEEDS = {c.component_id: c for c in load_seed()}
STATE = {
    "pairing-practice": "state", "replication-sim": "stage", "pairing-lab": "pick",
    "pairing-builder": "({topS,bot,sel})", "replication-lab": "({S1,stage,errAt})",
    "before-after": "({view,sc})", "code-trace": "cur", "distribution-line": "withOut",
    "function-explorer": "({showCmp,xs})", "graph-walk": "cur",
    "linear-transform-2d": "({mi,t,ang})", "param-bars": "({X,outs})",
    "random-sim": "({total,counts,series})", "scatter-line": "({pts,added,sel})",
    "sort-match": "({place,sel,checked})", "step-process": "({i,wrongMode})",
    "timeline-lanes": "({sc,t})", "network-edit": "S",
}


@pytest.mark.parametrize("component_id", SEEDS)
def test_managed_seed_switch_keeps_state_and_probe_identity(browser, component_id):  # noqa: F811
    component = SEEDS[component_id]
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    html = seed_html(component, language="en")
    probes = component.probes
    if component.example_configs:
        config = component.example_configs[0]
        html = inject(html, config)
        probes = resolve_probes(probes, config)
    page.set_content(html)
    assert _probe(page, probes[0]) is None
    state = page.evaluate("JSON.stringify(" + STATE[component_id] + ")")
    controls = page.locator("input,select").evaluate_all("els => els.map(e => [e.id,e.value,e.checked])")
    page.evaluate("EvoWikiLocale.setLanguage('zh')")
    assert page.evaluate("JSON.stringify(" + STATE[component_id] + ")") == state
    assert page.locator("input,select").evaluate_all("els => els.map(e => [e.id,e.value,e.checked])") == controls
    page.evaluate("EvoWikiLocale.setLanguage('en')")
    assert page.evaluate("JSON.stringify(" + STATE[component_id] + ")") == state
    assert page.locator(component.probes[0].target).count()
    assert not errors
    page.close()


def test_external_config_remains_original_and_locale_is_explicit(browser):  # noqa: F811
    component = SEEDS["param-bars"]
    config = json.loads(json.dumps(component.example_configs[0]))
    config["title"] = "用户原文保持中文"
    page = browser.new_page()
    page.set_content(inject(seed_html(component, language="en"), config))
    assert page.locator("html").get_attribute("lang") == "en"
    assert page.locator("#title").inner_text() == config["title"]
    page.evaluate("EvoWikiLocale.setLanguage('zh')")
    assert page.locator("#title").inner_text() == config["title"]
    page.close()
