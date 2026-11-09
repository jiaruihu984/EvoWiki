"""POST /wiki/reading: the plugin's batched reading-time heartbeats (B5 spec §1.6)."""

from typing import Annotated

from fastapi import APIRouter, Depends

from evowiki.bot.reading import ReadingIn, ReadingOut, record_reading
from evowiki.service.deps import Services, check_user, current_user, get_services

router = APIRouter(prefix="/wiki")
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]


@router.post("/reading", response_model=ReadingOut)
def post_reading(body: ReadingIn, svc: Svc, uid: User):
    check_user(svc, uid)
    return record_reading(svc.pool, uid, body, now=svc.clock())
