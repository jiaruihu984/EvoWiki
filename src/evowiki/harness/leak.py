"""What a pack diff adds, and whether that text is about one case (a variant of the leakage audit
Meta-Harness runs on candidate diffs: grep the diff for the cases' proper nouns and sentences).

`added_only`: only the sentences a text value adds to what is in force now; a whole prompt rewritten
to change one line adds one line. The rule guard reads this, so a candidate is never blamed for
default text it did not write.

`leaks`: a diff that writes a page title, a quoted memory or rule, a copied sentence of the learner
or a record id into a standing instruction is an instruction about one item, not a setting. It is
refused by code, before any replay is paid for; the improver hears it when it proposes
(`improver._diff`) and the round checks the composed diff again."""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from evowiki.harness.diff import KeyOp, PackDiff
from evowiki.harness.pack import Value

COPIED_CHARS = 12          # this many consecutive characters of his words = a copied sentence
QUOTED_CHARS = 4           # a quoted fragment shorter than this is a word, not an item
TITLE_CHARS = 2
HEADING_CHARS = 4          # this many consecutive characters of a heading = its example or its topic
TEXT_DIMS = ("H1", "H2", "H8")

_SENTENCE = re.compile(r"[^。！？；!?;\n]+[。！？；!?;\n]*")
_QUOTED = re.compile(r"「([^」]+)」|『([^』]+)』|“([^”]+)”|\"([^\"]+)\"")
_RECORD_ID = re.compile(r"\b(?:pg|sig|msg|act|asess|ent|rd|si|arat|aact|asel|wev)[_:][0-9a-zA-Z]{6,}")


def _norm(s: str) -> str:
    return "".join(s.split())


def sentences(text: str) -> list[str]:
    return [m.group().strip() for m in _SENTENCE.finditer(text) if m.group().strip()]


def added_text(before: str, after: str) -> str:
    """The sentences of `after` that are not in `before`, in order."""
    old = {_norm(s) for s in sentences(before)}
    return "\n".join(s for s in sentences(after) if _norm(s) not in old)


def _as_text(value: Value | None) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def added_only(diff: PackDiff, before: Mapping[str, Value | None],
               defaults: Mapping[str, Value]) -> PackDiff:
    """`diff` with each H1 / H2 / H8 value reduced to what it adds to the text in force (the pack's
    value, else the default); a value that only removes text is left out. Other ops stay as they are."""
    ops: list[KeyOp] = []
    for o in diff.ops:
        if o.value is None or o.name.split(".", 1)[0] not in TEXT_DIMS:
            ops.append(o)
            continue
        was = before.get(o.name)
        now = added_text(_as_text(was if was is not None else defaults.get(o.name)),
                         _as_text(o.value))
        if now:
            ops.append(KeyOp(o.name, now))
    return replace(diff, ops=tuple(ops))


@dataclass(frozen=True)
class CaseWords:
    """What only the round's cases and this learner's own items say."""

    titles: tuple[str, ...] = ()        # page titles of the cases
    headings: tuple[str, ...] = ()      # their section headings: an example lifted from them
    items: tuple[str, ...] = ()         # his rules, memories and decisions: quoting one names it
    said: tuple[str, ...] = ()          # the learner's words in the round's signals


def _copied(text: str, said: Iterable[str]) -> str | None:
    t = _norm(text)
    for s in said:
        n = _norm(s)
        for i in range(max(0, len(n) - COPIED_CHARS + 1)):
            if n[i:i + COPIED_CHARS] in t:
                return n[i:i + COPIED_CHARS]
    return None


def _shared_with_heading(text: str, headings: Iterable[str], before: str) -> str | None:
    """A run of HEADING_CHARS characters of a case's heading in `text`, not in the text in force."""
    t, old = _norm(text), _norm(before)
    for h in headings:
        n = _norm(h)
        for i in range(max(0, len(n) - HEADING_CHARS + 1)):
            run = n[i:i + HEADING_CHARS]
            if run in t and run not in old:
                return run
    return None


def _named_item(fragment: str, items: Iterable[str]) -> str | None:
    f = _norm(fragment)
    return next((i for i in items if f in _norm(i) or _norm(i) in f), None)


def leaks(diff: PackDiff, before: Mapping[str, Value | None], defaults: Mapping[str, Value],
          words: CaseWords) -> tuple[str, ...]:
    """Each thing the diff's added text says about one case or item, in plain Chinese; empty = none."""
    added = added_only(diff, before, defaults)
    old = "\n".join(_as_text(before.get(o.name) if before.get(o.name) is not None
                             else defaults.get(o.name)) for o in diff.ops)
    texts = [_as_text(o.value) for o in added.ops if o.name.split(".", 1)[0] in TEXT_DIMS]
    texts += [s.text or "" for s in diff.spec_ops if getattr(s, "text", None)]
    out: list[str] = []
    for text in texts:
        out += [f"写进了记录编号 {m.group()}" for m in _RECORD_ID.finditer(text)]
        out += [f"写进了这几页的标题「{t}」" for t in words.titles
                if len(_norm(t)) >= TITLE_CHARS and _norm(t) in _norm(text)]
        for m in _QUOTED.finditer(text):
            frag = next(g for g in m.groups() if g)
            if len(_norm(frag)) >= QUOTED_CHARS and _named_item(frag, words.items):
                out.append(f"引用了他的某一条记忆或规则「{frag}」")
        lifted = _shared_with_heading(text, words.headings, old)
        if lifted:
            out.append(f"用了这几段自己的内容「{lifted}」")
        copied = _copied(text, words.said)
        if copied:
            out.append(f"抄了他的原话「{copied}」")
    return tuple(dict.fromkeys(out))
