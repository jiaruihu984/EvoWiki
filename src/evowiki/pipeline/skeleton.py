import json
from collections.abc import Mapping

from pydantic import BaseModel, Field

from evowiki.content.models import Concept
from evowiki.llm.models import LLMRequest
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.linking import mentions
from evowiki.pipeline.report import LenientBatch, StageReport
from evowiki.pipeline.review import ReviewQueue, ReviewState, ReviewTarget

PURPOSE = "skeleton_expand"
PROMPT_VERSION = "skeleton.expand.v1"
DOMAINS = {"la": "Linear Algebra", "prob": "Probability", "ml": "Machine Learning",
           "dl": "Deep Learning"}
DOMAIN_TARGETS = {"la": 40, "prob": 30, "ml": 40, "dl": 40}
DOMAIN_ROOTS = {"la": "linear_algebra", "prob": "probability", "ml": "machine_learning",
                "dl": "deep_learning"}

SYSTEM = (
    "You build the concept inventory of a technical wiki from source passages. "
    "Propose only concepts that the given passages actually discuss, cite the passage ids "
    "that discuss each one, and never repeat an existing concept. Output JSON only."
)


class ProposedConcept(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name_en: str
    name_zh: str
    domain: str
    difficulty: int = Field(ge=1, le=5)
    aliases: list[str] = Field(default_factory=list)
    passage_ids: list[str] = Field(min_length=1)
    rationale: str


class ConceptExpansion(LenientBatch):
    concepts: list[ProposedConcept]


def _prompt(domain: str, existing: list[Concept], passages: list, quota: int) -> str:
    return (
        f"Domain: {DOMAINS[domain]} (code '{domain}').\n"
        f"Propose up to {quota} additional core concepts for this domain, ordered from most to "
        f"least central for a course leading to PCA, autoencoders and LoRA.\n"
        f"Existing concept ids (do not repeat): {json.dumps(sorted(c.id for c in existing))}\n"
        "Each concept: id (lower_snake_case), name_en, name_zh (standard Simplified Chinese "
        "term), domain, difficulty 1-5, aliases (English synonyms), passage_ids (passages "
        "below that discuss it; name or alias must appear in them), rationale (one sentence).\n"
        "Passages:\n" + "\n".join(f"[{p.id}] {p.text}" for p in passages)
    )


def _invalid(prop: ProposedConcept, domain: str, taken: set[str], allowed: set[str],
             ctx: StageContext) -> str | None:
    if prop.id in taken:
        return "duplicate id"
    if prop.domain != domain:
        return "wrong domain"
    if any(pid not in allowed for pid in prop.passage_ids):
        return "citation outside retrieved passages"
    probe = Concept(id=prop.id, name_en=prop.name_en, name_zh=prop.name_zh, domain=prop.domain,
                    difficulty=prop.difficulty, aliases=prop.aliases)
    if not any(mentions(ctx.passages[pid].text, probe) for pid in prop.passage_ids):
        return "name not found in cited passages"
    return None


def expand_domain(ctx: StageContext, domain: str,
                  targets: Mapping[str, int] = DOMAIN_TARGETS) -> StageReport:
    report = ctx.report("skeleton")
    existing = [c for c in ctx.concepts.values() if c.domain == domain]
    quota = targets[domain] - len(existing)
    if quota <= 0:
        report.dropped.append(f"{domain}: quota reached")
        return report
    passages = ctx.index.search([DOMAINS[domain], *(c.name_en for c in existing)],
                                k=max(20, ctx.retrieval_k))
    request = LLMRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=domain,
                         system=SYSTEM, prompt=_prompt(domain, list(ctx.concepts.values()),
                                                       passages, quota))
    out, call = ctx.gateway.call("generator", request, ConceptExpansion)
    report.llm_call_ids.append(call.id)
    report.dropped.extend(out.invalid_notes("skeleton"))
    taken = set(ctx.concepts)
    allowed = {p.id for p in passages}
    for prop in out.concepts:
        if len(report.flagged) >= quota:
            report.dropped.append(f"{prop.id}: over quota")
            continue
        reason = _invalid(prop, domain, taken, allowed, ctx)
        if reason is not None:
            report.dropped.append(f"{prop.id}: {reason}")
            continue
        taken.add(prop.id)
        ctx.queue.enqueue(ReviewTarget.CONCEPT, prop.id, "expansion_proposal",
                          payload=prop.model_dump(mode="json"))
        report.flagged.append(prop.id)
    return report


def approved_concepts(core: list[Concept], queue: ReviewQueue) -> list[Concept]:
    out = list(core)
    ids = {c.id for c in core}
    for item in queue.items(state=ReviewState.APPROVED, target=ReviewTarget.CONCEPT):
        if item.target_id in ids:
            continue
        p = ProposedConcept.model_validate(item.payload)
        out.append(Concept(id=p.id, name_en=p.name_en, name_zh=p.name_zh, domain=p.domain,
                           difficulty=p.difficulty, aliases=p.aliases))
        ids.add(p.id)
    return out
