import re
from collections.abc import Iterable

from pydantic import BaseModel

from evowiki.content.models import CitedSentence, Concept, Lang, ReviewStatus, Segment
from evowiki.llm.models import LLMRequest
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.linking import concept_names
from evowiki.pipeline.report import LenientBatch, StageReport
from evowiki.pipeline.review import ReviewTarget, review_fingerprint, spot_check_sample
from evowiki.pipeline.support import zh_id

TRANSLATE_PURPOSE = "translate_zh"
TRANSLATE_PROMPT_VERSION = "translate.zh.v1"
BACK_PURPOSE = "back_translate_en"
BACK_PROMPT_VERSION = "backtranslate.en.v1"
BACK_THRESHOLD = 0.4

TRANSLATE_SYSTEM = (
    "You translate technical wiki sentences from English into Simplified Chinese, one output "
    "sentence per input sentence, keeping the index. For every glossary term present, use the "
    "glossary Chinese term followed by the original English term in full-width parentheses, "
    "e.g. 主成分分析（PCA）. Do not add or drop content. Output JSON only."
)
BACK_SYSTEM = (
    "You translate Simplified Chinese technical sentences back into English literally, one "
    "output sentence per input sentence, keeping the index. Output JSON only."
)
_STOP = {"a", "an", "the", "of", "to", "in", "on", "and", "or", "is", "are", "be", "by",
         "for", "with", "as", "it", "its", "that", "this", "can", "from", "into", "at"}


class GlossaryEntry(BaseModel):
    term_en: str
    term_zh: str


def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(term) + r"(?:s|es)?(?![\w-])", re.IGNORECASE)


class Glossary:
    def __init__(self, entries: Iterable[GlossaryEntry]):
        self.entries: list[GlossaryEntry] = []
        seen: set[str] = set()
        for e in entries:
            if e.term_en.lower() not in seen:
                seen.add(e.term_en.lower())
                self.entries.append(e)

    @classmethod
    def from_concepts(cls, concepts: Iterable[Concept],
                      extra: Iterable[GlossaryEntry] = ()) -> "Glossary":
        derived = [GlossaryEntry(term_en=n, term_zh=c.name_zh)
                   for c in concepts for n in concept_names(c)]
        return cls([*extra, *derived])

    def matches(self, text_en: str) -> list[tuple[str, GlossaryEntry]]:
        """(surface form as written in text_en, entry) pairs in text order."""
        taken: list[tuple[int, int]] = []
        hits: list[tuple[int, str, GlossaryEntry]] = []
        for e in sorted(self.entries, key=lambda e: (-len(e.term_en), e.term_en)):
            for m in _pattern(e.term_en).finditer(text_en):
                if any(m.start() < end and start < m.end() for start, end in taken):
                    continue
                taken.append(m.span())
                hits.append((m.start(), m.group(0), e))
                break
        return [(surface, e) for _, surface, e in sorted(hits, key=lambda t: t[0])]

    def terms_in(self, text_en: str) -> list[GlossaryEntry]:
        return [e for _, e in self.matches(text_en)]


def glossary_violations(en: str, zh: str, glossary: Glossary) -> list[str]:
    out = []
    for surface, e in glossary.matches(en):
        if e.term_zh not in zh:
            out.append(f"missing zh term {e.term_zh!r} for {surface!r}")
        if e.term_en.lower() not in zh.lower():
            out.append(f"missing original English term {surface!r}")
    return out


def _content_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP}


def content_overlap(a: str, b: str) -> float:
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


class TranslatedSentence(BaseModel):
    index: int
    text: str


class SentenceTranslations(LenientBatch):
    sentences: list[TranslatedSentence]


def _numbered(texts: list[str]) -> str:
    return "\n".join(f"{i}: {t}" for i, t in enumerate(texts))


def translate_segment(ctx: StageContext, seg: Segment,
                      glossary: Glossary) -> tuple[Segment | None, StageReport]:
    report = ctx.report("translate")
    if seg.lang is not Lang.EN:
        raise ValueError(f"only en segments are translated: {seg.id}")
    sid = zh_id(seg.id)
    en_texts = [s.text for s in seg.sentences]
    terms = {e.term_en: e.term_zh for t in en_texts for e in glossary.terms_in(t)}
    request = LLMRequest(
        purpose=TRANSLATE_PURPOSE, prompt_version=TRANSLATE_PROMPT_VERSION, subject=seg.id,
        system=TRANSLATE_SYSTEM,
        prompt=f"Glossary: {terms}\nSentences:\n{_numbered(en_texts)}")
    out, call = ctx.gateway.call("generator", request, SentenceTranslations)
    report.llm_call_ids.append(call.id)
    report.dropped.extend(f"{sid} {n}" for n in out.invalid_notes("translate"))
    zh_by_index = {s.index: s.text for s in out.sentences}
    for i in range(len(en_texts)):
        if not zh_by_index.get(i, "").strip():
            report.dropped.append(f"{sid}: translation missing sentence {i}")
            return None, report
    zh_texts = [zh_by_index[i] for i in range(len(en_texts))]

    pairs = list(zip(en_texts, zh_texts, strict=True))
    issues = [f"sentence {i}: {v}" for i, (en, zh) in enumerate(pairs)
              for v in glossary_violations(en, zh, glossary)]
    back_req = LLMRequest(purpose=BACK_PURPOSE, prompt_version=BACK_PROMPT_VERSION, subject=sid,
                          system=BACK_SYSTEM, prompt=f"Sentences:\n{_numbered(zh_texts)}")
    back, back_call = ctx.gateway.call("verifier", back_req, SentenceTranslations)
    report.llm_call_ids.append(back_call.id)
    report.dropped.extend(f"{sid} {n}" for n in back.invalid_notes("back_translate"))
    back_by_index = {s.index: s.text for s in back.sentences}
    for i, en in enumerate(en_texts):
        ov = content_overlap(en, back_by_index.get(i, ""))
        if ov < BACK_THRESHOLD:
            issues.append(f"sentence {i}: back-translation overlap {ov:.2f} < {BACK_THRESHOLD}")

    status = ReviewStatus.AUTO_VERIFIED if not issues else ReviewStatus.FLAGGED
    zh_seg = Segment(
        id=sid, concept_id=seg.concept_id, kind=seg.kind, lang=Lang.ZH,
        sentences=[CitedSentence(text=zh, citations=s.citations)
                   for zh, s in zip(zh_texts, seg.sentences, strict=True)],
        license_obligations=seg.license_obligations, review_status=status,
        translated_from=seg.id)
    fp = review_fingerprint(zh_seg)
    if issues:
        ctx.queue.enqueue(ReviewTarget.TRANSLATION, sid, "translation_check_failed",
                          payload={"issues": issues}, fingerprint=fp)
    resolved = ctx.queue.resolve(ReviewTarget.TRANSLATION, sid, status, fingerprint=fp)
    if resolved is None:
        report.dropped.append(f"{sid}: rejected by reviewer")
        return None, report
    (report.flagged if resolved is ReviewStatus.FLAGGED else report.created).append(sid)
    if resolved is ReviewStatus.AUTO_VERIFIED and spot_check_sample([sid],
                                                                    ctx.spot_check_fraction):
        ctx.queue.enqueue(ReviewTarget.TRANSLATION, sid, "spot_check", spot_check=True,
                          fingerprint=fp)
    return zh_seg.model_copy(update={"review_status": resolved}), report
