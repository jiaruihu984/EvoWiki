"""GET /health (commit + in-flight DBOS workflows) and the localhost-only drain flag.

The forge supervisor restarts the service only at an idle point (`in_flight == 0`) and sets the drain flag
first, so new background work and business requests stop entering until the restart finishes."""

import os
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import BaseModel

from evowiki.agent import dbos_app, lifecycle

router = APIRouter()

InFlight = Callable[[], int]

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1"})


def dbos_in_flight() -> int:
    """Workflows of this app version that are PENDING or ENQUEUED. Checked against the dev DB (2026-10-22):
    no perpetual workflow exists for the current version (the per-minute `bot_tick` schedule finishes at once; the
    PENDING rows there belong to old app versions, which this filter excludes), so nothing is excluded by id."""
    from dbos import DBOS
    return len(DBOS.list_workflows(status=["PENDING", "ENQUEUED"], app_version=dbos_app.APP_VERSION,
                                   load_input=False, load_output=False))


class DrainBody(BaseModel):
    on: bool


@router.get("/health")
def health(request: Request) -> dict:
    in_flight: InFlight = getattr(request.app.state, "in_flight", None) or (lambda: 0)
    return {"ok": True, "commit": os.environ.get("EVOWIKI_COMMIT", ""), "app_version": dbos_app.APP_VERSION,
            "instance_root": os.environ.get("EVOWIKI_INSTANCE_ROOT", ""), "protocol_version": 3,
            "in_flight": int(in_flight()) + request.app.state.request_gate.active, "drained": lifecycle.drained()}


@router.get("/health/ready", response_model=None)
def ready(request: Request) -> dict | JSONResponse:
    if not request.app.state.memory_ready:
        return JSONResponse({"code": "memory_recovering", "detail": "正在核对删除记录"}, status_code=503)
    svc = request.app.state.services
    if svc.durable is not None and not dbos_app.launched():
        return JSONResponse({"code": "runtime_recovering", "detail": "后台任务尚未启动"}, status_code=503)
    from evowiki.store.migrate import MIGRATIONS_DIR
    expected = {p.stem for p in MIGRATIONS_DIR.glob("*.sql")}
    try:
        with svc.pool.connection() as conn:
            applied = {r["id"] for r in conn.execute("select id from schema_migrations").fetchall()}
    except (DatabaseError, PoolTimeout):
        return JSONResponse({"code": "database_unavailable", "detail": "数据库暂不可用，请检查服务状态"}, status_code=503)
    if applied != expected:
        return JSONResponse({"code": "database_version_mismatch", "detail": "数据库版本与代码不匹配，请运行启动器完成升级"}, status_code=503)
    return {"ok": True}


@router.get("/health/backup")
def backup_status() -> dict:
    from evowiki.service.backup import BackupConfig, status
    return status(BackupConfig.from_env(os.environ))


@router.post("/health/backup/run")
def backup_run(request: Request) -> dict:
    from evowiki.service.backup import BackupBusy, BackupConfig, BackupFailed, run_once, status
    if request.client is None or request.client.host not in _LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="没有权限进行这个操作")
    config = BackupConfig.from_env(os.environ)
    if config is None:
        raise HTTPException(status_code=409, detail="备份已关闭，请先在配置中启用")
    try:
        run_once(config, request.app.state.services.clock(), force=True)
    except BackupBusy:
        return status(config)
    except (BackupFailed, OSError):
        raise HTTPException(status_code=503, detail="备份未完成，请查看备份状态后重试") from None
    return status(config)


@router.get("/health/capabilities")
def feature_status(request: Request) -> dict:
    from evowiki.service.capabilities import capabilities
    return capabilities(request.app.state.services)


@router.post("/health/drain")
def drain(body: DrainBody, request: Request) -> dict:
    host = request.client.host if request.client else ""
    if host not in _LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="没有权限进行这个操作")
    request.app.state.request_gate.set_drained(body.on)
    return {"drained": lifecycle.drained()}
