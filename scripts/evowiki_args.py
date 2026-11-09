"""The product CLI grammar, usable before project dependencies or configuration exist."""

import argparse
import os
from pathlib import Path

from launcher_i18n import cli_format, cli_text, default_language, set_language

BACKEND_PORT = 8002


def add_backup_commands(commands, *, list_name: str = "list", epilog: str | None = None) -> None:
    commands.add_parser("backup", help="create a database and forget-ledger backup", epilog=epilog)
    commands.add_parser(list_name, help="list completed backups and their status", epilog=epilog)
    for name in ("restore-check", "restore"):
        parser = commands.add_parser(name, epilog=epilog)
        parser.add_argument("--backup", type=Path, required=True)
        parser.add_argument("--database", required=True)
        parser.add_argument("--target-dir", type=Path, required=True)
        parser.add_argument("--ledger", type=Path, help="latest authoritative external forget ledger")
        if name == "restore":
            parser.add_argument("--confirm", action="store_true")


def launcher_parser(*, wrapper: bool = False, root: Path | None = None) -> argparse.ArgumentParser:
    readme = "README.en.md" if default_language() == "en" else "README.md"
    epilog = cli_format('完整命令、安装与恢复步骤：{p0}', p0=(root or Path(__file__).resolve().parent.parent) / readme)
    parser = argparse.ArgumentParser(description="Run and maintain the local EvoWiki product.", epilog=epilog)
    parser.add_argument("--language", choices=("zh", "en"), default=default_language(), help="language of launcher messages")
    parser.add_argument("--root", type=Path, help="checkout whose code is served (lifecycle commands only)")
    commands = parser.add_subparsers(dest="cmd", required=True)
    if wrapper:
        commands.add_parser("setup", help="local configuration JSON protocol over stdin", epilog=epilog)
        commands.add_parser("init", help="create first-run configuration and the default vault", epilog=epilog)
        commands.add_parser("doctor", help="check installed tools and configuration", epilog=epilog)
        add_backup_commands(commands, list_name="backups", epilog=epilog)
    for command in ("up", "preflight"):
        up = commands.add_parser(command, help="start or inspect the owned runtime", epilog=epilog)
        up.add_argument("--port", type=int, default=BACKEND_PORT)
        up.add_argument("--vault", type=Path, default=os.environ.get("EVOWIKI_VAULT"))
        up.add_argument("--no-bot", action="store_true", help="serve with EVOWIKI_BOT=off (no background Bot)")
        up.add_argument("--from", dest="from_", choices=("master", "live"), default="master",
                        help="live: serve the sibling <repo-name>-live worktree and start the supervisor")
        up.add_argument("--resume", action="store_true", help="with --from live: clear a paused supervisor")
    supervise = commands.add_parser("supervise", help="operate the live supervisor", epilog=epilog)
    supervise.add_argument("--port", type=int, required=True)
    down = commands.add_parser("down", help="stop what up started", epilog=epilog)
    down.add_argument("--db", action="store_true", help="also stop Postgres (only if up started it)")
    down.add_argument("--port", type=int, default=BACKEND_PORT)
    status = commands.add_parser("status", help="show what runs", epilog=epilog)
    status.add_argument("--port", type=int, default=BACKEND_PORT)
    status.add_argument("--vault", type=Path, default=os.environ.get("EVOWIKI_VAULT"))
    uninstall = commands.add_parser("uninstall", help="preview removal; keep user data by default", epilog=epilog)
    uninstall.add_argument("--port", type=int, default=BACKEND_PORT)
    uninstall.add_argument("--vault", type=Path, default=os.environ.get("EVOWIKI_VAULT"))
    uninstall.add_argument("--apply", action="store_true", help="perform the displayed removal")
    uninstall.add_argument("--remove-runtime", action="store_true", help="remove this checkout's Python/npm environments")
    uninstall.add_argument("--delete-data", action="store_true", help="delete the owned default vault, config and stopped DB cluster")
    uninstall.add_argument("--confirm-delete-root", type=Path, help="explicit absolute checkout path for data deletion")
    for command_parser in commands.choices.values():
        command_parser.add_argument("--language", choices=("zh", "en"), default=argparse.SUPPRESS,
                                    help="language of launcher messages")
    return parser


def parse_launcher_args(argv: list[str] | None = None, *, wrapper: bool = False,
                        root: Path | None = None) -> argparse.Namespace:
    parser = launcher_parser(wrapper=wrapper, root=root)
    args = parser.parse_args(argv)
    set_language(args.language)
    if getattr(args, "port", BACKEND_PORT) not in range(1, 65536):
        parser.error("--port must be between 1 and 65535")
    if args.cmd in ("up", "preflight"):
        if args.port == 8001:
            parser.error(cli_text("端口 8001 属于其他应用，不能用"))
        if args.resume and args.from_ != "live":
            parser.error(cli_text("--resume 只能和 --from live 一起用"))
        if args.root is not None and args.from_ == "live":
            parser.error(cli_text("--from live 和 --root 不能同时用"))
    if wrapper and args.root is not None and args.cmd in ("setup", "init", "doctor", "backup", "backups", "restore-check", "restore"):
        parser.error("--root only applies to lifecycle commands; configuration belongs to the main checkout")
    return args


def backup_parser(root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backup and restore EvoWiki without model calls.")
    parser.add_argument("--env", type=Path, default=root / ".env")
    add_backup_commands(parser.add_subparsers(dest="command", required=True))
    return parser
