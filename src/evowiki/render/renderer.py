"""Page rendering for Stage I conditions S0–S3 (spec §5.1 steps 2–5, §10.3)."""

from __future__ import annotations

import random
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from evowiki.belief.theta import ThetaInfer
from evowiki.belief.tracer import BeliefState
from evowiki.content.graph import ContentGraph
from evowiki.content.models import Lang
from evowiki.content.relations import RelationCatalog, build_relation_catalog
from evowiki.content.segments import SegmentIndex
from evowiki.render.compose import LEVEL_DRIVEN
from evowiki.render.explore import EPSILON, explore
from evowiki.render.plan import (
    Condition,
    Expandable,
    Refresher,
    RenderPlan,
    RoleSource,
    SlotSource,
)
from evowiki.render.policy import PrereqTreatment, need_score, resolution_level, treatment_for
from evowiki.render.probe import choose_probe
from evowiki.render.templates import learn_slots, legacy_fields, role_slots, static_slots
from evowiki.render.theta import ThetaRender
from evowiki.render.trace import RenderTrace, ViewContext
from evowiki.view.constraints import compressible
from evowiki.view.grounding import concept_domains

if TYPE_CHECKING:
    from evowiki.render.context_role import NavContext


@dataclass(frozen=True)
class RenderRequest:
    plan_id: str
    user_id: str
    session_id: str
    concept_id: str
    now: datetime
    condition: Condition
    policy_version: str
    lang: Lang = Lang.EN
    probes_used: int = 0
    asked_item_ids: AbstractSet[str] = field(default_factory=frozenset)
    # S3 only: how the learner arrived (context role, spec §1.4). S0–S2 ignore it.
    nav: NavContext | None = None


@dataclass(frozen=True)
class RenderContext:
    graph: ContentGraph
    segments: SegmentIndex
    theta_rep: ThetaRender
    theta_infer: ThetaInfer
    relations: RelationCatalog | None = None

    def __post_init__(self) -> None:
        if self.relations is None:
            object.__setattr__(self, "relations", build_relation_catalog(self.graph))


def _prereq_segment_id(ctx: RenderContext, cid: str, lang: Lang) -> str:
    seg = ctx.segments.prereq_segment(cid, lang)
    if seg is None:
        raise ValueError(f"no approved one_line or definition segment for prerequisite {cid}")
    return seg.id


def _static_plan(req: RenderRequest, ctx: RenderContext) -> RenderPlan:
    links = tuple(
        Expandable(segment_id=_prereq_segment_id(ctx, p, req.lang), concept_id=p,
                   role="prerequisite")
        for p, _ in ctx.graph.direct_prerequisites(req.concept_id)
    )
    slots = static_slots(req.concept_id, ctx.segments, req.lang, ctx.theta_rep, ctx.relations,
                         links)
    # legacy_fields already lists the prerequisite links after the (empty) section expandables.
    segments_selected, expandables = legacy_fields(req.concept_id, slots)
    return RenderPlan(
        id=req.plan_id, user_id=req.user_id, session_id=req.session_id,
        concept_id=req.concept_id, condition=req.condition, policy_version=req.policy_version,
        ts=req.now, lang=req.lang,
        resolution_by_section=dict.fromkeys(LEVEL_DRIVEN, 4),
        segments_selected=segments_selected, expandables=expandables,
        explored=False, propensity=1.0, slots=slots,
    )


def _prerequisites(
    req: RenderRequest, state: BeliefState, ctx: RenderContext
) -> tuple[tuple[Refresher, ...], tuple[Expandable, ...]]:
    th = ctx.theta_rep
    refreshers: list[Refresher] = []
    blocks: list[Expandable] = []
    for p, kind in ctx.graph.direct_prerequisites(req.concept_id):
        sid = _prereq_segment_id(ctx, p, req.lang)
        if treatment_for(kind, state.beliefs[p], req.now, th.refresher) is PrereqTreatment.REFRESHER:
            refreshers.append(Refresher(concept_id=p, segment_id=sid))
        else:
            blocks.append(Expandable(segment_id=sid, concept_id=p, role="prerequisite"))
    return tuple(refreshers), tuple(blocks)


def _adaptive_plan(
    req: RenderRequest, state: BeliefState, ctx: RenderContext, rng: random.Random,
    epsilon: float,
) -> RenderPlan:
    th = ctx.theta_rep
    target = state.beliefs[req.concept_id]
    level = resolution_level(need_score(target, req.now, th.resolution), th.resolution)
    ex = explore(dict.fromkeys(LEVEL_DRIVEN, level), rng, epsilon)
    refreshers, prereq_blocks = _prerequisites(req, state, ctx)
    slots = learn_slots(req.concept_id, ex.levels, ctx.segments, req.lang, th, ctx.relations,
                        refreshers, prereq_blocks)
    segments_selected, expandables = legacy_fields(req.concept_id, slots)

    probe = choose_probe(req.concept_id, state, ctx.graph, th, ctx.theta_infer, req.now,
                         req.probes_used, req.asked_item_ids)
    return RenderPlan(
        id=req.plan_id, user_id=req.user_id, session_id=req.session_id,
        concept_id=req.concept_id, condition=req.condition, policy_version=req.policy_version,
        ts=req.now, lang=req.lang,
        resolution_by_section=ex.levels,
        segments_selected=segments_selected,
        refreshers=refreshers,
        expandables=expandables,
        probe=probe, explored=ex.explored, propensity=ex.propensity, slots=slots,
    )


def _s3_plan(req: RenderRequest, state: BeliefState, ctx: RenderContext, view: ViewContext,
             rng: random.Random, epsilon: float) -> tuple[RenderPlan, RenderTrace]:
    """S3 (+K+V v1): K-driven depth (identical rng draws to S2), then role and perspective
    resolution (default vs context, joint fallback R3), the role template under the
    SafetyClamp, and the RenderTrace. Context decisions are never written back to V."""
    # Imported here, not at module level: the service imports this module and must not load
    # the generator side (features / θ_rep¹) — tests/service/test_demo_view_isolation.py.
    from evowiki.render.context_role import (
        INELIGIBLE_AT_RENDER,
        MISSING_SUPPORTED_CONTENT,
        enforce_persistent_eligibility,
        finalize,
        resolve_perspective,
        resolve_role,
    )
    from evowiki.view.eligibility import eligibility_for
    from evowiki.view.features import FeatureContext, has_reviewed_relation

    th = ctx.theta_rep
    target = state.beliefs[req.concept_id]
    level = resolution_level(need_score(target, req.now, th.resolution), th.resolution)
    ex = explore(dict.fromkeys(LEVEL_DRIVEN, level), rng, epsilon)  # identical draws to S2
    refreshers, prereq_blocks = _prerequisites(req, state, ctx)
    fctx = FeatureContext(graph=ctx.graph, relations=ctx.relations,
                            beliefs=dict(state.beliefs), usage=view.usage, now=req.now,
                            params=view.theta.params)
    role = resolve_role(req.concept_id, view.state, req.nav, ctx.graph, ctx.relations)
    persp = resolve_perspective(req.concept_id, role, view.state, fctx)
    anchor_ok = role.anchor is not None and has_reviewed_relation(ctx.relations,
                                                                  req.concept_id, role.anchor)
    role, persp = finalize(req.concept_id, role, persp, view.state, fctx,
                           anchor_connection_available=anchor_ok)  # joint fallback (R3)
    # spec §1.4 at render (ruling I3); the eligibility follows V's policy version (Plan 4b-1d)
    role = enforce_persistent_eligibility(role, target,
                                          policy=eligibility_for(view.policy_version),
                                          concept=req.concept_id, graph=ctx.graph,
                                          relations=ctx.relations)
    comp = compressible(target, req.now)
    # A context fallback keeps source CONTEXT but renders the default page (ruling I2); an
    # ineligible default renders learn, whose slots are the S2 learn slots either way.
    context_page = (role.source is RoleSource.CONTEXT
                    and role.reason not in (MISSING_SUPPORTED_CONTENT, INELIGIBLE_AT_RENDER))
    src = SlotSource.CONTEXT if context_page else SlotSource.ROLE
    lay = role_slots(role.effective, req.concept_id, role.anchor, persp.effective, ex.levels,
                     ctx.segments, req.lang, th, ctx.relations, refreshers, prereq_blocks,
                     compressible=comp, role_source=src)
    segments_selected, expandables = legacy_fields(req.concept_id, lay.slots)
    probe = choose_probe(req.concept_id, state, ctx.graph, th, ctx.theta_infer, req.now,
                         req.probes_used, req.asked_item_ids)
    plan = RenderPlan(
        id=req.plan_id, user_id=req.user_id, session_id=req.session_id,
        concept_id=req.concept_id, condition=req.condition, policy_version=req.policy_version,
        view_version=view.state.version, ts=req.now, lang=req.lang,
        primary_hub_id=view.state.primary_hub(req.concept_id, view.usage.active_domain,
                                              concept_domains(ctx.graph)),
        perspective_segment_id=persp.effective,
        resolution_by_section=ex.levels, segments_selected=segments_selected,
        refreshers=refreshers, expandables=expandables, probe=probe,
        explored=ex.explored, propensity=ex.propensity, slots=lay.slots,
        role_default=role.default, role_requested=role.requested,
        role_effective=role.effective, role_source=role.source, role_anchor=role.anchor,
        perspective_default=persp.default, perspective_effective=persp.effective,
        perspective_source=persp.source, collapse_blocked=lay.collapse_blocked)
    trace = RenderTrace(
        role=role, perspective=persp, compressible=comp, clamp_decisions=lay.decisions,
        skipped=tuple((s.slot_type.value, s.skip_reason) for s in lay.slots if s.skip_reason),
        projected_order=tuple(s.slot_type.value for s in lay.slots
                              if s.source in (SlotSource.ROLE, SlotSource.CONTEXT)))
    return plan, trace


def render_page_traced(
    req: RenderRequest,
    state: BeliefState,
    ctx: RenderContext,
    rng: random.Random,
    epsilon: float = EPSILON,
    *,
    view: ViewContext | None = None,
) -> tuple[RenderPlan, RenderTrace | None]:
    """Build the RenderPlan for one concept page, plus the RenderTrace on S3 (None for S0–S2).
    Pure: never mutates `state` or `view`.

    S0 and S1 render the same static canonical page and draw nothing from rng. S2 adapts depth
    to K. S3 adds the personal view V and must receive a `ViewContext`; S0–S2 must not
    receive a view. Any other condition fails closed with NotImplementedError.
    """
    ctx.graph.concept(req.concept_id)  # KeyError for unknown concepts
    if req.condition in (Condition.S0, Condition.S1, Condition.S2) and view is not None:
        raise ValueError(f"condition {req.condition} must not read the personal view V")
    match req.condition:
        case Condition.S0 | Condition.S1:
            return _static_plan(req, ctx), None
        case Condition.S2:
            return _adaptive_plan(req, state, ctx, rng, epsilon), None
        case Condition.S3:
            if view is None:
                raise ValueError("condition S3 requires a ViewContext")
            if not isinstance(view, ViewContext):
                raise TypeError("S3 requires ViewContext")
            return _s3_plan(req, state, ctx, view, rng, epsilon)
        case _:
            raise NotImplementedError(
                f"Rendering for condition {req.condition} is not implemented"
            )


def render_page(
    req: RenderRequest,
    state: BeliefState,
    ctx: RenderContext,
    rng: random.Random,
    epsilon: float = EPSILON,
    *,
    view: ViewContext | None = None,
) -> RenderPlan:
    """`render_page_traced` without the trace."""
    return render_page_traced(req, state, ctx, rng, epsilon, view=view)[0]
