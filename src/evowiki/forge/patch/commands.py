"""The L2 command allowlist and child environment (design §4, ruling R4; plan P3 T4). No
arbitrary shell: Windows without admin cannot hard-isolate files or network, so only these argv
shapes run, each rewritten to a fixed real argv (Codex CLI keeps approval and sandbox separate;
this is the 'what may run at all' half). Nothing here spawns a process: the `Runner` does."""

import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from evowiki.harness.evalset_paths import sealed_dir

ALLOWED_TEXT = ("只能运行：pytest <tests/ 下的文件或用例> [-x] [-q] [-v] [-k 表达式] [--tb=short|line|no]；"
                "ruff check [路径]；git diff [--stat|--name-only] [路径]；git status [--short]；"
                "git log [--oneline] [-n N]")
PYTEST_FLAGS = frozenset({"-x", "-q", "-v", "--tb=short", "--tb=line", "--tb=no"})
ALLOW_HOSTS = "127.0.0.1,localhost,::1"
ENV_PASS = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "OS")
K_MAX_CHARS = 200
_NO_READ_SITE = Path(__file__).resolve().parents[1] / "sandbox" / "site"
GIT_LOG_MAX = 50
GIT_FLAGS = {"diff": frozenset({"--stat", "--name-only"}), "status": frozenset({"--short"}),
             "log": frozenset({"--oneline"})}
_K_OK = re.compile(r"^[\w .\[\]()\-]+$")        # a -k expression: names, and/or/not, brackets


class CmdRefused(ValueError):
    """The argv is not on the allowlist; the message lists what is allowed."""


@dataclass(frozen=True)
class Cmd:
    kind: Literal["pytest", "ruff", "git"]
    argv: tuple[str, ...]              # the real argv run by the Runner
    junit: Path | None                 # pytest only


def _refuse(why: str) -> CmdRefused:
    return CmdRefused(f"{why}。{ALLOWED_TEXT}")


def _relative(target: str) -> PurePosixPath | None:
    """A plain repo-relative POSIX path, or None: no backslash, drive, colon, absolute or `..`."""
    if (not target or "\\" in target or ":" in target or target.startswith("-")
            or any(c in target for c in "\r\n\t\0")):
        return None
    p = PurePosixPath(target)
    if p.is_absolute() or ".." in p.parts:
        return None
    return p


def _in_tests(root: Path, target: str) -> bool:
    path, _, rest = target.partition("::")
    if any(c in rest for c in "\r\n\0"):
        return False
    p = _relative(path)
    return p is not None and p.parts[:1] == ("tests",) and (root / path).exists()


def _in_repo_code(root: Path, target: str) -> bool:
    p = _relative(target)
    return p is not None and p.parts[:1] in (("src",), ("tests",)) and (root / target).exists()


def pytest_argv(targets: Sequence[str], *, python: str, junit: Path) -> tuple[str, ...]:
    """The one pytest argv the forge runs (also used by swe.py): no cache, network off except
    loopback, xunit1 junit with `file=` attributes, the repo's addopts cleared."""
    return (python, "-m", "pytest", "-p", "no:cacheprovider", "--disable-socket",
            f"--allow-hosts={ALLOW_HOSTS}", f"--junitxml={junit}", "-o", "junit_family=xunit1",
            "-o", "addopts=", "-q", *targets)


def _pytest(rest: list[str], *, root: Path, python: str, junit_dir: Path) -> Cmd:
    targets: list[str] = []
    flags: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "-k":
            expr = rest[i + 1] if i + 1 < len(rest) else ""
            if not expr or len(expr) > K_MAX_CHARS or not _K_OK.match(expr):
                raise _refuse(f"不允许的 -k 表达式 {expr!r}")
            flags += ["-k", expr]
            i += 2
            continue
        if a in PYTEST_FLAGS:
            flags.append(a)
        elif not a.startswith("-") and _in_tests(root, a):
            targets.append(a)
        else:
            raise _refuse(f"不允许的参数 {a!r}")
        i += 1
    junit = junit_dir / f"{uuid4().hex}.xml"
    chosen = targets or ["tests"]
    base = pytest_argv(chosen, python=python, junit=junit)
    return Cmd("pytest", (*base[:-len(chosen)], *flags, *chosen), junit)


def _ruff(rest: list[str], *, root: Path, python: str) -> Cmd:
    if rest[:1] != ["check"]:
        raise _refuse("ruff 只能用 check")
    paths = rest[1:]
    for p in paths:
        if not _in_repo_code(root, p):
            raise _refuse(f"不允许的参数 {p!r}")
    return Cmd("ruff", (python, "-m", "ruff", "check", "--no-cache", *paths), None)


def _git(rest: list[str]) -> Cmd:
    if not rest or rest[0] not in GIT_FLAGS:
        raise _refuse("git 只能用 diff、status、log")
    sub, args = rest[0], rest[1:]
    flags: list[str] = []
    paths: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if sub == "log" and a == "-n":
            n = args[i + 1] if i + 1 < len(args) else ""
            if not n.isdigit() or not 1 <= int(n) <= GIT_LOG_MAX:
                raise _refuse(f"-n 只能是 1 到 {GIT_LOG_MAX}")
            flags += ["-n", n]
            i += 2
            continue
        if a in GIT_FLAGS[sub]:
            flags.append(a)
        elif _relative(a) is not None:
            paths.append(a)
        else:
            raise _refuse(f"不允许的参数 {a!r}")
        i += 1
    extra = ("--no-ext-diff", "--no-textconv") if sub == "diff" else ()
    return Cmd("git", ("git", "--no-pager", "-c", "core.fsmonitor=false", sub, *extra, *flags,
                       "--", *paths), None)


def parse(argv: Sequence[str], *, root: Path, python: str, junit_dir: Path) -> Cmd:
    """Map a model-supplied argv onto one allowed command, or raise CmdRefused. Never a shell."""
    if not argv:
        raise _refuse("没有命令")
    head, rest = argv[0], list(argv[1:])
    if head == "pytest":
        return _pytest(rest, root=root, python=python, junit_dir=junit_dir)
    if head == "ruff":
        return _ruff(rest, root=root, python=python)
    if head == "git":
        return _git(rest)
    raise _refuse(f"不允许的命令 {head!r}")


def child_env(*, worktree: Path, tmp: Path, test_db_url: str, python: str) -> dict[str, str]:
    """The child's whole environment, built from an allowlist (never a copy of the parent's): no
    API keys, no dev DB URL. `PYTHONPATH` puts the worktree's `src` first, ahead of the editable
    install's site-packages entry, so the patched code is what the child imports; ahead of those,
    `sandbox/site` (its `sitecustomize` refuses the sealed set and the API keys, in any Python the
    child starts; from this running code, which a patch cannot change)."""
    env = {k: os.environ[k] for k in ENV_PASS if k in os.environ}
    sysroot = env.get("SYSTEMROOT", r"C:\Windows")
    git = shutil.which("git")
    env.update({
        "PATH": os.pathsep.join(x for x in (str(Path(python).parent),
                                            str(Path(git).parent) if git else "",
                                            str(Path(sysroot) / "System32")) if x),
        "PYTHONPATH": os.pathsep.join([str(_NO_READ_SITE), str(worktree / "src"), str(worktree)]),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1", "TEMP": str(tmp), "TMP": str(tmp), "USERPROFILE": str(tmp),
        "HOME": str(tmp), "EVOWIKI_TEST_DATABASE_URL": test_db_url, "EVOWIKI_EMBEDDER": "hash",
        "NO_PROXY": "*", "EVOWIKI_FORGE": "off", "EVOWIKI_BOT": "off",
        "EVOWIKI_FORGE_WRITE_ROOTS": os.pathsep.join([str(worktree), str(tmp)]),
        "EVOWIKI_SEALED_ROOT": str(sealed_dir()),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    })
    return env
