"""What an L2 child (a patch's pytest and every Python it starts, its replay child) may not do: read the
sealed evaluation set or the API keys, reach anything but this machine, write outside its worktree and temp
folder, start a shell. `patch/commands.child_env` puts this folder first on PYTHONPATH, so Python loads this
file at startup. Same Windows account, so the OS would let it (`patch/commands` says why there is no hard
isolation without admin); an audit hook refuses it.

This stops a test or a patched module written the ordinary way, which is the risk here: an optimiser seeing the
answers or changing what grades it. It is not a security boundary (PEP 578 says so): native code, `git` running an
alias and a superuser on the test Postgres can get past it. Hard isolation needs a second Windows account, which is
the user's call."""

import os
import sys
from pathlib import Path

SEALED_ENV = "EVOWIKI_SEALED_ROOT"
# The parent passes the resolved contract root. Standalone startup derives the
# same checkout-local default without importing potentially patched project code.
SEALED_DEFAULT = str(Path(__file__).resolve().parents[5] / ".evowiki-run/evalset-sealed")
WRITE_ROOTS_ENV = "EVOWIKI_FORGE_WRITE_ROOTS"
_READ_EVENTS = frozenset({"open", "os.listdir", "os.scandir", "os.stat", "_winapi.CreateFile"})
_MOVE_EVENTS = frozenset({"os.rename", "os.link", "os.symlink", "shutil.copyfile", "shutil.move"})
_REMOVE_EVENTS = frozenset({"os.remove", "os.rmdir", "os.truncate", "shutil.rmtree"})
_LOCAL = ("127.", "::1", "localhost", "0.0.0.0")
_PROGRAMS = frozenset({"python", "python3", "pythonw", "git", "ruff", "psql", "pg_ctl", "pg_dump", "pg_dumpall",
                       "pg_restore", "pg_isready", "createdb", "dropdb", "initdb", "postgres"})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _install() -> None:
    sealed = os.path.normcase(os.path.abspath(os.environ.get(SEALED_ENV) or SEALED_DEFAULT))
    roots = tuple(os.path.normcase(os.path.abspath(r))
                  for r in os.environ.get(WRITE_ROOTS_ENV, "").split(os.pathsep) if r)
    site = os.path.dirname(os.path.abspath(__file__))
    abspath, normcase, fsdecode, basename, sep, devnull = (os.path.abspath, os.path.normcase, os.fsdecode,
                                                           os.path.basename, os.sep, os.devnull)

    def forbidden(path: object) -> bool:
        if not isinstance(path, str | bytes | os.PathLike):
            return False
        text = fsdecode(path)
        full = normcase(abspath(text))
        return full == sealed or full.startswith(sealed + sep) or basename(text).startswith(".env")

    def outside(path: object) -> bool:
        """A write here leaves the worktree and the temp folder (no roots given: nothing is confined)."""
        if not roots or not isinstance(path, str | bytes | os.PathLike):
            return False
        text = fsdecode(path)
        if text == devnull or text.startswith(("\\\\.\\", "\\\\?\\pipe")):
            return False
        full = normcase(abspath(text))
        return not any(full == r or full.startswith(r + sep) for r in roots)

    def program(args: tuple) -> tuple[str, list[str]]:
        exe, argv = args[0], args[1]
        argv = [argv] if isinstance(argv, str | bytes) else list(argv or [])
        words = [fsdecode(a) if isinstance(a, str | bytes | os.PathLike) else str(a) for a in argv]
        first = fsdecode(exe) if exe else (words[0].split()[0].strip('"') if words and words[0].split() else "")
        name = basename(first.replace("/", sep)).lower()
        return name.removesuffix(".exe"), words

    def check_process(args: tuple) -> None:
        name, words = program(args)
        text = " ".join(words)
        if sealed in normcase(text.replace("/", sep)):
            raise PermissionError("forge: this process names the sealed set")
        if ".env" in text.lower():
            raise PermissionError("forge: this process names a .env file")
        if name not in _PROGRAMS:
            raise PermissionError(f"forge: this program does not run here: {name}")
        if not name.startswith("python"):
            return
        env = args[3] if len(args) > 3 else None
        if env is not None and site not in str(dict(env).get("PYTHONPATH", "")):
            raise PermissionError("forge: a Python started here keeps this hook")
        for w in words[1:]:
            if w in ("-c", "-m") or not w.startswith("-"):
                break
            if not w.startswith("--") and set(w[1:]) & set("IES"):
                raise PermissionError(f"forge: a Python started here keeps this hook: {w}")

    def hook(event: str, args: tuple) -> None:
        if event in _READ_EVENTS and args:
            if forbidden(args[0]):
                raise PermissionError(f"forge: not readable here: {args[0]!r}")
            if event == "open" and len(args) > 2 and outside(args[0]):
                mode, flags = args[1], args[2]
                if (isinstance(mode, str) and set(mode) & set("wax+")) or (
                        isinstance(flags, int) and flags & _WRITE_FLAGS):
                    raise PermissionError(f"forge: not writable here: {args[0]!r}")
        elif event in _MOVE_EVENTS and len(args) > 1:
            if forbidden(args[0]) or forbidden(args[1]) or outside(args[1]):
                raise PermissionError(f"forge: not here: {args[0]!r} -> {args[1]!r}")
        elif event in _REMOVE_EVENTS and args:
            if outside(args[0]):
                raise PermissionError(f"forge: not writable here: {args[0]!r}")
        elif event == "subprocess.Popen":
            check_process(args)
        elif event in ("os.system", "os.startfile", "os.spawn", "os.exec", "os.posix_spawn"):
            raise PermissionError(f"forge: not here: {event}")
        elif event in ("socket.connect", "socket.sendto") and len(args) > 1:
            addr = args[1]
            host = addr[0] if isinstance(addr, tuple) else addr
            if not str(host).startswith(_LOCAL):
                raise PermissionError(f"forge: no network here: {host}")
        elif event in ("socket.getaddrinfo", "socket.gethostbyname") and args:
            host = args[0].decode() if isinstance(args[0], bytes) else args[0]
            if host and not str(host).startswith(_LOCAL):
                raise PermissionError(f"forge: no network here: {host}")
    sys.addaudithook(hook)


_install()
