"""/wiki/prompts: Prompt 设置 (plan 2026-10-21 参数 redesign, D1 / D3) — every model step's
guidance, the user's own wording for it, and back to the default. Each change is an undoable row
of 最近改了什么 (`bot.prompts`)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from evowiki.bot import prompts
from evowiki.bot.actions import ActionLedger
from evowiki.harness.source import reader
from evowiki.harness.understand_format import format_note
from evowiki.llm.prompt_steps import STEPS, Step
from evowiki.service.deps import Services, check_user, current_user, get_services
from evowiki.service.prompt_defaults import default_texts
from evowiki.wikiapp.wiki_schemas import PromptIn, PromptStepOut

router = APIRouter(prefix="/wiki/prompts")
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]


UNAVAILABLE = "现在改不了。"


def _out(s: Step, override: str | None, default: str) -> PromptStepOut:
    return PromptStepOut(step=s.key, name=s.name, description=s.description, category=s.category,
                         text=default if override is None else override,
                         default_text=default, edited=override is not None,
                         format_note=format_note(s.key, override))


def _ledger(svc: Services) -> ActionLedger:
    if svc.actions is None:          # a change must be undoable: no ledger, no change
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return svc.actions


@router.get("", response_model=list[PromptStepOut])
def list_prompts(svc: Svc, uid: User):
    check_user(svc, uid)
    mine = reader(svc.pool)(uid).guidance()
    defaults = default_texts()
    return [_out(s, mine.get(s.key), defaults[s.key]) for s in STEPS]


def _change(svc: Services, uid: str, key: str, text: str | None) -> PromptStepOut:
    check_user(svc, uid)
    s = prompts.step(key)
    default = default_texts()[s.key]      # what the model reads with no override
    got = prompts.change(svc.pool, _ledger(svc), uid, s.key, text, default=default,
                         now=svc.clock(), new_id=svc.new_id)
    return _out(s, got, default)


@router.put("/{step}", response_model=PromptStepOut)
def put_prompt(step: str, body: PromptIn, svc: Svc, uid: User):
    return _change(svc, uid, step, body.text)


@router.delete("/{step}", response_model=PromptStepOut)
def delete_prompt(step: str, svc: Svc, uid: User):
    return _change(svc, uid, step, None)
