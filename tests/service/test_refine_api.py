"""The page writer over HTTP and Postgres (migration 0022): organize written by the deep agent itself
(single writer), the explicit whole-page rewrite (202 + background run, latest job), assets with their
CSP / sandbox headers, the per-page limit setting."""

import pytest
from fastapi.testclient import TestClient

from evowiki.deep.contracts import AssetMeta, RefineJobOut
from evowiki.deep.render import ASSET_CSP
from evowiki.deep.store import PgAssetStore, PgJobStore
from evowiki.service.app import create_app
from evowiki.wikiapp.refine_service import DeepService
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.wiki_schemas import PageOut, SettingsIO
from tests.deep.fakes import DeepScript, FakeChecker, library, simple_round
from tests.runtime.helpers import T0
from tests.service.conftest import H, parse_sse
from tests.service.test_wiki_api import new_page, organize, organized_page
from tests.wikiapp.script import Script
from tests.wikiapp.test_organize import OUTLINE, add_page


@pytest.fixture
def wiki(services, client):
    script = Script()
    services.gateway = script.gateway()
    return client, script


@pytest.fixture
def deep(services, pool):
    ds = DeepScript()
    services.deep = DeepService(repo=PgRepo(pool), jobs=PgJobStore(pool),
                                assets=PgAssetStore(pool), specs=WikiSpecSource(PgRepo(pool)),
                                checker=FakeChecker(), llm=ds.llm(), clock=lambda: T0,
                                new_id=services.new_id, library=library())
    return ds


def plain_page(wiki, services) -> str:
    """An organized page (written by the scripted page writer of the fixture)."""
    return organized_page(wiki)


def test_post_refine_runs_in_the_background_and_serves_the_asset(wiki, services, deep):
    client, _ = wiki
    pid = plain_page(wiki, services)
    deep.plan().write(*simple_round(with_interactive=True))
    r = client.post(f"/wiki/pages/{pid}/refine", headers=H("u1"))
    assert r.status_code == 202, r.text
    started = RefineJobOut.model_validate(r.json())
    assert started.job.stage == "queued" and started.job.trigger == "user"

    r = client.get(f"/wiki/pages/{pid}/refine", headers=H("u1"))
    out = RefineJobOut.model_validate(r.json())
    assert out.job.job_id == started.job.job_id and out.job.stage == "done", out.job
    [meta] = out.assets
    assert meta.interactive_id == "pair-demo" and meta.check.ok

    r = client.get(f"/wiki/assets/{meta.asset_id}", headers=H("u1"))
    assert r.status_code == 200 and r.text.startswith("<!doctype html>")
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == ASSET_CSP
    assert "sandbox allow-scripts" in ASSET_CSP and "connect-src 'none'" in ASSET_CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    r = client.get(f"/wiki/assets/{meta.asset_id}/meta", headers=H("u1"))
    assert AssetMeta.model_validate(r.json()) == meta
    assert client.get(f"/wiki/assets/{meta.asset_id}", headers=H("u2")).status_code == 404
    assert client.get("/wiki/assets/ia_nope", headers=H("u1")).status_code == 404

    page = PageOut.model_validate(client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json())
    assert page.writer.model == "anthropic:claude-sonnet-5-5"
    body = next(s for s in page.sections if s.id == "s2").body
    assert f"src: assets/{meta.asset_id}.html" in body


def test_a_run_that_does_not_start_ends_its_queued_job_failed(wiki, services, deep):
    client, _ = wiki
    pid = plain_page(wiki, services)

    class Broken:
        store = services.runs.store

        def start(self, *a, **k):
            raise RuntimeError("DBOS is not launched")

    services.runs = Broken()
    with pytest.raises(RuntimeError, match="not launched"):
        TestClient(create_app(services), raise_server_exceptions=True, base_url="http://127.0.0.1").post(
            f"/wiki/pages/{pid}/refine", headers=H("u1"))
    out = RefineJobOut.model_validate(client.get(f"/wiki/pages/{pid}/refine",
                                                 headers=H("u1")).json())
    assert out.job.stage == "failed" and out.job.failure == "internal"
    assert services.deep.running("u1", pid) is None        # does not block the next refine


def test_refine_errors(wiki, services, deep):
    client, _ = wiki
    pid = plain_page(wiki, services)
    assert client.post("/wiki/pages/nope/refine", headers=H("u1")).status_code == 404
    assert client.get("/wiki/pages/nope/refine", headers=H("u1")).status_code == 404
    empty = RefineJobOut.model_validate(client.get(f"/wiki/pages/{pid}/refine",
                                                   headers=H("u1")).json())
    assert empty.job is None
    unorganized = add_page(PgRepo(services.pool), services.new_id)
    assert client.post(f"/wiki/pages/{unorganized}/refine", headers=H("u1")).status_code == 409
    services.deep = None
    assert client.post(f"/wiki/pages/{pid}/refine", headers=H("u1")).status_code == 503


def test_organize_is_written_by_the_deep_agent_and_streamed(wiki, services, deep):
    """No fake writer: the route builds the page writer from the deep service; every section
    streams as the agent writes it, then the page is committed once with its writer mark."""
    from tests.deep.fakes import finish, section, turn
    client, _ = wiki
    services.page_writing = None
    pid = new_page(wiki)
    deep.plan(outline=OUTLINE).write(
        turn(section("s1", role="organizer", depth="quick", heading="从你会的出发")),
                      turn(section("s2")),
                      turn(section("s3", role="recap", depth="quick", heading="标准差提醒")),
                      turn(finish()))
    events = parse_sse(organize(client, pid).text)
    # one planning event first, then the writer outlines (plan_page) and writes the page
    planning = [d for e, d in events if e == "planning"]
    assert planning and [e for e, _ in events[:len(planning)]] == ["planning"] * len(planning)
    events = events[len(planning):]
    assert [e for e, _ in events] == ["outline", "writer", "section_delta", "section_done",
                                      "section_delta", "section_done", "section_delta",
                                      "section_done", "done"]
    assert events[1][1] == {"model": "anthropic:claude-sonnet-5-5", "fallback": False, "reason": ""}
    assert [d["section_id"] for e, d in events if e == "section_delta"] == ["s1", "s2", "s3"]
    done = events[-1][1]
    assert "refine_queued" not in done and done["model"] == "anthropic:claude-sonnet-5-5"
    assert done["cost_usd"] > 0 and not done["fallback"]
    page = PageOut.model_validate(client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json())
    assert page.writer.cost_usd == done["cost_usd"] and page.writer.fallback_reason is None
    assert next(x for x in page.sections if x.id == "s2").body == "s2 精修正文：先从印章讲起。"
    assert client.get(f"/wiki/pages/{pid}/refine", headers=H("u1")).json()["job"] is None
    assert deep.pending() == {}


def test_page_limit_setting_round_trip(wiki):
    client, _ = wiki
    new_page(wiki)
    got = SettingsIO.model_validate(client.get("/wiki/settings", headers=H("u1")).json())
    assert got.page_limit_usd is None                          # no per-page cap by default
    r = client.put("/wiki/settings", headers=H("u1"), json={"page_limit_usd": 1.0})
    assert SettingsIO.model_validate(r.json()).page_limit_usd == 1.0
    r = client.put("/wiki/settings", headers=H("u1"), json={"review_per_day": 7})
    assert SettingsIO.model_validate(r.json()).page_limit_usd == 1.0   # kept
    r = client.put("/wiki/settings", headers=H("u1"), json={"refine": {"auto": "all"}})
    assert r.status_code == 422                                        # an old field: refused
    r = client.put("/wiki/settings", headers=H("u1"), json={"page_limit_usd": 9})
    assert r.status_code == 200 and SettingsIO.model_validate(r.json()).page_limit_usd == 9   # no upper bound
    r = client.put("/wiki/settings", headers=H("u1"), json={"page_limit_usd": 0.01})
    assert r.status_code == 422


def test_an_evoui_block_asset_is_json_or_its_harness_page(wiki, services, deep):
    from tests.deep.test_evoui_parity import BASE
    from tests.deep.test_service import _block_round
    client, _ = wiki
    pid = plain_page(wiki, services)
    deep.plan().write(*_block_round())
    assert client.post(f"/wiki/pages/{pid}/refine", headers=H("u1")).status_code == 202
    out = RefineJobOut.model_validate(client.get(f"/wiki/pages/{pid}/refine", headers=H("u1")).json())
    [meta] = out.assets
    assert meta.kind == "evoui" and meta.interactive_id == "proj"

    r = client.get(f"/wiki/assets/{meta.asset_id}", headers=H("u1"))         # Pg kept the kind
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json()["title"] == BASE["title"]
    r = client.get(f"/wiki/assets/{meta.asset_id}?view=html", headers=H("u1"))
    assert r.headers["content-type"].startswith("text/html") and 'id="evoui-block"' in r.text
    assert r.headers["content-security-policy"] == ASSET_CSP
    page = PageOut.model_validate(client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json())
    assert "```evoui\n" in next(s for s in page.sections if s.id == "s2").body

    for name, kind in (("evoui.js", "javascript"), ("evoui.css", "text/css"), ("spec.md", "markdown")):
        r = client.get(f"/wiki/evoui/{name}")
        assert r.status_code == 200 and kind in r.headers["content-type"], (name, r.headers)
    assert client.get("/wiki/evoui/secret.py").status_code in (404, 422)     # only the three files


def test_an_image_asset_is_stored_on_postgres_and_served_as_its_base64(wiki, services, deep):
    """Migration 0076: wiki_asset.kind takes "image"; GET /wiki/assets/{id} is the WebP as base64 text."""
    import base64

    from tests.deep.fakes import call, finish, section, turn
    from tests.deep.test_images import FakeImages
    client, _ = wiki
    pid = plain_page(wiki, services)
    services.deep.images = FakeImages()
    deep.plan().write(
        turn(call("find_image", 1, query="human heart", purpose="看清四个腔")),
        turn(section("s2", 1, body="先看图。\n\n{{image:heart}}"),
             call("place_image", 1, candidate="img1", image_id="heart", section_id="s2", alt="心脏剖面")),
        turn(finish()))
    assert client.post(f"/wiki/pages/{pid}/refine", headers=H("u1")).status_code == 202
    out = RefineJobOut.model_validate(client.get(f"/wiki/pages/{pid}/refine", headers=H("u1")).json())
    assert out.job.stage == "done", out.job
    [meta] = out.assets
    assert meta.kind == "image" and meta.interactive_id == "heart"
    r = client.get(f"/wiki/assets/{meta.asset_id}", headers=H("u1"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert base64.b64decode(r.text)[8:12] == b"WEBP"
    page = PageOut.model_validate(client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json())
    assert f"```evowiki-image\nasset: {meta.asset_id}\n" in next(s for s in page.sections if s.id == "s2").body
