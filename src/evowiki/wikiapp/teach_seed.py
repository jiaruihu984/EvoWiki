"""Seeding a user's teaching spec from files (page quality A, 2026-10-13): a style guide the user
already wrote (one rule per paragraph) and an exemplar note they hold up as good, written into their
own spec without an LLM call. `scripts/seed_teach_style.py` is the command line.

One commit, one new spec version (action `import`): the new items (origin `seed`,
`origin="seed:<name>"`; a text the spec already has is skipped, so a rerun adds nothing), the
exemplar (skipped when one with the same `ref` is there) and, when given, `analogy_level`. Fail
closed: over 30 items, an item over 500 characters, a 4th exemplar or an exemplar over 60_000
characters → SpecInvalid and nothing is written. Nothing new at all → no commit (None)."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from evowiki.teach.contracts import Exemplar, SpecItem
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Repo
from evowiki.wikiapp.teach import commit_spec

MAX_EXEMPLAR_TEXT = 60_000
_FRONT = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_EMBED = re.compile(r"^```embedded-html\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)
_TITLE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)
_WIKILINK = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


@dataclass(frozen=True)
class SeedExemplar:
    ref: str        # where it came from (vault-relative path or file name)
    title: str
    text: str       # raw note text; `exemplar_text` converts it


def style_items(text: str) -> list[str]:
    """One rule per paragraph (blank-line separated), a leading list marker dropped, lines joined."""
    out = []
    for para in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [_BULLET.sub("", ln).strip() for ln in para.splitlines()]
        rule = "".join(ln for ln in lines if ln)
        if rule:
            out.append(rule)
    return out


def exemplar_text(note: str) -> str:
    """The note without vault-only syntax: front matter dropped, each `embedded-html` block becomes
    one line naming the interactive, `[[a|b]]` / `[[a]]` / `![[a]]` become `b` / `a`."""
    s = _FRONT.sub("", note.replace("\r\n", "\n"), count=1)

    def embed(m: re.Match[str]) -> str:
        t = _TITLE.search(m.group(1))
        return f"（这里是一个交互：{t.group(1) if t else '交互图'}）"

    s = _EMBED.sub(embed, s)
    s = _WIKILINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), s)
    return s.strip() + "\n"


def seed_style(repo: Repo, user_id: str, *, items: Sequence[str], origin: str,
               exemplar: SeedExemplar | None, analogy: int | None, now: datetime,
               new_id: Ids) -> int | None:
    """Writes the seed (module doc); the new spec version, or None when nothing was new."""
    snap = repo.load(user_id)
    spec = tspec.effective_spec(snap.state)
    have = {i.text.strip() for i in spec.items}
    texts = [t.strip() for t in items if t.strip() and t.strip() not in have]
    texts = list(dict.fromkeys(texts))
    # rule provenance contract §7.1 #13: a script's style pack, not the user's own words
    new_items = [SpecItem(item_id=new_id("si"), text=tspec._text(t), origin="seed",
                          authority="rsi", rev_by="system",
                          origin_ref=(origin if origin.startswith("seed:") else f"seed:{origin}")[:200])
                 for t in texts]
    ex = None
    if exemplar is not None and all(e.ref != exemplar.ref for e in spec.exemplars):
        body = exemplar_text(exemplar.text)
        if len(body) > MAX_EXEMPLAR_TEXT:
            raise tspec.SpecInvalid(f"范例最多 {MAX_EXEMPLAR_TEXT} 字，这一篇有 {len(body)} 字")
        if len(spec.exemplars) >= tspec.MAX_EXEMPLARS:
            raise tspec.SpecInvalid(tspec.TOO_MANY_EXEMPLARS)
        ex = Exemplar(exemplar_id=new_id("ex"), kind="vault_note", ref=exemplar.ref[:400],
                      title=exemplar.title[:200], text=body)
    params = (spec.params.model_copy(update={"analogy_level": analogy})
              if analogy is not None and analogy != spec.params.analogy_level else None)
    if not new_items and ex is None and params is None:
        return None

    def change(s):
        nxt = tspec.append_imported(s, new_items)
        update = {}
        if ex is not None:
            update["exemplars"] = (*s.exemplars, ex)
        if params is not None:
            update["params"] = params
        return type(nxt).model_validate({**nxt.model_dump(), **update}) if update else nxt

    done = commit_spec(repo, snap, change, actor="system", action="import", origin=origin[:200],
                       now=now,
                       new_id=new_id)
    return tspec.effective_version(done.state)
