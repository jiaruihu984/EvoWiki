import math
import re
from collections import Counter
from collections.abc import Iterable

from evowiki.content.models import Concept, Passage
from evowiki.pipeline.linking import concept_names

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class PassageIndex:
    """Deterministic lexical retrieval over passages (pipeline-time only; runtime uses pgvector)."""

    def __init__(self, passages: Iterable[Passage]):
        self._passages = sorted(passages, key=lambda p: p.id)
        self._tf = [Counter(_tokens(p.text)) for p in self._passages]
        self._df: Counter[str] = Counter()
        for tf in self._tf:
            self._df.update(tf.keys())
        self._n = len(self._passages)

    def search(self, phrases: list[str], k: int = 8) -> list[Passage]:
        query = {t for ph in phrases for t in _tokens(ph)}
        scored: list[tuple[float, str, Passage]] = []
        for p, tf in zip(self._passages, self._tf, strict=True):
            score = sum((1 + math.log(tf[t])) * math.log(1 + self._n / self._df[t])
                        for t in query if tf[t])
            lowered = p.text.lower()
            score += 5.0 * sum(1 for ph in phrases if ph.strip() and ph.lower() in lowered)
            if score > 0:
                scored.append((-score, p.id, p))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [p for _, _, p in scored[:k]]

    def for_concepts(self, concepts: Iterable[Concept], k: int = 8) -> list[Passage]:
        return self.search([n for c in concepts for n in concept_names(c)], k)
