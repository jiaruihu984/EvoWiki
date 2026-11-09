"""Hidden connection-transfer post-test (Stage I-B spec §4.4). Implemented in Plan 4b-1a and
NOT run in Plan 4b-1: every call requires a PosttestAuthorization, which can only be built from
a manipulation record whose core gate passed and whose hashes match. This module returns
per-item responses only; aggregation into accuracy belongs to Plan 4b-2.

PosttestAuthorization is SELF-CERTIFYING: `expected` is supplied by the caller, so any caller can
mint an authorization by passing a record that matches its own `expected`. It is an accident
guard only. In Plan 4b-1 the actual enforcement is (1) the AST guard in
tests/experiments/test_no_efficacy_guard.py, (2) the transitive sys.modules check that the
manipulation package never loads this module, and (3) the one-way process latch in
evowiki.simulator.efficacy_lock, which makes every call here raise once locked. Plan 4b-2 must
load `expected` from the frozen, committed freeze/manipulation manifests instead of taking it
as a caller argument."""

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from evowiki.simulator.actr import activation, retrievability
from evowiki.simulator.crn import CRN
from evowiki.simulator.efficacy_lock import efficacy_locked
from evowiki.simulator.params import POSTTEST_VERSION, SimParams
from evowiki.simulator.truth import HiddenFinalState
from evowiki.simulator.world import SimWorld

# POSTTEST_VERSION is defined in params (re-exported here) so the freeze can record it without
# loading this module.
__all__ = ["POSTTEST_VERSION", "REQUIRED_RECORD_KEYS", "PosttestAuthorization",
           "PosttestNotAuthorized", "TransferItem", "TransferResponse",
           "administer_transfer_posttest", "transfer_items"]
REQUIRED_RECORD_KEYS = ("core_pass", "manipulation_spec_version", "theta_rep_hash",
                        "content_version", "simulator_version", "relation_catalog_hash",
                        "C_test_hash", "random_key_scheme_version")
_ISSUE = object()


class PosttestNotAuthorized(PermissionError):
    """The transfer post-test was called without a passing, hash-matching manipulation record."""


@dataclass(frozen=True)
class PosttestAuthorization:
    """Proof that a passing, hash-matching manipulation record was presented.

    Self-certifying: `expected` comes from the caller, so this is an accident guard, not a
    security boundary (see the module docstring). Plan 4b-2 must source `expected` from the
    frozen, committed freeze/manipulation manifests.
    """

    record_digest: str
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _ISSUE:
            raise PosttestNotAuthorized("build authorizations with from_manipulation_record")

    @classmethod
    def from_manipulation_record(cls, record: Mapping[str, object], *,
                                 expected: Mapping[str, str]) -> "PosttestAuthorization":
        if record.get("core_pass") is not True:
            raise PosttestNotAuthorized("the manipulation core gate has not passed")
        bad = [k for k in REQUIRED_RECORD_KEYS[1:]
               if k not in expected or record.get(k) != expected[k]]
        if bad:
            raise PosttestNotAuthorized(f"manipulation record does not match: {bad}")
        text = json.dumps(dict(record), sort_keys=True, default=str)
        return cls(hashlib.sha256(text.encode()).hexdigest(), _ISSUE)


@dataclass(frozen=True)
class TransferItem:
    relation_id: str
    transfer_kind: Literal["seen", "near"]
    a: float
    b: float
    c: float
    w_r: float
    w_end: float


@dataclass(frozen=True)
class TransferResponse:
    relation_id: str
    transfer_kind: Literal["seen", "near"]
    z: float
    p_correct: float
    correct: bool


def transfer_items(world: SimWorld, c_test_ids: Iterable[str],
                   params: SimParams | None = None) -> tuple[TransferItem, ...]:
    """Item parameters per (relation, kind); `params` defaults to `world.params`."""
    pp = (params or world.params).posttest
    crn = CRN(world.world_seed)
    out = []
    for rid in sorted(set(c_test_ids)):
        world.relations.get(rid)  # KeyError for a relation outside the catalog
        for kind in ("near", "seen"):
            rng = crn.rng("post_item", rid, kind)
            b_lo, b_hi = pp.b_near if kind == "near" else pp.b_seen
            c_lo, c_hi = pp.c_near if kind == "near" else pp.c_seen
            out.append(TransferItem(
                rid, kind, a=rng.uniform(*pp.a_range), b=rng.uniform(b_lo, b_hi),
                c=rng.uniform(c_lo, c_hi),
                w_r=pp.w_r_near if kind == "near" else pp.w_r_seen,
                w_end=pp.w_end_near if kind == "near" else pp.w_end_seen))
    return tuple(out)


def _rho(traces: tuple, d: float, tau: float, s: float, at) -> float:
    return retrievability(activation(traces, at, d), tau, s) if traces else 0.0


def administer_transfer_posttest(final: HiddenFinalState, world: SimWorld,
                                 c_test_ids: Iterable[str], *,
                                 auth: PosttestAuthorization | None,
                                 params: SimParams | None = None
                                 ) -> tuple[TransferResponse, ...]:
    """Per-item responses at last study session start + delay_days (policy-independent time,
    spec §4.4). `params` defaults to `world.params`."""
    if efficacy_locked():
        raise PosttestNotAuthorized("efficacy is locked for this process")
    if not isinstance(auth, PosttestAuthorization):
        raise PosttestNotAuthorized("administer_transfer_posttest requires an authorization")
    params = params or world.params
    at = final.last_session_start + timedelta(days=params.posttest.delay_days)
    traces = dict(final.traces)
    rel_traces = dict(final.rel_traces)

    def level(cid: str) -> float:
        kcs = world.kcs_of_concept(cid)
        return sum(_rho(traces.get(k, ()), final.decay_d, final.tau, final.rho_scale, at)
                   if k in final.known else 0.0 for k in kcs) / len(kcs)

    crn = CRN(final.seed, namespace=(final.learner_type,))
    out = []
    for item in transfer_items(world, c_test_ids, params):
        a_end, b_end = world.relation_endpoints(item.relation_id)
        r = 1.0 if item.relation_id in final.rel_known else 0.0
        rho_e = _rho(rel_traces.get(item.relation_id, ()), final.decay_d * final.rel_decay_mult,
                     final.tau, final.rho_scale, at)
        penalty = sum(world.misconception(m).delta for m in final.misconceptions
                      if world.misconception(m).concept_id in (a_end, b_end))
        z = item.w_r * r * rho_e + item.w_end * (level(a_end) + level(b_end)) / 2.0 - penalty
        p = item.c + (1.0 - item.c) / (1.0 + math.exp(-item.a * (z - item.b)))
        correct = crn.bernoulli(p, "transfer_post", item.relation_id, item.transfer_kind)
        out.append(TransferResponse(item.relation_id, item.transfer_kind, z, p, correct))
    return tuple(out)
