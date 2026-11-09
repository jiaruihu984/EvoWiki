"""Browser request boundary for the loopback-only desktop service."""

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# TrustedHostMiddleware's IPv6 parser retains brackets around the literal.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
OBSIDIAN_ORIGIN = "app://obsidian.md"


def _web_origin(value: str) -> tuple[str, str, int] | None:
    try:
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                or url.password is not None or url.path or url.query or url.fragment):
            return None
        port = url.port
        return url.scheme, url.hostname, port if port is not None else (443 if url.scheme == "https" else 80)
    except ValueError:
        return None


class OriginMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            headers = Headers(scope=scope)
            origins = headers.getlist("origin")
            # requestUrl and the Node SSE transport are native HTTP clients with no Origin.
            if origins:
                own = _web_origin(f"{scope['scheme']}://{headers.get('host', '')}")
                allowed = len(origins) == 1 and (
                    origins[0] == OBSIDIAN_ORIGIN
                    or (own is not None and _web_origin(origins[0]) == own))
                if not allowed:
                    await JSONResponse({"detail": "Untrusted request origin.",
                                        "code": "untrusted_origin", "params": {}},
                                       status_code=403)(scope, receive, send)
                    return
        await self.app(scope, receive, send)
