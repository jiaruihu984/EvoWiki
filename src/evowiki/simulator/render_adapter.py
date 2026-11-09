"""The single boundary between L3's RenderPlan and the simulator.

The simulator only ever consumes its own `PageStimulus`. This module is the one place allowed
to read RenderPlan fields, and it reads them through the Protocols below. It does not import
RenderPlan (evowiki.render.plan), which keeps the simulator independent of L3. The learner sees
only the page's slots (type, segment ids, visibility) and a few page-level fields listed in
PLAN_FIELDS_READ / SLOT_FIELDS_READ; an AST test pins both whitelists. If L3 renames or reshapes
a field, test_real_render_plan_is_compatible fails here.
"""

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from types import MappingProxyType
from typing import Protocol

from evowiki.simulator.stimulus import DETAIL_RANK, PageStimulus, SlotStimulus


class SlotView(Protocol):
    @property
    def slot_type(self) -> str: ...
    @property
    def segment_ids(self) -> Sequence[str]: ...
    @property
    def visibility(self) -> str: ...


class ProbeView(Protocol):
    @property
    def item_id(self) -> str: ...


class RenderPlanView(Protocol):
    """The subset of RenderPlan (evowiki.render.plan) the simulated learner reacts to.

    Members are read-only properties so frozen models (tuple fields, str-enum kinds) satisfy it.
    """

    @property
    def id(self) -> str: ...
    @property
    def concept_id(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def primary_hub_id(self) -> str | None: ...
    @property
    def slots(self) -> Sequence[SlotView]: ...
    @property
    def probe(self) -> ProbeView | None: ...
    @property
    def qa_enabled(self) -> bool: ...


PLAN_FIELDS_READ: frozenset[str] = frozenset(
    {"id", "concept_id", "kind", "primary_hub_id", "slots", "probe", "qa_enabled"})
SLOT_FIELDS_READ: frozenset[str] = frozenset({"slot_type", "segment_ids", "visibility"})
_SECTIONS = frozenset(DETAIL_RANK)


def stimulus_from_render_plan(
    plan: RenderPlanView, *, hub_members: Mapping[str, AbstractSet[str]],
    perspective_concepts: Mapping[str, AbstractSet[str]],
    misconception_fix_segments: Mapping[str, AbstractSet[str]],
    segment_concepts: Mapping[str, str],
    segment_kinds: Mapping[str, str] = MappingProxyType({}),
) -> PageStimulus:
    """What the learner sees (Stage I-B spec §4): the page's slots in order with segment ids
    and visibility. Never reads role, perspective scores, sources, skip reasons, K levels or
    the condition. Legacy PageStimulus fields are derived from the slots plus content maps."""
    if plan.kind != "page":
        raise ValueError(f"only page RenderPlans are shown to the learner, got {plan.kind!r}")
    if not plan.slots:
        raise ValueError("RenderPlan has no slots; the simulator reads only slots")
    slots = tuple(SlotStimulus(str(slot.slot_type), tuple(slot.segment_ids), str(slot.visibility))
                  for slot in plan.slots if str(slot.visibility) != "omit")
    shown = [s for s in slots if s.visibility == "shown"]
    hub = plan.primary_hub_id
    persp = next((s.segment_ids[0] for s in slots if s.slot_type == "perspective"), None)

    def rank(sid: str) -> int | None:
        return DETAIL_RANK.get(segment_kinds.get(sid, ""))

    prereqs = [(segment_concepts[s.segment_ids[0]], s.segment_ids[0])
               for s in slots if s.slot_type == "prereq_link"]
    refreshers = [(segment_concepts[s.segment_ids[0]], s.segment_ids[0])
                  for s in shown if s.slot_type == "prereq_refresher"]
    section_shown = [sid for s in shown if s.slot_type in _SECTIONS for sid in s.segment_ids]
    return PageStimulus(
        concept_id=plan.concept_id,
        refreshers=frozenset(c for c, _ in refreshers),
        expandables=frozenset(c for c, _ in prereqs),
        misconception_fixes=frozenset(
            m for s in shown if s.slot_type == "misconception_fix"
            for sid in s.segment_ids for m in misconception_fix_segments.get(sid, ())),
        sections=tuple(sid for s in shown if s.slot_type != "prereq_refresher"
                       for sid in s.segment_ids),
        refresher_segments=tuple(sorted(refreshers)),
        expandable_segments=tuple(sorted(prereqs)),
        hub_members=frozenset(hub_members[hub]) if hub is not None else None,
        perspective_concepts=frozenset(perspective_concepts[persp]) if persp else None,
        render_plan_id=plan.id,
        page_depth=None,
        section_ranks=tuple((sid, r) for sid in section_shown if (r := rank(sid)) is not None),
        section_expandables=tuple(sorted(
            ((s.segment_ids[0], r) for s in slots if s.visibility == "collapsed"
             and s.slot_type in _SECTIONS and (r := rank(s.segment_ids[0])) is not None),
            key=lambda t: (t[1], t[0]))),
        probe_item_id=plan.probe.item_id if plan.probe is not None else None,
        qa_enabled=bool(plan.qa_enabled),
        slots=slots,
    )
