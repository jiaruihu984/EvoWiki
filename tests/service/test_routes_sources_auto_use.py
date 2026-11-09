"""T14: `POST /wiki/sources/{id}/uses` with `{"use": "auto"}` (the material decides, then the same
path as an explicit use), and the vision quote gone. Inline runs, no LLM."""

import dataclasses

import pytest

from evowiki.service.deps import import_deps
from evowiki.sources import store
from evowiki.sources.models import SourceRow
from evowiki.sources.runs import InlineSourceRuns
from evowiki.sources.vision import NoVision
from tests.runtime.helpers import T0
from tests.service.conftest import H, signup
from tests.service.test_routes_sources_phase2 import post, started

PAPER = "# 摘要\n\n本文研究注意力。\n\n# 1 引言\n\n背景。\n\n# 2 方法\n\n方法。\n\n# 参考文献\n\n[1] 某文。"
NOTES = "# 前言\n\n本讲义。\n\n# 第1章 向量\n\n向量是……\n\n# 第2章 矩阵\n\n矩阵是……"
EXAM = "# 一、选择题\n\n" + "\n".join(
    f"{i}. 下列说法正确的是（ ）\nA. 甲\nB. 乙" for i in range(1, 21))


@pytest.fixture
def c(client, services):
    services.sources = InlineSourceRuns(dataclasses.replace(
        import_deps(services), llm_for=lambda _u: None, local=NoVision(), claude=NoVision()))
    signup(client, "u1")
    signup(client, "u2")
    return client


def auto(c, sid, uid="u1"):
    return c.post(f"/wiki/sources/{sid}/uses", headers=H(uid), json={"use": "auto"})


def test_auto_use_end_to_end(c):
    ids = {name: post(c, text.encode(), f"{name}.md", use="ask", session_id="chat1").json()["id"]
           for name, text in (("论文", PAPER), ("2023年期末考试", EXAM), ("讲义", NOTES))}
    got = {}
    for name, sid in ids.items():
        r = auto(c, sid)
        assert r.status_code == 201, r.text
        got[name] = r.json()["use"]
        assert started(c)[-1] == f"source:u1:{sid}:1"      # the full import, as for learn
    assert got == {"论文": "understand", "2023年期末考试": "exam", "讲义": "learn"}
    uses = c.get(f"/wiki/sources/{ids['讲义']}", headers=H("u1")).json()["uses"]
    assert sorted(u["use"] for u in uses) == ["ask", "learn"]           # "auto" is never stored


def test_auto_while_still_reading_is_409(c):
    r = SourceRow(source_id="r1", user_id="u1", kind="text", title="r1", sha256="r1", bytes=1,
                  pages=None, status="reading", error=None, unreadable_pages=(), toc_mode=None,
                  progress={}, origin=None, created_at=T0, updated_at=T0, deleted_at=None)
    with c.app.state.services.pool.connection() as conn:
        store.insert_source(conn, r, b"x", "text/plain")
    resp = auto(c, "r1")
    assert resp.status_code == 409 and resp.json()["detail"] == "资料还在读"


def test_auto_on_a_failed_source_is_409_not_read(c):
    r = SourceRow(source_id="f1", user_id="u1", kind="text", title="f1", sha256="f1", bytes=1,
                  pages=None, status="failed", error="打不开", unreadable_pages=(),
                  toc_mode=None, progress={}, origin=None, created_at=T0, updated_at=T0,
                  deleted_at=None)
    with c.app.state.services.pool.connection() as conn:
        store.insert_source(conn, r, b"x", "text/plain")
    resp = auto(c, "f1")
    assert resp.status_code == 409 and resp.json()["detail"] == "资料没有读出来"


def test_auto_on_someone_elses_source_is_404(c):
    sid = post(c, NOTES.encode(), "讲义.md", use="ask").json()["id"]
    assert auto(c, sid, uid="u2").status_code == 404


def test_an_unknown_use_is_422(c):
    sid = post(c, NOTES.encode(), "讲义.md", use="ask").json()["id"]
    r = c.post(f"/wiki/sources/{sid}/uses", headers=H("u1"), json={"use": "guess"})
    assert r.status_code == 422


def test_there_is_no_vision_quote_nor_its_route(c):
    sid = post(c, NOTES.encode(), "讲义.md").json()["id"]
    assert "vision_quote" not in c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()
    assert c.post(f"/wiki/sources/{sid}/vision", headers=H("u1")).status_code in (404, 405)


def test_a_run_that_never_started_is_started_at_start_up(c):
    """Start-up recovery starts such runs itself: nobody has to ask again."""
    from evowiki.sources.uses import start_unstarted
    svc = c.app.state.services
    with svc.pool.connection() as conn:
        for sid, status in (("lost", "reading"), ("usable", "ready")):
            r = SourceRow(source_id=sid, user_id="u1", kind="text", title=sid, sha256=sid,
                          bytes=10, pages=None, status=status, error=None, unreadable_pages=(),
                          toc_mode=None, progress={}, origin=None, created_at=T0,
                          updated_at=T0, deleted_at=None)
            store.insert_source(conn, r, NOTES.encode(), "text/plain")
            store.add_use(conn, "u1", sid, "ask", session_id="chat1", now=T0)
    assert start_unstarted(svc.pool, svc.sources) == 2
    assert sorted(started(c)) == ["source:u1:lost", "source:u1:usable"]
    assert c.get("/wiki/sources/lost", headers=H("u1")).json()["status"] == "ready"
    assert start_unstarted(svc.pool, svc.sources) == 0             # nothing left
