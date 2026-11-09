import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from evowiki.agent import lifecycle
from evowiki.service.app import create_app


@pytest.fixture(autouse=True)
def undrained():
    lifecycle.DRAINED.clear()
    yield
    lifecycle.DRAINED.clear()


def test_drain_waits_for_stream_body_and_refuses_new_business_requests():
    app = create_app(SimpleNamespace(durable=None))
    entered, release = threading.Event(), threading.Event()

    @app.get("/work")
    def work():
        async def body():
            yield b"first"
            entered.set()
            await asyncio.to_thread(release.wait)
            yield b"last"
        return StreamingResponse(body())

    client = TestClient(app, client=("127.0.0.1", 5000), base_url="http://127.0.0.1")
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(client.get, "/work")
        try:
            assert entered.wait(5)
            assert client.post("/health/drain", json={"on": True}).status_code == 200
            assert client.get("/health").json()["in_flight"] == 1
            refused = client.get("/work")
            assert refused.status_code == 503 and refused.json()["code"] == "service_draining"
            assert refused.headers["retry-after"] == "5"
            assert client.get("/openapi.json").status_code == 200
        finally:
            release.set()
        assert running.result(timeout=5).content == b"firstlast"
    assert client.get("/health").json()["in_flight"] == 0
    client.post("/health/drain", json={"on": False})
    assert client.get("/work").status_code == 200


def test_request_failure_releases_the_admission_counter():
    app = create_app(SimpleNamespace(durable=None))

    @app.get("/broken")
    def broken():
        raise RuntimeError("broken")

    client = TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1")
    assert client.get("/broken").status_code == 500
    assert client.get("/health").json()["in_flight"] == 0
