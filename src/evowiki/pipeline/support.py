from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from evowiki.content.models import (
    CitedSentence,
    Lang,
    ReviewStatus,
    Segment,
    SegmentKind,
)
from evowiki.llm.models import LLMRequest
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.report import LenientBatch, StageReport
from evowiki.pipeline.review import ReviewTarget, review_fingerprint, spot_check_sample
from evowiki.pipeline.sources import license_obligations

SUPPORT_PURPOSE = "support_verify"
SUPPORT_PROMPT_VERSION = "support.verify.v1"

SUPPORT_SYSTEM = (
    "You are a strict fact checker for a technical wiki. For each numbered sentence decide "
    "whether its cited passages, and only those passages, fully support it. Paraphrase is "
    "fine; any claim not stated or directly implied by the cited text is unsupported. "
    "Output JSON only."
)


class DraftSentence(BaseModel):
    text: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)


class SentenceVerdict(BaseModel):
    index: int
    supported: bool
    reason: str = ""


class SupportVerdicts(LenientBatch):
    verdicts: list[SentenceVerdict]


def verify_support(ctx: StageContext, subject: str, sentences: list[CitedSentence],
                   notes: list[str] | None = None) -> tuple[list[bool], str | None]:
    """Per-sentence support flags (a missing verdict counts as unsupported); invalid
    verdict items are appended to `notes` when given."""
    if not sentences:
        return [], None
    blocks = []
    for i, s in enumerate(sentences):
        cited = "\n".join(f"  [{pid}] {ctx.passages[pid].text}" for pid in s.citations)
        blocks.append(f"Sentence {i}: {s.text}\n  Cited passages:\n{cited}")
    prompt = ("Judge each sentence. Return verdicts with index, supported, reason.\n\n"
              + "\n\n".join(blocks))
    request = LLMRequest(purpose=SUPPORT_PURPOSE, prompt_version=SUPPORT_PROMPT_VERSION,
                         subject=subject, system=SUPPORT_SYSTEM, prompt=prompt)
    out, call = ctx.gateway.call("verifier", request, SupportVerdicts)
    if notes is not None:
        notes.extend(out.invalid_notes("support"))
    by_index = {v.index: v.supported for v in out.verdicts}
    return [by_index.get(i, False) for i in range(len(sentences))], call.id


@dataclass
class VettedSentences:
    kept: list[CitedSentence]
    dropped: list[str]
    all_ok: bool
    llm_call_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # reported, but do not affect all_ok


def vet_sentences(ctx: StageContext, subject: str, drafts: list[DraftSentence],
                  allowed: set[str]) -> VettedSentences:
    candidates: list[tuple[int, CitedSentence]] = []
    dropped: list[str] = []
    for i, d in enumerate(drafts):
        if not d.citations or any(c not in allowed for c in d.citations):
            dropped.append(f"sentence {i}: citation outside retrieved passages")
            continue
        candidates.append((i, CitedSentence(text=d.text, citations=sorted(set(d.citations)))))
    notes: list[str] = []
    flags, call_id = verify_support(ctx, subject, [s for _, s in candidates], notes)
    kept = []
    for (i, s), ok in zip(candidates, flags, strict=True):
        if ok:
            kept.append(s)
        else:
            dropped.append(f"sentence {i}: unsupported")
    return VettedSentences(kept=kept, dropped=dropped, all_ok=not dropped,
                           llm_call_ids=[call_id] if call_id else [], notes=notes)


def segment_id(concept_id: str, kind: SegmentKind, lang: Lang = Lang.EN,
               slug: str | None = None) -> str:
    return ".".join([concept_id, kind.value, *([slug] if slug else []), lang.value])


def zh_id(en_id: str) -> str:
    if not en_id.endswith(".en"):
        raise ValueError(f"not an en segment id: {en_id}")
    return en_id[:-3] + ".zh"


def build_segment(ctx: StageContext, sid: str, concept_id: str, kind: SegmentKind,
                  vetted: VettedSentences, report: StageReport, *,
                  target: ReviewTarget = ReviewTarget.SEGMENT) -> Segment | None:
    report.llm_call_ids.extend(vetted.llm_call_ids)
    report.dropped.extend(f"{sid} {d}" for d in [*vetted.notes, *vetted.dropped])
    if not vetted.kept:
        report.dropped.append(f"{sid}: no supported sentences")
        return None
    status = ReviewStatus.AUTO_VERIFIED if vetted.all_ok else ReviewStatus.FLAGGED
    citations = {c for s in vetted.kept for c in s.citations}
    seg = Segment(id=sid, concept_id=concept_id, kind=kind, lang=Lang.EN, sentences=vetted.kept,
                  license_obligations=license_obligations(citations, ctx.passages, ctx.sources),
                  review_status=status)
    fp = review_fingerprint(seg)
    if not vetted.all_ok:
        ctx.queue.enqueue(target, sid, "unsupported_sentences",
                          payload={"dropped": vetted.dropped}, fingerprint=fp)
    resolved = ctx.queue.resolve(target, sid, status, fingerprint=fp)
    if resolved is None:
        report.dropped.append(f"{sid}: rejected by reviewer")
        return None
    (report.flagged if resolved is ReviewStatus.FLAGGED else report.created).append(sid)
    return seg.model_copy(update={"review_status": resolved})


def sample_spot_checks(ctx: StageContext, segments: list[Segment], *,
                       target: ReviewTarget = ReviewTarget.SEGMENT) -> None:
    auto = {s.id: s for s in segments if s.review_status is ReviewStatus.AUTO_VERIFIED}
    for sid in sorted(spot_check_sample(auto, ctx.spot_check_fraction)):
        ctx.queue.enqueue(target, sid, "spot_check", spot_check=True,
                          fingerprint=review_fingerprint(auto[sid]))
