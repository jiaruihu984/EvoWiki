"""Rebuilds metacademy_ml.json and lecturebank_ml.json from the two downloaded sources (see
README.md). Not a test and never run by pytest; reads the sources as data only (no code from them
is executed). Usage, from the repository root:

    python -I tests/llm_cases/gold/build_gold.py <metacademy-content dir> <LectureBank LB-Paper dir>

<metacademy-content dir>: an extracted metacademy-content tarball (has concepts/ and courses/).
<LB-Paper dir>: holds 208topics.csv and prerequisite_annotation.csv from Yale-LILY/LectureBank.

The aliases below are the only hand-curated part (README.md "Aliases"); the closure and the core
trimming come from the source graph by `coverage.core_of`. A core concept without aliases fails
the build (fail closed)."""

import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from coverage import core_of, support_min

MC_COMMIT = "2654877392cea3857e9fa91ddf896a4a2f5361c8"
LB_COMMIT = "b237565ded5b112295fd5bc39035f53b0dd4286b"

# ---- shared aliases (zh first, then en); exclude = names containing these never count ----------
LINREG = ["线性回归", "最小二乘回归", "linear regression"]
LOGREG = ["逻辑回归", "逻辑斯蒂回归", "对数几率回归", "logistic回归", "logistic regression"]
GD = ["梯度下降", "gradient descent"]
REG = ["正则化", "正则项", "权重衰减", "regularization", "weight decay"]
BIASVAR = ["偏差方差", "偏差与方差", "方差偏差", "bias variance"]
NN = ["神经网络", "前馈网络", "多层感知机", "MLP", "neural network", "neural net"]
BACKPROP = ["反向传播", "BP算法", "backpropagation", "backprop"]
SVM = ["支持向量机", "SVM", "support vector machine"]
KMEANS = ["K均值", "k-means", "kmeans"]
PCA = ["主成分分析", "主成分", "PCA", "principal component"]
DTREE = ["决策树", "decision tree"]
MATMUL = ["矩阵乘法", "矩阵相乘", "矩阵乘", "矩阵运算", "matrix multiplication"]
SVD = ["奇异值分解", "奇异值", "SVD", "singular value"]

MC_ALIASES: dict[str, tuple[list[str], list[str]]] = {
    # ML anchors
    "linear_regression": (LINREG, []),
    "logistic_regression": (LOGREG, []),
    "gradient_descent": (GD, []),
    "regularization": (REG, []),
    "cross_validation": (["交叉验证", "留一法", "验证集", "cross validation"], []),
    "bias_variance_decomposition": (BIASVAR, []),
    "feed_forward_neural_nets": (NN, []),
    "backpropagation": (BACKPROP, []),
    "support_vector_machine": (SVM, []),
    "k_means": (KMEANS, []),
    "principal_component_analysis": (PCA, []),
    "naive_bayes": (["朴素贝叶斯", "naive bayes"], []),
    "decision_trees": (DTREE, []),
    # linear algebra
    "vectors": (["向量", "vector"],
                ["支持向量", "特征向量", "向量空间", "向量化", "词向量", "support vector",
                 "eigenvector", "vector space", "vectoriz"]),
    "dot_product": (["点积", "内积", "点乘", "数量积", "dot product", "inner product"], []),
    "matrix_multiplication": (MATMUL, []),
    "matrix_transpose": (["转置", "transpose"], []),
    "matrix_inverse": (["逆矩阵", "矩阵的逆", "矩阵求逆", "矩阵逆", "可逆", "matrix inverse",
                        "invertible"], []),
    "linear_systems_as_matrices": (["线性方程组", "方程组", "线性系统", "linear system",
                                    "linear equation"], []),
    "gaussian_elimination": (["高斯消元", "消元法", "初等行变换", "行变换",
                              "gaussian elimination"], []),
    "subspaces": (["子空间", "张成", "生成空间", "subspace", "span"], ["正交子空间", "基本子空间"]),
    "bases": (["基向量", "基底", "一组基", "维数", "线性无关", "线性相关", "basis", "linear independ"],
              ["基函数", "basis function"]),
    "column_space_and_nullspace": (["列空间", "零空间", "核空间", "秩", "column space", "null space",
                                    "nullspace", "rank"], []),
    "four_fundamental_subspaces": (["四个基本子空间", "基本子空间", "fundamental subspace"], []),
    "orthogonal_subspaces": (["正交子空间", "正交补", "正交", "orthogonal"],
                             ["标准正交", "正交基", "正交矩阵", "orthonormal"]),
    "projection_onto_a_subspace": (["投影", "projection"], []),
    "vector_spaces": (["向量空间", "线性空间", "vector space"], []),
    "linear_transformations_as_matrices": (["线性变换", "线性映射", "linear transformation",
                                            "linear map"], []),
    "change_of_basis": (["基变换", "坐标变换", "换基", "change of basis"], []),
    "determinant": (["行列式", "determinant"], []),
    "eigenvalues_and_eigenvectors": (["特征值", "特征向量", "eigenvalue", "eigenvector"], []),
    "spectral_decomposition": (["谱分解", "特征分解", "特征值分解", "对角化", "spectral decomposition",
                                "eigendecomposition", "diagonaliz"], []),
    "orthonormal_bases": (["标准正交基", "正交基", "正交矩阵", "施密特正交化", "orthonormal",
                           "gram schmidt"], []),
    "positive_definite_matrices": (["正定", "positive definite", "positive semidefinite"], []),
    "singular_value_decomposition": (SVD, []),
    "complex_numbers": (["复数", "complex number"], []),
    "roots_of_polynomials": (["多项式的根", "多项式根", "代数基本定理", "roots of polynomial",
                              "polynomial root"], []),
    # multivariable calculus
    "functions_of_several_variables": (["多元函数", "多变量函数", "多元微积分",
                                        "functions of several variables", "multivariable"], []),
    "partial_derivatives": (["偏导数", "偏导", "偏微分", "partial derivative"], []),
    "gradient": (["梯度", "gradient"], ["梯度下降", "gradient descent"]),
    "linear_approximation": (["线性近似", "线性逼近", "一阶近似", "全微分", "可微", "泰勒展开",
                              "linear approximation", "differentiab", "taylor"], []),
    "limits_and_continuity_in_rn": (["极限", "连续性", "limit", "continuity"], []),
    "multiple_integrals": (["重积分", "多重积分", "二重积分", "multiple integral",
                            "double integral"], []),
    "optimization_problems": (["最优化", "优化问题", "优化方法", "极值", "最小化", "optimization",
                               "minimiz"], []),
    "chain_rule": (["链式法则", "链式求导", "chain rule"], []),
    # probability theory
    "probability": (["概率", "probability"], []),
    "conditional_probability": (["条件概率", "贝叶斯定理", "贝叶斯公式", "conditional probability",
                                 "bayes rule", "bayes theorem"], []),
    "independent_events": (["独立事件", "事件独立", "独立性", "independent event", "independence"],
                           []),
    "random_variables": (["随机变量", "random variable"], []),
    "independent_random_variables": (["独立随机变量", "随机变量的独立", "独立同分布", "独立性", "iid",
                                      "independent random variable", "independence"], []),
    "multivariate_distributions": (["多元分布", "联合分布", "联合概率", "边缘分布", "多维随机变量",
                                    "joint distribution", "multivariate distribution"], []),
    "expectation_and_variance": (["期望", "方差", "expectation", "variance"],
                                 ["偏差方差", "偏差与方差", "方差偏差", "bias variance"]),
    # ML building blocks
    "generalization": (["泛化", "过拟合", "欠拟合", "generalization", "overfitting",
                        "underfitting"], []),
    "binary_linear_classifiers": (["线性分类", "二分类", "二元分类", "感知机", "linear classifier",
                                   "binary classification", "perceptron"], []),
    "basis_function_expansions": (["基函数", "特征映射", "特征变换", "多项式回归", "basis function",
                                   "feature map", "polynomial regression"], []),
    "stochastic_gradient_descent": (["随机梯度下降", "小批量", "SGD", "stochastic gradient",
                                     "mini batch"], []),
}

# Metacademy dependency tags that name a renamed / unmerged concept: tag → the concept it means.
MC_TAG_FIX = {"feed_forward_neural_networks": "feed_forward_neural_nets",
              "random variables": "random_variables", "variance": "expectation_and_variance"}
MC_FIELDS = {"linear_algebra": ["线性代数", "linear algebra"],
             "multivariable_calculus": ["微积分", "多元微积分", "多变量微积分", "calculus"],
             "probability_theory": ["概率论", "概率与统计", "概率统计", "probability theory"]}
# intro-ML anchor set (README.md "Goals"): the supervised / unsupervised topics of a standard
# introductory ML course (Ng's CS229 / Coursera ML syllabus) that exist as Metacademy concepts.
MC_GOALS = [
    ("机器学习", "machine learning",
     ["linear_regression", "logistic_regression", "gradient_descent", "regularization",
      "cross_validation", "bias_variance_decomposition", "feed_forward_neural_nets",
      "backpropagation", "support_vector_machine", "k_means", "principal_component_analysis",
      "naive_bayes", "decision_trees"]),
    ("神经网络", "neural networks", ["feed_forward_neural_nets", "backpropagation"]),
    ("奇异值分解", "singular value decomposition", ["singular_value_decomposition"]),
]

LB_ALIASES: dict[str, tuple[list[str], list[str]]] = {
    "75": (LINREG, []), "28": (LOGREG, []), "119": (GD, []), "173": (REG, []),
    "160": (BIASVAR, []), "158": (NN, []), "26": (BACKPROP, []), "89": (SVM, []),
    "116": (KMEANS, []), "90": (DTREE, []),
    "84": (["降维", "主成分分析", "主成分", "PCA", "dimensionality reduction"], []),
    "166": (SVD, []),
    "202": (["线性代数", "linear algebra"], []),
    "203": (["概率", "probability"], []),
    "127": (MATMUL, []),
    "168": (["损失函数", "代价函数", "目标函数", "均方误差", "交叉熵损失", "loss function",
             "cost function"], []),
    "128": (["微积分", "微分", "导数", "求导", "calculus", "derivative", "differential"], []),
    "180": (["激活函数", "sigmoid", "relu", "activation function"], []),
}
# LectureBank labels kept out of the gold (README.md "Exclusions"), with the reason.
LB_EXCLUDE = {
    "208": "relational databases: a CS-infrastructure topic, not a learning prerequisite of "
           "regression / SVM / k-means / decision trees (annotation noise)",
    "130": "backpropagation through time: an RNN method that builds on neural nets; labelled as "
           "a prerequisite of 'Artificial neural network' (direction reversed)",
}
LB_GOALS = [
    ("机器学习", "machine learning",
     ["75", "28", "119", "173", "160", "158", "26", "89", "116", "84", "90"]),
    ("神经网络", "neural networks", ["158", "26"]),
    ("奇异值分解", "singular value decomposition", ["166"]),
]


def closure(edges: dict[str, list[str]], anchors: list[str]) -> set[str]:
    seen, stack = set(anchors), list(anchors)
    while stack:
        for d in edges.get(stack.pop(), ()):
            if d not in seen:
                seen.add(d)
                stack.append(d)
    return seen


def concept_entry(name: str, url: str, aliases: dict, cid: str, core: bool, **extra) -> dict:
    out = {"name": name, "url": url, **extra}
    if cid in aliases:
        al, ex = aliases[cid]
        out["aliases"] = al
        if ex:
            out["exclude"] = ex
    elif core:
        raise SystemExit(f"core concept {cid} ({name}) has no aliases")
    return out


def goals_block(edges, goals, aliases, fields_of=None):
    out, used = [], set()
    for goal, goal_en, anchors in goals:
        clo = closure(edges, anchors)
        sub = {c: sorted(d for d in edges.get(c, ()) if d in clo) for c in sorted(clo)}
        k = support_min(len(anchors))
        core = core_of(sub, anchors, k)
        for c in core:
            if c not in aliases:
                raise SystemExit(f"{goal}: core concept {c} has no aliases")
        g = {"goal": goal, "goal_en": goal_en, "anchors": anchors, "support_min": k,
             "closure_size": len(clo), "core": core, "edges": sub}
        if fields_of is not None:
            g["fields"] = sorted({fields_of[c] for c in core if c in fields_of})
        out.append(g)
        used |= clo
    return out, used


def build_metacademy(root: Path) -> dict:
    raw = {}
    for d in sorted((root / "concepts").iterdir()):
        if not d.is_dir() or not (d / "id.txt").exists():      # ANNOTATED_EXAMPLE
            continue
        deps = []
        dep_file = d / "dependencies.txt"
        if dep_file.exists():
            for line in dep_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("tag:"):
                    t = line[4:].strip()
                    t = MC_TAG_FIX.get(t, t.replace("-", "_"))
                    deps.append(t)
        title = (d / "title.txt").read_text(encoding="utf-8").strip()
        raw[d.name] = {"title": title, "id": (d / "id.txt").read_text(encoding="utf-8").strip(), "deps": deps}
    edges = {t: [d for d in v["deps"] if d in raw] for t, v in raw.items()}
    fields_of = {}
    for course in sorted((root / "courses").iterdir()):
        for t in (course / "concepts.txt").read_text(encoding="utf-8").split():
            fields_of.setdefault(t.replace("-", "_"), course.name)
    goals, used = goals_block(edges, MC_GOALS, MC_ALIASES, fields_of)
    cores = {c for g in goals for c in g["core"]}
    concepts = {}
    for t in sorted(used):
        extra = {"metacademy_id": raw[t]["id"]}
        if t in fields_of:
            extra["field"] = fields_of[t]
        concepts[t] = concept_entry(
            raw[t]["title"], f"https://github.com/metacademy/metacademy-content/tree/{MC_COMMIT}"
            f"/concepts/{t}", MC_ALIASES, t, t in cores, **extra)
    unresolved = sorted({d for t in used for d in raw[t]["deps"] if d not in raw})
    return {
        "source": {
            "name": "Metacademy",
            "repository": "https://github.com/metacademy/metacademy-content",
            "commit": MC_COMMIT,
            "files": ["concepts/<tag>/{title,id,dependencies}.txt", "courses/<course>/concepts.txt"],
            "license": "CC BY-SA 3.0",
            "license_url": "https://creativecommons.org/licenses/by-sa/3.0/",
            "license_text": "LICENSE.txt: Creative Commons Attribution-ShareAlike 3.0 Unported",
            "attribution": "Metacademy content (metacademy.org, Roger Grosse, Colorado Reed and "
                           "contributors), prerequisite graph from metacademy-content; this "
                           "fixture is a derived excerpt (closure + trimming + Chinese aliases) "
                           "shared under the same license.",
            "notes": "The repository is deprecated (the site moved to a database) but holds the "
                     "expert-curated graph. Dependency tags naming a renamed concept are mapped "
                     "(tag_fixes); tags with no concept folder are dropped (unresolved).",
        },
        "rule": rule_text(),
        "tag_fixes": MC_TAG_FIX,
        "unresolved": unresolved,
        "fields": {f: {"name": f.replace("_", " "), "aliases": a} for f, a in MC_FIELDS.items()},
        "concepts": concepts,
        "goals": goals,
    }


def build_lecturebank(lb: Path) -> dict:
    topics = {}
    with (lb / "208topics.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if row:
                topics[row[0].strip()] = (row[1].strip(), row[2].strip())
    edges: dict[str, list[str]] = {}
    unnamed = set()
    with (lb / "prerequisite_annotation.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if not row or row[2].strip() != "1":
                continue
            src, tgt = row[0].strip(), row[1].strip()
            for x in (src, tgt):
                if x not in topics:
                    unnamed.add(x)
            if src in topics and tgt in topics and src not in LB_EXCLUDE and tgt not in LB_EXCLUDE:
                edges.setdefault(tgt, []).append(src)       # source is a prerequisite of target
    goals, used = goals_block(edges, LB_GOALS, LB_ALIASES)
    cores = {c for g in goals for c in g["core"]}
    row_url = f"https://github.com/Yale-LILY/LectureBank/blob/{LB_COMMIT}/LB-Paper/208topics.csv"
    concepts = {t: concept_entry(topics[t][0],             # some rows have URL "NULL"
                                 topics[t][1] if topics[t][1].startswith("https://") else row_url,
                                 LB_ALIASES, t, t in cores, lecturebank_id=int(t))
                for t in sorted(used, key=int)}
    return {
        "source": {
            "name": "LectureBank",
            "repository": "https://github.com/Yale-LILY/LectureBank",
            "commit": LB_COMMIT,
            "files": ["LB-Paper/208topics.csv", "LB-Paper/prerequisite_annotation.csv"],
            "license": "none stated (the repository has no LICENSE file and its READMEs state "
                       "no terms; only a citation request)",
            "attribution": "Irene Li, Alexander R. Fabbri, Robert R. Tung, Dragomir R. Radev. "
                           "What Should I Learn First: Introducing LectureBank for NLP Education "
                           "and Prerequisite Chain Learning. AAAI 2019. arXiv:1811.12181.",
            "notes": "Only topic ids, topic names, Wikipedia URLs and prerequisite pairs for a few "
                     "topics are excerpted (about 20 of 208 topics), for evaluation, with the "
                     "citation; ask the authors before redistributing more. Pairs naming ids "
                     "with no topic in 208topics.csv are dropped (unnamed_ids).",
        },
        "rule": rule_text(),
        "unnamed_ids": sorted(unnamed, key=int),
        "excluded": LB_EXCLUDE,
        "concepts": concepts,
        "goals": goals,
    }


def rule_text() -> str:
    return ("closure = the anchors and every concept reachable through 'is a prerequisite of' "
            "edges; core = the anchors plus each closure concept that at least support_min = "
            "max(1, round(0.25 x number of anchors)) anchors reach; a decomposition is scored "
            "on core only (tests/llm_cases/gold/coverage.py)")


def main(argv: list[str]) -> None:
    if len(argv) != 3:
        raise SystemExit(__doc__)
    for name, data in (("metacademy_ml.json", build_metacademy(Path(argv[1]))),
                       ("lecturebank_ml.json", build_lecturebank(Path(argv[2])))):
        (HERE / name).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                 encoding="utf-8")
        print(name, [(g["goal"], g["closure_size"], len(g["core"])) for g in data["goals"]])


if __name__ == "__main__":
    main(sys.argv)
