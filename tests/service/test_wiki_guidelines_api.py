"""Task 11 routes: the learner's own guidelines (list / add / delete)."""

import pytest

from evowiki.wikiapp.wiki_schemas import GuidelineOut, GuidelinesOut
from tests.service.conftest import H, parse_sse, signup
from tests.service.test_wiki_api import new_page, organize
from tests.wikiapp.script import Script
from tests.wikiapp.test_organize import script_page


@pytest.fixture
def wiki(services, client):
    script = Script()
    services.gateway = script.gateway()
    return client, script


def listed(client) -> GuidelinesOut:
    r = client.get("/wiki/guidelines", headers=H("u1"))
    assert r.status_code == 200, r.text
    return GuidelinesOut.model_validate(r.json())


def test_add_list_delete(wiki):
    client, _ = wiki
    signup(client, "u1")
    assert listed(client).items == []
    r = client.post("/wiki/guidelines", headers=H("u1"), json={"text": "多用图"})
    assert r.status_code == 201, r.text
    g = GuidelineOut.model_validate(r.json())
    assert (g.text, g.source, g.evidence) == ("多用图", "user", None)
    assert listed(client).items == [g]
    assert client.delete(f"/wiki/guidelines/{g.guideline_id}", headers=H("u1")).status_code == 204
    assert listed(client).items == []
    assert client.delete(f"/wiki/guidelines/{g.guideline_id}", headers=H("u1")).status_code == 404
    assert client.post("/wiki/guidelines", headers=H("u1"), json={"proposal_id": "p"}).status_code == 422


def test_added_guideline_reaches_the_next_outline_prompt(wiki, services):
    client, s = wiki
    pid = new_page(wiki)
    r = client.post("/wiki/guidelines", headers=H("u1"), json={"text": "先给一个具体例子再讲定义"})
    assert r.status_code == 201
    script_page(s)
    assert parse_sse(organize(client, pid).text)[-1][0] == "done"
    from evowiki.deep.agent import write_prompt
    from evowiki.deep.tools import Workspace
    inp = services.page_writing.writer.inputs[-1]          # the writer's outline part
    assert "先给一个具体例子再讲定义" in write_prompt(inp, (), Workspace(inp.page))


def test_unknown_user_is_404(wiki):
    client, _ = wiki
    assert client.get("/wiki/guidelines", headers=H("ghost")).status_code == 404
