"""GET /wiki/components over Postgres (migration 0027): the library listing without html."""

from evowiki.deep.store import PgLibrary
from tests.deep.fakes import component
from tests.service.conftest import H


def test_lists_components_without_html(services, client, pool):
    with pool.connection() as conn:
        conn.execute("delete from component_library")
    services.library = PgLibrary(pool)
    r = client.get("/wiki/components", headers=H("u1"))
    assert r.status_code == 200 and r.json() == []
    services.library.put(component("zeta-demo"))
    services.library.put(component("pair-demo"))
    r = client.get("/wiki/components", headers=H("u1"))
    assert r.status_code == 200
    got = r.json()
    assert [c["component_id"] for c in got] == ["pair-demo", "zeta-demo"]
    assert got[0] == {"component_id": "pair-demo", "name": "拼一条链",
                      "description": "点按钮看计数加一", "learning_question": "对面放什么？",
                      "params": {"加一": "点一下计数加一"}, "height": 480, "scenarios": [],
                      "configurable": False, "config_schema": None, "example_configs": []}
    assert "html" not in got[0] and "probes" not in got[0]


def test_503_when_the_library_is_not_wired(services, client):
    services.library = None
    assert client.get("/wiki/components", headers=H("u1")).status_code == 503


def test_needs_a_user_header(services, client):
    assert client.get("/wiki/components").status_code == 422


def test_configurable_components_list_their_schema(services, client, pool):
    from tests.deep.test_components import SCHEMA, configurable
    with pool.connection() as conn:
        conn.execute("delete from component_library")
    services.library = PgLibrary(pool)
    services.library.put(configurable())
    services.library.put(component("pair-demo"))
    got = {c["component_id"]: c for c in client.get("/wiki/components", headers=H("u1")).json()}
    steps = got["steps-demo"]
    assert steps["configurable"] and steps["config_schema"] == SCHEMA
    assert [e["title"] for e in steps["example_configs"]] == ["复制 DNA", "二分查找"]
    assert steps["scenarios"] == ["DNA 复制", "算法"]
