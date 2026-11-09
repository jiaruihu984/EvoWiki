"""「建议先复习 X」 (user decision 2026-11-04): a wrong answer on a point the next page builds on.

A suggestion on the next step (`StepOut.review_first`, 今天 and the end of a page read it), 0 calls.
先复习 and 跳过 both close it for the wrong answers it stood on; a new wrong answer on the point
brings it back. It never changes the path, never blocks the step and never writes a page: 先复习 is a
chat turn on the point, started by the client; the path changes only when he asks for that.

What counts is `AspectK.open_miss` on a cell of a point the next page builds on that still reads
shaky / gap: one wrong answer on a point with a strong record stays silent. A concept's 粗心答错 /
现在会了, its opening probe, review cards and confirmation questions are not open misses.

「Builds on」: the row's `page_needs`, or a point an earlier row of the same path teaches that the
next page names in its title or text. Most goals' graphs have no prerequisite edges between a block's
parts (`me`: none), so the edges alone would leave 巨分量 silent about 连通分量."""

from collections.abc import Sequence
from typing import Literal

from evowiki.belief.aspects import AspectKView
from evowiki.goals.contracts import cell_id
from evowiki.goals.guardrails import WEAK
from evowiki.goals.path_edit import Item
from evowiki.wikiapp.events import ReviewFirstAnswered, WikiEvent
from evowiki.wikiapp.kwrite import Ids, concept_name
from evowiki.wikiapp.path_order import page_needs
from evowiki.wikiapp.repo import Commit, Repo
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import ReviewFirstOut

ReviewFirstChoice = Literal["review", "skip"]


def _open_misses(state: WikiState, kv: AspectKView, node_id: str) -> tuple[str, ...]:
    """The wrong answers still standing on the weak cells of this node, not answered here yet."""
    out = []
    for a in kv.aspects(node_id):
        cell = cell_id(node_id, a)
        miss = kv.open_miss(cell)
        if miss is not None and miss not in state.review_first_answered and kv.cell(cell).band in WEAK:
            out.append(miss)
    return tuple(out)


def _teaching_page(state: WikiState, node_id: str) -> tuple[str | None, str]:
    for pid in state.page_order:
        rec = state.pages[pid]
        if pid not in state.trashed_pages and rec.has_body and node_id in rec.targets:
            return pid, rec.title
    return None, ""


def _named_earlier(state: WikiState, item: Item, earlier: Sequence[Item],
                   page_id: str | None) -> set[str]:
    """The points of the earlier rows that the row's title or its page's text names."""
    rec = state.pages.get(page_id) if page_id is not None else None
    bodies = [t.body for t in rec.page.texts.values()] if rec is not None and rec.page else []
    text = "\n".join((item.title, *bodies))
    out = set()
    for row in earlier:
        for n in row.node_ids:
            named = state.names.get(n)
            if named is not None and named.name.strip() and named.name in text:
                out.add(n)
    return out - set(item.node_ids)


def suggestion(state: WikiState, kv: AspectKView, item: Item, earlier: Sequence[Item] = (),
               page_id: str | None = None) -> ReviewFirstOut | None:
    """The point to suggest before path row `item` (`earlier`: the rows shown before it; `page_id`:
    its wiki page), if any: one, the first by id."""
    for node_id in sorted(page_needs(state, item) | _named_earlier(state, item, earlier, page_id)):
        if _open_misses(state, kv, node_id):
            page_id, title = _teaching_page(state, node_id)
            return ReviewFirstOut(node_id=node_id, name=concept_name(state, node_id),
                                  page_id=page_id, page_title=title)
    return None


def answer(repo: Repo, user_id: str, node_id: str, choice: ReviewFirstChoice, *, now,
           new_id: Ids) -> None:
    """POST /wiki/progress/review-first: closes the suggestion on `node_id`. Nothing standing (a
    second tap, an answer corrected meanwhile): nothing to record."""
    snap = repo.load(user_id)
    misses = _open_misses(snap.state, snap.kview(now), node_id)
    if misses:
        repo.commit(snap, Commit(wiki=[WikiEvent(
            event_id=new_id("wev"), ts=now,
            body=ReviewFirstAnswered(node_id=node_id, answer_ids=misses, choice=choice))]))
