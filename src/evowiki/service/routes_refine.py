"""/wiki page rewrites and interactive assets (plan rev 3.3 §10.2–10.3; shapes in deep/contracts.py).

POST /wiki/pages/{id}/refine starts a whole-page rewrite by the page writer (202, runs after the
response as a page workflow, `page_flows`; no UI offers it — pages are written once by organize, single writer 2026-10-10); GET returns
the latest job. The only automatic rewrite is the lazy one after an RSI rule change: opening a page
the change left out of date (POST /wiki/pages/{id}/open?rewrite=1) starts one rewrite under the current spec
(trigger "rsi", `rsi_runs.stale_rewrite`). Assets are served as standalone
HTML with a CSP that forbids every network fetch and sandboxes the document (scripts only)."""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from evowiki.deep.contracts import AssetMeta, RefineJobOut
from evowiki.deep.evoui import CSS_PATH, JS_PATH, harness_html, spec_text
from evowiki.service.deps import Services, current_user, get_services
from evowiki.service.http_common import ASSET_HEADERS
from evowiki.service.refine_runs import start_refine, start_refine_run
from evowiki.wikiapp.llm import LLMUnavailable
from evowiki.wikiapp.refine_service import DeepService

router = APIRouter(prefix="/wiki")
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]
def _deep(svc: Services) -> DeepService:
    if svc.deep is None:
        raise LLMUnavailable("请切换模型重试")
    return svc.deep


@router.post("/pages/{page_id}/refine", status_code=202)
def post_refine(page_id: str, svc: Svc, uid: User) -> JSONResponse:
    deep = _deep(svc)
    job = start_refine(deep, uid, page_id, "user")
    body = RefineJobOut(job=job).model_dump(mode="json")
    start_refine_run(svc, uid, page_id, job, kind="refine")
    return JSONResponse(status_code=202, content=body)


@router.get("/pages/{page_id}/refine")
def get_refine(page_id: str, svc: Svc, uid: User) -> RefineJobOut:
    deep = _deep(svc)
    if page_id not in deep.repo.load(uid).state.pages:
        raise LookupError(f"没有这个页面：{page_id}")
    return deep.out(uid, page_id)


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, svc: Svc, uid: User,
              view: Literal["raw", "html"] = "raw") -> Response:
    """An html asset: the HTML (CSP + sandbox headers). An image asset: its WebP as base64 text.
    An evoui asset: the block JSON
    (application/json) — clients render it with /wiki/evoui/evoui.js — or, with `?view=html`, a
    standalone page that renders it (same CSP + sandbox headers as an html asset)."""
    a = _deep(svc).assets.get(uid, asset_id)
    if a is None:
        raise LookupError(f"没有这个交互：{asset_id}")
    if a.meta.kind == "image":                # the WebP as base64 text (what meta.sha256 is of)
        return Response(content=a.html, media_type="text/plain; charset=utf-8",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private"})
    if a.meta.kind == "evoui":
        if view == "html":
            return Response(content=harness_html(json.loads(a.html)),
                            media_type="text/html; charset=utf-8", headers=ASSET_HEADERS)
        return Response(content=a.html, media_type="application/json",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private"})
    return Response(content=a.html, media_type="text/html; charset=utf-8",
                    headers=ASSET_HEADERS)


@router.get("/evoui/{name}")
def get_evoui_file(name: Literal["evoui.js", "evoui.css", "spec.md"]) -> Response:
    """The one EvoUI renderer, its stylesheet and the format spec (static; no user header needed)."""
    media = {"evoui.js": "text/javascript", "evoui.css": "text/css",
             "spec.md": "text/markdown"}[name]
    text = (spec_text() if name == "spec.md"
            else {"evoui.js": JS_PATH, "evoui.css": CSS_PATH}[name].read_text(encoding="utf-8"))
    return Response(content=text, media_type=f"{media}; charset=utf-8",
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})


@router.get("/assets/{asset_id}/meta")
def get_asset_meta(asset_id: str, svc: Svc, uid: User) -> AssetMeta:
    a = _deep(svc).assets.get(uid, asset_id)
    if a is None:
        raise LookupError(f"没有这个交互：{asset_id}")
    return a.meta
