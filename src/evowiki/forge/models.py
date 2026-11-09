"""Data of the tool forge (design 2026-10-22 §6.1). Pure: no IO."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evowiki.forge.limits import HOST_READ_TOOLS

NAME = r"^[a-z][a-z0-9_]{1,40}$"
ForgeKind = Literal["tool", "patch"]
CITE_KINDS = ("signal:", "trace:", "cmp:")
# Names of the fixed meta tools a run offers (design 2026-10-22 §6.2); no forged tool may take one.
RESERVED_NAMES = frozenset({"call_tool", "find_tool", "request_tool"})


class _F(BaseModel):
    model_config = ConfigDict(frozen=True)


class HostExchange(_F):
    """One host read-only call of a forged tool and its result (plan P2 T5)."""
    tool: str
    args: dict[str, Any]
    ok: bool
    content: str                      # the host tool's ToolResult.content (or error text)


class ToolExample(_F):
    args: dict[str, Any]
    expect: dict[str, Any]
    host: tuple[HostExchange, ...] = ()   # cassette: the exact host calls this example makes


class ToolRequest(_F):
    name: str = Field(pattern=NAME)
    title: str = Field(min_length=2, max_length=16)   # learner-facing: 新增了工具「title」
    does: str = Field(default="", max_length=80)      # learner-facing: what it does, in plain words
    purpose: str = Field(min_length=1, max_length=2000)
    agent: Literal["deep", "assistant", "bot"]
    examples: tuple[ToolExample, ...] = Field(min_length=1)
    scope: Literal["user", "global"] = "user"
    parent: str | None = None
    holdout: tuple[ToolExample, ...] = ()   # requester-supplied held-out examples (LATM)
    host_tools: tuple[str, ...] = ()  # the agent's read-only tools the forged tool may call

    @model_validator(mode="after")
    def _host_tools_allowed(self) -> "ToolRequest":
        allowed = HOST_READ_TOOLS.get(self.agent, ())
        bad = sorted(set(self.host_tools) - set(allowed))
        if bad:
            raise ValueError(f"host_tools {bad} are not read-only tools of the {self.agent} agent")
        stray = sorted({h.tool for e in (*self.examples, *self.holdout) for h in e.host}
                       - set(self.host_tools))
        if stray:
            raise ValueError(f"examples call host tools {stray} not in host_tools")
        return self

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v in RESERVED_NAMES:
            raise ValueError(f"{v!r} is a reserved tool name")
        return v


class PatchRequest(_F):               # L2 (phase 3)
    goal: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=2, max_length=16)     # learner-facing: 修改了代码：<title>
    failing_case: str = Field(min_length=1, max_length=4000)
    paths_hint: tuple[str, ...] = ()
    parent: str | None = None


class Evidence(_F):
    citations: tuple[str, ...] = Field(min_length=1)
    note: str = ""

    @field_validator("citations")
    @classmethod
    def _known(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [c for c in v if not c.startswith(CITE_KINDS) or c.split(":", 1)[1] == ""]
        if bad:
            raise ValueError(f"unknown citation(s): {bad}")
        return v


class SandboxStats(_F):
    wall_s: float
    fuel_used: int | None = None
    exit: Literal["ok", "error", "timeout", "fuel", "memory", "output_cap"]


class RunOutcome(_F):                 # one tool call in the sandbox
    ok: bool
    output: dict[str, Any] | None = None
    error: str = ""                   # traceback tail / limit reason, <= 2000 chars
    stats: SandboxStats
    host_calls: tuple[HostExchange, ...] = ()


class TestCase(_F):
    __test__ = False                  # not a pytest class
    name: str
    passed: bool
    detail: str = ""


class TestReport(_F):
    __test__ = False
    cases: tuple[TestCase, ...]
    stats: SandboxStats
    error: str = ""                   # collection/import failure

    @property
    def passed(self) -> int:
        return sum(c.passed for c in self.cases)

    @property
    def failed(self) -> int:
        return len(self.cases) - self.passed


class BuildReport(_F):
    ok: bool
    tests_passed: int
    tests_failed: int
    examples_passed: int
    examples_failed: int
    holdout_passed: int = 0
    holdout_failed: int = 0
    holdout_total: int = 0
    lint_ok: bool
    schema_ok: bool
    policy_violations: tuple[str, ...] = ()
    sandbox: SandboxStats
    run_id: str = ""
    cost_usd: float | None = None
    reason: str                       # 人话, shown in ? tooltip


class PatchBuildReport(BuildReport):
    """An L2 build (plan P3 T9). `tests_passed` = FAIL_TO_PASS + still-passing PASS_TO_PASS,
    `tests_failed` = new tests not passing + broken PASS_TO_PASS; `examples_*` are 0."""
    fail_to_pass: tuple[str, ...] = ()
    broken: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    diff_lines: int = 0
    commit: str = ""
    base_commit: str = ""


class ForgeResult(_F):
    candidate_id: str
    kind: ForgeKind
    build: BuildReport


class ForgeDisabled(RuntimeError): ...
class ForgeNotFound(LookupError): ...
class ForgeMisconfigured(RuntimeError): ...
class PromoteRefused(RuntimeError): ...


class PatchesOff(PromoteRefused):
    """Approve refused because code patches are switched off; HTTP 409 with `code: patches_off`."""
    error_code = "patches_off"


class ForgeForbidden(PermissionError): ...   # e.g. a global rollback by someone not the operator
