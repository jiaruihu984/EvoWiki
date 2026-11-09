"""Simulator parameters for Stage I-B (spec §4.1, §4.2, §4.4). Defaults are priors set in
Plan 4b-1a before any run; they are frozen in Task 11 (spec §4.5). Changing any value after the
freeze requires a new freeze version with a written reason."""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from math import log

ATTENTION_MODEL_VERSION = "attn-v1"
POSTTEST_VERSION = "transfer-posttest-v1"


@dataclass(frozen=True)
class AttentionParams:
    mu_C: float = log(8.0)      # C_u ~ LogNormal(mu_C, sigma_C), in load units, all types
    sigma_C: float = 0.35
    a0: float = 3.0             # P(attend_1) = sigmoid(a0) ≈ 0.95
    a_pos: float = 0.15
    a_load: float = 1.0
    load_unit_tokens: int = 50  # one load unit = 50 normalized tokens


@dataclass(frozen=True)
class RelationParams:
    p_rel_prior: float = 0.3
    p_rel_base: float = 0.5
    kappa_persp: float = 0.5
    d_base: float = 0.2
    d_hop: float = 0.15
    eps_scale: float = 0.1      # eps_e ~ U(-eps_scale, eps_scale), keyed by (world_seed, rid)
    rel_decay_mult: float = 1.0 # relation ACT-R decay = learner decay_d * rel_decay_mult


@dataclass(frozen=True)
class PosttestParams:
    w_r_seen: float = 1.5
    w_end_seen: float = 1.0
    w_r_near: float = 2.5
    w_end_near: float = 0.25
    a_range: tuple[float, float] = (1.5, 3.0)
    b_seen: tuple[float, float] = (0.6, 1.0)
    b_near: tuple[float, float] = (1.8, 2.2)
    c_seen: tuple[float, float] = (0.15, 0.25)
    c_near: tuple[float, float] = (0.02, 0.08)
    delay_days: float = 1.0     # post-test time: last study session start + delay_days


@dataclass(frozen=True)
class SimParams:
    attention: AttentionParams = field(default_factory=AttentionParams)
    relation: RelationParams = field(default_factory=RelationParams)
    posttest: PosttestParams = field(default_factory=PosttestParams)

    def to_json(self) -> dict:
        return json.loads(json.dumps(asdict(self)))

    def digest(self) -> str:
        text = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()


DEFAULT_PARAMS = SimParams()
