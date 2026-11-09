"""Offline cases (spec §3) and how they become harness replay cases.

An offline case is one section to rewrite: the learner, what they said about it and the earlier
text. `world` builds one in-memory wiki per case, owned by a stand-in user of its own
(`stand_in`), holding a one-section page, and the harness `Case` for it ("换个讲法" on that
section, as the product runs it: feedback mode, the product's own instruction plus the learner's
words), so `harness.replay.replay` rewrites it like any traced section. Who the learner is goes
where the product keeps it: the stand-in user's profile 「我是谁」, which the gateway appends to
the writer's calls (`profiles`, passed as the writer's `profile=`) exactly as online. An exemplar case (source "D") carries a good text and no complaint: replays of it are the
regression check (`Case.anchor`).

The split is by topic, never by case (two sections of one page never land on both sides), and a
pure function of (topics, seed), so it never moves once chosen."""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evowiki.belief.aspects import DefineConcept
from evowiki.deep.agent import SnapshotSeqs
from evowiki.goals.contracts import Aspect, OutlineSection, PageOutline, SectionRole, SectionText
from evowiki.goals.fold import Page, states_for
from evowiki.goals.page_update import feedback_outline
from evowiki.goals.rewrite_knobs import feedback_instruction
from evowiki.harness.replay import Case
from evowiki.profile.context import context_for
from evowiki.profile.note import WHO
from evowiki.wikiapp.events import (
    ConceptName,
    ConceptsNamed,
    LanguageSet,
    PageRecord,
    PageSaved,
    WikiEvent,
)
from evowiki.wikiapp.repo import Commit, MemoryRepo

T0 = datetime(2026, 10, 28, tzinfo=UTC)
SECTION = "s1"
SOURCES = frozenset({"A", "B", "C", "D"})     # TutorBench, CogBench, own Chinese, exemplar
SAID_LINE = "学习者看完这一段后接着说：「{said}」"


@dataclass(frozen=True)
class RubricItem:
    text: str
    critical: bool = False


@dataclass(frozen=True)
class OfflineCase:
    id: str
    topic: str
    source: str
    heading: str
    learner: str          # who the learner is (profile summary)
    said: str             # what they said about the section ("" for an exemplar)
    original: str         # the section's current text
    rubric: tuple[RubricItem, ...] = ()

    @property
    def exemplar(self) -> bool:
        return self.source == "D"


def load(path: Path) -> list[OfflineCase]:
    """One JSON object per line; a malformed line raises with its line number."""
    out: list[OfflineCase] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            c = OfflineCase(id=d["id"], topic=d["topic"], source=d["source"],
                            heading=d["heading"], learner=d["learner"], said=d.get("said", ""),
                            original=d["original"],
                            rubric=tuple(RubricItem(r["text"], bool(r.get("critical")))
                                         for r in d.get("rubric", ())))
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            raise ValueError(f"{path}:{n}: {e}") from e
        if c.source not in SOURCES:
            raise ValueError(f"{path}:{n}: unknown source {c.source!r}")
        if not c.original.strip():
            raise ValueError(f"{path}:{n}: case {c.id} has no original text")
        if not c.exemplar and not c.said.strip():
            raise ValueError(f"{path}:{n}: case {c.id} is not an exemplar but has no 'said'")
        out.append(c)
    ids = [c.id for c in out]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: duplicate case ids")
    return out


def _bucket(topic: str, seed: str) -> float:
    h = hashlib.sha256(f"{seed}:{topic}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def split(cases: Sequence[OfflineCase], *, held_out: float = 0.3,
          seed: str = "split-v1") -> tuple[list[OfflineCase], list[OfflineCase]]:
    """(dev, held_out): a topic goes to held-out when its hash bucket is below `held_out`."""
    dev = [c for c in cases if _bucket(c.topic, seed) >= held_out]
    test = [c for c in cases if _bucket(c.topic, seed) < held_out]
    return dev, test


def _page_id(c: OfflineCase) -> str:
    return f"pg-off-{c.id}"


def stand_in(user_id: str, c: OfflineCase) -> str:
    """The stand-in user of case `c`: one per case, so each has its own profile."""
    return f"{user_id}:{c.id}"


def profiles(cases: Sequence[OfflineCase], *, user_id: str) -> Callable[[str, str], str]:
    """The profile block per (user, purpose), as `profile.service.ProfileService.context` gives it:
    a case's learner text is its stand-in user's 「我是谁」; any other user has none."""
    who = {stand_in(user_id, c): c.learner.strip() for c in cases}

    def context(uid: str, purpose: str) -> str:
        text = who.get(uid, "")
        return context_for({WHO: (text,)}, purpose) if text else ""
    return context


def world(cases: Sequence[OfflineCase], *, user_id: str,
          language: str = "zh") -> tuple[MemoryRepo, dict[str, Case]]:
    """(repo, harness case per offline case id). Each case's page is in the wiki of its stand-in
    user (`stand_in`), its snapshot that wiki after the page was saved; `user_id` itself holds an
    empty wiki (language only), the one the base teaching spec is read from. The writer sees the
    learner only through the profile (`profiles`)."""
    users = {c.id: stand_in(user_id, c) for c in cases}
    repo = MemoryRepo([user_id, *users.values()])
    repo.commit(repo.load(user_id), Commit(aspect=[], wiki=[
        WikiEvent(event_id="wev-off-lang", ts=T0, body=LanguageSet(language=language))]))
    out: dict[str, Case] = {}
    for c in cases:
        uid, pid, cid = users[c.id], _page_id(c), f"t-{c.id}"
        outline = PageOutline(page_id=pid, goal=c.heading, concepts=(cid,), targets=(cid,),
                              sections=(OutlineSection(id=SECTION, heading=c.heading,
                                                       role=SectionRole.EXPLAIN),))
        page = Page(outline=outline, states=states_for(outline), texts={SECTION: SectionText(
            section_id=SECTION, cue=c.heading, body=c.original, cache_key="")})
        rec = PageRecord(page_id=pid, title=c.heading, goal=c.heading, concepts=(cid,),
                         targets=(cid,), page=page, created_at=T0)
        bodies = (LanguageSet(language=language),
                  ConceptsNamed(names=(ConceptName(concept_id=cid, name=c.heading),)),
                  PageSaved(record=rec, action="created"))
        aspects = [DefineConcept(event_id=f"aev-off-{c.id}", ts=T0, concept_id=cid,
                                 aspects=(Aspect.TERM, Aspect.PROCEDURE, Aspect.INTERPRET))]
        repo.commit(repo.load(uid), Commit(aspect=aspects, wiki=[
            WikiEvent(event_id=f"wev-off-{c.id}-{i}", ts=T0, body=b)
            for i, b in enumerate(bodies)]))
        snap = repo.load(uid)
        seqs = SnapshotSeqs(wiki_seq=snap.wiki_seq, aspect_seq=snap.aspect_seq,
                            infer_version=snap.infer_version)
        saved = snap.state.pages[pid]
        assert saved.page is not None
        target = saved.page.model_copy(update={
            "outline": feedback_outline(saved.page, SECTION, "other")})
        lines = [feedback_instruction("other", c.heading)]
        if c.said.strip():
            lines.append(SAID_LINE.format(said=c.said))
        out[c.id] = Case(case_id=c.id, run_id=f"offline:{c.id}", page_id=pid,
                         section_id=SECTION, signal_ids=(), anchor=c.exemplar, snapshot=seqs,
                         mode="feedback", instruction="\n".join(lines), user_id=uid,
                         page=target.model_dump(mode="json"))
    return repo, out


def signals_text(c: OfflineCase) -> str:
    """What the judge is told the learner said (empty for an exemplar)."""
    return c.said.strip()
