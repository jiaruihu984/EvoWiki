"""Gold-standard coverage helper (tests/llm_cases/gold/, used by the live goals.intake gold case
tests/llm_cases/live/test_live_gold_coverage.py). Offline: hand-made decomposition outputs only,
no LLM. Checks the fixtures follow their documented rule (core recomputed from the stored source
edges), carry source / license / attribution, and that the scorer matches by alias, honours
excludes, and ranks a decomposition without 概率论 lower with probability listed as missing. The two
parts are judged apart (user ruling 2026-10-11): the goal's own core topics (`core_score`, over the
anchors) and its prerequisites (`prereq_score`), each with its floor; a math-only decomposition
fails the core floor and a core-only one the prereq floor."""

import json
import re

import pytest

from tests.llm_cases.gold.coverage import (
    HAS_LECTUREBANK,
    HERE,
    LECTUREBANK,
    MIN_ML_CORE,
    MIN_ML_COVERAGE,
    MIN_ML_PREREQ,
    GoldConcept,
    core_of,
    coverage,
    load_goal,
    matches,
    normalize,
    support_min,
)

FIXTURES = ("metacademy_ml.json", *((LECTUREBANK,) if HAS_LECTUREBANK else ()))
needs_lb = pytest.mark.skipif(not HAS_LECTUREBANK, reason="LectureBank excerpt is local-only "
                              "(no license): build it with tests/llm_cases/gold/build_gold.py")
CJK = re.compile(r"[一-鿿]")

# A plausible goal-level ML decomposition (regions + concrete members), written by hand.
REFERENCE_ML = [
    "线性代数", "微积分", "概率论", "机器学习基础", "监督学习", "无监督学习", "神经网络",
    "向量", "矩阵乘法", "矩阵转置", "逆矩阵", "特征值与特征向量", "导数", "偏导数", "梯度", "链式法则",
    "随机变量", "条件概率", "贝叶斯定理", "期望与方差", "正态分布", "最大似然估计",
    "损失函数", "过拟合与欠拟合", "训练集与测试集", "线性回归", "逻辑回归", "梯度下降", "正则化",
    "决策树", "K均值聚类", "主成分分析", "前馈神经网络", "反向传播",
]
PROBABILITY_WORDS = ("概率", "随机变量", "贝叶斯", "期望", "正态分布", "似然")
NO_PROBABILITY_ML = [n for n in REFERENCE_ML if not any(w in n for w in PROBABILITY_WORDS)]
REGIONS_ONLY_ML = ["线性代数", "微积分", "概率论", "机器学习"]
# Its two halves: the math regions with their points, and the goal's own ML part.
MATH_ONLY_ML = REFERENCE_ML[:3] + REFERENCE_ML[7:22]     # 线性代数 微积分 概率论 + 向量 … 最大似然估计
CORE_ONLY_ML = [n for n in REFERENCE_ML if n not in MATH_ONLY_ML]
# Live run 2 (intake@v7, the product specification):
# regions and members as named. The math is there; the ML core topics and calculus are not.
LIVE2_ML = ["线性代数（机器学习用到的部分）", "向量和点积", "矩阵和矩阵乘法", "矩阵转置、逆、行列式",
            "概率论（机器学习用到的部分）", "随机变量和概率分布", "条件概率和贝叶斯公式", "期望和方差",
            "优化", "损失函数", "梯度下降", "学习率", "监督学习", "线性回归", "逻辑回归"]


def _data(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_core_follows_the_documented_rule(name):
    data = _data(name)
    assert data["goals"], name
    for g in data["goals"]:
        k = support_min(len(g["anchors"]))
        assert g["support_min"] == k
        assert g["core"] == core_of(g["edges"], g["anchors"], k), g["goal"]
        assert set(g["core"]) <= set(g["edges"]) <= set(data["concepts"])
        assert g["closure_size"] == len(g["edges"])


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_names_source_license_and_attribution(name):
    src = _data(name)["source"]
    for key in ("name", "repository", "commit", "license", "attribution"):
        assert src.get(key), (name, key)
    assert re.fullmatch(r"[0-9a-f]{40}", src["commit"])


@pytest.mark.parametrize("name", FIXTURES)
def test_every_core_concept_has_chinese_and_english_aliases(name):
    data = _data(name)
    for g in data["goals"]:
        for cid in g["core"]:
            c = data["concepts"][cid]
            assert any(CJK.search(a) for a in c["aliases"]), (cid, c["aliases"])
            assert any(not CJK.search(a) for a in c["aliases"]), (cid, c["aliases"])
            assert c["name"] and c["url"].startswith("https://")


def test_core_of_counts_anchor_support_and_tolerates_cycles():
    edges = {"a": ["x", "y"], "b": ["x"], "x": ["z"], "y": ["y2"], "z": ["x"]}
    assert core_of(edges, ["a", "b"], 2) == ["a", "b", "x", "z"]
    assert core_of(edges, ["a", "b"], 1) == ["a", "b", "x", "z", "y", "y2"]
    assert support_min(13) == 3 and support_min(11) == 3 and support_min(2) == 1


def test_normalize_and_aliases_ignore_case_width_and_punctuation():
    assert normalize("K-Means （ＰＣＡ）") == "kmeanspca"
    km = GoldConcept(id="k", name="k-means", aliases=("K均值", "kmeans"))
    assert matches("K-Means 聚类", km) and matches("k 均值聚类", km)
    assert not matches("层次聚类", km)


def test_excludes_keep_narrower_names_from_counting_for_the_broad_concept():
    ml = load_goal("metacademy_ml.json", "机器学习")
    gradient, vectors = ml.concept("gradient"), ml.concept("vectors")
    assert matches("梯度", gradient) and not matches("梯度下降", gradient)
    assert matches("向量", vectors)
    assert not matches("支持向量机", vectors) and not matches("特征向量", vectors)
    variance = ml.concept("expectation_and_variance")
    assert matches("期望与方差", variance) and not matches("偏差与方差权衡", variance)


def test_reference_decomposition_clears_both_floors_and_regions_only_does_not():
    for name in FIXTURES:
        gold = load_goal(name, "机器学习")
        ref, thin = coverage(REFERENCE_ML, gold), coverage(REGIONS_ONLY_ML, gold)
        assert ref.score >= MIN_ML_COVERAGE[name] + 0.1, (name, ref.as_report())
        assert thin.score < MIN_ML_COVERAGE[name], (name, thin.as_report())


def test_reference_clears_the_core_and_prereq_floors_and_each_half_fails_the_other():
    for name in FIXTURES:
        gold = load_goal(name, "机器学习")
        ref = coverage(REFERENCE_ML, gold)
        math, core = coverage(MATH_ONLY_ML, gold), coverage(CORE_ONLY_ML, gold)
        assert ref.core_score >= MIN_ML_CORE[name] + 0.1, (name, ref.as_report())
        assert ref.prereq_score >= MIN_ML_PREREQ[name] + 0.1, (name, ref.as_report())
        assert math.core_score < MIN_ML_CORE[name], (name, math.as_report())     # all math, no ML
        assert core.prereq_score < MIN_ML_PREREQ[name], (name, core.as_report())  # ML, no math
        assert coverage(REGIONS_ONLY_ML, gold).core_score < MIN_ML_CORE[name]


def test_core_score_is_over_the_anchors_and_prereq_score_over_the_rest():
    gold = load_goal("metacademy_ml.json", "机器学习")
    targets = [c for c in gold.concepts if c.role == "target"]
    cov = coverage(REFERENCE_ML, gold)
    assert cov.core_score == sum(c.id in cov.matched for c in targets) / len(targets)
    assert coverage(MATH_ONLY_ML, gold).core_score == 0
    names = [c.aliases[0] for c in targets]
    assert coverage(names, gold).core_score == 1


def test_live_run_2_math_without_the_ml_core_fails_the_core_floor():
    for name in FIXTURES:
        cov = coverage(LIVE2_ML, load_goal(name, "机器学习"))
        assert cov.core_score < MIN_ML_CORE[name], (name, cov.as_report())
        assert cov.score < MIN_ML_COVERAGE[name], (name, cov.as_report())
    mc = coverage(LIVE2_ML, load_goal("metacademy_ml.json", "机器学习"))
    assert not mc.fields["multivariable_calculus"], mc.as_report()
    assert {"cross_validation", "bias_variance_decomposition", "regularization",
            "feed_forward_neural_nets"} <= set(mc.missing)


def test_ml_decomposition_without_probability_scores_lower_and_lists_it_missing():
    mc = load_goal("metacademy_ml.json", "机器学习")
    full, cut = coverage(REFERENCE_ML, mc), coverage(NO_PROBABILITY_ML, mc)
    assert "probability" in full.matched and "概率论" in full.matched["probability"]
    assert cut.score < full.score and cut.prereq_score < full.prereq_score
    assert "probability" in cut.missing and "probability" in cut.missing_names
    assert {"conditional_probability", "random_variables"} <= set(cut.missing)
    assert full.fields["probability_theory"] and not cut.fields["probability_theory"]
    assert cut.fields["linear_algebra"]


@needs_lb
def test_lecturebank_ml_without_probability_lists_it_missing():
    lb = load_goal("lecturebank_ml.json", "机器学习")
    lb_full, lb_cut = coverage(REFERENCE_ML, lb), coverage(NO_PROBABILITY_ML, lb)
    assert "203" in lb_full.matched and "203" in lb_cut.missing       # 203 = Probability
    assert "Probability" in lb_cut.missing_names and lb_cut.score < lb_full.score


# REFERENCE_ML as an intake@v14 output: the areas are nodes, their points part_of them.
REFERENCE_PARTS = {
    "线性代数": ["向量", "矩阵乘法", "矩阵转置", "逆矩阵", "特征值与特征向量"],
    "微积分": ["导数", "偏导数", "梯度", "链式法则"],
    "概率论": ["随机变量", "条件概率", "贝叶斯定理", "期望与方差", "正态分布", "最大似然估计"],
    "机器学习基础": ["损失函数", "过拟合与欠拟合", "训练集与测试集", "梯度下降", "正则化"],
    "监督学习": ["线性回归", "逻辑回归", "决策树"],
    "无监督学习": ["K均值聚类", "主成分分析"],
    "神经网络": ["前馈神经网络", "反向传播"],
}


def _reference_nodes() -> dict:
    nodes, rels = [], []
    for i, (area, parts) in enumerate(REFERENCE_PARTS.items(), 1):
        nodes.append({"id": f"a{i}", "name": area, "target": i > 3})
        for j, name in enumerate(parts, 1):
            nodes.append({"id": f"a{i}p{j}", "name": name})
            rels.append({"kind": "part_of", "from": f"a{i}p{j}", "to": f"a{i}", "phrase": ""})
    rels += [{"kind": "prerequisite", "from": f"a{i}", "to": "a4", "phrase": ""} for i in (1, 2, 3)]
    return {"title": "机器学习", "nodes": nodes, "relations": rels}


def test_empty_output_scores_zero_and_misses_everything():
    gold = load_goal("metacademy_ml.json", "神经网络")
    cov = coverage(["", "  "], gold)
    assert cov.score == 0 and cov.core_score == 0 and cov.prereq_score == 0 and not cov.matched
    assert len(cov.missing) == len(gold.concepts)


def test_report_and_roles():
    gold = load_goal("metacademy_ml.json", "机器学习")
    assert sum(c.role == "target" for c in gold.concepts) == 13
    assert len(gold.concepts) == 42
    rep = coverage(REFERENCE_ML, gold).as_report()
    assert set(rep) == {"score", "core_score", "prereq_score", "matched", "missing", "fields"}
    json.dumps(rep, ensure_ascii=False)


def test_unknown_goal_fails_loudly():
    with pytest.raises(KeyError):
        load_goal("metacademy_ml.json", "量子计算")
