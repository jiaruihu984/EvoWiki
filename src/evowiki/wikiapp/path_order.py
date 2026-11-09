"""A learning path's effective order (unified node graph T7, goals.path_edit): the stored pages
with the user's edits replayed. The one source for every reader of the order — the path views
and 「下一页」 (wikiapp.paths) and the Bot's facts — so
they all see the order the plugin shows. Kept apart from wikiapp.paths so lower modules can read
it without importing the page-writing machinery."""

from evowiki.goals.path_edit import Item, apply_edits, visible
from evowiki.wikiapp.events import PathRecord
from evowiki.wikiapp.state import WikiState


def shown(rec: PathRecord) -> list[Item]:
    """The path's pages as the user sees them (removed ones hidden)."""
    return visible(apply_edits(rec.path.pages, rec.edits))


def startable(rec: PathRecord) -> list[Item]:
    """Shown pages 「下一页」 may reach, in order: not skipped, with a page to teach."""
    return [i for i in shown(rec) if i.page is not None and not i.skipped]


def page_needs(state: WikiState, item: Item) -> set[str]:
    """What a path page builds on: its targets' graph prerequisites and its page's supporting
    concepts (never its own targets)."""
    out = {p for t in item.node_ids for p in state.graph.prereqs(t)}
    if item.page is not None:
        out |= set(item.page.concepts) - set(item.page.targets)
    return out - set(item.node_ids)


def stored_index(rec: PathRecord, key: str) -> int:
    """The page's (0-based) position in the stored path (`rec.page_ids` is aligned with it)."""
    return next(n for n, p in enumerate(rec.path.pages) if p.page_id == key)


def startable_page_ids(rec: PathRecord) -> list[str | None]:
    """The wiki page of each `startable` page, in the order shown (None: not started yet)."""
    return [rec.page_ids[stored_index(rec, i.key)] for i in startable(rec)]


def shown_page_ids(rec: PathRecord) -> list[str | None]:
    """The wiki page of each shown page that has a stored page (what the path view lists), in
    the order shown (None: not started yet)."""
    return [rec.page_ids[stored_index(rec, i.key)] for i in shown(rec) if i.page is not None]
