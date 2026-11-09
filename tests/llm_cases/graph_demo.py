"""The approved node-graph demo (the product specification the plugin's
graph-page/fixture.ts DEMO_NODES) as a Python NodeGraph: the 机器学习 / 数学 / 工具 graph with
multi-parent PCA, kind_of SVM / 决策树 and prerequisite phrases. Used by the graph.groups cases
(offline and live) and the graph API tests."""

from evowiki.nodes import NodeGraph, NodeRec, Relation, place_missing

# id: (name, parents, [(prerequisite id, phrase)], kind_of phrase)
ML_NODES: dict[str, tuple[str, tuple[str, ...], tuple[tuple[str, str], ...], str]] = {
    "vec": ("向量和点积", ("la",), (), ""),
    "mul": ("矩阵乘法", ("la",), (("vec", "一行乘一列就是点积"),), ""),
    "eig": ("特征值与特征向量", ("la",), (("mul", "Av 就是矩阵乘向量"),), ""),
    "la": ("线性代数", ("math",), (), ""),
    "der": ("导数", ("ca",), (), ""),
    "grad": ("梯度", ("ca",), (("der", "梯度是每个方向上的导数排在一起"),), ""),
    "ca": ("微积分", ("math",), (), ""),
    "mean": ("均值", ("pr",), (), ""),
    "var": ("方差", ("pr",), (("mean", "方差量的是离均值有多远"),), ""),
    "cond": ("条件概率", ("pr",), (), ""),
    "pr": ("概率统计", ("math",), (), ""),
    "py": ("Python", ("tools",), (), ""),
    "gd": ("梯度下降", ("opt",), (("grad", "每一步沿梯度的反方向走"),), ""),
    "linreg": ("线性回归", ("sl",), (("mul", "预测值 = Xw，就是矩阵乘法"),
                                     ("gd", "用梯度下降找到最好的 w")), ""),
    "loss": ("损失函数", ("sl",), (("linreg", "量的是预测和真实差多远"),), ""),
    "logreg": ("逻辑回归", ("sl",), (("linreg", "在线性回归外面套一个 S 形函数"),
                                     ("cond", "输出的是条件概率")), ""),
    "svm": ("SVM", ("sl",), (("vec", "间隔用点积来算"),), "一种分类模型"),
    "tree": ("决策树", ("sl",), (), "一种分类模型"),
    "nn": ("神经网络", ("sl",), (("logreg", "每个神经元就是一个逻辑回归"),
                                 ("gd", "靠梯度下降训练")), ""),
    "reg": ("正则化", ("ev",), (("loss", "在损失上加一项惩罚"),), ""),
    "of": ("过拟合与泛化", ("ev",), (), ""),
    "km": ("k-means", ("ul",), (("mean", "每一类的中心就是均值"),), ""),
    "pca": ("PCA 降维", ("ul", "la"), (("eig", "主方向就是协方差矩阵的特征向量"),
                                       ("var", "保留方差最大的方向")), ""),
    "math": ("数学", (), (), ""),
    "opt": ("优化", ("ml",), (), ""),
    "sl": ("监督学习", ("ml",), (), ""),
    "ev": ("模型评估", ("ml",), (), ""),
    "ul": ("无监督学习", ("ml",), (), ""),
    "ml": ("机器学习", (), (), ""),
    "tools": ("工具", (), (), ""),
    "clf": ("分类模型", ("sl",), (), ""),
}


def ml_graph() -> NodeGraph:
    nodes = [NodeRec(id=i, name=n) for i, (n, _, _, _) in ML_NODES.items()]
    rels: list[Relation] = []
    for i, (_, parents, pre, kind) in ML_NODES.items():
        rels += [Relation(kind="part_of", src=i, dst=p, source="intake") for p in parents]
        rels += [Relation(kind="prerequisite", src=s, dst=i, phrase=ph) for s, ph in pre]
        if kind:
            rels.append(Relation(kind="kind_of", src=i, dst="clf", phrase=kind))
    return place_missing(NodeGraph.of(nodes, rels))
