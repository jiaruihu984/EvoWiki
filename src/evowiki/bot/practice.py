"""The real writer behind the assistant's `page_practice` action (spec §1.2 `add_practice`): ONE
practice for a written page — questions, memory cards or an interactive — composed by the page
writer in practice mode (`deep.agent.practice`: the write round's tools, render check and fence,
scoped to adding one interactive to one section; no outline, no text), on the same writers, page
job budget (「每页最多花」) and user meter as every refine mode (`deep.writer.PageWriter.practice`).

The block is composed when the action is PROPOSED (`compose`), so the learner sees the draft in the
page before deciding: the ledger row's `content["draft"]` is a `PracticeDraft` (the block or the
component, where it goes, its render check, the page version it was written against). The run is
its own durable workflow (`practice_workflow`, `evowiki.add_practice`: every model call and tool call a
step), started from the assistant's tool step under a fixed id (`practice:<the step's key>`, so a
replayed step gets the same draft) and waited for; without DBOS it runs inline in a virtual
workflow. A run that ends without a usable interactive is refused in plain words, no row written.
Confirming (`add`) makes no model call: the drafted block lands on the page in the SAME commit as
the ledger's effect (`extra_for` hands the caller the undo data before the commit).

An added practice is a local insertion, so a page changed since the draft does not stop it: it is
put at the end of its section as the page is NOW (re-anchored, `fits`). Only a section that is gone
or was renamed (a different section now) refuses it; then `redraft` composes it again against the
current page for the same request (the 重试 the card and the page bar offer).

The block goes into a section as an ```evoui fence (the form a note carries, `wikiapp.refine_apply._body`) and
is also stored as an `evoui` asset; a component is stored as an `html` asset when it is drafted (the page shows
the draft from it) and goes in as its embed fence on confirm. Undo takes the fence out again, but only while
the section still holds it exactly once (the page changed since -> refused)."""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from dbos import DBOS
from pydantic import BaseModel, ConfigDict

from evowiki.agent import durable
from evowiki.bot.actions import ActionRefused
from evowiki.deep.contracts import AssetMeta, RenderCheck, embed_block
from evowiki.deep.evoui import asset_fence, block_fence, block_json
from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.render import has_app
from evowiki.deep.store import AssetStore, StoredAsset, sha256
from evowiki.goals.contracts import SectionText
from evowiki.llm.run_context import traced_entry
from evowiki.wikiapp.events import PageRecord, PageSaved, WikiEvent
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.refine_apply import asset_src, image_check, stored_images
from evowiki.wikiapp.repo import Commit, Repo
from evowiki.wikiapp.sectionlog import partial_write

if TYPE_CHECKING:
    from evowiki.wikiapp.organize import Writing

log = logging.getLogger(__name__)

# What the learner reads when no usable block came back (copy rule: plain words, 请切换模型重试).
FAILED = "这个练习没做好，请切换模型重试"
UNAVAILABLE = "请切换模型重试"
NOT_WRITTEN = "这页还没写好，没法加练习"
NO_WRITER = "加练习的写作还没有接上"
# the run's failures that are the model's or the budget's, not the practice's
_UNAVAILABLE = frozenset({"llm_unavailable", "llm_busy", "llm_timeout", "budget_exceeded"})

ExtraFor = Callable[[dict[str, Any]], Sequence[Callable[[Any], None]]]


class PracticeDraft(BaseModel):
    """A composed, checked practice waiting for the learner (the `page_practice` row's
    `content["draft"]`). `base_version`: `page_fingerprint` of the page it was written for. A block
    (`block`) or a library component already stored as an asset (`component_id` + `asset`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_id: str
    base_version: str
    section_id: str
    # the section's heading when it was drafted (None: a draft from before re-anchoring): a
    # renamed section is another section, the draft does not fit it any more (`fits`)
    section_heading: str | None = None
    why: str
    block_id: str
    # the block's JSON text, not a dict: the row's content is jsonb, which reorders keys, and a block's
    # `derived` may only read earlier names (a block in jsonb came back invalid: live run 2026-11-04)
    block: str | None = None
    component_id: str | None = None
    asset: AssetMeta | None = None
    check: RenderCheck
    cost_usd: float = 0.0


def fits(rec: PageRecord | None, draft: PracticeDraft) -> bool:
    """The draft can go onto the page as it is now: its section is still there under the same
    heading (anything else on the page may have changed; the block is appended to the section)."""
    if rec is None or rec.page is None:
        return False
    sec = next((s for s in rec.page.outline.sections if s.id == draft.section_id), None)
    return sec is not None and (draft.section_heading is None or sec.heading == draft.section_heading)


def draft_fence(draft: PracticeDraft) -> str:
    """The draft as the page shows it before it is confirmed: a block inline (a block without an asset
    reports no results, so playing the draft records nothing); a component as its embed fence (its
    asset is stored when it was drafted)."""
    if draft.asset is not None:
        return embed_block(draft.asset, asset_src(draft.asset.asset_id))
    return block_fence(json.loads(draft.block or "{}"))


# ---- composing: the practice run ----------------------------------------------------------

class PracticeJob(BaseModel):
    """The practice workflow's only argument (JSON). `key`: the assistant tool step's key (the
    workflow id is `practice:<key>`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str
    page_id: str
    kind: Literal["question", "card", "interactive"]
    covers: str = ""
    now: datetime
    key: str


def workflow_id(job: PracticeJob) -> str:
    return f"practice:{job.key}"


@traced_entry("practice", subject=lambda a: a["job"].page_id,
              user=lambda a: a["job"].user_id)
def compose_body(job: PracticeJob, repo: Repo, writing: "Writing | None",
                 new_id: Callable[[str], str]) -> dict[str, Any]:
    """The practice run (the workflow's body; inline in a virtual workflow): {"draft": PracticeDraft
    JSON} or {"refused": the learner's line}. The page is read through the durable repo, the writer
    runs on the page's job budget, a drafted component's asset is stored in a step."""
    from evowiki.deep.agent import PracticeAsk
    from evowiki.deep.writer import WriteFailed
    from evowiki.llm.budget import JobBudget
    from evowiki.wikiapp.durable_repo import DurableRepo
    from evowiki.wikiapp.page_flows import durable_writing
    from evowiki.wikiapp.refine_apply import refine_input

    drepo = DurableRepo(repo)
    snap = drepo.load(job.user_id)
    rec = snap.state.pages.get(job.page_id)
    if rec is None or not rec.has_body:
        return {"refused": NOT_WRITTEN}
    w = durable_writing(writing, drepo)
    if w is None:
        return {"refused": NO_WRITER}
    inp = replace(refine_input(snap, job.page_id, job.now, w.specs, mode="practice"),
                  practice=PracticeAsk(kind=job.kind, covers=job.covers))
    try:
        res = w.writer.practice(inp, budget=JobBudget(snap.state.refine.per_page_limit_usd))
    except WriteFailed as exc:
        log.warning("practice %s/%s: %s: %s", job.user_id, job.page_id, exc.failure, exc.detail)
        return {"refused": UNAVAILABLE if exc.failure in _UNAVAILABLE else FAILED}
    out = res.outcome
    ids = durable.ids(new_id)
    block_id = f"practice-{ids('blk')}"
    assert rec.page is not None
    heading = next((s.heading for s in rec.page.outline.sections if s.id == out.section_id), None)
    base: dict[str, Any] = {"page_id": job.page_id, "base_version": page_fingerprint(rec),
                            "section_id": out.section_id, "section_heading": heading,
                            "why": out.why.strip(),
                            "block_id": block_id, "check": out.check, "cost_usd": res.cost_usd}
    if out.block is not None:
        content = block_json(out.block.block)
        if out.images:
            if w.assets is None:             # fail closed: the pictures could not be kept
                log.warning("practice %s/%s: no asset store for its images", job.user_id, job.page_id)
                return {"refused": NO_WRITER}
            pictures = [StoredAsset(
                user_id=job.user_id, job_id=f"practice:{block_id}", created_at=job.now, html=m.data_b64,
                meta=AssetMeta(asset_id=ids("ia"), page_id=job.page_id, section_id=out.section_id,
                               interactive_id=m.image_id, title=m.title[:200], height=400,
                               sha256=sha256(m.data_b64), check=image_check(m), kind="image"))
                for m in out.images]
            keeper = w.assets
            durable.step("practice.images", lambda: keeper.put(pictures) or True)
            content = block_json(stored_images(out.block.block, {a.meta.interactive_id: a.meta.asset_id
                                                                 for a in pictures}))
        if w.assets is not None and has_app(out.block.block):
            # The page shows the draft inline and the plugin runs an `app` only when the server stored its content.
            meta = AssetMeta(asset_id=ids("ia"), page_id=job.page_id, section_id=out.section_id,
                             interactive_id=block_id, title=out.block.block.title[:120], height=400,
                             sha256=sha256(content), check=out.check, kind="evoui")
            drawn = StoredAsset(user_id=job.user_id, meta=meta, html=content, job_id=f"practice:{block_id}",
                                created_at=job.now)
            assets_ = w.assets
            durable.step("practice.asset", lambda: assets_.put([drawn]) or True)
        draft = PracticeDraft(**base, block=content)
        return {"draft": draft.model_dump(mode="json")}
    d = out.interactive
    assert d is not None and out.component_id is not None
    if w.assets is None:                 # fail closed: the draft could not be shown
        log.warning("practice %s/%s: no asset store for the component", job.user_id, job.page_id)
        return {"refused": NO_WRITER}
    meta = AssetMeta(asset_id=ids("ia"), page_id=job.page_id, section_id=out.section_id,
                     interactive_id=out.component_id, title=d.title[:120], height=d.height,
                     sha256=sha256(d.html), check=out.check, kind="html")
    assets = w.assets
    asset = StoredAsset(user_id=job.user_id, meta=meta, html=d.html, job_id=f"practice:{block_id}",
                        created_at=job.now)
    durable.step("practice.asset", lambda: assets.put([asset]) or True)
    draft = PracticeDraft(**base, component_id=out.component_id, asset=meta)
    return {"draft": draft.model_dump(mode="json")}


@DBOS.workflow(name="evowiki.add_practice")
def practice_workflow(job: dict) -> dict:
    from evowiki.wikiapp.page_flows import installed
    deps = installed()
    return compose_body(PracticeJob.model_validate(job), deps.repo, deps.writing, deps.new_id)


def run_practice(job: PracticeJob, *, on_dbos: bool, repo: Repo, writing: "Writing | None",
                 new_id: Callable[[str], str]) -> dict[str, Any]:
    """Runs the practice workflow and waits for it: on DBOS (`on_dbos`, the service) as the root
    workflow `practice:<key>`, started off the caller's step (`durable.outside_dbos`; an existing
    id returns that workflow's result) over the page workflows' installed deps; else inline in a
    virtual workflow over the deps given here."""
    wid = workflow_id(job)
    if on_dbos:
        from dbos import SetWorkflowID

        def go() -> dict[str, Any]:
            with SetWorkflowID(wid):
                handle = DBOS.start_workflow(practice_workflow, job.model_dump(mode="json"))
            return handle.get_result()
        return durable.outside_dbos(go)

    def inline() -> dict[str, Any]:
        with durable.virtual_workflow(wid):
            return compose_body(job, repo, writing, new_id)
    return durable.outside_dbos(inline)


class DeepPracticeWriter:
    """`bot.executors_assistant.PracticeWriter` over the page store and the deep tier's asset store,
    and the composer of `add_practice` (`compose`: the practice run)."""

    def __init__(self, repo: Repo, writing: "Callable[[], Writing | None]", assets: AssetStore,
                 clock: Callable[[], datetime], new_id: Callable[[str], str], *,
                 on_dbos: bool = False):
        """`writing()`: the page writer kit an inline run uses (read at call time; a DBOS run uses
        the page workflows' installed deps). `on_dbos`: runs are DBOS workflows (the service)."""
        self._repo, self._writing, self._assets = repo, writing, assets
        self._clock, self._new_id, self._on_dbos = clock, new_id, on_dbos

    def compose(self, user_id: str, page_id: str, kind: str, covers: str,
                key: str) -> PracticeDraft:
        """A render-checked practice for the page (nothing written; a component's asset stored),
        from the practice run keyed by the assistant's step `key`. ActionRefused in plain words
        when the page is not written or no usable interactive came back (the reasons go to the
        log)."""
        rec = self._repo.load(user_id).state.pages.get(page_id)
        if rec is None or not rec.has_body:
            raise ActionRefused(NOT_WRITTEN)
        job = PracticeJob(user_id=user_id, page_id=page_id, kind=kind,  # type: ignore[arg-type]
                          covers=covers, now=self._clock(), key=key)
        out = run_practice(job, on_dbos=self._on_dbos, repo=self._repo, writing=self._writing(),
                           new_id=self._new_id)
        if "draft" not in out:
            raise ActionRefused(str(out.get("refused") or FAILED))
        return PracticeDraft.model_validate(out["draft"])

    def add(self, user_id: str, draft: PracticeDraft, expect_version: str | None,
            extra_for: ExtraFor) -> dict[str, Any]:
        """The drafted block at the end of its section as the page is now (no model call; the page
        may have changed elsewhere since the draft, `fits`), stored as an asset, in one commit with
        `extra_for(undo)`; ActionRefused(stale) when its section is gone or renamed.
        `expect_version` is not checked: the insertion does not depend on the rest of the page."""
        snap = self._repo.load(user_id)
        rec = snap.state.pages.get(draft.page_id)
        if rec is None or not rec.has_body:
            raise ActionRefused("这页还没写好，没法加练习")
        if not fits(rec, draft):
            raise ActionRefused("", stale=True)
        if not draft.check.ok:
            raise ActionRefused("这份练习草稿已经用不了了，没有保存")
        if draft.asset is not None:          # a library component, stored when it was drafted
            fence = draft_fence(draft)
            undo = {"section_id": draft.section_id, "fence": fence, "asset_id": draft.asset.asset_id}
            text = rec.page.texts.get(draft.section_id)
            body = (text.body if text is not None else "").rstrip() + "\n\n" + fence
            self._commit(snap, _with_body(rec, draft.section_id, body, "inserted"), "practice",
                         [*extra_for(undo)], written=(draft.section_id, draft.block_id))
            return {"undo": undo, "block_id": draft.block_id}
        block = EvoUIBlock.model_validate_json(draft.block or "{}")
        content = block_json(block)
        asset = StoredAsset(
            user_id=user_id, created_at=self._clock(), job_id=f"practice:{draft.block_id}",
            html=content,
            meta=AssetMeta(asset_id=self._new_id("ia"), page_id=draft.page_id,
                           section_id=draft.section_id, interactive_id=draft.block_id,
                           title=block.title, height=400, sha256=sha256(content), check=draft.check,
                           kind="evoui"))
        fence = asset_fence(asset.meta.asset_id)
        undo = {"section_id": draft.section_id, "fence": fence, "asset_id": asset.meta.asset_id}
        text = rec.page.texts.get(draft.section_id)
        body = (text.body if text is not None else "").rstrip() + "\n\n" + fence
        new = _with_body(rec, draft.section_id, body, "inserted")
        self._commit(snap, new, "practice", [self._assets.writer([asset]), *extra_for(undo)],
                     written=(draft.section_id, draft.block_id))
        return {"undo": undo, "block_id": draft.block_id}

    def remove(self, user_id: str, page_id: str, undo: dict[str, Any],
               extra: Sequence[Callable[[Any], None]]) -> None:
        snap = self._repo.load(user_id)
        rec = snap.state.pages.get(page_id)
        text = rec.page.texts.get(undo["section_id"]) if rec and rec.page else None
        if text is None or text.body.count(undo["fence"]) != 1:
            raise ActionRefused("这一段在加练习之后又改过了，不能撤销，避免覆盖新的内容")
        body = text.body.replace("\n\n" + undo["fence"], "").replace(undo["fence"], "").rstrip()
        self._commit(snap, _with_body(rec, undo["section_id"], body, "rewritten"),
                     "practice_undone", list(extra))

    def _commit(self, snap, rec: PageRecord, action: str,
                extra: list[Callable[[Any], None]], *,
                written: tuple[str, str] | None = None) -> None:
        """`written`: (section id, block id) of a practice block added by this commit: the page
        record gets a practice WriteMark and the section a SectionLog; undo passes None."""
        now = self._clock()
        logs: list[WikiEvent] = []
        if written is not None:
            rec, logs = partial_write(snap, rec, [written[0]], kind="practice", ref=written[1],
                                      producer="edit", now=now, new_id=self._new_id)
        ev = WikiEvent(event_id=self._new_id("wev"), ts=now,
                       body=PageSaved(record=rec, action=action))
        self._repo.commit(snap, Commit(wiki=[ev, *logs], extra=extra))


def _with_body(rec: PageRecord, section_id: str, body: str, change: str) -> PageRecord:
    page = rec.page
    old = page.texts.get(section_id)
    text = (old.model_copy(update={"body": body, "note_body": None}) if old is not None else
            SectionText(section_id=section_id, body=body, cache_key=f"practice:{section_id}"))
    new_page = page.model_copy(update={"texts": {**page.texts, section_id: text}})
    return rec.model_copy(update={"page": new_page, "changed": {**rec.changed, section_id: change}})
