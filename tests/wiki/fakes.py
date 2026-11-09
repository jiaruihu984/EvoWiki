"""Test doubles: a fake aspect K and the z-score → PCA bands."""

from evowiki.goals.contracts import Aspect, Band, CellBelief, cell_id, split_cell

A = Aspect


class FakeK:
    """KView over a fixed concept → aspect → band table."""

    def __init__(self, bands: dict[str, dict[Aspect, Band]]):
        self._b = bands

    def aspects(self, concept_id: str) -> tuple[Aspect, ...]:
        return tuple(self._b.get(concept_id, {}))

    def cell(self, cell: str) -> CellBelief:
        c, a = split_cell(cell)
        band = self._b.get(c, {}).get(a, Band.NONE)
        return CellBelief(cell=cell, m=0.9 if band is Band.KNOWN else 0.3, u=0.1, band=band)

    def cells(self, concept_ids: tuple[str, ...]) -> dict[str, CellBelief]:
        return {cell_id(c, a): self.cell(cell_id(c, a)) for c in concept_ids for a in self.aspects(c)}

    def note(self, concept_id: str) -> tuple:
        return ()


def zscore_pca_k(pca_known: bool = False) -> FakeK:
    k = Band.KNOWN
    return FakeK({
        "mean": {A.TERM: k, A.PROCEDURE: k},
        "var": {A.TERM: k, A.PROCEDURE: k},
        "zscore": {A.TERM: k, A.NOTATION: k, A.PROCEDURE: k},
        "cov": {A.TERM: Band.LIKELY, A.PROCEDURE: Band.SHAKY},
        "pca": {A.PURPOSE: k if pca_known else Band.LIKELY,
                A.PROCEDURE: k if pca_known else Band.GAP},
        "tsne": {A.PURPOSE: Band.GAP},
    })
