"""`context_for`: what a model call learns about the learner. The gateway appends it to the system
prompt of every call it makes for a user (`llm.gateway.Gateway`, `profile=`); no pipeline builds
its own learner context.

Which sections a purpose gets, most important first, within its token budget. His own lines
(`owned`: what he typed, the facts from his words) are placed first, every section's, and the
server's derived lines fill what is left, so a long 还不会的 never pushes out what he said
(a section is cut from its last line within each pass). A purpose not listed gets nothing
(the curator, materials, RSI, ...)."""

from collections.abc import Mapping, Sequence

from evowiki.goals.context import Part, assemble
from evowiki.profile.note import GAPS, HOW, WANT, WHERE, WHO

HEADER = "关于这位学习者（他的档案「我」，他自己能改）"
# D10: a want is not an experience; only his own words say what he did or can do
RULE = ("「想学什么」是他想做的事，不是他做过的事；他做过什么、会什么，只看「我是谁」，"
        "那里没写的不要当成他的经历或已有的本事。")

_LEARN = (WHO, WANT, HOW, GAPS)
# A goal's scope is what he says for THIS goal (the prompt's goal and his words): making a goal and
# splitting a block read who he is and how he learns, never what he wants or lacks for his other
# goals — those replaced the goal itself (bug 2026-11-03: `python` came back as 复杂网络中的渗流).
# What K has on a node reaches them on its row (`goals.intake._known_rows`).
_DECOMPOSE = (WHO, HOW)
# 怎么学比较好 is the teaching spec's rules as he stated them (`guidelines.said_rules`): the writers
# and the assistant already get those rules from the spec (`PageSpec.guidelines`, the assistant's
# principles list), so their profile block leaves it out — each rule reaches a model once (stage
# 3 T6). The calls that read no spec (intake, path, reading, review) get it here. 已经会的 (K's
# view) goes to no call: a call that needs K reads K.
_PAGE = (WHO, GAPS)

# purpose prefix -> (sections, token budget); the longest matching prefix wins
PURPOSES: Mapping[str, tuple[tuple[str, ...], int]] = {
    "assistant.turn": ((WHO, WANT, WHERE, GAPS), 1200),
    "goals.intake": (_DECOMPOSE, 600),
    "goals.enter": (_DECOMPOSE, 600),
    "goals.page_fit": (_DECOMPOSE, 600),
    "goals.reading.unit": (_LEARN, 600),       # 看这一段 (reading-mode spec §3.5)
    "goals.path": ((WHO, WANT, HOW, WHERE), 600),
    "deep.": (_PAGE, 800),
    "review.": ((GAPS, HOW), 400),
}


def _rule_for(purpose: str) -> tuple[tuple[str, ...], int] | None:
    hits = [p for p in PURPOSES
            if purpose == p or purpose.startswith(p if p.endswith(".") else f"{p}.")]
    return PURPOSES[max(hits, key=len)] if hits else None


def context_for(sections: Mapping[str, Sequence[str]], purpose: str,
                owned: Mapping[str, Sequence[str]] = {}) -> str:
    """The profile block for one call of `purpose` ("" = this call gets none, or nothing is
    known yet). `owned`: his own lines per section (a subset of `sections`), placed first."""
    rule = _rule_for(purpose)
    if rule is None:
        return ""
    names, budget = rule
    lines = {s: tuple(sections.get(s, ())) for s in names}
    if not any(lines.values()):
        return ""
    split = {(s, own): tuple(x for x in lines[s] if (x in owned.get(s, ())) == own)
             for s in names for own in (True, False)}
    # his lines of every section first, then the derived ones (a header is paid per part, so a
    # section kept in both passes is counted twice: the estimate stays on the safe side)
    order = [(s, own) for own in (True, False) for s in names]
    pack = assemble([Part(name=f"{s}:{own}", header=f"## {s}",
                          items=tuple(f"- {x}" for x in split[s, own])) for s, own in order],
                    budget)
    blocks = []
    for s in names:          # shown in the note's order: derived, then his
        kept = {x for own in (True, False) for x in split[s, own][:pack.kept(f"{s}:{own}")]}
        body = [f"- {x}" for x in lines[s] if x in kept]
        if body:
            blocks.append(f"## {s}\n" + "\n".join(body))
    if not blocks:
        return ""
    head = f"{HEADER}\n{RULE}" if names != _DECOMPOSE else HEADER   # it is about 想学什么
    return f"{head}\n\n" + "\n\n".join(blocks)
