"""S6: real ASGI response owners across headers, completion, failure and cancellation."""

import asyncio
import threading
from collections import Counter

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from evowiki.service.drain import DrainMiddleware, RequestGate

COVERAGE = Counter()


class DrainProtocol(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.gate = RequestGate(threading.Event())
        self.closed = False
        self.active = {}
        self.next_id = 0

        async def backend(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            if scope["path"] == "/health":
                await send({"type": "http.response.body", "body": b"ok"})
                return
            await scope["release"].wait()
            if scope["fail"]:
                raise RuntimeError("scripted body failure")
            await send({"type": "http.response.body", "body": b"last"})

        self.app = DrainMiddleware(backend, self.gate)

    def advance(self):
        self.loop.run_until_complete(asyncio.sleep(0))

    @rule()
    def begin(self):
        messages = []
        release = asyncio.Event()
        scope = {"type": "http", "path": "/work", "release": release, "fail": False}

        async def send(message):
            messages.append(message)

        async def receive():
            return {"type": "http.request", "body": b""}

        task = self.loop.create_task(self.app(scope, receive, send))
        self.advance()
        if self.closed:
            assert task.done() and messages[0]["status"] == 503
            task.result()
            COVERAGE["refused"] += 1
        else:
            assert messages[0]["status"] == 200 and not task.done()
            self.next_id += 1
            self.active[self.next_id] = (task, release, scope)
            COVERAGE["headers_open_body"] += 1

    @rule(on=st.booleans())
    def drain(self, on):
        self.gate.set_drained(on)
        self.closed = on
        COVERAGE["drain" if on else "reopen"] += 1

    @precondition(lambda self: bool(self.active))
    @rule(
        index=st.integers(min_value=0, max_value=10),
        outcome=st.sampled_from(["done", "error", "cancel"]),
    )
    def finish(self, index, outcome):
        key = sorted(self.active)[index % len(self.active)]
        task, release, scope = self.active.pop(key)
        if outcome == "cancel":
            task.cancel()
        else:
            scope["fail"] = outcome == "error"
            release.set()
        self.advance()
        assert task.done()
        if outcome == "cancel":
            assert task.cancelled()
        elif outcome == "error":
            assert isinstance(task.exception(), RuntimeError)
        else:
            assert task.exception() is None
        COVERAGE[outcome] += 1

    @rule()
    def health_is_available(self):
        messages = []

        async def send(message):
            messages.append(message)

        async def receive():
            return {"type": "http.request", "body": b""}

        self.loop.run_until_complete(self.app({"type": "http", "path": "/health"}, receive, send))
        assert messages[0]["status"] == 200
        COVERAGE["health"] += 1

    @invariant()
    def owners_match_actual_active_responses(self):
        assert self.gate.active == len(self.active)
        assert self.gate.drained.is_set() == self.closed

    def teardown(self):
        for task, _, _ in self.active.values():
            task.cancel()
        if self.active:

            async def settle():
                await asyncio.gather(
                    *(task for task, _, _ in self.active.values()), return_exceptions=True
                )

            self.loop.run_until_complete(settle())
        assert self.gate.active == 0
        self.loop.close()


TestDrainProtocol = DrainProtocol.TestCase
TestDrainProtocol.settings = settings(max_examples=200, stateful_step_count=50, deadline=None)
