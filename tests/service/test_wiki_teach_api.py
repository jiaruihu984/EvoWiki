"""R1 Task 2 routes: /wiki/teach/* over PgRepo, and /wiki/guidelines over the spec."""

import pytest

from evowiki.teach.default_spec import DEFAULT_ITEMS
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.events import Guideline, GuidelineAdded, TeachingSpecSaved, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.teach import commit_spec
from evowiki.wikiapp.wiki_schemas import GuidelinesOut, TeachSpecOut, TeachVersionsOut
from tests.runtime.helpers import T0
from tests.service.conftest import H, signup
from tests.service.test_wiki_api import organized_page
from tests.wikiapp.script import Script


@pytest.fixture
def wiki(services, client):
    script = Script()
    services.gateway = script.gateway()
    return client, script


def spec(client, uid="u1") -> TeachSpecOut:
    r = client.get("/wiki/teach/spec", headers=H(uid))
    assert r.status_code == 200, r.text
    return TeachSpecOut.model_validate(r.json())


def ok(r, status=200) -> TeachSpecOut:
    assert r.status_code == status, r.text
    return TeachSpecOut.model_validate(r.json())


def old_guidelines(services, *gs: Guideline):
    repo = PgRepo(services.pool)
    repo.commit(repo.load("u1"), Commit(wiki=[
        WikiEvent(event_id=services.new_id("wev"), ts=T0, body=GuidelineAdded(guideline=g))
        for g in gs]))


def test_spec_versions_items_params_and_base_version(wiki, services):
    client, _ = wiki
    signup(client, "u1")
    old_guidelines(services, Guideline(guideline_id="gl_a", text="多用图", source="user"))
    v = spec(client)
    assert (v.version, v.stored, v.max_items, v.max_exemplars) == (1, False, 30, 3)
    assert [i.item_id for i in v.items][-1] == "gl_a" and len(v.items) == len(DEFAULT_ITEMS) + 1
    r = client.get("/wiki/teach/versions", headers=H("u1"))
    assert r.status_code == 200 and r.json() == {"current": 1, "items": []}

    stale = client.post("/wiki/teach/items?base_version=2", headers=H("u1"), json={"text": "x"})
    assert stale.status_code == 409 and "第 1 版" in stale.json()["detail"]
    out = ok(client.post("/wiki/teach/items?base_version=1", headers=H("u1"),
                         json={"text": "先给结论", "scope": "concept", "domain": "统计"}), 201)
    assert (out.version, out.stored) == (2, True)
    sid = out.items[-1].item_id
    vs = TeachVersionsOut.model_validate(client.get("/wiki/teach/versions", headers=H("u1")).json())
    assert vs.current == 2 and [(m.version, m.action) for m in vs.items] == \
        [(1, "migrated"), (2, "edit")]
    evs = [e.body for e in PgRepo(services.pool).wiki_events_of("u1", ("teaching_spec_saved",))]
    assert [b.spec.version for b in evs] == [1, 2] and all(isinstance(b, TeachingSpecSaved)
                                                           for b in evs)

    out = ok(client.patch(f"/wiki/teach/items/{sid}", headers=H("u1"), json={"domain": None}))
    assert out.items[-1].domain is None and out.version == 3
    assert client.patch(f"/wiki/teach/items/{sid}", headers=H("u1"), json={}).status_code == 422
    assert client.patch("/wiki/teach/items/nope", headers=H("u1"),
                        json={"scope": "any"}).status_code == 404
    out = ok(client.put("/wiki/teach/params", headers=H("u1"),
                        json={"recap_max_per_page": 0, "analogy_level": 2, "fading_level": 1,
                              "interactive_bias": 0}))
    assert out.params.analogy_level == 2 and out.version == 4
    assert client.put("/wiki/teach/params", headers=H("u1"),
                      json={"recap_max_per_page": 9}).status_code == 422
    out = ok(client.delete(f"/wiki/teach/items/{sid}?base_version=4", headers=H("u1")))
    assert out.version == 5 and sid not in {i.item_id for i in out.items}
    assert client.delete(f"/wiki/teach/items/{sid}", headers=H("u1")).status_code == 404
    assert client.get("/wiki/teach/spec", headers=H("ghost")).status_code == 404


def test_restore_reads_the_old_version_from_the_log(wiki, services):
    client, _ = wiki
    signup(client, "u1")
    ok(client.post("/wiki/teach/items", headers=H("u1"), json={"text": "A"}), 201)    # v1, v2
    ok(client.delete("/wiki/teach/items/d01", headers=H("u1")))                       # v3
    v2 = ok(client.get("/wiki/teach/versions/2", headers=H("u1")))
    assert v2.version == 2 and v2.items[0].item_id == "d01" and v2.items[-1].text == "A"
    assert client.get("/wiki/teach/versions/9", headers=H("u1")).status_code == 404
    assert client.post("/wiki/teach/versions/2/restore?base_version=2",
                       headers=H("u1")).status_code == 409
    out = ok(client.post("/wiki/teach/versions/2/restore", headers=H("u1")))
    assert out.version == 4 and out.items == v2.items
    m = TeachVersionsOut.model_validate(
        client.get("/wiki/teach/versions", headers=H("u1")).json()).items[-1]
    assert (m.version, m.action, m.origin) == (4, "restore", "v2")
    assert client.post("/wiki/teach/versions/9/restore", headers=H("u1")).status_code == 404


def test_no_item_count_cap_but_text_caps_are_422(wiki, services):
    client, _ = wiki
    signup(client, "u1")
    for i in range(31 - len(DEFAULT_ITEMS)):
        ok(client.post("/wiki/teach/items", headers=H("u1"), json={"text": f"规则 {i}"}), 201)
    assert client.post("/wiki/teach/items", headers=H("u1"), json={"text": "第 32 条"}).status_code == 201
    assert client.post("/wiki/guidelines", headers=H("u1"), json={"text": "第 33 条"}).status_code == 201
    assert client.post("/wiki/teach/items", headers=H("u1"),
                       json={"text": "x" * 501}).status_code == 422
    assert client.post("/wiki/teach/items", headers=H("u1"),
                       json={"text": "   "}).status_code == 422


def test_more_than_30_old_guidelines_migrate(wiki, services):
    client, _ = wiki
    signup(client, "u1")
    old_guidelines(services, *[Guideline(guideline_id=f"g{i}", text=f"原则 {i}", source="user")
                               for i in range(31)])
    out = ok(client.get("/wiki/teach/spec", headers=H("u1")))
    assert len(out.items) == len(DEFAULT_ITEMS) + 31
    assert client.post("/wiki/teach/items", headers=H("u1"), json={"text": "x"}).status_code == 201


def test_old_guideline_api_on_a_pre_branch_log(wiki, services):
    client, _ = wiki
    signup(client, "u1")
    old_guidelines(services, Guideline(guideline_id="gl_a", text="多用图", source="user"),
                   Guideline(guideline_id="gl_b", text="先给公式", source="feedback",
                             evidence="你两次要公式"))
    before = GuidelinesOut.model_validate(client.get("/wiki/guidelines", headers=H("u1")).json())
    assert [(g.guideline_id, g.source, g.evidence) for g in before.items] == \
        [("gl_a", "user", None), ("gl_b", "feedback", "你两次要公式")]
    r = client.post("/wiki/guidelines", headers=H("u1"), json={"text": "少用表格"})
    assert r.status_code == 201 and set(r.json()) == {"guideline_id", "text", "source", "evidence"}
    assert client.delete("/wiki/guidelines/gl_a", headers=H("u1")).status_code == 204
    after = GuidelinesOut.model_validate(client.get("/wiki/guidelines", headers=H("u1")).json())
    assert [g.text for g in after.items] == ["先给公式", "少用表格"]
    assert after.items[0].evidence == "你两次要公式"
    assert client.delete("/wiki/guidelines/gl_a", headers=H("u1")).status_code == 404
    types = [e.body.type for e in PgRepo(services.pool).wiki_events_of(
        "u1", ("guideline_added", "guideline_removed", "teaching_spec_saved"))]
    assert types == ["guideline_added", "guideline_added", "teaching_spec_saved",
                     "teaching_spec_saved", "teaching_spec_saved"]
    assert spec(client).version == 3


def test_exemplars_page_snapshot_vault_note_and_4th_rejected(wiki, services):
    client, _ = wiki
    pid = organized_page(wiki)
    out = ok(client.post("/wiki/teach/exemplars", headers=H("u1"),
                         json={"kind": "wiki_page", "ref": pid}), 201)
    ex = out.exemplars[0]
    assert (ex.kind, ex.ref) == ("wiki_page", pid) and ex.text.startswith(f"# {ex.title}\n")
    assert "## z 分数怎么读" in ex.text
    assert client.post("/wiki/teach/exemplars", headers=H("u1"),
                       json={"kind": "wiki_page", "ref": "pg_nope"}).status_code == 404
    note = {"kind": "vault_note", "ref": "notes/a.md", "title": "A", "text": "正文"}
    ok(client.post("/wiki/teach/exemplars", headers=H("u1"), json=note), 201)
    ok(client.post("/wiki/teach/exemplars", headers=H("u1"), json=note), 201)
    r = client.post("/wiki/teach/exemplars", headers=H("u1"), json=note)
    assert r.status_code == 422 and r.json() == {"detail": "范例最多 3 个", "code": "example_limit", "params": {"limit": 3}}
    assert client.post("/wiki/teach/exemplars", headers=H("u1"),
                       json={**note, "text": "x" * 60_001}).status_code == 422
    assert client.post("/wiki/teach/exemplars", headers=H("u1"),
                       json={"kind": "wiki_page", "ref": pid, "text": "x"}).status_code == 422
    out = ok(client.delete(f"/wiki/teach/exemplars/{ex.exemplar_id}", headers=H("u1")))
    assert [e.kind for e in out.exemplars] == ["vault_note", "vault_note"]
    assert client.delete(f"/wiki/teach/exemplars/{ex.exemplar_id}",
                         headers=H("u1")).status_code == 404


def test_unorganized_page_exemplar_is_422(wiki, services):
    from tests.service.test_wiki_api import new_page

    client, _ = wiki
    pid = new_page(wiki)
    r = client.post("/wiki/teach/exemplars", headers=H("u1"), json={"kind": "wiki_page", "ref": pid})
    assert r.status_code == 422 and "还没整理" in r.json()["detail"]


def test_spec_lists_the_users_rejected_and_revoked_rules_not_in_force(wiki, services):
    """个性化 · 管理偏好 「没采用和撤回的」: a pending rule not adopted (rejected) and a rule the user
    removed (revoked) show under `decisions`, newest first, until the user adds the text back."""
    client, _ = wiki
    signup(client, "u1")
    repo = PgRepo(services.pool)
    commit_spec(repo, repo.load("u1"), lambda s: tspec.add_item(
        s, item_id="mb1", text="多用能算的小例子", origin="memory_backfill", authority="pending"),
        actor="system", action="guideline", now=T0, new_id=services.new_id)
    assert spec(client).decisions == []
    ok(client.delete("/wiki/teach/items/mb1", headers=H("u1")))
    mine = ok(client.post("/wiki/teach/items", headers=H("u1"), json={"text": "先给结论"}), 201)
    sid = mine.items[-1].item_id
    out = ok(client.delete(f"/wiki/teach/items/{sid}", headers=H("u1")))
    assert [(d.rule_id, d.text, d.action, d.via) for d in out.decisions] == [
        (sid, "先给结论", "revoked", "ui"), ("mb1", "多用能算的小例子", "rejected", "ui")]
    back = ok(client.post("/wiki/teach/items", headers=H("u1"), json={"text": "多用能算的小例子"}), 201)
    assert back.items[-1].authority == "user"
    assert [d.rule_id for d in back.decisions] == [sid]
    assert [d.rule_id for d in spec(client).decisions] == [sid]


def test_walkthrough_backend_writes_one_pending_preference(wiki, services):
    """tests/e2e/blocks_backend.py --pending-pref: the system's backfill write, shown as pending
    with its evidence line; the user's PATCH makes it theirs (reaccepted)."""
    from tests.e2e.blocks_backend import add_pending_pref

    client, _ = wiki
    signup(client, "u1")
    rid = add_pending_pref(services.pool, "u1", "讲数学时先给能算的小例子", "你在 3 次对话里都要了",
                           services.new_id)
    v = spec(client)
    item = next(i for i in v.items if i.item_id == rid)
    assert (item.origin, item.authority, v.evidence[rid]) == ("memory_backfill", "pending", "你在 3 次对话里都要了")
    out = ok(client.patch(f"/wiki/teach/items/{rid}", headers=H("u1"), json={"text": "讲数学先给小例子"}))
    assert next(i for i in out.items if i.item_id == rid).authority == "user"
