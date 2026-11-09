"""Display names for concepts and pages, plus K helpers (all_known: the reference-card check)."""

import logging
from collections.abc import Iterable

from evowiki.goals.contracts import Band, KView, cell_id

log = logging.getLogger(__name__)

UNNAMED_CONCEPT = "未命名概念"
UNNAMED_PAGE = "未命名页面"


def concept_label(name: str | None, concept_id: str | None, *, user_id: str = "") -> str:
    """What a user sees for a concept: its name, else 「未命名概念」 (never the raw id), with a
    warning that carries the user and the concept id."""
    if name:
        return name
    log.warning("concept without a name: user_id=%s concept_id=%s", user_id, concept_id)
    return UNNAMED_CONCEPT


def concept_bands(k: KView, concept_id: str) -> tuple[Band, ...]:
    """Band of every aspect cell of a concept; a cell K does not return counts as no evidence."""
    cells = k.cells((concept_id,))
    return tuple(cells[cid].band if (cid := cell_id(concept_id, a)) in cells else Band.NONE
                 for a in k.aspects(concept_id))


def all_known(k: KView, concept_ids: Iterable[str]) -> bool:
    """Every aspect of every concept is 确定会. Fails closed: no concepts, or a concept without
    aspects, is never all-known."""
    ids = list(concept_ids)
    if not ids:
        return False
    for c in ids:
        bands = concept_bands(k, c)
        if not bands or any(b is not Band.KNOWN for b in bands):
            return False
    return True
