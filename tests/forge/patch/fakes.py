"""Fakes for the L2 patch tests (plan P3 T4 Step 7): a scripted `Runner` and a junit writer, so the
SWE and build tests never spawn pytest."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from evowiki.forge.models import SandboxStats
from evowiki.forge.sandbox.jobs import ProcOutcome


def outcome(exit_code: int | None = 0, output: str = "", exit: str = "ok", wall_s: float = 0.1,
            peak_memory: int | None = None) -> ProcOutcome:
    return ProcOutcome(exit_code=exit_code, output=output,
                       stats=SandboxStats(wall_s=wall_s, exit=exit), peak_memory=peak_memory)


def write_junit(path: Path, results: Mapping[str, str]) -> None:
    """Write xunit1 junit XML (with `file=`, as `pytest_argv` asks pytest for) for
    {nodeid: passed|failed|error|skipped}."""
    suite = ElementTree.Element("testsuite", name="pytest", tests=str(len(results)))
    for nodeid, result in results.items():
        file, *rest = nodeid.split("::")
        module = file.removesuffix(".py").replace("/", ".")
        classname = ".".join([module, *rest[:-1]])
        case = ElementTree.SubElement(suite, "testcase", classname=classname, name=rest[-1], file=file)
        if result == "failed":
            ElementTree.SubElement(case, "failure", message="failed").text = "assert False"
        elif result == "error":
            ElementTree.SubElement(case, "error", message="error").text = "error"
        elif result == "skipped":
            ElementTree.SubElement(case, "skipped", message="skipped")
        elif result != "passed":
            raise ValueError(result)
    root = ElementTree.Element("testsuites")
    root.append(suite)
    path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


Match = Callable[[tuple[str, ...], Path], bool]


@dataclass
class FakeRunner:
    """Scripted Runner: `script` maps a predicate over argv to a ProcOutcome and an optional junit
    writer (path, {nodeid: outcome}) so swe/build tests never spawn pytest."""
    script: list[tuple[Match, ProcOutcome, dict[str, str] | None]]
    policy: str = "workspace-write"
    calls: list[tuple[tuple[str, ...], Path, dict]] = field(default_factory=list)

    def run(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
            wall_s: float = 600.0) -> ProcOutcome:
        argv = tuple(argv)
        self.calls.append((argv, Path(cwd), dict(env)))
        for match, result, junit in self.script:
            if match(argv, Path(cwd)):
                if junit is not None:
                    path = Path(next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml=")))
                    write_junit(path, junit)
                return result
        raise AssertionError(f"unscripted command {argv}")


@dataclass
class FakeBackend:
    """Scripted `forge.patch.live.Backend` over a live worktree (plan P3 T8). `boots[i]` is what the
    i-th restart does: "ok" (serves the live HEAD), "down" (/health never answers) or "stale" (the
    old process keeps answering with the commit it served before). Carries its own clock: pass
    `sleep=backend.sleep, clock=backend.clock` to the code under test."""
    live: object                                  # forge.patch.git.Git
    in_flight: int = 0
    boots: list[str] = field(default_factory=list)
    smoke_ok: bool = True
    restarts: int = 0
    drains: list[bool] = field(default_factory=list)
    t: float = 0.0
    serving: str = ""
    up: bool = True

    def __post_init__(self) -> None:
        self.serving = self.live.head()

    def health(self) -> dict | None:
        if not self.up:
            return None
        return {"ok": True, "commit": self.serving, "in_flight": self.in_flight,
                "drained": bool(self.drains and self.drains[-1])}

    def drain(self, on: bool) -> None:
        self.drains.append(on)

    def restart(self) -> None:
        mode = self.boots[self.restarts] if self.restarts < len(self.boots) else "ok"
        self.restarts += 1
        self.up = mode != "down"
        if mode == "ok":
            self.serving = self.live.head()

    def smoke(self) -> bool:
        return self.up and self.smoke_ok

    def sleep(self, s: float) -> None:
        self.t += s

    def clock(self) -> float:
        return self.t
