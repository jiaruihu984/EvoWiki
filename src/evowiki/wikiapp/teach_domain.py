"""A page's domain for the teaching spec (decisions: "Applicable items for a page"): one of the spec's
item domains, or none, for a page being written. Picked only when the spec has at least one
domain item and the page has no domain yet, or its stored domain no longer matches any domain named
in the spec (e.g. the item was renamed or deleted), in which case it is re-picked.

No model call (LLM-call consolidation stage 4: the LLM tie-break is gone): the page
(`title + goal + concept names`) and each domain label are embedded with the service's embedder
(`retrieval.embed.Embedder`) and the nearest label is the domain when its cosine is at least
MIN_SIM; a weaker nearest label means none of them. No embedder, or the embedder failing (logged),
leaves the domain None for this run and the next write asks again. A page without a domain is not
cut off from the domain items: they reach its writer with their domain named and the writer judges
(`wikiapp.teachspec.applicable`); only a page picked for another domain leaves them out."""

import logging
import math
from collections.abc import Sequence

from evowiki.retrieval.embed import Embedder
from evowiki.teach.contracts import TeachingSpec
from evowiki.wikiapp.kwrite import concept_name
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.teachspec import spec_domains

log = logging.getLogger(__name__)
_WARNED: set[str] = set()

MIN_SIM = 0.5                       # the nearest label's cosine at or above this: the domain


def _names(state: WikiState, page_id: str, user_id: str) -> str:
    rec = state.pages[page_id]
    return "、".join(concept_name(state, c, user_id=user_id) for c in rec.concepts)


def page_text(state: WikiState, page_id: str, *, user_id: str = "") -> str:
    """What is embedded for the page: title, goal and concept names."""
    rec = state.pages[page_id]
    return "\n".join(t for t in (rec.title, rec.goal, _names(state, page_id, user_id)) if t)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb) if na and nb else 0.0


def ranked(embedder: Embedder, text: str, domains: tuple[str, ...]) -> list[tuple[str, float]]:
    """The domains by cosine similarity to `text`, nearest first (ties keep spec order)."""
    q = embedder.embed_query(text)
    vecs = embedder.embed_documents(list(domains))
    return sorted(((d, _cosine(q, v)) for d, v in zip(domains, vecs, strict=True)),
                  key=lambda x: -x[1])


def pick_domain(state: WikiState, spec: TeachingSpec, page_id: str, *, user_id: str = "",
                embedder: Embedder | None = None) -> str | None:
    """The page's domain, one of `spec_domains(spec)` in the spec's spelling, or None (module
    doc). Never calls a model."""
    domains = spec_domains(spec)
    if not domains or embedder is None:
        return None
    try:
        order = ranked(embedder, page_text(state, page_id, user_id=user_id), domains)
    except Exception:  # noqa: BLE001 — any embedder failure (logged): no domain this run
        first = embedder.id not in _WARNED          # the traceback once per embedder
        _WARNED.add(embedder.id)
        (log.warning if first else log.info)(
            "domain embedding failed for %s; no domain this run", page_id, exc_info=first)
        return None
    return order[0][0] if order[0][1] >= MIN_SIM else None
