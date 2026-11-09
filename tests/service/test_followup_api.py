"""Wrong-answer follow-up over HTTP and Postgres: the routes, their status codes, today's
confirms and the claim / follow-up events surviving a reload."""

from evowiki.wikiapp.followup import PURPOSE
from evowiki.wikiapp.wiki_schemas import (
    CheckAnswerOut,
    CheckFollowupOut,
    ConfirmAnswerOut,
    ReviewTodayOut,
)
from tests.service.conftest import H
from tests.service.test_wiki_api import organized_page, wiki  # noqa: F401 — fixture
from tests.wikiapp.test_followup import NEW_Q

URL = "/wiki/pages/{pid}/checks/s4"


def test_followup_and_confirm_routes(wiki, services):  # noqa: F811
    client, s = wiki
    pid = organized_page(wiki)
    base = URL.format(pid=pid)
    r = client.post(base, headers=H("u1"), json={"choice": 1})            # key is 0: wrong
    ans = CheckAnswerOut.model_validate(r.json())
    assert ans.correct is False and ans.answer_ref == f"check:{pid}:s4"
    assert client.post(base + "/followup", headers=H("u1"),
                       json={"choice": "nope"}).status_code == 422

    gw, services.gateway = services.gateway, None                        # slip needs an LLM
    assert client.post(base + "/followup", headers=H("u1"),
                       json={"choice": "slip"}).status_code == 503
    services.gateway = gw
    s.push(PURPOSE, NEW_Q)
    r = client.post(base + "/followup", headers=H("u1"), json={"choice": "learned"})
    assert r.status_code == 200, r.text
    out = CheckFollowupOut.model_validate(r.json())
    assert out.status == "pending" and out.llm_calls == 1
    assert client.post(base + "/followup", headers=H("u1"),
                       json={"choice": "unclear"}).status_code == 409

    services.clock = lambda: out.confirm_due                             # now due
    today = ReviewTodayOut.model_validate(
        client.get("/wiki/review/today", headers=H("u1")).json())
    [card] = today.confirms
    assert card.followup_id == out.followup_id and card.check.stem == NEW_Q["stem"]
    r = client.post(f"/wiki/review/confirms/{out.followup_id}", headers=H("u1"),
                    json={"choice": 9})
    assert r.status_code == 422
    r = client.post(f"/wiki/review/confirms/{out.followup_id}", headers=H("u1"),
                    json={"choice": -1})
    res = ConfirmAnswerOut.model_validate(r.json())
    assert res.outcome == "refuted" and res.reteach is not None and res.signal_id
    assert client.post(f"/wiki/review/confirms/{out.followup_id}", headers=H("u1"),
                       json={"choice": 0}).status_code == 409
    assert client.post("/wiki/review/confirms/fu_nope", headers=H("u1"),
                       json={"choice": 0}).status_code == 404


def test_block_results_route(wiki, services):  # noqa: F811
    from evowiki.deep.store import PgAssetStore
    from evowiki.wikiapp.wiki_schemas import BlockResultsOut
    from tests.wikiapp.test_block_results import stored
    client, _ = wiki
    pid = organized_page(wiki)
    mem, aid = stored(pid)
    with services.pool.connection() as conn, conn.transaction():
        PgAssetStore(services.pool).writer(list(mem.rows.values()))(conn)
    r = client.post(f"/wiki/assets/{aid}/results", headers=H("u1"),
                    json={"answers": [{"quiz": 0, "question": 0, "chosen": 1}], "state": {"k": 1}})
    assert r.status_code == 200, r.text
    out = BlockResultsOut.model_validate(r.json())
    assert out.right == 0 and out.answers[0].answer_ref
    r = client.post(f"/wiki/pages/{pid}/checks/s2/followup", headers=H("u1"),
                    json={"choice": "unclear", "answer_ref": out.answers[0].answer_ref})
    assert r.status_code == 200, r.text
    assert client.post("/wiki/assets/ia_nope/results", headers=H("u1"),
                       json={"state": {}}).status_code == 404
    assert client.post(f"/wiki/assets/{aid}/results", headers=H("u1"), json={}).status_code == 422
