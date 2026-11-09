"""/wiki/sources phase 2: retry, long scans read without a quote, ask uploads, a first learning use of
a chat attachment, the cleanup after uploads. Inline runs, fake readers and a fake JsonLLM."""

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest

from evowiki.llm.providers import ProviderRejectedError
from evowiki.service import routes_sources
from evowiki.service.deps import import_deps
from evowiki.sources import events, store
from evowiki.sources.models import SourceRow
from evowiki.sources.runs import InlineSourceRuns
from evowiki.wikiapp.llm import PROVIDER_REJECTED_DETAIL
from tests.runtime.helpers import T0
from tests.service.conftest import H, parse_sse, signup
from tests.sources.test_importer import CountingLLM
from tests.sources.test_importer_vision import Reader, make_pdf

FIX = Path(__file__).parent.parent / "fixtures" / "sources"
TEXT = "# 标题\n\n第一段文字。\n\n## 小节\n\n第二段文字。".encode()


@pytest.fixture
def box():
    return {"llm": None, "local": Reader("local", available=False), "claude": Reader("claude")}


@pytest.fixture
def c(client, services, box):
    deps = import_deps(services)
    services.sources = InlineSourceRuns(dataclasses.replace(
        deps, llm_for=lambda _u: box["llm"], local=box["local"], claude=box["claude"]))
    signup(client, "u1")
    signup(client, "u2")
    return client


def post(c, data: bytes, name: str, uid="u1", **extra):
    return c.post("/wiki/sources", headers=H(uid), files={"file": (name, data)}, data=extra)


def progress(c, sid, uid="u1"):
    r = c.get(f"/wiki/sources/{sid}/progress", headers=H(uid))
    assert r.status_code == 200
    return parse_sse(r.text)


def row(c, sid, uid="u1") -> SourceRow:
    with c.app.state.services.pool.connection() as conn:
        return store.get_source(conn, uid, sid)


def started(c) -> list[str]:
    return c.app.state.services.sources.started


def test_retry_after_a_402_while_summarizing_pays_only_for_what_is_missing(c, box,
                                                                           monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")
    box["llm"] = llm = CountingLLM(fail=ProviderRejectedError("402", status_code=402),
                                   fail_from=2)
    sid = post(c, (FIX / "outline.pdf").read_bytes(), "outline.pdf").json()["id"]
    assert progress(c, sid)[-1] == ("error", {"detail": PROVIDER_REJECTED_DETAIL,
                                               "code": "provider_rejected"})
    got = c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()
    readable = sum(1 for s in got["sections"] if s["tokens"] > 0)
    summaries = lambda: [x for x in llm.calls if x == "sources.summary"]
    assert got["status"] == "ready" and len(summaries()) == 2 and readable > 2

    llm.fail = None
    r = c.post(f"/wiki/sources/{sid}/retry", headers=H("u1"))
    assert r.status_code == 202 and r.json()["status"] == "ready"
    assert started(c)[-1] == f"source:u1:{sid}:1"
    events_ = progress(c, sid)                 # follows the newest generation
    assert events_[-1][0] == "done" and "usable" in [n for n, _ in events_]
    assert len(summaries()) == 2 + (readable - 1)  # the one summary paid for is not asked again
    got = c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()
    assert all(s["summary"] for s in got["sections"] if s["tokens"] > 0)
    r = c.post(f"/wiki/sources/{sid}/retry", headers=H("u1"))
    assert r.status_code == 409 and r.json()["detail"] == "这份资料不需要重试"


def test_retry_of_a_failed_import_runs_again(c, monkeypatch):
    monkeypatch.setattr(routes_sources, "check_pdf", lambda _data: None)   # past the upload's look
    sid = post(c, (FIX / "encrypted.pdf").read_bytes(), "e.pdf").json()["id"]
    assert row(c, sid).status == "failed"
    r = c.post(f"/wiki/sources/{sid}/retry", headers=H("u1"))
    assert r.status_code == 202 and started(c)[-1] == f"source:u1:{sid}:1"
    assert row(c, sid).status == "failed" and row(c, sid).run_gen == 1   # still encrypted
    assert c.post("/wiki/sources/nope/retry", headers=H("u1")).status_code == 404
    assert c.post(f"/wiki/sources/{sid}/retry", headers=H("u2")).status_code == 404


def test_a_long_scan_is_read_in_full_without_a_quote(c, box):
    """No cost gate: 25 scanned pages (no text layer, a picture on each) are read at import; nothing waits for the user."""
    sid = post(c, make_pdf("s" * 25), "scan.pdf").json()["id"]
    assert box["claude"].calls == list(range(1, 26))
    done = progress(c, sid)[-1]
    assert done[0] == "done" and "看图读" not in (done[1]["note"] or "")
    assert row(c, sid).status == "ready" and row(c, sid).run_gen == 0


def test_an_upload_whose_run_cannot_start_fails_plainly_without_durable_retries(c):
    from evowiki.sources.uses import MSG_NOT_STARTED
    runs = c.app.state.services.sources

    def broken(job):
        raise RuntimeError("no runner")
    runs.start = broken                       # inline runs have no durable retries either
    r = post(c, TEXT, "a.md")
    assert r.status_code == 503 and r.json()["detail"] == MSG_NOT_STARTED
    (got,) = [s for s in c.get("/wiki/sources", headers=H("u1")).json()]
    assert (got["status"], got["error"]) == ("failed", MSG_NOT_STARTED)


def test_ask_upload_then_learn_starts_a_full_import_once(c, box, monkeypatch):
    sid = post(c, TEXT, "a.md", use="ask", session_id="chat1").json()["id"]
    assert started(c) == [f"source:u1:{sid}"]
    calls = []
    real = events.source_added

    def spy(*a, **kw):
        calls.append(a[3])
        return real(*a, **kw)
    monkeypatch.setattr(events, "source_added", spy)
    r = c.post(f"/wiki/sources/{sid}/uses", headers=H("u1"), json={"use": "learn"})
    assert r.status_code == 201 and r.json()["use"] == "learn"
    assert calls == ["learn"]
    assert started(c) == [f"source:u1:{sid}", f"source:u1:{sid}:1"]
    assert progress(c, sid)[-1][0] == "done"
    r = c.post(f"/wiki/sources/{sid}/uses", headers=H("u1"), json={"use": "exam"})
    assert r.status_code == 201 and calls == ["learn", "exam"]
    assert len(started(c)) == 2                     # not ask-only any more: no new run
    uses = c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()["uses"]
    assert sorted(u["use"] for u in uses) == ["ask", "exam", "learn"]   # one clock: any order


def test_another_ask_use_does_not_start_a_run(c):
    sid = post(c, TEXT, "a.md", use="ask").json()["id"]
    assert c.post(f"/wiki/sources/{sid}/uses", headers=H("u1"),
                  json={"use": "ask"}).status_code == 201
    assert started(c) == [f"source:u1:{sid}"]


def test_an_ask_upload_of_an_imported_file_reads_nothing_again(c, box):
    box["local"] = local = Reader("local")
    c.app.state.services.sources.deps.local = local
    data = make_pdf("tbt")
    first = post(c, data, "s.pdf", use="learn")
    assert first.status_code == 201 and local.calls == [2]
    again = post(c, data, "s.pdf", use="ask", session_id="chat1")
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert len(started(c)) == 1 and local.calls == [2]


def test_uploading_an_attachment_again_to_learn_from_starts_a_full_import(c):
    sid = post(c, TEXT, "a.md", use="ask").json()["id"]
    again = post(c, TEXT, "a.md", use="learn")
    assert again.status_code == 200 and again.json()["id"] == sid
    assert started(c) == [f"source:u1:{sid}", f"source:u1:{sid}:1"]


def _old_attachment(c, sid, days):
    at = T0 - timedelta(days=days)
    r = SourceRow(source_id=sid, user_id="u1", kind="text", title=sid, sha256=sid, bytes=1,
                  pages=None, status="ready", error=None, unreadable_pages=(), toc_mode=None,
                  progress={}, origin=None, created_at=at, updated_at=at, deleted_at=None)
    with c.app.state.services.pool.connection() as conn:
        store.insert_source(conn, r, b"x", "text/plain")
        store.add_use(conn, "u1", sid, "ask", session_id="chat1", now=at)


def test_an_upload_cleans_up_old_attachments(c):
    _old_attachment(c, "old", 31)
    _old_attachment(c, "young", 29)
    assert post(c, TEXT, "a.md").status_code == 201
    assert row(c, "old").deleted_at is not None and row(c, "young").deleted_at is None


def test_a_failing_cleanup_does_not_fail_the_upload(c, monkeypatch):
    def boom(*_a):
        raise RuntimeError("db down")
    monkeypatch.setattr(routes_sources, "sweep_ask_attachments", boom)
    assert post(c, TEXT, "a.md").status_code == 201


# ---- service wiring (deps.py) ---------------------------------------------------------------

class _Closing(Reader):
    def __init__(self):
        super().__init__("local")
        self.closed = 0

    def close(self):
        self.closed += 1


def test_import_deps_without_a_gateway_reads_nothing_and_sees_no_images(services):
    deps = import_deps(services)
    assert deps.local.name == "none" and deps.claude.name == "none"
    assert deps.assistant_sees_images("u1") is False


def test_the_assistant_sees_images_when_its_turns_go_to_anthropic(services, tmp_path):
    from evowiki.llm.gateway import Gateway
    from evowiki.llm.log import CallLog
    from evowiki.llm.models import GatewayConfig, ModelRef
    for gen, want in (("anthropic", True), ("deepseek", False)):
        model = "claude-sonnet-5-5" if gen == "anthropic" else "deepseek-v4-pro"
        services.gateway = Gateway(GatewayConfig(
            generator=ModelRef(provider=gen, model=model),
            verifier=ModelRef(provider="deepseek", model="d"), allow_single_provider=True),
            CallLog(tmp_path / f"{gen}.jsonl"))
        assert import_deps(services).assistant_sees_images("u1") is want


def test_eyes_gateway_needs_an_anthropic_key_and_a_priced_model(pool):
    from evowiki.llm.prices import load_prices
    from evowiki.service.deps import build_eyes_gateway
    prices = load_prices()
    assert build_eyes_gateway({}, pool, None, prices, {"deepseek": object()}, T0) is None
    gw = build_eyes_gateway({}, pool, None, prices, {"anthropic": object()}, lambda: T0)
    assert gw is not None and gw.metered
    assert gw.config.generator.model == "claude-sonnet-5-5"
    assert gw.route_provider("generator") == "anthropic"
    with pytest.raises(ValueError):
        build_eyes_gateway({"EVOWIKI_VISION_MODEL": "no-such-model"}, pool, None, prices,
                           {"anthropic": object()}, lambda: T0)


def test_the_durable_runtime_closes_the_local_reader_on_stop(services, monkeypatch):
    from evowiki.agent import dbos_app
    from evowiki.service.deps import DurableRuntime
    monkeypatch.setattr(dbos_app, "shutdown", lambda: None)
    reader = _Closing()
    rt = DurableRuntime("postgresql://unused", executor_id="t")
    rt.sources = dataclasses.replace(import_deps(services), local=reader)
    rt.stop()
    assert reader.closed == 1


def test_attaching_an_old_file_again_keeps_it(c):
    first = post(c, TEXT, "a.md", use="ask", session_id="chat1").json()["id"]
    with c.app.state.services.pool.connection() as conn:   # make the first upload 40 days old
        conn.execute("update source set created_at = created_at - interval '40 days' "
                     "where source_id = %s", (first,))
        conn.execute("update source_use set created_at = created_at - interval '40 days' "
                     "where source_id = %s", (first,))
    again = post(c, TEXT, "a.md", use="ask", session_id="chat2")
    assert again.status_code == 200 and again.json()["id"] == first
    assert again.json()["deleted"] is False and row(c, first).deleted_at is None


def listed(c, uid="u1") -> list[str]:
    return [s["id"] for s in c.get("/wiki/sources", headers=H(uid)).json()]


def test_the_list_leaves_out_attachments_kept_only_in_their_conversation(c):
    """Spec §7 C4: a chat attachment nobody kept stays in its conversation; 参数 → 资料 lists it
    once it has a use beyond ask."""
    kept = post(c, TEXT, "k.md", use="learn").json()["id"]
    sid = post(c, b"# B\n\nanother text", "b.md", use="ask", session_id="chat1").json()["id"]
    assert listed(c) == [kept]
    assert c.get(f"/wiki/sources/{sid}", headers=H("u1")).status_code == 200   # still readable
    assert c.post(f"/wiki/sources/{sid}/uses", headers=H("u1"),
                  json={"use": "exam"}).status_code == 201
    assert sorted(listed(c)) == sorted([kept, sid])


def test_adding_a_chat_attachment_again_without_a_use_keeps_it(c):
    """参数 → 资料 adds a file that was only a chat attachment: it becomes a kept material (a
    supplement use, listed) and is imported in full."""
    sid = post(c, TEXT, "a.md", use="ask", session_id="chat1").json()["id"]
    n = len(started(c))
    again = post(c, TEXT, "a.md")
    assert again.status_code == 200 and again.json()["id"] == sid
    assert sorted(u["use"] for u in again.json()["uses"]) == ["ask", "supplement"]
    assert sid in listed(c)
    assert len(started(c)) == n + 1 and row(c, sid).progress.get("mode") == "full"


def test_attaching_a_file_again_takes_the_page_check_of_this_version(c):
    data = make_pdf("tf")
    first = post(c, data, "s.pdf", use="ask", session_id="chat1")
    sid = first.json()["id"]
    pool = c.app.state.services.pool
    with pool.connection() as conn:     # as a row imported before the check knew these pages
        uid = conn.execute("select user_id from source where source_id = %s", (sid,)).fetchone()["user_id"]
        store.set_text_first(conn, uid, sid, text_first=True, imprecise_pages=[], image_pages=[])
    again = post(c, data, "s.pdf", use="ask", session_id="chat2")
    assert again.status_code == 200 and again.json()["id"] == sid
    with pool.connection() as conn:
        assert store.get_source(conn, uid, sid).image_pages == (2,)


def test_a_locked_or_broken_pdf_is_refused_at_the_upload_and_leaves_no_row(c):
    for name, say in (("encrypted.pdf", "加了密码"), ("broken.pdf", "可能损坏")):
        r = post(c, (FIX / name).read_bytes(), name)
        assert r.status_code == 422 and say in r.json()["detail"]
    assert c.get("/wiki/sources", headers=H("u1")).json() == []
