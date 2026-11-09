import pytest

from evowiki.forge.patch import testdb
from evowiki.forge.sandbox import SandboxUnavailable


def test_configured_url_is_used(monkeypatch):
    monkeypatch.setenv("EVOWIKI_FORGE_TEST_DATABASE_URL", "postgresql://u@127.0.0.1:5999/postgres")
    monkeypatch.setenv("EVOWIKI_DATABASE_URL", "postgresql://u@127.0.0.1:15432/evowiki")
    assert testdb.admin_url() == "postgresql://u@127.0.0.1:5999/postgres"


@pytest.mark.parametrize("test_url,dev_url", [
    ("postgresql://u@127.0.0.1:15432/postgres", None),                              # the dev port
    ("postgresql://u@127.0.0.1:5999/x", "postgresql://u@127.0.0.1:5999/evowiki"),   # same server
    ("postgresql://u@db:5432/x", "postgresql://u@db/evowiki"),                      # default port
])
def test_dev_server_is_refused(monkeypatch, test_url, dev_url):
    monkeypatch.setenv("EVOWIKI_FORGE_TEST_DATABASE_URL", test_url)
    if dev_url:
        monkeypatch.setenv("EVOWIKI_DATABASE_URL", dev_url)
    else:
        monkeypatch.delenv("EVOWIKI_DATABASE_URL", raising=False)
    with pytest.raises(SandboxUnavailable):
        testdb.admin_url()


def test_no_configured_url_is_unavailable(monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE_TEST_DATABASE_URL", raising=False)
    with pytest.raises(SandboxUnavailable, match="EVOWIKI_FORGE_TEST_DATABASE_URL"):
        testdb.admin_url()
