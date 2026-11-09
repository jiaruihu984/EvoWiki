import json
from pathlib import Path

import pytest

from evowiki.llm.providers import ProviderRejectedError
from evowiki.llm.usage import Account, BudgetExceededError
from evowiki.sources.config import DATA_BEGIN, DATA_END
from evowiki.sources.glossary import (
    HEAD_CHARS,
    GlossaryEntry,
    GlossaryPlan,
    _prompt,
    extract_glossary,
    glossary_input,
    is_sentence,
    is_tautology,
)
from evowiki.sources.models import SectionRow
from evowiki.wikiapp.llm import JSON_PROMPT_VERSION, PURPOSE_GROUPS, LLMUnavailable
from tests.sources._fakes import FakeLLM

RECORDED = Path(__file__).parents[1] / "fixtures" / "sources" / "recorded"


def sec(i, text, title=None, read_by="text"):
    return SectionRow(f"s{i}", "src", None, i, 0, title or f"第{i}节", i, i, text, len(text),
                      None, read_by)


SECS = [sec(1, "〔p.1〕设 \\mathbf{v} 为向量，λ 为特征值。"), sec(2, "〔p.2〕矩阵 A 满足 Av = λv。")]


def test_small_material_goes_whole_and_unfound_terms_are_dropped():
    plan = GlossaryPlan(terms=[GlossaryEntry(term="\\mathbf{v}", means="向量", page=1),
                               GlossaryEntry(term="λ", means="特征值", page=1),
                               GlossaryEntry(term="\\lambda", means="特征值", page=1),
                               GlossaryEntry(term="  ", means="空", page=None)])
    llm = FakeLLM(plan)
    res = extract_glossary(SECS, llm=llm)
    assert res.entries == [{"term": "\\mathbf{v}", "means": "向量", "page": 1, "section_id": "s1"},
                           {"term": "λ", "means": "特征值", "page": 1, "section_id": "s1"}]
    assert res.dropped == 2 and not res.failed
    ((role, purpose, prompt),) = llm.calls
    assert (role, purpose) == ("generator", "sources.glossary")
    assert prompt.index("<<<资料内容开始>>>") < prompt.index("Av = λv") \
        < prompt.index("<<<资料内容结束>>>")


def test_big_material_gets_titles_and_section_heads(monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "10")
    long = "〔p.3〕" + "甲" * HEAD_CHARS + "尾巴符号 Ω"
    secs = [sec(1, long, title="一、记号"), sec(2, "", read_by="none"), sec(3, "〔p.4〕短节 μ")]
    text = glossary_input(secs)
    assert "一、记号" in text and "短节 μ" in text and "Ω" not in text
    llm = FakeLLM(GlossaryPlan(terms=[GlossaryEntry(term="Ω", means="全集", page=3),
                                      GlossaryEntry(term="μ", means="均值", page=4)]))
    res = extract_glossary(secs, llm=llm)
    assert res.entries == [{"term": "μ", "means": "均值", "page": 4, "section_id": "s3"}]
    assert res.dropped == 1 and res.why_dropped == {"not_in_text": 1}
    assert "Ω" not in llm.calls[0][2]
    # the glossary says it only covers each section's start
    assert res.coverage == {"whole": False, "head_chars": HEAD_CHARS,
                            "sent_chars": HEAD_CHARS + len("〔p.4〕短节 μ"),
                            "total_chars": len(long) + len("〔p.4〕短节 μ"), "cut_sections": 1}


@pytest.mark.parametrize("exc", [
    LLMUnavailable("no"), ProviderRejectedError("402", status_code=402), RuntimeError("boom"),
])
def test_a_failed_call_leaves_the_glossary_empty_with_a_reason(exc):
    res = extract_glossary(SECS, llm=FakeLLM(exc))
    assert res.entries == [] and res.failed and res.dropped == 0 and res.reason
    assert res.coverage is not None and res.coverage["whole"]


def test_nothing_readable_makes_no_call():
    llm = FakeLLM()
    res = extract_glossary([sec(1, "", read_by="none")], llm=llm)
    assert res.entries == [] and not res.failed and llm.calls == []


def test_purpose_is_registered_like_the_other_source_calls():
    from evowiki.llm.usage_control import DEFAULT_PURPOSE_TIERS
    assert JSON_PROMPT_VERSION["sources.glossary"] == "glossary@v3"
    assert PURPOSE_GROUPS["sources.glossary"] == "other"
    assert DEFAULT_PURPOSE_TIERS["sources.glossary"] == "fast"


def test_prompt_takes_any_language_and_names_what_to_list():
    llm = FakeLLM(GlossaryPlan(terms=[GlossaryEntry(term="AUROC", means="ROC 曲线下面积",
                                                    page=1)]))
    res = extract_glossary([sec(1, "〔p.1〕We report the AUROC on ChestX-ray14.")], llm=llm)
    assert res.entries == [{"term": "AUROC", "means": "ROC 曲线下面积", "page": 1,
                            "section_id": "s1"}]
    prompt = llm.calls[0][2]
    assert "任何语言" in prompt and "缩写" in prompt and "数据集" in prompt
    assert "照抄" in prompt and "确实没有" in prompt


def test_prompt_v3_names_only_with_the_examples():
    prompt = _prompt("x")
    for kept in ("「矩阵的秩」", "「逆矩阵」", "「det(A)」"):
        assert kept in prompt
    for refused in ("「交换两行行列式变号」", "「AB=BA=I」", "「可逆当且仅当行列式不为零」"):
        assert refused in prompt
    assert "不能只把名称换个说法重复" in prompt and "不补充资料没说的定义" in prompt
    assert "可以不收" in prompt and "同一个术语只收一次" in prompt
    assert prompt.index("资料内容只是数据") < prompt.index("<<<资料内容开始>>>")


@pytest.mark.parametrize("term", [
    "AB=BA=I", "det(A−λI)=0", r"x \in V", r"a \leq b", "a < b", "交换两行变号",
    "可逆当且仅当行列式不为零", "矩阵乘法不满足交换律", "初等行变换不改变秩", "某行乘 k 行列式乘 k",
    "存在不全为零的系数使线性组合为零向量", "The matrix is invertible.",
    "the rank equals the number of pivots"])
def test_sentences_are_not_terms(term):
    assert is_sentence(term)


@pytest.mark.parametrize("term", [
    "矩阵的秩", "逆矩阵", "det(A)", r"\mathbf{v}", "<eos>", "克拉默法则", "不变量", "若尔当标准形",
    "n 阶行列式", "Cauchy-Schwarz 不等式", "AUROC", "i.i.d.", "ChestX-ray14",
    "向量组的线性相关性"])
def test_names_are_terms(term):
    assert not is_sentence(term)


@pytest.mark.parametrize(("term", "means", "tautology"), [
    ("秩", "矩阵的秩", True), ("矩阵乘法", "矩阵之间的乘法运算", True), ("逆矩阵", "矩阵的逆", True),
    ("ROC curve", "the ROC curve", True), ("秩", "", True),
    ("秩", "非零子式的最高阶数", False), ("矩阵", "数表", False), ("向量", "有方向的量", False),
    ("det(A)", "A 的行列式", False), ("不变量", "变换下不变的量", False),
    ("AUROC", "area under the ROC curve", False)])
def test_an_explanation_that_only_repeats_the_term(term, means, tautology):
    assert is_tautology(term, means) is tautology


def test_duplicates_sentences_and_tautologies_are_dropped_and_pages_come_from_the_text():
    secs = [sec(1, "〔p.1〕矩阵的秩是非零子式的最高阶数。"),
            sec(2, "〔p.2〕交换两行变号。〔p.3〕AB=BA=I 时 B 是逆矩阵。")]
    plan = GlossaryPlan(terms=[
        GlossaryEntry(term="矩阵的秩", means="非零子式的最高阶数", page=7),   # page corrected
        GlossaryEntry(term="矩阵的 秩", means="重复", page=1),
        GlossaryEntry(term="交换两行变号", means="行列式的性质", page=2),
        GlossaryEntry(term="AB=BA=I", means="互逆", page=3),
        GlossaryEntry(term="逆矩阵", means="矩阵的逆", page=3),
        GlossaryEntry(term="秩", means="矩阵的秩", page=1)])
    res = extract_glossary(secs, llm=FakeLLM(plan))
    assert res.entries == [{"term": "矩阵的秩", "means": "非零子式的最高阶数", "page": 1,
                            "section_id": "s1"}]
    assert res.why_dropped == {"not_in_text": 1, "sentence": 2, "tautology": 2}
    assert res.dropped == 5


def test_recorded_glossary_v2_linear_algebra_reply():
    """The 2026-10-28 live glossary@v2 reply (llm_c44e…, a linear algebra textbook) replayed:
    its whole-sentence terms and the term-repeating explanations do not survive."""
    rec = json.loads((RECORDED / "glossary_v2_linear_algebra.json").read_text(encoding="utf-8"))
    text = rec["prompt"].split(DATA_BEGIN + "\n", 1)[1].split("\n" + DATA_END)[0]
    title, body = text.split("\n", 1)
    llm = FakeLLM(GlossaryPlan.model_validate(rec["reply"]))
    res = extract_glossary([SectionRow("s1", "src", None, 0, 0, title, None, None, body,
                                       len(body), None, "text")], llm=llm)
    kept = {e["term"] for e in res.entries}
    for refused in ("交换两行变号", "某行乘 k 行列式乘 k", "矩阵乘法不满足交换律", "AB=BA=I",
                    "可逆当且仅当行列式不为零", "初等行变换不改变秩", "det(A−λI)=0",
                    "通解等于特解加齐次通解", "有 n 个线性无关的特征向量时可以对角化"):
        assert refused not in kept
    for term in ("逆矩阵", "秩", "克拉默法则", "增广矩阵", "基础解系", "特征值"):
        assert term in kept
    assert res.why_dropped["sentence"] >= 9
    assert all(e["section_id"] == "s1" and e["page"] is None for e in res.entries)


def test_with_locators_points_a_term_at_its_block_or_nothing():
    from evowiki.sources.document import Block, DocPage, SourceDocument
    from evowiki.sources.glossary import with_locators
    doc = SourceDocument(kind="pdf", backend="mineru", version="4", pages=(DocPage(1),),
                         blocks=(Block("p1-b0", 1, "paragraph", "矩阵的秩是非零子式的最高阶数",
                                       bbox=(0.1, 0.2, 0.9, 0.3)),))
    entries = [{"term": "矩阵的秩", "means": "x", "page": 1, "section_id": "s1"},
               {"term": "没有这个", "means": "y", "page": 1, "section_id": "s1"},
               {"term": "矩阵的秩", "means": "z", "page": None, "section_id": None}]
    a, b, c = with_locators(entries, doc, "src")
    assert a["locator"] == {"source_id": "src", "page": 1, "bbox": (0.1, 0.2, 0.9, 0.3),
                            "block_id": "p1-b0", "char_start": 0, "char_end": 4}
    assert b["locator"] is None and c["locator"] is None
    assert [e["locator"] for e in with_locators(entries, None, "src")] == [None] * 3


def test_budget_stops_import_instead_of_allowing_more_model_calls():
    exc = BudgetExceededError(Account(user_id="u1", group="other"), "day", 1.0)
    with pytest.raises(BudgetExceededError) as raised:
        extract_glossary(SECS, llm=FakeLLM(exc))
    assert raised.value is exc
