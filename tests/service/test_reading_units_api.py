"""/wiki/readings/* (reading-mode spec §3.5, plan stage 4 Task 4) over HTTP and Postgres with the
scripted model: open a unit (「看这一段」 once, its teaches links in the same commit, its page on
`start`), the stability ops (refusals 409) and a prerequisite found while reading. No paid call."""

import pytest

from evowiki.goals.reading import ReadingPlan, ReadingUnit, UnitSpan
from evowiki.goals.reading_unit import PURPOSE
from evowiki.sources import store
from evowiki.wikiapp.goal_sources import load_materials
from evowiki.wikiapp.reading import create_reading_goal_with_source
from evowiki.wikiapp.repo import PgRepo
from tests.service.conftest import H, signup
from tests.service.test_goal_sources_api import PAPER, add_source
from tests.wikiapp.goal_script import auto_probe
from tests.wikiapp.script import Script

OUT = {"teaches": [{"name": "新结构", "summary": "这篇提出的模型", "atomic": True}],
       "uses": [{"name": "编码器", "summary": "把输入变成向量", "atomic": True}],
       "focus": ["「新结构」指的是什么"]}


@pytest.fixture
def reading(services, client):
    s = Script()
    auto_probe(s)
    services.gateway = s.gateway()
    signup(client, "u1")
    sid = add_source(client, PAPER, title="一篇论文")
    toc = load_materials(services.pool, "u1", [sid])[0].toc
    body = [x for x in toc.sections if x.title != "参考文献"]
    units = tuple(ReadingUnit(unit_id=f"ru_{i}", order=i, title=x.title,
                              spans=(UnitSpan(section_id=x.id),))
                  for i, x in enumerate(body, start=1))
    plan = ReadingPlan(units=units, source_id=sid, title="一篇论文")
    res = create_reading_goal_with_source(PgRepo(services.pool), services.pool, None, "u1",
                                          "读懂《一篇论文》", (), plan, runs=services.sources,
                                          now=services.clock(), new_id=services.new_id)
    return client, s, services, res.reading_id, sid


def test_open_analyzes_once_writes_links_and_starts_the_page(reading):
    client, s, svc, rid, sid = reading
    s.push(PURPOSE, OUT)
    r = client.post(f"/wiki/readings/{rid}/units/ru_1/open", headers=H("u1"),
                    json={"start": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["llm_calls"] == 1 and out["unit"]["analyzed"]
    assert [t["name"] for t in out["unit"]["teaches"]] == ["新结构"]
    assert [(u["name"], u["on_map"]) for u in out["unit"]["uses"]] == [("编码器", True)]
    assert out["page_id"] and out["path_id"] == f"path-{rid}"
    assert out["unit"]["page_id"] == out["page_id"]
    assert out["unit"]["teaches"][0]["page_id"] == out["page_id"]
    teach = out["unit"]["teaches"][0]["id"]
    with svc.pool.connection() as conn:
        links = store.links_for_nodes(conn, "u1", [teach])
    assert [(k.role, k.by, k.source_id) for k in links] == [("teaches", "reading", sid)]
    again = client.post(f"/wiki/readings/{rid}/units/ru_1/open", headers=H("u1"))
    assert again.status_code == 200 and again.json()["llm_calls"] == 0
    assert s.calls(PURPOSE) == 1


def test_a_use_added_while_reading(reading):
    client, s, _, rid, _ = reading
    early = client.post(f"/wiki/readings/{rid}/units/ru_1/uses", headers=H("u1"),
                        json={"name": "注意力"})
    assert early.status_code == 409                         # not analyzed yet
    s.push(PURPOSE, OUT)
    client.post(f"/wiki/readings/{rid}/units/ru_1/open", headers=H("u1"))
    r = client.post(f"/wiki/readings/{rid}/units/ru_1/uses", headers=H("u1"),
                    json={"name": "注意力", "summary": "按相关程度加权"})
    assert r.status_code == 200, r.text
    assert [u["name"] for u in r.json()["uses"]] == ["编码器", "注意力"]
    assert client.post(f"/wiki/readings/{rid}/units/ru_1/uses", headers=H("u1"),
                       json={}).status_code == 422


def test_the_goal_shows_its_reading_with_locations_progress_and_coverage(reading):
    """GoalMapOut / GoalOut.reading (spec §5): units in order with their § locations and status,
    progress without skipped units; the sections the units read are covered (only 参考文献 is
    left), a teaches point is labelled 「在《T》的第 N 段里读」."""
    client, s, svc, rid, _ = reading
    mid = PgRepo(svc.pool).load("u1").state.readings[rid].map_id
    m = client.get(f"/wiki/maps/{mid}", headers=H("u1"))
    assert m.status_code == 200, m.text
    rd = m.json()["reading"]
    assert [u["unit_id"] for u in rd["units"]] == ["ru_1", "ru_2", "ru_3"]
    assert [u["location"] for u in rd["units"]] == ["摘要", "§1", "§2"]
    assert {u["status"] for u in rd["units"]} == {"not_started"}
    assert rd["progress"] == {"done": 0, "total": 3}
    s.push(PURPOSE, OUT)
    page = client.post(f"/wiki/readings/{rid}/units/ru_1/open", headers=H("u1"),
                       json={"start": True}).json()
    assert page["unit"]["status"] == "reading" and page["unit"]["location"] == "摘要"
    g = client.get("/wiki/graph", headers=H("u1")).json()
    goal = next(x for x in g["goals"] if x["map_id"] == mid)
    assert goal["reading"]["progress"] == {"done": 0, "total": 3}
    assert [u["status"] for u in goal["reading"]["units"]] == ["reading", "not_started",
                                                               "not_started"]
    assert goal["sources"][0]["uncovered"] == ["参考文献"]
    path = client.get(f"/wiki/paths/path-{rid}", headers=H("u1")).json()
    assert path["pages"][0]["sections"] == ["在《一篇论文》的第 1 段里读"]


def test_a_units_text_gone_from_the_material_is_a_plain_404(services, client):
    """M9: a span outside its section's paragraphs now is 404 with a learner-safe line (the
    section id stays in the log)."""
    from evowiki.goals.reading import span_of
    s = Script()
    auto_probe(s)
    services.gateway = s.gateway()
    signup(client, "u1")
    sid = add_source(client, "# 1 方法\n\n第一段。\n\n第二段。\n\n第三段。\n", title="短文")
    toc = load_materials(services.pool, "u1", [sid])[0].toc
    sec = toc.sections[0]
    plan = ReadingPlan(units=(ReadingUnit(unit_id="ru_1", order=1, title="后两段", spans=(
        span_of(sec.paras, sec.id, 0, len(sec.paras) - 1),)),), source_id=sid, title="短文")
    res = create_reading_goal_with_source(PgRepo(services.pool), services.pool, None, "u1",
                                          "读懂《短文》", (), plan, runs=services.sources,
                                          now=services.clock(), new_id=services.new_id)
    with services.pool.connection() as conn:
        store.update_section_text(conn, "u1", sid, sec.id, text="只剩一段。", tokens=3,
                                  read_by="text")
    r = client.post(f"/wiki/readings/{res.reading_id}/units/ru_1/open", headers=H("u1"))
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "这一段的原文在资料里找不到了，请重新整理这份资料后再试"
    assert sec.id not in r.text and s.calls(PURPOSE) == 0
