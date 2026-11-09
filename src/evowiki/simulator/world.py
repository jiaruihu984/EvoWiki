import re
from dataclasses import dataclass

from evowiki.content.graph import ContentGraph
from evowiki.content.models import split_tag
from evowiki.content.relations import RelationCatalog, build_relation_catalog
from evowiki.simulator.crn import CRN
from evowiki.simulator.irt import ItemParams
from evowiki.simulator.params import DEFAULT_PARAMS, SimParams

GUESS_BY_KIND = {"recall": 0.2, "application": 0.15, "transfer": 0.1}
_TOKEN = re.compile(r"[一-鿿]|[A-Za-z0-9]+")


@dataclass(frozen=True)
class Misconception:
    id: str
    concept_id: str
    delta: float


def _owner(tag: str) -> str:
    parts = split_tag(tag)
    if parts is None:
        raise ValueError(f"tag must look like '<concept_id>:<name>' or '<concept_id>/<name>': {tag!r}")
    return parts[0]


class SimWorld:
    """Learner-independent hidden truth derived from G and a world seed.

    Holds the KC catalog, the misconception catalog (with δ_m), the hidden 3PL item bank and
    the relation catalog with each relation's hidden difficulty d_e (Stage I-B spec §4.1).
    KC and misconception ids come from quiz-item tags of the form '<concept_id>:<name>' (or the
    pipeline's '<concept_id>/<name>'); a concept
    without tagged KCs gets the single KC '<concept_id>:core'.
    """

    def __init__(self, graph: ContentGraph, world_seed: int, params: SimParams | None = None):
        self.graph = graph
        self.world_seed = world_seed
        self.params = params or DEFAULT_PARAMS
        crn = CRN(world_seed)
        self._seg_concept = {s.id: s.concept_id for s in graph.segments}
        self._seg_body = {s.id: s.body for s in graph.segments}
        self._fixes = {f.segment_id: frozenset(f.misconception_tags)
                       for f in graph.misconception_fixes()}
        self._load_cache: dict[tuple[str, int], float] = {}
        kc_sets: dict[str, set[str]] = {cid: set() for cid in graph.concept_ids}
        mc_sets: dict[str, set[str]] = {cid: set() for cid in graph.concept_ids}
        for item_id in graph.item_ids:
            item = graph.item(item_id)
            for tags, sets in ((item.kc_tags, kc_sets), (item.misconception_tags, mc_sets)):
                for tag in tags:
                    owner = _owner(tag)
                    if owner not in item.concept_ids:
                        raise ValueError(f"item {item_id}: tag {tag} is not owned by its concepts")
                    sets[owner].add(tag)
        self._kcs = {
            cid: tuple(sorted(s)) if s else (f"{cid}:core",) for cid, s in kc_sets.items()
        }
        self._kc_owner = {k: cid for cid, ks in self._kcs.items() for k in ks}
        self._misconceptions = {
            m: Misconception(m, cid, 0.4 + 0.4 * crn.uniform("delta", m))
            for cid in graph.concept_ids
            for m in sorted(mc_sets[cid])
        }
        self._item_kcs: dict[str, tuple[str, ...]] = {}
        self._params: dict[str, ItemParams] = {}
        for item_id in sorted(graph.item_ids):
            item = graph.item(item_id)
            if item.kc_tags:
                kcs = tuple(sorted(set(item.kc_tags)))
            else:
                kcs = tuple(sorted({k for cid in item.concept_ids for k in self._kcs[cid]}))
            rng = crn.rng("item", item_id)
            a = rng.uniform(6.0, 10.0)
            b = 0.2 + 0.12 * (item.difficulty - 1) + rng.uniform(-0.05, 0.05)
            raw = [(k, rng.uniform(0.5, 1.5)) for k in kcs]
            total = sum(w for _, w in raw)
            self._item_kcs[item_id] = kcs
            self._params[item_id] = ItemParams(
                a=a, b=b, c=GUESS_BY_KIND[item.kind], weights=tuple((k, w / total) for k, w in raw)
            )

        self.relations: RelationCatalog = build_relation_catalog(graph)
        rp = self.params.relation
        self._rel_d: dict[str, float] = {}
        for e in self.relations.entries:
            eps = rp.eps_scale * (2.0 * crn.uniform("rel_eps", e.relation_id) - 1.0)
            self._rel_d[e.relation_id] = min(1.0, max(0.0, rp.d_base + rp.d_hop * (e.hops - 1)
                                                      + eps))

    @property
    def concept_ids(self) -> list[str]:
        return self.graph.concept_ids

    def kcs_of_concept(self, concept_id: str) -> tuple[str, ...]:
        return self._kcs[concept_id]

    def concept_of_kc(self, kc: str) -> str:
        return self._kc_owner[kc]

    def kcs_of_item(self, item_id: str) -> tuple[str, ...]:
        return self._item_kcs[item_id]

    def item_params(self, item_id: str) -> ItemParams:
        return self._params[item_id]

    def item_misconceptions(self, item_id: str) -> frozenset[str]:
        return frozenset(self.graph.item(item_id).misconception_tags)

    def items_for_concept(self, concept_id: str, primary_only: bool = False) -> tuple[str, ...]:
        """Items testing `concept_id`, sorted by id; `primary_only` keeps items listing it first."""
        out = []
        for item_id in sorted(self.graph.item_ids):
            cids = self.graph.item(item_id).concept_ids
            if (cids[0] == concept_id) if primary_only else (concept_id in cids):
                out.append(item_id)
        return tuple(out)

    def segment_concept(self, segment_id: str) -> str:
        return self._seg_concept[segment_id]

    def segment_load(self, segment_id: str, unit: int) -> float:
        """Normalized token load ℓ of one segment (spec §4.2): CJK characters and Latin words
        count one token each; one load unit is `unit` tokens."""
        key = (segment_id, unit)
        if key not in self._load_cache:
            n = len(_TOKEN.findall(self._seg_body[segment_id]))
            self._load_cache[key] = max(1, n) / unit
        return self._load_cache[key]

    def fix_misconceptions(self, segment_id: str) -> frozenset[str]:
        return self._fixes.get(segment_id, frozenset())

    def relation_endpoints(self, rid: str) -> tuple[str, str]:
        return self.relations.get(rid).endpoints

    def relation_difficulty(self, rid: str) -> float:
        """Hidden relation difficulty d_e = d_base + d_hop·(hops − 1) + ε_e, clipped to [0, 1]."""
        return self._rel_d[rid]

    def relations_of_segment(self, sid: str) -> tuple[str, ...]:
        return self.relations.relations_of_segment(sid)

    @property
    def misconception_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._misconceptions))

    def misconception(self, misconception_id: str) -> Misconception:
        return self._misconceptions[misconception_id]

    def misconceptions_of_concept(self, concept_id: str) -> tuple[Misconception, ...]:
        return tuple(
            m for _, m in sorted(self._misconceptions.items()) if m.concept_id == concept_id
        )

    @property
    def deltas(self) -> dict[str, float]:
        return {m.id: m.delta for m in self._misconceptions.values()}
