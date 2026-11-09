"""A section illustration drafted by the existing page writer and revision ledger."""

from dataclasses import replace
from datetime import datetime

from dbos import DBOS, SetWorkflowID
from pydantic import BaseModel, ConfigDict

from evowiki.agent import durable
from evowiki.bot.actions import ActionRefused
from evowiki.deep.writer import WriteFailed
from evowiki.goals.contracts import SectionRole
from evowiki.llm.budget import JobBudget
from evowiki.wikiapp.durable_repo import DurableRepo
from evowiki.wikiapp.page_flows import durable_writing
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.refine_apply import (
    checked_citations,
    make_assets,
    refine_input,
    written_record,
)
from evowiki.wikiapp.revise import RevisionDraft, RevisionSource


class VisualJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: str
    page_id: str
    section_id: str
    request: str
    now: datetime
    key: str


def validate_target(snap, page_id: str, section_id: str):
    rec = snap.state.pages.get(page_id)
    if rec is None or page_id in snap.state.trashed_pages:
        raise ActionRefused("没有这页，或这一页已经删除")
    if not rec.has_body or rec.page is None:
        raise ActionRefused("这一页还没写好，不能配图")
    sec = next((s for s in rec.page.outline.sections if s.id == section_id), None)
    if sec is None:
        raise ActionRefused("没有这一节，请先读页面确认段落")
    if sec.role is SectionRole.CHECK:
        raise ActionRefused("确认题不能配图，请选择讲解段落")
    if rec.page.state(section_id).pinned:
        raise ActionRefused("这一节已固定，请先取消固定再配图")
    return rec, sec


def compose_body(job: VisualJob, repo, writing, new_id) -> dict:
    drepo = DurableRepo(repo)
    snap = drepo.load(job.user_id)
    rec, sec = validate_target(snap, job.page_id, job.section_id)
    writing = durable_writing(writing, drepo)
    if writing is None:
        raise ActionRefused("配图的页面写作还没有接上")
    instruction = ("只给指定这一节配图，不改其他段落。使用 find_image/place_image 找合适的真实图片，"
                   "或用 draw_visual 画清楚原理；不要用字符、方框、竖线拼示意图。"
                   "保留这一节已有的讲解、公式和资源，在图前后写清要看什么。学习者的要求：" + job.request)
    inp = replace(refine_input(snap, job.page_id, job.now, writing.specs,
                               scope=(job.section_id,), instruction=instruction),
                  required=(job.section_id,))
    try:
        result = writing.writer.write(inp, budget=JobBudget(snap.state.refine.per_page_limit_usd))
    except WriteFailed as exc:
        raise ActionRefused(exc.detail or "配图没画成，请切换模型重试") from exc
    out = result.outcome
    draft = out.draft
    if {s.section_id for s in draft.sections} != {job.section_id}:
        raise ActionRefused("配图改到了其他段落，这次没有生成草稿")
    media = (*draft.images, *draft.blocks, *draft.interactives)
    if not media or any(m.section_id != job.section_id for m in media):
        raise ActionRefused("这一节没有生成可用的配图，请重试")
    if writing.assets is None:
        raise ActionRefused("配图资源库没有接上，这次没有生成草稿")
    # A visual request changes this section's body, never its concepts or teaching metadata.
    target = draft.sections[0].model_copy(update={
        "heading": sec.heading, "role": sec.role, "cells": sec.cells,
    })
    draft = draft.model_copy(update={"sections": (target,)})
    draft, issues = checked_citations(draft, inp, writing.sources)
    latest = repo.load(job.user_id).state.pages.get(job.page_id)
    if latest is None or page_fingerprint(latest) != page_fingerprint(rec):
        raise ActionRefused("这一页在配图时被改过了，请按现在的页面重试")
    def store_preview():
        stored = make_assets(draft, out.checks, user_id=job.user_id,
                             job_id=f"section-visual:{job.key}", now=job.now, new_id=new_id)
        writing.assets.put(stored)
        preview = written_record(rec, draft, stored, now=job.now,
                                 job_id=f"section-visual:{job.key}", model=result.model)
        return preview.page.texts[job.section_id].body
    body = durable.step("section-visual.preview", store_preview)
    revision = RevisionDraft(
        page_id=job.page_id, base_version=page_fingerprint(rec), draft=draft,
        scope=(job.section_id,), checks=tuple(out.checks.values()), cite_issues=issues,
        preview={job.section_id: body},
        sources=tuple(RevisionSource(source_id=s.source_id, title=s.title,
                                     page_offset=s.page_offset) for s in rec.sources))
    return revision.model_dump(mode="json")


@DBOS.workflow(name="evowiki.illustrate_section")
def visual_workflow(job: dict) -> dict:
    from evowiki.wikiapp.page_flows import installed
    deps = installed()
    try:
        return {"draft": compose_body(VisualJob.model_validate(job), deps.repo, deps.writing, deps.new_id)}
    except ActionRefused as exc:
        return {"refused": str(exc)}


def compose(job: VisualJob, *, repo, writing, on_dbos: bool, new_id) -> RevisionDraft:
    def run():
        if on_dbos:
            with SetWorkflowID(f"section-visual:{job.key}"):
                handle = DBOS.start_workflow(visual_workflow, job.model_dump(mode="json"))
            return handle.get_result()
        with durable.virtual_workflow(f"section-visual:{job.key}"):
            return {"draft": compose_body(job, repo, writing, new_id)}
    out = durable.outside_dbos(run)
    if "draft" not in out:
        raise ActionRefused(str(out.get("refused") or "配图没有生成，请重试"))
    return RevisionDraft.model_validate(out["draft"])
