"""θ_rep¹ = (B: typed rules, C: bounded params, ThetaRender). Layer A lives in
evowiki.view.constraints and is deliberately absent from this schema."""

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evowiki.render.theta import ThetaRender
from evowiki.rep.dsl import RepRule, complexity_report
from evowiki.view.constraints import M_FLOOR, MAX_RULES, U_CEIL

POLICY_VERSION_V1 = "theta_rep@v1"
POLICY_VERSION_V2 = "theta_rep@v2"
# R1 (2026-10-05): θ_rep² rules, params and eligibility + exposure-bounded role persistence
# (view/persistence.py). The ThetaRep object is θ_rep² (ThetaRep.v2()); the policy version
# selects the persistence layer.
POLICY_VERSION_V3 = "theta_rep@v3"
# R1.1 (2026-10-05): θ_rep² + application-bounded role persistence — a protected role's competing
# SetRole winners go through history, resolve and hysteresis as under θ_rep² and are withheld only
# at application, reserving the cap they would have consumed (view/persistence.py). v3 unchanged.
POLICY_VERSION_V4 = "theta_rep@v4"
# R2 (2026-10-05): θ_rep@v4 (R1.1) + one bridge-only co-usage change — a cross-domain pair (c, x)
# is co-used iff |last_use_session(c) - last_use_session(x)| <= W - 1 (view/features.py). W, N,
# cap, priorities, eligibility, templates and all R1.1 behaviour are unchanged.
POLICY_VERSION_V5 = "theta_rep@v5"
# R2.1 (2026-10-06): θ_rep@v5 + a concept-scoped bridge lease with K = 1 refractory
# (view/lease.py): an effective bridge exposure lapses the bridge back to the pre-lease
# (role, anchor); the next bridge application waits for one non-bridge exposure. Everything
# else (R2 co-location, R1.1, W, N, cap, priorities, eligibility, templates) is unchanged.
POLICY_VERSION_V6 = "theta_rep@v6"
_SIMPLEX_TOL = 1e-9


class RepParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    m_learn: float = Field(0.85, ge=M_FLOOR, le=0.95)
    u_learn: float = Field(0.10, ge=0.03, le=U_CEIL)
    tau_co: float = Field(0.34, ge=0.1, le=1.0)
    w_dom: float = Field(0.40, ge=0.0, le=1.0)
    w_co: float = Field(0.35, ge=0.0, le=1.0)
    w_rec: float = Field(0.25, ge=0.0, le=1.0)
    w_gap: float = Field(0.30, ge=0.0, le=1.0)
    tau_rec: float = Field(2.0, ge=0.5, le=10.0)
    theta_min: float = Field(0.20, ge=0.0, le=0.8)
    delta_switch: float = Field(0.10, ge=0.02, le=0.5)

    @model_validator(mode="after")
    def _simplex(self) -> "RepParams":
        if abs(self.w_dom + self.w_co + self.w_rec - 1.0) > _SIMPLEX_TOL:
            raise ValueError("(w_dom, w_co, w_rec) must sum to 1")
        return self


class ThetaRep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    rules: tuple[RepRule, ...] = ()
    params: RepParams = Field(default_factory=RepParams)
    render: ThetaRender = Field(default_factory=ThetaRender.default)

    @model_validator(mode="after")
    def _bounded(self) -> "ThetaRep":
        if len(self.rules) > MAX_RULES:
            raise ValueError(f"at most {MAX_RULES} rules")
        ids = [r.id for r in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError("rule ids must be unique")
        return self

    @property
    def complexity(self) -> int:
        return complexity_report(self.rules).total

    def rule(self, rule_id: str) -> RepRule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(rule_id)

    @classmethod
    def initial(cls) -> "ThetaRep":
        from evowiki.rep.rules import RULES
        return cls(rules=RULES)

    @classmethod
    def v2(cls) -> "ThetaRep":
        """θ_rep² (Plan 4b-1d): new B-layer rules; C-layer params and render = defaults."""
        from evowiki.rep.rules import RULES_V2
        return cls(rules=RULES_V2)


def patch_whitelist(theta: ThetaRep) -> frozenset[str]:
    """JSON-pointer prefixes RSI-B may patch: B-layer rules and C-layer params/render fields."""
    paths = {"/rules"} | {f"/params/{k}" for k in RepParams.model_fields}
    paths |= {f"/render/{k}" for k in type(theta.render).model_fields}
    return frozenset(paths)


def theta_rep_hash(theta: ThetaRep) -> str:
    text = json.dumps(theta.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()
