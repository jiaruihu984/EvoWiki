"""Scripted answers of the shared one-level split (goals.enter, enter@v3) as a 细分 entrance gets
them (wikiapp.node_split): parts, or the node left whole (`keep`)."""

from evowiki.goals.intake import ENTER_PURPOSE

PURPOSE = ENTER_PURPOSE


def answer(**kw) -> dict:
    """矩阵乘法 split into three parts (the second builds on the first: one `recommended`)."""
    base = {"parts": [
        {"name": "一行乘一列", "summary": "结果的每个数是一行和一列的点积", "atomic": True,
         "aspects": ["procedure", "term"]},
        {"name": "维度要对上", "summary": "左边的列数等于右边的行数", "atomic": True,
         "aspects": ["term"]},
        {"name": "不满足交换律", "summary": "AB 一般不等于 BA", "atomic": False,
         "aspects": ["interpret", "心情"]}],
        "recommended": [{"from": "一行乘一列", "to": "不满足交换律", "why": "先会算才看得出顺序有影响"}]}
    return {**base, **kw}


def parts(*names: str, **kw) -> dict:
    """A split into these parts (no aspects, not judged)."""
    return {"parts": [{"name": n, "summary": f"{n}是什么"} for n in names], **kw}


def keep(why: str = "它已经是一件事，换个例子讲会更清楚") -> dict:
    """The node left whole."""
    return {"parts": [], "keep": why}
