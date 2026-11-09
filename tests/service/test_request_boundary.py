"""Local API trust boundary: reject browser-controlled authorities before side effects."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from evowiki.agent.lifecycle import DRAINED
from evowiki.service.app import create_app


@pytest.fixture
def app():
    before = DRAINED.is_set()
    DRAINED.clear()
    application = create_app(SimpleNamespace(forget=None, durable=None, pool=None))
    yield application
    DRAINED.set() if before else DRAINED.clear()


@pytest.mark.parametrize("host", ["evil.example:8002", "localhost.evil.example", "127.0.0.1.evil.example", "www.localhost", "192.168.1.2"])
def test_foreign_host_cannot_change_state(app, host):
    client = TestClient(app, base_url="http://127.0.0.1:8002", client=("127.0.0.1", 1234))
    result = client.post("/health/drain", headers={"Host": host}, json={"on": True})
    assert result.status_code == 400
    assert not DRAINED.is_set()


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://127.0.0.1:8003", "https://127.0.0.1:8002", "app://obsidian.md.evil.example", "http://127.0.0.1:8002/", "http://user@127.0.0.1:8002", "http://127.0.0.1:8002?query=1"])
def test_foreign_origin_cannot_change_state(app, origin):
    client = TestClient(app, base_url="http://127.0.0.1:8002", client=("127.0.0.1", 1234))
    result = client.post("/health/drain", headers={"Origin": origin}, json={"on": True})
    assert result.status_code == 403
    assert result.json()["code"] == "untrusted_origin"
    assert not DRAINED.is_set()


@pytest.mark.parametrize("base_url", ["http://localhost:8002", "http://127.0.0.1:8002", "http://[::1]:8002", "http://localhost:80"])
@pytest.mark.parametrize("origin_kind", ["native", "same_origin", "obsidian"])
def test_local_and_obsidian_mutations_still_work(app, base_url, origin_kind):
    origin = "app://obsidian.md" if origin_kind == "obsidian" else base_url.removesuffix(":80")
    headers = {} if origin_kind == "native" else {"Origin": origin}
    client = TestClient(app, base_url=base_url, client=("127.0.0.1", 1234))
    result = client.post("/health/drain", headers=headers, json={"on": True})
    assert result.status_code == 200
    assert DRAINED.is_set()


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_origin_guard_applies_to_all_mutation_methods(app, method):
    client = TestClient(app, base_url="http://127.0.0.1")
    result = getattr(client, method)("/not-a-route", headers={"Origin": "http://evil.example"})
    assert result.status_code == 403


def test_host_validation_also_precedes_safe_requests(app):
    client = TestClient(app, base_url="http://evil.example")
    assert client.get("/health").status_code == 400


def test_port_zero_is_not_the_default_http_port(app):
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234))
    result = client.post("/health/drain", headers={"Origin": "http://localhost:0"}, json={"on": True})
    assert result.status_code == 403
    assert not DRAINED.is_set()


def test_multiple_origins_are_rejected_before_state_change(app):
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234))
    result = client.post("/health/drain", headers=[("Origin", "http://localhost"), ("Origin", "app://obsidian.md")], json={"on": True})
    assert result.status_code == 403
    assert not DRAINED.is_set()
