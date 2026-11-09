"""GET /wiki/components — the interactive component library (B5), for UIs and for the prompts' catalog.

Lists id, name, description, learning question, adjustable params, height, the topics it fits and,
for a configurable component, its config schema and example configs; never the html (a placed
component reaches a page as that page's own asset). 503 while the library is not wired."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from evowiki.deep.contracts import LibraryComponent
from evowiki.i18n import current_language, tr
from evowiki.service.deps import Services, current_user, get_services
from evowiki.wikiapp.llm import LLMUnavailable

router = APIRouter(prefix="/wiki")
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]


class ComponentOut(BaseModel):
    component_id: str
    name: str
    description: str
    learning_question: str
    params: dict[str, str]
    height: int
    scenarios: list[str] = []
    configurable: bool = False                       # false = fixed-content (one topic)
    config_schema: dict[str, Any] | None = None
    example_configs: list[dict[str, Any]] = []       # each one rendered fine

    @classmethod
    def of(cls, c: LibraryComponent, *, language: str | None = None) -> "ComponentOut":
        lang = language or current_language()
        return cls(component_id=c.component_id, name=tr(c.name, lang=lang),
                   description=tr(c.description, lang=lang),
                   learning_question=tr(c.learning_question, lang=lang),
                   params={tr(k, lang=lang): tr(v, lang=lang) for k, v in c.params.items()}, height=c.height,
                   scenarios=[tr(v, lang=lang) for v in c.scenarios], configurable=c.configurable,
                   config_schema=c.config_schema, example_configs=list(c.example_configs))


def _library(svc: Services):
    if svc.library is None:
        raise LLMUnavailable("交互组件库不可用")
    return svc.library


@router.get("/components")
def list_components(svc: Svc, uid: User) -> list[ComponentOut]:
    return [ComponentOut.of(c) for c in _library(svc).list()]
