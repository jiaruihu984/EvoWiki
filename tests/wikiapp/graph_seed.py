"""A small node graph for the split / merge / maintain tests: 线性代数 with two parts (矩阵乘法 and
行列式), 向量 a prerequisite of 矩阵乘法, every node a K concept (written through graph_events, as
the goal flow writes it). Ids are long and odd so a test can assert none reaches a prompt."""

from datetime import UTC, datetime, timedelta

from evowiki.belief.aspects import AddNote, Observe
from evowiki.goals.contracts import Aspect, Observation, cell_id
from evowiki.nodes import NodeGraph, NodeRec, Relation
from evowiki.wikiapp.graph_sync import graph_events
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.state import replay

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
LA, MM, DET, VEC = "nd_linalg_7q", "nd_matmul_3x", "nd_determ_5k", "nd_vector_9z"
IDS = (LA, MM, DET, VEC)


class Ids:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n:04d}"


def graph() -> NodeGraph:
    return NodeGraph.of(
        [NodeRec(id=LA, name="线性代数", definition="研究向量和线性变换"),
         NodeRec(id=MM, name="矩阵乘法", definition="两个矩阵相乘得到新矩阵"),
         NodeRec(id=DET, name="行列式", definition="方阵对应的一个数"),
         NodeRec(id=VEC, name="向量", definition="有方向和大小的量")],
        [Relation(kind="part_of", src=MM, dst=LA, phrase="线代里最常用的运算"),
         Relation(kind="part_of", src=DET, dst=LA),
         Relation(kind="prerequisite", src=VEC, dst=MM, phrase="矩阵乘法按向量点积算"),
         Relation(kind="related", src=MM, dst=DET, phrase="都是矩阵的运算")])


def seed(ids: Ids | None = None) -> tuple[MemoryRepo, Ids]:
    ids = ids or Ids()
    repo = MemoryRepo(["u1"])
    snap = repo.load("u1")
    aspect, wiki = graph_events(snap.state, snap.k, graph(), "goal_nodes", now=NOW, new_id=ids,
                                aspects={MM: (Aspect.TERM, Aspect.PROCEDURE)})
    repo.commit(snap, Commit(aspect=list(aspect), wiki=wiki))
    return repo, ids


def observe(repo: MemoryRepo, ids: Ids, node: str, polarity: str, *, aspect=Aspect.TERM,
            kind="quiz", strength=1.0, at: datetime = NOW, label: str | None = None) -> None:
    snap = repo.load("u1")
    cell = cell_id(node, aspect)
    eid = ids("aev")
    evs = [Observe(event_id=eid, ts=at, observation=Observation(
        cell=cell, polarity=polarity, strength=strength, kind=kind,  # type: ignore[arg-type]
        evidence_ref=f"quiz:{eid}"))]
    if label:
        evs.append(AddNote(event_id=ids("aev"), ts=at, cell=cell, text=label[:240],
                           evidence_ref=f"quiz:{eid}", label=label))
    repo.commit(snap, Commit(aspect=evs))


def mixed_answers(repo: MemoryRepo, ids: Ids, node: str = MM) -> None:
    observe(repo, ids, node, "+", at=NOW + timedelta(hours=1),
            label="题目：2×3 乘 3×2 得几行几列？；选了「2 行 2 列」，答对了")
    observe(repo, ids, node, "-", aspect=Aspect.PROCEDURE, at=NOW + timedelta(hours=2),
            label="题目：AB 和 BA 一样吗？；选了「一样」，答错了")


def replays_equal(repo: MemoryRepo, snap) -> bool:
    again = repo.load("u1")
    return (replay(repo.wiki_log("u1")).to_json() == snap.state.to_json()
            and again.state.to_json() == snap.state.to_json()
            and again.k.to_json() == snap.k.to_json())
