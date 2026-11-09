"""The 「我早会了」 confirm routes of the assistant: HTTP shape and error mapping (the logic is in
test_action_tools.py)."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from evowiki.assistant.contracts import KnownAnswerOut, KnownItemOut
from evowiki.bot.actions import ActionRefused
from evowiki.service.app import create_app

U1 = {"X-EvoWiki-User": "u1"}


class FakeAssistant:
    def __init__(self):
        self.calls = []
        self.error = None

    def known_item(self, uid, action_id):
        return KnownItemOut(stem="题", options=("a", "b"))

    def known_answer(self, uid, action_id, choice):
        if self.error:
            raise self.error
        return KnownAnswerOut(correct=False, correct_index=1, undone=True, message="撤回了")


def client():
    a = FakeAssistant()
    return TestClient(create_app(SimpleNamespace(assistant=a)), base_url="http://127.0.0.1"), a


def test_known_confirm_routes_never_send_the_answer():
    c, a = client()
    r = c.get("/assistant/actions/act1/confirm", headers=U1)
    assert r.status_code == 200 and set(r.json()) == {"stem", "options"}
    ok = c.post("/assistant/actions/act1/confirm", json={"choice": 0}, headers=U1)
    assert ok.json()["undone"] is True and ok.json()["message"] == "撤回了"
    a.error = ActionRefused("这道确认题已经答过了")
    assert c.post("/assistant/actions/act1/confirm", json={"choice": 0},
                  headers=U1).status_code == 409
