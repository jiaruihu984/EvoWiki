from tests.service.conftest import SURVEY, H, signup


def test_create_user_maps_survey_to_background(client):
    r = client.post("/users", json={"user_id": "u1", "survey": SURVEY})
    assert r.status_code == 201 and r.json() == {"user_id": "u1", "background": "ml_beginner",
                                                 "condition": "S2", "lang": "en"}
    assert client.post("/users", json={"user_id": "u1", "survey": SURVEY}).status_code == 409


def test_survey_is_strict_and_conditions_limited(client):
    bad = {**SURVEY, "extra": 1}
    assert client.post("/users", json={"user_id": "u2", "survey": bad}).status_code == 422
    assert client.post("/users", json={"user_id": "u3", "survey": SURVEY,
                                       "condition": "S4"}).status_code == 422


def test_me_returns_the_user_and_404s_unknown(client):
    signup(client, lang="en")
    me = client.get("/me", headers=H("u1"))
    assert me.status_code == 200
    assert me.json() == {"user_id": "u1", "background": "ml_beginner", "condition": "S2",
                         "lang": "en"}
    assert client.get("/me", headers=H("ghost")).status_code == 404
    assert client.get("/me").status_code == 422  # missing identity header


def test_selected_language_initializes_learning_and_switches_the_account_atomically(client):
    signup(client, lang="en")
    assert client.get("/wiki/settings", headers=H("u1")).json()["language"] == "en"
    changed = client.put("/wiki/settings", headers=H("u1"), json={"language": "zh"})
    assert changed.status_code == 200
    assert client.get("/me", headers=H("u1")).json()["lang"] == "zh"
    assert client.get("/wiki/settings", headers=H("u1")).json()["language"] == "zh"
