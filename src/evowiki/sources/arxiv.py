"""An arXiv url's PDF and title. arXiv PDFs mostly have no metadata Title, and their largest text is
the watermark. The title is read from the paper's own page (its `citation_title` meta, the Highwire
tag Zotero reads too), else from arXiv's API, which is not reachable from everywhere. Any failure
(rate limit, network, an id it does not know) means no title here, and the next source of a title
is used. https://info.arxiv.org/help/api/user-manual.html"""

import html
import logging
import re
import xml.etree.ElementTree as ET

from evowiki.assistant.tools.fetch_url import Fetcher

log = logging.getLogger(__name__)

_ID = re.compile(r"^https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/"
                 r"(?P<id>\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?P<v>v\d+)?(?:\.pdf)?/?(?:[?#].*)?$")
_API = "https://export.arxiv.org/api/query?max_results=1&id_list="
_ATOM = "{http://www.w3.org/2005/Atom}"
_META = re.compile(r'<meta\s+name="citation_title"\s+content="([^"]+)"')


def arxiv_id(url: str) -> str | None:
    m = _ID.match(url.strip())
    return m.group("id") if m else None


def arxiv_pdf_url(url: str) -> str:
    """The paper itself for any of its arXiv urls: an abstract page holds only the abstract."""
    m = _ID.match(url.strip())
    return f"https://arxiv.org/pdf/{m.group('id')}{m.group('v') or ''}" if m else url.strip()


def _from_page(aid: str, fetcher: Fetcher) -> str | None:
    m = _META.search(fetcher.fetch(f"https://arxiv.org/abs/{aid}").body.decode("utf-8", "replace"))
    return " ".join(html.unescape(m.group(1)).split()) or None if m else None


def _from_api(aid: str, fetcher: Fetcher) -> str | None:
    entry = ET.fromstring(fetcher.fetch(_API + aid).body).find(f"{_ATOM}entry")
    if entry is None or "/api/errors" in (entry.findtext(f"{_ATOM}id") or ""):
        return None
    return " ".join((entry.findtext(f"{_ATOM}title") or "").split()) or None


def arxiv_title(url: str, fetcher: Fetcher) -> str | None:
    aid = arxiv_id(url)
    if aid is None:
        return None
    for read in (_from_page, _from_api):
        try:
            title = read(aid, fetcher)
        except Exception as exc:  # noqa: BLE001 - a title is never worth failing the import
            log.warning("arXiv title of %s not read (%s): %s", aid, read.__name__, exc)
            continue
        if title:
            return title
    return None
