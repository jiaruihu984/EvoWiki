"""Openly licensed images for the page writer (find_image / place_image, deep@v24).

The writer searches open image libraries — Wikimedia Commons first (no key; its `extmetadata` carries
the licence, author and file page), then Openverse (no key; CC-licensed images from many sites, served
through its own thumbnail endpoint) — and keeps only licences that allow reuse with attribution:
public domain, CC0, CC BY and CC BY-SA (`reusable_license`). Anything else (NC, ND, GFDL-only, fair
use, unknown) is never offered. Attribution follows the CC "TASL" practice: title, author, source,
licence — the caption the note shows under the image in small grey text.

The chosen image is downloaded here, server side, from the provider's own image host only
(`ALLOWED_HOSTS`, redirects not followed), at most `MAX_DOWNLOAD_BYTES`, then decoded and re-encoded
with Pillow (`prepare`: a real JPEG / PNG / WebP / GIF, at most `MAX_PIXELS`, scaled to `MAX_SIDE`,
saved as WebP — no metadata, no polyglot bytes survive). The bytes become a wiki asset (kind "image");
the page and the plugin never load a remote URL, so every CSP stays as it is.

Network is behind `Http` (tests pass a fake; `HttpxHttp` is the real one). Fail closed: a provider
that errors gives no candidates (logged), a download or decode problem is `ImageRejected` and nothing
is placed."""

import base64
import html
import io
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("evowiki.deep.images")

# Wikimedia's User-Agent policy: a name, a version and a contact URL (a bare client string gets 403)
USER_AGENT = "EvoWiki/0.1 (https://github.com/jiaruihu984/EvoWiki; personal learning wiki) httpx"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"
# where an image may be downloaded from (the providers' own file / thumbnail hosts)
ALLOWED_HOSTS = frozenset({"upload.wikimedia.org", "thumb.wikimedia.org", "api.openverse.org"})
MAX_DOWNLOAD_BYTES = 8_000_000
MAX_PIXELS = 40_000_000                 # decoded size guard (decompression bombs)
MAX_SIDE = 1280                         # longest side after scaling
THUMB_WIDTH = 1280                      # Commons thumbnail width asked for
PREVIEW_SIDE = 320                      # what the model looks at when choosing (cheap: under 150 tokens each)
COMMONS_PREVIEW = "330px-"              # Commons serves standard thumbnail widths only
MAX_IMAGE_BYTES = 1_500_000             # the stored WebP
MIME = "image/webp"
TIMEOUT_S = 15.0

Provider = Literal["commons", "openverse"]
PROVIDER_NAMES: Mapping[str, str] = {"commons": "Wikimedia Commons", "openverse": "Openverse"}


class ImageRejected(Exception):
    """A candidate that cannot be used (download / type / size / decode); the text is for the model."""


# ---- licences ----

_CC = re.compile(r"^(?:cc[ -]?)?(by(?:-sa)?)(?:[ -](\d(?:\.\d)?))?", re.IGNORECASE)


def reusable_license(name: str, version: str = "") -> str | None:
    """The licence's short name when it allows reuse with attribution (PD, CC0, CC BY, CC BY-SA),
    else None. `version`: Openverse gives it apart from the code."""
    n = re.sub(r"\s+", " ", name.strip())
    low = n.lower()
    if not low:
        return None
    if low in ("pd", "pdm") or low.startswith(("pd-", "pd ", "public domain")):
        return "Public domain"
    if low in ("cc0", "cc-zero", "cc0 1.0", "cc zero"):
        return "CC0"
    m = _CC.match(low)
    if m is None or re.search(r"\b(nc|nd)\b|-nc|-nd", low):
        return None
    rest = low[m.end():].strip()
    if rest and not re.fullmatch(r"[\d.,\s]*([a-z]{2,3})?", rest):   # "… 2.5,2.0,1.0", "… 3.0 de"
        return None
    kind = m.group(1).upper()
    ver = m.group(2) or version.strip()
    return f"CC {kind}" + (f" {ver}" if ver else "")


# ---- candidates ----

class ImageCandidate(BaseModel):
    """One search hit the writer may choose. `file_url` is downloaded only when it is placed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider
    title: str = Field(max_length=200)
    description: str = Field(default="", max_length=400)
    author: str = Field(default="", max_length=160)
    license: str = Field(max_length=40)                  # reusable_license's short name
    license_url: str = Field(default="", max_length=300)
    source_url: str = Field(max_length=500)              # the file's page (attribution link)
    file_url: str = Field(max_length=1000)               # what place_image downloads
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class PreparedImage:
    data: bytes                     # WebP
    width: int
    height: int

    @property
    def data_b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


class HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]
    content: bytes


class Http(Protocol):
    def get(self, url: str, params: Mapping[str, str] | None = None) -> HttpResponse:
        """One GET without following redirects; raises on transport errors."""
        ...


class ImageProvider(Protocol):
    def search(self, query: str, limit: int) -> "list[ImageCandidate]": ...


class ImageSearch(Protocol):
    def search(self, query: str, limit: int) -> list[ImageCandidate]: ...

    def fetch(self, candidate: ImageCandidate) -> PreparedImage:
        """Downloads and prepares the image; ImageRejected when it cannot be used."""
        ...

    def thumbnail(self, candidate: ImageCandidate) -> PreparedImage | None:
        """A small picture the model can look at when choosing; None when there is none."""
        ...


def plain(text: str, n: int) -> str:
    """Commons metadata is HTML: tags dropped, entities decoded, whitespace collapsed, clipped."""
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = re.sub(r"\s+", " ", html.unescape(t)).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _meta(ext: Mapping[str, object], key: str) -> str:
    v = ext.get(key)
    return str(v.get("value", "")) if isinstance(v, Mapping) else ""


class CommonsSearch:
    """Wikimedia Commons: generator=search over the File namespace (bitmaps; SVG / PDF come back as a
    rendered thumbnail), imageinfo with a THUMB_WIDTH thumbnail and the licence metadata."""

    def __init__(self, http: "Http"):
        self._http = http

    def search(self, query: str, limit: int) -> list[ImageCandidate]:
        params = {"action": "query", "format": "json", "formatversion": "2",
                  "generator": "search", "gsrsearch": f"{query} filetype:bitmap|drawing",
                  "gsrnamespace": "6", "gsrlimit": str(min(limit * 3, 30)),
                  "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata",
                  "iiurlwidth": str(THUMB_WIDTH),
                  "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|ImageDescription|ObjectName"}
        r = self._http.get(COMMONS_API, params)
        if r.status_code != 200:
            raise RuntimeError(f"commons search HTTP {r.status_code}")
        pages = (json.loads(r.content).get("query") or {}).get("pages") or []
        out: list[ImageCandidate] = []
        for p in sorted(pages, key=lambda p: p.get("index", 0)):
            info = (p.get("imageinfo") or [{}])[0]
            ext = info.get("extmetadata") or {}
            lic = reusable_license(_meta(ext, "LicenseShortName"))
            url = info.get("thumburl") or info.get("url") or ""
            if lic is None or not url or not info.get("descriptionurl"):
                continue
            title = plain(_meta(ext, "ObjectName"), 200) or plain(
                str(p.get("title", "")).removeprefix("File:").rsplit(".", 1)[0], 200)
            out.append(ImageCandidate(
                provider="commons", title=title or "（无标题）",
                description=plain(_meta(ext, "ImageDescription"), 400),
                author=plain(_meta(ext, "Artist"), 160), license=lic,
                license_url=_meta(ext, "LicenseUrl")[:300],
                source_url=str(info["descriptionurl"])[:500], file_url=str(url)[:1000],
                width=int(info.get("thumbwidth") or info.get("width") or 0),
                height=int(info.get("thumbheight") or info.get("height") or 0)))
            if len(out) >= limit:
                break
        return out


class OpenverseSearch:
    """Openverse: CC0 / PDM / BY / BY-SA only (asked for, and checked again); downloaded through its
    own thumbnail endpoint, never the original site."""

    def __init__(self, http: "Http"):
        self._http = http

    def search(self, query: str, limit: int) -> list[ImageCandidate]:
        r = self._http.get(OPENVERSE_API, {"q": query, "license": "cc0,pdm,by,by-sa",
                                           "page_size": str(min(limit * 2, 20)), "mature": "false"})
        if r.status_code != 200:
            raise RuntimeError(f"openverse search HTTP {r.status_code}")
        out: list[ImageCandidate] = []
        for x in json.loads(r.content).get("results") or []:
            lic = reusable_license(str(x.get("license", "")), str(x.get("license_version") or ""))
            thumb = str(x.get("thumbnail") or "")
            landing = str(x.get("foreign_landing_url") or "")
            if lic is None or not thumb or not landing:
                continue
            tags = ", ".join(str(t.get("name", "")) for t in (x.get("tags") or [])[:8]
                             if isinstance(t, Mapping))
            out.append(ImageCandidate(
                provider="openverse", title=plain(str(x.get("title") or ""), 200) or "（无标题）",
                description=plain(f"{x.get('source') or ''}；标签：{tags}" if tags else
                                  str(x.get("source") or ""), 400),
                author=plain(str(x.get("creator") or ""), 160), license=lic,
                license_url=str(x.get("license_url") or "")[:300], source_url=landing[:500],
                file_url=thumb[:1000], width=int(x.get("width") or 0),
                height=int(x.get("height") or 0)))
            if len(out) >= limit:
                break
        return out


def prepare(raw: bytes, side: int = MAX_SIDE) -> PreparedImage:
    """Decodes `raw` (JPEG / PNG / WebP / GIF only), scales it to `side` and re-encodes it as WebP.
    ImageRejected for anything else."""
    from PIL import Image, UnidentifiedImageError
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise ImageRejected("图片太大")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = probe.format
            w, h = probe.size
            probe.verify()
        if fmt not in ("JPEG", "PNG", "WEBP", "GIF"):
            raise ImageRejected(f"不支持的图片格式 {fmt}")
        if w * h > MAX_PIXELS or w < 16 or h < 16:
            raise ImageRejected(f"图片尺寸不合适（{w}×{h}）")
        with Image.open(io.BytesIO(raw)) as img:
            img.seek(0)
            img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
            img.thumbnail((side, side))
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=82, method=4)
            data, (w, h) = buf.getvalue(), img.size
    except ImageRejected:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as e:
        raise ImageRejected(f"不是能用的图片（{type(e).__name__}）") from e
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRejected("图片压缩后仍太大")
    return PreparedImage(data=data, width=w, height=h)


class OpenImages:
    """Commons first, then Openverse to fill up to `limit`; a provider that fails is skipped (logged)."""

    def __init__(self, http: "Http", providers: Sequence[ImageProvider] | None = None):
        self._http = http
        self._providers = list(providers) if providers is not None else [
            CommonsSearch(http), OpenverseSearch(http)]

    def search(self, query: str, limit: int) -> list[ImageCandidate]:
        out: list[ImageCandidate] = []
        seen: set[str] = set()
        for p in self._providers:
            if len(out) >= limit:
                break
            try:
                got = p.search(query, limit - len(out))
            except Exception:
                log.warning("image search via %s failed", type(p).__name__, exc_info=True)
                continue
            for c in got:
                if c.source_url not in seen:
                    seen.add(c.source_url)
                    out.append(c)
        return out[:limit]

    def fetch(self, candidate: ImageCandidate) -> PreparedImage:
        return prepare(self._download(candidate.file_url))

    def thumbnail(self, candidate: ImageCandidate) -> PreparedImage | None:
        small = re.sub(r"/\d+px-", f"/{COMMONS_PREVIEW}", candidate.file_url) if candidate.provider == "commons" \
            else candidate.file_url
        try:
            return prepare(self._download(small), PREVIEW_SIDE)
        except ImageRejected as e:
            log.info("no preview of %s: %s", candidate.source_url, e)
            return None

    def _download(self, url: str) -> bytes:
        u = urlsplit(url)
        if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
            raise ImageRejected("图片不在允许的图库地址上")
        try:
            r = self._http.get(url)
        except Exception as e:
            raise ImageRejected(f"下载失败（{type(e).__name__}）") from e
        if r.status_code != 200:
            raise ImageRejected(f"下载失败（HTTP {r.status_code}）")
        ctype = str(r.headers.get("content-type", "")).split(";")[0].strip().lower()
        if not ctype.startswith("image/") or ctype == "image/svg+xml":
            raise ImageRejected(f"下载到的不是图片（{ctype or '没有类型'}）")
        return r.content


class HttpxHttp:
    """The real `Http`: httpx, no redirects, a short timeout, a response size bound."""

    def __init__(self, timeout_s: float = TIMEOUT_S):
        import httpx
        self._client = httpx.Client(timeout=timeout_s, follow_redirects=False,
                                    headers={"User-Agent": USER_AGENT})

    def get(self, url: str, params: Mapping[str, str] | None = None) -> HttpResponse:
        with self._client.stream("GET", url, params=params) as r:
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise ImageRejected("图片太大")
                chunks.append(chunk)
            return _Resp(r.status_code, dict(r.headers), b"".join(chunks))


@dataclass
class _Resp:
    status_code: int
    headers: Mapping[str, str]
    content: bytes
