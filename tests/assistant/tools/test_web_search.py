import http.client
import io
import urllib.error

import pytest

from evowiki.assistant.tools import web_search as ws
from evowiki.assistant.tools.web_search import (
    TAVILY_URL,
    SearchError,
    SearchHit,
    TavilyProvider,
    WebSearchTool,
)


class FakeProvider:
    def __init__(self, hits=(), error=None):
        self.hits, self.error = list(hits), error

    def search(self, query, limit):
        if self.error:
            raise SearchError(self.error)
        return self.hits[:limit]


def test_results_with_sources(ctx):
    p = FakeProvider([SearchHit(title="Standard score", url="https://en.wikipedia.org/wiki/Standard_score",
                                snippet="In statistics, the standard score is ...")])
    r = WebSearchTool(p).run({"query": "z-score"}, ctx)
    assert r.error is None and "[1] Standard score" in r.content
    assert r.content.startswith("[1] ")
    assert r.sources[0].url.startswith("https://en.wikipedia.org")
    assert WebSearchTool(p).spec.effect == "network"


def test_no_results_and_errors(ctx):
    assert "没有搜到" in WebSearchTool(FakeProvider()).run({"query": "x"}, ctx).content
    r = WebSearchTool(FakeProvider(error="未配置 TAVILY_API_KEY")).run({"query": "x"}, ctx)
    assert r.error and "TAVILY_API_KEY" in r.error
    assert WebSearchTool(FakeProvider()).run({"query": ""}, ctx).error


def test_long_results_clipped(ctx):
    hits = [SearchHit(title="t" * 400, url=f"https://e.com/{i}", snippet="s" * 5000)
            for i in range(8)]
    r = WebSearchTool(FakeProvider(hits)).run({"query": "x", "limit": 8}, ctx)
    assert len(r.content) <= 4000 and all(len(s.title) <= 300 for s in r.sources)


def test_tavily_missing_key_is_explicit():
    with pytest.raises(SearchError, match="TAVILY_API_KEY"):
        TavilyProvider.from_env({}).search("x", 3)


def test_tavily_request_and_parsing():
    seen = {}

    def post(url, headers, body, timeout):
        seen.update(url=url, headers=headers, body=body, timeout=timeout)
        return {"results": [
            {"title": "A", "url": "https://a.com", "content": "aa"},
            {"title": "", "url": "https://b.com", "content": "no title"},
            {"title": "C", "url": "ftp://c.com", "content": "bad scheme"},
            {"title": "D", "url": "http://d.com", "content": "d" * 1000},
        ]}

    hits = TavilyProvider("k-123", post=post).search("z-score", 4)
    assert seen["url"] == TAVILY_URL and seen["headers"]["Authorization"] == "Bearer k-123"
    assert seen["body"] == {"query": "z-score", "max_results": 4, "search_depth": "basic"}
    assert [h.title for h in hits] == ["A", "D"] and len(hits[1].snippet) <= 300


def test_tavily_bad_response_is_search_error():
    for bad in ({}, {"results": "x"}, None, [1]):
        with pytest.raises(SearchError):
            TavilyProvider("k", post=lambda *a, b=bad: b).search("x", 3)


class _Opener:
    def __init__(self, outcome):
        self.outcome, self.requests = outcome, []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return io.BytesIO(self.outcome)


@pytest.mark.parametrize("outcome", [
    urllib.error.HTTPError(TAVILY_URL, 401, "Bearer secret-key", {}, None),
    urllib.error.HTTPError(TAVILY_URL, 302, "Bearer secret-key", {}, None),
    urllib.error.URLError("secret-key"),
    TimeoutError("secret-key"),
    http.client.IncompleteRead(b"secret-key"),
    b"not json secret-key",
])
def test_default_transport_errors_are_search_errors_without_key(monkeypatch, outcome):
    opener = _Opener(outcome)
    monkeypatch.setattr(ws, "_build_opener", lambda: opener)
    with pytest.raises(SearchError) as ei:
        TavilyProvider("secret-key").search("x", 3)
    assert "secret-key" not in str(ei.value)
    assert opener.requests and opener.requests[0].full_url == TAVILY_URL


def test_default_transport_success(monkeypatch):
    body = b'{"results": [{"title": "A", "url": "https://a.com", "content": "aa"}]}'
    monkeypatch.setattr(ws, "_build_opener", lambda: _Opener(body))
    assert [h.title for h in TavilyProvider("k").search("x", 3)] == ["A"]


def test_default_opener_does_not_follow_redirects():
    from urllib.request import HTTPRedirectHandler, Request
    handlers = [h for h in ws._build_opener().handlers if isinstance(h, HTTPRedirectHandler)]
    assert handlers and all(
        h.redirect_request(Request(TAVILY_URL), None, 302, "Found", {}, "https://evil.example/")
        is None for h in handlers)


def test_sources_only_for_hits_shown(ctx):
    hits = [SearchHit(title="t" * 300, url=f"https://e.com/{i}", snippet="s" * 300)
            for i in range(8)]
    r = WebSearchTool(FakeProvider(hits)).run({"query": "x", "limit": 8}, ctx)
    assert r.error is None and len(r.content) <= 4000
    assert 0 < len(r.sources) < 8
    assert all(s.url in r.content for s in r.sources)
    assert f"https://e.com/{len(r.sources)}" not in r.content


def test_tool_cleans_any_provider_hits(ctx):
    hits = [SearchHit(title="", url="https://a.com", snippet="no title"),
            SearchHit(title="   ", url="https://b.com"),
            SearchHit(title="C", url="javascript:alert(1)"),
            SearchHit(title="D", url="https://d.com", snippet="a\n\n  b " + "x" * 1000)]
    r = WebSearchTool(FakeProvider(hits)).run({"query": "x", "limit": 8}, ctx)
    assert r.error is None and [s.title for s in r.sources] == ["D"]
    assert "[1] D\nhttps://d.com\na b " in r.content and len(r.content) < 400


def test_limit_field_is_described():
    props = WebSearchTool(FakeProvider()).spec.parameters["properties"]
    assert props["limit"].get("description")


def test_whitespace_query_is_argument_error(ctx):
    r = WebSearchTool(FakeProvider()).run({"query": "   \n "}, ctx)
    assert r.error and "query" in r.error
