"""The forge's own throwaway Postgres for sandboxed tests (plan P3 T4). The child gets only this
cluster's URL (as EVOWIKI_TEST_DATABASE_URL); `tests/pg.py` inside the child builds its template
and fresh databases there as usual. Never the dev DB: refuse when it is the same server (fail
closed with SandboxUnavailable)."""

import os

from psycopg.conninfo import conninfo_to_dict

from evowiki.forge.sandbox import SandboxUnavailable

ENV = "EVOWIKI_FORGE_TEST_DATABASE_URL"
DEV_PORT = 15432                    # = tests/pg.py DEV_PORT = scripts/dev_db.py DEFAULT_PORT (the shared dev server)


def _server_of(url: str) -> tuple[str | None, str]:
    d = conninfo_to_dict(url)
    return d.get("host"), str(d.get("port") or 5432)


def check_not_dev_server(test_url: str, dev_url: str | None) -> None:
    """Same semantics as tests/pg.py:check_not_dev_server (copied: src never imports tests)."""
    host, port = _server_of(test_url)
    same = dev_url is not None and (host, port) == _server_of(dev_url)
    if same or port == str(DEV_PORT):
        raise SandboxUnavailable(f"forge test database points at the shared dev server ({host}:{port})")


def admin_url() -> str:
    """The forge's own throwaway cluster: EVOWIKI_FORGE_TEST_DATABASE_URL. The runtime never starts
    a bundled Postgres (that server package is a dev/test dependency only): without the variable
    there are no sandboxed tests, refused plainly (`SandboxUnavailable`). A caller with its own
    cluster injects it through `PatchDeps.test_db_url`."""
    dev = os.environ.get("EVOWIKI_DATABASE_URL") or None
    configured = os.environ.get(ENV)
    if not configured:
        raise SandboxUnavailable(f"forge test database unavailable: {ENV} is not set")
    check_not_dev_server(configured, dev)
    return configured
