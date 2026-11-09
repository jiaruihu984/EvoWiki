"""Layer A of θ_rep¹ (Stage I-B spec §3): immutable system constraints. NOT part of θ —
RSI-B can never patch anything here (tests/rep/test_theta.py enforces this)."""

from collections.abc import Set as AbstractSet
from datetime import datetime
from typing import Final

from evowiki.belief.beta import Belief
from evowiki.content.models import Perspective
from evowiki.view.roles import Role

M_FLOOR: Final = 0.80
U_CEIL: Final = 0.12
R_FLOOR: Final = 0.60
WINDOW_SESSIONS: Final = 3
HYSTERESIS_N: Final = 2
VIEWOP_CAP_PER_SESSION: Final = 3
MAX_PATH_HOPS: Final = 3
MAX_RULES: Final = 30
MAX_CONDITIONS: Final = 4
COMPLEXITY_LAMBDA: Final[float | None] = None   # fixed at pre-registration (Plan 4b-2)
FEATURE_VOCABULARY: Final = (
    "m", "u", "R", "mastered", "compressible", "domain_active", "co_usage_cross",
    "has_grounded_bridge", "downstream_active", "used_in_window", "sessions_since_seen",
    "user_accept_rate", "cohesion")
A_LAYER_NAMES: Final = frozenset({
    "m_floor", "u_ceil", "r_floor", "window_sessions", "hysteresis_n",
    "viewop_cap_per_session", "max_path_hops", "max_rules", "max_conditions",
    "complexity_lambda"})
# Curriculum phase of a content domain (content metadata; used only for trace/readiness labels —
# never inside dom(p), decision D1).
PHASE_OF_DOMAIN: Final = {"la": "LA", "prob": "LA", "ml": "ML", "dl": "DL"}
PHASE_ORDER: Final = {"LA": 0, "ML": 1, "DL": 2}


def mastered(b: Belief) -> bool:
    return b.mastery >= M_FLOOR and b.uncertainty <= U_CEIL


def compressible(b: Belief, now: datetime) -> bool:
    return mastered(b) and b.recall(now) >= R_FLOOR


def persistent_role_allowed(role: Role, b: Belief) -> bool:
    """Spec §1.4: a persistent support/reference/bridge role requires mastered(c)."""
    return role is Role.LEARN or mastered(b)


def persistent_role_allowed_v2(role: Role, b: Belief, *, anchor_grounded: bool) -> bool:
    """Plan 4b-1d design §2 (amended spec §1.4, θ_rep²): support/bridge need a grounded anchor
    (reviewed relation or an approved path of ≤ MAX_PATH_HOPS hops), not mastery; reference
    needs mastered; learn is always allowed. Compression stays guarded by the SafetyClamp."""
    if role is Role.LEARN:
        return True
    if role in (Role.SUPPORT, Role.BRIDGE):
        return anchor_grounded
    return mastered(b)


def perspective_admissible(p: Perspective, mastered_ids: AbstractSet[str]) -> bool:
    """Spec §2.1 hard filter (the graph already holds only publishable segments)."""
    return set(p.presumes_required) <= set(mastered_ids)
