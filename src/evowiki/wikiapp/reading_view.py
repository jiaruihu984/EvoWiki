"""What the wire shows of a 读懂 goal's reading (reading-mode spec §5): computed at read time from
the replayed state, K and the material's section labels; nothing is stored. Pure, 0 LLM calls.

- A unit's `status` reads its page only (spec §2.3 读完 ≠ 掌握): 不读 (`skipped`), 读完 (its page has a
  body and the learner said 学完: `PageRecord.done`), 在读 (it has a live page), else 未开始 (a
  trashed page counts as none). K never decides it.
- `location`: the § labels of the sections its spans read (`sources.labels.section_labels`, each
  once) and the PDF pages they are on (「§3.2 p.4–5」); "" when the labels are not given.
- `progress`: units read / units to read, a skipped unit in neither.
- Points: K colour as a map node's, `known` = he has met it (every cell 熟悉 or better, itself or its marked block), the page that teaches
  it (the 先补 chip's target), whether it is on this goal's map's top level."""

from collections.abc import Mapping

from evowiki.belief.aspects import AspectKView
from evowiki.goals.contracts import Band, cell_id
from evowiki.goals.path import LEARNED_BANDS
from evowiki.goals.reading import Reading, ReadingUnit
from evowiki.goals.regions import GoalMap
from evowiki.sources.labels import pages_text
from evowiki.wikiapp.kwrite import concept_name
from evowiki.wikiapp.node_state import atomic_of
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import (
    ReadingOut,
    ReadingPointOut,
    ReadingProgressOut,
    ReadingUnitOut,
    ReadingUnitStatus,
)


def reading_path_id(reading_id: str) -> str:
    """The reading's one learning path (its pages in unit order)."""
    return f"path-{reading_id}"


def _teaching_page(state: WikiState, cid: str) -> str | None:
    """A live page that teaches `cid` (the first in page order), else None."""
    return next((pid for pid in state.page_order
                 if pid not in state.trashed_pages and (r := state.pages.get(pid)) is not None
                 and cid in r.targets), None)


def _met(kv: AspectKView, cid: str) -> bool:
    """He has met it: every cell at least 熟悉 (a 摸底 mark of 熟悉 reads shaky; 「你还没学」 would be false)."""
    bands = [kv.cell(cell_id(cid, a)).band for a in kv.aspects(cid)]
    return bool(bands) and all(b in LEARNED_BANDS or b is Band.SHAKY for b in bands)


def _under_met(kv: AspectKView, m: GoalMap | None, cid: str) -> bool:
    """A point of a block of this goal he marked at least 熟悉, with no evidence of its own against it."""
    node = None if m is None else m.node(cid)
    while m is not None and node is not None and node.parent is not None:
        node = m.node(node.parent)
        if node is not None and _met(kv, node.id):
            return not any(kv.cell(cell_id(cid, a)).band is Band.GAP for a in kv.aspects(cid))
    return False


def point_out(state: WikiState, kv: AspectKView, m: GoalMap | None, cid: str
              ) -> ReadingPointOut:
    """A teaches / uses point with its K colour (as a map node's), its page, whether it is on
    this goal's map's top level."""
    beliefs = [kv.cell(cell_id(cid, a)) for a in kv.aspects(cid)]
    counts = {b: 0 for b in Band}
    for x in beliefs:
        counts[x.band] += 1
    total = len(beliefs)
    top = {n.id for n in m.nodes if n.parent is None} if m is not None else set()
    return ReadingPointOut(
        id=cid, name=concept_name(state, cid), atomic=atomic_of(state, cid),
        known=_met(kv, cid) or _under_met(kv, m, cid),
        mix={b: c / total for b, c in counts.items()} if total else {},
        mastery=sum(x.m for x in beliefs) / total if total else None,
        page_id=_teaching_page(state, cid), on_map=cid in top)


def unit_status(state: WikiState, unit: ReadingUnit) -> ReadingUnitStatus:
    """From the unit's page only (module doc); never from K."""
    if unit.skipped:
        return "skipped"
    pid = unit.page_id
    rec = state.pages.get(pid) if pid is not None and pid not in state.trashed_pages else None
    if rec is None:
        return "not_started"
    return "done" if rec.has_body and rec.done else "reading"


def unit_location(unit: ReadingUnit, labels: Mapping[str, str]) -> str:
    """「§3.2 p.4–5」: its sections' labels (each once, 、-joined) and the pages its spans are on."""
    secs = list(dict.fromkeys(labels[s.section_id] for s in unit.spans
                              if s.section_id in labels))
    starts = [s.page_from for s in unit.spans if s.page_from is not None]
    ends = [s.page_to if s.page_to is not None else s.page_from for s in unit.spans
            if s.page_from is not None]
    pages = pages_text(min(starts), max(e for e in ends if e is not None)) if starts else ""
    return " ".join(x for x in ("、".join(secs), pages) if x)


def unit_out(state: WikiState, kv: AspectKView, reading: Reading, unit: ReadingUnit, *,
             labels: Mapping[str, str] = {}) -> ReadingUnitOut:
    m = state.maps.get(reading.map_id)
    return ReadingUnitOut(
        unit_id=unit.unit_id, order=unit.order, title=unit.title, about=unit.about,
        analyzed=unit.analyzed, skipped=unit.skipped, page_id=unit.page_id,
        status=unit_status(state, unit), location=unit_location(unit, labels),
        teaches=[point_out(state, kv, m, t) for t in unit.teaches],
        uses=[point_out(state, kv, m, u) for u in unit.uses], focus=list(unit.focus),
        uncertain=any(sp.uncertain for sp in unit.spans))


def reading_out(state: WikiState, kv: AspectKView, reading: Reading, *,
                labels: Mapping[str, str] = {}) -> ReadingOut:
    """The reading with its units in order and the progress (module doc). `labels`: section_id
    -> 「§3.2」 of the reading's material."""
    units = [unit_out(state, kv, reading, u, labels=labels)
             for u in sorted(reading.units, key=lambda u: u.order)]
    todo = [u for u in units if u.status != "skipped"]
    path_id = reading_path_id(reading.reading_id)
    return ReadingOut(
        reading_id=reading.reading_id, source_id=reading.source_id, title=reading.title,
        path_id=path_id if path_id in state.paths else None, units=units,
        progress=ReadingProgressOut(done=sum(u.status == "done" for u in todo), total=len(todo)))
