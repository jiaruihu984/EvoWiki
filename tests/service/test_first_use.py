"""A new personal wiki: real database/lifecycle/routes, scripted models, no paid calls."""

from fastapi.testclient import TestClient

from evowiki.service.app import create_app
from evowiki.service.deps import build_services
from evowiki.wikiapp.repo import PgRepo
from tests.runtime.helpers import T0, FixedClock
from tests.service.conftest import H, parse_sse
from tests.service.test_wiki_api import fb, new_page, organize, script_page
from tests.wikiapp.fake_writer import writing
from tests.wikiapp.script import Script


def test_first_learning_page_feedback_and_restart_on_empty_database(db_url):
    env = {"EVOWIKI_DATABASE_URL": db_url, "EVOWIKI_EMBEDDER": "hash"}
    script = Script()
    svc = build_services(env, clock=FixedClock(T0))
    try:
        assert not svc.ctx.graph.concept_ids
        svc.gateway = script.gateway()
        svc.page_writing = writing(PgRepo(svc.pool), script)
        with TestClient(create_app(svc), base_url="http://127.0.0.1") as client:
            pid = new_page((client, script))
            script_page(script)
            assert parse_sse(organize(client, pid).text)[-1][0] == "done"
            response = fb(client, pid, "s2", "know")
            assert response.status_code == 200, response.text
            page = client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json()
            assert next(s for s in page["sections"] if s["id"] == "s2")["folded"]
    finally:
        svc.pool.close()

    restarted = build_services(env, clock=FixedClock(T0))
    try:
        with TestClient(create_app(restarted), base_url="http://127.0.0.1") as client:
            assert client.get("/me", headers=H("u1")).status_code == 200
            page = client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json()
            assert next(s for s in page["sections"] if s["id"] == "s2")["folded"]
    finally:
        restarted.pool.close()
