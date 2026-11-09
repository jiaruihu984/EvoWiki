import http.server
import socket
import threading

import pytest

from evowiki.assistant.tools import fetch_url as fetch_mod
from evowiki.assistant.tools.fetch_url import (
    Fetched,
    FetchError,
    FetchUrlTool,
    UrlBlocked,
    UrlFetcher,
    _plain_html_to_text,
    check_url,
    html_to_text,
    ip_allowed,
)

PUBLIC = lambda host: ["93.184.216.34"]  # example.com's address; never dialled


@pytest.mark.parametrize("url", [
    "http://localhost/", "http://LOCALHOST:8080/x", "http://foo.localhost/",
    "http://127.0.0.1/", "http://127.1.2.3/", "http://[::1]/", "http://0.0.0.0/",
    "http://10.0.0.5/", "http://10.255.1.1:8080/", "http://192.168.1.1/",
    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://[::ffff:127.0.0.1]/", "http://[fd00::1]/",
    "file:///etc/passwd", "file://C:/Windows/win.ini", "ftp://example.com/",
    "gopher://example.com/", "http://user:pw@example.com/", "http:///nohost",
    "http://example.com:22/",
])
def test_blocked_urls(url):
    with pytest.raises(UrlBlocked):
        check_url(url, resolve=PUBLIC)


def test_hostname_resolving_to_private_is_blocked():
    with pytest.raises(UrlBlocked):
        check_url("http://intranet.example.com/", resolve=lambda h: ["93.184.216.34", "10.0.0.1"])


def test_public_url_allowed():
    assert check_url("https://example.com/a?b=1", resolve=PUBLIC).startswith("https://example.com")
    assert ip_allowed("8.8.8.8") and not ip_allowed("192.168.0.1")


# --- holes beyond the brief's list -------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://localhost./", "http://foo.local./", "http://LocalHost.:443/",   # trailing-dot names
    "http://2130706433/", "http://0x7f.1/", "http://0177.0.0.1/", "http://127.1/",  # inet_aton forms
    "http://[64:ff9b::7f00:1]/", "http://[::7f00:1]/", "http://[::a00:1]/",  # embedded IPv4
    "http://[2002:7f00:1::]/", "http://[fe80::1%25eth0]/", "http://[::1/",
    "http://example.com:99999/", "http://example.com:0/", "HTTP://[::]/",
])
def test_blocked_url_holes(url):
    with pytest.raises(UrlBlocked):
        check_url(url, resolve=PUBLIC)


@pytest.mark.parametrize("ip", ["64:ff9b::7f00:1", "::7f00:1", "::ffff:10.0.0.1", "fe80::1%eth0",
                                "100.64.0.1", "224.0.0.1", "ff02::1", "not-an-ip", ""])
def test_ip_allowed_rejects_embedded_and_garbage(ip):
    assert not ip_allowed(ip)


def test_ip_allowed_keeps_public_v6():
    assert ip_allowed("2606:4700:4700::1111") and ip_allowed("64:ff9b::808:808")


def test_resolver_garbage_is_blocked_not_crash():
    with pytest.raises(UrlBlocked):
        check_url("http://x.example.com/", resolve=lambda h: ["nonsense"])
    with pytest.raises(UrlBlocked):
        check_url("http://x.example.com/", resolve=lambda h: [])


def test_html_to_text():
    title, text = html_to_text(
        "<html><head><title>Z 分数</title><style>p{}</style></head><body>"
        "<script>alert(1)</script><h1>标准分</h1><p>z = (x-μ)/σ</p><ul><li>一</li><li>二</li>"
        "</ul></body></html>")
    assert title == "Z 分数"
    assert "alert" not in text and "p{}" not in text
    assert text.splitlines() == ["标准分", "z = (x-μ)/σ", "一", "二"]


class FakeFetcher:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def fetch(self, url):
        if self.error:
            raise self.error
        return self.result


def test_tool_extracts_text_and_source(ctx):
    f = FakeFetcher(Fetched(final_url="https://e.com/z", content_type="text/html; charset=utf-8",
                            body="<title>T</title><p>正文</p>".encode(), truncated=False))
    r = FetchUrlTool(f).run({"url": "https://e.com/z"}, ctx)
    assert r.error is None and "正文" in r.content
    assert r.sources[0].url == "https://e.com/z" and r.sources[0].title == "T"
    assert FetchUrlTool(f).spec.effect == "network"


def test_tool_decodes_declared_charset(ctx):
    f = FakeFetcher(Fetched(final_url="https://e.com", content_type='text/plain; charset="gbk"',
                            body="中文".encode("gbk"), truncated=False))
    r = FetchUrlTool(f).run({"url": "https://e.com"}, ctx)
    assert "中文" in r.content and r.sources[0].title == "https://e.com"


_ARTICLE = ("<html><head><title>特征值</title></head><body><nav>首页 | 登录 | 注册</nav>"
            "<article><h1>特征值</h1>" + "<p>矩阵的特征值满足特征方程，它描述线性变换的伸缩方向。</p>" * 4 +
            "<table><tr><th>名称</th><th>值</th></tr><tr><td>迹</td><td>特征值之和</td></tr></table>"
            "</article><footer>版权所有 备案号 12345</footer></body></html>")


def test_html_uses_trafilatura_main_content_with_tables():
    title, text = html_to_text(_ARTICLE)
    assert title == "特征值"
    assert "特征方程" in text and "迹" in text and "特征值之和" in text      # table kept
    assert "备案号" not in text                                              # boilerplate dropped


def test_html_falls_back_to_plain_extractor_when_trafilatura_finds_nothing(monkeypatch):
    import trafilatura
    monkeypatch.setattr(trafilatura, "extract", lambda *a, **k: None)
    title, text = html_to_text(_ARTICLE)
    assert title == "特征值" and "备案号" in text      # the plain extractor keeps everything


def test_html_falls_back_when_trafilatura_raises(monkeypatch):
    import trafilatura

    def boom(*a, **k):
        raise ValueError("lxml")
    monkeypatch.setattr(trafilatura, "extract", boom)
    assert html_to_text("<p>正文</p>") == ("", "正文")


def test_html_both_extractors_empty_stays_empty():
    assert html_to_text("<script>x</script>") == ("", "")


def test_tool_html_gbk_page_declared_only_in_meta(ctx):
    body = ("<html><head><meta http-equiv=\"Content-Type\" content=\"text/html; charset=gb2312\">"
            "<title>中文标题</title></head><body><p>这是一个没有在响应头声明编码的页面。</p></body></html>"
            ).encode("gbk")
    f = FakeFetcher(Fetched(final_url="https://e.com/g", content_type="text/html", body=body))
    r = FetchUrlTool(f).run({"url": "https://e.com/g"}, ctx)
    assert r.error is None and "这是一个没有在响应头声明编码的页面" in r.content
    assert r.sources[0].title == "中文标题"


def test_tool_html_meta_charset_form_and_header_wins(ctx):
    meta = "<meta charset=gbk><title>标题</title><p>中文正文内容在此。</p>".encode("gbk")
    r = FetchUrlTool(FakeFetcher(Fetched(final_url="https://e.com", content_type="text/html",
                                         body=meta))).run({"url": "https://e.com"}, ctx)
    assert "中文正文内容在此" in r.content
    # the header's charset beats a (wrong) meta
    utf = "<meta charset=gbk><title>标题</title><p>中文正文内容在此。</p>".encode()
    r = FetchUrlTool(FakeFetcher(Fetched(final_url="https://e.com",
                                         content_type="text/html; charset=utf-8",
                                         body=utf))).run({"url": "https://e.com"}, ctx)
    assert "中文正文内容在此" in r.content


def test_tool_marks_truncated_page(ctx):
    f = FakeFetcher(Fetched(final_url="https://e.com", content_type="text/plain",
                            body=b"short", truncated=True))
    r = FetchUrlTool(f).run({"url": "https://e.com"}, ctx)
    assert r.content.endswith("（页面过大，只读了前 2 MB）")


def test_tool_clips_long_pages(ctx):
    f = FakeFetcher(Fetched(final_url="https://e.com", content_type="text/plain",
                            body=("字" * 50000).encode(), truncated=True))
    r = FetchUrlTool(f).run({"url": "https://e.com"}, ctx)
    assert len(r.content) <= 6000 and "截断" in r.content
    assert r.content.endswith("（页面过大，只读了前 2 MB）")


def test_tool_errors(ctx):
    assert FetchUrlTool(FakeFetcher(error=UrlBlocked("内网"))).run({"url": "http://x.com"}, ctx).error
    assert FetchUrlTool(FakeFetcher(error=FetchError("HTTP 404"))).run({"url": "http://x.com"}, ctx).error
    assert FetchUrlTool(FakeFetcher(error=RuntimeError("?"))).run({"url": "http://x.com"}, ctx).error
    assert FetchUrlTool(FakeFetcher()).run({"url": ""}, ctx).error


@pytest.mark.parametrize("url", ["http://localhost/", "http://10.1.2.3/", "http://192.168.0.10/",
                                 "file:///etc/passwd"])
def test_default_tool_refuses_without_network(url, ctx):
    r = FetchUrlTool().run({"url": url}, ctx)
    assert r.error and "不允许" in r.content


@pytest.fixture
def loopback_server():
    class H(http.server.BaseHTTPRequestHandler):
        hits = 0

        def do_GET(self):
            H.hits += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"secret")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, H
    srv.shutdown()
    srv.server_close()


def test_peer_check_stops_dns_rebinding(loopback_server):
    """The pre-check sees a public address, but DNS 'changes its answer' before connecting
    (connect_host) and the socket lands on 127.0.0.1: the connect-time check must refuse
    before any request is sent."""
    srv, handler = loopback_server
    port = srv.server_address[1]
    fetcher = UrlFetcher(resolve=PUBLIC, allowed_ports=None,
                         connect_host=lambda host: "127.0.0.1")
    with pytest.raises(UrlBlocked) as ei:
        fetcher.fetch(f"http://rebind.test:{port}/")
    assert handler.hits == 0
    assert str(ei.value) == "实际连接的地址不是公网地址"     # generic: no internal IP echoed
    assert "127.0.0.1" not in str(ei.value)


# --- raw TCP listener: proves not a single byte (HTTP request or TLS ClientHello) is sent -----

@pytest.fixture
def raw_listener():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    received: list[bytes] = []
    accepted = threading.Event()

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        accepted.set()
        conn.settimeout(1.0)
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                received.append(data)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield srv.getsockname()[1], received, accepted, t
    srv.close()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_rebinding_sends_zero_bytes_http_and_https(scheme, raw_listener):
    port, received, accepted, t = raw_listener
    fetcher = UrlFetcher(resolve=PUBLIC, allowed_ports=None,
                         connect_host=lambda host: "127.0.0.1")
    with pytest.raises(UrlBlocked):
        fetcher.fetch(f"{scheme}://rebind.test:{port}/")
    assert accepted.wait(2)          # the TCP connect did happen ...
    t.join(3)
    assert received == []            # ... but nothing was written on it (no request, no TLS hello)


def test_default_connect_path_checks_peer(raw_listener, monkeypatch):
    """Without connect_host the real dial is peer-checked too: even if the URL pre-check were
    fooled into passing, a socket that lands on 127.0.0.1 sends nothing."""
    port, received, accepted, t = raw_listener
    monkeypatch.setattr(fetch_mod, "check_url", lambda url, *a, **k: url)
    fetcher = UrlFetcher(resolve=PUBLIC, allowed_ports=None)
    with pytest.raises(UrlBlocked):
        fetcher.fetch(f"http://127.0.0.1:{port}/")
    assert accepted.wait(2)
    t.join(3)
    assert received == []


# --- full flows against a loopback server, with only the peer check relaxed --------------------

@pytest.fixture
def scripted_server():
    """routes: path -> (status, headers, body). Records every request path."""
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
    seen: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            status, headers, body = routes.get(self.path, (404, {}, b""))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], routes, seen
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def lab(scripted_server, monkeypatch):
    """A fetcher whose host 'site.test' dials the loopback server; only the *peer* check is
    relaxed to accept 127.0.0.1, every URL check stays real."""
    port, routes, seen = scripted_server
    monkeypatch.setattr(fetch_mod, "_peer_allowed", lambda ip: ip == "127.0.0.1")
    fetcher = UrlFetcher(resolve=PUBLIC, allowed_ports=None, max_bytes=100,
                         connect_host=lambda host: "127.0.0.1")
    return f"http://site.test:{port}", routes, seen, fetcher


def test_fetch_ok_and_size_cap(lab):
    base, routes, _seen, fetcher = lab
    routes["/a"] = (200, {"Content-Type": "text/html; charset=utf-8"}, b"<p>hi</p>")
    routes["/big"] = (200, {"Content-Type": "text/plain"}, b"x" * 500)
    f = fetcher.fetch(base + "/a")
    assert f.body == b"<p>hi</p>" and not f.truncated and f.final_url == base + "/a"
    big = fetcher.fetch(base + "/big")
    assert big.truncated and len(big.body) == 100


def test_fetch_follows_relative_redirect(lab):
    base, routes, seen, fetcher = lab
    routes["/r"] = (302, {"Location": "/a"}, b"")
    routes["/a"] = (200, {"Content-Type": "text/plain"}, b"ok")
    f = fetcher.fetch(base + "/r")
    assert f.body == b"ok" and f.final_url == base + "/a" and seen == ["/r", "/a"]


@pytest.mark.parametrize("target", ["http://10.0.0.1/", "http://localhost/", "file:///etc/passwd",
                                    "http://169.254.169.254/latest/meta-data/",
                                    "http://user:pw@example.com/"])
def test_redirect_to_blocked_target_is_refused(lab, target):
    base, routes, seen, fetcher = lab
    routes["/r"] = (301, {"Location": target}, b"")
    with pytest.raises(UrlBlocked):
        fetcher.fetch(base + "/r")
    assert seen == ["/r"]


def test_redirect_loop_limited(lab):
    base, routes, seen, fetcher = lab
    routes["/loop"] = (307, {"Location": "/loop"}, b"")
    with pytest.raises(FetchError, match="重定向太多"):
        fetcher.fetch(base + "/loop")
    assert len(seen) == 4          # first request + max_redirects (3)


@pytest.mark.parametrize("path,status,headers,match", [
    ("/nf", 404, {}, "HTTP 404"),
    ("/err", 500, {}, "HTTP 500"),
    ("/img", 200, {"Content-Type": "image/png"}, "不支持的内容类型"),
    ("/r0", 302, {}, "HTTP 302"),        # redirect without Location
])
def test_fetch_errors(lab, path, status, headers, match):
    base, routes, _seen, fetcher = lab
    routes[path] = (status, headers, b"x")
    with pytest.raises(FetchError, match=match):
        fetcher.fetch(base + path)


def test_system_proxy_is_ignored(lab, monkeypatch):
    base, routes, _seen, _fetcher = lab
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://203.0.113.9:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    routes["/a"] = (200, {"Content-Type": "text/plain"}, b"direct")
    fresh = UrlFetcher(resolve=PUBLIC, allowed_ports=None, connect_host=lambda h: "127.0.0.1")
    assert fresh.fetch(base + "/a").body == b"direct"


def test_unreachable_is_fetch_error(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                        # nothing listens there now
    monkeypatch.setattr(fetch_mod, "_peer_allowed", lambda ip: True)
    fetcher = UrlFetcher(resolve=PUBLIC, allowed_ports=None, timeout=2,
                         connect_host=lambda h: "127.0.0.1")
    with pytest.raises(FetchError):
        fetcher.fetch(f"http://site.test:{port}/")


# --- fix round 1 --------------------------------------------------------------------------------

def test_html_without_closing_head_keeps_body():
    title, text = _plain_html_to_text("<html><head><title>T</title><meta charset=utf-8>"
                               "<body><p>正文</p></body></html>")
    assert title == "T" and text == "正文"


def test_html_head_text_ignored_but_block_tag_ends_head():
    title, text = _plain_html_to_text("<head><title>T</title><p>段落")
    assert title == "T" and text == "段落"


def test_html_svg_title_not_appended_to_page_title():
    title, text = _plain_html_to_text("<head><title>页面</title></head><body><svg><title>图标</title>"
                               "</svg><p>x</p><title>第二个</title></body>")
    assert title == "页面" and "图标" not in text and text == "x"


def test_html_title_in_body_counts_when_none_before():
    title, _ = _plain_html_to_text("<body><svg><title>图标</title></svg><title>真标题</title><p>x</p>")
    assert title == "真标题"


@pytest.fixture
def drip_server():
    """Declares 1000 bytes, then drips one byte every 0.15 s (each recv well under the timeout)."""
    stop = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.flush()
            for _ in range(1000):
                if stop.wait(0.15):
                    return
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    stop.set()
    srv.shutdown()
    srv.server_close()


def test_slow_drip_hits_whole_read_deadline(drip_server, monkeypatch):
    import time
    monkeypatch.setattr(fetch_mod, "_peer_allowed", lambda ip: ip == "127.0.0.1")
    fetcher = UrlFetcher(resolve=PUBLIC, allowed_ports=None, timeout=0.4,
                         connect_host=lambda h: "127.0.0.1")
    t0 = time.monotonic()
    with pytest.raises(FetchError, match="读取超时"):
        fetcher.fetch(f"http://site.test:{drip_server}/")
    assert time.monotonic() - t0 < 2.5        # deadline is 3 x 0.4 s = 1.2 s


def test_accept_param_controls_pdf(lab):
    base, routes, _seen, fetcher = lab
    routes["/f.pdf"] = (200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 x")
    with pytest.raises(FetchError, match="不支持的内容类型"):
        fetcher.fetch(base + "/f.pdf")                      # default: still refused
    wide = UrlFetcher(resolve=PUBLIC, allowed_ports=None, max_bytes=100,
                      connect_host=lambda host: "127.0.0.1",
                      accept=fetch_mod.TEXT_TYPES + ("application/pdf",))
    assert wide.fetch(base + "/f.pdf").body.startswith(b"%PDF")


def test_check_url_encodes_a_pasted_chinese_path_once():
    resolve = lambda _host: ["93.184.216.34"]
    assert fetch_mod.check_url("https://zh.wikipedia.org/wiki/行列式?a=值&b=1", resolve) == (
        "https://zh.wikipedia.org/wiki/%E8%A1%8C%E5%88%97%E5%BC%8F?a=%E5%80%BC&b=1")
    done = "https://zh.wikipedia.org/wiki/%E8%A1%8C"
    assert fetch_mod.check_url(done, resolve) == done
