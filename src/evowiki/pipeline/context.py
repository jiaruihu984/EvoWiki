from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from evowiki.content.models import Concept, Passage, Source
from evowiki.llm.gateway import Gateway
from evowiki.pipeline.report import StageReport
from evowiki.pipeline.retrieval import PassageIndex
from evowiki.pipeline.review import ReviewQueue
from evowiki.pipeline.sources import IneligibleSourceError, require_eligible


@dataclass(frozen=True)
class StageContext:
    gateway: Gateway
    queue: ReviewQueue
    concepts: Mapping[str, Concept]
    passages: Mapping[str, Passage]
    sources: Mapping[str, Source]
    index: PassageIndex
    spot_check_fraction: float = 0.1
    retrieval_k: int = 8

    @classmethod
    def build(cls, gateway: Gateway, queue: ReviewQueue, concepts: Iterable[Concept],
              passages: Iterable[Passage], sources: Iterable[Source], *,
              spot_check_fraction: float = 0.1, retrieval_k: int = 8) -> "StageContext":
        src = {s.id: s for s in sources}
        pas = {p.id: p for p in passages}
        for p in pas.values():
            if p.source_id not in src:
                raise IneligibleSourceError(f"passage {p.id} has unregistered source {p.source_id}")
            require_eligible(src[p.source_id])
        return cls(gateway=gateway, queue=queue, concepts={c.id: c for c in concepts},
                   passages=pas, sources=src, index=PassageIndex(pas.values()),
                   spot_check_fraction=spot_check_fraction, retrieval_k=retrieval_k)

    def retrieve(self, concepts: list[Concept]) -> list[Passage]:
        return self.index.for_concepts(concepts, k=self.retrieval_k)

    def report(self, stage: str) -> StageReport:
        return StageReport(stage=stage)
