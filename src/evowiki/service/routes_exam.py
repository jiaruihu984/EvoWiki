"""/wiki 真题 (materials phase 5): the page's exam practice and self-reports on its open items
(`wikiapp.exam_view`). Quiz answers go through POST /wiki/assets/{id}/results. 0 LLM calls."""

from typing import Annotated

from fastapi import APIRouter, Depends

from evowiki.deep.store import PgAssetStore
from evowiki.service.deps import Services, current_user, get_services
from evowiki.wikiapp.exam_view import page_exam, self_report
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.wiki_schemas import ExamSelfIn, ExamSelfOut, PageExamOut

Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]
router = APIRouter(prefix="/wiki")


@router.get("/pages/{page_id}/exam")
def get_page_exam(page_id: str, svc: Svc, uid: User) -> PageExamOut:
    """The page's 真题: a quiz asset for its keyed objective items, the rest as open items; nothing
    attached gives asset_id null and no open items. 404 unknown page."""
    snap = PgRepo(svc.pool).load(uid)
    with svc.pool.connection() as conn:
        return page_exam(conn, snap, PgAssetStore(svc.pool), uid, page_id, now=svc.clock(),
                         new_id=svc.new_id)


@router.post("/exam/items/{item_id}/self")
def post_exam_self(item_id: str, body: ExamSelfIn, svc: Svc, uid: User) -> ExamSelfOut:
    """「我答对了 / 没答对 / 做完了」 on an exam item. 404 unknown item, 422 right / wrong on an item
    without a key, 409 the same report is being recorded concurrently."""
    return self_report(PgRepo(svc.pool), svc.pool, uid, item_id, body.result, now=svc.clock(),
                       new_id=svc.new_id)
