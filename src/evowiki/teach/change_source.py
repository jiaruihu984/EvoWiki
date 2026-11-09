"""External change sources (e.g. the knowledge graph's split / merge, track "G"): their rows join the
one RSI change list (GET /wiki/rsi/changes, `RsiCycle.changes`) and their undo goes through the same
route (POST /wiki/rsi/patches/{id}/undo), dispatched by id prefix before the patch / method-change
lookup.

A source owns its rows and writes: `list` returns its ChangeOut rows for the user (any order; the
cycle sorts), `undo` undoes one of its own changes and returns the updated row. Every change id it
issues starts with `prefix` (e.g. "g_"), and prefixes must not collide with patch / method-change
ids. `undo` raises LookupError for an unknown id (404) and ValueError when the change cannot be
undone (409). `rsi_related`: the source's rows are what an RSI round changes, so their undo waits
for the round (the user's cycle lock); the others (a graph split, a ledger row) undo at once.

A source whose rows can wait for the user's review (L2 code patches, status `awaiting_user`) also
has `approve(user_id, change_id, *, now)` / `discard(...)` returning the updated row (POST
/wiki/rsi/patches/{id}/approve | discard; same LookupError / ValueError contract); sources without
them answer 404 there."""

from datetime import datetime
from typing import Protocol

from evowiki.teach.contracts import ChangeOut


class ChangeSource(Protocol):
    prefix: str                                                # e.g. "g_"
    rsi_related: bool

    def list(self, user_id: str) -> list[ChangeOut]: ...

    def undo(self, user_id: str, change_id: str, *, now: datetime) -> ChangeOut: ...


def source_for(sources: "tuple[ChangeSource, ...] | list[ChangeSource]",
               change_id: str) -> ChangeSource | None:
    """The source whose prefix the id starts with (the longest prefix wins), or None."""
    hits = [s for s in sources if s.prefix and change_id.startswith(s.prefix)]
    return max(hits, key=lambda s: len(s.prefix), default=None)
