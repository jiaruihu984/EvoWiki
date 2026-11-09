"""Which artifacts a wiki commit saves, and what each was made from (RSI observability plan
2026-10-29 §5 1b; ruling 3: only at save points). `commit_lineage` reads the events of one commit
against the state before it; the repo writes the result in the commit's own transaction
(`Repo.commit`), so every writer that saves a page, the graph, a path, the teaching spec or a
follow-up registers what it saved — no writer can skip it.

What is an artifact here (kind → ref, version_key):
  section   → "{page}/{section}", `section_key` (heading, cue, body) — one per written section whose
              text changed (new or rewritten); parents: the outline, the section's earlier text
              (`previous`), its nodes, the rule revs and K cells of its SectionLog in this commit,
              the materials the page was written from
  check     → "{page}/{section}", `check_key` — an embedded check item; parents: outline, its node
  card      → the review card id (`review.card_id`), `card_key` — a cue / verified-check card;
              parent: its section / check
  outline   → page id, the outline's hash; parents: the page's nodes
  asset     → asset id (content-addressed: the id is the version); parent: the section citing it
  node      → node id, (name, definition); a split's new nodes have the removed one as `split_from`
              parent
  path      → path id, the record's hash; parents: its nodes
  rule      → spec item id, its rev; parent: the previous rev
  followup  → follow-up id, its confirmation question's hash; parent: the check answered wrong
Parents named only by reference (a K cell's band, a material, a rule rev saved before 0091) are
registered with no producer. Unsaved or refused candidates never reach a commit, so never here.

A view computes the same ids from what it shows (`section_artifact`, `check_artifact`,
`card_artifact`); the client sends them back with its feedback."""

import json
from collections.abc import Sequence
from datetime import datetime

from evowiki.goals.contracts import CheckItem, OutlineSection, PageOutline, SectionText, split_cell
from evowiki.goals.fold import Page
from evowiki.goals.review import sources_from_page
from evowiki.nodes.model import NodeGraph, NodeRec
from evowiki.store.artifacts import Artifact, Dependency, Lineage, artifact_id, text_key
from evowiki.teach.contracts import SectionLog, TeachingSpec
from evowiki.wikiapp.events import (
    FollowupScheduled,
    GraphSaved,
    PageRecord,
    PageSaved,
    PathRecord,
    PathSaved,
    SectionLogsSaved,
    TeachingSpecSaved,
    WikiEvent,
)
from evowiki.wikiapp.state import WikiState, apply_event


def _dump(model) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def section_key(heading: str, text: SectionText) -> str:
    return text_key("section", heading, text.cue, text.body)


def check_key(check: CheckItem) -> str:
    return text_key("check", _dump(check))


def card_key(heading: str, mode: str, cue: str = "", check: CheckItem | None = None) -> str:
    return (text_key("card", "cue", heading, cue) if mode == "cue"
            else text_key("card", "check", _dump(check)))


def outline_key(outline: PageOutline) -> str:
    return text_key("outline", _dump(outline))


def node_key(node: NodeRec) -> str:
    return text_key("node", node.name, node.definition)


def path_key(rec: PathRecord) -> str:
    return text_key("path", _dump(rec))


def _ref(page_id: str, section_id: str) -> str:
    return f"{page_id}/{section_id}"


def card_id(page_id: str, section_id: str, mode: str) -> str:
    from evowiki.wikiapp.review import card_id as rc  # review imports the repo, which imports us
    return rc(page_id, section_id, mode)


# ---- what a view names (the version it shows) ----

def section_artifact(user_id: str, page: Page, section_id: str) -> str | None:
    """The artifact id of the section's text as `page` holds it; None while it has no text."""
    sec = next((s for s in page.outline.sections if s.id == section_id), None)
    text = page.texts.get(section_id)
    if sec is None or text is None:
        return None
    return artifact_id(user_id, "section", _ref(page.outline.page_id, section_id),
                       section_key(sec.heading, text))


def check_artifact(user_id: str, page_id: str, section_id: str, check: CheckItem) -> str:
    return artifact_id(user_id, "check", _ref(page_id, section_id), check_key(check))


def card_artifact(user_id: str, page_id: str, section_id: str, heading: str, mode: str, *,
                  cue: str = "", check: CheckItem | None = None) -> str:
    return artifact_id(user_id, "card", card_id(page_id, section_id, mode),
                       card_key(heading, mode, cue, check))


def asset_artifact(user_id: str, asset_id: str) -> str:
    return artifact_id(user_id, "asset", asset_id, asset_id)


def followup_artifact(user_id: str, followup_id: str, item: CheckItem) -> str:
    return artifact_id(user_id, "followup", followup_id, check_key(item))


# ---- what a commit saves ----

class _Build:
    def __init__(self, user_id: str, run_id: str | None, at: datetime):
        self.user_id, self.run_id, self.at = user_id, run_id, at
        self.arts: list[Artifact] = []
        self.deps: list[Dependency] = []

    def made(self, kind: str, ref: str, version_key: str) -> str:
        """An artifact this commit saves (producer = the run)."""
        a = Artifact(self.user_id, kind, ref, version_key, self.run_id, self.at)
        self.arts.append(a)
        return a.artifact_id

    def known(self, kind: str, ref: str, version_key: str) -> str:
        """A parent named by reference (no producer if it is not registered yet)."""
        a = Artifact(self.user_id, kind, ref, version_key, None, self.at)
        self.arts.append(a)
        return a.artifact_id

    def edge(self, child: str, parent: str, role: str) -> None:
        if child != parent:
            self.deps.append(Dependency(child, parent, role))

    def lineage(self) -> Lineage:
        return Lineage(tuple(self.arts), tuple(self.deps))


def _node(b: _Build, graph: NodeGraph, concept_id: str) -> str | None:
    n = next((x for x in graph.nodes if x.id == concept_id), None)
    return b.known("node", n.id, node_key(n)) if n is not None else None


def _concepts(cells: Sequence[str]) -> list[str]:
    out: list[str] = []
    for c in cells:
        try:
            cid = split_cell(c)[0]
        except ValueError:
            continue
        if cid not in out:
            out.append(cid)
    return out


def _effective(state: WikiState) -> TeachingSpec:
    from evowiki.wikiapp.teachspec import effective_spec
    return effective_spec(state)


def _rule_revs(spec: TeachingSpec | None) -> dict[str, int]:
    return {} if spec is None else {i.item_id: i.rev for i in spec.items}


def _log_edges(b: _Build, sid: str, log: SectionLog, revs: dict[str, int]) -> None:
    """A section's SectionLog: the rule revs in force and the K cells (with their bands) used."""
    for item in log.spec_items:
        if item in revs:
            b.edge(sid, b.known("rule", item, str(revs[item])), "rule")
    for cell, band in log.bands.items():
        b.edge(sid, b.known("k_cell", cell, band), "k_cell")


def _page(b: _Build, state: WikiState, old: PageRecord | None, new: PageRecord,
          logs: dict[tuple[str, str], SectionLog]) -> None:
    page = new.page
    if page is None:
        return
    pid = new.page_id
    before = old.page if old is not None else None
    old_secs: dict[str, OutlineSection] = (
        {s.id: s for s in before.outline.sections} if before is not None else {})
    old_texts = before.texts if before is not None else {}
    graph, revs = state.graph, _rule_revs(_effective(state))
    outline = page.outline
    okey = outline_key(outline)
    if before is None or outline_key(before.outline) != okey:
        out_id = b.made("outline", pid, okey)
        for cid in new.concepts:
            if (nid := _node(b, graph, cid)) is not None:
                b.edge(out_id, nid, "node")
    else:
        out_id = b.known("outline", pid, okey)
    sources = [b.known("source", s.source_id, s.title) for s in new.sources]
    section_ids: dict[str, str] = {}
    for s in outline.sections:
        text = page.texts.get(s.id)
        if text is None:
            continue
        key = section_key(s.heading, text)
        prev = old_secs.get(s.id)
        prev_text = old_texts.get(s.id)
        prev_key = (section_key(prev.heading, prev_text)
                    if prev is not None and prev_text is not None else None)
        log = logs.get((pid, s.id))
        if prev_key == key:
            # a run that commits as it goes logs its sections in its final commit: the log still
            # says what that text was written under
            sid = section_ids[s.id] = b.known("section", _ref(pid, s.id), key)
            if log is not None:
                _log_edges(b, sid, log, revs)
            continue
        sid = section_ids[s.id] = b.made("section", _ref(pid, s.id), key)
        b.edge(sid, out_id, "outline")
        if prev_key is not None:
            b.edge(sid, b.known("section", _ref(pid, s.id), prev_key), "previous")
        for cid in _concepts(s.cells):
            if (nid := _node(b, graph, cid)) is not None:
                b.edge(sid, nid, "node")
        for src in sources:
            b.edge(sid, src, "source")
        if log is not None:
            _log_edges(b, sid, log, revs)
    for s in outline.sections:
        if s.check is None:
            continue
        ck = check_key(s.check)
        p = old_secs.get(s.id)
        if p is not None and p.check is not None and check_key(p.check) == ck:
            continue
        cid_ = b.made("check", _ref(pid, s.id), ck)
        b.edge(cid_, out_id, "outline")
        for concept in _concepts((s.check.cell,)):
            if (nid := _node(b, graph, concept)) is not None:
                b.edge(cid_, nid, "node")
    old_cards = _cards(before) if before is not None else {}
    for (sid_, mode), (heading, cue, check) in _cards(page).items():
        key = card_key(heading, mode, cue, check)
        if old_cards.get((sid_, mode)) == (heading, cue, check):
            continue
        card = b.made("card", card_id(pid, sid_, mode), key)
        if mode == "cue" and sid_ in section_ids:
            b.edge(card, section_ids[sid_], "section")
        elif check is not None:
            b.edge(card, b.known("check", _ref(pid, sid_), check_key(check)), "check")
    w = new.written
    if w is not None and (old is None or old.written != w):
        for asset in w.assets:
            aid = b.made("asset", asset, asset)
            for s in outline.sections:
                t = page.texts.get(s.id)
                if t is not None and asset in t.body and s.id in section_ids:
                    b.edge(aid, section_ids[s.id], "section")


def _cards(page: Page) -> dict[tuple[str, str], tuple[str, str, CheckItem | None]]:
    """(section, mode) → (heading, cue, check) of the page's review cards (`sources_from_page`)."""
    out: dict[tuple[str, str], tuple[str, str, CheckItem | None]] = {}
    for src in sources_from_page(page.outline, page.texts):
        if src.cue:
            out[(src.section_id, "cue")] = (src.heading, src.cue, None)
        if src.check is not None:
            out[(src.section_id, "check")] = (src.heading, "", src.check)
    return out


def _graph(b: _Build, old: NodeGraph, new: NodeGraph) -> None:
    before = {n.id: node_key(n) for n in old.nodes}
    for n in new.nodes:
        if before.get(n.id) != node_key(n):
            b.made("node", n.id, node_key(n))
    seen = {c.change_id for c in old.changes}
    for ch in new.changes:
        if ch.change_id in seen or ch.kind != "split":
            continue
        removed = [b.known("node", n.id, node_key(n)) for n in ch.removed_nodes]
        for n in ch.added_nodes:
            child = b.made("node", n.id, node_key(n))
            for r in removed:
                b.edge(child, r, "split_from")


def _path(b: _Build, state: WikiState, old: PathRecord | None, new: PathRecord) -> None:
    key = path_key(new)
    if old is not None and path_key(old) == key:
        return
    pid = b.made("path", new.path.path_id, key)
    if old is not None:
        b.edge(pid, b.known("path", old.path.path_id, path_key(old)), "previous")
    concepts = dict.fromkeys(c for page in new.path.pages for c in page.concepts)
    for cid in concepts:
        if (nid := _node(b, state.graph, cid)) is not None:
            b.edge(pid, nid, "node")
    for src in new.sources:
        b.edge(pid, b.known("source", src, ""), "source")


def _spec(b: _Build, old: TeachingSpec | None, new: TeachingSpec) -> None:
    revs = _rule_revs(old)
    for item in new.items:
        if revs.get(item.item_id) == item.rev:
            continue
        rid = b.made("rule", item.item_id, str(item.rev))
        if item.item_id in revs:
            b.edge(rid, b.known("rule", item.item_id, str(revs[item.item_id])), "previous")


def _followup(b: _Build, state: WikiState, body: FollowupScheduled) -> None:
    f = body.followup
    if f.item is None:
        return
    fid = b.made("followup", f.followup_id, check_key(f.item))
    rec = state.pages.get(f.page_id)
    sec = (next((s for s in rec.page.outline.sections if s.id == f.section_id), None)
           if rec is not None and rec.page is not None else None)
    if sec is not None and sec.check is not None:
        b.edge(fid, b.known("check", _ref(f.page_id, f.section_id), check_key(sec.check)),
               "check")


def commit_lineage(user_id: str, state: WikiState, events: Sequence[WikiEvent], *,
                   run_id: str | None) -> Lineage:
    """The artifacts the commit `events` saves on top of `state` (the state before it), with their
    parents. Events are walked in order, each against the state the earlier ones left."""
    logs = {(g.page_id, g.section_id): g for e in events
            if isinstance(e.body, SectionLogsSaved) for g in e.body.logs}
    builds: list[Lineage] = []
    for ev in events:
        body = ev.body
        b = _Build(user_id, run_id, ev.ts)
        match body:
            case PageSaved():
                _page(b, state, state.pages.get(body.record.page_id), body.record, logs)
            case GraphSaved():
                _graph(b, state.graph, body.graph)
            case PathSaved():
                _path(b, state, state.paths.get(body.record.path.path_id), body.record)
            case TeachingSpecSaved():
                _spec(b, state.spec, body.spec)
            case FollowupScheduled():
                _followup(b, state, body)
        if b.arts:
            builds.append(b.lineage())
        state = apply_event(state, ev)
    arts = tuple(a for x in builds for a in x.artifacts)
    deps = tuple(d for x in builds for d in x.dependencies)
    return Lineage(arts, deps)
