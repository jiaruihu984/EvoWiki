from tests.llm_cases.evalset import graders

CARD = ("已生成草稿（act_1）：「Python」分成 3 块。等用户在卡片上确认。\n分块（卡片上已经列出来了）：\n"
        "1. 变量与类型〔基础〕：存数据（包含：整数、字符串、列表）\n"
        "2. 循环与函数〔核心〕：重复做事（包含：for 循环、函数定义）\n"
        "3. 渗流实验〔应用〕：在随机图上删节点\n")


def card_rec(text: str = CARD) -> dict:
    return {"turns": [{"say": "x", "answer": "", "tools": [{"name": "new_goal", "ok": True, "result": text}],
                       "proposals": []}]}


def test_goal_card_names_are_read_and_off_topic_blocks_fail():
    names = graders.goal_names(card_rec())
    assert {"变量与类型", "整数", "for 循环", "渗流实验"} <= set(names)
    assert not graders.blocks_lack(card_rec(), terms=("渗流",)).ok        # goals.md: 渗流 blocks in a python draft
    assert graders.blocks_lack(card_rec(), terms=("孔雀鱼",)).ok
    assert graders.blocks_between(card_rec(), lo=3, hi=3).ok
    assert not graders.blocks_between(card_rec(), hi=2).ok
    none = {"turns": [{"say": "x", "answer": "", "tools": [], "proposals": []}]}
    assert not graders.blocks_lack(none, terms=("x",)).ok


def test_gold_coverage_fails_a_decomposition_that_misses_the_goal_and_passes_a_covering_one():
    assert not graders.gold_coverage(card_rec(), goal="机器学习").ok
    cover = ("1. 线性回归与逻辑回归〔核心〕：x（包含：梯度下降、正则化、交叉验证、反向传播、支持向量机、k-means、"
             "主成分分析、朴素贝叶斯、决策树、偏差方差、前馈神经网络、向量、点积、矩阵乘法、偏导数）\n")
    assert graders.gold_coverage(card_rec(cover), goal="机器学习", min_score=0.2, min_prereq=0.1).ok


def test_gold_coverage_reads_what_the_blocks_descriptions_say():
    """A topic named only in a block's summary or definition counts as taught; the names-only graders do not read them."""
    names_only = "1. 监督学习〔核心〕：x（包含：y）\n"
    in_summary = ("1. 监督学习〔核心〕：线性回归、逻辑回归、梯度下降、正则化、交叉验证、反向传播、支持向量机（包含：y）\n"
                  "2. 无监督学习〔核心〕：k-means、主成分分析、朴素贝叶斯、决策树、偏差方差、前馈神经网络（包含：z）\n"
                  "3. 数学〔基础〕：向量，点积，矩阵乘法，偏导数（包含：w）\n")
    assert not graders.gold_coverage(card_rec(names_only), goal="机器学习", min_score=0.2, min_prereq=0.1).ok
    assert graders.gold_coverage(card_rec(in_summary), goal="机器学习", min_score=0.2, min_prereq=0.1).ok
    assert "线性回归" not in "".join(graders.goal_names(card_rec(in_summary)))
    rec = card_rec(names_only)
    call = next(x for t in rec["turns"] for x in t["tools"] if x["name"] == "new_goal")
    rec["actions"] = [{"action_id": "a1", "content": {"blocks": [{"definition": "梯度下降，以及正则化"}]}}]
    call["action_id"] = "a1"
    assert {"梯度下降", "以及正则化"} <= set(graders.goal_text(rec))


def test_second_turn_may_not_make_another_goal():
    t = {"say": "x", "answer": "", "proposals": []}
    rec = {"turns": [{**t, "tools": []}, {**t, "tools": [{"name": "new_goal", "ok": True}]}]}
    assert not graders.turn_tool_not_called(rec, turn=2, names=("new_goal",)).ok
    assert graders.turn_tool_not_called(rec, turn=1, names=("new_goal",)).ok
