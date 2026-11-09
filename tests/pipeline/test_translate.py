from dataclasses import replace

from evowiki.content.models import (
    CitedSentence,
    Lang,
    LicenseObligations,
    ReviewStatus,
    Segment,
    SegmentKind,
)
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.scripted import ScriptedProvider
from evowiki.pipeline.review import ReviewState, ReviewTarget
from evowiki.pipeline.translate import (
    BACK_PURPOSE,
    TRANSLATE_PURPOSE,
    Glossary,
    GlossaryEntry,
    content_overlap,
    glossary_violations,
    translate_segment,
)
from tests.pipeline.helpers import GW_CONFIG, MINI_CONCEPTS, NOW, respond

EN = Segment(id="pca.one_line.en", concept_id="pca", kind=SegmentKind.ONE_LINE, lang=Lang.EN,
             sentences=[CitedSentence(text="PCA finds directions of maximal variance.",
                                      citations=["p_pca2"])],
             license_obligations=LicenseObligations(attribution=["fx"], share_alike=False,
                                                    non_commercial=False),
             review_status=ReviewStatus.AUTO_VERIFIED)
GLOSSARY = Glossary.from_concepts(MINI_CONCEPTS, extra=[GlossaryEntry(term_en="subspace",
                                                                      term_zh="子空间")])
GOOD_ZH = "主成分分析（PCA）寻找方差（variance）最大的方向。"


def _tables(zh: str, back: str):
    gen = {(TRANSLATE_PURPOSE, EN.id): {"sentences": [{"index": 0, "text": zh}]}}
    ver = {(BACK_PURPOSE, "pca.one_line.zh"): {"sentences": [{"index": 0, "text": back}]}}
    return gen, ver


def test_glossary_terms_longest_first():
    g = Glossary.from_concepts(MINI_CONCEPTS)
    terms = [e.term_en for e in g.terms_in("The covariance matrix and PCA.")]
    assert terms == ["Covariance Matrix", "PCA"]


def test_glossary_violations_require_zh_and_english():
    assert glossary_violations(EN.sentences[0].text, GOOD_ZH, GLOSSARY) == []
    v = glossary_violations(EN.sentences[0].text, "主成分分析寻找方差最大的方向。", GLOSSARY)
    assert v == ["missing original English term 'PCA'", "missing original English term 'variance'"]
    v = glossary_violations(EN.sentences[0].text, "PCA finds variance directions", GLOSSARY)
    assert "missing zh term '主成分分析' for 'PCA'" in v


def test_content_overlap():
    assert content_overlap("PCA finds directions of maximal variance.",
                           "Principal component analysis (PCA) finds the directions of "
                           "maximal variance.") >= 0.6
    assert content_overlap("", "") == 1.0
    assert content_overlap("PCA finds directions", "It is a cat") == 0.0


def test_translate_segment_passes_checks(make_ctx):
    gen, ver = _tables(GOOD_ZH, "Principal component analysis (PCA) finds the directions "
                                "of maximal variance.")
    ctx = make_ctx(gen, ver)
    zh, report = translate_segment(ctx, EN, GLOSSARY)
    assert zh.id == "pca.one_line.zh" and zh.lang is Lang.ZH
    assert zh.translated_from == EN.id and zh.review_status is ReviewStatus.AUTO_VERIFIED
    assert zh.sentences[0].citations == ["p_pca2"] and zh.body == GOOD_ZH
    assert zh.license_obligations == EN.license_obligations
    assert report.created == ["pca.one_line.zh"] and len(report.llm_call_ids) == 2


def test_translate_segment_flags_glossary_and_backtranslation_failures(make_ctx):
    gen, ver = _tables("主成分分析寻找方差最大的方向。", "It is a cat.")
    ctx = make_ctx(gen, ver)
    zh, _ = translate_segment(ctx, EN, GLOSSARY)
    assert zh.review_status is ReviewStatus.FLAGGED
    [item] = ctx.queue.items(target=ReviewTarget.TRANSLATION)
    assert item.reason == "translation_check_failed"
    issues = "\n".join(item.payload["issues"])
    assert "missing original English term 'PCA'" in issues and "back-translation" in issues


def test_translate_segment_drops_incomplete_translation(make_ctx):
    gen = {(TRANSLATE_PURPOSE, EN.id): {"sentences": []}}
    zh, report = translate_segment(make_ctx(gen, {}), EN, GLOSSARY)
    assert zh is None and "pca.one_line.zh: translation missing sentence 0" in report.dropped


def _gateway(path, zh: str, back: str) -> Gateway:
    gen, ver = _tables(zh, back)
    return Gateway(GW_CONFIG, CallLog(path), mode="record",
                   providers={"gen": ScriptedProvider("gen", respond(gen)),
                              "ver": ScriptedProvider("ver", respond(ver))},
                   clock=lambda: NOW)


def test_translation_approval_binds_to_zh_content(make_ctx, tmp_path):
    bad_zh, back = "主成分分析寻找方差最大的方向。", "PCA finds directions of maximal variance."
    ctx = make_ctx(*_tables(bad_zh, back))
    zh, _ = translate_segment(ctx, EN, GLOSSARY)
    assert zh.review_status is ReviewStatus.FLAGGED
    [item] = ctx.queue.items(target=ReviewTarget.TRANSLATION)
    ctx.queue.decide(item.id, ReviewState.APPROVED, decided_by="h", now=NOW)

    same = replace(ctx, gateway=_gateway(tmp_path / "same.jsonl", bad_zh, back))
    zh, report = translate_segment(same, EN, GLOSSARY)
    assert zh.review_status is ReviewStatus.HUMAN_APPROVED and report.created == [zh.id]

    other_zh = "主成分分析寻找方差变化最大的方向。"
    changed = replace(ctx, gateway=_gateway(tmp_path / "changed.jsonl", other_zh, back))
    zh, report = translate_segment(changed, EN, GLOSSARY)
    assert zh.body == other_zh and zh.review_status is ReviewStatus.FLAGGED
    assert report.flagged == [zh.id]
    open_items = ctx.queue.items(state=ReviewState.OPEN, target=ReviewTarget.TRANSLATION)
    assert [i.target_id for i in open_items] == ["pca.one_line.zh"]
