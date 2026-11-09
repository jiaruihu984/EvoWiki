"""Embedded check items: structural checks only (user ruling 2026-10-11: the separate
verification call was 「多余」). A `CheckItem` already guarantees what makes a question gradable — a
stem, 2–8 distinct options, the key in range — so every check item the organizer or a patch writes is
accepted as is and marked `verified` (the flag means "may count as evidence"). No LLM call."""

from collections.abc import Collection

from evowiki.goals.contracts import PageOutline, SectionRole


def accept_outline_checks(outline: PageOutline, *,
                          only: Collection[str] | None = None) -> PageOutline:
    """`outline` with its check items marked verified (`only`: just these section ids — the
    sections a patch just wrote). Unchanged outline → the same object."""
    targets = {s.id for s in outline.sections
               if s.role is SectionRole.CHECK and s.check is not None and not s.check.verified
               and (only is None or s.id in only)}
    if not targets:
        return outline
    sections = tuple(s.model_copy(update={"check": s.check.model_copy(update={"verified": True})})
                     if s.id in targets else s for s in outline.sections)
    return outline.model_copy(update={"sections": sections})
