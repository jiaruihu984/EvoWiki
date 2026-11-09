"""Close HTTP admission atomically, and wait for complete responses before restarting."""

import threading

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestGate:
    def __init__(self, drained: threading.Event):
        self.drained = drained
        self._lock = threading.Lock()
        self._active = 0

    def enter(self) -> bool:
        with self._lock:
            if self.drained.is_set():
                return False
            self._active += 1
            return True

    def leave(self) -> None:
        with self._lock:
            self._active -= 1

    def set_drained(self, on: bool) -> None:
        with self._lock:
            self.drained.set() if on else self.drained.clear()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active


class DrainMiddleware:
    def __init__(self, app: ASGIApp, gate: RequestGate):
        self.app, self.gate = app, gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in (
                "/health", "/health/ready", "/health/drain", "/openapi.json"):
            await self.app(scope, receive, send)
            return
        if not self.gate.enter():
            await JSONResponse({"detail": "服务正在更新", "code": "service_draining"},
                               status_code=503, headers={"Retry-After": "5"})(scope, receive, send)
            return
        try:
            # Includes streaming bodies and background tasks, not just response headers.
            await self.app(scope, receive, send)
        finally:
            self.gate.leave()
