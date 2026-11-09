"""From wiki state to the agent's input, and from a finished draft back to the page (plan rev 3.3
§10.2). Pure: no IO, no LLM.

`refine_input` writes the plan's `known` from K: the page concepts' and their prerequisites' 确定会 /
可能会 cells — the model never writes it.
A page on a learning path also gets its place on the path (`path_context`: the previous and next page's
objective), so it neither re-teaches the previous page nor takes over the next one.

`apply_draft` is the minimal patch: a draft section whose id is on the page writes that section
(its id, `check`, notes and fold state are kept; cells too when the draft leaves them empty); a new id is
inserted after `after` (None = page top), in draft order. Nothing is deleted. Each written section gets
its recall cue and change mark (rewritten / inserted); `written_record` puts the result on the record
with a `WriteMark`. Component placeholders become `embedded-html` blocks pointing at their stored asset;
EvoUI block placeholders become ```evoui fences holding the block JSON (the block is also stored as an
`evoui` asset). `stream_body` is what a section shows while the page is still being written (blocks
inline, components only once committed).

Materials (Task 6): `with_sources` adds the material sections the page's concepts map to (the one read
here, through the caller's `SourceExcerpts`); `page_sources` is what the written record keeps of them.
A reading page (reading-mode spec §4, `PageRecord.kind`) reads its unit's own paragraphs instead
(`for_spans`), and its input carries the unit's 讲解重点 and the pages of the prerequisites it uses
(`reading_fields`)."""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

from evowiki.deep.agent import RefineInput, SnapshotSeqs
from evowiki.deep.contracts import (
    AssetMeta,
    ImageDraft,
    RefineDraft,
    RenderCheck,
    embed_block,
    image_fence,
    image_placeholder,
)
from evowiki.deep.evoui import app_images, asset_fence, block_fence, block_json
from evowiki.deep.evoui.contracts import EvoUIBlock, block_placeholder
from evowiki.deep.render import has_app
from evowiki.deep.store import StoredAsset, sha256
from evowiki.deep.tools import PLACED, Workspace, merge_sections, placeholder
from evowiki.goals.contracts import (
    Band,
    SectionRole,
    SectionText,
    cell_id,
)
from evowiki.goals.fold import Page, keep_locked
from evowiki.goals.guardrails import BAND_ZH, PageSpec, cell_label
from evowiki.goals.scope import ConceptScope
from evowiki.sources.cite import (
    DIFFERS_RE,
    CiteIssue,
    cited_pages,
    stored_marks,
    stored_titles,
    verify,
)
from evowiki.sources.excerpts import DocumentSource, SourceExcerpts, SpanExcerpts
from evowiki.sources.locate import locate_json
from evowiki.teach.contracts import TeachingSpec
from evowiki.wikiapp import path_order
from evowiki.wikiapp.events import (
    PageRecord,
    PageSource,
    PathRecord,
    SectionChange,
    WriteKind,
    WriteMark,
)
from evowiki.wikiapp.kwrite import concept_name, page_spec
from evowiki.wikiapp.page_reader import WikiPage
from evowiki.wikiapp.refine_spec import SpecSource, with_exemplar
from evowiki.wikiapp.repo import Snapshot
from evowiki.wikiapp.series import render
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.teachspec import applicable, page_kind

KNOWN_MAX = 1200
SIMPLIFIED = "这里简化了"


def asset_src(asset_id: str) -> str:
    """`src` of an interactive's embed block: the Obsidian note writer saves the asset at this path
    next to the note; the web reads the asset id from the file name (GET /wiki/assets/{id})."""
    return f"assets/{asset_id}.html"


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


_KEEP: Any = object()
KEEP_DOMAIN: Any = _KEEP      # refine_input's `domain` default: the page's stored domain


def path_of(state: WikiState, page_id: str) -> tuple[str, PathRecord] | None:
    """(path id, record) of the learning path of more than one page that holds `page_id`."""
    for pid, rec in state.paths.items():
        if page_id in rec.page_ids and len(rec.path.pages) >= 2:
            return pid, rec
    return None


def series_note(state: WikiState, page_id: str) -> str:
    """The rendered series note of the page's path without this page's own line and terms ("" when
    none yet or no path)."""
    found = path_of(state, page_id)
    note = state.series.get(found[0]) if found is not None else None
    return render(note, exclude_page=page_id) if note is not None else ""


def other_written_pages(state: WikiState, page_id: str) -> tuple[WikiPage, ...]:
    """Every other page with content, on any path: the writer's prompt lists them with their ids
    (`page_spec.other_pages`), so its `read_page` opens all of them."""
    return tuple(WikiPage(page_id=pid, title=r.title, page=r.page)
                 for pid in state.page_order if pid != page_id and pid not in state.trashed_pages
                 and (r := state.pages[pid]).page is not None)


def path_context(state: WikiState, page_id: str) -> str:
    """One line for a page on a learning path: its place, this page's abstract
    (path@v6, when it has one), the previous and the next page (their objectives, and a
    `[title](#p:id)` link when that page exists); "" for a page on no path."""
    rec = next((rec for rec in state.paths.values() if page_id in rec.page_ids), None)
    if rec is not None:
        # the path as the learner has it now (edits replayed), not as it was first planned
        rows = [(i.page, rec.page_ids[path_order.stored_index(rec, i.key)])
                for i in path_order.shown(rec) if i.page is not None]
        ids = [pid for _, pid in rows]
        if page_id not in ids:
            return ""
        pages = [p for p, _ in rows]
        i = ids.index(page_id)

        def link(j: int) -> str:
            # only a page that exists (not in the trash) gets a link (never an invented id)
            pid = ids[j] if j < len(ids) else None
            other = (state.pages.get(pid) if pid is not None and pid not in state.trashed_pages
                     else None)
            return f"（链接：[{other.title}](#p:{pid})）" if other is not None else ""

        neighbors = []
        boundaries = []
        if len(pages) == 1:
            neighbors.append("这是该路径唯一一页")
        else:
            neighbors.append(f"上一页《{pages[i - 1].objective}》{link(i - 1)}" if i > 0
                             else "这是第一页")
            neighbors.append(f"下一页《{pages[i + 1].objective}》{link(i + 1)}"
                             if i + 1 < len(pages) else "这是最后一页")
        if any("#p:" in neighbor for neighbor in neighbors):
            boundaries.append("需要提到相邻页面时用给出的链接")
        if i > 0:
            boundaries.append("上一页讲过的只提一句")
        if i + 1 < len(pages):
            boundaries.append("下一页的内容这里不展开")
        else:
            boundaries.append("没有后续页面，本页目标在本页讲完，不得把目标内容留到以后")
        this = f"本页：{pages[i].abstract}；" if pages[i].abstract else ""
        return (f"学习路径（第 {i + 1}/{len(pages)} 页）：{this}{'，'.join(neighbors)}"
                f"（{'；'.join(boundaries)}）")
    return ""


def refine_input(snap: Snapshot, page_id: str, now: datetime, specs: SpecSource, *,
                 scope: tuple[str, ...] = (), spec_version: int | None = None,
                 page: Page | None = None, domain: str | None = _KEEP,
                 mode: Literal["page", "rewrite", "feedback", "practice"] = "rewrite",
                 instruction: str = "", outline_spec: PageSpec | None = None) -> RefineInput:
    """LookupError: unknown page / spec version; ValueError: the page is not organized yet. The
    teaching spec carries only the items in force for this page (kind + domain, R1) and at least
    one exemplar (`wikiapp.refine_spec.with_exemplar`). `page`: write this page instead of the stored one (a
    page written from its fresh outline has empty texts); `domain`: the page's teaching domain when
    it was just picked (default: the stored one). `mode="page"` requires every non-check section.
    `outline_spec`: the writer plans the outline (a first write, a reorganize: `RefineInput.spec`);
    the input then has no page and no required sections yet (plan_page sets both)."""
    state = snap.state
    rec = state.pages.get(page_id)
    if rec is None:
        raise LookupError(f"没有这个页面：{page_id}")
    if outline_spec is not None:
        page = given = None
    else:
        given = page if page is not None and page != rec.page else None     # traced for replay
        page = page or rec.page
        if page is None:
            raise ValueError("页面还没生成，不能改写")
    dom = rec.domain if domain is _KEEP else domain
    spec = page_spec(state, snap.k, page_id, user_id=snap.user_id)
    kv = snap.kview(now)
    page_cells = tuple(cell_id(c.id, a) for c in spec.concepts for a in kv.aspects(c.id))
    names = {c.id: c.name for c in spec.concepts}
    prereqs = [p for c in spec.concepts for p in c.prereqs if p not in names]
    for p in dict.fromkeys(prereqs):
        names[p] = concept_name(state, p, user_id=snap.user_id)

    def label(cell: str) -> str:
        cid = cell.split(".", 1)[0]
        return cell_label(spec, cell) if cid in {c.id for c in spec.concepts} else \
            f"{names.get(cid, cid)}·{cell.split('.', 1)[1]}"

    known: dict[Band, list[str]] = {Band.KNOWN: [], Band.LIKELY: []}
    gaps: list[str] = []
    for cid in [*(c.id for c in spec.concepts), *dict.fromkeys(prereqs)]:
        for a in kv.aspects(cid):
            x = cell_id(cid, a)
            band = kv.cell(x).band
            if band in known:
                known[band].append(label(x))
            elif cid in {c.id for c in spec.concepts}:
                gaps.append(f"{label(x)}：{BAND_ZH[band]}")
    lines = [f"{BAND_ZH[b]}：{'、'.join(xs)}" for b, xs in known.items() if xs]
    if not lines:
        lines.append("K 里没有「确定会」或「可能会」的格子：不要假设他懂任何相关的专业内容。")
    scopes = tuple(ConceptScope(c.name, c.definition, kv.aspects(c.id)) for c in spec.concepts)
    required = tuple(s.id for s in page.outline.sections
                     if s.role is not SectionRole.CHECK) if mode == "page" and page else ()
    reading = reading_fields(state, rec)
    return RefineInput(user_id=snap.user_id, page_id=page_id, title=rec.title, goal=rec.goal,
                       page=page, kview=kv, names=names, known=_clip("\n".join(lines), KNOWN_MAX),
                       gaps="\n".join(gaps), page_cells=page_cells,
                       teaching=_page_spec(snap, page_id, specs, spec_version, dom), scope=scope,
                       language=state.language, mode=mode, required=required,
                       instruction=instruction, path=path_context(state, page_id),
                       series=series_note(state, page_id),
                       other_pages=other_written_pages(state, page_id), concept_scope=scopes,
                       snapshot_seqs=SnapshotSeqs(snap.wiki_seq, snap.aspect_seq, snap.infer_version),
                       given_page=given, domain=dom, spec=outline_spec, **reading)


def reading_fields(state: WikiState, rec: PageRecord) -> dict[str, Any]:
    """A reading page's part of its input (reading-mode spec §4): `page_kind`, its unit's
    `focus`, the pages of the unit's uses that have one (id, title) and the unit's material and
    spans; {} for a learn page.
    A reading page whose reading or unit is gone is a LookupError (fail closed: never written
    from the whole sections its points link to instead of its own paragraphs)."""
    if rec.kind != "reading":
        return {}
    reading = state.readings.get(rec.reading_id or "")
    unit = (next((u for u in reading.units if u.unit_id == rec.unit_id), None)
            if reading is not None else None)
    if reading is None or unit is None:
        raise LookupError(f"reading page {rec.page_id}: its reading {rec.reading_id} / unit "
                          f"{rec.unit_id} is gone")
    pages = []
    for cid in unit.uses:
        pid = next((p for p in state.page_order
                    if p != rec.page_id and p not in state.trashed_pages
                    and (r := state.pages.get(p)) is not None and cid in r.targets), None)
        if pid is not None and (pid, state.pages[pid].title) not in pages:
            pages.append((pid, state.pages[pid].title))
    return {"page_kind": "reading", "focus": unit.focus, "prereq_pages": tuple(pages),
            "unit_source": reading.source_id, "unit_spans": unit.spans}


def concept_ancestors(state: WikiState, concepts: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Each concept's part_of ancestors in the graph, nearest first (none when the graph lacks
    it): where a point with no material links of its own takes them from (stage 3)."""
    g = state.graph
    return {c: g.ancestors(c) if g.node(c) is not None else () for c in concepts}


def with_sources(inp: RefineInput, concepts: Sequence[str],
                 excerpts: SourceExcerpts | None, *, readable: bool = False,
                 parents: Mapping[str, Sequence[str]] | None = None) -> RefineInput:
    """`inp` with the material sections the page's concepts (`PageRecord.concepts`, the ones
    `page_spec` lists) map to — a concept with no links of its own uses its nearest ancestor's
    (`parents`, `concept_ancestors`; spec 2026-10-28 §2.5); unchanged when no material reader is
    configured. A reading page
    gets its unit's own paragraphs (`for_spans`: a split section only its part, reading-mode spec
    §4), never the whole sections its points link to. A failing read raises (fail closed: the
    page is never silently written without its materials).
    `readable`: the writer gets the material tools (a reader is configured)."""
    if excerpts is None:
        return inp
    if inp.page_kind == "reading" and inp.unit_spans:
        if not isinstance(excerpts, SpanExcerpts):
            raise RuntimeError("这个资料读取器不能按段落取原文，带读页不能写")
        found = excerpts.for_spans(inp.user_id, inp.unit_source, list(inp.unit_spans))
    else:
        found = excerpts.for_nodes(inp.user_id, list(concepts), parents=parents or {})
    return replace(inp, sources=tuple(found), sources_readable=readable)


def page_sources(inp: RefineInput) -> tuple[PageSource, ...]:
    """The materials a write used, once each in first-use order, with their titles (and printed
    page offsets) then."""
    return tuple(dict.fromkeys(PageSource(source_id=e.source_id, title=e.source_title,
                                          page_offset=e.page_offset)
                               for e in inp.sources))


def checked_citations(draft: RefineDraft, inp: RefineInput,
                      excerpts: SourceExcerpts | None) -> tuple[RefineDraft, tuple[CiteIssue, ...]]:
    """The draft with every section body's citations and disagreement marks checked against the
    materials' own text (`sources.cite.verify`, Task 6b), and the issues (each with its section).
    The pages read come through `excerpts`: N and N+1 of every quoted citation, plus the mapped
    sections' pages for a disagreement's quote (a section given in full uses that text). A read
    that fails raises (fail closed); a citation that needs a read without `excerpts` raises too."""
    srcs = page_sources(inp)
    wanted: dict[str, set[int]] = {}
    for s in draft.sections:
        for sid, pages in cited_pages(s.body, srcs).items():
            wanted.setdefault(sid, set()).update(pages)
    has_differs = any(DIFFERS_RE.search(s.body) for s in draft.sections)
    if has_differs:
        for e in inp.sources:
            if e.text is None and e.page_from is not None:
                wanted.setdefault(e.source_id, set()).update(
                    range(e.page_from, (e.page_to or e.page_from) + 1))
    texts: dict[str, dict[int, str]] = {}
    for sid, pages in wanted.items():
        if excerpts is None:
            raise RuntimeError("没有资料读取器，不能核对出处")
        texts[sid] = excerpts.page_texts(inp.user_id, sid, sorted(pages))
    mapped = ""
    if has_differs:
        mapped = "\n".join(
            e.text if e.text is not None else "\n".join(
                texts.get(e.source_id, {}).get(p, "")
                for p in range(e.page_from or 0, (e.page_to or e.page_from or 0) + 1))
            for e in inp.sources)

    def page_text(source_id: str, page: int) -> str:
        return texts.get(source_id, {}).get(page, "")

    sections, issues = [], []
    for s in draft.sections:
        # the stored text the write started from (none on a page the writer outlined)
        before = inp.page.texts.get(s.section_id) if inp.page is not None else None
        body, found = verify(s.body, srcs, page_text, mapped,
                             kept=stored_marks(before.body) if before is not None else ())
        sections.append(s.model_copy(update={"body": body}) if body != s.body else s)
        issues += [i.model_copy(update={"section_id": s.section_id}) for i in found]
    located = _located(issues, inp.user_id, excerpts)
    return draft.model_copy(update={"sections": tuple(sections)}), located


def _located(issues: list[CiteIssue], user_id: str,
             excerpts: SourceExcerpts | None) -> tuple[CiteIssue, ...]:
    """The verified citations with their place in the stored document, when the reader gives it."""
    if not isinstance(excerpts, DocumentSource):
        return tuple(issues)
    docs: dict[str, Any] = {}

    def located(i: CiteIssue) -> CiteIssue:
        if i.kind != "verified" or i.source_id is None:
            return i
        if i.source_id not in docs:
            docs[i.source_id] = excerpts.document(user_id, i.source_id)
        loc = locate_json(docs[i.source_id], i.source_id, i.page, i.quote)
        return i.model_copy(update={"locator": loc})

    return tuple(located(i) for i in issues)


def _page_spec(snap: Snapshot, page_id: str, specs: SpecSource,
               version: int | None, domain: str | None) -> TeachingSpec:
    full = specs.spec(snap, version)
    items = applicable(full, page_kind(snap.state, page_id), domain)
    return with_exemplar(full.model_copy(update={"items": items}))


def content(rec: PageRecord) -> tuple | None:
    """What a refine was written against: the outline and the texts. Display state (folds, pins),
    check answers and open marks may move underneath without making the draft stale."""
    if rec.page is None:
        return None
    return rec.page.outline, rec.page.texts


def make_assets(draft: RefineDraft, checks: Mapping[str, RenderCheck], *, user_id: str,
                job_id: str, now: datetime, new_id: Callable[[str], str]) -> list[StoredAsset]:
    out = []
    for d in draft.interactives:
        meta = AssetMeta(asset_id=new_id("ia"), page_id=draft.page_id, section_id=d.section_id,
                         interactive_id=d.interactive_id, title=d.title, height=d.height,
                         sha256=sha256(d.html), check=checks[d.interactive_id])
        out.append(StoredAsset(user_id=user_id, meta=meta, html=d.html, job_id=job_id,
                               created_at=now))
    image_assets = [StoredAsset(user_id=user_id, job_id=job_id, created_at=now, html=m.data_b64, meta=AssetMeta(
        asset_id=new_id("ia"), page_id=draft.page_id, section_id=m.section_id, interactive_id=m.image_id,
        title=m.title[:200], height=400, sha256=sha256(m.data_b64), check=image_check(m), kind="image"))
        for m in draft.images]                # images: the asset holds the WebP as base64 text
    stored = {a.meta.interactive_id: a.meta.asset_id for a in image_assets}
    for b in draft.blocks:                    # EvoUI blocks: the asset holds the block JSON
        content = block_json(stored_images(b.block, stored))
        meta = AssetMeta(asset_id=new_id("ia"), page_id=draft.page_id, section_id=b.section_id,
                         interactive_id=b.block_id, title=b.block.title, height=400,
                         sha256=sha256(content), check=checks[b.block_id], kind="evoui")
        out.append(StoredAsset(user_id=user_id, meta=meta, html=content, job_id=job_id,
                               created_at=now))
    return out + image_assets


def stored_images(block: EvoUIBlock, assets: Mapping[str, str]) -> dict[str, Any]:
    """The block as stored: each of its app's images points at its asset (a draft's points at its ImageDraft)."""
    d = block.model_dump(mode="json")
    for im in app_images(d):
        im["asset"] = assets[im["asset"]]
    return d


def image_check(m: ImageDraft) -> RenderCheck:
    """An image has no render check: it was decoded and re-encoded when it was placed
    (`deep.images.prepare`), which is the check."""
    return RenderCheck(interactive_id=m.image_id, ok=True,
                       detail=f"image {m.width}x{m.height} re-encoded server side")


def _simplified(body: str, simplifications: tuple[str, ...]) -> str:
    if simplifications:
        body = body.rstrip() + "\n\n" + "\n".join(f"> {SIMPLIFIED}：{s}" for s in simplifications)
    return body


def stream_body(ws: Workspace, sid: str, *, blocks: bool = True) -> str:
    """A written section as it shows while the page is still being written: EvoUI blocks the writer
    has already composed are inline (an evoui fence); a component (stored only at the commit) or a
    placeholder whose interactive is not there yet is left out — a line holding only placeholders
    disappears. `blocks=False`: the text alone (every placeholder left out), what a page being
    written commits for the section (its interactives are stored with the final commit)."""
    s = ws.sections[sid]

    def sub(m: re.Match[str]) -> str:
        b = ws.blocks.get(m.group(1))
        return (block_fence(json.loads(block_json(b.block)))
                # an app appears with the commit: only then does the server vouch for it, and its images exist
                if blocks and b is not None and not has_app(b.block) and m.group(0) == block_placeholder(b.block_id)
                else "")

    lines = []
    for ln in s.body.splitlines():
        out = PLACED.sub(sub, ln)
        if PLACED.search(ln) and not out.strip():
            continue
        lines.append(out)
    return _simplified("\n".join(lines), s.simplifications)


def _body(body: str, simplifications: tuple[str, ...], assets: Mapping[str, StoredAsset],
          images: Mapping[str, ImageDraft]) -> str:
    """Placeholders become what a note carries: a component → its `embedded-html` block pointing at
    the asset; an EvoUI block → a ```evoui `asset: <id>` fence (results and progress report to it);
    an image → a ```evowiki-image fence (the asset id and the attribution)."""
    for iid, a in assets.items():
        if a.meta.kind == "image":
            if (img := images.get(iid)) is not None:
                body = body.replace(image_placeholder(iid), image_fence(a.meta.asset_id, img))
        elif a.meta.kind == "evoui":
            body = body.replace(block_placeholder(iid), asset_fence(a.meta.asset_id))
        else:
            body = body.replace(placeholder(iid), embed_block(a.meta, asset_src(a.meta.asset_id)))
    return _simplified(body, simplifications)


def apply_draft(page: Page, draft: RefineDraft, assets: list[StoredAsset], *,
                job_id: str) -> tuple[Page, dict[str, SectionChange]]:
    """The page with the draft applied, and the change mark of each written section (page order).
    ValueError when the draft does not fit this page (e.g. an `after` that is gone)."""
    by_iid = {a.meta.interactive_id: a for a in assets}
    images = {m.image_id: m for m in draft.images}
    old_by = {o.id: o for o in page.outline.sections}
    drafted = {s.section_id: s for s in draft.sections}
    if bad := [s.section_id for s in draft.sections
               if s.section_id not in old_by and s.after is not None
               and s.after not in old_by and s.after not in drafted]:
        raise ValueError(f"sections {bad}: their `after` is not on the page")
    page, changed = merge_sections(page, drafted, lambda s: SectionText(
        section_id=s.section_id, cue=s.cue, answer=s.answer,
        cache_key=f"deep:{job_id}:{s.section_id}",
        body=_body(s.body, s.simplifications, by_iid, images)))
    return page, dict(changed)


def written_record(rec: PageRecord, draft: RefineDraft, assets: list[StoredAsset], *, job_id: str,
                   now: datetime, model: str = "", spec_version: int | None = None,
                   page: Page | None = None, cost_usd: float = 0.0,
                   fallback_reason: str | None = None,
                   kind: WriteKind = "rewrite",
                   sources: tuple[PageSource, ...] = (),
                   cite_issues: tuple[CiteIssue, ...] = ()) -> PageRecord:
    """`rec` with the draft applied to `page` (default: the stored page) and a `WriteMark`.
    `sources`: the materials this write used (`page_sources`); `cite_issues`: this write's
    citation check (`checked_citations`). The record's sources still cited anywhere on the new page
    are kept (matched on (source_id, title)), and untouched sections keep their earlier issues
    (`kept_marks`). ValueError when the page is not organized or the
    draft does not fit it."""
    base = page or rec.page
    if base is None:
        raise ValueError("page is not organized")
    new_page, changed = apply_draft(base, draft, assets, job_id=job_id)
    new_page = keep_locked(rec.page, new_page)
    if rec.page is not None:      # a locked section keeps its text whatever the draft said
        changed = {sid: c for sid, c in changed.items() if not rec.page.state(sid).pinned}
    mark = WriteMark(job_id=job_id, at=now, sections=tuple(changed), model=model,
                     assets=tuple(a.meta.asset_id for a in assets), spec_version=spec_version,
                     cost_usd=round(cost_usd, 6), fallback_reason=fallback_reason or None,
                     kind=kind)
    old = rec.page.texts if rec.page is not None else {}
    untouched = {sid for sid, t in new_page.texts.items() if sid not in changed and old.get(sid) == t}
    kept_sources, kept_issues = kept_marks(rec, untouched, new_page)
    mine = {(x.source_id, x.title) for x in sources}
    merged = (*sources, *(s for s in kept_sources if (s.source_id, s.title) not in mine))
    return rec.model_copy(update={"page": new_page, "changed": changed, "written": mark,
                                  "sources": merged,
                                  "cite_issues": (*kept_issues, *cite_issues)})


def kept_marks(rec: PageRecord, untouched: set[str],
               page: Page) -> tuple[list[PageSource], list[CiteIssue]]:
    """The record's sources still cited (stored marks, by the title written then) anywhere on the
    new page — untouched sections and marks a rewritten section carried over — once per
    (source_id, title), so a material renamed or deleted since still resolves (final review #4);
    and the untouched sections' earlier citation issues, plus the verified records of marks a
    rewritten section carried over (their quote is only there)."""
    titles = set().union(*(stored_titles(t.body) for t in page.texts.values()))
    kept = dict.fromkeys(s for s in rec.sources if s.title in titles)

    def carried(i: CiteIssue) -> bool:
        text = page.texts.get(i.section_id)
        return i.kind == "verified" and text is not None and i.mark in text.body

    return (list(kept), [i for i in rec.cite_issues if i.section_id in untouched or carried(i)])
