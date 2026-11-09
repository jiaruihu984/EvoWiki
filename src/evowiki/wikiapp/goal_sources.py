"""A goal made from attached materials (materials plan task 16): the assistant's `goal_new`
executor, for a goal made from the blocks the learner confirmed in chat.

`create_goal_from_blocks_with_sources` checks every material before anything is written (unknown,
another user's or deleted: 404; still being read: 409; failed, or only split by pages: 422), then
makes the goal from the blocks and writes the blocks' node → section links (by "blocks") and one
use per material (`sources.uses.record_use`, phase 2's `apply_use` writes) INSIDE the goal's own
commit: the goal and its material records land together or not at all. The goal's own material
use (`material_use`) is stored on the map. A 读懂 goal is made by `wikiapp.reading` (reading-mode
spec §3.1, §6). No code writes `reads` links any more (old goals' rows stay readable). A refusal
is a `GoalSourceError` (status + learner-language detail); the executor answers it as a failed
row. `read_materials` runs the checks over any source reader (the assistant plans the blocks from
the tables of contents before any row).

A learning use of a material that has no full import yet (a chat attachment) starts that import
right AFTER the goal's commit (`start_next`). If it cannot start, the goal stays made: the
material shows the error end (MSG_NOT_STARTED) and the result's `source_note`
says so (the executor puts it in its result; the route logs it)."""

import dataclasses
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg_pool import ConnectionPool

from evowiki.goals.blocks import Block
from evowiki.goals.intake import GoalUse, SourceToc, SourceUse
from evowiki.sources import labels
from evowiki.sources import store as source_store
from evowiki.sources import uses as material_uses
from evowiki.sources.importer import ImportJob
from evowiki.sources.models import LinkRow, SectionRow, SourceRow, Use
from evowiki.sources.runs import SourceRuns
from evowiki.sources.use import guess_material_use, guess_text_use
from evowiki.wikiapp.goal import DraftResult, Ids, create_goal_from_blocks
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import Repo

MSG_SOURCE_NOT_FOUND = "找不到这份资料"
MSG_SOURCE_READING = "资料还在读，读完再开始"
MSG_SOURCE_FAILED = "这份资料没能读出来，换一份再试"
MSG_SOURCE_NO_TOC = "这份资料没认出目录，没法按它排路径"
MSG_UNDERSTAND_ONE = "读懂这篇一次只能附一份资料"
MSG_LINKS_NOT_SAVED = "资料出处没记上，目标没有建，请重试"

log = logging.getLogger(__name__)


class GoalSourceError(Exception):
    """A material that cannot be used; nothing was written. `status`: the HTTP status the route
    answers; `detail`: the learner-language reason (the same text for the chat's failed row)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass(frozen=True)
class Material:
    """One attached material: its row, its sections and its table of contents for intake."""

    row: SourceRow
    sections: list[SectionRow]
    toc: SourceToc


def load_materials(pool: ConnectionPool, user_id: str,
                   source_ids: Sequence[str]) -> list[Material]:
    """Each attached material (repeats dropped, order kept) with its table of contents, labels
    prefixed 「1·」… when there are several. GoalSourceError before any LLM call: unknown, another
    user's or deleted 404; still being read 409; failed, or only split by pages, 422."""
    if not source_ids:
        return []                                  # no material: no connection needed
    with pool.connection() as conn:
        return read_materials(lambda sid: source_store.get_source(conn, user_id, sid),
                              lambda sid: source_store.sections(conn, user_id, sid), source_ids)


def read_materials(get: Callable[[str], SourceRow | None],
                   sections: Callable[[str], list[SectionRow]],
                   source_ids: Sequence[str], *, before_layout: bool = False) -> list[Material]:
    """`load_materials` over any reader of the user's sources (the assistant's `SourceReader`
    when it plans a goal's blocks from materials before any row is written): the same checks
    and the same tables of contents, so the blocks see what the goal's executor will.
    `before_layout`: a look before `uses.await_layout`: a text-first material's page groups are
    not its last word yet."""
    ids = list(dict.fromkeys(source_ids))
    out = []
    for i, sid in enumerate(ids, 1):
        row = get(sid)
        if row is None or row.deleted_at is not None:
            raise GoalSourceError(404, MSG_SOURCE_NOT_FOUND)
        if row.status == "reading":
            raise GoalSourceError(409, MSG_SOURCE_READING)
        if row.status == "failed":
            raise GoalSourceError(422, MSG_SOURCE_FAILED)
        if row.toc_mode == "pages" and not (before_layout and row.text_first):
            raise GoalSourceError(422, MSG_SOURCE_NO_TOC)
        secs = sections(sid)
        toc = labels.source_toc(row, secs, prefix=f"{i}·" if len(ids) > 1 else "")
        out.append(Material(row, secs, toc))
    return out


@dataclasses.dataclass(frozen=True)
class MaterialUse:
    """How a goal uses its materials (spec 2026-10-28 §2.5, decision Q2): `use` (None = ask
    first), `default` (no stated intent: the learner is told in one sentence and can change it)."""
    use: GoalUse | None
    default: bool = False


def material_use(goal: str, use: GoalUse | None,
                 materials: Sequence[Material]) -> MaterialUse | None:
    """The decided table of §2.5: no material → None (a use only means something for
    materials). A stated intent wins and is never asked again: the caller's `use` (the assistant
    read the sentence; its value is checked by the schema), else the goal text's plain wording
    (`guess_text_use`). Only without either the defaults apply: goal text → reference; no text,
    one paper (`guess_material_use`) → ask (`use` None); no text otherwise → follow. 读懂 with
    several materials is the caller's refusal (MSG_UNDERSTAND_ONE)."""
    if not materials:
        return None
    if use is not None:
        return MaterialUse(use)
    stated = guess_text_use(goal)
    if stated is not None:
        return MaterialUse(stated)
    if goal.strip():
        return MaterialUse("reference", default=True)
    if len(materials) == 1 and guess_material_use(
            materials[0].row, materials[0].sections) == "understand":
        return MaterialUse(None)
    return MaterialUse("follow", default=True)


def recorded_use(how: SourceUse, m: Material) -> Use:
    """The use row written for one material (plan decision 2): an exam paper (guessed from the
    material) under a learning goal is recorded as `exam`, with the goal, so its questions are
    extracted; intake still outlined it as learn. Else `how`."""
    if how == "learn" and guess_material_use(m.row, m.sections) == "exam":
        return "exam"
    return how


def _goal_ref(res: DraftResult) -> str | None:
    """The goal a material use row points at: the map, else the path, else the draft."""
    d = res.draft
    return res.map_id or (d.map_id if d is not None else None) or res.path_id or (
        d.draft_id if d is not None else None)


def _record(user_id: str, materials: Sequence[Material], use: SourceUse, now: datetime,
            reruns: list[ImportJob]) -> Callable[[Any, DraftResult], None]:
    """The goal commit's material write: the result's links (`write_links`) and one use per material
    (`recorded_use`); the import
    runs those uses need are appended to `reruns` (started after the commit). A material deleted
    meanwhile is GoalSourceError(404), one with no sections 409, any other failure 500; each
    rolls the goal's commit back with it."""
    def write(conn: Any, res: DraftResult) -> None:
        try:
            _write(conn, res)
        except LookupError as exc:
            raise GoalSourceError(404, MSG_SOURCE_NOT_FOUND) from exc
        except material_uses.StillReading as exc:
            raise GoalSourceError(409, MSG_SOURCE_READING) from exc
        except Exception as exc:
            log.exception("goal source links for %s were not saved", user_id)
            raise GoalSourceError(500, MSG_LINKS_NOT_SAVED) from exc

    def _write(conn: Any, res: DraftResult) -> None:
        reruns.clear()                 # a retried commit writes (and collects) again
        write_links(conn, user_id, res.links, now)
        for m in materials:
            _added, rerun = material_uses.record_use(conn, user_id, m.row.source_id,
                                                     recorded_use(use, m), now,
                                                     goal_id=_goal_ref(res), page_id=res.page_id)
            if rerun is not None:
                reruns.append(rerun)
    return write


def write_links(conn: Any, user_id: str, links: Sequence[Any], now: datetime) -> None:
    """Node → section links (goals.intake.NodeSectionLink), replacing each writer's (`by`) links
    of those nodes; the caller owns the transaction (store.replace_links' errors propagate)."""
    for by in dict.fromkeys(x.by for x in links):
        mine = [x for x in links if x.by == by]
        source_store.replace_links(
            conn, user_id, node_ids=list(dict.fromkeys(x.node_id for x in mine)),
            links=[LinkRow(node_id=x.node_id, section_id=x.section_id, source_id=x.source_id,
                           role=x.role, by=by) for x in mine], by=by, now=now)


def trimmed_toc(toc: SourceToc, section_ids: set[str]) -> SourceToc | None:
    """`toc` with only the given sections and their subsections (the sections after one that are
    deeper than it), in reading order; None when none of them is in it."""
    keep: list[Any] = []
    depth: int | None = None
    for sec in toc.sections:
        if depth is not None and sec.depth > depth:
            keep.append(sec)
            continue
        depth = sec.depth if sec.id in section_ids else None
        if depth is not None:
            keep.append(sec)
    return toc.model_copy(update={"sections": tuple(keep)}) if keep else None


class PooledEnterSources:
    """`goal.EnterSources` over the database: the goal's materials (`load_materials`) and their
    `source_link` rows."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def block_tocs(self, user_id: str, source_ids: Sequence[str],
                   node_ids: Sequence[str]) -> list[SourceToc]:
        try:
            materials = load_materials(self._pool, user_id, source_ids)
        except GoalSourceError:           # a material deleted or unusable since: split without
            log.warning("materials of %s for entering are not usable", user_id)
            return []
        with self._pool.connection() as conn:
            links = source_store.links_for_nodes(conn, user_id, list(node_ids))
        mine = {m.row.source_id for m in materials}
        first = next((n for n in node_ids
                      if any(k.node_id == n and k.source_id in mine for k in links)), None)
        if first is None:
            return []
        wanted = {k.section_id for k in links if k.node_id == first}
        return [t for t in (trimmed_toc(m.toc, wanted) for m in materials) if t is not None]

    def write_links(self, conn: Any, user_id: str, links: Sequence[Any], now: datetime) -> None:
        write_links(conn, user_id, links, now)


def _start(pool: ConnectionPool, runs: SourceRuns, reruns: Sequence[ImportJob]) -> str | None:
    """Starts the imports the goal's uses need; MSG_NOT_STARTED when any could not start
    (even after the durable retries could not be scheduled; that material shows it), else None."""
    note = None
    for job in reruns:
        try:
            material_uses.start_next(pool, runs, job)
        except material_uses.RunNotStarted as exc:
            log.warning("goal of %s: the full import of %s did not start", job.user_id,
                        job.source_id)
            note = exc.detail
    return note


def create_goal_from_blocks_with_sources(repo: Repo, pool: ConnectionPool, gw: CountingGateway,
                                         user_id: str, goal: str,
                                         blocks: Sequence[Block], source_ids: Sequence[str] = (),
                                         *, runs: SourceRuns, title: str = "",
                                         words: Sequence[str] = (), limits: Sequence[str] = (),
                                         beginner: str = "",
                                         use: GoalUse | None = None, now: datetime,
                                         new_id: Ids,
                                         extra: Sequence[Callable[[Any], None]] = ()
                                         ) -> DraftResult:
    """`create_goal_from_blocks` with the materials the blocks were planned from (chat-first
    blocks, the assistant's confirmed `goal_new` draft): the material checks (`load_materials`)
    before anything is written, and one use per material in the goal's
    commit (always a learning goal — 读懂这篇 never gets blocks; an exam paper is still recorded
    as exam, `recorded_use`). `use`: how the goal uses its materials (reference / follow, decided
    when the blocks were planned; stored on the map); the blocks' section labels become
    `source_link` rows (by "blocks") in the same commit. The imports the uses need start after
    the commit (`source_note`: one could not start)."""
    materials = load_materials(pool, user_id, source_ids)
    reruns: list[ImportJob] = []
    res = create_goal_from_blocks(
        repo, gw, user_id, goal, blocks, now=now, new_id=new_id, title=title,
        words=words, limits=limits, beginner=beginner, sources=[m.toc for m in materials],
        material_use=(use or "follow") if materials else None, extra=extra,
        on_commit=_record(user_id, materials, "learn", now, reruns) if materials else None)
    note = _start(pool, runs, reruns)
    return dataclasses.replace(res, source_note=note) if note else res
