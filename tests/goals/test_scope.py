"""A concept's scope (goals.scope, audit D09): the path is planned inside each concept's scope (the
特征值概念 case: defined without the 计算过程 layer, it became a page about computing eigenvalues)."""

from evowiki.goals.contracts import Aspect
from evowiki.goals.intake import DATA_BEGIN, DATA_END
from evowiki.goals.path import PURPOSE, plan_path
from evowiki.goals.scope import concept_scope
from tests.goals.llm_script import Script
from tests.goals.test_path_fakes import concept, goal, json_llm, k_for

CONCEPT_ONLY = (Aspect.TERM, Aspect.NOTATION, Aspect.MECHANISM, Aspect.INTERPRET)
EIGEN_DEF = "介绍特征值与特征向量的定义、几何意义（变换后方向不变）和基本性质。"


def test_a_concept_without_the_procedure_layer_is_scoped_without_computation():
    line = concept_scope("特征值概念", EIGEN_DEF, CONCEPT_ONLY)
    assert line.startswith(f"特征值概念：{EIGEN_DEF}；讲的层面：术语 / 定义")
    assert line.endswith("不讲：用途 / 何时用、计算过程")


def test_the_path_prompt_carries_every_concept_scope_as_data(tmp_path):
    eig = concept("eig", "特征值概念", aspects=CONCEPT_ONLY, target=True)
    eig = eig.model_copy(update={"definition": EIGEN_DEF})
    g = goal(concept("mat", "矩阵乘法"), eig, text="系统学习主成分分析（PCA） · 数学基础")
    script = Script().push(PURPOSE, {"pages": [
        {"concepts": ["mat"], "objective": "学完这页你能算矩阵乘法"},
        {"concepts": ["eig"], "objective": "学完这页你能说清特征向量为什么方向不变"}]})
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    prompt = script.requests(PURPOSE)[0].prompt
    line = f"- eig {concept_scope('特征值概念', EIGEN_DEF, CONCEPT_ONLY)}"
    at = prompt.index(line)
    assert prompt.rfind(DATA_BEGIN, 0, at) > prompt.rfind(DATA_END, 0, at)   # fenced
    assert "「不讲」的层面不写进去" in prompt
    # the scope travels with the path to the page writer
    assert next(c for c in path.concepts if c.id == "eig").definition == EIGEN_DEF
