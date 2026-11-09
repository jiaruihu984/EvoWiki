"""Inline page runs for tests that call `organize_stream` / `feedback_rewrite_stream` several times
on one repo: the run store is per repo (as the service's Postgres store is shared), so run ids
(`page:<user>:<page>:<n>`) never repeat on the same repo."""

from weakref import WeakKeyDictionary

from evowiki.wikiapp.page_run_store import MemoryPageRunStore
from evowiki.wikiapp.page_runs import InlinePageRuns

_STORES: "WeakKeyDictionary[object, MemoryPageRunStore]" = WeakKeyDictionary()


def runs_for(repo, **kw) -> InlinePageRuns:
    return InlinePageRuns(store=_STORES.setdefault(repo, MemoryPageRunStore()), **kw)
