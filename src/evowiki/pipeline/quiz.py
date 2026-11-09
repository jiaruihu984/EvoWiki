import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field

from evowiki.content.models import Concept, Connection, QuizItem, ReviewStatus
from evowiki.llm.models import LLMRequest
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.report import LenientBatch, StageReport
from evowiki.pipeline.review import (
    ReviewQueue,
    ReviewState,
    ReviewTarget,
    review_fingerprint,
    spot_check_sample,
)

PURPOSE = "quiz_generate"
PROMPT_VERSION = "quiz.generate.v3"
ANSWER_PURPOSE = "quiz_answer_verify"
ANSWER_PROMPT_VERSION = "quiz.answer_verify.v1"
CONCEPT_MAX_ITEMS = 4
CONNECTION_MAX_ITEMS = 2   # exactly one 'seen' and one 'near' transfer item per connection

SYSTEM = (
    "You write short quiz items for a technical wiki, grounded in the given passages. Each "
    "item has a stem, a concise answer_key, kind (recall|application|transfer), difficulty "
    "1-5, kc_tags (knowledge components as '<concept_id>/<slug>'), misconception_tags "
    "('<concept_id>/m_<slug>') for wrong answers the item diagnoses, and citations "
    "(passage ids). For relationship (transfer) items also give transfer_kind: 'seen' asks "
    "about the relationship as the passages state it; 'near' applies the same relationship "
    "to a new but closely related case that the passages do not spell out. Output JSON only."
)
ANSWER_SYSTEM = (
    "You independently answer a quiz item using only the cited passages, then judge whether "
    "the provided answer key is correct. Output JSON only."
)


class DraftQuizItem(BaseModel):
    stem: str = Field(min_length=1)
    answer_key: str = Field(min_length=1)
    kind: Literal["recall", "application", "transfer"]
    difficulty: int = Field(ge=1, le=5)
    kc_tags: list[str] = Field(default_factory=list)
    misconception_tags: list[str] = Field(default_factory=list)
    citations: list[str] = Field(min_length=1)
    transfer_kind: Literal["seen", "near"] | None = None


class QuizDraftBatch(LenientBatch):
    items: list[DraftQuizItem]


class AnswerCheck(BaseModel):
    answer: str
    key_is_correct: bool
    reason: str = ""


def quiz_item_id(stem: str) -> str:
    return "q_" + hashlib.sha256(stem.encode()).hexdigest()[:10]


def normalize_tag(tag: str, default_cid: str, concept_ids: list[str], *,
                  misconception: bool) -> str | None:
    cid, sep, slug = tag.partition("/")
    if not sep:
        cid, slug = default_cid, tag
    if cid not in concept_ids:
        return None
    slug = re.sub(r"[^a-z0-9_]+", "_", slug.lower()).strip("_")
    if not slug:
        return None
    if misconception and not slug.startswith("m_"):
        slug = "m_" + slug
    return f"{cid}/{slug}"


# Plan 4b-1d H1b: a human review of an item's correctness / ambiguity / source support. An
# approved one counts as human verification of the answer key (like core_answer_key).
H1B_REASON = "h1b_review"
HUMAN_VERIFY_REASONS = ("core_answer_key", H1B_REASON)


def apply_quiz_review(queue: ReviewQueue, item: QuizItem) -> QuizItem | None:
    fp = review_fingerprint(item)
    resolved = queue.resolve(ReviewTarget.QUIZ_ANSWER, item.id, item.review_status,
                             fingerprint=fp)
    if resolved is None:
        return None
    verified = item.answer_verified_by_human or any(
        i.state is ReviewState.APPROVED and i.target_id == item.id
        and i.reason in HUMAN_VERIFY_REASONS and i.fingerprint in (None, fp)
        for i in queue.items(target=ReviewTarget.QUIZ_ANSWER))
    return item.model_copy(update={"review_status": resolved,
                                   "answer_verified_by_human": verified})


def _prompt(ctx: StageContext, concept_ids: list[str], connection: Connection | None,
            passages: list, max_items: int) -> str:
    if connection is not None:
        ask = ("Write exactly two quiz items, one 'seen' and one 'near' transfer item, about the "
               f"relationship along path {' -> '.join(connection.path)}.")
    else:
        cid = concept_ids[0]
        ask = f"Write up to {max_items} quiz items about concept {cid} ({ctx.concepts[cid].name_en})."
    return (f"{ask} Tag concept ids from: {concept_ids}.\nPassages:\n"
            + "\n".join(f"[{p.id}] {p.text}" for p in passages))


def _check_answer(ctx: StageContext, qid: str, d: DraftQuizItem) -> tuple[AnswerCheck, str]:
    cited = "\n".join(f"[{pid}] {ctx.passages[pid].text}" for pid in d.citations)
    request = LLMRequest(purpose=ANSWER_PURPOSE, prompt_version=ANSWER_PROMPT_VERSION,
                         subject=qid, system=ANSWER_SYSTEM,
                         prompt=f"Question: {d.stem}\nProposed answer key: {d.answer_key}\n"
                                f"Cited passages:\n{cited}")
    out, call = ctx.gateway.call("verifier", request, AnswerCheck)
    return out, call.id


def generate_quiz(ctx: StageContext, concept: Concept, *, core: bool,
                  connection: Connection | None = None,
                  max_items: int | None = None) -> tuple[list[QuizItem], StageReport]:
    """Quiz items for a concept (up to CONCEPT_MAX_ITEMS by default) or, with `connection`,
    exactly CONNECTION_MAX_ITEMS transfer items (one 'seen', one 'near')."""
    if connection is not None and max_items not in (None, CONNECTION_MAX_ITEMS):
        raise ValueError(f"connection quizzes have exactly {CONNECTION_MAX_ITEMS} items")
    if max_items is None:
        max_items = CONNECTION_MAX_ITEMS if connection is not None else CONCEPT_MAX_ITEMS
    report = ctx.report("quiz")
    concept_ids = list(connection.path) if connection else [concept.id]
    subject = connection.segment_id if connection else concept.id
    passages = ctx.index.for_concepts([ctx.concepts[c] for c in concept_ids],
                                      k=ctx.retrieval_k)
    allowed = {p.id for p in passages}
    request = LLMRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=subject,
                         system=SYSTEM,
                         prompt=_prompt(ctx, concept_ids, connection, passages, max_items))
    out, call = ctx.gateway.call("generator", request, QuizDraftBatch)
    report.llm_call_ids.append(call.id)
    report.dropped.extend(out.invalid_notes("quiz"))
    items: list[QuizItem] = []
    for d in out.items[:max_items]:
        qid = quiz_item_id(d.stem)
        if any(c not in allowed for c in d.citations):
            report.dropped.append(f"{qid}: citation outside retrieved passages")
            continue
        if connection is not None and d.transfer_kind is None:
            report.dropped.append(f"{qid}: connection item without transfer_kind")
            continue
        tags: dict[str, list[str]] = {"kc": [], "mis": []}
        for field_name, raw_tags, mis in (("kc", d.kc_tags, False),
                                          ("mis", d.misconception_tags, True)):
            for t in raw_tags:
                norm = normalize_tag(t, concept.id, concept_ids, misconception=mis)
                if norm is None:
                    report.dropped.append(f"{qid}: dropped tag {t}")
                elif norm not in tags[field_name]:
                    tags[field_name].append(norm)
        check, check_id = _check_answer(ctx, qid, d)
        report.llm_call_ids.append(check_id)
        status = ReviewStatus.AUTO_VERIFIED if check.key_is_correct else ReviewStatus.FLAGGED
        item = QuizItem(
            id=qid, concept_ids=concept_ids, kc_tags=tags["kc"], misconception_tags=tags["mis"],
            difficulty=d.difficulty, kind="transfer" if connection else d.kind,
            answer_verified_by_human=False, stem=d.stem, answer_key=d.answer_key, core=core,
            tests_connection_id=connection.segment_id if connection else None,
            transfer_kind=d.transfer_kind if connection else None,
            citations=sorted(set(d.citations)), review_status=status)
        fp = review_fingerprint(item)
        if not check.key_is_correct:
            ctx.queue.enqueue(ReviewTarget.QUIZ_ANSWER, qid, "verifier_mismatch",
                              payload={"verifier_answer": check.answer, "reason": check.reason},
                              fingerprint=fp)
        if core:
            ctx.queue.enqueue(ReviewTarget.QUIZ_ANSWER, qid, "core_answer_key",
                              payload={"stem": d.stem, "answer_key": d.answer_key},
                              fingerprint=fp)
        resolved = apply_quiz_review(ctx.queue, item)
        if resolved is None:
            report.dropped.append(f"{qid}: rejected by reviewer")
            continue
        items.append(resolved)
        (report.flagged if resolved.review_status is ReviewStatus.FLAGGED
         else report.created).append(qid)
    auto = {q.id: q for q in items
            if not q.core and q.review_status is ReviewStatus.AUTO_VERIFIED}
    for qid in sorted(spot_check_sample(auto, ctx.spot_check_fraction)):
        ctx.queue.enqueue(ReviewTarget.QUIZ_ANSWER, qid, "spot_check", spot_check=True,
                          fingerprint=review_fingerprint(auto[qid]))
    return items, report
