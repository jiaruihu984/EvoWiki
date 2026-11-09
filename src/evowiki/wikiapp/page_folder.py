"""Where a page's file lives (`EvoWiki/Wiki/<folder>/<title>.md`; decision 2026-11-04: stable, apart from the graph's
groups): chosen once when the page is created and stored in `PageRecord.folder`, never recomputed, so no change to the
graph or to its grouping moves a file.

A page of a goal goes where the goal's first page is, else into a folder named after the goal. A file has one place: a page
whose first node stands on several goals, written for none of them, goes with the oldest. A page on no goal goes into a
folder named after the top of its node's part_of chain, or into the Wiki root ("") when the node has no parent."""

from evowiki.goals.regions import GoalMap, goal_name
from evowiki.wikiapp.events import PageRecord
from evowiki.wikiapp.state import WikiState

_UNSAFE_FOLDER_CHARS = '\\/／:*?"<>|#^[]'


def folder_name(name: str) -> str:
    """A name as a file or folder name: the plugin's `sanitizeFileName` characters (and the full-width `／`) become `-`."""
    return "".join("-" if c in _UNSAFE_FOLDER_CHARS else c for c in name).strip().rstrip(".")


def first_node(page: PageRecord) -> str | None:
    """The node a page mainly teaches: its first target, else its first concept."""
    return (page.targets or page.concepts or (None,))[0]


def goal_folder(state: WikiState, m: GoalMap) -> str:
    """The folder of the goal's first page, else the goal's name. A stored folder never moves, so a goal keeps the folder its
    first page got, whatever it was called then. A node it shares with an older goal brings that goal's pages on the map:
    they are not this goal's first page (a page's `goal` starts with the goal it was planned for)."""
    ids = [p for n in m.nodes if n.path_id in state.paths for p in state.paths[n.path_id].page_ids]
    ids += [n.page_id for n in m.nodes]
    pages = [state.pages[p] for p in ids if p is not None and p in state.pages and state.pages[p].folder
             and state.pages[p].goal.startswith(m.goal)]
    return min(pages, key=lambda r: r.created_at).folder if pages else folder_name(goal_name(m))


def page_folder(state: WikiState, page: PageRecord) -> str:
    """The folder a new page gets. `page` is not stored yet."""
    node = first_node(page)
    if node is None or state.graph.node(node) is None:
        return ""
    chain, todo = [], [node]
    while todo:
        n = todo.pop(0)
        if n not in chain:
            chain.append(n)
            todo.extend(state.graph.parents(n))
    for n in chain:              # nearest first: its own goal before a goal only an ancestor is on
        on = [m for m in state.maps.values() if m.node(n) is not None]
        if on:
            return goal_folder(state, min(on, key=lambda m: m.created_at))
    if len(chain) == 1:
        return ""
    top = state.graph.node(chain[-1])
    return folder_name(top.name) if top is not None else ""
