"""A goal from attached materials as the chat makes it (S1, then 确认: tests.service.conftest
.chat_goal; materials plan task 5, S1 2026-10-28): the material's table of contents goes to S1, a
use row (source_use) and the blocks' section links (source_link, by blocks) are written; a
material still being read, failed, split only by pages, missing or another user's is refused
(GoalSourceError) before any LLM call. Materials are markdown files imported inline; scripted LLM
only."""

import pytest

from evowiki.sources import store
from evowiki.sources import uses as material_uses
from evowiki.wikiapp.goal_sources import GoalSourceError
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.wiki_schemas import GoalDraftOut
from tests.service.conftest import H, signup
from tests.wikiapp.goal_script import auto_probe, chat_goal
from tests.wikiapp.script import Script

BOOK = "# 1 向量\n\n向量是有方向的量。\n\n## 1.1 向量加法\n\n首尾相接。\n\n## 1.2 数乘\n\n伸缩。\n"
PAPER = ("# 摘要\n\n我们提出一种新结构。\n\n# 1 引言\n\n序列建模很重要。\n\n# 2 方法\n\n"
         "模型由编码器组成。\n\n# 参考文献\n\n[1] 某某。\n")
LEARN = {"title": "向量", "blocks": [
    {"name": "向量加法", "summary": "两个向量怎么加", "page": True, "sections": ["§1.1"],
     "target": True},
    {"name": "数乘", "summary": "数乘向量", "page": True, "sections": ["§1.2"]}]}


@pytest.fixture
def wiki(services, client):
    s = Script()
    auto_probe(s)
    services.gateway = s.gateway()
    signup(client, "u1")
    signup(client, "u2")
    return client, s, services


def add_source(client, text=BOOK, title="线性代数讲义", uid="u1") -> str:
    r = client.post("/wiki/sources", headers=H(uid), data={"title": title},
                    files={"file": (f"{title}.md", text.encode())})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert client.get(f"/wiki/sources/{sid}", headers=H(uid)).json()["status"] == "ready"
    return sid


def make(client, goal="", sids=(), use=None) -> GoalDraftOut:
    return GoalDraftOut.model_validate(chat_goal(client, "u1", goal, sids, use=use))


def refused(client, goal="", sids=(), use=None) -> GoalSourceError:
    with pytest.raises(GoalSourceError) as e:
        make(client, goal, sids, use)
    return e.value


def set_source(svc, sid, **cols):
    with svc.pool.connection() as conn:
        for k, v in cols.items():
            conn.execute(f"update source set {k} = %s where source_id = %s", (v, sid))


def drafts(svc) -> int:
    return len(PgRepo(svc.pool).load("u1").state.drafts)


def test_learn_from_a_material_writes_links_and_a_use(wiki):
    client, s, svc = wiki
    sid = add_source(client)
    s.push("goals.intake", LEARN)
    out = make(client, "", [sid], use="follow")
    p = s.requests("goals.intake")[0].prompt
    assert "「按《线性代数讲义》学」" in p and "§1.1 1.1 向量加法" in p
    with svc.pool.connection() as conn:
        links = store.links_for_sources(conn, "u1", [sid])
        uses = store.uses_of(conn, "u1", sid)
        assert store.links_for_sources(conn, "u2", [sid]) == []
    # stage 3: the blocks' sections are linked (by "blocks"); the goal's own use is on the map
    assert out.map is not None and s.calls("goals.intake") == 1
    ids = [n.id for n in out.map.nodes]
    assert [(x.node_id, x.role, x.by) for x in links] == [
        (ids[0], "teaches", "blocks"), (ids[1], "teaches", "blocks")]
    assert [u["use"] for u in uses] == ["learn"]
    assert PgRepo(svc.pool).load("u1").state.maps[out.map.map_id].material_use == "follow"


def test_entering_a_block_writes_its_parts_links_by_enter(wiki):
    """Stage 3: entering a block passes the TOC trimmed to its linked sections and writes the
    parts' links in the split's commit."""
    client, s, svc = wiki
    sid = add_source(client)
    s.push("goals.intake", {"title": "向量", "blocks": [
        {"name": "向量", "summary": "向量和它的运算", "sections": ["§1"], "target": True},
        {"name": "别的", "summary": "别的"}]})
    out = make(client, "", [sid])
    if out.draft_id is not None:
        client.post(f"/wiki/goals/{out.draft_id}/stop", headers=H("u1"))
    map_id = out.map.map_id if out.map is not None else PgRepo(svc.pool).load(
        "u1").state.drafts[out.draft_id].map_id
    block = PgRepo(svc.pool).load("u1").state.maps[map_id].nodes[0].id
    s.push("goals.intake", {"parts": [
        {"name": "向量加法", "atomic": True, "sections": ["§1.1"]},
        {"name": "数乘", "atomic": True, "sections": ["§1.2"]}]})
    r = client.post(f"/wiki/maps/{map_id}/regions/{block}/enter", headers=H("u1"))
    assert r.status_code == 201, r.text
    p = s.requests("goals.enter")[-1].prompt
    assert "按资料来拆" in p and "§1.1 1.1 向量加法" in p
    kids = {n.name: n.id for n in PgRepo(svc.pool).load("u1").state.maps[map_id].children(block)}
    with svc.pool.connection() as conn:
        links = store.links_for_sources(conn, "u1", [sid])
    assert {(x.node_id, x.by) for x in links} == {
        (block, "blocks"), (kids["向量加法"], "enter"), (kids["数乘"], "enter")}


def test_a_partly_read_material_is_fine(wiki):
    client, s, svc = wiki
    sid = add_source(client)
    set_source(svc, sid, status="partial")
    s.push("goals.intake", LEARN)
    assert make(client, "", [sid], use="follow").map is not None


@pytest.mark.parametrize(("cols", "code", "detail"), [
    ({"status": "reading"}, 409, "资料还在读，读完再开始"),
    ({"status": "failed"}, 422, "这份资料没能读出来，换一份再试"),
    ({"toc_mode": "pages"}, 422, "这份资料没认出目录，没法按它排路径"),
])
def test_unusable_material_is_refused_before_any_call(wiki, cols, code, detail):
    client, s, svc = wiki
    sid = add_source(client)
    set_source(svc, sid, **cols)
    before = drafts(svc)
    e = refused(client, "学向量", [sid], use="follow")
    assert (e.status, e.detail) == (code, detail)
    assert s.calls() == 0 and drafts(svc) == before


def test_missing_foreign_or_deleted_material_is_404(wiki):
    client, s, _ = wiki
    foreign = add_source(client, uid="u2")
    gone = add_source(client, title="删掉的")
    assert client.delete(f"/wiki/sources/{gone}", headers=H("u1")).status_code == 204
    for sid in ("nope", foreign, gone):
        e = refused(client, "学向量", [sid])
        assert (e.status, e.detail) == (404, "找不到这份资料")
    assert s.calls() == 0


# --- the goal S1 works on ----------------------------------------------------------------------

def _guessed(wiki, goal, *texts) -> str:
    client, s, _ = wiki
    sids = [add_source(client, t, title) for t, title in texts]
    make(client, goal, sids)
    return s.requests("goals.intake")[0].prompt


def test_text_that_does_not_ask_learns(wiki):
    wiki[1].push("goals.intake", LEARN)
    p = _guessed(wiki, "学向量加法", (BOOK, "线性代数讲义"))
    assert "学向量加法" in p and "读懂《" not in p


def test_empty_goal_and_a_textbook_learns(wiki):
    wiki[1].push("goals.intake", LEARN)
    p = _guessed(wiki, "", (BOOK, "线性代数讲义"))
    assert "「按《线性代数讲义》学」" in p


def test_empty_goal_and_two_materials_learns(wiki):
    wiki[1].push("goals.intake", {**LEARN, "blocks": [
        {**b, "sections": ["1" + "·" + x for x in b["sections"]]} for b in LEARN["blocks"]]})
    p = _guessed(wiki, "", (PAPER, "论文"), (BOOK, "线性代数讲义"))
    assert "「按《论文、线性代数讲义》学」" in p


def test_links_not_saved_is_a_500_that_says_so_and_no_goal_is_stored(wiki, monkeypatch):
    client, s, svc = wiki
    sid = add_source(client)
    s.push("goals.intake", LEARN)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(material_uses, "record_use", boom)
    e = refused(client, "", [sid])
    assert (e.status, e.detail) == (500, "资料出处没记上，目标没有建，请重试")
    with svc.pool.connection() as conn:
        assert store.links_for_sources(conn, "u1", [sid]) == []     # the goal's commit: rolled back
    assert drafts(svc) == 0                                          # ... the goal with it


def test_understand_words_without_a_material_make_a_plain_goal(wiki):
    """A use only means something for materials: 「帮我读懂这篇」 with none attached is an ordinary
    goal (wikiapp.goal_sources.material_use), not S1's one-material reading plan."""
    client, s, _ = wiki
    s.push("goals.intake", {"title": "编码器", "blocks": [
        {"name": "编码器", "summary": "把输入变成向量", "page": True, "target": True}]})
    make(client, "帮我读懂这篇论文")
    assert "学习者要读懂这一篇资料" not in s.requests("goals.intake")[0].prompt


def add_attachment(client, text=BOOK, title="线性代数讲义", uid="u1") -> str:
    """A chat attachment: imported in mode ask, its only use `ask`."""
    r = client.post("/wiki/sources", headers=H(uid), data={"use": "ask", "session_id": "chat1"},
                    files={"file": (f"{title}.md", text.encode())})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_a_goal_from_a_chat_attachment_starts_its_full_import_after_the_commit(wiki):
    """apply_use's rule (phase 2): the first learning use of an ask-only material starts the
    full import; the goal's use is that use."""
    client, s, svc = wiki
    sid = add_attachment(client)
    assert svc.sources.started == [f"source:u1:{sid}"]
    s.push("goals.intake", LEARN)
    make(client, "", [sid], use="follow")
    assert svc.sources.started == [f"source:u1:{sid}", f"source:u1:{sid}:1"]
    with svc.pool.connection() as conn:
        row = store.get_source(conn, "u1", sid)
        uses = store.uses_of(conn, "u1", sid)
        links = store.links_for_sources(conn, "u1", [sid])
    assert row.run_gen == 1 and row.progress["end"] == "done" and row.progress["mode"] == "full"
    assert sorted(u["use"] for u in uses) == ["ask", "learn"]
    assert {x.by for x in links} == {"blocks"}     # stage 3: the blocks' section links
    # a second goal from it: the full import is done, nothing starts again
    s.push("goals.intake", LEARN)
    assert make(client, "", [sid], use="follow").map is not None
    assert len(svc.sources.started) == 2


def test_a_full_import_that_cannot_start_keeps_the_goal_and_the_material_says_so(wiki):
    client, s, svc = wiki
    sid = add_attachment(client)

    def broken(job):
        raise RuntimeError("no runner")
    svc.sources.start = broken
    s.push("goals.intake", LEARN)
    out = make(client, "", [sid], use="follow")
    assert out.map is not None                               # the goal is made
    assert drafts(svc) == 1
    with svc.pool.connection() as conn:
        row = store.get_source(conn, "u1", sid)
        uses = store.uses_of(conn, "u1", sid)
    assert row.progress["end"] == "error"
    assert row.progress["detail"] == material_uses.MSG_NOT_STARTED
    assert row.status == "ready" and sorted(u["use"] for u in uses) == ["ask", "learn"]
    assert client.post(f"/wiki/sources/{sid}/retry", headers=H("u1")).status_code == 503


def test_a_material_deleted_before_the_commit_is_404_and_no_goal_is_stored(wiki, monkeypatch):
    client, s, svc = wiki
    sid = add_source(client)
    s.push("goals.intake", LEARN)

    def gone(*a, **k):
        raise LookupError("deleted meanwhile")
    monkeypatch.setattr(material_uses, "record_use", gone)
    e = refused(client, "", [sid])
    assert (e.status, e.detail) == (404, "找不到这份资料")
    assert drafts(svc) == 0
