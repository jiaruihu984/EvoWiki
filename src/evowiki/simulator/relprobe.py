"""Observable relation probes (`sim-relprobe-v1`;
the product specification).

A relation probe is a short in-session quiz ("how are X and Y related?") asked by the system and
answered by the simulated learner. It exists so that relation learning, which is otherwise
silent, has an observable correlate.

- Item pool: one item per catalogue relation, `relprobe:<relation_id>`. Parameters are drawn from
  `CRN(world_seed)` with key `"rel_probe_item"`, distinct from the transfer post-test's
  `"post_item"`, within the post-test's *seen* ranges (S4a). Probe items are therefore never the
  post-test items, even for C_test relations.
- Answer model: the transfer post-test's model evaluated at the probe time
  (`SimLearner.answer_relation_probe`).
- Measurement only (S1a): answering adds no trace, no acquisition and no practice, does not
  advance the learner clock, and is logged on a separate observable channel (`probe_events`).
  The main event stream, the hidden state and every other draw are identical with probes on or
  off."""

from dataclasses import dataclass

from evowiki.simulator.crn import CRN
from evowiki.simulator.world import SimWorld

RELPROBE_VERSION = "sim-relprobe-v1"
ITEM_PREFIX = "relprobe:"
PROBE_SLOT_TYPES = frozenset({"connection", "perspective"})


@dataclass(frozen=True)
class RelProbeItem:
    relation_id: str
    a: float
    b: float
    c: float
    w_r: float
    w_end: float

    @property
    def item_id(self) -> str:
        return f"{ITEM_PREFIX}{self.relation_id}"


def relprobe_item(world: SimWorld, relation_id: str) -> RelProbeItem:
    """Deterministic in (world seed, relation id, post-test params); computed on demand."""
    world.relations.get(relation_id)  # KeyError for a relation outside the catalog
    pp = world.params.posttest
    rng = CRN(world.world_seed).rng("rel_probe_item", relation_id)
    return RelProbeItem(relation_id, a=rng.uniform(*pp.a_range), b=rng.uniform(*pp.b_seen),
                        c=rng.uniform(*pp.c_seen), w_r=pp.w_r_seen, w_end=pp.w_end_seen)


def probeable_relations(world: SimWorld, page) -> frozenset[str]:
    """Relations bound to a connection / perspective segment SHOWN on `page` (S2a scope)."""
    return frozenset(rid for slot in page.slots or ()
                     if slot.slot_type in PROBE_SLOT_TYPES and slot.visibility == "shown"
                     for seg in slot.segment_ids for rid in world.relations_of_segment(seg))
