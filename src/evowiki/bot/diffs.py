"""The 改动对比 of an action (`ActionOut.diff`): lines of kind ctx / add / del, built from the
ledger row (and the wiki state, for names and the text an edit replaces), one helper per kind
of row. Read-only."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from evowiki.agent.tools import ProposalDraft
from evowiki.deep.tools import PLACED
from evowiki.goals.contracts import SectionRole
from evowiki.i18n import current_language, tr
from evowiki.store.bot import ActionRow
from evowiki.wikiapp.events import PageRecord
from evowiki.wikiapp.revise import KIND as PAGE_REVISE
from evowiki.wikiapp.revise import RevisionDraft
from evowiki.wikiapp.state import WikiState

Line = dict[str, str]


def ctx(text: str) -> Line:
    return {"kind": "ctx", "text": text}


def add(text: str) -> Line:
    return {"kind": "add", "text": text}


def rem(text: str) -> Line:
    return {"kind": "del", "text": text}


class Names:
    """Readable names for the ids a diff mentions (pages, concepts, hubs), the written sections
    of a page, and the title of a stored EvoUI block (`asset_title`: asset id -> title, None when
    unknown; without it an `asset: <id>` fence shows as 互动)."""

    def __init__(self, state: WikiState | None,
                 asset_title: Callable[[str], str | None] | None = None):
        self._state = state
        self._asset_title = asset_title
        self._assets: dict[str, str] = {}

    def asset(self, asset_id: str) -> str:
        if asset_id not in self._assets:      # read once per Names (a diff shows a block on both sides)
            t = None if self._asset_title is None else self._asset_title(asset_id)
            self._assets[asset_id] = t or "互动"
        return self._assets[asset_id]

    def page(self, pid: str) -> str:
        rec = None if self._state is None else self._state.pages.get(pid)
        return rec.title if rec is not None and rec.title else "未命名页面"

    def concept(self, cid: str) -> str:
        n = None if self._state is None else self._state.names.get(cid)
        return n.name if n is not None and n.name else "未命名概念"

    def record(self, page_id: str) -> PageRecord | None:
        return None if self._state is None else self._state.pages.get(page_id)

    def section(self, page_id: str, section_id: str) -> tuple[str, str] | None:
        """(heading, body) of a section, None when unknown."""
        rec = None if self._state is None else self._state.pages.get(page_id)
        if rec is None or rec.page is None:
            return None
        heading = next((s.heading for s in rec.page.outline.sections if s.id == section_id), None)
        text = rec.page.texts.get(section_id)
        return None if heading is None else (heading, "" if text is None else text.body)


_PAGE_LINK = re.compile(r"\[([^\]]*)\]\(#p:[^)]*\)")


def proposal_change(data: Any, user_id: str, draft: ProposalDraft) -> dict[str, Any]:
    """`ProposalChange` of a page-edit proposal against the page as it is now: a link to a page
    shows as its words."""
    def shown(body: str) -> str:
        return _PAGE_LINK.sub(r"\1", shown_body(body, {}))

    old_heading, old_body = None, ""
    if draft.kind == "rewrite_section":
        view = data.page(user_id, draft.page_id)
        sec = next((s for s in view.sections if s.section_id == draft.section_id), None) if view else None
        if sec is not None:
            old_heading, old_body = sec.heading, shown(sec.body)
    return {"old_heading": old_heading, "old_body": old_body, "new_body": shown(draft.text)}


def _lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def same_text(a: str, b: str) -> bool:
    """Equal but for whitespace (spaces, indentation, line breaks): the rule the plugin's review
    mode compares paragraphs by (revise/model.ts `paraDiff`), so a section whose only difference is
    a re-indented code block or formula is neither marked nor counted as changed."""
    return "".join(a.split()) == "".join(b.split())


# --- assistant edits ----------------------------------------------------------------------

def assistant_edit(row: ActionRow, names: Names) -> list[Line]:
    """Heading as context, the replaced text as del lines, the new text as add lines. An inserted
    section has no del lines; the last line of the section it follows is context."""
    d = ProposalDraft.model_validate(row.content)
    out: list[Line] = []
    old = ""
    if d.kind == "rewrite_section":
        prev = (row.undo or {}).get("prev_text")              # an executed edit: what it replaced
        if prev is not None:
            old = prev.get("body", "")
        elif d.section_id is not None and row.status != "executed":
            found = names.section(d.page_id, d.section_id)
            old = found[1] if found is not None else ""
    elif d.section_id is not None:
        found = names.section(d.page_id, d.section_id)
        before = _lines(found[1]) if found is not None else []
        if before:
            out.append(ctx(before[-1]))
    out.append(ctx(d.heading))
    out += [rem(ln) for ln in _lines(old)]
    out += [add(ln) for ln in _lines(d.text)]
    return out


# --- page revisions from materials --------------------------------------------------------

# a stored page carries its interactives as note blocks (`wikiapp.refine_apply._body`), a draft as
# placeholders - or as the note blocks it kept from the old text: all of them show as one line
# 〔互动：title〕, so an interactive that is still there is never a "change"
_EMBED = re.compile(r"^```embedded-html\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)
_EVOUI = re.compile(r"^```evoui\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)
_FIELD = re.compile(r"^(id|title):\s*(.+?)\s*$", re.MULTILINE)
_ASSET = re.compile(r"asset:\s*(\S+)")
_IMAGE = re.compile(r"^```evowiki-image\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)
_IMAGE_TITLE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)
_IMAGE_PLACED = re.compile(r"\{\{image:([^{}\s]+)\}\}")
SectionStatus = Literal["changed", "added", "removed"]


def interactive_line(title: str) -> str:
    return f"〔互动：{title}〕"


def image_line(title: str) -> str:
    return f"〔图片：{title}〕"


def _image_title(m: re.Match[str]) -> str:
    t = _IMAGE_TITLE.search(m.group(1))
    return t.group(1) if t is not None else "图片"


def _embed_fields(m: re.Match[str]) -> dict[str, str]:
    return {k: v for k, v in _FIELD.findall(m.group(1))}


def _evoui_title(m: re.Match[str], asset: Callable[[str], str] | None = None) -> str:
    ref = _ASSET.fullmatch(m.group(1).strip())
    if ref is not None:                       # a stored block (`asset_fence`): its title is in the asset
        return asset(ref.group(1)) if asset is not None else "互动"
    try:
        title = json.loads(m.group(1)).get("title")
    except (ValueError, AttributeError):
        title = None
    return title if isinstance(title, str) and title else "互动"


def shown_body(body: str, titles: dict[str, str],
               asset: Callable[[str], str] | None = None) -> str:
    """`body` with every interactive (placeholder, embedded-html block, evoui fence) as one
    〔互动：title〕 line and every image (placeholder, evowiki-image fence) as one 〔图片：title〕
    line; `titles`: interactive / block / image id -> title; `asset`: a stored block's title by
    asset id (`Names.asset`)."""
    def embed(m: re.Match[str]) -> str:
        f = _embed_fields(m)
        return interactive_line(f.get("title") or titles.get(f.get("id", ""), "互动"))

    body = _EMBED.sub(embed, body)
    body = _EVOUI.sub(lambda m: interactive_line(_evoui_title(m, asset)), body)
    body = _IMAGE.sub(lambda m: image_line(_image_title(m)), body)
    body = _IMAGE_PLACED.sub(lambda m: image_line(titles.get(m.group(1), "图片")), body)
    return PLACED.sub(lambda m: interactive_line(titles.get(m.group(1), m.group(1))), body)


@dataclass(frozen=True)
class SectionChange:
    """One section a revision changes. `old_heading`: the heading before (None for an added
    section); bodies as shown (`shown_body`), "" on the side that has none."""

    section_id: str
    heading: str
    status: SectionStatus
    old_heading: str | None
    old_body: str
    new_body: str


@dataclass(frozen=True)
class RevisionView:
    """A ready `page_revise` row against the page it replaces: the sections that change (the
    draft's order, then the dropped ones) and how many stay as they are. Check sections are kept
    by the swap and never counted."""

    draft: RevisionDraft
    sections: tuple[SectionChange, ...]
    unchanged: int


def revision_view(row: ActionRow, names: Names) -> RevisionView | None:
    """None while the draft is being written, or when it failed. The old page is the one the swap
    replaced (`undo.prev_record`) once executed, else the current page. ValueError when the stored
    draft does not validate."""
    if row.content.get("state") != "ready" or row.content.get("draft") is None:
        return None
    rd = RevisionDraft.model_validate(row.content["draft"])
    new = rd.draft
    prev = (row.undo or {}).get("prev_record") if row.status in ("executed", "undone") else None
    rec = PageRecord.model_validate(prev) if prev is not None else names.record(rd.page_id)
    old: dict[str, tuple[str, str]] = {}
    titles = {**{i.interactive_id: i.title for i in new.interactives},
              **{b.block_id: b.block.title for b in new.blocks},
              **{m.image_id: m.title for m in new.images}}
    if rec is not None and rec.page is not None:
        for s in rec.page.outline.sections:
            if s.role is not SectionRole.CHECK:
                t = rec.page.texts.get(s.id)
                old[s.id] = (s.heading, "" if t is None else t.body)
        for _, body in old.values():          # an interactive the draft keeps by its id
            for m in _EMBED.finditer(body):
                f = _embed_fields(m)
                if f.get("id") and f.get("title"):
                    titles.setdefault(f["id"], f["title"])
    out: list[SectionChange] = []
    same = 0
    for s in new.sections:
        before = old.pop(s.section_id, None)
        body = shown_body(s.body, titles, names.asset)
        if before is None:
            out.append(SectionChange(s.section_id, s.heading, "added", None, "", body))
            continue
        was = shown_body(before[1], titles, names.asset)
        if before[0] == s.heading and same_text(was, body):
            same += 1
            continue
        out.append(SectionChange(s.section_id, s.heading, "changed", before[0], was, body))
    for sid, (heading, body) in old.items():          # sections the draft leaves out
        if rd.scope:
            same += 1
        else:
            out.append(SectionChange(sid, heading, "removed", heading, shown_body(body, titles, names.asset), ""))
    return RevisionView(draft=rd, sections=tuple(out), unchanged=same)


def page_revise(row: ActionRow, names: Names) -> list[Line]:
    """`revision_view` as lines: per changed section its heading as context, the old text as del
    lines, the new text as add lines (a heading change adds 标题 del/add); a new section has only
    add lines, a dropped one only del lines. No diff while writing, or when it failed."""
    view = revision_view(row, names)
    if view is None:
        return []
    out: list[Line] = []
    for c in view.sections:
        out.append(ctx(c.heading))
        if c.status == "added":
            out += [add(ln) for ln in _lines(c.new_body)]
        elif c.status == "removed":
            out += [rem(ln) for ln in _lines(c.old_body)]
        else:
            if c.old_heading != c.heading:
                out += [rem(f"标题：{c.old_heading}"), add(f"标题：{c.heading}")]
            if not same_text(c.old_body, c.new_body):
                out += [rem(ln) for ln in _lines(c.old_body)] + [add(ln) for ln in _lines(c.new_body)]
    return out


def _material_names(titles: list[str], *, short: bool) -> str:
    """《A》; several: 《A》《B》, or 《A》等 2 份资料 (`short`, the 最近更新 row); none named: 资料. A file
    extension is not part of the name (the plugin's revise/rows.ts `sourceNames`)."""
    t = [n for n in (re.sub(r"\.[A-Za-z0-9]{1,5}$", "", x).strip() for x in titles) if n]
    if not t:
        return tr("Materials", lang=current_language()) if current_language() == "en" else "资料"
    if short and len(t) > 1:
        return f"《{t[0]}》等 {len(t)} 份资料"
    return "".join(f"《{x}》" for x in t)


REBUILD_WHY = "这一页按资料改过好几次了，这次按资料从头写，避免越改越走样"


def revise_label(row: ActionRow, names: Names) -> tuple[str, str] | None:
    """A ready `page_revise` row as the 参数 change list says it (full-demo app.js `reviseRow`):
    「「X」按《A》更新了 N 节」 and the reason for its circled ? (the same words as the plugin's
    revise/rows.ts `reviseTip`: 资料重写, the row's own why, else who asked). N counts the sections
    `revision_view` lists, against the page the swap replaced once executed. None when the row has
    no draft (still writing, failed) or the draft does not validate: the stored summary stays."""
    if row.kind != PAGE_REVISE:
        return None
    try:
        view = revision_view(row, names)
    except ValueError:
        return None
    if view is None:
        return None
    rd = view.draft
    prev = (row.undo or {}).get("prev_record") if row.status in ("executed", "undone") else None
    title = (str(prev.get("title") or "") if isinstance(prev, dict) else "") or names.page(rd.page_id)
    titles = [s.title for s in rd.sources]
    if current_language() == "en":
        summary = tr("Updated {count} sections of {title} from {materials}", lang="en",
                     count=len(view.sections), title=title, materials=", ".join(titles) or "materials")
        why = row.why.strip() or tr("Revised using the cited materials.", lang="en")
        if rd.rebuild:
            why = tr("Rebuilt from sources after multiple revisions to keep the page coherent.", lang="en")
        return summary, why
    summary = f"「{title}」按{_material_names(titles, short=True)}更新了 {len(view.sections)} 节"
    who = _material_names(titles, short=False)
    why = (REBUILD_WHY if rd.rebuild else row.why.strip() or (
        f"你让我按{who}改这一页" if row.source == "assistant"
        else f"{who}讲到了这一页已有的内容" if row.source == "bot" else f"按{who}改这一页"))
    return summary, why


def needs_state(row: ActionRow) -> bool:
    """Whether `diff_for` (or the in-page draft of a waiting `page_practice` row,
    `revise_schemas.practice_out`) reads the wiki state for this row."""
    return (row.kind in ("assistant_edit", PAGE_REVISE)
            or (row.kind == "page_practice" and row.status == "proposed"))


def rsi_change(row: ActionRow) -> list[Line]:
    """An RSI change proposed to the user (`teach.approval`) carries its 改动对比 in
    `content["diff"]` (written by the RSI cycle: the old and new values / rule); a mirror of an
    applied change has none."""
    lines = row.content.get("diff")
    if not isinstance(lines, list):
        return []
    return [{"kind": ln["kind"], "text": str(ln["text"])} for ln in lines
            if isinstance(ln, dict) and ln.get("kind") in ("ctx", "add", "del") and "text" in ln]


def diff_for(row: ActionRow, names: Names) -> list[Line]:
    """preference rows and RSI mirrors have no diff."""
    if row.kind == "assistant_edit":
        try:
            return assistant_edit(row, names)
        except ValueError:                                # a draft that no longer validates
            return [ctx(row.summary)]
    if row.kind == PAGE_REVISE:
        try:
            return page_revise(row, names)
        except ValueError:                                # a draft that no longer validates
            return [ctx(row.summary)]
    if row.kind in ("rsi_patch", "rsi_method"):
        return rsi_change(row)
    if row.kind == "path_edit" and row.content.get("op") in ("move", "add"):
        return path_edit(row)                    # the Bot's (B5b); the assistant's has none
    if row.kind == "bot_write" and isinstance(row.content.get("picks"), list):
        return [add(str(p.get("title", ""))) for p in row.content["picks"]]    # 要写 N 页
    return []


def path_edit(row: ActionRow) -> list[Line]:
    """The Bot's path edit: the shown order before and after (titles, numbered)."""
    def order(titles: Any) -> str:
        return "　".join(f"{n}. {t}" for n, t in enumerate(titles or [], 1))
    before, after = row.content.get("order_before"), row.content.get("order_after")
    if not before or not after:
        return [ctx(row.summary)]
    return ([rem(f"Before: {order(before)}"), add(f"After: {order(after)}")]
            if current_language() == "en" else
            [rem(f"之前：{order(before)}"), add(f"之后：{order(after)}")])
