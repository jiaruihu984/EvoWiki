"""The SSE routes on FastAPI's native SSE (`service.sse`): the wire format the plugin parses, the
keep-alive comment during a silence, and the producer closed when the client leaves - for every
streaming route."""
import time
from collections.abc import Iterator
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.testclient import TestClient
from pydantic import BaseModel

from evowiki.assistant.contracts import TextDelta
from evowiki.service import routes_sources, routes_wiki
from evowiki.service.app import create_app
from evowiki.service.sse import closing_stream, event
from tests.service.conftest import leave_mid_stream, parse_sse

U1 = {"X-EvoWiki-User": "u1"}


class Tick(BaseModel):
    n: int
    text: str = "主成分"


class Producer:
    """An endless item source that records whether it was closed; the test keeps the reference,
    so only an explicit close (not garbage collection) can close it."""

    def __init__(self, item=None):
        self.closed = False
        self.started = False
        self.item = item or (lambda n: ("section_delta", Tick(n=n)))
        self.gen = self._run()

    def _run(self) -> Iterator:
        self.started = True
        n = 0
        try:
            while True:
                yield self.item(n)
                n += 1
                time.sleep(0.01)
        finally:
            self.closed = True

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.gen)

    def close(self):
        self.gen.close()


def _tiny_app(source, *sources):
    app = FastAPI()

    def events() -> Iterator[Iterator[ServerSentEvent]]:
        yield from closing_stream(source(), *sources)

    @app.get("/s", response_class=EventSourceResponse)
    def stream(ev: Annotated[Iterator[ServerSentEvent], Depends(events)]) -> Iterator[ServerSentEvent]:
        yield from ev

    return app


def test_frames_are_event_and_utf8_json_data_lines_with_an_optional_id():
    def source():
        yield event("token", {"text": "主成分"})
        yield event("done", {"ok": True}, id="m:1")

    r = TestClient(_tiny_app(source)).get("/s")
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text == ('event: token\ndata: {"text": "主成分"}\n\n'
                      'event: done\ndata: {"ok": true}\nid: m:1\n\n')
    assert parse_sse(r.text) == [("token", {"text": "主成分"}), ("done", {"ok": True})]


def test_a_silence_gets_keep_alive_comment_frames(monkeypatch):
    import fastapi.routing
    monkeypatch.setattr(fastapi.routing, "_PING_INTERVAL", 0.05)

    def source():
        yield event("planning", {})
        time.sleep(0.4)                  # a page write thinking
        yield event("done", {})

    text = TestClient(_tiny_app(source)).get("/s").text
    assert ": ping\n\n" in text
    assert [n for n, _ in parse_sse(text)] == ["planning", "done"]


def test_leaving_closes_the_events_and_their_sources():
    p, q = Producer(), Producer()
    framed = (event(n, m.model_dump()) for n, m in p)
    sent = leave_mid_stream(_tiny_app(lambda: framed, p, q), "GET", "/s")
    assert parse_sse(b"".join(m.get("body", b"") for m in sent).decode())[0][0] == "section_delta"
    assert framed.gi_frame is None                 # the events generator is closed
    assert p.closed and not q.started              # its source too; q (never started) just skipped


def test_every_close_runs_when_one_raises():
    p = Producer()

    class Bad:
        def close(self):
            raise RuntimeError("close failed")

    gen = closing_stream(iter([]), Bad(), p)
    next(gen)
    next(p)
    with pytest.raises(RuntimeError):
        gen.close()
    assert p.closed


# ---- every streaming route closes its producer when the client leaves ----

def _wiki_app(monkeypatch, **svc):
    monkeypatch.setattr(routes_wiki, "require", lambda gw, uid: object())
    monkeypatch.setattr(routes_wiki, "rsi_tick", lambda svc, uid: None)
    base = {"gateway": object(), "pool": None, "clock": lambda: None, "new_id": lambda p: p,
            "writing": lambda: None, "runs": None, "bot_notify": None}
    return create_app(SimpleNamespace(**{**base, **svc}))


def _body_events(sent):
    return parse_sse(b"".join(m.get("body", b"") for m in sent
                              if m["type"] == "http.response.body").decode())


@pytest.mark.parametrize("at_once", [False, True])
def test_organize_closes_its_run_follower(monkeypatch, at_once):
    p = Producer()
    monkeypatch.setattr(routes_wiki, "organize_stream", lambda *a, **k: p)
    sent = leave_mid_stream(_wiki_app(monkeypatch), "POST", "/wiki/pages/p1/organize",
                            headers=U1, at_once=at_once)
    assert sent[0]["status"] == 200
    if not at_once:
        assert _body_events(sent)[0] == ("section_delta", {"n": 0, "text": "主成分"})
    assert p.closed or not p.started


def test_run_stream_closes_its_watcher(monkeypatch):
    p = Producer()
    state = SimpleNamespace(pages={"p1": object()})
    monkeypatch.setattr(routes_wiki, "PgRepo",
                        lambda pool: SimpleNamespace(load=lambda uid: SimpleNamespace(state=state)))
    row = SimpleNamespace(run_id="r1", user_id="u1", page_id="p1", status="writing", discarded_at=None)
    runs = SimpleNamespace(store=SimpleNamespace(get=lambda run_id: row),
                           follow=lambda run_id, consume: p)
    sent = leave_mid_stream(_wiki_app(monkeypatch, runs=runs), "GET", "/wiki/pages/p1/run/stream?run_id=r1",
                            headers=U1)
    assert sent[0]["status"] == 200 and _body_events(sent)
    assert p.started and p.closed


def test_source_progress_closes_its_follower(monkeypatch):
    p = Producer()
    monkeypatch.setattr("evowiki.service.app._request_language", lambda _request: "zh")
    monkeypatch.setattr(routes_sources, "_live", lambda conn, uid, sid: None)
    svc = {"pool": SimpleNamespace(connection=nullcontext),
           "sources": SimpleNamespace(follow=lambda uid, sid: p)}
    sent = leave_mid_stream(_wiki_app(monkeypatch, **svc), "GET", "/wiki/sources/s1/progress",
                            headers=U1)
    assert sent[0]["status"] == 200 and _body_events(sent)
    assert p.started and p.closed


class Turn(Producer):
    """An assistant turn: events with a `type`, a cursor for the SSE id, and an idempotent close
    that frees the session lock (also for a turn never iterated)."""

    def __init__(self):
        super().__init__(item=lambda n: TextDelta(at=None, text=f"答{n}"))
        self.message_id, self.cursor, self.lock_freed = "msg_1", 0, False

    def close(self):
        self.lock_freed = True
        super().close()


@pytest.mark.parametrize("at_once", [False, True])
def test_assistant_send_closes_the_turn(at_once):
    t = Turn()
    app = create_app(SimpleNamespace(assistant=SimpleNamespace(send=lambda uid, sid, body: t)))
    sent = leave_mid_stream(app, "POST", "/assistant/sessions/s/messages", headers=U1,
                            body=b'{"text": "x"}', at_once=at_once)
    assert sent[0]["status"] == 200
    if not at_once:
        assert _body_events(sent)[0][0] == "status"   # the resume point before the model's first word
    assert t.lock_freed and (t.closed or not t.started)


def test_assistant_resume_closes_the_turn():
    t = Turn()
    app = create_app(SimpleNamespace(assistant=SimpleNamespace(resume=lambda uid, sid, mid, after: t)))
    sent = leave_mid_stream(app, "GET", "/assistant/sessions/s/events?last_event_id=msg_1:0",
                            headers=U1)
    assert sent[0]["status"] == 200 and _body_events(sent)
    assert t.lock_freed and t.closed
