"""Hand-built aspect K for organizer-lane tests: a `contracts.KView` whose bands are set directly
(m / u are representative values of the band; the organizer lane reads bands, never thresholds),
plus the z-score page of plan rev 3 Phase 1: mean and variance procedure shaky, the ±1 reading of
z unknown, everything else known."""

from evowiki.goals.contracts import Aspect, Band, CellBelief, NoteEntry, cell_id, split_cell
from evowiki.goals.guardrails import PageConcept, PageSpec

A = Aspect
_MU = {Band.KNOWN: (0.9, 0.08), Band.LIKELY: (0.6, 0.25), Band.SHAKY: (0.4, 0.15),
       Band.GAP: (0.1, 0.1), Band.NONE: (0.3, 0.3)}


class FakeK:
    def __init__(self, aspects: dict[str, tuple[Aspect, ...]],
                 bands: dict[str, Band] | None = None, *, default: Band = Band.NONE,
                 notes: dict[str, tuple[NoteEntry, ...]] | None = None):
        self._aspects = dict(aspects)
        self._bands = {cell_id(c, a): default for c, asp in aspects.items() for a in asp}
        for cell, band in (bands or {}).items():
            self.set(cell, band)
        self._notes = dict(notes or {})

    def aspects(self, concept_id: str) -> tuple[Aspect, ...]:
        return self._aspects.get(concept_id, ())

    def cell(self, cell: str) -> CellBelief:
        band = self._bands[cell]
        m, u = _MU[band]
        return CellBelief(cell=cell, m=m, u=u, direct=1.0, band=band)

    def cells(self, concept_ids: tuple[str, ...]) -> dict[str, CellBelief]:
        return {x: self.cell(x) for c in concept_ids for x in (cell_id(c, a) for a in self.aspects(c))}

    def note(self, concept_id: str) -> tuple[NoteEntry, ...]:
        return self._notes.get(concept_id, ())

    def set(self, cell: str, band: Band | str) -> None:
        concept, aspect = split_cell(cell)
        if aspect not in self._aspects.get(concept, ()):
            raise KeyError(cell)
        self._bands[cell] = Band(band)


ZSCORE_ASPECTS: dict[str, tuple[Aspect, ...]] = {
    "mean": (A.TERM, A.NOTATION, A.PROCEDURE),
    "variance": (A.TERM, A.NOTATION, A.PROCEDURE),
    "z_score": (A.PURPOSE, A.NOTATION, A.PROCEDURE, A.INTERPRET),
}
ZSCORE_WEAK = {"mean.procedure": Band.SHAKY, "variance.procedure": Band.SHAKY,
               "z_score.interpret": Band.GAP}


def zscore_k(overrides: dict[str, Band] | None = None) -> FakeK:
    return FakeK(ZSCORE_ASPECTS, {**ZSCORE_WEAK, **(overrides or {})}, default=Band.KNOWN)


def zscore_spec(**kw) -> PageSpec:
    base = {"page_id": "p-z", "goal": "我想学 z-score 标准化",
            "concepts": (PageConcept(id="mean", name="均值"),
                         PageConcept(id="variance", name="方差", prereqs=("mean",)),
                         PageConcept(id="z_score", name="z-score",
                                     prereqs=("mean", "variance"))),
            "targets": ("z_score",)}
    return PageSpec(**{**base, **kw})


# A passing outline for the z-score page (the plan's acceptance shape), as LLM JSON.
ZSCORE_OUTLINE = {
    "profile": "你会算 z，也熟悉均值、方差的定义；方差的计算有点模糊，还不清楚 z = ±1 意味着什么。",
    "assumed": ["mean.term", "mean.notation", "variance.term", "variance.notation"],
    "sections": [
        {"id": "s1", "heading": "z 的公式", "role": "line",
         "cells": ["z_score.purpose", "z_score.notation", "z_score.procedure"],
         "why": "z 的用途、公式、计算你都确定会", "notes": "直接给 z = (x − μ)/σ"},
        {"id": "s2", "heading": "回顾：均值与方差怎么算", "role": "recap",
         "cells": ["mean.procedure", "variance.procedure"],
         "why": "两者的计算你模糊", "notes": "成对给出两个公式"},
        {"id": "s3", "heading": "z = ±1 意味着什么", "role": "explain",
         "cells": ["z_score.interpret"], "why": "解读你还不会",
         "notes": "68/95/99.7，偏斜分布时什么不成立", "analogy_sources": ["z_score.notation"]},
    ],
}
