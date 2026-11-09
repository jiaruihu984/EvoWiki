"""An answer's internal ids: finding them (`leaked_ids`, what the turn re-asks on) and showing them as
names (`plain_ids`, output normalisation, not a judgement of what it means). Tools speak in ids so the
model can call them back (live 2026-11-04: 「对应概念 c11」, 「原页的开头和第 s1–s3 段」; 2026-11-05: 「（段 id：
s2）」). A short id counts only if this learner's graph or page really has it; a long one by its shape; a
tool name only if it has an underscore (`confirm` is a word). Code blocks, formulas, citations and link
targets stay as written, and a token he said, the page says or the answer's code uses is literal."""

import re
from collections.abc import Collection, Mapping, Sequence

from evowiki.i18n import current_language

_NODE_ID = r"(?:c\d+|n_[0-9a-f]{6,})"
_ID = re.compile(rf"(?<![A-Za-z0-9_.])(?:{_NODE_ID}|s\d+|pg_[0-9a-f]{{6,}})(?![A-Za-z0-9_.])")
_KEEP = re.compile(r"```.*?```|`[^`\n]*`|\$\$.*?\$\$|\$[^$\n]*\$|〔[^〕\n]*〕|\]\([^)\n]*\)", re.DOTALL)
_PAREN = re.compile(rf"\s*[（(]\s*({_NODE_ID}|s\d+)\s*[）)]")
_RANGE = re.compile(r"(?:第\s*)?(?<![A-Za-z0-9_.])(s\d+)\s*[–—~\-到至]\s*(s\d+)(?![A-Za-z0-9_.])(?:\s*[段节])?")
_SECTION = re.compile(r"(?:第\s*)?(?<![A-Za-z0-9_.])(s\d+)(?![A-Za-z0-9_.])(?:\s*[段节])?")
_NODE = re.compile(rf"(?<![A-Za-z0-9_.])({_NODE_ID})(?![A-Za-z0-9_.])\s*")
_PAGE = re.compile(r"(?<![A-Za-z0-9_.])(pg_[0-9a-f]{6,})(?![A-Za-z0-9_.])")


# what stays literal when looking for a leak: inline code does not, 「`s4` 那一段」 is the known leak
_LITERAL = re.compile(r"```.*?```|\$\$.*?\$\$|\$[^$\n]*\$|〔[^〕\n]*〕|\]\([^)\n]*\)", re.DOTALL)
_WORD = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z][A-Za-z0-9_-]*(?![A-Za-z0-9_.-])")
_SHORT = re.compile(r"[cs]\d+")
_LONG = re.compile(r"(?:[a-z]+_)+[0-9a-f]{6,}|(?:path|map)-[0-9a-f]{12}")
_DIGIT = re.compile(r"\d")


def has_internal_ids(text: str) -> bool:
    return _ID.search(_KEEP.sub(" ", text)) is not None


def any_id_word(text: str) -> bool:
    """Could `text` hold an id or a tool name at all? (Before reading what the page and graph know.)"""
    return any(_SHORT.fullmatch(w) or "_" in w or _LONG.fullmatch(w) for w in _WORD.findall(_LITERAL.sub(" ", text)))


def id_tokens(text: str) -> set[str]:
    """Every word of `text` that could be taken for an id or a tool name: what he said or the page
    says is literal from then on."""
    return set(_WORD.findall(text))


def leaked_ids(text: str, *, known: Collection[str], tools: Collection[str],
               literal: Collection[str] = ()) -> list[str]:
    """The internal ids and tool names `text` shows, sorted. `known`: this learner's short ids (the
    page's sections, the graph's points). `tools`: the turn's tool names. `literal`: words that are
    content here (`id_tokens` of his words and of the page)."""
    used = set(literal)
    for m in _LITERAL.finditer(text):
        used |= id_tokens(m.group(0))
    out = set()
    for w in _WORD.findall(_LITERAL.sub(" ", text)):
        if w not in used and ((_SHORT.fullmatch(w) and w in known) or ("_" in w and w in tools)
                              or (_LONG.fullmatch(w) and _DIGIT.search(w))):
            out.add(w)
    return sorted(out)


def unquoted_ids(text: str, ids: Collection[str]) -> str:
    """`text` with inline code that is exactly one of `ids` as plain text (`plain_ids` leaves code alone)."""
    return re.sub(r"`([^`\n]+)`", lambda m: m.group(1) if m.group(1) in ids else m.group(0), text)


def _words(text: str, nodes: Mapping[str, str], sections: Sequence[tuple[str, str]],
           pages: Mapping[str, str]) -> str:
    order = {sid: i for i, (sid, _) in enumerate(sections, 1)}
    heading = dict(sections)

    def paren(m: re.Match[str]) -> str:      # 「连通分量」（c10）: it was named right there
        return "" if m.group(1) in nodes or m.group(1) in heading else m.group(0)

    def span(m: re.Match[str]) -> str:
        a, b = order.get(m.group(1)), order.get(m.group(2))
        return m.group(0) if a is None or b is None else (f"Sections {a}–{b}" if current_language() == "en"
                                                      else f"第 {a} 到第 {b} 节")

    def section(m: re.Match[str]) -> str:
        name = heading.get(m.group(1))
        return m.group(0) if name is None else f"「{name}」"

    def node(m: re.Match[str]) -> str:
        name = nodes.get(m.group(1))
        if name is None:
            return m.group(0)
        said = m.string[m.end():].startswith(name)  # 「c11 巨分量」: the name comes next
        return "" if said else f"「{name}」"

    def page(m: re.Match[str]) -> str:
        name = pages.get(m.group(1))
        return m.group(0) if name is None else f"《{name}》"

    text = _PAREN.sub(paren, text)
    text = _RANGE.sub(span, text)
    text = _SECTION.sub(section, text)
    return _PAGE.sub(page, _NODE.sub(node, text))


def plain_ids(text: str, *, nodes: Mapping[str, str], sections: Sequence[tuple[str, str]],
              pages: Mapping[str, str]) -> str:
    """`text` with the ids known here as names. `sections`: (id, heading) of the page it is about, in
    page order."""
    out, at = [], 0
    for m in _KEEP.finditer(text):
        out += [_words(text[at:m.start()], nodes, sections, pages), m.group(0)]
        at = m.end()
    out.append(_words(text[at:], nodes, sections, pages))
    return "".join(out)
