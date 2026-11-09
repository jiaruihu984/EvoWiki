from pathlib import Path

import pytest

from evowiki.assistant.tools.fetch_url import Fetched, FetchError
from evowiki.sources import config
from tests.service.conftest import H, parse_sse, signup

FIX = Path(__file__).parent.parent / "fixtures" / "sources"
MSG_URL = "打不开这个网址，可以把正文贴进来"


def upload(client, uid="u1", name="outline.pdf", **extra):
    data = (FIX / name).read_bytes()
    return client.post("/wiki/sources", headers=H(uid), files={"file": (name, data)}, data=extra)


def progress(client, sid, uid="u1"):
    r = client.get(f"/wiki/sources/{sid}/progress", headers=H(uid))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    return parse_sse(r.text)


class FakeFetcher:
    def __init__(self, body: bytes, ctype: str = "text/html; charset=utf-8", fail=False):
        self.body, self.ctype, self.fail, self.calls = body, ctype, fail, []

    def fetch(self, url: str) -> Fetched:
        self.calls.append(url)
        if self.fail:
            raise FetchError("boom")
        return Fetched(final_url=url, content_type=self.ctype, body=self.body)


@pytest.fixture
def c(client):
    signup(client, "u1")
    signup(client, "u2")
    return client


def test_upload_pdf_then_progress_done(c):
    r = upload(c)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "pdf" and body["title"] == "学习方法 Learning Methods"   # its own title
    events = progress(c, body["id"])
    assert events[-1][0] == "done"
    assert events[-1][1]["status"] == "ready"
    got = c.get(f"/wiki/sources/{body['id']}", headers=H("u1")).json()
    assert got["status"] == "ready" and got["sections"] and got["uses"] == []
    assert got["deleted"] is False
    assert [s["id"] for s in c.get("/wiki/sources", headers=H("u1")).json()] == [body["id"]]


def test_same_file_twice_dedups_and_adds_use(c):
    first = upload(c)
    runs = c.app.state.services.sources
    n = len(runs.started)
    second = upload(c, use="learn", goal_id="g1")
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert len(runs.started) == n
    uses = c.get(f"/wiki/sources/{first.json()['id']}", headers=H("u1")).json()["uses"]
    assert [(u["use"], u["goal_id"]) for u in uses] == [("learn", "g1")]
    # the list, the upload answer and a rename carry the same uses
    listed = c.get("/wiki/sources", headers=H("u1")).json()
    assert [(u["use"], u["goal_id"]) for u in listed[0]["uses"]] == [("learn", "g1")]
    assert [u["use"] for u in second.json()["uses"]] == ["learn"]
    renamed = c.patch(f"/wiki/sources/{first.json()['id']}", headers=H("u1"), json={"title": "新名字"}).json()
    assert [u["use"] for u in renamed["uses"]] == ["learn"]
    # another user's identical file is a new source
    assert upload(c, uid="u2").status_code == 201


def test_too_big_413(c, monkeypatch):
    monkeypatch.setenv("EVOWIKI_SOURCE_MAX_MB", "0")
    r = upload(c)
    assert r.status_code == 413
    assert "文件太大了" in r.json()["detail"]


def test_unsupported_415(c):
    r = c.post("/wiki/sources", headers=H("u1"), files={"file": ("a.exe", b"MZ")})
    assert r.status_code == 415
    assert ".pdf" in r.json()["detail"]


def test_none_or_several_inputs_422(c):
    assert c.post("/wiki/sources", headers=H("u1"), data={}).status_code == 422
    r = c.post("/wiki/sources", headers=H("u1"), data={"text": "x", "url": "http://a.b"})
    assert r.status_code == 422


def test_text_upload(c):
    r = c.post("/wiki/sources", headers=H("u1"),
               data={"text": "# 归并排序\n\n把数组分成两半分别排序，再合并。", "title": "我的笔记"})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "text" and r.json()["title"] == "我的笔记"
    assert progress(c, r.json()["id"])[-1][0] == "done"
    f = c.get(f"/wiki/sources/{r.json()['id']}/file", headers=H("u1"))
    assert f.status_code == 200 and f.headers["content-type"].startswith("text/plain")
    assert "归并排序" in f.content.decode()


def test_empty_text_422(c):
    r = c.post("/wiki/sources", headers=H("u1"), data={"text": "   "})
    assert r.status_code == 422


def test_url_upload_with_fake_fetcher(c):
    html = (FIX / "page.html").read_bytes()
    fake = FakeFetcher(html)
    c.app.state.source_fetcher = fake
    r = c.post("/wiki/sources", headers=H("u1"), data={"url": "https://example.com/ms"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "url" and body["origin"] == "https://example.com/ms"
    assert "Merge Sort" in body["title"]
    assert progress(c, body["id"])[-1][0] == "done"
    assert fake.calls == ["https://example.com/ms"]
    f = c.get(f"/wiki/sources/{body['id']}/file", headers=H("u1"))
    assert f.content == html and f.headers["content-type"].startswith("text/html")
    assert f.headers["content-disposition"].startswith("attachment; ")
    assert f.headers["x-content-type-options"] == "nosniff"


class ArxivFetcher(FakeFetcher):
    """The PDF at its url; the API's feed (or its failure) for an export.arxiv.org request."""

    def __init__(self, pdf: bytes, feed: bytes | None):
        super().__init__(pdf, ctype="application/pdf")
        self.feed = feed

    def fetch(self, url: str) -> Fetched:
        if "export.arxiv.org" not in url:
            return super().fetch(url)
        self.calls.append(url)
        if self.feed is None:
            raise FetchError("HTTP 429")
        return Fetched(final_url=url, content_type="application/atom+xml", body=self.feed)


FEED = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1409.0473v7</id>'
        b"<title>Neural Machine\n  Translation</title></entry></feed>")
ARXIV = "https://arxiv.org/pdf/1409.0473"


def _arxiv(c, feed, **data):
    # no_outline.pdf: no metadata Title, no bookmark, like most arXiv PDFs
    c.app.state.source_fetcher = ArxivFetcher((FIX / "no_outline.pdf").read_bytes(), feed)
    return c.post("/wiki/sources", headers=H("u1"), data={"url": ARXIV, **data}).json()


def test_an_arxiv_url_has_its_real_title_at_once_and_is_a_pdf(c):
    body = _arxiv(c, FEED)
    assert (body["title"], body["kind"], body["origin"]) == ("Neural Machine Translation", "pdf", ARXIV)


def test_an_abstract_page_url_imports_the_paper(c):
    c.app.state.source_fetcher = ArxivFetcher((FIX / "no_outline.pdf").read_bytes(), FEED)
    body = c.post("/wiki/sources", headers=H("u1"), data={"url": "https://arxiv.org/abs/1409.0473"}).json()
    assert (body["kind"], body["origin"]) == ("pdf", ARXIV)


def test_arxiv_down_falls_through_to_the_url(c):
    assert _arxiv(c, None)["title"] == ARXIV


def test_the_title_he_gave_wins_without_asking_arxiv(c):
    assert _arxiv(c, FEED, title="我的论文")["title"] == "我的论文"
    assert not [u for u in c.app.state.source_fetcher.calls if "export.arxiv.org" in u]


def test_url_charset_kept_for_import(c):
    """A GBK page: the stored content type keeps charset=gbk, so the import decodes it."""
    html = ("<html><head><title>归并排序讲解</title></head><body><article><h1>归并排序讲解</h1>"
            "<p>" + "归并排序把数组分成两半，分别排好序，再把两个有序的半边合并起来。" * 6 +
            "</p></article></body></html>").encode("gbk")
    c.app.state.source_fetcher = FakeFetcher(html, ctype="text/html; charset=gbk")
    r = c.post("/wiki/sources", headers=H("u1"), data={"url": "https://example.com/gbk"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert progress(c, sid)[-1][0] == "done"
    from evowiki.sources import store
    with c.app.state.services.pool.connection() as conn:
        assert store.get_file(conn, "u1", sid)[1] == "text/html; charset=gbk"
        secs = store.sections(conn, "u1", sid)
    text = "".join(s.text for s in secs)
    assert "归并排序把数组分成两半" in text and "�" not in text


def test_url_unreachable_422(c):
    c.app.state.source_fetcher = FakeFetcher(b"", fail=True)
    r = c.post("/wiki/sources", headers=H("u1"), data={"url": "https://example.com/x"})
    assert r.status_code == 422
    assert r.json()["detail"] == MSG_URL


def test_internal_url_422(c):
    r = c.post("/wiki/sources", headers=H("u1"), data={"url": "http://127.0.0.1/secret"})
    assert r.status_code == 422
    assert r.json()["detail"] == MSG_URL


def test_other_user_gets_404(c):
    sid = upload(c).json()["id"]
    for path in ("", "/progress", "/file"):
        assert c.get(f"/wiki/sources/{sid}{path}", headers=H("u2")).status_code == 404
    assert c.get("/wiki/sources", headers=H("u2")).json() == []
    assert c.post(f"/wiki/sources/{sid}/uses", headers=H("u2"),
                  json={"use": "ask"}).status_code == 404
    assert c.delete(f"/wiki/sources/{sid}", headers=H("u2")).status_code == 404
    assert c.patch(f"/wiki/sources/{sid}", headers=H("u2"), json={"title": "x"}).status_code == 404


def test_file_returns_original(c):
    raw = (FIX / "outline.pdf").read_bytes()
    sid = upload(c).json()["id"]
    f = c.get(f"/wiki/sources/{sid}/file", headers=H("u1"))
    assert f.status_code == 200 and f.content == raw
    assert f.headers["content-type"] == "application/pdf"
    assert f.headers["content-disposition"].startswith("inline; filename*=UTF-8''")
    assert f.headers["x-content-type-options"] == "nosniff"


def test_delete_cancels_and_hides(c):
    sid = upload(c).json()["id"]
    cancelled = []
    runs = c.app.state.services.sources
    orig = runs.cancel

    def spy(uid, s):
        cancelled.append((uid, s))
        orig(uid, s)
    runs.cancel = spy
    assert c.delete(f"/wiki/sources/{sid}", headers=H("u1")).status_code == 204
    assert cancelled == [("u1", sid)]
    assert c.get("/wiki/sources", headers=H("u1")).json() == []
    assert c.get(f"/wiki/sources/{sid}/file", headers=H("u1")).status_code == 404
    assert c.get(f"/wiki/sources/{sid}", headers=H("u1")).status_code == 404
    assert c.delete(f"/wiki/sources/{sid}", headers=H("u1")).status_code == 404
    # a deleted source does not dedup: the same file imports anew
    assert upload(c).status_code == 201


def test_uses(c):
    sid = upload(c).json()["id"]
    r = c.post(f"/wiki/sources/{sid}/uses", headers=H("u1"),
               json={"use": "exam", "page_id": "p1", "session_id": "s1"})
    assert r.status_code == 201
    out = r.json()
    assert out["use"] == "exam" and out["page_id"] == "p1" and out["id"] and out["created_at"]
    uses = c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()["uses"]
    assert [u["id"] for u in uses] == [out["id"]]
    assert c.post(f"/wiki/sources/{sid}/uses", headers=H("u1"),
                  json={"use": "bogus"}).status_code == 422


def test_rename(c):
    sid = upload(c).json()["id"]
    r = c.patch(f"/wiki/sources/{sid}", headers=H("u1"), json={"title": "  新名字  "})
    assert r.status_code == 200 and r.json()["title"] == "新名字"
    assert c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()["title"] == "新名字"
    assert c.patch(f"/wiki/sources/{sid}", headers=H("u1"),
                   json={"title": "   "}).status_code == 422
    assert c.patch(f"/wiki/sources/{sid}", headers=H("u1"),
                   json={"title": "x" * 201}).status_code == 422
    assert c.patch("/wiki/sources/nope", headers=H("u1"), json={"title": "a"}).status_code == 404


def test_the_plugin_says_where_it_saved_the_original(c):
    sid = upload(c).json()["id"]
    r = c.patch(f"/wiki/sources/{sid}", headers=H("u1"), json={"origin": "EvoWiki/资料/a.pdf"})
    assert r.status_code == 200 and r.json()["origin"] == "EvoWiki/资料/a.pdf"
    assert c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()["title"] == r.json()["title"]
    assert c.patch(f"/wiki/sources/{sid}", headers=H("u2"), json={"origin": "x"}).status_code == 404


def test_max_bytes_default():
    assert config.max_bytes() == 50 * 1024 * 1024


def test_late_failure_surfaces_as_note(c, services):
    from evowiki.sources import store
    sid = upload(c).json()["id"]
    with services.pool.connection() as conn:
        store.set_progress(conn, "u1", sid, {"end": "error", "detail": "整理资料时出错了"})
    out = c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()
    assert out["status"] == "ready" and out["error"] is None
    assert out["note"] == "整理资料时出错了"


def test_text_over_starlette_field_limit_is_accepted(c):
    text = "# 长文\n\n" + "归并排序。" * 75_000        # about 1.1 MB of utf-8, over the 1 MB field cap
    r = c.post("/wiki/sources", headers=H("u1"), data={"text": text})
    assert r.status_code == 201, r.text


def test_text_over_cap_is_chinese_413(c, monkeypatch):
    monkeypatch.setenv("EVOWIKI_SOURCE_MAX_MB", "1")
    r = c.post("/wiki/sources", headers=H("u1"), data={"text": "字" * 500_000})   # 1.5 MB
    assert r.status_code == 413
    assert r.json()["detail"] == "文件太大了，上限 1 MB"


def test_content_length_rejected_before_body(c, monkeypatch):
    monkeypatch.setenv("EVOWIKI_SOURCE_MAX_MB", "1")
    r = c.post("/wiki/sources", headers={**H("u1"), "Content-Length": str(50 * 1024 * 1024),
                                         "Content-Type": "multipart/form-data; boundary=x"},
               content=b"")
    assert r.status_code == 413
    assert "文件太大了" in r.json()["detail"]


def test_patch_unknown_is_chinese_404_without_user_id(c):
    r = c.patch("/wiki/sources/nope", headers=H("u1"), json={"title": "a"})
    assert r.status_code == 404
    assert r.json()["detail"] == "找不到这份资料" and "u1" not in r.text


def test_rename_validation_is_chinese(c):
    sid = upload(c).json()["id"]
    r = c.patch(f"/wiki/sources/{sid}", headers=H("u1"), json={"title": " "})
    assert r.status_code == 422 and "名字不能为空" in r.text
    r = c.patch(f"/wiki/sources/{sid}", headers=H("u1"), json={"title": "x" * 201})
    assert r.status_code == 422 and "名字太长了" in r.text


def test_file_extension_from_mime(c):
    for name, ext in (("photo.png", ".png"),):
        sid = upload(c, name=name).json()["id"]
        cd = c.get(f"/wiki/sources/{sid}/file", headers=H("u1")).headers["content-disposition"]
        assert cd.endswith(ext)
    from evowiki.service.routes_sources import _EXT_BY_MIME, _mime
    assert _EXT_BY_MIME[_mime("image", "a.jpeg")] == ".jpg"
    assert _EXT_BY_MIME[_mime("image", "a.webp")] == ".webp"


def test_failed_start_marks_failed_and_503(c, services):
    class Boom:
        def start(self, job):
            raise RuntimeError("no runner")

    real = services.sources
    services.sources = Boom()
    r = upload(c)
    assert r.status_code == 503 and r.json()["detail"] == "资料没能开始整理"
    services.sources = real
    listed = c.get("/wiki/sources", headers=H("u1")).json()
    assert [(s["status"], s["error"]) for s in listed] == [("failed", "资料没能开始整理")]
    assert upload(c).status_code == 201      # failed rows do not dedup


def test_multipart_text_over_cap_is_chinese_413(c, monkeypatch):
    # a real multipart body whose declared length is understated: the parser limit fires
    monkeypatch.setenv("EVOWIKI_SOURCE_MAX_MB", "1")
    body = ("--b\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n"
            + "a" * 1_500_000 + "\r\n--b--\r\n").encode()
    r = c.post("/wiki/sources", content=body, headers={
        **H("u1"), "Content-Type": "multipart/form-data; boundary=b", "Content-Length": "100"})
    assert r.status_code == 413, r.text
    assert r.json()["detail"] == "文件太大了，上限 1 MB"


def test_multipart_file_over_cap_is_chinese_413(c, monkeypatch):
    monkeypatch.setenv("EVOWIKI_SOURCE_MAX_MB", "1")
    r = c.post("/wiki/sources", headers=H("u1"),
               files={"file": ("a.txt", b"a" * 1_200_000), "title": (None, "t")})
    assert r.status_code == 413 and "文件太大了" in r.json()["detail"]


def test_multipart_garbage_is_chinese_422(c):
    r = c.post("/wiki/sources", content=b"--b\r\nbroken", headers={
        **H("u1"), "Content-Type": "multipart/form-data; boundary=b"})
    assert r.status_code == 422 and r.json()["detail"] in (
        "上传的内容读不了，请重新添加", "文件、网址、文字三选一")


def test_detail_has_the_printed_page_offset_and_the_glossary(c):
    from evowiki.sources import store
    sid = upload(c).json()["id"]
    progress(c, sid)
    got = c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()
    assert got["page_offset"] is None and got["glossary"] == []
    with c.app.state.services.pool.connection() as conn:
        store.set_page_offset(conn, "u1", sid, 4)
        store.set_glossary(conn, "u1", sid, [{"term": "λ", "means": "特征值", "page": 3}])
    got = c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()
    assert got["page_offset"] == 4
    # an entry saved before 0101 has no section; no coverage recorded
    assert got["glossary"] == [{"term": "λ", "means": "特征值", "page": 3, "section_id": None,
                                "locator": None}]
    assert got["glossary_coverage"] is None
    coverage = {"whole": False, "head_chars": 800, "sent_chars": 1600, "total_chars": 9000,
                "cut_sections": 2}
    with c.app.state.services.pool.connection() as conn:
        store.set_glossary(conn, "u1", sid, [{"term": "λ", "means": "特征值", "page": 3,
                                             "section_id": "x1"}], coverage)
    got = c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()
    assert got["glossary"][0]["section_id"] == "x1" and got["glossary_coverage"] == coverage


def _ready(c, uid="u1"):
    sid = upload(c, uid).json()["id"]
    progress(c, sid, uid)
    return sid, c.get(f"/wiki/sources/{sid}", headers=H(uid)).json()["sections"]


def _seed_sections(c, sid, uid="u1"):
    """Four flat sections a, b, c, d (depth 0) in ord order, replacing what the import made."""
    from evowiki.sources import store
    from evowiki.sources.models import SectionRow
    rows = [SectionRow(section_id=f"{sid}-{k}", source_id=sid, parent_id=None, ord=i, depth=0,
                       title=k.upper(), page_from=i + 1, page_to=i + 1, text=k, tokens=1,
                       summary=None, read_by="text") for i, k in enumerate("abcd")]
    with c.app.state.services.pool.connection() as conn:
        store.replace_sections(conn, uid, sid, rows)
    return [r.section_id for r in rows]


def test_patch_sections_renames_and_relevels(c):
    sid, _ = _ready(c)
    a, b, cc, d = _seed_sections(c, sid)
    r = c.patch(f"/wiki/sources/{sid}/sections", headers=H("u1"), json=[
        {"id": b, "depth": 1}, {"id": cc, "depth": 2, "title": "  第三节 "}, {"id": d, "depth": 1}])
    assert r.status_code == 200, r.text
    got = {s["id"]: s for s in r.json()["sections"]}
    assert [got[k]["depth"] for k in (a, b, cc, d)] == [0, 1, 2, 1]
    assert [got[k]["parent_id"] for k in (a, b, cc, d)] == [None, a, b, a]
    assert got[cc]["title"] == "第三节" and got[a]["title"] == "A"
    again = c.get(f"/wiki/sources/{sid}", headers=H("u1")).json()["sections"]
    assert [s["id"] for s in again] == [a, b, cc, d]          # order and text untouched
    assert {s["id"]: s["parent_id"] for s in again} == {a: None, b: a, cc: b, d: a}
    # back up a level: c's parent is now a
    r = c.patch(f"/wiki/sources/{sid}/sections", headers=H("u1"), json=[{"id": cc, "depth": 1}])
    assert {s["id"]: s["parent_id"] for s in r.json()["sections"]}[cc] == a


def test_patch_sections_refusals(c):
    from evowiki.sources import store
    sid, _ = _ready(c)
    a, *_ = _seed_sections(c, sid)
    url = f"/wiki/sources/{sid}/sections"
    for bad in ([{"id": "nope", "title": "x"}], [{"id": a, "depth": 3}],
                [{"id": a, "depth": -1}], [{"id": a, "title": "  "}]):
        r = c.patch(url, headers=H("u1"), json=bad)
        assert r.status_code == 422, bad
    assert c.patch(url, headers=H("u2"), json=[{"id": a, "title": "x"}]).status_code == 404
    with c.app.state.services.pool.connection() as conn:
        store.set_status(conn, "u1", sid, status="reading")
    r = c.patch(url, headers=H("u1"), json=[{"id": a, "title": "x"}])
    assert r.status_code == 409 and r.json()["detail"] == "资料还在读，读完再改目录"
    with c.app.state.services.pool.connection() as conn:
        store.set_status(conn, "u1", sid, status="partial")
    assert c.patch(url, headers=H("u1"), json=[{"id": a, "title": "x"}]).status_code == 200
    assert c.delete(f"/wiki/sources/{sid}", headers=H("u1")).status_code == 204
    assert c.patch(url, headers=H("u1"), json=[{"id": a, "title": "x"}]).status_code == 404


def test_an_unnamed_pdf_takes_its_own_title_and_a_given_title_wins(c):
    """Follow-up B (2026-10-22, replaces final review #5's file-name-wins for PDFs): a PDF the
    user did not name takes its metadata Title over the file name; a title the user gave wins;
    titles are cleaned and capped at 80 characters."""
    data = (FIX / "outline.pdf").read_bytes()
    given = c.post("/wiki/sources", headers=H("u1"), files={"file": ("x.pdf", data)},
                   data={"title": "线性代数讲义"})
    assert given.status_code == 201 and given.json()["title"] == "线性代数讲义"
    for name in ("线性代数讲义.pdf", "document.pdf"):       # u2: the same file, not yet theirs
        unnamed = c.post("/wiki/sources", headers=H("u2"), files={"file": (name, data)})
        assert unnamed.status_code in (200, 201), unnamed.text
        got = c.get(f"/wiki/sources/{unnamed.json()['id']}", headers=H("u2")).json()
        assert got["title"] == "学习方法 Learning Methods"
    long = c.post("/wiki/sources", headers=H("u1"),
                  data={"text": "# 一\n\n正文。", "title": "长\x07" * 100})
    assert long.status_code == 201 and long.json()["title"] == "长" * 80


def test_a_named_upload_keeps_its_name_and_an_untitled_pdf_falls_back_to_the_file_name(c):
    assert upload(c, title="我的讲义").json()["title"] == "我的讲义"
    assert upload(c, name="no_outline.pdf").json()["title"] == "no_outline"


def test_size_and_title_validation_expose_safe_fields_in_english(c, monkeypatch):
    signup(c, "english", lang="en")
    sid = upload(c, uid="english").json()["id"]
    invalid = c.patch(f"/wiki/sources/{sid}", headers=H("english"), json={"title": " "})
    assert invalid.json() == {"code": "source_title_empty", "params": {"field": "title"},
                              "detail": "A material name cannot be empty."}
    secret = "private-title-never-echo" * 20
    too_long = c.patch(f"/wiki/sources/{sid}", headers=H("english"), json={"title": secret})
    assert too_long.json() == {"code": "source_title_too_long", "params": {"field": "title", "limit": 200},
                               "detail": "A material name can contain at most 200 characters."}
    assert secret not in too_long.text
    monkeypatch.setenv("EVOWIKI_SOURCE_MAX_MB", "1")
    too_big = c.post("/wiki/sources", headers=H("english"), data={"text": "x" * 1_100_000})
    assert too_big.status_code == 413
    assert too_big.json() == {"code": "upload_too_large", "params": {"limit": 1},
                              "detail": "The file is too large. The limit is 1 MB."}
