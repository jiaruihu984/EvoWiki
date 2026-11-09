"""Observable usage for θ_rep (Stage I-B spec §2.2): page_view, quiz_answer and notes count
as use. Ask is deliberately excluded (decision R2): production Ask payloads are text-only and
the simulator's oracle intent fields must never be read. active_domain comes only from content
domain labels and usage counts in the last W sessions — never from V.

Two semantics (Plan 4b-1d design §3, knowledge evidence ≠ usage evidence):
- `usage-v1` (default; θ_rep¹ / manipulation-spec-v4): a quiz answer uses every concept its item
  tags.
- `usage-v2` (θ_rep²): a quiz answer uses only its *focus concept* — the concept of the most
  recent page_view (with a known concept_id) in the same session before the answer — and only
  if the item tags it; otherwise it uses nothing. Other tags still update K (extract_rule_v1 is
  untouched) but create neither usage nor co-usage. Page views and notes count as in v1."""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from evowiki.content.graph import ContentGraph
from evowiki.evidence.models import Event, EventType
from evowiki.view.constraints import WINDOW_SESSIONS

NEVER_SEEN_SESSIONS = 10000
USAGE_V1: Final = "usage-v1"
USAGE_V2: Final = "usage-v2"
USAGE_SEMANTICS: Final = (USAGE_V1, USAGE_V2)


def _used_concepts(ev: Event, graph: ContentGraph) -> list[str]:
    p = ev.payload
    known = set(graph.concept_ids)
    match ev.type:
        case EventType.PAGE_VIEW:
            return [p["concept_id"]] if p.get("concept_id") in known else []
        case EventType.QUIZ_ANSWER:
            return list(graph.item(p["item_id"]).concept_ids)
        case EventType.NOTE_CREATED | EventType.NOTE_EDITED:
            return [p["concept_id"]] if p.get("concept_id") in known else []
        case _:
            return []


def _page_concept(ev: Event, known: frozenset[str]) -> str | None:
    """The concept of a page_view with a known concept_id (hub navigation views have none)."""
    if ev.type is EventType.PAGE_VIEW and ev.payload.get("concept_id") in known:
        return ev.payload["concept_id"]
    return None


def focus_concept(events_sorted: Sequence[Event], i: int, graph: ContentGraph) -> str | None:
    """Concept of the most recent page_view (with a known concept_id) in the same session
    before events_sorted[i]; None if there is none."""
    sid = events_sorted[i].session_id
    known = frozenset(graph.concept_ids)
    for ev in reversed(events_sorted[:i]):
        if ev.session_id != sid:
            continue
        c = _page_concept(ev, known)
        if c is not None:
            return c
    return None


def _used_concepts_v2(ev: Event, focus: str | None, graph: ContentGraph) -> list[str]:
    match ev.type:
        case EventType.PAGE_VIEW | EventType.NOTE_CREATED | EventType.NOTE_EDITED:
            return _used_concepts(ev, graph)
        case EventType.QUIZ_ANSWER:
            tags = graph.item(ev.payload["item_id"]).concept_ids
            return [focus] if focus is not None and focus in tags else []
        case _:
            return []


class UsageIndex:
    def __init__(self, sessions: tuple[str, ...], used: Mapping[str, frozenset[int]],
                 domain_counts: Counter, last_domain: str | None, accepted: int, rejected: int,
                 semantics: str = USAGE_V1):
        self.semantics = semantics
        self.sessions = sessions
        self._used = dict(used)
        self.accepted, self.rejected = accepted, rejected
        top = domain_counts.most_common()
        if not top:
            self.active_domain: str | None = None
        else:
            best = top[0][1]
            tied = sorted(d for d, n in top if n == best)
            self.active_domain = last_domain if last_domain in tied else tied[0]

    @classmethod
    def from_events(cls, events: Iterable[Event], graph: ContentGraph, *,
                    semantics: str = USAGE_V1) -> "UsageIndex":
        if semantics not in USAGE_SEMANTICS:
            raise ValueError(f"unknown usage semantics {semantics!r}")
        evs = sorted(events, key=lambda e: e.seq)
        order: dict[str, int] = {}
        for ev in evs:
            order.setdefault(ev.session_id, len(order))
        lo = len(order) - WINDOW_SESSIONS
        used: dict[str, set[int]] = {}
        counts: Counter = Counter()
        last_domain = None
        acc = rej = 0
        known = frozenset(graph.concept_ids)
        focus: dict[str, str] = {}      # usage-v2: session -> latest page_view concept so far
        for ev in evs:
            s = order[ev.session_id]
            if ev.type is EventType.VIEW_SUGGESTION_ACCEPTED:
                acc += 1
            elif ev.type is EventType.VIEW_SUGGESTION_REJECTED:
                rej += 1
            if semantics == USAGE_V1:
                concepts = _used_concepts(ev, graph)
            else:
                concepts = _used_concepts_v2(ev, focus.get(ev.session_id), graph)
                if (pc := _page_concept(ev, known)) is not None:
                    focus[ev.session_id] = pc
            for c in concepts:
                used.setdefault(c, set()).add(s)
                if s >= lo:
                    counts[graph.concept(c).domain] += 1
                    last_domain = graph.concept(c).domain
        return cls(tuple(order), {c: frozenset(v) for c, v in used.items()}, counts,
                   last_domain, acc, rej, semantics)

    @property
    def n_sessions(self) -> int:
        return len(self.sessions)

    def session_index(self, session_id: str) -> int:
        return self.sessions.index(session_id) if session_id in self.sessions else self.n_sessions

    def window_sessions(self) -> frozenset[int]:
        return frozenset(range(max(0, self.n_sessions - WINDOW_SESSIONS), self.n_sessions))

    def used_in_window(self, c: str) -> bool:
        return bool(self._used.get(c, frozenset()) & self.window_sessions())

    def recent(self) -> frozenset[str]:
        w = self.window_sessions()
        return frozenset(c for c, ss in self._used.items() if ss & w)

    def co_usage_w(self, a: str, b: str) -> float:
        w = self.window_sessions()
        both = self._used.get(a, frozenset()) & self._used.get(b, frozenset()) & w
        return len(both) / WINDOW_SESSIONS

    def last_use(self, c: str) -> int | None:
        """Index of the latest session in which c was used (None if never) — R2."""
        ss = self._used.get(c)
        return None if not ss else max(ss)

    def age_sessions(self, c: str) -> int | None:
        ss = self._used.get(c)
        return None if not ss else self.n_sessions - 1 - max(ss)

    def sessions_since_seen(self, c: str) -> int:
        age = self.age_sessions(c)
        return NEVER_SEEN_SESSIONS if age is None else age

    @property
    def accept_rate(self) -> float:
        return (self.accepted + 1) / (self.accepted + self.rejected + 2)
