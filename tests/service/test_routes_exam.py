"""真题 over HTTP and Postgres: GET /wiki/pages/{id}/exam (quiz asset graded by the existing
results route), POST /wiki/exam/items/{id}/self, and SourceDetail.exam. No LLM."""

from evowiki.deep.store import PgAssetStore
from evowiki.sources import exam_store, store
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.wiki_schemas import ExamSelfOut, PageExamOut
from tests.service.conftest import H
from tests.service.test_wiki_api import organized_page, wiki  # noqa: F401 — fixture
from tests.sources.test_exam_store import _item
from tests.sources.test_store import NOW, _src


def _seed(services, pid, *, exam_use: bool = True):
    snap = PgRepo(services.pool).load("u1")
    node = snap.state.pages[pid].concepts[0]
    cell = f"{node}.{min(snap.k.aspects(node))}"
    items = [_item("i1", 0, node_id=node, cell=None),
             _item("i2", 1, kind="open", options=(), answer="原文答案", node_id=node,
                   cell=cell),
             _item("i3", 2, answer=None, has_key=False, node_id=node)]
    with services.pool.connection() as conn:
        store.insert_source(conn, _src("s1", "u1", sha="a", title="往年题", status="ready"),
                            b"x", "application/pdf")
        if exam_use:          # the practice reads need the material's exam use
            store.add_use(conn, "u1", "s1", "exam", now=NOW)
        exam_store.replace_items(conn, "u1", "s1", items, gen=1, now=NOW)


def test_page_exam_route_and_results(wiki, services):  # noqa: F811
    client, _ = wiki
    pid = organized_page(wiki)
    _seed(services, pid)
    r = client.get(f"/wiki/pages/{pid}/exam", headers=H("u1"))
    assert r.status_code == 200, r.text
    out = PageExamOut.model_validate(r.json())
    assert out.quiz_count == 1 and out.asset_id
    assert [o.item_id for o in out.open_items] == ["i2", "i3"] and out.sources == ["往年题"]
    again = PageExamOut.model_validate(client.get(f"/wiki/pages/{pid}/exam", headers=H("u1")).json())
    assert again.asset_id == out.asset_id                       # reused, not stored twice
    stored = PgAssetStore(services.pool).get("u1", out.asset_id)
    assert stored is not None and stored.meta.kind == "evoui" and stored.job_id.startswith("exam:")
    r = client.post(f"/wiki/assets/{out.asset_id}/results", headers=H("u1"),
                    json={"answers": [{"quiz": 0, "question": 0, "chosen": 1}]})
    assert r.status_code == 200, r.text
    assert (r.json()["right"], r.json()["total"]) == (1, 1)
    assert client.get("/wiki/pages/pg_nope/exam", headers=H("u1")).status_code == 404


def test_self_report_route(wiki, services):  # noqa: F811
    client, _ = wiki
    pid = organized_page(wiki)
    _seed(services, pid)

    def post(item, result):
        return client.post(f"/wiki/exam/items/{item}/self", headers=H("u1"),
                           json={"result": result})

    r = post("i2", "wrong")
    assert r.status_code == 200, r.text
    assert ExamSelfOut.model_validate(r.json()).status == "k"
    log = PgRepo(services.pool).aspect_events("u1")
    assert any(getattr(e, "observation", None) is not None
               and e.observation.evidence_ref.startswith("exam:i2:") for e in log)
    assert post("i2", "right").json()["status"] == "duplicate"
    assert post("i3", "done").json()["status"] == "done"
    r = post("i3", "right")
    assert r.status_code == 422 and r.json()["detail"] == "这道题没有答案，只能标做完"
    assert post("nope", "done").status_code == 404
    assert post("i2", "maybe").status_code == 422
    out = PageExamOut.model_validate(client.get(f"/wiki/pages/{pid}/exam", headers=H("u1")).json())
    assert {o.item_id: o.done for o in out.open_items} == {"i2": True, "i3": True}


def test_source_detail_carries_exam_summary(wiki, services):  # noqa: F811
    client, _ = wiki
    pid = organized_page(wiki)
    _seed(services, pid, exam_use=False)
    got = client.get("/wiki/sources/s1", headers=H("u1"))
    assert got.status_code == 200, got.text
    assert got.json()["exam"] is None
    with services.pool.connection() as conn:
        store.add_use(conn, "u1", "s1", "exam", now=NOW)
        store.set_progress(conn, "u1", "s1", {"exam": {"state": "done", "items": 3}})
    exam = client.get("/wiki/sources/s1", headers=H("u1")).json()["exam"]
    assert exam == {"state": "done", "items": 3, "attached": 3, "unattached": 0, "pending": 0,
                    "unreadable": 0, "incomplete": 0, "unattached_items": [], "detail": None}
