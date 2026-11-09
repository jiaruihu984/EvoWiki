"""Fixtures for the learning-path tests (no test functions here): goals built by hand as
`IntakeResult`s, a `contracts.KView` with bands set directly (fakes_k.FakeK), and a JsonLLM over
the real Gateway with a ScriptedProvider (llm_script.Script: one attempt per input, never the
network)."""

from pathlib import Path

from pydantic import BaseModel

from evowiki.goals.contracts import Aspect, Band
from evowiki.goals.intake import IntakeConcept, IntakeRelation, IntakeResult
from evowiki.goals.observe import JsonLLM, Role
from evowiki.llm.models import LLMRequest
from tests.goals.fakes_k import FakeK
from tests.goals.llm_script import Script

ALL = tuple(Aspect)   # 6 aspects → 6 cells per concept


def concept(cid: str, name: str, *prereqs: str, aspects: tuple[Aspect, ...] = ALL,
            target: bool = False, reused: bool = False) -> IntakeConcept:
    return IntakeConcept(id=cid, name=name, aspects=aspects, prereqs=prereqs, target=target,
                         reused=reused)


def goal(*concepts: IntakeConcept, text: str = "我想学 Transformer") -> IntakeResult:
    targets = tuple(c.id for c in concepts if c.target) or (concepts[0].id,)
    return IntakeResult(goal=text, title=text, concepts=concepts, targets=targets,
                        observations=(), dropped=(), resolutions=())


def k_for(g: IntakeResult, bands: dict[str, Band] | None = None, *,
          default: Band = Band.GAP) -> FakeK:
    return FakeK({c.id: c.aspects for c in g.concepts}, bands, default=default)


def transformer(text: str = "我想学 Transformer") -> IntakeResult:
    """5 concepts × 6 aspects = 30 cells. vec, soft → dot(vec) → attn(dot, soft) → tf(attn)."""
    return goal(concept("vec", "向量与矩阵"), concept("soft", "softmax"),
                concept("dot", "点积相似度", "vec"), concept("attn", "注意力", "dot", "soft"),
                concept("tf", "Transformer", "attn", target=True), text=text)


def verified(g: IntakeResult) -> IntakeResult:
    """`g` with every concept prerequisite also a VERIFIED prerequisite relation (from the graph):
    all its edges are hard (relation layers: only a verified prerequisite is hard)."""
    rels = tuple(IntakeRelation(kind="prerequisite", src=p, dst=c.id, verified=True)
                 for c in g.concepts for p in c.prereqs)
    return g.model_copy(update={"relations": (*g.relations, *rels)})


GOOD = {"pages": [
    {"concepts": ["vec", "soft"], "objective": "学完这页你能用矩阵乘法和 softmax 把一组分数变成权重"},
    {"concepts": ["dot", "attn"], "objective": "学完这页你能手算一个小例子的注意力输出"},
    {"concepts": ["tf"], "objective": "学完这页你能说清 Transformer 一层里数据怎么流动"},
]}


def pages(*groups: list[str]) -> dict:
    return {"pages": [{"concepts": g, "objective": f"学完这页你能用上{'、'.join(g)}"}
                      for g in groups]}


def json_llm(script: Script, tmp_path: Path) -> JsonLLM:
    gw = script.gateway(tmp_path)

    def call[T: BaseModel](role: Role, purpose: str, prompt: str, schema: type[T]) -> T:
        req = LLMRequest(purpose=purpose, prompt_version="test", subject=purpose, system="",
                         prompt=prompt)
        return gw.call(role, req, schema)[0]

    return call
