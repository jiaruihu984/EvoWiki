import json

import pytest
from fastapi.testclient import TestClient

from evowiki.retrieval.embed import HashingEmbedder
from evowiki.service.app import create_app
from evowiki.service.deps import Services, import_deps
from evowiki.sources.runs import InlineSourceRuns
from evowiki.store.vectors import index_segments
from evowiki.wikiapp.page_run_store import PgPageRunStore
from evowiki.wikiapp.page_runs import InlinePageRuns
from evowiki.wikiapp.repo import PgRepo
from tests.runtime.helpers import T0, FixedClock, seed_runtime
from tests.wikiapp.fake_writer import writing

SURVEY = {"math": "university", "ml": "beginner", "dl": "none"}


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n:04d}"


@pytest.fixture
def services(pool) -> Services:
    ctx = seed_runtime(pool)
    emb = HashingEmbedder()
    with pool.connection() as conn:
        index_segments(conn, ctx.content, emb)
    # pages are written by the scripted page writer (the test's Script), runs inline
    svc = Services(pool=pool, ctx=ctx, clock=FixedClock(T0), embedder=emb, gateway=None,
                   new_id=Ids(),
                   runs=InlinePageRuns(store=PgPageRunStore(pool)),
                   page_writing=writing(PgRepo(pool)))
    svc.sources = InlineSourceRuns(import_deps(svc))   # source imports run inline, no LLM
    return svc


@pytest.fixture
def client(services) -> TestClient:
    return TestClient(create_app(services))


def H(uid: str) -> dict[str, str]:
    return {"X-EvoWiki-User": uid}


def signup(client: TestClient, uid: str = "u1", condition: str = "S2",
           survey: dict | None = None, *, lang: str = "zh") -> str:
    """Register this suite's Chinese learning account; English cases opt in explicitly."""
    r = client.post("/users", json={"user_id": uid, "survey": survey or SURVEY,
                                    "condition": condition, "lang": lang})
    assert r.status_code == 201, r.text
    return uid


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """(event, data) per event; comment frames (FastAPI's `: ping` keep-alive) are skipped."""
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines()
                     if ": " in line and not line.startswith(":"))
        if "data" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def leave_mid_stream(app, method: str, path: str, *, body: bytes = b"",
                     headers: dict[str, str] | None = None, at_once: bool = False) -> list[dict]:
    """Drives `app` as uvicorn does (ASGI http spec 2.3, which has StreamingResponse listen for
    the disconnect) and leaves: after the first event arrives, or before any (`at_once`). Returns
    the ASGI messages sent. An explicit loop, not asyncio.run (whose shutdown_asyncgens would
    finalize abandoned iterators itself and hide a missing close); callers keep a reference to
    their producer, so only an explicit close can run its `finally`."""
    import asyncio

    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if body:
        hdrs.append((b"content-type", b"application/json"))
    path, _, query = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
             "headers": hdrs,
             "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 50000)}
    sent: list[dict] = []

    async def main() -> None:
        first = asyncio.Event()
        asked = False

        async def receive() -> dict:
            nonlocal asked
            if not asked:
                asked = True
                return {"type": "http.request", "body": body, "more_body": False}
            if not at_once:
                await first.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first.set()

        await asyncio.wait_for(app(scope, receive, send), timeout=20)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
    return sent
