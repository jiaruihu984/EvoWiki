"""Deleting in Phoenix over its REST API (checked against arize-phoenix 20.26.1's
schemas/openapi.json, 2026-10-29):

- `DELETE /v1/traces/{trace_identifier}`: the identifier may be an OTel trace id (hex); every span
  of the trace goes. 204, or 404 when it is not there (already deleted, or never arrived).
- `POST /v1/sessions/delete` `{"session_identifiers": [...]}`: by the session.id strings the spans
  carried; their traces, spans and annotations are cascade deleted; unknown ids are skipped. 204.

Both are exact, so deletion does not rest on retention alone; the server's own retention
(`PHOENIX_DEFAULT_RETENTION_POLICY_DAYS`) and the exporter's are the second line."""

from collections.abc import Sequence
from typing import Protocol

import httpx


class PhoenixError(RuntimeError):
    """Phoenix did not confirm a delete: the ledger rows stay and the next tick tries again."""


class Deleter(Protocol):
    def delete_trace(self, trace_id: str) -> None: ...

    def delete_sessions(self, session_keys: Sequence[str]) -> None: ...


class PhoenixDeleter:
    def __init__(self, base_url: str, *, api_key: str | None = None, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers,
                                  timeout=timeout, transport=transport)

    def delete_trace(self, trace_id: str) -> None:
        r = self._http.delete(f"/v1/traces/{trace_id}")
        if r.status_code not in (204, 404):
            raise PhoenixError(f"delete trace: HTTP {r.status_code}")

    def delete_sessions(self, session_keys: Sequence[str]) -> None:
        if not session_keys:
            return
        r = self._http.post("/v1/sessions/delete",
                            json={"session_identifiers": list(session_keys)})
        if r.status_code != 204:
            raise PhoenixError(f"delete sessions: HTTP {r.status_code}")

    def close(self) -> None:
        self._http.close()
