"""The RSI cycle's `UserData` (evowiki.teach.cycle), built from what the wiki app observes: the user's
aspect log and wiki state. Never anything else (no simulator, no hidden state).

- domain of a concept = the first page in `page_order` whose `concepts` include it (domain id =
  page id, label = page title); a concept on no page is in `OTHER_DOMAIN`.
- `signals_since(t)` = aspect `Observe` events (any kind) with ts > t; None = all of them."""

from collections.abc import Sequence
from datetime import datetime

from evowiki.belief.aspects import AspectEvent, Observe
from evowiki.teach.cycle import UserData
from evowiki.teach.predict import OTHER_DOMAIN
from evowiki.wikiapp.state import WikiState

OTHER_LABEL = "其他"


def domains(state: WikiState) -> tuple[dict[str, str], dict[str, str]]:
    """(concept → domain id, domain id → label)."""
    domain_of: dict[str, str] = {}
    label: dict[str, str] = {OTHER_DOMAIN: OTHER_LABEL}
    for pid in state.page_order:
        page = state.pages.get(pid)
        if page is None:
            continue
        label[pid] = page.title
        for c in page.concepts:
            domain_of.setdefault(c, pid)
    return domain_of, label


def user_data(state: WikiState, events: Sequence[AspectEvent]) -> UserData:
    events = tuple(events)
    observed = tuple(e.ts for e in events if isinstance(e, Observe))
    domain_of, label = domains(state)

    def signals_since(t: datetime | None) -> int:
        return len(observed) if t is None else sum(1 for ts in observed if ts > t)

    return UserData(events=events, domain_of=domain_of, domain_label=label,
                    signals_since=signals_since)
