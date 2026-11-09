"""Anti-circularity for the per-user RSI cycle (evowiki.teach): it sees only what a deployment
sees (pattern: tests/rsib/test_isolation.py).

1. Static: `evowiki/teach/**/*.py` import only evowiki.teach, evowiki.belief, evowiki.goals.contracts,
   evowiki.timeutil, evowiki.llm.models and the pure gettext presentation helper evowiki.i18n.
2. Static: no hidden-state name (the RSI-B list) is ever named in evowiki/teach.
3. Runtime: importing `evowiki.teach.cycle` in a fresh interpreter loads no simulator, experiment,
   simulation-harness or store module.
4. Runtime: during a /wiki/rsi/run the cycle's `UserData.events` are all aspect events."""

import json
import subprocess
import sys
import typing
from pathlib import Path

from evowiki.belief.aspects import AspectEvent
from evowiki.teach import cycle as cycle_mod
from tests.isolation import HIDDEN_NAMES, _imports, _names
from tests.service.conftest import client, services  # noqa: F401 — fixtures for test 4

TEACH = Path(__file__).resolve().parents[2] / "src" / "evowiki" / "teach"
ALLOWED = ("evowiki.teach", "evowiki.belief", "evowiki.goals.contracts", "evowiki.timeutil",
           "evowiki.schema", "evowiki.plain_text", "evowiki.llm.models", "evowiki.i18n")
ASPECT_EVENT_TYPES = typing.get_args(typing.get_args(AspectEvent)[0])


def test_teach_imports_stay_inside_the_allow_list():
    files = sorted(TEACH.rglob("*.py"))                     # teach/layer2/, teach/fit/ included
    assert {f.name for f in files} >= {"cycle.py", "predict.py", "gate_a.py", "memory.py",
                                       "registry.py", "fitter.py", "priors.py"}
    for f in files:
        for name in _imports(f, "evowiki.teach"):
            if name.startswith("evowiki"):
                assert any(name == a or name.startswith(a + ".") for a in ALLOWED), \
                    f"{f.name} imports {name}"


def test_teach_never_names_hidden_state():
    for f in sorted(TEACH.rglob("*.py")):
        assert not _names(f) & set(HIDDEN_NAMES), f"{f.name}: {_names(f) & set(HIDDEN_NAMES)}"


def test_importing_the_cycle_loads_no_simulator_or_store_module():
    code = ("import json, sys, evowiki.teach.cycle\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('evowiki.'))))")
    mods = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True,
                                     text=True, check=True).stdout)
    assert "evowiki.teach.cycle" in mods
    assert not [m for m in mods if m.startswith(("evowiki.simulator", "evowiki.experiments",
                                                 "evowiki.store"))]


def test_the_cycle_receives_only_aspect_events(client, services, monkeypatch):  # noqa: F811
    from tests.service.test_rsi_api import seed
    seen = []
    real = cycle_mod.RsiCycle.run

    def spy(self, user_id, data, **kw):
        assert data.events and all(isinstance(e, ASPECT_EVENT_TYPES) for e in data.events)
        assert all(isinstance(v, str) for v in data.domain_of.values())
        seen.append(user_id)
        return real(self, user_id, data, **kw)
    monkeypatch.setattr(cycle_mod.RsiCycle, "run", spy)
    seed(services, client)
    r = client.post("/wiki/rsi/run", headers={"X-EvoWiki-User": "u1"})
    assert r.status_code == 200, r.text
    assert seen == ["u1"]


def test_gettext_presentation_helper_has_no_product_or_model_dependencies():
    helper = TEACH.parent / "i18n.py"
    assert not [name for name in _imports(helper, "evowiki") if name.startswith("evowiki.")]
