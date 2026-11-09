"""User ruling 2026-10-24 (「怎么进入了还问啊，我都说了我不会了」): a block the opening diagnosis
decided *teach* because it failed there (不会/没学过, 不知道, wrong) passes that decision to every point
under it, the points its lazy split makes on 进入 included: entering it asks nothing (no
goals.probe call) and plans its pages at once, every point teach.

The data is the real case (goal 「复杂网络中的渗流」, dev DB user `me`, read-only dump of its wiki and
aspect events up to the end of the opening diagnosis: data/percolation_opening.json, the user id
replaced): seven blocks, 概率与生成函数 answered right, the six others 不会/没学过 and then 不知道 on
each block's question (the old probe asked them; probe §Failed below no longer does)."""

import json
import re
from pathlib import Path

from evowiki.belief.aspects import AspectEventAdapter
from evowiki.wikiapp.events import WikiEvent
from evowiki.wikiapp.goal import enter_region
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import MemoryRepo
from tests.wikiapp.goal_script import PROBE
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids

DATA = Path(__file__).parent / "data" / "percolation_opening.json"
MAP = "map-190b2c942b43"
BLOCK = "c2"                                   # 网络表示与基础量
PARTS = ["节点与边", "度与度分布", "连通分量", "巨分量"]
_ROW = re.compile(r"^- (\S+) ([^（\n]+)", re.MULTILINE)


def _repo() -> MemoryRepo:
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    wiki = [WikiEvent.model_validate(e) for e in raw["wiki"]]
    aspect = [AspectEventAdapter.validate_python(e) for e in raw["aspect"]]
    return MemoryRepo.of_logs("u1", wiki, aspect)


def _path_reply(prompt: str) -> dict:
    rows = [(cid, name.strip()) for cid, name in _ROW.findall(prompt) if name.strip() in PARTS]
    return {"pages": [{"title": name, "concepts": [cid], "objective": f"学完这页你能讲清{name}"}
                      for cid, name in rows]}


def test_entering_a_block_answered_dont_asks_nothing_and_plans_every_point_teach():
    """On-demand decomposition (spec 2026-10-28): entering the block only splits it (no probe,
    no path); opening one of its points asks nothing either — the block's *teach* reaches the
    point — and plans that point's page, hung on the point."""
    repo, s = _repo(), Script()
    s.push("goals.intake", {"blocks": [{"name": n, "page": True} for n in PARTS],
                            "recommended": [{"from": "节点与边", "to": "度与度分布", "why": "度数的是边"}]})
    s.always("goals.path", _path_reply)
    opening = repo.load("u1").state.maps[MAP]
    assert opening.node(BLOCK).state == "collapsed" and not opening.children(BLOCK)
    ids = Ids()
    out = enter_region(repo, CountingGateway(s.gateway()), "u1", MAP, BLOCK, now=NOW, new_id=ids)
    assert s.calls(PROBE) == 0 and s.calls("goals.path") == 0     # entering only splits
    assert not out.open and out.path_id is None and out.map_id == MAP
    m = repo.load("u1").state.maps[MAP]
    children = [n for n in m.nodes if n.parent == BLOCK]
    assert [n.name for n in children] == PARTS
    point = children[0]
    out = enter_region(repo, CountingGateway(s.gateway()), "u1", MAP, point.id, now=NOW,
                       new_id=ids)
    assert s.calls(PROBE) == 0                                     # no 学习评估 on the point
    assert not out.open and out.path_id is not None
    final = out.draft.rounds[-1]                                   # done at once: no question
    assert final.done and final.questions == ()
    decided = {d.node: (d.decision, d.source) for d in final.decisions}
    assert decided[point.id] == ("teach", "opening")
    m = repo.load("u1").state.maps[MAP]
    assert m.node(point.id).path_id == out.path_id and m.node(BLOCK).path_id is None
