"""POST /assistant/sessions/{sid}/messages/{mid}/rating (助手回答评价): 👍 / 👎 / clear on one
answer, stored as `rating` entries in the session log, shown on GET session. Scripted model only."""

from evowiki.llm.chat import ChatTurn
from tests.service.test_assistant_api import U1, U2, events, make


def answered(tmp_path):
    """A client with one answered turn: (client, session id, user message id, answer id)."""
    c = make(tmp_path, [ChatTurn(text="答案")])
    sid = c.post("/assistant/sessions", json={"page_id": "p1"}, headers=U1).json()["session_id"]
    evs = events(c.post(f"/assistant/sessions/{sid}/messages", json={"text": "问"}, headers=U1))
    answer = next(d for e, d in evs if e == "done")["message_id"]
    msgs = c.get(f"/assistant/sessions/{sid}", headers=U1).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["message_id"] == answer
    return c, sid, msgs[0]["message_id"], answer


def rate(c, sid, mid, body, headers=U1):
    return c.post(f"/assistant/sessions/{sid}/messages/{mid}/rating", json=body, headers=headers)


def shown(c, sid):
    return c.get(f"/assistant/sessions/{sid}", headers=U1).json()["messages"][1]["rating"]


def test_up_down_and_clear_latest_wins_and_get_session_shows_it(tmp_path):
    c, sid, _, answer = answered(tmp_path)
    assert shown(c, sid) is None
    r = rate(c, sid, answer, {"value": "up"})
    assert r.status_code == 200
    assert r.json() == {"message_id": answer, "value": "up", "chips": [], "note": ""}
    assert shown(c, sid)["value"] == "up"
    r = rate(c, sid, answer, {"value": "down", "chips": ["太啰嗦", "不对", "太啰嗦"],
                              "note": "  说了 三遍 " + "长" * 300})
    out = r.json()
    assert out["value"] == "down" and out["chips"] == ["太啰嗦", "不对"]      # duplicates dropped
    assert out["note"].startswith("说了 三遍 长") and len(out["note"]) == 200   # clipped
    assert shown(c, sid) == out
    r = rate(c, sid, answer, {"value": None, "note": "不要了"})
    assert r.json() == {"message_id": answer, "value": None, "chips": [], "note": ""}
    assert shown(c, sid) is None
    user_msg = c.get(f"/assistant/sessions/{sid}", headers=U1).json()["messages"][0]
    assert user_msg["rating"] is None                                   # only answers carry one


def test_chips_only_with_down_and_bad_values_are_422(tmp_path):
    c, sid, _, answer = answered(tmp_path)
    assert rate(c, sid, answer, {"value": "up", "chips": ["不对"]}).status_code == 422
    assert rate(c, sid, answer, {"value": None, "chips": ["其他"]}).status_code == 422
    assert rate(c, sid, answer, {"value": "down", "chips": ["很好"]}).status_code == 422
    assert rate(c, sid, answer, {"value": "meh"}).status_code == 422
    assert rate(c, sid, answer, {}).status_code == 422                  # value is required
    assert shown(c, sid) is None


def test_someone_elses_session_and_unknown_messages_are_404(tmp_path):
    c, sid, user_mid, answer = answered(tmp_path)
    assert rate(c, sid, answer, {"value": "down"}, headers=U2).status_code == 404
    assert rate(c, "nope", answer, {"value": "down"}).status_code == 404
    assert rate(c, sid, "nope", {"value": "down"}).status_code == 404
    assert rate(c, sid, user_mid, {"value": "down"}).status_code == 404   # a question, not an answer
    assert shown(c, sid) is None
