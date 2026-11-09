"""K-write helpers for the wiki flows: observations -> (Observe, AddNote) events, concept
definitions, the wiki's concepts as intake sees them, a page's `PageSpec` (with the roles worked
out from the pages, `concept_roles`), and the in-memory K a request reasons over before it commits.

Note text is raw content only (D21): the user's quote, the chosen option text, the question stem.
Its kind is derived from the evidence_ref prefix (goal:/quiz:/check:/feedback:/ask:/declare:).
Quiz/check notes also carry a display `label` 「题目：<题干>；选了「…」」 (old notes: None)."""

from collections.abc import Callable, Sequence
from datetime import datetime

from evowiki.belief.aspects import (
    AddNote,
    AspectEvent,
    AspectK,
    AspectKView,
    DefineConcept,
    Observe,
)
from evowiki.goals.contracts import Aspect, ConceptRole, Observation
from evowiki.goals.fold import is_stale
from evowiki.goals.guardrails import OtherPage, PageConcept, PageSpec
from evowiki.goals.intake import DEFAULT_ASPECTS, ConceptRef, IntakeConcept, Subitem
from evowiki.wiki.catalog import concept_label
from evowiki.wikiapp.events import PageRecord
from evowiki.wikiapp.repo import Snapshot
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.teachspec import PageTeach, page_teach

Ids = Callable[[str], str]
NOTE_MAX = 240
IDK_LABEL = "不知道"
LABEL_MAX = 600
LABEL_STEM_MAX = 500


def _cap(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def answer_label(stem: str, choice: str) -> str:
    """「题目：<题干>；选了「<choice>」」 for a quiz/check note: the stem capped at 500 chars (+…),
    the choice capped so the whole sentence stays ≤ 600."""
    head = f"题目：{_cap(stem, LABEL_STEM_MAX)}；选了「"
    room = LABEL_MAX - len(head) - 1 - 1          # closing 」 and a possible …
    return f"{head}{_cap(choice, room)}」"


def _default_note(o: Observation) -> str:
    if not o.quote:
        raise ValueError(f"observation on {o.cell} has no quote; pass note_text")
    return o.quote


def observation_events(obs: Sequence[Observation], *, ts: datetime, new_id: Ids,
                       note_text: Callable[[Observation], str] | None = None,
                       note_label: Callable[[Observation], str | None] | None = None
                       ) -> list[AspectEvent]:
    """Per observation: Observe, then an AddNote on the same cell (D13), text ≤ 240 chars.
    Default note text = the observation's quote (self-reports); `note_label` (if given) fills
    AddNote.label."""
    text_of = note_text or _default_note
    out: list[AspectEvent] = []
    for o in obs:
        out.append(Observe(event_id=new_id("aev"), ts=ts, observation=o))
        out.append(AddNote(event_id=new_id("aev"), ts=ts, cell=o.cell,
                           text=text_of(o)[:NOTE_MAX], evidence_ref=o.evidence_ref,
                           label=note_label(o) if note_label is not None else None))
    return out


def define_events(concepts: Sequence[IntakeConcept], *, ts: datetime,
                  new_id: Ids) -> list[AspectEvent]:
    """DefineConcept for every intake concept, new or reused (redefining unions aspects and
    prereqs)."""
    return [DefineConcept(event_id=new_id("aev"), ts=ts, concept_id=c.id,
                          aspects=c.aspects or DEFAULT_ASPECTS, prereqs=c.prereqs)
            for c in concepts]


def _subitems(state: WikiState, cid: str) -> tuple[Subitem, ...]:
    n = state.names.get(cid)
    return n.subitems if n is not None else ()


def existing_refs(state: WikiState, k: AspectK) -> list[ConceptRef]:
    """The user's named concepts in K definition order, as intake's `existing`."""
    return [ConceptRef(id=c, name=state.names[c].name, aspects=k.aspects(c),
                       prereqs=k.prereqs(c), subitems=_subitems(state, c),
                       definition=node.definition if (node := state.graph.node(c)) is not None else "")
            for c in k.concept_ids() if c in state.names]


def goal_definition(state: WikiState, page_id: str, concept: str) -> str:
    """What the concept is about in the goal this page belongs to: a point another goal also has
    keeps one node and one definition, written in the first goal's words; each goal's split says
    what it is about there (`MapNode.scope` of a part, never a block's covers). "" when the page is on
    no map or the split said nothing: the node's definition applies."""
    for m in state.maps.values():
        paths = {n.path_id for n in m.nodes if n.path_id in state.paths
                 and page_id in state.paths[n.path_id].page_ids}
        if paths or any(n.page_id == page_id for n in m.nodes):
            here = m.node(concept)
            return here.scope if here is not None and here.parent is not None else ""
    return ""


def _prereqs(k: AspectK, cid: str) -> tuple[str, ...]:
    return k.prereqs(cid) if cid in k.concept_ids() else ()


def concept_name(state: WikiState, cid: str, *, user_id: str = "") -> str:
    """The concept's recorded name, else 「未命名概念」 (logged; never the raw id)."""
    n = state.names.get(cid)
    return concept_label(n.name if n is not None else None, cid, user_id=user_id)


def page_spec(state: WikiState, k: AspectK, page_id: str, *, user_id: str = "",
              teach: PageTeach | None = None, bands_only: bool = False) -> PageSpec:
    """What the organizer / writer / patch need about one page besides K. KeyError if the page
    does not exist. `guidelines` = the teaching spec in force for the page (`teach`, default
    `wikiapp.teachspec.page_teach(state, page_id)`: applicable item texts + the 4 param lines; raises
    `SpecInvalid` for a user whose old guidelines overflow the spec). `bands_only`: a caller that
    only reads bands / staleness gets no guidelines and never raises SpecInvalid."""
    rec = state.pages[page_id]
    guidelines: tuple[str, ...] = ()
    if not bands_only:
        guidelines = (teach if teach is not None else page_teach(state, page_id)).guidelines
    concepts = []
    for c in rec.concepts:
        subs: dict[Aspect, tuple[str, ...]] = {}
        for s in _subitems(state, c):
            subs[s.aspect] = (*subs.get(s.aspect, ()), s.name)
        node = state.graph.node(c)
        concepts.append(PageConcept(id=c, name=concept_name(state, c, user_id=user_id),
                                    prereqs=_prereqs(k, c), subitems=subs,
                                    definition=goal_definition(state, page_id, c)
                                    or (node.definition if node is not None else "")))
    others = tuple(OtherPage(page_id=p, title=state.pages[p].title,
                             concepts=state.pages[p].concepts)
                   for p in state.page_order if p != page_id and state.pages[p].has_body
                   and p not in state.trashed_pages)
    roles = concept_roles(state, page_id)
    return PageSpec(page_id=page_id, goal=rec.goal,
                    concepts=tuple(concepts), targets=rec.targets or rec.concepts, other_pages=others,
                    guidelines=guidelines, roles=roles,
                    language=state.language)


def concept_roles(state: WikiState, page_id: str | None = None) -> dict[str, ConceptRole]:
    """Roles worked out from the pages, never stored and never from K (spec 2026-10-29
    retire-curate §3: a learned concept can still be a support). On page `page_id`: its targets
    are 在学, its other concepts 支撑, or 参考 when they are a target of a shelved page. Without a
    page (the K grid): a target of a page not shelved is 在学, a target of only shelved pages 参考,
    a concept only used by pages 支撑; a concept on no page has none."""
    live = [state.pages[p] for p in state.page_order if p not in state.trashed_pages]
    shelved = {t for r in live if r.shelved for t in r.targets}
    if page_id is not None:
        rec = state.pages.get(page_id)
        if rec is None:
            return {}
        return {c: "learn" if c in rec.targets else "reference" if c in shelved else "support"
                for c in rec.concepts}
    learning = {t for r in live if not r.shelved for t in r.targets}
    out: dict[str, ConceptRole] = {c: "support" for r in live for c in r.concepts}
    out.update({c: "reference" for c in shelved})
    out.update({c: "learn" for c in learning})
    return out


def is_page_stale(state: WikiState, k: AspectK, record: PageRecord, now: datetime, *,
                  user_id: str = "") -> bool:
    """「待更新」 (D7): K moved since the page's snapshot. An unorganized page, or one a run is
    writing, is never stale."""
    if not record.has_body:
        return False
    return is_stale(page_spec(state, k, record.page_id, user_id=user_id, bands_only=True),
                    record.page, AspectKView(k, now))


def apply_in_memory(snap: Snapshot, events: Sequence[AspectEvent]) -> AspectK:
    """A copy of the snapshot's K with `events` applied (the snapshot is left untouched)."""
    k = snap.k.copy()
    for ev in events:
        k.apply(ev)
    return k
