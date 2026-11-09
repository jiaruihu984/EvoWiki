from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from evowiki.forge.models import HostExchange, RunOutcome, TestReport


class SandboxUnavailable(RuntimeError):
    """The isolated runtime is missing or unusable. Never fall back to running unsandboxed."""


# A host port runs one of the agent's own read-only tools on the host side (with the run's
# context, which never crosses into the guest) and returns the exchange; only `content` (text)
# enters the guest.
HostPort = Callable[[str, dict[str, Any]], HostExchange]


@dataclass(frozen=True)
class HostSpec:
    allowed: tuple[str, ...]          # the tool's host_tools (from its candidate spec, never its files)
    port: HostPort


class Sandbox(Protocol):
    policy: str                                   # Codex-style: "read-only"

    def run_tool(self, files: Mapping[str, str], args: dict[str, Any],
                 host: HostSpec | None = None) -> RunOutcome: ...

    def run_tests(self, files: Mapping[str, str]) -> TestReport: ...
