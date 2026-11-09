"""Code patches from the harness improver (harness 3b, Task 10): the hooks a round needs to hand a
patch request to the forge, and the request the improver records.

The improver's `propose_patch` tool only records a `PatchProposal` (at most one per run); after the
key path of the round, `harness.round` builds the forge's `PatchDeps` (`PatchHooks.deps_for`, the
coder on the round's recording gateway), a `HarnessPatchGate` (the child on the same recording
gateway, `PatchHooks.child_for`) and runs `forge.patch.round.run_patch`. A pass leaves the patch
`awaiting_approval`: the user approves or discards it in the forge's own review flow
(`POST /wiki/rsi/patches/{id}/approve|discard`, the `forgep:` rows of the change list). Nothing
here lands code.

`build_hooks` is the service's wiring; it returns None (no tool offered, fail closed) when the L2
sandbox (pywin32 Job Objects) or the forge's test database is not available."""

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evowiki.forge.models import ForgeResult, PatchRequest
from evowiki.llm.gateway import Gateway
from evowiki.llm.usage import Account

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from evowiki.agent.trace import TraceSink
    from evowiki.forge.api import PatchDeps
    from evowiki.forge.patch.replay import ReplayResult as ChildResult
    from evowiki.harness.pack import HarnessPack
    from evowiki.harness.replay import Case

log = logging.getLogger("evowiki.harness.patching")

Child = Callable[[str, list[dict]], "ChildResult"]


@dataclass(frozen=True)
class PatchProposal:
    """What the improver's `propose_patch` recorded: the forge request and the citations it
    checked (the patch's evidence)."""

    request: PatchRequest
    citations: tuple[str, ...]


@dataclass(frozen=True)
class PatchHooks:
    """What a harness round needs to propose, build and evaluate a code patch.
    `deps_for(gateway, account, pack)`: the forge's PatchDeps with its coder on `gateway` (the
    round's recording gateway on the patch tier) billed to `account`, and `pack` (the round's base
    pack) lowering the coder's cap. `enabled(conn, user)`: the forge's patch switch
    (`forge.switch.enabled(conn, user, "patch")`, off by default). `child_for(gateway, account)`:
    the gate's replay child (`patch_gate.forge_child` on the deep tier's recording gateway).
    `propose` / `encode`: None = the forge's `propose` / `patch_gate.deep_encoder` (tests swap
    them)."""

    deps_for: Callable[[Gateway, Account, "HarnessPack"], "PatchDeps"]
    enabled: Callable[[Any, str], bool]
    child_for: Callable[[Gateway, Account], Child]
    main_root: Path
    runner: Any = None
    propose: Callable[..., ForgeResult] | None = None
    encode: Callable[["Case", "HarnessPack"], dict] | None = None


def _enabled(conn: Any, user_id: str) -> bool:
    from evowiki.forge import switch
    return switch.enabled(conn, user_id, "patch")


_warned: set[str] = set()


def _unavailable(why: str) -> None:
    """Logged at WARNING the first time per reason in this process (the hooks are built per
    cycle), then at DEBUG."""
    level = logging.DEBUG if why in _warned else logging.WARNING
    _warned.add(why)
    log.log(level, "harness code patches unavailable (the improver gets no propose_patch): %s",
            why)


def default_main_root() -> Path:
    """The git repository the running code is checked out from (src/evowiki/.. /..): forge
    worktrees and branches are added from it."""
    return Path(__file__).resolve().parents[3]


def build_hooks(pool: "ConnectionPool", trace: "TraceSink", *,
                main_root: Path | None = None) -> PatchHooks | None:
    """The service's PatchHooks: an L2 JobRunner, the forge's test database, the repo root. None
    (and logged) when any of them is unavailable: then no round offers `propose_patch`."""
    from evowiki.forge import limits
    from evowiki.forge.binding import ForgeConverse
    from evowiki.forge.patch import testdb
    from evowiki.forge.sandbox import SandboxUnavailable
    from evowiki.harness.patch_gate import forge_child

    try:
        from evowiki.forge.api import PatchDeps  # its build step needs jsonschema
        from evowiki.forge.sandbox.jobs import JobRunner
        runner = JobRunner()
        test_db = testdb.admin_url()
    except (SandboxUnavailable, ImportError, OSError) as e:
        _unavailable(str(e))
        return None
    root = main_root or default_main_root()
    if not (root / ".git").exists():
        _unavailable(f"{root} is not a git checkout")
        return None
    tmp = Path(limits.FORGE_ROOT) / "tmp" / "harness-replay"

    def deps_for(gateway: Gateway, account: Account, pack: "HarnessPack") -> PatchDeps:
        return PatchDeps(pool=pool, converse=ForgeConverse(gateway=gateway, account=account),
                         runner=runner, trace=trace, main_root=root, python=sys.executable,
                         test_db_url=lambda: test_db, pack=pack)

    def child_for(gateway: Gateway, account: Account) -> Child:
        return forge_child(gateway=gateway, account=account, main_root=root, runner=runner,
                           python=sys.executable, test_db_url=test_db, tmp=tmp)

    return PatchHooks(deps_for=deps_for, enabled=_enabled, child_for=child_for, main_root=root,
                      runner=runner)
