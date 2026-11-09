"""Per-section structured logs (`SectionLog`) for the sections a flow just produced: which cells,
their bands at that moment (from the K view), the role, the organizer's `why`, the spec items in
force and their version, the θ_infer version, and whether the final outline still passes the
deterministic guardrails for that section.

Written by the flows in one `section_logs_saved` event in the SAME commit as the `page_saved`
that produced the sections (organize → `organizer`, deeper/other rewrite → `writer`, lazy patch
→ `patch`). Lookups for later readers: `WikiState.section_log(page_id, section_id)` (latest per
section; history stays in the event log) and `log_versions` below."""

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Literal

from evowiki.belief.aspects import AspectK, AspectKView
from evowiki.goals.contracts import KView, PageOutline
from evowiki.goals.guardrails import (
    PageSpec,
    check_outline,
    diagnose_outline,
    draft_of,
    page_bands,
)
from evowiki.teach.contracts import SectionLog
from evowiki.wikiapp.events import PageRecord, SectionLogsSaved, WikiEvent, WriteKind, WriteMark
from evowiki.wikiapp.kwrite import page_spec
from evowiki.wikiapp.repo import Snapshot
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.teachspec import PageTeach, SpecInvalid, effective_version, page_teach

WHY_MAX = 600
Producer = Literal["organizer", "writer", "patch", "refine", "edit"]     # SectionLog.producer


def failing_sections(outline: PageOutline, spec: PageSpec, k: KView) -> frozenset[str]:
    """Sections a guardrail violation or a diagnostic names when the final outline is re-checked
    (`check_outline` + `diagnose_outline` - diagnostics refuse nothing, but they are the signal
    RSI-B reads); page-level ones name none."""
    draft, bands = draft_of(outline), page_bands(spec, k)
    violations = check_outline(draft, spec, bands) + diagnose_outline(draft, spec, bands)
    return frozenset(v.section for v in violations if v.section is not None)


def section_logs(outline: PageOutline, section_ids: Iterable[str] | None, *, producer: Producer,
                 spec: PageSpec, k: KView, teach: PageTeach, at: datetime,
                 infer_version: int) -> tuple[SectionLog, ...]:
    """One log per section of `outline` (page order) whose id is in `section_ids` (None: all).
    `spec` is the PageSpec the flow generated with (its guidelines = `teach.guidelines`); `k` the
    K view the flow used; `infer_version` the θ_infer,u version K was built with
    (`Snapshot.infer_version`, R2's track-A registry)."""
    wanted = None if section_ids is None else set(section_ids)
    bad = failing_sections(outline, spec, k)
    return tuple(
        SectionLog(page_id=outline.page_id, section_id=s.id, at=at, producer=producer,
                   cells=s.cells, bands={c: k.cell(c).band.value for c in s.cells},
                   role=s.role.value, why=s.why[:WHY_MAX], spec_items=teach.item_ids,
                   spec_version=teach.version, infer_version=infer_version,
                   guardrails_ok=s.id not in bad)
        for s in outline.sections if wanted is None or s.id in wanted)


def logs_event(logs: Sequence[SectionLog], *, new_id: Callable[[str], str],
               now: datetime) -> list[WikiEvent]:
    """`[section_logs_saved]`, or `[]` when there is nothing to log."""
    if not logs:
        return []
    return [WikiEvent(event_id=new_id("wev"), ts=now, body=SectionLogsSaved(logs=tuple(logs)))]


def log_versions(state: WikiState, page_id: str,
                 section_id: str | None = None) -> tuple[int | None, int | None]:
    """(spec_version, infer_version) of the section's latest log, else of the page's latest log,
    else (None, None) — what a signal records."""
    log = state.section_log(page_id, section_id)
    return (log.spec_version, log.infer_version) if log is not None else (None, None)


def partial_write(snap: Snapshot, record: PageRecord, sections: Iterable[str], *, kind: WriteKind,
                  ref: str, producer: Producer, now: datetime, new_id: Callable[[str], str],
                  model: str = "", cost_usd: float = 0.0, state: WikiState | None = None,
                  k: AspectK | None = None, run_id: str | None = None
                  ) -> tuple[PageRecord, list[WikiEvent]]:
    """A write that changed only `sections` (page order; those the page still has) of `record`:
    (the record with `written` = a WriteMark of `kind`, the `section_logs_saved` event to commit
    right after its `page_saved`). `ref` identifies the write (a feedback / proposal / card /
    practice / revision id); the mark's job_id is the trace run's id when one is current, else
    `{kind}:{ref}`. `state`: the state `record` belongs to (default the snapshot's); `k`: the K the
    write saw (default the snapshot's). Nothing changed → (record, [])."""
    page = record.page
    if page is None:
        return record, []
    want = set(sections)
    order = tuple(x.id for x in page.outline.sections if x.id in want)
    if not order:
        return record, []
    if run_id is None:
        from evowiki.store.artifacts import current_run_id
        run_id = current_run_id()
    st = state if state is not None else snap.state
    mark = WriteMark(job_id=run_id or f"{kind}:{ref}", at=now, model=model, sections=order,
                     spec_version=effective_version(st), cost_usd=round(cost_usd, 6), kind=kind)
    record = record.model_copy(update={"written": mark})
    return record, written_logs(snap, record, producer=producer, now=now, new_id=new_id,
                                state=st, k=k)


def written_logs(snap: Snapshot, record: PageRecord, *, producer: Producer, now: datetime,
                 new_id: Callable[[str], str], state: WikiState | None = None,
                 k: AspectK | None = None) -> list[WikiEvent]:
    """The `section_logs_saved` event for the sections of `record.written` (the flow that set the
    mark commits it right after the `page_saved`); `[]` with a warning when the page's teaching
    spec cannot be built (the write itself is not refused for it)."""
    page, mark = record.page, record.written
    if page is None or mark is None:
        return []
    st = state if state is not None else snap.state
    kk = k if k is not None else snap.k
    try:
        teach = page_teach(st, record.page_id)
        spec = page_spec(st, kk, record.page_id, user_id=snap.user_id, teach=teach)
        logs = section_logs(page.outline, mark.sections, producer=producer, spec=spec,
                            k=AspectKView(kk, now), teach=teach, at=now,
                            infer_version=snap.infer_version)
    except (SpecInvalid, KeyError):
        logging.getLogger(__name__).warning(
            "no section logs for %s/%s (%s write): spec unavailable", snap.user_id,
            record.page_id, mark.kind, exc_info=True)
        return []
    return logs_event(logs, new_id=new_id, now=now)
