"""Citations in a chat answer: the page writer's check (`sources.cite`), with one difference. A page
drops what fails; an answer says so in place, so a failed citation stays visible and never looks
like a checked one. The model writes `〔名 p.N：「原文片段」〕` (a material without pages:
`〔名：「原文片段」〕`). What the answer then holds (and what its later turns send to the model,
never recomputed, so the prompt cache stays valid):

- `〔名 p.印码｜N｜source_id：「片段」〕`: found on PDF page N (or running over onto N+1), as the writer
  checks it, and in what this turn put in front of the model. Never moved to another page.
- `〔名 p.印码｜N｜此前读取｜source_id：「片段」〕`: not read this turn, found in the stored read of the
  page's image.
- `〔名｜source_id：「片段」〕`: found in a material without pages, and read this turn.
- `〔名 p.N｜未核对：「片段」〕`, `〔名｜未核对：「片段」〕`, `〔名 p.N｜未核对〕`: anything else — the fragment
  not found, no fragment, a name that is no material of his, words that stand in the material but
  that this turn did not read (they came from memory, where the model had them right by luck or
  not), and `（名 p.N）`, the shape recalled answers have (`plain_marks`), which checks nothing.

Two shapes around that. A quote from something this turn read that is no material of his (a web
page it opened) stands as `（名：「片段」）`: no material to check against, and not a failed check. A
sentence that names a page of a material with no mark at all (「讲义第 13 页给出了」) reads like a
checked citation and is none, so it gets `〔名 p.N｜未核对〕` (`bare_pages`).

The quote stays in the mark, unlike a page's stored form: every mark is checked again whatever form
it comes in, so a mark the model copied from its earlier answer gets no pass, and carries its quote.
A bracket that is neither (a side note the model wrote, `〔这一页没有讲平均度…〕`) is ordinary words:
`（…）`, 〔〕 being only a citation's state. Pure: the text comes from the caller."""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from evowiki.sources.cite import match_source, quote_in, quote_on
from evowiki.sources.printed import printed_page

NOT_CHECKED = "未核对"
EARLIER = "此前读取"
_MARK = re.compile(r"〔(?P<body>[^〔〕\n]+)〕")
_PLAIN = re.compile(r"（(?P<name>[^（）\n]+?)\s*p\.\s*(?P<page>\d+)[^（）\n]*）")
_EITHER = re.compile(f"{_MARK.pattern}|{_PLAIN.pattern}")
_QUOTED = re.compile(r"[：:]\s*[「“\"](?P<quote>.*)[」”\"]\s*(?:｜[^｜]*)?$")
_PAGE = re.compile(r"(?:^|\s)p\.\s*(?P<page>\d+)")
# the same bracket, cut off by the end of the text (an answer that broke off)
_OPEN_MARK = re.compile(r"〔[^〔〕\n]*(?:p\.\s*\d|[：:]\s*[「“\"])[^〔〕\n]*$")


_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_BARE_PAGE = re.compile(r"(?P<word>讲义|资料|课件|教材|课本|附件|论文|PDF|pdf|(?i:lecture|material|slides?|textbook|attachment|paper))"
                        r"[^，,；;\n]{0,30}?(?:第\s*|(?i:page|p\.)\s*)(?P<page>\d+)\s*页?")


@dataclass(frozen=True)
class ChatSource:
    """A live material of the learner. `paged`: its text carries 〔p.N〕 marks."""

    source_id: str
    title: str
    page_offset: int | None
    paged: bool
    image_pages: tuple[int, ...] = ()
    earlier_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Cited:
    name: str
    page: int | None
    quote: str | None


def _parse(body: str) -> _Cited | None:
    """The citation a bracket holds, in any of the forms above; None when it is no citation."""
    head, quote = body, None
    if (q := _QUOTED.search(body)) is not None:
        head, quote = body[:q.start()], q.group("quote").strip()
    fields = head.split("｜")
    found = _PAGE.search(fields[0])
    name = (fields[0] if found is None else fields[0][:found.start()]).strip()
    page = None if found is None else int(found.group("page"))
    if len(fields) > 1 and fields[1].strip().isdigit():
        page = int(fields[1])                  # the stored form's PDF page (p. there is printed)
    if page is None and quote is None:
        return None
    return _Cited(name, page, quote)


def _failed(c: _Cited) -> str:
    page = "" if c.page is None else f" p.{c.page}"
    quote = "" if c.quote is None else f"：「{c.quote}」"
    return f"〔{c.name}{page}｜{NOT_CHECKED}{quote}〕"


def needs_check(text: str) -> bool:
    return "〔" in text or _PLAIN.search(text) is not None or _BARE_PAGE.search(text) is not None


def check_marks(text: str, sources: Sequence[ChatSource], page_text: Callable[[str, int], str],
                whole_text: Callable[[str], str], seen: Callable[[str], bool],
                stored: Callable[[str, int], str]) -> str:
    """`text` with every citation in the answer's form (module doc). `page_text`: (source id, PDF
    page) -> that page's text; `whole_text`: source id -> all of it; `seen`: the quote is in what
    this turn put in front of the model; `stored`: (source id, PDF page) -> its stored read."""

    def one(m: re.Match[str]) -> str:
        if m.group("body") is None:
            if match_source(m.group("name"), sources) is None:
                return m.group(0)
            return _failed(_Cited(m.group("name").strip(), int(m.group("page")), None))
        c = _parse(m.group("body"))
        if c is None:
            return f"（{m.group('body')}）"
        src = match_source(c.name, sources)
        if src is None and c.page is None and c.quote and seen(c.quote):
            return f"（{c.name}：「{c.quote}」）"
        if src is None or not c.quote:
            return _failed(c)
        if not src.paged:
            if not seen(c.quote) or not quote_in(c.quote, whole_text(src.source_id)):
                return _failed(c)
            return f"〔{src.title}｜{src.source_id}：「{c.quote}」〕"
        if c.page is None:
            return _failed(c)
        printed = printed_page(c.page, src.page_offset) or c.page
        if seen(c.quote) and quote_on(c.quote, page_text(src.source_id, c.page),
                                      page_text(src.source_id, c.page + 1)):
            return f"〔{src.title} p.{printed}｜{c.page}｜{src.source_id}：「{c.quote}」〕"
        if quote_on(c.quote, stored(src.source_id, c.page), stored(src.source_id, c.page + 1)):
            return f"〔{src.title} p.{printed}｜{c.page}｜{EARLIER}｜{src.source_id}：「{c.quote}」〕"
        return _failed(c)

    return _EITHER.sub(one, _one_quote_each(text))


def _one_quote_each(text: str) -> str:
    """〔名：「甲」「乙」〕, which models do write, as two citations: each quote is checked by itself."""

    def split(m: re.Match[str]) -> str:
        head, sep, rest = m.group("body").partition("：「")
        if not sep or "」「" not in rest or not rest.endswith("」"):
            return m.group(0)
        return "".join(f"〔{head}：「{q}」〕" for q in rest[:-1].split("」「"))

    return _MARK.sub(split, text)


def bare_pages(text: str, sources: Sequence[ChatSource]) -> str:
    """`text` with `〔名 p.N｜未核对〕` after each sentence that says a material's page says something
    without a mark. The name: the material's when he has one by that word or only one, else the word."""

    def one(m: re.Match[str]) -> str:
        sent = m.group(0)
        found = _BARE_PAGE.search(sent)
        if found is None or "〔" in sent:
            return sent
        named = match_source(found.group("word"), sources)
        name = named.title if named else sources[0].title if len(sources) == 1 else found.group("word")
        body, end = (sent[:-1], sent[-1]) if sent[-1] in "。！？!?" else (sent, "")
        return f"{body}〔{name} p.{found.group('page')}｜{NOT_CHECKED}〕{end}"

    return _SENTENCE.sub(one, text)


def plain_marks(text: str) -> str:
    """The answer as words (what the memory index keeps): `（名 p.N）`; a failed one says so."""

    def one(m: re.Match[str]) -> str:
        c = _parse(m.group("body"))
        if c is None:
            return m.group(0)
        page = "" if c.page is None else f" p.{c.page}"
        missed = "，没有在原文核对到" if NOT_CHECKED in m.group("body") else ""
        return f"（{c.name}{page}{missed}）"

    return _MARK.sub(one, text)


def cited_ids(text: str) -> set[str]:
    """The source ids of the checked citations of a stored answer."""
    out: set[str] = set()
    for m in _MARK.finditer(text):
        fields = m.group("body").split("：", 1)[0].split("｜") if "｜" in m.group("body") else []
        sid = fields[-1].strip() if len(fields) > 1 else ""
        if sid and sid != NOT_CHECKED and not sid.isdigit():
            out.add(sid)
    return out


def without_open_mark(text: str) -> str:
    """An answer that broke off inside a citation: what stood before it."""
    return _OPEN_MARK.sub("", text).rstrip(" \t　") if "〔" in text else text
