"""θ_rep¹ rule DSL (layer B; Stage I-B spec §3). A condition compares a frozen-vocabulary
feature with a constant or with a layer-C parameter (ParamRef). Complexity is computed."""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evowiki.view.constraints import FEATURE_VOCABULARY, MAX_CONDITIONS
from evowiki.view.ops import Salience
from evowiki.view.roles import Role

Feature = StrEnum("Feature", {name.upper(): name for name in FEATURE_VOCABULARY})
FEATURE_RANGE = MappingProxyType({
    "m": (0.0, 1.0), "u": (0.0, 0.5), "R": (0.0, 1.0), "mastered": (0.0, 1.0),
    "compressible": (0.0, 1.0), "domain_active": (0.0, 1.0), "co_usage_cross": (0.0, 1.0),
    "has_grounded_bridge": (0.0, 1.0), "downstream_active": (0.0, 500.0),
    "used_in_window": (0.0, 1.0), "sessions_since_seen": (0.0, 10000.0),
    "user_accept_rate": (0.0, 1.0), "cohesion": (0.0, 1.0)})
MAX_ACTIONS = 2


class Cmp(StrEnum):
    GT = ">"
    LT = "<"
    GE = ">="
    LE = "<="


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ParamRef(_Frozen):
    param: Literal["m_learn", "u_learn", "tau_co", "theta_min"]


class Cond(_Frozen):
    feature: Feature
    op: Cmp
    value: float | ParamRef

    @model_validator(mode="after")
    def _in_range(self) -> "Cond":
        if isinstance(self.value, float):
            lo, hi = FEATURE_RANGE[self.feature.value]
            if not lo <= self.value <= hi:
                raise ValueError(f"{self.feature} constant {self.value} outside [{lo}, {hi}]")
        return self

    def test(self, x: float, params) -> bool:
        v = getattr(params, self.value.param) if isinstance(self.value, ParamRef) else self.value
        return {Cmp.GT: x > v, Cmp.LT: x < v, Cmp.GE: x >= v, Cmp.LE: x <= v}[self.op]


class ConceptSubject(_Frozen):
    kind: Literal["concept"] = "concept"


class GroupSubject(_Frozen):
    kind: Literal["group"] = "group"
    min_size: int = Field(ge=2, le=6)


Subject = Annotated[ConceptSubject | GroupSubject, Field(discriminator="kind")]
AnchorRule = Literal["best_bridge_anchor", "best_support_anchor"]


class SetRoleAction(_Frozen):
    kind: Literal["SET_ROLE"] = "SET_ROLE"
    role: Role
    anchor: AnchorRule | None = None

    @model_validator(mode="after")
    def _anchor_matches_role(self) -> "SetRoleAction":
        need = {Role.BRIDGE: "best_bridge_anchor", Role.SUPPORT: "best_support_anchor"}
        if need.get(self.role) != self.anchor:
            raise ValueError(f"role {self.role} requires anchor {need.get(self.role)!r}")
        return self


class SetPerspectiveAction(_Frozen):
    kind: Literal["SET_PERSPECTIVE"] = "SET_PERSPECTIVE"


class SurfaceLinkAction(_Frozen):
    kind: Literal["SURFACE_LINK"] = "SURFACE_LINK"


class AttachAction(_Frozen):
    kind: Literal["ATTACH"] = "ATTACH"
    salience: Salience = 0.5


class CreateHubAction(_Frozen):
    kind: Literal["CREATE_HUB"] = "CREATE_HUB"


Action = Annotated[SetRoleAction | SetPerspectiveAction | SurfaceLinkAction | AttachAction
                   | CreateHubAction, Field(discriminator="kind")]
CONCEPT_ACTIONS = frozenset({"SET_ROLE", "SET_PERSPECTIVE", "SURFACE_LINK", "ATTACH"})
GROUP_ACTIONS = frozenset({"CREATE_HUB"})


class RepRule(_Frozen):
    id: str = Field(pattern=r"^[A-Z][0-9]+$")
    version: int = Field(ge=1)
    priority: int = Field(ge=0, le=100)
    subject: Subject = ConceptSubject()
    when: tuple[Cond, ...] = Field(min_length=1, max_length=MAX_CONDITIONS)
    then: tuple[Action, ...] = Field(min_length=1, max_length=MAX_ACTIONS)
    propose: bool = False

    @model_validator(mode="after")
    def _well_typed(self) -> "RepRule":
        kinds = [a.kind for a in self.then]
        if len(set(kinds)) != len(kinds):
            raise ValueError("an action kind may appear at most once per rule")
        allowed = GROUP_ACTIONS if isinstance(self.subject, GroupSubject) else CONCEPT_ACTIONS
        if not set(kinds) <= allowed:
            raise ValueError(f"actions {kinds} do not fit subject {self.subject.kind}")
        if "CREATE_HUB" in kinds and not self.propose:
            raise ValueError("structural actions must be proposals")
        if len({(c.feature, c.op, c.value) for c in self.when}) != len(self.when):
            raise ValueError("duplicate condition")
        return self

    @property
    def specificity(self) -> int:
        return len(self.when)

    def constants(self) -> int:
        n = len(self.when)
        n += 1 if isinstance(self.subject, GroupSubject) else 0
        n += sum(1 for a in self.then if isinstance(a, AttachAction))
        return n


@dataclass(frozen=True)
class ComplexityReport:
    rule_count: int
    condition_count: int
    constant_count: int

    @property
    def total(self) -> int:
        return self.rule_count + self.condition_count + self.constant_count


def complexity_report(rules) -> ComplexityReport:
    rules = tuple(rules)
    return ComplexityReport(len(rules), sum(len(r.when) for r in rules),
                            sum(r.constants() for r in rules))


def _action_text(a, subj: str) -> str:
    if isinstance(a, SetRoleAction):
        anchor = f", anchor={a.anchor}" if a.anchor else ""
        return f"SET_ROLE({subj}, {a.role.value}{anchor})"
    return f"{a.kind}({subj})"


def format_rule(r: RepRule) -> str:
    def val(c: Cond) -> str:
        return c.value.param if isinstance(c.value, ParamRef) else f"{c.value:g}"
    subj = "c" if isinstance(r.subject, ConceptSubject) else "S"
    head = f"rule {r.id} (v{r.version}) priority {r.priority}"
    if isinstance(r.subject, GroupSubject):
        head += f" over groups(min_size={r.subject.min_size})"
    when = " AND ".join(f"{c.feature.value}({subj}) {c.op.value} {val(c)}" for c in r.when)
    then = "; ".join(_action_text(a, subj) for a in r.then)
    return f"{head}:\n  WHEN {when}\n  THEN {'PROPOSE ' if r.propose else ''}{then}"
