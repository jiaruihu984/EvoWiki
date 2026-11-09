"""Reading assistant tools (plan rev 3.2 §9.1, lane F2). Each tool answers one thing the model
cannot do alone; see the product specification"""

from collections.abc import Mapping, Sequence

from evowiki.agent.read_k import ReadKTool
from evowiki.agent.read_page import ReadPageTool
from evowiki.agent.source_tools import ListSourcesTool, ReadSourceTool, SearchSourceTool
from evowiki.agent.tools import Tool
from evowiki.assistant.tools.fetch_url import Fetcher, FetchUrlTool
from evowiki.assistant.tools.memory import ReadPastTurnTool, SearchMemoryTool
from evowiki.assistant.tools.propose import ProposePageEditTool
from evowiki.assistant.tools.run_python import RunPythonTool
from evowiki.assistant.tools.search_wiki import ContentSearch, SearchWikiTool, VectorContentSearch
from evowiki.assistant.tools.web_search import SearchProvider, TavilyProvider, WebSearchTool
from evowiki.forge.sandbox import Sandbox
from evowiki.goals.contracts import KView
from evowiki.memory.ports import MemoryPorts
from evowiki.sources.reader import SourceReader
from evowiki.wikiapp.page_reader import PageStore

__all__ = [
    "VectorContentSearch",
    "build_tools",
]


def build_tools(*, pages: PageStore, kview: KView, content: ContentSearch | None = None,
                search: SearchProvider | None = None, fetcher: Fetcher | None = None,
                names: Mapping[str, str] | None = None,
                more: Sequence[Tool] = (),
                sources: SourceReader | None = None,
                sandbox: Sandbox | None = None,
                memory: MemoryPorts | None = None) -> dict[str, Tool]:
    """The six tools for ONE turn of ONE user, keyed by name, plus `more` (the read and
    action tools of `WikiTools`: `tools.read_more`, `tools.actions`); with `sources`
    (the user's materials) also list_sources / read_source / search_source; with `sandbox` also
    run_python; with `memory` (cross-session memory) also search_memory / read_past_turn.

    Build once per turn for `ctx.user_id`: `kview` must be that user's K at the turn's as_of, and
    the result must never be cached across users or turns (read_k would show stale or another
    user's K). `pages` is scoped by `ctx.user_id` at call time.

    What F1 still supplies: `content` — a VectorContentSearch holds a psycopg connection, so give
    it a per-turn connection (taken from the pool for the turn, returned after); `search` — omitted
    means Tavily from TAVILY_API_KEY (unset → web_search returns an explicit error); `fetcher` —
    omitted means the guarded UrlFetcher; `names` — concept id → display name for read_k. Network
    control (confirming calls that carry page text, a per-turn network cap) is also F1's."""
    tools: list[Tool] = [
        SearchWikiTool(pages, content),
        ReadPageTool(pages),
        ReadKTool(kview, names),
        WebSearchTool(search or TavilyProvider.from_env()),
        FetchUrlTool(fetcher),
        ProposePageEditTool(pages),
        *more,
    ]
    if sources is not None:
        tools += [ListSourcesTool(sources), ReadSourceTool(sources, image_pages=True),
                  SearchSourceTool(sources)]
    if sandbox is not None:
        tools.append(RunPythonTool(sandbox))
    if memory is not None:
        tools += [SearchMemoryTool(memory), ReadPastTurnTool(memory)]
    return {t.spec.name: t for t in tools}
