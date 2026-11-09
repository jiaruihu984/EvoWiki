"""The learning-path step as a traced run (observability plan 2026-10-29 §5 1a): goals.path's
plan_path (goals/ never imports the LLM layer, so the run is opened here) with each draft it sent
back recorded as a rejection, naming the call that produced it."""

from collections.abc import Callable

from evowiki.goals.path import LearningPath
from evowiki.llm.run_context import traced_step
from evowiki.wikiapp.llm import GatewayJsonLLM

STEP = "path"


def traced_plan(user_id: str, subject: str, llm: GatewayJsonLLM,
                plan: Callable[[GatewayJsonLLM], LearningPath]) -> LearningPath:
    """`plan(llm)` (a plan_path call) in a run of step `path`: its stop is the path's source
    (llm / fallback / single / previous), each rejected draft a rejection with its attempt number
    and the call it came from (a draft that did not parse is the call's own error, not one)."""
    with traced_step(STEP, subject, user_id) as step:
        first = len(llm.calls)
        path = plan(llm)
        calls = llm.calls[first:]
        for i, vs in enumerate(path.rejected):
            call = calls[i] if i < len(calls) else None
            if call is not None:
                step.reject("；".join(v.message for v in vs), call, attempt=i + 1,
                            rules=[v.rule for v in vs])
        step.stop(path.source if calls else "unchanged")
        return path
