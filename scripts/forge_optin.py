"""Manage the forge-local opt-in for generated tools.

  uv run python scripts/forge_optin.py (--user U | --global) --agent deep|assistant \
      --name NAME|request_tool (--on | --off) [--env .env]

Writes `forge_setting (user | "global", "opt_in.<agent>.<name>")` through `optin.set_local`: the
page writer / the assistant then offer that forged tool (or `request_tool`) on their next run.
Harness 3b's `H3.<agent>.<name>` pack keys win over this setting once they exist. No model call;
the database is the env file's EVOWIKI_DATABASE_URL (else the process environment's)."""

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from envfile import read_env_file

from evowiki.forge import optin
from evowiki.store.db import make_pool


def _database_url(path: Path | None) -> str:
    if path is not None and path.exists():
        value = read_env_file(path).get("EVOWIKI_DATABASE_URL")
        if value:
            return value
    url = os.environ.get("EVOWIKI_DATABASE_URL")
    if not url:
        raise SystemExit("EVOWIKI_DATABASE_URL is not set (env file or environment)")
    return url


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--user")
    who.add_argument("--global", dest="global_", action="store_true")
    ap.add_argument("--agent", required=True, choices=("deep", "assistant"))
    ap.add_argument("--name", required=True, help="a forged tool's name, or request_tool")
    state = ap.add_mutually_exclusive_group(required=True)
    state.add_argument("--on", action="store_true")
    state.add_argument("--off", action="store_true")
    ap.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    a = ap.parse_args(argv)
    pool = make_pool(_database_url(a.env))
    try:
        with pool.connection() as conn:
            optin.set_local(conn, None if a.global_ else a.user, a.agent, a.name, bool(a.on),
                            datetime.now(UTC))
    finally:
        pool.close()
    print(f"opt_in.{a.agent}.{a.name} = {bool(a.on)} for {'global' if a.global_ else a.user}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
