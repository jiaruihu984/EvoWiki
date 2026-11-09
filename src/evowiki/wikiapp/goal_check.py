"""The goal footer's confirmation questions use ordinary page CHECK sections and grading."""

from datetime import datetime

from evowiki.goals.contracts import OutlineSection, SectionRole, split_cell
from evowiki.wikiapp.followup import node_confirm_item
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.llm import LLMFailed, require
from evowiki.wikiapp.page import mark_done
from evowiki.wikiapp.progress import Ctx, goal_of_page, map_paths, page_progress, path_rows
from evowiki.wikiapp.repo import Repo, Snapshot, WikiConflict
from evowiki.wikiapp.wiki_schemas import DoneOut

PREFIX = "goal_check_"


def goal_cells(snap: Snapshot, page_id: str, *, now: datetime) -> tuple[str, ...]:
    """Sample across the goal's taught concepts before taking a second aspect of one concept."""
    state = snap.state
    gid = goal_of_page(state, page_id)
    paths = map_paths(state)
    ctx = Ctx(state, snap.kview(now), {})
    rows = [r for pid, path in state.paths.items() if paths.get(pid, pid) == gid
            for r in path_rows(ctx, path) if not r.item.skipped]
    pages = [(r.page_id, r.item.page.targets) for r in rows if r.page_id is not None]
    if gid in state.maps:
        pages.extend((n.page_id, state.pages[n.page_id].targets) for n in state.maps[gid].nodes
                     if n.page_id is not None and n.page_id in state.pages)
    cells: dict[str, list[str]] = {}
    for pid, targets in dict.fromkeys(pages):
        rec = state.pages.get(pid)
        if rec is None or rec.page is None or pid in state.trashed_pages:
            continue
        for section in rec.page.outline.sections:
            if section.id.startswith(PREFIX):
                continue
            for cell in section.cells:
                cid, aspect = split_cell(cell)
                if cid not in targets or cid not in snap.k.concept_ids() or aspect not in snap.k.aspects(cid):
                    continue
                bucket = cells.setdefault(cid, [])
                if cell not in bucket:
                    bucket.append(cell)
    candidates = [bucket[i] for i in range(max(map(len, cells.values()), default=0))
                  for bucket in cells.values() if i < len(bucket)]
    # Three distinct concepts when possible, including the beginning and end of a large goal.
    if len(cells) > 3:
        first = [bucket[0] for bucket in cells.values()]
        return first[0], first[len(first) // 2], first[-1]
    return tuple(candidates[:3])


def complete_page(repo: Repo, gateway, user_id: str, page_id: str, *, now: datetime,
                  new_id: Ids) -> DoneOut:
    """Generate outside any write transaction; commit the questions and read mark together."""
    snap = repo.load(user_id)
    rec = snap.state.pages.get(page_id)
    if rec is None:
        raise LookupError(f"没有这一页：{page_id}")
    if not rec.has_body or page_id in snap.state.trashed_pages:
        raise WikiConflict("这一页还没写好或已删除，不能学完")
    terminal = page_progress(snap.state, snap.kview(now), {}, page_id).after.kind == "goal_done"
    existing = any(s.id.startswith(PREFIX) for s in rec.page.outline.sections)
    checks = ()
    if terminal and not existing:
        cells = goal_cells(snap, page_id, now=now)
        if not cells:
            raise LLMFailed("这个目标还没有可出题的知识点，请先完成页面生成")
        gw = require(gateway, user_id)
        checks = tuple(OutlineSection(
            id=f"{PREFIX}{i}", heading="Goal check" if snap.state.language == "en" else "目标小测",
            role=SectionRole.CHECK, cells=(cell,),
            check=node_confirm_item(gw, snap, split_cell(cell)[0], cell, now=now))
            for i, cell in enumerate(cells, 1))
    return mark_done(repo, user_id, page_id, now=now, new_id=new_id, checks=checks,
                     expected_page=rec)
