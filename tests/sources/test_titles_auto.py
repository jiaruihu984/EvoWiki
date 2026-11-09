"""Where a material's title comes from, and that it never breaks what refers to the material."""

from dataclasses import dataclass, field

import pytest

from evowiki.assistant.tools.fetch_url import Fetched
from evowiki.sources.arxiv import arxiv_id, arxiv_pdf_url, arxiv_title
from evowiki.sources.chat_cite import ChatSource, check_marks
from evowiki.sources.titles import looks_like_title

FEED = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/1409.0473v7</id>
<title>Neural Machine Translation by Jointly
  Learning to Align and Translate</title></entry></feed>"""
UNKNOWN = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/api/errors#incorrect_id</id><title>Error</title></entry></feed>"""


@dataclass
class Answers:
    body: bytes = FEED
    fail: Exception | None = None
    asked: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> Fetched:
        self.asked.append(url)
        if self.fail is not None:
            raise self.fail
        return Fetched(final_url=url, content_type="application/atom+xml", body=self.body)


@pytest.mark.parametrize(("url", "aid"), [
    ("https://arxiv.org/pdf/1409.0473", "1409.0473"),
    ("https://arxiv.org/pdf/1409.0473v7.pdf", "1409.0473"),
    ("http://www.arxiv.org/abs/1706.03762v7", "1706.03762"),
    ("https://arxiv.org/abs/cs/0112017", "cs/0112017"),
    ("https://example.com/pdf/1409.0473", None),
])
def test_arxiv_id(url, aid):
    assert arxiv_id(url) == aid


def test_arxiv_title_reads_the_feed_with_its_own_capitals():
    a = Answers()
    assert arxiv_title("https://arxiv.org/pdf/1409.0473", a) == (
        "Neural Machine Translation by Jointly Learning to Align and Translate")
    assert a.asked[-1] == "https://export.arxiv.org/api/query?max_results=1&id_list=1409.0473"


def test_arxiv_title_prefers_the_papers_own_page():
    page = b'<head><meta name="citation_title" content="Very Deep &amp; Wide Networks" /></head>'
    a = Answers(body=page)
    assert arxiv_title("https://arxiv.org/pdf/1409.1556", a) == "Very Deep & Wide Networks"
    assert a.asked == ["https://arxiv.org/abs/1409.1556"]


def test_any_arxiv_url_of_a_paper_imports_the_paper():
    assert arxiv_pdf_url("https://arxiv.org/abs/1409.0473v7") == "https://arxiv.org/pdf/1409.0473v7"
    assert arxiv_pdf_url(" https://example.com/abs/1 ") == "https://example.com/abs/1"


@pytest.mark.parametrize("answers", [
    Answers(fail=RuntimeError("HTTP 429")), Answers(body=UNKNOWN), Answers(body=b"<not xml"),
])
def test_arxiv_failure_means_no_title_here(answers):
    assert arxiv_title("https://arxiv.org/pdf/1409.0473", answers) is None


def test_no_lookup_for_other_urls():
    a = Answers()
    assert arxiv_title("https://example.com/a.pdf", a) is None and a.asked == []


@pytest.mark.parametrize(("heading", "ok"), [
    ("NEURAL MACHINE TRANSLATION BY JOINTLY LEARNING", True), ("Attention Is All You Need", True),
    ("1 Introduction", False), ("Abstract", False), ("arXiv:1409.0473v7", False), ("目录", False),
])
def test_a_first_heading_stands_for_the_document_only_when_it_can(heading, ok):
    assert looks_like_title(heading) is ok


def test_a_citation_by_an_earlier_name_finds_the_source_and_shows_its_title():
    src = ChatSource("s1", "Attention Is All You Need", None, True, earlier_titles=("attention",))
    out = check_marks("这样。〔attention p.3：「scaled dot product」〕", [src],
                      lambda _sid, _p: "the scaled dot product here", lambda _sid: "",
                      lambda _quote: True, lambda _sid, _p: "the scaled dot product here")
    assert out == "这样。〔Attention Is All You Need p.3｜3｜s1：「scaled dot product」〕"
