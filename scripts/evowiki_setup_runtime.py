"""Internal owned-runtime bridge; accepts only inspect/restart, no configuration input."""
import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path


def operate(op: str) -> dict:
    import evowiki_ctl as ctl
    from envfile import read_env_file

    root = Path(__file__).resolve().parent.parent
    if ctl.dev_db.main_checkout().resolve() != root:
        raise ctl.Refused("wrong instance")
    env = read_env_file(root / ".env")
    state = ctl.load_state(root / ".evowiki-run" / "state.json")
    entry = state.get("backend") or {}
    served = Path(entry.get("root") or root)
    vault = Path(entry.get("vault") or env.get("EVOWIKI_VAULT") or root / ".evowiki-vault")
    if not vault.is_absolute():
        vault = root / vault
    paths = ctl.Paths(root, served, vault)
    port = entry.get("port", 8002)
    process = ctl.owned_backend(paths, state)
    identity = ctl.backend_identity(port)
    if identity and (process is None or not ctl.matches_identity(paths, identity)):
        raise ctl.Refused("unknown instance")
    if entry and process is None:
        raise ctl.Refused("unverified runtime record")
    if state.get("supervisor") and ctl.owned_process(state["supervisor"]) is None:
        raise ctl.Refused("unverified supervisor")
    if op == "inspect" or process is None:
        return {"state": "running" if process else "stopped", "port": port}
    args = argparse.Namespace(db=False, port=port, no_bot=not entry.get("bot", True),
                              from_=entry.get("from", "master"), resume=False, root=None)
    # cmd_down verifies/drains the owned process and stops its supervisor first.
    ctl.cmd_down(paths, args)
    if ctl.cmd_up(paths, args) != 0:
        raise ctl.Refused("restart failed")
    state = ctl.load_state(paths.state)
    return {"state": "restarted", "port": state["backend"]["port"]}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"inspect", "restart"}:
        print(json.dumps({"ok": False, "code": "invalid_request"}))
        return 1
    try:
        # Lifecycle diagnostics can contain environment-derived paths. Keep the wire
        # response bounded and nonsecret, including failures from dependencies.
        stdout_fd, stderr_fd = os.dup(1), os.dup(2)
        try:
            with open(os.devnull, "w") as sink:
                os.dup2(sink.fileno(), 1)
                os.dup2(sink.fileno(), 2)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    restart = operate(sys.argv[1])
        finally:
            os.dup2(stdout_fd, 1)
            os.dup2(stderr_fd, 2)
            os.close(stdout_fd)
            os.close(stderr_fd)
        result = {"ok": True, "restart": restart}
    except Exception:  # noqa: BLE001 -- never send environment-derived exception text over the wire
        result = {"ok": False, "code": "runtime_ownership_or_restart_failed"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
