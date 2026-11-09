"""Guard: the private DBOS symbols the code reaches into still exist (a DBOS upgrade that moves
one must fail here, not at runtime). Each line cites its user in src."""

import importlib

import pytest

SYMBOLS = [
    ("dbos._dbos", "dynamic_scheduler_loop"),      # agent/dbos_app.py patches it
    ("dbos._dbos", "_dbos_global_instance"),       # agent/dbos_app.py, wikiapp/page_runs.py
    ("dbos._context", "get_local_dbos_context"),   # agent/durable.py
    ("dbos._utils", "GlobalParams"),               # wikiapp/page_runs.py, assistant/turn_runs.py
    ("dbos._error", "DBOSException"),              # assistant/turn_runs.py
    ("dbos._error", "DBOSStreamTimeoutError"),     # assistant/turn_runs.py
    ("dbos._error", "DBOSNonExistentWorkflowError"),  # sources/runs.py
]


@pytest.mark.parametrize(("module", "name"), SYMBOLS)
def test_private_dbos_symbol_exists(module, name):
    assert hasattr(importlib.import_module(module), name)


def test_attributes_read_off_those_symbols():
    from dbos._utils import GlobalParams
    assert hasattr(GlobalParams, "app_version")
    import inspect

    from dbos import DBOS

    # `_launched` is an instance attribute set in __init__ (agent/dbos_app.launched reads it)
    assert "self._launched" in inspect.getsource(DBOS.__init__)
