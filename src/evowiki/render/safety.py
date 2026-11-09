"""SafetyClamp (Stage I-B spec §1.3, layer A): role may reorder, insert and — only when the
concept is compressible — demote K-visible content to collapsed. It never omits K-visible
content, never touches one_line or formal prerequisite refreshers, and never collapses
anything when the concept is not compressible."""

from typing import NamedTuple

from evowiki.render.plan import SlotSource, SlotType, TemplateVisibility, Visibility

CLAMP_REASONS = ("one_line_always", "formal_prerequisite", "not_compressible",
                 "k_visible_outside_template")


class ClampDecision(NamedTuple):
    visibility: Visibility
    source: SlotSource
    reason: str | None


def clamp(slot_type: SlotType, template_vis: TemplateVisibility | None,
          k_vis: Visibility | None, *, compressible: bool,
          formal_refresher: bool = False) -> ClampDecision:
    if slot_type is SlotType.ONE_LINE:
        if template_vis is TemplateVisibility.ALWAYS:
            return ClampDecision(Visibility.SHOWN, SlotSource.ROLE, None)
        return ClampDecision(Visibility.SHOWN, SlotSource.SAFETY, "one_line_always")
    if formal_refresher:
        return ClampDecision(Visibility.SHOWN, SlotSource.SAFETY, "formal_prerequisite")
    k_visible = k_vis is Visibility.SHOWN
    if template_vis is None:
        if k_visible:
            if compressible:
                return ClampDecision(Visibility.COLLAPSED, SlotSource.SAFETY,
                                     "k_visible_outside_template")
            return ClampDecision(Visibility.SHOWN, SlotSource.SAFETY, "not_compressible")
        return ClampDecision(k_vis or Visibility.OMIT, SlotSource.K, None)
    if template_vis is TemplateVisibility.ALWAYS:
        return ClampDecision(Visibility.SHOWN, SlotSource.ROLE, None)
    if template_vis is TemplateVisibility.K:
        return ClampDecision(k_vis or Visibility.OMIT, SlotSource.K, None)
    if k_visible and not compressible:
        return ClampDecision(Visibility.SHOWN, SlotSource.SAFETY, "not_compressible")
    return ClampDecision(Visibility.COLLAPSED, SlotSource.ROLE, None)
