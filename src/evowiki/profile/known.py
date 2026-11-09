"""A line he types into 「已经会的」 is a self-report in K (user ruling 2026-10-29: 你会什么，在 K 里).
No model call: code matches the line to one point and the aspects it names.

- the point: a recorded concept name in the line (the longest; two different points in one line
  is not matched: one line, one point);
- the aspects: the plain words of each aspect the point has (「均值：会怎么算」, the derived lines'
  own words, or the aspect labels 「计算过程」); there is no every-aspect default
  (`goals.selfreport`: 「我会这个」 never makes a whole point known without answers), so a line
  without one is not matched;
- the claim is `can_do` (会用) at the self-report table's strength; K weighs it.

A line that does not match is never stored: the note he gets back keeps it in 已经会的 with
why it was not recorded (`unmatched`), so he can fix or delete it; the next save tries it again.
A matched line is one `Observe` + `AddNote` per cell (`evidence_ref = profile:<hash>#<i>`, the
note text = his line), and 已经会的 shows it back from K (`profile.derive`).

A line he deletes takes back what he said about the cells it names (`retraction`): every standing
`+` self-report on them (typed here, said in the chat, ticked in the 摸底) stops counting, a 「会」 he
declared is cleared, their notes retire. K's bands follow, and so do the path's 可以跳过, the words the
UI shows and the writer's K. What his answers proved stays: nothing he was tested on is touched. The undo
data says it all again (`restored`): new events, the log only grows."""

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from evowiki.belief.aspects import (
    AddNote,
    AspectEvent,
    AspectK,
    Declare,
    Observe,
    RetireNote,
    RetractSelf,
)
from evowiki.goals.contracts import Aspect, Observation, split_cell
from evowiki.goals.selfreport import self_observation
from evowiki.i18n import tr
from evowiki.teach.textsim import squash
from evowiki.wikiapp.kwrite import NOTE_MAX
from evowiki.wikiapp.state import WikiState

PROFILE_REF = "profile:"
CLAIM = "can_do"
# what an aspect is about, as he would say it (「还不清楚是什么、什么时候用」)
PLAIN = {Aspect.TERM: "是什么", Aspect.PURPOSE: "什么时候用", Aspect.NOTATION: "公式和记号",
         Aspect.PROCEDURE: "怎么算", Aspect.INTERPRET: "结果怎么看", Aspect.MECHANISM: "为什么成立"}
# the words that name an aspect in his line: the plain words and the aspect labels
WORDS: dict[Aspect, tuple[str, ...]] = {
    Aspect.TERM: ("是什么", "术语", "定义", "definition", "terminology", "what it is"),
    Aspect.PURPOSE: ("什么时候用", "用途", "何时用", "purpose", "when to use", "when it is used"),
    Aspect.NOTATION: ("公式和记号", "公式", "记号", "formula", "formulas", "notation"),
    Aspect.PROCEDURE: ("怎么算", "计算过程", "计算", "calculation", "calculate", "how to compute", "procedure"),
    Aspect.INTERPRET: ("结果怎么看", "解读结果", "解读", "interpret", "interpretation", "read the result"),
    Aspect.MECHANISM: ("为什么成立", "原理", "推导", "mechanism", "derivation", "why it works"),
}

NOT_RECORDED = re.compile(r"（没记下：[^）]*）$|\s*\(Not recorded: [^)]*\)$", re.IGNORECASE)
# A known-aspects line is affirmative evidence, never an uncertainty, question or condition.
_NOT_AFFIRMATIVE = re.compile(
    r"[?？]|不(?:会|懂|理解|清楚|知道|确定)|没(?:学过|懂|理解)|如果|假如|要是|"
    r"\b(?:not|never|if|unless|might|unsure|uncertain|cannot|can['’]t|don['’]t|didn['’]t)\b",
    re.IGNORECASE)
NO_WIKI = "还没有知识点，先学一个目标再写"
NO_POINT = "没找到这个知识点，要用知识点的名字"
TWO_POINTS = "一行只写一个知识点"
NO_ASPECT = "要写清会它的哪方面，比如「{}：会{}」"
TOO_LONG = f"太长了，最多 {NOTE_MAX} 字"
BUSY = "K 正在被别的操作改动，这次没有记下，再保存一次"


@dataclass(frozen=True)
class Matched:
    line: str
    cells: tuple[str, ...]


def clean(line: str) -> str:
    """His line without the 「（没记下：…）」 the note added last time."""
    return NOT_RECORDED.sub("", " ".join(line.split())).strip()


def unmatched(line: str, why: str, *, lang: str = "zh") -> str:
    """The line as the note shows it back: his words, then why it was not recorded."""
    return tr("{line} (Not recorded: {reason})", lang=lang, line=line, reason=why)


def match(state: WikiState, k: AspectK, line: str, *, lang: str | None = None) -> Matched | str:
    """The point and aspects `line` names, or why it names none (module doc)."""
    lang = lang or state.language
    if _NOT_AFFIRMATIVE.search(line):
        return tr("Only record aspects you explicitly say you know.", lang=lang)
    if len(line) > NOTE_MAX:
        return tr("This line is too long (maximum {limit} characters).", lang=lang, limit=NOTE_MAX)
    names = [(c, state.names[c].name) for c in k.concept_ids()
             if c in state.names and state.names[c].name]
    if not names:
        return tr(NO_WIKI, lang=lang)
    q = squash(line).casefold()
    hits = [(c, n) for c, n in names if squash(n) and squash(n).casefold() in q]
    if not hits:
        return tr(NO_POINT, lang=lang)
    cid, name = max(hits, key=lambda h: len(squash(h[1])))
    if any(c != cid and squash(n) not in squash(name) for c, n in hits):
        return tr(TWO_POINTS, lang=lang)
    rest = re.sub(re.escape(name), "", line, flags=re.IGNORECASE)
    aspects = [a for a in k.aspects(cid) if any((re.search(r"\b" + re.escape(w) + r"\b", rest, re.IGNORECASE)
                                                     if w.isascii() else w in rest) for w in WORDS[a])]
    if not aspects:
        first = k.aspects(cid)[0] if k.aspects(cid) else Aspect.PROCEDURE
        return tr("Specify which aspect you know, for example: {name}: I know {aspect}.",
                  lang=lang, name=name, aspect=tr(PLAIN[first], lang=lang))
    return Matched(line=line, cells=tuple(f"{cid}.{a.value}" for a in aspects))


@dataclass(frozen=True)
class Retraction:
    lines: tuple[str, ...]                 # the deleted lines that took back something
    aspect: tuple[AspectEvent, ...]
    undo: dict                             # what `restored` needs (a ledger row's undo data)


def retraction(state: WikiState, k: AspectK, log: Sequence[AspectEvent], lines: Sequence[str],
               *, ts: datetime, new_id: Callable[[str], str]) -> Retraction | None:
    """The events that take back what the deleted `lines` said (module doc); None: they said nothing
    K holds as his word (lines that only show what his answers proved, or name no point)."""
    said = {e.event_id: e for e in log if isinstance(e, Observe)}
    out: list[AspectEvent] = []
    took: list[str] = []
    done: set[str] = set()
    observed: list[dict] = []
    declared: list[str] = []
    notes: list[dict] = []
    for line in lines:
        m = match(state, k, clean(line))
        if isinstance(m, str):
            continue
        before = len(out)
        for cell in m.cells:
            if cell in done:
                continue
            done.add(cell)
            refs = {f"skip:{split_cell(cell)[0]}", f"declare:{cell}"}
            for eid in k.self_reports(cell):
                refs.add(said[eid].observation.evidence_ref)
                observed.append(said[eid].observation.model_dump(mode="json"))
                out.append(RetractSelf(event_id=new_id("aev"), ts=ts, source_event_id=eid))
            if k.belief(cell, ts).declared == "know":
                declared.append(cell)
                out.append(Declare(event_id=new_id("aev"), ts=ts, cell=cell, value=None))
            for n in k.notes(split_cell(cell)[0]):
                if n.cell == cell and n.evidence_ref in refs:
                    notes.append({"cell": cell, "text": n.text, "evidence_ref": n.evidence_ref,
                                  "label": n.label})
                    out.append(RetireNote(event_id=new_id("aev"), ts=ts, entry_id=n.entry_id))
        if len(out) > before:
            took.append(m.line)
    if not out:
        return None
    return Retraction(tuple(took), tuple(out), {"lines": took, "observed": observed,
                                                "declared": declared, "notes": notes})


def restored(undo: dict, *, ts: datetime, new_id: Callable[[str], str]) -> list[AspectEvent]:
    """What `retraction` took back, said again."""
    out: list[AspectEvent] = [Observe(event_id=new_id("aev"), ts=ts,
                                      observation=Observation.model_validate(o))
                              for o in undo["observed"]]
    out += [Declare(event_id=new_id("aev"), ts=ts, cell=c, value="know") for c in undo["declared"]]
    out += [AddNote(event_id=new_id("aev"), ts=ts, **n) for n in undo["notes"]]
    return out


def ref_of(line: str) -> str:
    return PROFILE_REF + hashlib.sha256(line.encode("utf-8")).hexdigest()[:12]


def observations(m: Matched) -> tuple[Observation, ...]:
    """The self-report of `m` on each of its cells (the note text is his line)."""
    ref = ref_of(m.line)
    return tuple(self_observation(CLAIM, c, quote=m.line, evidence_ref=f"{ref}#{i}")
                 for i, c in enumerate(m.cells))


def label(line: str, *, lang: str = "zh") -> str:
    return tr("You wrote in your background: {line} (can use)", lang=lang, line=line)


def split(state: WikiState, k: AspectK, lines: Sequence[str], *, lang: str | None = None
          ) -> tuple[list[Matched], list[str]]:
    """`lines` as matched ones and the note's unmatched lines (with why)."""
    ok: list[Matched] = []
    bad: list[str] = []
    for raw in lines:
        line = clean(raw)
        if not line:
            continue
        m = match(state, k, line, lang=lang)
        if isinstance(m, str):
            bad.append(unmatched(line, m, lang=lang or state.language))
        else:
            ok.append(m)
    return ok, bad
