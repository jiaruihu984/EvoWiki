"""A remedial reminder reaches the page's real assistant session with a fake provider."""

from datetime import timedelta

from evowiki.belief.aspects import DefineConcept, Observe
from evowiki.bot.pipeline import dispatch, scan_events
from evowiki.goals.contracts import Aspect, Observation
from evowiki.llm.chat import ChatTurn
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.service.deps import build_assistant
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.service.conftest import H, parse_sse, signup
from tests.service.test_routes_bot import add_page


def test_wrong_answers_card_to_page_assistant_over_http(client, services, tmp_path):
    signup(client)
    add_page(services, "p1")
    now, ids = services.clock(), services.new_id
    repo = PgRepo(services.pool)
    repo.commit(repo.load("u1"), Commit(aspect=[
        DefineConcept(event_id=ids("ae"), ts=now, concept_id="c1", aspects=(Aspect.INTERPRET,)),
        *(Observe(event_id=ids("ae"), ts=now - timedelta(minutes=3 - i),
                  observation=Observation(cell="c1.interpret", polarity="-", strength=0.8,
                                          kind="check", evidence_ref=f"check:p1:s{i}"))
          for i in range(3))]))
    before = repo.load("u1").aspect_seq
    [tid] = scan_events(services.pool, "u1", now=now, new_id=ids)
    assert dispatch(services.pool, tid, now=now, new_id=ids).outcome == "pass"
    response = client.get("/wiki/bot/suggestions", headers=H("u1"))
    assert response.status_code == 200, response.text
    [card] = response.json()
    assert card["kind"] == "wrong_answers" and card["status"] == "shown"
    action = card["action"]
    assert action["type"] == "open_page" and action["page_id"] == "p1"
    response = client.post(f"/wiki/bot/suggestions/{card['suggestion_id']}/react",
                           headers=H("u1"), json={"reaction": "accepted"})
    assert response.status_code == 200, response.text
    # The same action consumed by the plugin: the target page's own conversation.
    seen = []

    def reply(model, request):
        seen.append(request)
        return ChatTurn(text="先换一种说法。练习：请用自己的话解释结果。")

    ref = ModelRef(provider="scripted", model="remedial-test")
    gw = Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
                 CallLog(tmp_path / "calls.jsonl"), mode="record",
                 providers={"scripted": ScriptedChatProvider("scripted", reply)})
    services.assistant = build_assistant(services.pool, gw, services.ctx.content,
                                         services.embedder, services.clock, ids)
    response = client.patch("/usage/settings", headers=H("u1"), json={"confirm_budget": True})
    assert response.status_code == 200, response.text
    response = client.get(f"/wiki/pages/{action['page_id']}", headers=H("u1"))
    assert response.status_code == 200, response.text
    response = client.post("/assistant/sessions", headers=H("u1"),
                           json={"page_id": action["page_id"]})
    assert response.status_code == 200, response.text
    sid = response.json()["session_id"]
    response = client.post(f"/assistant/sessions/{sid}/messages", headers=H("u1"),
                           json={"text": action["ask"]})
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    assert events[-1][0] == "done", events
    session = client.get(f"/assistant/sessions/{sid}", headers=H("u1")).json()
    assert session["page_id"] == "p1"
    assert session["messages"][0]["text"] == action["ask"]
    assert "练习" in session["messages"][1]["text"] and len(seen) == 1
    assert repo.load("u1").aspect_seq == before
