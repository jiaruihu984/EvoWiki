"""python -m evowiki.service — operate the EvoWiki runtime (see docs/runtime.md)."""

import argparse
import ipaddress
import logging
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import psycopg

from evowiki.clock import SystemClock
from evowiki.llm.providers import ENV_KEYS
from evowiki.retrieval.embed import embedder_from_env
from evowiki.runtime.llm import check_runtime_provider, runtime_gateway_config
from evowiki.store.content import activate, active_version, import_snapshot_file, load_content
from evowiki.store.db import admin_database_url, database_url, fetch_all, make_pool, role_problem
from evowiki.store.llm_logs import import_jsonl
from evowiki.store.migrate import MIGRATIONS_DIR, migrate
from evowiki.store.vectors import index_segments

__all__ = ["is_loopback", "main"]
log = logging.getLogger("evowiki.service")
CONNECT_TIMEOUT_S = 5


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evowiki.service")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply pending SQL migrations")
    ic = sub.add_parser("import-content", help="import a content snapshot (graph.json)")
    ic.add_argument("path", type=Path)
    ic.add_argument("--activate", action="store_true")
    sub.add_parser("index-embeddings", help="embed active-content segments (configured model)")
    il = sub.add_parser("import-llm-logs", help="import llm_calls/llm_attempts jsonl files")
    il.add_argument("--calls", type=Path)
    il.add_argument("--attempts", type=Path)
    s = sub.add_parser("serve", help="HTTP API (loopback only unless --unsafe-expose)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8002)
    s.add_argument("--unsafe-expose", action="store_true",
                   help="allow a non-loopback host although v1 has NO authentication")
    sub.add_parser("status", help="DB, content, embeddings, LLM config (key set/unset only)")
    sub.add_parser("prepare", help="install/check Chromium and register missing built-in components")
    return p


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] = os.environ) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "serve" and not is_loopback(args.host):
        if not args.unsafe_expose:
            print(f"refusing to bind {args.host}: v1 has no authentication and X-EvoWiki-User "
                  "is only a namespace. Bind 127.0.0.1/::1, or pass --unsafe-expose if you "
                  "really mean it.", file=sys.stderr)
            return 2
        log.warning("UNSAFE: serving on %s without authentication; anyone who can reach this "
                    "port can read and write any user's data", args.host)
    try:
        dsn = database_url(env)
        check_runtime_provider(env)  # fail fast on a bad EVOWIKI_RUNTIME_PROVIDER
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    clock = SystemClock()
    if args.cmd == "migrate":
        return _migrate(admin_database_url(env))
    problem = _preflight(dsn)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    if args.cmd == "serve":
        return _run_services(args, env, clock)
    pool = make_pool(dsn, min_size=1, max_size=4)
    try:
        return _run(args, pool, env, clock)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"{args.cmd} failed: {exc}", file=sys.stderr)
        return 1
    finally:
        pool.close()


def _connect(dsn: str) -> psycopg.Connection:
    # A plain connection, not the pool: the pool registers the pgvector type on connect, which
    # fails before migration 0001 has created the extension (and retries for 30 s).
    return psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT_S)


def _migrate(dsn: str) -> int:
    try:
        conn = _connect(dsn)
    except psycopg.OperationalError as exc:
        print(f"database unreachable: {exc}", file=sys.stderr)
        return 1
    with conn:
        try:
            print(f"applied: {migrate(conn)}")
        except psycopg.Error as exc:  # e.g. the role may not create the vector extension
            print(f"migrate failed: {exc}", file=sys.stderr)
            return 1
    return 0


def _preflight(dsn: str) -> str | None:
    """Why the database cannot serve this command (unreachable / not migrated), or None."""
    try:
        with _connect(dsn) as conn:
            have = conn.execute("select to_regclass('schema_migrations') is not null").fetchone()
            applied = ({r[0] for r in conn.execute("select id from schema_migrations")}
                       if have[0] else set())
            pending = sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql") if p.stem not in applied)
            if pending:
                return (f"database is not migrated ({len(pending)} pending: {', '.join(pending)}); "
                        "run `python -m evowiki.service migrate`")
            return role_problem(conn)       # 0082: the service never runs as an owner or superuser
    except psycopg.OperationalError as exc:
        return f"database unreachable: {exc}"


def _run_services(args, env, clock) -> int:
    from evowiki.service.deps import build_services  # imports FastAPI only when needed

    try:
        _ensure_browser()
        svc = build_services(env, clock=clock)
    except RuntimeError as exc:  # browser/GPU preflight or service wiring failed
        print(f"{args.cmd} failed: {exc}", file=sys.stderr)
        return 1
    try:
        import uvicorn

        from evowiki.service.app import create_app

        _initialize_library(svc.pool)

        if env.get("EVOWIKI_BOT") != "off":
            try:
                _add_bot_hook(svc, clock)
            except Exception as exc:  # noqa: BLE001 - fail closed: the operator turns the Bot off explicitly
                print(f"serve failed: the Bot could not start ({exc}); fix it or set "
                      "EVOWIKI_BOT=off to run without it.", file=sys.stderr)
                return 1
        uvicorn.run(create_app(svc), host=args.host, port=args.port)
        return 0
    except RuntimeError as exc:
        print(f"serve failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        svc.pool.close()


def _initialize_library(pool) -> None:
    from evowiki.deep.library_seed import load_seed, register_seed, seed_html
    from evowiki.deep.render import PlaywrightChecker
    from evowiki.deep.store import PgLibrary

    library = PgLibrary(pool)
    existing = {component.component_id: component for component in library.list()}
    # a component whose code or schema changed in the repo is checked and stored again (what is already on a page keeps its own copy)
    missing = [component for component in load_seed()
               if (have := existing.get(component.component_id)) is None or have.html != seed_html(component)
               or have.config_schema != component.config_schema]
    if missing:
        log.info("正在检查并注册 %d 个内置交互组件（不调用模型）", len(missing))
        checker = PlaywrightChecker()
        for index, component in enumerate(missing, 1):
            log.info("组件检查 %d/%d：%s", index, len(missing), component.component_id)
            results = register_seed(library, checker, [component])
            failed = [r for r in results if not r.registered]
            if failed:
                raise RuntimeError("内置组件检查失败：" + "；".join(f"{r.component_id}: {r.error}" for r in failed))


def _ensure_browser() -> None:
    """Check the actual runtime interpreter's browser before any paid work can begin."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少渲染依赖，请使用 EvoWiki-start.cmd 安装并启动") from exc
    # The Chromium executable can exist while a previous download left its headless
    # shell or other required components missing. The official installer checks the
    # whole bundle and reuses complete downloads without network access.
    log.info("正在检查并补全 Chromium 运行组件（已安装的组件会复用）")
    result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    if result.returncode != 0:
        raise RuntimeError("Chromium 安装失败，请检查网络后重新启动")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        raise RuntimeError(f"Chromium 无法启动：{exc}") from exc


def _add_bot_hook(svc, clock) -> None:
    """Register the Bot on B1b's one DBOS launch: an `after_launch` hook of `svc.durable` (run by
    the app lifespan right after `dbos_app.launch`). The hook registers the Bot's queue and schedule
    and then turns on `svc.bot_notify`. A failed start (DBOS launch, this hook or any other hook
    raising) undoes the whole Bot install through `on_failed_start` — notify off, schedule deleted,
    runtime dropped — before DurableRuntime shuts DBOS down and re-raises. Without a durable
    runtime the Bot cannot run: raise (fail closed)."""
    from evowiki.bot import durable
    from evowiki.service.rsi_runs import bot_rsi_runner

    if svc.durable is None:
        raise RuntimeError("no durable runtime (DBOS) to register the Bot on")

    # the runtime must exist before DBOS launches: launch recovers pending workflows (a bot_tick)
    from evowiki.wikiapp.llm import CountingGateway
    durable.install(durable.BotRuntime(svc.pool, clock, svc.new_id,
                                       rsi=bot_rsi_runner(svc),
                                       actions=getattr(svc, "actions", None),
                                       write_queue=getattr(svc, "write_queue", None),
                                       gateway=lambda uid: CountingGateway(svc.gateway, uid) if svc.gateway is not None else None))

    def hook() -> None:
        durable.register()
        svc.bot_notify = durable.notify

    def undo() -> None:
        svc.bot_notify = None
        durable.uninstall()

    svc.durable.on_failed_start.append(undo)
    svc.durable.after_launch.append(hook)


def _run(args, pool, env, clock) -> int:
    match args.cmd:
        case "prepare":
            _ensure_browser()
            _initialize_library(pool)
            print("浏览器与内置组件已就绪")
        case "import-content":
            with pool.connection() as conn:
                cv = import_snapshot_file(conn, args.path, clock())
                if args.activate:
                    activate(conn, cv, clock())
            print(f"imported {cv}{' (active)' if args.activate else ''}")
        case "index-embeddings":
            emb = embedder_from_env(env)
            with pool.connection() as conn:
                n = index_segments(conn, load_content(conn), emb)
            print(f"indexed {n} segments with {emb.id} {emb.model_version}")
        case "import-llm-logs":
            print(import_jsonl(pool, calls=args.calls, attempts=args.attempts))
        case "status":
            _status(pool, env)
    return 0


def _status(pool, env) -> None:
    with pool.connection() as conn:
        cv = active_version(conn)
        print(f"database: reachable\ncontent: {cv or 'none'}")
        rows = fetch_all(
            conn, "select embedder_id, model_version, dim, count(*) as n "
                  "from segment_embedding where content_version = %s "
                  "group by embedder_id, model_version, dim order by 1, 2", (cv,)) if cv else []
    # The configured id only: model_version would load (and maybe download) the model.
    emb_id = embedder_from_env(env).id
    print(f"embedder: {emb_id}")
    for r in rows:
        print(f"embeddings: {r['embedder_id']} {r['model_version']} dim={r['dim']} "
              f"segments={r['n']}")
    if cv and not any(r["embedder_id"] == emb_id for r in rows):
        print(f"WARNING: no {emb_id} embeddings for {cv}; run index-embeddings")
    config = runtime_gateway_config(env)
    print(f"runtime LLM: generator {config.generator.provider}:{config.generator.model}, "
          f"verifier {config.verifier.provider}:{config.verifier.model}")
    if config.single_provider:
        print(f"WARNING: single-provider runtime ({config.generator.provider}) — spec §8 "
              "downgrade: generator and verifier share one provider; results and recordings "
              "are not reportable")
    for var in ENV_KEYS.values():
        print(f"{var}: {'set' if env.get(var) else 'unset'}")
