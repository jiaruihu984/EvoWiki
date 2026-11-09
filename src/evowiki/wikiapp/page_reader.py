"""Read-only Wiki page contracts shared by readers and writers."""

from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from evowiki.goals.contracts import SectionRole
from evowiki.goals.fold import Page

READ_SCOPE = "这里只包含服务端受管内容，不包含本地个人笔记；看不到笔记不代表没有笔记。"


def check_status_lines(page: Page, answers: Mapping[str, int] | None) -> dict[str, str]:
    """Safe question state shared by page reads and assistant context; never include a key.

    Writer drafts have no answer records (None); a stored page's empty mapping means unanswered.
    """
    lines = {}
    for section in page.outline.sections:
        if section.role is not SectionRole.CHECK:
            continue
        item = section.check
        if item is None or not item.verified:
            status = "确认题未就绪"
        else:
            status = "确认题已生成，"
            if answers is None:
                status += "作答状态未知"
            elif section.id not in answers:
                status += "未作答"
            else:
                choice = answers[section.id]
                status += "已作答"
                if choice >= 0:
                    status += "，答对" if choice == item.answer else "，答错"
        lines[section.id] = f"- {section.id} · {section.heading}（{status}）"
    return lines


class WikiPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_id: str
    title: str
    page: Page
    # a run is writing the page, or one stopped part-way: the outline it planned, the sections it
    # committed so far (`PageRecord.writing`)
    writing: bool = False
    # None on a writer draft: no claim about the learner's answers. Stored readers provide the
    # mapping from the same PageRecord as `page`; only check_status_lines exposes it to models.
    check_answers: dict[str, int] | None = None

    @property
    def display_title(self) -> str:
        """The title, or the page id when the title is empty (Source.title needs ≥1 char)."""
        return self.title or self.page_id


class UnwrittenPage(BaseModel):
    """A page that exists (on a path, in the Wiki) but has no outline yet: queued, or never
    written. What it is meant to cover is all there is to read."""

    model_config = ConfigDict(frozen=True)

    page_id: str
    title: str
    goal: str
    points: tuple[str, ...] = ()       # the points it teaches, as 「名字（id）」


class PageStore(Protocol):
    """`unwritten(user_id, page_id) -> UnwrittenPage | None` is optional (a store without it has
    no unwritten pages)."""

    def get(self, user_id: str, page_id: str) -> WikiPage | None: ...

    def all(self, user_id: str) -> Sequence[WikiPage]: ...


class InMemoryPageStore:
    def __init__(self, pages: Mapping[str, Sequence[WikiPage]]):
        self._pages = {u: tuple(ps) for u, ps in pages.items()}

    def get(self, user_id: str, page_id: str) -> WikiPage | None:
        return next((p for p in self._pages.get(user_id, ()) if p.page_id == page_id), None)

    def all(self, user_id: str) -> Sequence[WikiPage]:
        return self._pages.get(user_id, ())
