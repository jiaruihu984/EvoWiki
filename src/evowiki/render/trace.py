"""S3 render inputs from V v1 and the per-page RenderTrace (Stage I-B spec §1.4–1.6, §2.3).
The trace records the role/perspective decisions and the SafetyClamp outcome of one S3 page;
Plan 4b-1c reads it for gates M2/M3/M5/M7. It is never written back to V."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: importing this module must not load the generator side (θ_rep¹ / features),
    # because the service imports the renderer (tests/service/test_demo_view_isolation.py).
    from evowiki.render.context_role import PerspectiveDecision, RoleDecision
    from evowiki.rep.theta import ThetaRep
    from evowiki.view.state import ViewState
    from evowiki.view.usage import UsageIndex


# = evowiki.rep.theta.POLICY_VERSION_V1 (a literal: this module must not import the generator
# side at runtime; tests/render/test_render_v2.py pins the equality).
DEFAULT_POLICY_VERSION = "theta_rep@v1"


@dataclass(frozen=True)
class ViewContext:
    """What the S3 renderer reads besides K: the persistent view V, observable usage and the
    θ_rep parameters (the latter two only feed the context-bridge perspective choice).
    `policy_version` selects the render-time persistent-role eligibility (ruling I3; Plan 4b-1d:
    θ_rep² uses the amended layer-A invariant)."""

    state: ViewState
    usage: UsageIndex
    theta: ThetaRep
    policy_version: str = DEFAULT_POLICY_VERSION


@dataclass(frozen=True)
class RenderTrace:
    role: RoleDecision
    perspective: PerspectiveDecision
    compressible: bool
    clamp_decisions: tuple[tuple[str, str], ...]
    skipped: tuple[tuple[str, str], ...]
    projected_order: tuple[str, ...]
