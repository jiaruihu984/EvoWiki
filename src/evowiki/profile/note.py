"""The profile note: its sections, its text, and the learner's edits of it.

Each thing is kept in ONE place (user ruling 2026-10-29: 「你是谁、你想要什么，在档案里；以后怎么教你，
在规则里；你会什么，在 K 里」). The profile stores only *owned* lines of 我是谁 and 想学什么
(`STORED`): what he typed there, and the facts the assistant wrote down from his own words
(`add_fact`). Every other section is a view the server derives (`profile.derive`): 怎么学比较好 is
his teaching rules (the spec), 已经会的 is K, 学到哪了 / 还不会的 are progress and K. The note shows
owned lines after the derived ones.

His edit wins (`merge_edit`): a line he added or changed in a stored section becomes owned; a
line he typed into a view goes to that view's store (`Edit.added`: 怎么学比较好 → a rule,
已经会的 → a self-report in K; the service does both); a line typed under 学到哪了 / 还不会的 or a
heading of his own is kept under 我是谁 (like text before the first heading). A derived line he
deleted is dropped and stays hidden while the server still derives it, and deleting a line of 怎么学比较好
revokes that rule, of 已经会的 takes back what he said in K (`Edit.removed`, `known.retraction`). Only a line that left the note counts as
deleted: moved under another heading it stays what it was, and a rule's text is never his line of
another section. A note without a single line (an empty file, a template of bare headings) says
nothing. `sent` remembers the derived lines of the copy last written to the vault, so a line derived
after that copy is never read as one he deleted.

Every copy has a version (`note_version`, the hash of its text), and a save names the copy it was
based on (`base`). Only a save based on the copy the server last handed out can retract: `sent` and
`sent_owned` describe that copy and no other. Any other save (another window, a note restored from
elsewhere, a client that names no base) fails closed: it adds his lines, deletes nothing and reports
what the note lacks (`Edit.kept`), so both sides stay."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from evowiki.teach.textsim import bigram_jaccard, normalize

WHO = "我是谁"                 # who he is, what he did, his non-concept skills (会用 Excel)
WANT = "想学什么"
HOW = "怎么学比较好"            # his teaching rules (`guidelines.said_rules`): a view of the spec
WHERE = "学到哪了"
KNOWN = "已经会的"              # what K holds he knows: a view of K
GAPS = "还不会的"

SECTIONS: tuple[str, ...] = (WHO, WANT, HOW, WHERE, KNOWN, GAPS)
SECTION_IDS = dict(zip(SECTIONS, ("who", "want", "how", "where", "known", "gaps"), strict=True))
EN_HEADINGS = dict(zip(SECTIONS, ("About me", "What I want to learn", "How I learn best",
                                    "My progress", "What I know", "What I need to learn"), strict=True))
_HEADINGS = {**{s: s for s in SECTIONS}, **{v: k for k, v in EN_HEADINGS.items()}}
_LINE_REF = re.compile(r"^<!--evowiki:derived ([a-f0-9]{16}) ([a-f0-9]{16})-->$")
# the sections the profile stores lines of; a fact from the chat may go only here
FACT_SECTIONS: tuple[str, ...] = (WHO, WANT)
STORED = FACT_SECTIONS
# views whose typed lines go to their own store (the service writes them, `Edit`)
VIEWS: tuple[str, ...] = (HOW, KNOWN)

Lines = Mapping[str, Sequence[str]]

_HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")


class FactMeta(BaseModel):
    """Where a fact the assistant wrote down came from (memory contract `Provenance`): his words
    in this message (explicit_current), in an earlier one he confirmed (explicit_history), or
    inferred and confirmed by him. `origin_ref`: `<session>/<message>` of the words (or
    `deleted:…` once that session was deleted); `evidence`: 「你说过「…」」 (redacted with the
    session). Only facts from the chat carry it; a line he typed into the note has none."""

    model_config = ConfigDict(frozen=True)

    provenance: str
    origin_ref: str | None = None
    evidence: str | None = None
    action_id: str | None = None        # the ledger row that wrote it


class Profile(BaseModel):
    model_config = ConfigDict(frozen=True)

    owned: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    dropped: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    sent: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    # line_key(section, line) -> where that owned line came from (facts from the chat only)
    fact_meta: dict[str, FactMeta] = Field(default_factory=dict)
    # his 已经会的 lines of the last save that K did not take, each with why (`known.unmatched`):
    # shown in the note until he changes them, never in a prompt, never in K
    unmatched: tuple[str, ...] = ()
    # the owned lines and the version of the copy last handed out (`with_sent`)
    sent_owned: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    version: str = ""


def line_key(section: str, line: str) -> str:
    """The key of one owned line: its section and a hash of its text."""
    import hashlib
    h = hashlib.sha256(" ".join(line.split()).encode("utf-8")).hexdigest()[:12]
    return f"{section}:{h}"


def note_version(text: str) -> str:
    """The version of one copy of the note."""
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _norm(line: str) -> str:
    return " ".join(line.split())


def owned_lines(profile: Profile) -> dict[str, tuple[str, ...]]:
    """His stored lines: only those of `STORED` (an old profile's lines of other sections are not
    read; no migration)."""
    return {s: v for s, v in profile.owned.items() if s in STORED and v}


def shown(profile: Profile, derived: Lines) -> dict[str, tuple[str, ...]]:
    """Every section's lines as the note shows them: derived (minus the dropped), then owned."""
    own_all = owned_lines(profile)
    out: dict[str, tuple[str, ...]] = {}
    for s in SECTIONS:
        drop = set(profile.dropped.get(s, ()))
        own = own_all.get(s, ())
        lines = [d for d in derived.get(s, ()) if d not in drop and d not in own]
        out[s] = tuple(dict.fromkeys([*lines, *own]))
    return out


def render(sections: Lines) -> str:
    """The note's text: one `## heading` per section, a bullet per line (empty sections keep their
    heading, so he can write under it)."""
    blocks = []
    for name, lines in sections.items():
        body = "\n".join(f"- {line}" for line in lines)
        blocks.append(f"## {name}\n{body}" if body else f"## {name}")
    return "\n\n".join(blocks) + "\n"


def render_note(profile: Profile, derived: Lines, *, language: str,
                display: Mapping[str, Mapping[str, str]]) -> str:
    """A vault projection whose line identities survive a display-language switch."""
    sections = shown(profile, derived)
    if profile.unmatched:
        sections[KNOWN] = tuple(dict.fromkeys([*sections[KNOWN], *profile.unmatched]))
    blocks = []
    for section, lines in sections.items():
        heading = EN_HEADINGS[section] if language == "en" else section
        body = []
        for line in lines:
            visible = display.get(section, {}).get(line, line)
            if line in derived.get(section, ()):
                identity = note_version(f"{SECTION_IDS[section]}\0{line}")
                body.append(f"<!--evowiki:derived {identity} {note_version(_norm(visible))}-->")
            body.append(f"- {visible}")
        blocks.append("\n".join([f"## {heading}", *body]))
    return "\n\n".join(blocks) + "\n"


def parse(text: str, *, canonical: Mapping[str, str] | None = None) -> dict[str, tuple[str, ...]]:
    """The note's sections as he left them. Text before the first heading is his and goes to
    我是谁; a line is a bullet's text or any other non-empty line; frontmatter is skipped."""
    out: dict[str, list[str]] = {}
    current = WHO
    pending = None
    body = text
    if body.startswith("---\n"):
        end = body.find("\n---", 4)
        body = body[end + 4:] if end >= 0 else body
    for raw in body.splitlines():
        if ref := _LINE_REF.fullmatch(raw.strip()):
            pending = ref
            continue
        m = _HEADING.match(raw)
        if m:
            pending = None
            heading = m.group(1).strip()
            current = _HEADINGS.get(heading, heading)
            out.setdefault(current, [])
            continue
        if raw.lstrip().startswith("#"):
            continue
        line = _norm(_BULLET.sub("", raw))
        if pending is not None and line:
            if canonical is not None and note_version(line) == pending[2]:
                # An expired system line never becomes an asserted user fact.
                line = canonical.get(pending[1], "")
            pending = None
        if line.startswith("<!--") and line.endswith("-->"):
            continue
        if line:
            out.setdefault(current, []).append(line)
    return {k: tuple(dict.fromkeys(v)) for k, v in out.items()}


@dataclass(frozen=True)
class Edit:
    """What one save of the note did. `profile`: the new profile (owned lines of `STORED` only).
    `added`: per view (`VIEWS`), the lines he typed there (怎么学比较好 → new rules, 已经会的 →
    self-reports); `removed`: 怎么学比较好's derived lines he deleted (rules to revoke); `kept`: his
    rules and owned lines a save that may not retract left out of the note (they stay)."""

    profile: Profile
    added: dict[str, tuple[str, ...]] = field(default_factory=dict)
    removed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    kept: tuple[str, ...] = ()


def merge_edit(profile: Profile, derived: Lines, text: str, base: str | None = None) -> Edit:
    """The profile after he saved the note as `text`, a copy of version `base` (module doc). A section
    whose heading is gone (renamed, a note of an older format) deletes nothing: its lines come back,
    rules and K untouched. Derived lines he left in place stay derived; lines that are neither derived
    now nor in the last copy he saw are his: owned in 我是谁 / 想学什么, `added` in a view, else kept
    under 我是谁; a line of that copy he deleted leaves the note (a derived one is dropped and comes back
    once he types it again, in the views `removed`). A line still somewhere in the note was not deleted,
    and a current rule's text stays a rule wherever it is. A save based on any other copy adds his
    lines only: 我是谁 / 想学什么, his own headings and 怎么学比较好 (已经会的 of an old copy is the server's
    old words, not a self-report); nothing leaves, and a line hidden before shows again if the note has it."""
    identities = {note_version(f"{SECTION_IDS[s]}\0{line}"): line
                  for source in (derived, profile.sent) for s, lines in source.items()
                  if s in SECTION_IDS for line in lines}
    edited = parse(text, canonical=identities)
    in_note = {x for v in edited.values() for x in v}
    derived_lines = {x for source in (derived, profile.sent) for lines in source.values() for x in lines}
    rules = set(derived.get(HOW, ()))
    current = owned_lines(profile)
    missing = tuple(sorted((rules | {x for v in current.values() for x in v}) - in_note))
    if not in_note:
        return Edit(profile=profile, kept=missing)
    trusted = bool(profile.version) and base == profile.version
    mine: dict[str, tuple[str, ...]] = {}
    dropped: dict[str, tuple[str, ...]] = {}
    for s in dict.fromkeys([*SECTIONS, *edited]):
        lines = edited.get(s, ())
        sent = set(profile.sent.get(s, ()))
        typed = tuple(x for x in lines if x not in derived_lines)
        if typed:
            mine[s] = typed
        if s not in edited:           # no heading: he said nothing about this section
            if s in profile.dropped:
                dropped[s] = profile.dropped[s]
            continue
        hidden = set(profile.dropped.get(s, ()))
        gone = ((sent | hidden) if trusted else hidden) - in_note
        if gone:
            dropped[s] = tuple(sorted(gone))
    owned: dict[str, tuple[str, ...]] = {}
    for s in STORED:
        deleted = (set(profile.sent_owned.get(s, ())) - set(edited[s])
                   if trusted and s in edited else set())
        lines = [x for x in current.get(s, ()) if x not in deleted and x not in rules]
        lines = list(dict.fromkeys([*lines, *mine.get(s, ())]))
        if lines:
            owned[s] = tuple(lines)
    # lines under a heading he renamed: the ones the note already shows elsewhere stay there
    shown_lines = {x for src in (derived, profile.sent) for v in src.values() for x in v}
    own_only = () if trusted else SECTIONS
    elsewhere = [x for s, v in mine.items() if s not in STORED and s not in VIEWS and s not in own_only
                 for x in v if x not in shown_lines]
    if elsewhere:
        owned[WHO] = tuple(dict.fromkeys([*owned.get(WHO, ()), *elsewhere]))
    revoked = dropped.pop(HOW, None)
    removed = {HOW: revoked} if revoked and trusted else {}
    # (a line hidden before was not in the copy he saved: `sent` is what the server derived, not what it showed)
    took_back = (set(profile.sent.get(KNOWN, ())) - set(profile.dropped.get(KNOWN, ())) - in_note
                 if trusted and KNOWN in edited else set())
    if took_back:
        removed[KNOWN] = tuple(sorted(took_back))
    views = VIEWS if trusted else (HOW,)
    return Edit(profile=profile.model_copy(update={"owned": owned, "dropped": dropped}),
                added={s: mine[s] for s in views if s in mine},
                removed=removed,
                kept=() if trusted else missing)


def scoped_line(topic: str | None, text: str) -> str:
    """The profile line of a fact said about one domain or skill: it names that domain, so 「什么都不
    会」 said about Python never reads as a statement about the whole person (`topic` None or
    blank: the fact is about him). A line that already speaks of it (the live reply: topic
    「线性代数（矩阵运算）」, line 「线性代数学过，矩阵运算很熟」) stays as it is."""
    t, line = _norm(topic or ""), _norm(text)
    if not t or t.casefold() in line.casefold() or bigram_jaccard(normalize(t), normalize(line)) > 0:
        return line
    return f"{t}：{line}"


def add_fact(profile: Profile, section: str, text: str, replaces: str = "",
             meta: FactMeta | None = None) -> Profile:
    """One fact in `section` (owned): `replaces` names the owned line it updates (exact text);
    a line already there is not added twice. `meta`: where it came from (`fact_meta`; the
    replaced line's entry goes)."""
    if section not in FACT_SECTIONS:
        raise ValueError(f"not a fact section: {section}")
    line, old = _norm(text), _norm(replaces)
    lines = list(profile.owned.get(section, ()))
    metas = dict(profile.fact_meta)
    if old and old in lines:
        lines[lines.index(old)] = line
        metas.pop(line_key(section, old), None)
    elif line not in lines:
        lines.append(line)
    if meta is not None:
        metas[line_key(section, line)] = meta
    return profile.model_copy(update={"owned": {**profile.owned,
                                                section: tuple(dict.fromkeys(lines))},
                                      "fact_meta": metas})


def with_sent(profile: Profile, derived: Lines, version: str = "") -> Profile:
    """`profile` remembering the copy just handed out: its derived lines, its owned lines, its version."""
    return profile.model_copy(update={"sent": {s: tuple(v) for s, v in derived.items() if v},
                                      "sent_owned": owned_lines(profile), "version": version})
