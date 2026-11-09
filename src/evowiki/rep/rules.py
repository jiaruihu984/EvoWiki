"""θ_rep¹ initial rules (Stage I-B spec §3, B layer; H1 follows design decision D3) and the
θ_rep² rules (Plan 4b-1d design §2: organisation readiness ≠ compression readiness)."""

from evowiki.rep.dsl import (
    Cmp,
    Cond,
    CreateHubAction,
    Feature,
    GroupSubject,
    ParamRef,
    RepRule,
    SetPerspectiveAction,
    SetRoleAction,
)
from evowiki.view.roles import Role

F = Feature
RULES: tuple[RepRule, ...] = (
    RepRule(id="L1", version=1, priority=40,
            when=(Cond(feature=F.M, op=Cmp.LT, value=ParamRef(param="m_learn")),),
            then=(SetRoleAction(role=Role.LEARN),)),
    RepRule(id="L2", version=1, priority=40,
            when=(Cond(feature=F.U, op=Cmp.GT, value=ParamRef(param="u_learn")),),
            then=(SetRoleAction(role=Role.LEARN),)),
    RepRule(id="B1", version=1, priority=30,
            when=(Cond(feature=F.MASTERED, op=Cmp.GE, value=1.0),
                  Cond(feature=F.CO_USAGE_CROSS, op=Cmp.GE, value=ParamRef(param="tau_co")),
                  Cond(feature=F.HAS_GROUNDED_BRIDGE, op=Cmp.GE, value=1.0)),
            then=(SetRoleAction(role=Role.BRIDGE, anchor="best_bridge_anchor"),)),
    RepRule(id="S1", version=1, priority=20,
            when=(Cond(feature=F.MASTERED, op=Cmp.GE, value=1.0),
                  Cond(feature=F.DOWNSTREAM_ACTIVE, op=Cmp.GE, value=1.0)),
            then=(SetRoleAction(role=Role.SUPPORT, anchor="best_support_anchor"),)),
    RepRule(id="F1", version=1, priority=10,
            when=(Cond(feature=F.MASTERED, op=Cmp.GE, value=1.0),),
            then=(SetRoleAction(role=Role.REFERENCE),)),
    RepRule(id="P1", version=1, priority=0,
            when=(Cond(feature=F.USED_IN_WINDOW, op=Cmp.GE, value=1.0),),
            then=(SetPerspectiveAction(),)),
    RepRule(id="H1", version=1, priority=0, subject=GroupSubject(min_size=3), propose=True,
            when=(Cond(feature=F.COHESION, op=Cmp.GT, value=0.5),
                  Cond(feature=F.M, op=Cmp.GE, value=0.6)),
            then=(CreateHubAction(),)),
)

# θ_rep² (Plan 4b-1d design §2). L1/L2 are removed; learn is the fallback. Mastery gates
# reference only. The DSL is conjunctive, so the learn fallback ("not mastered and no grounded
# relational signal") is two rules. The plan text names them L0a/L0b; RepRule ids must match
# ^[A-Z][0-9]+$, so they are L3 (= L0a) and L4 (= L0b) — a naming change only (ledger ruling).
RULES_V2: tuple[RepRule, ...] = (
    RepRule(id="B2", version=1, priority=30,
            when=(Cond(feature=F.CO_USAGE_CROSS, op=Cmp.GE, value=ParamRef(param="tau_co")),
                  Cond(feature=F.HAS_GROUNDED_BRIDGE, op=Cmp.GE, value=1.0)),
            then=(SetRoleAction(role=Role.BRIDGE, anchor="best_bridge_anchor"),)),
    RepRule(id="S2", version=1, priority=20,
            when=(Cond(feature=F.DOWNSTREAM_ACTIVE, op=Cmp.GE, value=1.0),),
            then=(SetRoleAction(role=Role.SUPPORT, anchor="best_support_anchor"),)),
    RULES[4],   # F1 reference: mastered (unchanged object)
    RepRule(id="L3", version=1, priority=1,
            when=(Cond(feature=F.MASTERED, op=Cmp.LT, value=1.0),
                  Cond(feature=F.DOWNSTREAM_ACTIVE, op=Cmp.LT, value=1.0),
                  Cond(feature=F.CO_USAGE_CROSS, op=Cmp.LT, value=ParamRef(param="tau_co"))),
            then=(SetRoleAction(role=Role.LEARN),)),
    RepRule(id="L4", version=1, priority=1,
            when=(Cond(feature=F.MASTERED, op=Cmp.LT, value=1.0),
                  Cond(feature=F.DOWNSTREAM_ACTIVE, op=Cmp.LT, value=1.0),
                  Cond(feature=F.HAS_GROUNDED_BRIDGE, op=Cmp.LT, value=1.0)),
            then=(SetRoleAction(role=Role.LEARN),)),
    RULES[5],   # P1 perspective (unchanged object)
    RULES[6],   # H1 hubs (unchanged object)
)
