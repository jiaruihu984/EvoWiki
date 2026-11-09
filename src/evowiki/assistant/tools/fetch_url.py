"""fetch_url: one web page as plain text, clipped (Claude Code `WebFetch`, without the
summarising model call). SSRF guard in two layers: the URL's host must resolve only to public
addresses, and the socket's actual peer must be public too (stops DNS rebinding) - checked on the
bare TCP connection, before a single request or TLS byte is written. Redirects are followed by
hand and re-checked; no system proxy (the peer would always be the proxy); http/https only."""

import http.client
import ipaddress
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import quote, urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import (
    FETCH_CHARS,
    TITLE_CHARS,
    clip,
    fail,
    run_guarded,
    spec,
)
from evowiki.agent.tools import Source, ToolContext, ToolResult

DESCRIPTION = ("读取一个网页（http/https）的正文纯文本，最多 6000 字。用户给了链接、或搜索摘要不够"
               "需要看原文时用。不能访问本机和内网地址。返回标题、最终地址和正文。")

DEFAULT_PORTS = frozenset({80, 443, 8080, 8443})
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown",
              "application/json")
REDIRECTS = {301, 302, 303, 307, 308}
TRUNCATED_NOTE = "\n（页面过大，只读了前 2 MB）"
_BAD_SUFFIXES = (".localhost", ".local", ".internal")
# Legacy inet_aton spellings (2130706433, 0x7f.1, 0177.0.0.1, 127.1): the OS resolver turns them
# into addresses, so they are refused outright rather than judged as names.
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+)){0,3}$")
# IPv6 ranges that carry an IPv4 address in their low 32 bits and can reach it (IPv4-compatible
# ::/96, NAT64 well-known prefix): judge the embedded IPv4 address, not the IPv6 wrapper.
_V4_EMBEDDING = (ipaddress.IPv6Network("::/96"), ipaddress.IPv6Network("64:ff9b::/96"))
_READ_BUDGET = 3                 # whole-body read deadline, in multiples of the socket timeout

Resolver = Callable[[str], Sequence[str]]


class UrlBlocked(Exception):
    pass


class FetchError(Exception):
    pass


def _resolve(host: str) -> Sequence[str]:
    try:
        return sorted({str(ai[4][0]) for ai in socket.getaddrinfo(host, None)})
    except (socket.gaierror, UnicodeError) as e:
        raise FetchError(f"域名解析失败：{host}") from e


def _public(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return addr.is_global and not addr.is_multicast


def ip_allowed(ip: str) -> bool:
    """True only for a public unicast address. Unparsable input is not allowed."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return _public(addr.ipv4_mapped)
        if any(addr in net for net in _V4_EMBEDDING):
            return _public(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
        if addr.sixtofour is not None and not _public(addr.sixtofour):
            return False
        if addr.teredo is not None and not all(_public(a) for a in addr.teredo):
            return False
    return _public(addr)


_URL_SAFE = "/%:@!$&'()*+,;=~-._"


def check_url(url: str, resolve: Resolver = _resolve,
              allowed_ports: frozenset[int] | None = DEFAULT_PORTS) -> str:
    """The URL, normalised, if it may be fetched; otherwise UrlBlocked saying why."""
    try:
        parts = urlsplit(url.strip())
        hostname = parts.hostname
        port = parts.port
    except ValueError as e:
        raise UrlBlocked("地址格式不合法") from e
    if parts.scheme.lower() not in ("http", "https"):
        raise UrlBlocked(f"只允许 http/https，不允许 {parts.scheme or '无协议'}")
    host = (hostname or "").lower().rstrip(".")
    if not host:
        raise UrlBlocked("地址里没有主机名")
    if parts.username is not None or parts.password is not None:
        raise UrlBlocked("地址里不能带用户名密码")
    if port == 0 or (allowed_ports is not None and port is not None
                     and port not in allowed_ports):
        raise UrlBlocked(f"不允许端口 {port}")
    if host == "localhost" or host.endswith(_BAD_SUFFIXES):
        raise UrlBlocked(f"不允许访问本机或内网名字 {host}")
    try:
        ips: Sequence[str] = [str(ipaddress.ip_address(host))]   # literal IP: judge it directly
    except ValueError:
        if _NUMERIC_HOST.match(host):
            raise UrlBlocked(f"不允许非标准写法的 IP 地址 {host}") from None
        ips = resolve(host)
    if not ips or not all(ip_allowed(ip) for ip in ips):
        raise UrlBlocked(f"{host} 指向本机或内网地址")
    # 「…/wiki/行列式」 as pasted: the request line takes ASCII only; what is encoded already stays
    return parts._replace(path=quote(parts.path, safe=_URL_SAFE),
                          query=quote(parts.query, safe=_URL_SAFE + "?")).geturl()


class Fetched(BaseModel):
    model_config = ConfigDict(frozen=True)

    final_url: str
    content_type: str
    body: bytes
    truncated: bool = False


class Fetcher(Protocol):
    def fetch(self, url: str) -> Fetched: ...


def _peer_allowed(ip: str) -> bool:
    """The connect-time check (a seam so tests can let a loopback server through)."""
    return ip_allowed(ip)


def _guarded_connections(connect_host: Callable[[str], str] | None):
    def open_socket(conn: http.client.HTTPConnection) -> None:
        # Our own dial instead of HTTPConnection.connect: that one would write a CONNECT line
        # (proxy tunnel) before we could look at the peer. Proxies are disabled anyway.
        if getattr(conn, "_tunnel_host", None):
            raise UrlBlocked("不允许经代理连接")
        target = conn.host if connect_host is None else connect_host(conn.host)
        sock = socket.create_connection((target, conn.port), conn.timeout,
                                       conn.source_address)
        try:
            peer = str(sock.getpeername()[0])
        except OSError:
            sock.close()
            raise
        if not _peer_allowed(peer):
            sock.close()
            raise UrlBlocked("实际连接的地址不是公网地址")   # don't echo internal IPs
        conn.sock = sock

    class Http(http.client.HTTPConnection):
        def connect(self) -> None:
            open_socket(self)

    class Https(http.client.HTTPSConnection):
        def connect(self) -> None:
            open_socket(self)                       # TCP only; TLS starts after the check
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)

    return Http, Https


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Returning None makes urllib raise the 3xx as an HTTPError; `UrlFetcher.fetch` follows it
    after re-checking the target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrlFetcher:
    """The real fetcher. `allowed_ports=None` and `connect_host` exist for tests only: no port
    limit (loopback test servers use random ports), and a host → dial-address map that simulates
    DNS changing its answer after the pre-check."""

    def __init__(self, *, resolve: Resolver = _resolve,
                 allowed_ports: frozenset[int] | None = DEFAULT_PORTS,
                 max_bytes: int = 2_000_000, timeout: float = 10.0, max_redirects: int = 3,
                 connect_host: Callable[[str], str] | None = None,
                 accept: tuple[str, ...] = TEXT_TYPES):
        self._resolve, self._ports = resolve, allowed_ports
        self._accept = accept
        self._max, self._timeout, self._redirects = max_bytes, timeout, max_redirects
        http_cls, https_cls = _guarded_connections(connect_host)

        class H(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(http_cls, req)

        class S(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(https_cls, req, context=self._context)

        # A bare OpenerDirector: no ProxyHandler from the environment (ProxyHandler({}) is the
        # explicit "none"), no FTP/file/data handlers, no automatic redirects, no cookies.
        self._opener = urllib.request.OpenerDirector()
        for h in (urllib.request.ProxyHandler({}), H(), S(), _NoRedirect(),
                  urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor(),
                  urllib.request.UnknownHandler()):
            self._opener.add_handler(h)

    def _open(self, url: str) -> Any:
        req = urllib.request.Request(url, headers={
            "User-Agent": "EvoWiki-Assistant/1",
            "Accept": ",".join(self._accept)})
        try:
            return self._opener.open(req, timeout=self._timeout)
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as e:
            if isinstance(e.reason, UrlBlocked):
                raise e.reason from None
            raise FetchError(f"连接失败：{type(e.reason).__name__}") from None
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise FetchError(f"连接失败：{type(e).__name__}") from None

    def _read(self, resp: Any) -> bytes:
        deadline = time.monotonic() + self._timeout * _READ_BUDGET
        chunks: list[bytes] = []
        size = 0
        try:
            while size <= self._max:
                # read1: at most one recv per call (also for chunked bodies), so the deadline
                # is checked after every recv; read(n) would block until n bytes or EOF.
                chunk = resp.read1(min(65536, self._max + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if time.monotonic() > deadline:
                    raise FetchError("读取超时")
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f"读取中断：{type(e).__name__}") from None
        return b"".join(chunks)

    def fetch(self, url: str) -> Fetched:
        current = check_url(url, self._resolve, self._ports)
        for _ in range(self._redirects + 1):
            try:
                resp = self._open(current)
            except urllib.error.HTTPError as e:
                location = e.headers.get("Location") if e.headers is not None else None
                e.close()
                if e.code in REDIRECTS and location:
                    current = check_url(urljoin(current, location), self._resolve, self._ports)
                    continue
                raise FetchError(f"HTTP {e.code}") from None
            with resp:
                ctype = resp.headers.get("Content-Type", "")
                if ctype.split(";")[0].strip().lower() not in self._accept:
                    raise FetchError(f"不支持的内容类型 {ctype or '未知'}")
                body = self._read(resp)
            return Fetched(final_url=current, content_type=ctype, body=body[:self._max],
                           truncated=len(body) > self._max)
        raise FetchError("重定向太多")


log = logging.getLogger(__name__)

_SKIP = {"script", "style", "noscript", "svg", "template"}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section",
          "article", "pre", "blockquote", "ul", "ol", "table", "header", "footer", "main"}


class _Text(HTMLParser):
    """<head> text is dropped (except the title) until </head>, <body> or the first block tag
    (pages often omit </head>). Only the first <title> outside svg/script etc. is the title;
    text of any other <title> is dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._skip = 0
        self._in_head = False
        self._in_title = False       # inside the <title> that becomes the page title
        self._title_tag = False      # inside any <title>: its text is never body text
        self._title_done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "head":
            self._in_head = True
        elif tag == "body":
            self._in_head = False
            self._skip = 0
        elif tag == "title":
            self._title_tag = True
            self._in_title = not self._skip and not self._title_done
        elif tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self._in_head = False
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._in_head = False
        elif tag == "title":
            if self._in_title and "".join(self.title).strip():
                self._title_done = True
            self._in_title = self._title_tag = False
        elif tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif not self._skip and not self._in_head and not self._title_tag:
            self.parts.append(data)


def _plain_html_to_text(html: str) -> tuple[str, str]:
    """The fallback extractor: script/style/head etc. dropped, block tags become line breaks,
    whitespace collapsed per line, blank lines removed."""
    p = _Text()
    p.feed(html)
    p.close()
    lines = (" ".join(line.split()) for line in "".join(p.parts).splitlines())
    return " ".join("".join(p.title).split()), "\n".join(x for x in lines if x)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, body text) of a page: trafilatura's main-content extraction (tables included,
    plain text, link targets dropped), so navigation and footers fall away; when it finds nothing
    (a fragment, an odd page) the plain tag-stripping extractor above. Both empty gives an empty
    text, as before."""
    import trafilatura
    plain_title, plain_text = _plain_html_to_text(html)    # the <title> tag is the page title
    text = meta_title = ""
    try:
        text = trafilatura.extract(html, include_tables=True, include_links=False,
                                   include_comments=False, favor_recall=True,
                                   output_format="txt") or ""
        if text.strip() and not plain_title:
            meta = trafilatura.extract_metadata(html)
            meta_title = " ".join((meta.title if meta is not None and meta.title else "").split())
    except Exception:                           # lxml / trafilatura internals: fall back, loudly
        log.warning("trafilatura failed on a fetched page; using the plain extractor",
                    exc_info=True)
        text = ""
    if not text.strip():
        return plain_title, plain_text
    lines = (" ".join(line.split()) for line in text.splitlines())
    return plain_title or meta_title, "\n".join(x for x in lines if x)


_META_CHARSET = re.compile(rb"""<meta\s[^>]*?charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""",
                           re.IGNORECASE)
_ALIASES = {"gb2312": "gbk", "gb_2312-80": "gbk", "x-gbk": "gbk"}   # pages say gb2312, mean gbk


def _declared_charset(f: Fetched) -> str | None:
    """The charset the HTTP header gives; else (HTML only) the first <meta charset> /
    <meta http-equiv=content-type content="...charset=..."> in the first 4 KiB."""
    for piece in f.content_type.split(";")[1:]:
        k, _, v = piece.strip().partition("=")
        if k.strip().lower() == "charset" and v.strip():
            return v.strip().strip('"\'')
    if "html" in f.content_type.lower():
        m = _META_CHARSET.search(f.body[:4096])
        if m:
            return m.group(1).decode("ascii", errors="replace")
    return None


def _decode(f: Fetched) -> tuple[str, str]:
    charset = (_declared_charset(f) or "utf-8").lower()
    charset = _ALIASES.get(charset, charset)
    try:
        raw = f.body.decode(charset, errors="replace")
    except LookupError:
        raw = f.body.decode("utf-8", errors="replace")
    if "html" in f.content_type.lower():
        return html_to_text(raw)
    return "", raw.strip()


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=2000)


class FetchUrlTool:
    def __init__(self, fetcher: Fetcher | None = None):
        self._fetcher = fetcher or UrlFetcher()
        self.spec = spec("fetch_url", DESCRIPTION, Args, "network")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(Args, args, self._run)

    def _run(self, a: Args) -> ToolResult:
        try:
            f = self._fetcher.fetch(a.url)
        except UrlBlocked as e:
            return fail(f"不允许访问这个地址：{e}")
        except FetchError as e:
            return fail(f"读取失败：{e}")
        title, text = _decode(f)
        note = TRUNCATED_NOTE if f.truncated else ""
        body = clip(f"{title}\n{f.final_url}\n\n{text}".strip(), FETCH_CHARS - len(note))
        return ToolResult(content=body + note,
                          sources=(Source(title=clip(title or f.final_url, TITLE_CHARS),
                                          url=f.final_url),))
