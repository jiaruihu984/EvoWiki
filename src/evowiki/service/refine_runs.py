"""Starting a rewrite (refine) by the page writer: the job row, then the page workflow run. Used by
POST /wiki/pages/{id}/refine, the RSI stale-page rewrite and the scoped write of a page's patched
sections (service.rsi_runs)."""

import logging
from typing import Literal

from evowiki.deep.contracts import RefineJob, RefineScope, RefineTrigger
from evowiki.service.deps import Services, page_deps
from evowiki.wikiapp.page_runs import PageJob, RefineArgs
from evowiki.wikiapp.refine_service import DeepService, JobRunning
from evowiki.wikiapp.repo import WikiConflict

log = logging.getLogger("evowiki.service.refine")


def start_refine(deep: DeepService, uid: str, page_id: str, trigger: RefineTrigger) -> RefineJob:
    """409 (WikiConflict) when one is already running or the page is not organized yet."""
    try:
        return deep.start(uid, page_id, trigger)
    except JobRunning as exc:
        raise WikiConflict(str(exc)) from exc
    except ValueError as exc:
        raise WikiConflict(str(exc)) from exc


def start_refine_run(svc: Services, uid: str, page_id: str, job: RefineJob, *,
                     kind: Literal["refine", "rsi_rewrite", "reorder_rewrite", "harness_rewrite",
                                   "patch_rewrite"],
                     spec_version: int | None = None, scope: RefineScope | None = None) -> str:
    """Starts the page workflow that runs the queued `job` (never attaches: one refine per page is
    held by the job store). `scope`: what to write (None: the whole page). Returns the run id."""
    try:
        run_id, _ = svc.runs.start(
            PageJob(kind=kind, user_id=uid, page_id=page_id, now=svc.clock(), origin="background",
                    refine=RefineArgs(job=job.model_dump(mode="json"), spec_version=spec_version,
                                      scope=scope.model_dump(mode="json") if scope else None)),
            page_deps(svc), attach=False)
    except Exception as exc:
        # the job was saved `queued` before the run: end it here, or it blocks refines of the page
        # until the running window passes
        log.exception("refine %s of %s/%s: the run did not start", job.job_id, uid, page_id)
        if svc.deep is not None:
            try:
                svc.deep.jobs.save(uid, job.model_copy(update={
                    "stage": "failed", "failure": "internal", "finished_at": svc.clock(),
                    "failure_detail": f"run not started: {type(exc).__name__}: {exc}"[:500]}))
            except Exception:
                log.exception("refine %s: could not mark the job failed", job.job_id)
        raise
    return run_id
