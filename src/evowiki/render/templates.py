"""Page templates as ordered slots (Stage I-B spec §1.2, §4.0). Template structure lives in
code and is outside θ (RSI-B cannot change it). S2 renders every concept with LEARN_TEMPLATE;
S0 renders the static full page. S3 lays a page out from the effective role (`role_slots`):
learn is the S2 learn template; support/reference/bridge follow spec §1.2 under the
SafetyClamp (§1.3, `render.safety`)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from evowiki.content.models import PUBLISHABLE, Lang, SegmentKind
from evowiki.content.relations import RelationCatalog
from evowiki.content.segments import SegmentIndex
from evowiki.render.compose import SHOW_LEVEL
from evowiki.render.plan import (
    SECTION_SLOT_TYPES,
    Expandable,
    Refresher,
    Slot,
    SlotSource,
    SlotType,
    TemplateVisibility,
    Visibility,
)
from evowiki.render.theta import ThetaRender
from evowiki.view.roles import Role

# TemplateVisibility moved to render.plan (Plan 4b-1b); it stays importable from here.

LEARN_TEMPLATE: tuple[tuple[SlotType, TemplateVisibility], ...] = (
    (SlotType.ONE_LINE, TemplateVisibility.ALWAYS),
    (SlotType.INTUITION, TemplateVisibility.K),
    (SlotType.DEFINITION, TemplateVisibility.ALWAYS),
    (SlotType.DERIVATION, TemplateVisibility.K),
    (SlotType.EXAMPLE, TemplateVisibility.K),
    (SlotType.MISCONCEPTION_FIX, TemplateVisibility.K),
    (SlotType.CONNECTION, TemplateVisibility.COLLAPSED),
    (SlotType.ADVANCED, TemplateVisibility.COLLAPSED),
)
NO_SEGMENT = "no_segment"


def k_visibility(kind: SegmentKind, level: int) -> Visibility:
    show = SHOW_LEVEL[kind]
    if show is None:
        return Visibility.COLLAPSED
    if level >= show:
        return Visibility.SHOWN
    return Visibility.COLLAPSED if level == show - 1 else Visibility.OMIT


def _section_ids(cid: str, kind: SegmentKind, level: int, segments: SegmentIndex, lang: Lang,
                 theta: ThetaRender) -> tuple[str, ...]:
    segs = segments.renderable(cid, kind, lang)
    n = theta.max_examples if kind is SegmentKind.EXAMPLE and level == 4 else 1
    return tuple(s.id for s in segs[:n])


def _skip(slot_type: SlotType, source: SlotSource = SlotSource.K) -> Slot:
    return Slot(slot_type=slot_type, visibility=Visibility.OMIT, source=source,
                skip_reason=NO_SEGMENT)


def prerequisite_slots(refreshers: Sequence[Refresher],
                       blocks: Sequence[Expandable]) -> list[Slot]:
    out = [Slot(slot_type=SlotType.PREREQ_REFRESHER, segment_ids=(r.segment_id,),
                visibility=Visibility.SHOWN, source=SlotSource.K,
                k_visibility=Visibility.SHOWN, concept_id=r.concept_id) for r in refreshers]
    out += [Slot(slot_type=SlotType.PREREQ_LINK, segment_ids=(b.segment_id,),
                 visibility=Visibility.COLLAPSED, source=SlotSource.K,
                 k_visibility=Visibility.COLLAPSED, concept_id=b.concept_id) for b in blocks]
    return out


def connection_slots(concept_id: str, relations: RelationCatalog,
                     visibility: Visibility) -> list[Slot]:
    return [Slot(slot_type=SlotType.CONNECTION, segment_ids=(e.primary_connection_segment_id,),
                 visibility=visibility, source=SlotSource.K, relation_id=e.relation_id)
            for e in relations.for_concept(concept_id)]


def learn_slots(concept_id: str, levels: Mapping[SegmentKind, int], segments: SegmentIndex,
                lang: Lang, theta: ThetaRender, relations: RelationCatalog,
                refreshers: Sequence[Refresher],
                prereq_blocks: Sequence[Expandable]) -> tuple[Slot, ...]:
    out: list[Slot] = []
    for slot_type, tv in LEARN_TEMPLATE:
        if slot_type is SlotType.CONNECTION:
            out += connection_slots(concept_id, relations, Visibility.COLLAPSED)
            continue
        kind = SegmentKind(slot_type.value)
        level = levels.get(kind, 4)
        ids = _section_ids(concept_id, kind, level, segments, lang, theta)
        if not ids:
            out.append(_skip(slot_type))
        else:
            kv = k_visibility(kind, level)
            vis = {TemplateVisibility.K: kv, TemplateVisibility.ALWAYS: Visibility.SHOWN,
                   TemplateVisibility.COLLAPSED: Visibility.COLLAPSED}[tv]
            out.append(Slot(slot_type=slot_type, segment_ids=ids, visibility=vis,
                            source=SlotSource.K, k_visibility=kv))
        if slot_type is SlotType.ONE_LINE:
            out += prerequisite_slots(refreshers, prereq_blocks)
    return tuple(out)


def static_slots(concept_id: str, segments: SegmentIndex, lang: Lang, theta: ThetaRender,
                 relations: RelationCatalog,
                 prereq_blocks: Sequence[Expandable]) -> tuple[Slot, ...]:
    """S0 (spec §4.0): every approved segment of every section in θ.order, prerequisite links
    after the one-line, all connections shown at the end in relation_id order."""
    out: list[Slot] = []
    for kind in theta.order:
        slot_type = SlotType(kind.value)
        ids = tuple(s.id for s in segments.renderable(concept_id, kind, lang))
        out.append(Slot(slot_type=slot_type, segment_ids=ids, visibility=Visibility.SHOWN,
                        source=SlotSource.K, k_visibility=Visibility.SHOWN) if ids
                   else _skip(slot_type))
        if kind is SegmentKind.ONE_LINE:
            out += prerequisite_slots((), prereq_blocks)
    out += connection_slots(concept_id, relations, Visibility.SHOWN)
    return tuple(out)


def legacy_fields(concept_id: str,
                  slots: Sequence[Slot]) -> tuple[tuple[str, ...], tuple[Expandable, ...]]:
    """Plan 3 fields derived from slots: shown section segments in slot order; collapsed
    sections, then collapsed prerequisite links, as expandables."""
    selected = tuple(sid for s in slots if s.slot_type in SECTION_SLOT_TYPES
                     and s.visibility is Visibility.SHOWN for sid in s.segment_ids)
    sections = tuple(Expandable(segment_id=s.segment_ids[0], concept_id=concept_id,
                                role="section") for s in slots
                     if s.slot_type in SECTION_SLOT_TYPES and s.visibility is Visibility.COLLAPSED)
    prereqs = tuple(Expandable(segment_id=s.segment_ids[0], concept_id=s.concept_id or "",
                               role="prerequisite") for s in slots
                    if s.slot_type is SlotType.PREREQ_LINK)
    return selected, sections + prereqs


# --- S3 role templates (Stage I-B spec §1.2) -------------------------------------------------
# Markers for template positions that are not a single concept section.
ANCHOR_CONNECTION, PERSPECTIVE, PREREQ_REVIEW, PREREQ_LINKS = (
    "ANCHOR_CONNECTION", "PERSPECTIVE", "PREREQ_REVIEW", "PREREQ_LINKS")
NO_REVIEWED_CONNECTION = "no_reviewed_connection"
NO_PERSPECTIVE = "no_perspective"
TV = TemplateVisibility
SUPPORT_TEMPLATE = ((SlotType.ONE_LINE, TV.ALWAYS), (SlotType.DEFINITION, TV.ALWAYS),
                    (ANCHOR_CONNECTION, TV.ALWAYS), (SlotType.INTUITION, TV.COLLAPSED),
                    (SlotType.DERIVATION, TV.COLLAPSED), (SlotType.EXAMPLE, TV.COLLAPSED),
                    (SlotType.MISCONCEPTION_FIX, TV.COLLAPSED), (SlotType.ADVANCED, TV.COLLAPSED))
REFERENCE_TEMPLATE = ((SlotType.ONE_LINE, TV.ALWAYS), (SlotType.DEFINITION, TV.ALWAYS),
                      (SlotType.EXAMPLE, TV.COLLAPSED), (SlotType.DERIVATION, TV.COLLAPSED),
                      (PREREQ_LINKS, TV.K))
BRIDGE_TEMPLATE = ((PERSPECTIVE, TV.ALWAYS), (ANCHOR_CONNECTION, TV.ALWAYS),
                   (SlotType.ONE_LINE, TV.ALWAYS), (PREREQ_REVIEW, TV.K),
                   (SlotType.DERIVATION, TV.COLLAPSED))
ROLE_TEMPLATES = {Role.SUPPORT: SUPPORT_TEMPLATE, Role.REFERENCE: REFERENCE_TEMPLATE,
                  Role.BRIDGE: BRIDGE_TEMPLATE}


@dataclass(frozen=True)
class RoleLayout:
    """A role's slot layout. `decisions` lists `(entry, reason)` for every clamp or skip
    decision that carries a reason; `collapse_blocked` is set when any decision has reason
    not_compressible (the SafetyClamp kept a template collapse SHOWN, or kept a non-anchor
    connection COLLAPSED instead of omitting it, because the concept is not compressible)."""

    slots: tuple[Slot, ...]
    collapse_blocked: bool
    decisions: tuple[tuple[str, str], ...]


def role_slots(role: Role, concept_id: str, anchor: str | None, perspective_id: str | None,
               levels: Mapping[SegmentKind, int], segments: SegmentIndex, lang: Lang,
               theta: ThetaRender, relations: RelationCatalog,
               refreshers: Sequence[Refresher], prereq_blocks: Sequence[Expandable], *,
               compressible: bool, role_source: SlotSource = SlotSource.ROLE) -> RoleLayout:
    """Lay out a page for `role` (spec §1.2) under the SafetyClamp (§1.3). Learn is exactly the
    S2 learn template. The SafetyClamp baseline of each section is its learn-template
    visibility. Missing reviewed connection/perspective content (including a perspective_id
    that is not a publishable PERSPECTIVE segment of concept_id) is skipped with a
    skip_reason, never generated. Connections other than the anchor's are outside every role
    template: on a compressible page they are omitted; on a non-compressible page they keep
    their learn visibility (COLLAPSED, source SAFETY, reason not_compressible), in learn order
    after the template sections (final-review ruling I1, spec §1.5/§1.3)."""
    from evowiki.content.relations import relation_id
    from evowiki.render.safety import clamp
    from evowiki.view.features import has_reviewed_relation

    if role is Role.LEARN:
        return RoleLayout(learn_slots(concept_id, levels, segments, lang, theta, relations,
                                      refreshers, prereq_blocks), False, ())
    template = ROLE_TEMPLATES[role]
    slots: list[Slot] = []
    reasons: list[tuple[str, str]] = []

    def src(source: SlotSource) -> SlotSource:
        return role_source if source is SlotSource.ROLE else source

    learn_tv = dict(LEARN_TEMPLATE)

    def section(st: SlotType) -> tuple[tuple[str, ...], Visibility, Visibility]:
        """(ids, k_visibility as learn_slots records it, clamp baseline). The baseline is the
        section's S2 learn-template visibility, so a non-compressible role page never shows
        less than the learn page would."""
        kind = SegmentKind(st.value)
        level = levels.get(kind, 4)
        kv = k_visibility(kind, level)
        baseline = {TemplateVisibility.K: kv, TemplateVisibility.ALWAYS: Visibility.SHOWN,
                    TemplateVisibility.COLLAPSED: Visibility.COLLAPSED}[learn_tv[st]]
        return _section_ids(concept_id, kind, level, segments, lang, theta), kv, baseline

    def valid_perspective(pid: str | None) -> bool:
        if pid is None:
            return False
        try:
            seg = segments.segment(pid)
        except KeyError:
            return False
        return (seg.kind is SegmentKind.PERSPECTIVE and seg.concept_id == concept_id
                and seg.review_status in PUBLISHABLE)

    def prerequisites(tv: TemplateVisibility | None) -> list[Slot]:
        out = []
        for s in prerequisite_slots(refreshers, prereq_blocks):
            if s.slot_type is SlotType.PREREQ_REFRESHER:
                d = clamp(SlotType.PREREQ_REFRESHER, tv, s.k_visibility,
                          compressible=compressible, formal_refresher=True)
                if d.reason is not None:
                    reasons.append((f"{SlotType.PREREQ_REFRESHER.value}:{s.concept_id}",
                                    d.reason))
                s = s.model_copy(update={"visibility": d.visibility, "source": src(d.source)})
            out.append(s)  # prerequisite links stay COLLAPSED, source K
        return out

    listed = {entry for entry, _ in template}
    for entry, tv in template:
        if entry == ANCHOR_CONNECTION:
            if anchor is not None and has_reviewed_relation(relations, concept_id, anchor):
                e = relations.get(relation_id(concept_id, anchor))
                slots.append(Slot(slot_type=SlotType.CONNECTION,
                                  segment_ids=(e.primary_connection_segment_id,),
                                  visibility=Visibility.SHOWN, source=role_source,
                                  relation_id=e.relation_id))
            else:
                slots.append(Slot(slot_type=SlotType.CONNECTION, visibility=Visibility.OMIT,
                                  source=role_source, skip_reason=NO_REVIEWED_CONNECTION))
                reasons.append((SlotType.CONNECTION.value, NO_REVIEWED_CONNECTION))
        elif entry == PERSPECTIVE:
            if valid_perspective(perspective_id):
                slots.append(Slot(slot_type=SlotType.PERSPECTIVE, segment_ids=(perspective_id,),
                                  visibility=Visibility.SHOWN, source=role_source))
            else:
                slots.append(Slot(slot_type=SlotType.PERSPECTIVE, visibility=Visibility.OMIT,
                                  source=role_source, skip_reason=NO_PERSPECTIVE))
                reasons.append((SlotType.PERSPECTIVE.value, NO_PERSPECTIVE))
        elif entry in (PREREQ_REVIEW, PREREQ_LINKS):
            slots += prerequisites(tv)
        else:
            ids, kv, baseline = section(entry)
            if not ids:
                slots.append(_skip(entry, role_source))
                reasons.append((entry.value, NO_SEGMENT))
            else:
                d = clamp(entry, tv, baseline, compressible=compressible)
                if d.reason is not None:
                    reasons.append((entry.value, d.reason))
                slots.append(Slot(slot_type=entry, segment_ids=ids, visibility=d.visibility,
                                  source=src(d.source), k_visibility=kv))
        if entry is SlotType.ONE_LINE and not listed & {PREREQ_REVIEW, PREREQ_LINKS}:
            slots += prerequisites(None)

    for st, _ in LEARN_TEMPLATE:
        if st is SlotType.CONNECTION:
            if not compressible:
                # Final I1 (spec §1.5/§1.3): the learn page carries every connection COLLAPSED;
                # without compressible nothing may be downgraded, so the non-anchor connections
                # stay COLLAPSED here (learn order, primary segment, one segment per slot).
                shown = {s.relation_id for s in slots
                         if s.slot_type is SlotType.CONNECTION and s.segment_ids}
                for c in connection_slots(concept_id, relations, Visibility.COLLAPSED):
                    if c.relation_id in shown:
                        continue
                    reasons.append((f"{SlotType.CONNECTION.value}:{c.relation_id}",
                                    "not_compressible"))
                    slots.append(c.model_copy(update={"source": SlotSource.SAFETY}))
            continue  # compressible: non-anchor connections are outside the template, omitted
        if st in listed:
            continue
        ids, kv, baseline = section(st)
        if not ids:
            continue
        d = clamp(st, None, baseline, compressible=compressible)
        if d.reason is not None:
            reasons.append((st.value, d.reason))
        slots.append(Slot(slot_type=st, segment_ids=ids, visibility=d.visibility,
                          source=src(d.source), k_visibility=kv))
    return RoleLayout(tuple(slots), any(r == "not_compressible" for _, r in reasons),
                      tuple(reasons))
