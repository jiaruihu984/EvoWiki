import re
from collections.abc import Iterable
from functools import lru_cache

from evowiki.content.models import Concept


def concept_names(concept: Concept) -> list[str]:
    return [n for n in [concept.name_en, *concept.aliases] if n.strip()]


@lru_cache(maxsize=4096)
def _pattern(name: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(name) + r"(?:s|es)?(?![\w-])", re.IGNORECASE)


def mentions(text: str, concept: Concept) -> bool:
    return any(_pattern(n).search(text) for n in concept_names(concept))


def link_concepts(text: str, concepts: Iterable[Concept]) -> set[str]:
    names = sorted(((n, c.id) for c in concepts for n in concept_names(c)),
                   key=lambda t: (-len(t[0]), t[0]))
    taken: list[tuple[int, int]] = []
    found: set[str] = set()
    for name, cid in names:
        for m in _pattern(name).finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append(m.span())
            found.add(cid)
    return found
