"""Review scheduling, forgetting and the daily / weekly digest (plan rev 3.2 §9.3).

Pure and deterministic: no IO, no LLM, no clock (callers pass aware `as_of`). Reads `AspectK`
only; the digest replays the aspect event log, so the same log always gives the same digest.

Scheduling is FSRS's (user ruling 2026-10-24): K's cell memory is FSRS-6 (`belief.memory`), and
a cell is due when its predicted recall falls below the desired retention (DESIRED_RETENTION,
FSRS's default 0.9), i.e. once the FSRS interval has passed.
That target is a scheduling one: a cell's band only drops (to shaky) much later, below
`BandParams.recall_min`. The three self-grades map to FSRS ratings as every answer does
(`AspectK`): 想起来了 `+` Good, 有点模糊 `~` Hard, 没想起来 `-` Again. "Think first, then compare, grade
yourself in three steps" is the Anki / SuperMemo practice; a section's recall cue is the question
and its short answer the thing compared with, so no new items are generated."""

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from math import log1p
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from evowiki.belief.aspects import AspectConfig, AspectEvent, AspectK, Observe
from evowiki.belief.memory import days_to, retrievability
from evowiki.goals.contracts import (
    Band,
    CheckItem,
    Observation,
    PageOutline,
    Polarity,
    SectionText,
    cell_id,
    split_cell,
)

Grade = Literal["recalled", "fuzzy", "forgot"]     # 想起来了 / 有点模糊 / 没想起来
GRADE_POLARITY: dict[Grade, Polarity] = {"recalled": "+", "fuzzy": "~", "forgot": "-"}
RECALL_STRENGTH = 1.0      # same weight as a quiz answer (user ruling: recall updates like quiz)
GOAL_BONUS = 1.0
EXAM_BONUS = 1.0           # a frequently examined node (真题 常考) ranks like a goal concept
LEARNED = frozenset({Band.KNOWN, Band.LIKELY})
PAGE_ROUND_ID = "rvp"      # the id prefix of an answer in 「考考我这页」's round (`_in_page_round`)
DESIRED_RETENTION = 0.9    # FSRS's default target: due once predicted recall is below it


def recall_observation(cell: str, grade: Grade, evidence_ref: str) -> Observation:
    return Observation(cell=cell, polarity=GRADE_POLARITY[grade], strength=RECALL_STRENGTH,
                       kind="recall", evidence_ref=evidence_ref)


class ReviewSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_per_day: int = Field(default=10, ge=0)   # a batch size: the client may ask for more
    # look-ahead: a cell due within this many days is due now. 0 = FSRS's own rule (review on the
    # due day); FSRS's first interval is ~2 days, so a look-ahead would list a page just learned.
    horizon_days: float = Field(default=0.0, ge=0.0, le=30.0)


DEFAULT_SETTINGS = ReviewSettings()   # frozen, so a shared default is safe


class ReviewSource(BaseModel):
    """One page section that can drive a review: its recall cue with its answer or its verified
    check item."""

    model_config = ConfigDict(frozen=True)

    page_id: str
    section_id: str
    heading: str
    cells: tuple[str, ...]
    cue: str = ""
    answer: str = ""    # what the cue is compared with
    check: CheckItem | None = None


def sources_from_page(outline: PageOutline,
                      texts: Mapping[str, SectionText]) -> tuple[ReviewSource, ...]:
    out: list[ReviewSource] = []
    for s in outline.sections:
        t = texts.get(s.id)
        # a card is a question with its answer, about the section's text: a cue alone is only the
        # fold title
        cue = (t.cue.strip()
               if t is not None and t.body.strip() and t.answer.strip() else "")
        check = s.check if s.check is not None and s.check.verified else None
        if not cue and check is None:
            continue
        cells = s.cells or ((check.cell,) if check is not None else ())
        out.append(ReviewSource(page_id=outline.page_id, section_id=s.id, heading=s.heading,
                                cells=cells, cue=cue,
                                answer=t.answer.strip() if cue else "",
                                check=check))
    return tuple(out)


class Fading(BaseModel):
    """A tested cell not declared 「会」, learned (undecayed band) or answered after studying it (a
    grade of 模糊 / 忘了 must come back), predicted to be below the desired
    retention within the horizon (FSRS: due), or one that is relearning (forgotten, then missed, or
    declared 「不会」: its recall is fresh but its band stays capped at shaky until a recall-kind `+`).
    `days_to_threshold`: days until recall reaches the desired retention; < 0: already due."""

    model_config = ConfigDict(frozen=True)

    cell: str
    band: Band
    band_undecayed: Band
    recall_now: float
    recall_at_horizon: float
    days_to_threshold: float
    relearning: bool = False


def fading(k: AspectK, as_of: datetime, settings: ReviewSettings = DEFAULT_SETTINGS,
           deadline_days: Mapping[str, float] | None = None,
           postponed: Mapping[str, datetime] | None = None) -> tuple[Fading, ...]:
    """Cells due for review (FSRS): predicted recall at the horizon below the desired retention,
    or relearning.
    Sorted by days_to_threshold, then cell id. `deadline_days` (concept id -> days left until the
    exam of a goal that uses it): the horizon of those concepts reaches to the deadline when that
    is further than `horizon_days`, so what would be forgotten before the exam comes up now;
    the forgetting model itself is untouched, and a deadline in the past changes nothing.
    `postponed` (cell -> time): a cell is not due before that time (the assistant's
    postpone_reviews); only its due date moves, its stability / recall / band are what they are."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    target = DESIRED_RETENTION
    out: list[Fading] = []
    for c in k.concept_ids():
        for a in k.aspects(c):
            cell = cell_id(c, a)
            mem = k.memory(cell)
            if mem.last_tested is None or mem.stability is None:
                continue
            if postponed is not None and postponed.get(cell, as_of) > as_of:
                continue
            raw = k.belief(cell, as_of, decay=False)
            if raw.declared == "know" or (raw.band not in LEARNED and not k.studied(cell)):
                continue
            # as_of is checked aware above; last_tested is aware. Inline
            # rather than evowiki.timeutil: goals/ may import only G/K/role modules.
            elapsed = max(0.0, (as_of - mem.last_tested).total_seconds() / 86400.0)
            horizon = max(settings.horizon_days, (deadline_days or {}).get(c, 0.0))
            r_h = retrievability(mem.stability, elapsed + horizon)
            if not mem.relearning and r_h >= target:
                continue
            out.append(Fading(cell=cell, band=k.belief(cell, as_of).band, band_undecayed=raw.band,
                              recall_now=raw.recall, recall_at_horizon=r_h,
                              days_to_threshold=days_to(mem.stability, target) - elapsed,
                              relearning=mem.relearning))
    return tuple(sorted(out, key=lambda f: (f.days_to_threshold, f.cell)))


def dependents(k: AspectK) -> dict[str, int]:
    """How many defined concepts list each concept as a direct prerequisite."""
    out = {c: 0 for c in k.concept_ids()}
    for c in k.concept_ids():
        for p in k.prereqs(c):
            if p in out:
                out[p] += 1
    return out


class ReviewItem(BaseModel):
    """One review card: one section's recall cue (mode "cue") or its verified check ("check").
    `cells`: the due cells this card reviews, lead first (a check card has one); `cell` is the
    lead, the most important due one, and the card's metrics are its."""

    model_config = ConfigDict(frozen=True)

    cell: str
    cells: tuple[str, ...]
    mode: Literal["cue", "check"]   # cue: cover the cue, recall, compare, grade; check: ask it
    page_id: str
    section_id: str
    heading: str
    cue: str = ""
    answer: str = ""            # cue mode
    check: CheckItem | None = None
    recall_now: float
    recall_at_horizon: float
    days_to_threshold: float
    in_goal: bool
    dependents: int
    score: float
    exam: bool = False          # the concept is a frequently examined node (常考)


class ReviewPlan(BaseModel):
    """Today's list of cards. `due` counts every due card (beyond the cap too). A fading cell no
    section can review (no cue, no verified check) is no card, and nothing here counts it."""

    model_config = ConfigDict(frozen=True)

    items: tuple[ReviewItem, ...]
    due: int


CardKey = tuple[str, str, str]      # (page_id, section_id, mode)
_NEVER = datetime.min.replace(tzinfo=UTC)
_KIND_MODE = {"recall": "cue", "check": "check"}


def last_answered(events: Iterable[AspectEvent], before: datetime) -> dict[CardKey, datetime]:
    """When each card was last answered in a review, read from the answer refs
    (`review:{page}:{section}:{id}`; the evidence kind tells the mode). Only answers before `before`
    (the start of the user's today): a card still due after today's 模糊 / 忘了 stays the same card
    today, and the next due round moves on."""
    out: dict[CardKey, datetime] = {}
    for ev in events:
        if not isinstance(ev, Observe) or ev.ts >= before:
            continue
        mode = _KIND_MODE.get(ev.observation.kind)
        parts = ev.observation.evidence_ref.split(":", 3)
        if mode is not None and len(parts) == 4 and parts[0] == "review":
            key = (parts[1], parts[2], mode)
            out[key] = max(ev.ts, out.get(key, ev.ts))
    return out


def _pick(cell: str, sources: tuple[ReviewSource, ...],
          answered: Mapping[CardKey, datetime]) -> tuple[str, ReviewSource] | None:
    """The card on `cell` answered longest ago (never answered first, then page order): the cards
    of a cell take turns."""
    cards = [(mode, s) for s in sources
             for mode, covers in (("cue", cell in s.cells and bool(s.cue)),
                                  ("check", s.check is not None and s.check.cell == cell))
             if covers]
    return min(cards, key=lambda c: answered.get((c[1].page_id, c[1].section_id, c[0]), _NEVER),
               default=None)


def schedule(k: AspectK, as_of: datetime, sources: tuple[ReviewSource, ...] = (),
             goal_concepts: frozenset[str] = frozenset(),
             settings: ReviewSettings = DEFAULT_SETTINGS,
             already_reviewed: int = 0,
             deadline_days: Mapping[str, float] | None = None,
             postponed: Mapping[str, datetime] | None = None,
             exam_concepts: frozenset[str] = frozenset(),
             answered: Mapping[CardKey, datetime] | None = None) -> ReviewPlan:
    """Today's cards. Each fading cell goes to one card: of the cue and check cards covering it,
    the one `answered` longest ago (`_pick`, `last_answered`); a card is due when it got a cell,
    and it reviews exactly the cells it got - a cell shared by several cards is reviewed once,
    and a section's cells that are not due are not on its card. One grade of the card is one
    answer for each of its cells.
    Cards rank by their lead cell's urgency × importance and the next batch is kept: at most
    `max_per_day - already_reviewed` (a caller asking for more passes already_reviewed=0).
    urgency = 1 - predicted recall at the horizon, or 1.0 for a relearning cell (its recall is
    fresh, but it was just missed after being forgotten); importance = 1 + GOAL_BONUS·[concept
    in an active goal] + EXAM_BONUS·[concept in `exam_concepts`: examined often] + ln(1 +
    #concepts using it as a prerequisite). Ties break by cell id. `deadline_days`: see
    `fading`."""
    ranked, _ = _cards(k, fading(k, as_of, settings, deadline_days, postponed), sources,
                       goal_concepts, exam_concepts, answered or {})
    cap = max(0, settings.max_per_day - already_reviewed)
    return ReviewPlan(items=tuple(ranked[:cap]), due=len(ranked))


def _cards(k: AspectK, cells: Iterable[Fading], sources: tuple[ReviewSource, ...],
           goal_concepts: frozenset[str], exam_concepts: frozenset[str],
           answered: Mapping[CardKey, datetime]) -> tuple[list[ReviewItem], list[str]]:
    """Each of `cells` on one card (`schedule`), best first, and the cells no card covers."""
    deps = dependents(k)
    cards: dict[tuple[str, str, str], list[ReviewItem]] = {}
    missing: list[str] = []
    for f in cells:
        picked = _pick(f.cell, sources, answered)
        if picked is None:
            missing.append(f.cell)
            continue
        mode, s = picked
        concept, _ = split_cell(f.cell)
        in_goal = concept in goal_concepts
        in_exam = concept in exam_concepts
        n_dep = deps.get(concept, 0)
        urgency = 1.0 if f.relearning else 1.0 - f.recall_at_horizon
        score = urgency * (1.0 + GOAL_BONUS * in_goal + EXAM_BONUS * in_exam + log1p(n_dep))
        cards.setdefault((s.page_id, s.section_id, mode), []).append(ReviewItem(
            cell=f.cell, cells=(f.cell,), mode=mode, page_id=s.page_id,
            section_id=s.section_id, heading=s.heading, cue=s.cue if mode == "cue" else "",
            answer=s.answer if mode == "cue" else "",
            check=s.check if mode == "check" else None, recall_now=f.recall_now,
            recall_at_horizon=f.recall_at_horizon, days_to_threshold=f.days_to_threshold,
            in_goal=in_goal, dependents=n_dep, score=score, exam=in_exam))
    ranked: list[ReviewItem] = []
    for per_cell in cards.values():
        per_cell.sort(key=lambda i: (-i.score, i.cell))
        ranked.append(per_cell[0].model_copy(update={"cells": tuple(i.cell for i in per_cell)}))
    ranked.sort(key=lambda i: (-i.score, i.cell))
    return ranked, missing


def page_cards(k: AspectK, as_of: datetime, sources: tuple[ReviewSource, ...],
               goal_concepts: frozenset[str] = frozenset(),
               exam_concepts: frozenset[str] = frozenset(),
               answered: Mapping[CardKey, datetime] | None = None) -> ReviewPlan:
    """「考考我这页」: every cell `sources` (one page's) covers on one card, due or not, as in `schedule`:
    a cell's cards take turns. A grade of a card not yet due is an early review: FSRS sees the short
    elapsed time and gives little (`belief.memory`)."""
    cells: list[Fading] = []
    known = {cell_id(c, a) for c in k.concept_ids() for a in k.aspects(c)}
    for cell in dict.fromkeys(c for s in sources for c in s.cells if c in known):
        mem = k.memory(cell)
        b = k.belief(cell, as_of)
        cells.append(Fading(cell=cell, band=b.band, band_undecayed=k.belief(cell, as_of, decay=False).band,
                            recall_now=b.recall, recall_at_horizon=b.recall,
                            days_to_threshold=0.0 if mem.stability is None or mem.last_tested is None
                            else days_to(mem.stability, DESIRED_RETENTION)
                            - max(0.0, (as_of - mem.last_tested).total_seconds() / 86400.0),
                            relearning=mem.relearning))
    ranked, _ = _cards(k, cells, sources, goal_concepts, exam_concepts, answered or {})
    return ReviewPlan(items=tuple(ranked), due=len(ranked))


# Tier order for progressed / slipped. NONE ("no evidence") is not a tier: a change from or to it
# is neither progress nor a slip.
_RANK = {Band.KNOWN: 3, Band.LIKELY: 2, Band.SHAKY: 1, Band.GAP: 0}
_POLARITY_GRADE: dict[str, Grade] = {p: g for g, p in GRADE_POLARITY.items()}
Period = Literal["day", "week"]
_PERIOD_DAYS: dict[str, int] = {"day": 1, "week": 7}


def replay(events: Iterable[AspectEvent], config: AspectConfig, until: datetime, *,
           trusted: frozenset[tuple[str, str]] | None = None) -> AspectK:
    """State as of `until`: events with ts <= until, applied in log order. `trusted`: the
    prereq pairs K inference may follow (`AspectK`; None = all)."""
    if until.tzinfo is None:
        raise ValueError("until must be timezone-aware")
    k = AspectK(config, trusted=trusted)
    for ev in events:
        if ev.ts <= until:
            k.apply(ev)
    return k


def reviews_between(events: Iterable[AspectEvent], start: datetime,
                    end: datetime) -> dict[Grade, int]:
    """Self-graded cards with start < ts <= end: one per grade (its cells' recall evidence
    share the grade's evidence_ref). Check-mode reviews are `check` evidence and are not told
    apart from ordinary check answers, so they are not counted."""
    seen: dict[str, Grade] = {}
    for ev in events:
        if isinstance(ev, Observe) and ev.observation.kind == "recall" and start < ev.ts <= end:
            seen[ev.observation.evidence_ref] = _POLARITY_GRADE[ev.observation.polarity]
    out: dict[Grade, int] = {"recalled": 0, "fuzzy": 0, "forgot": 0}
    for g in seen.values():
        out[g] += 1
    return out


def _in_page_round(ref: str) -> bool:
    """An answer in 「考考我这页」's round (`review:{page}:{section}:{PAGE_ROUND_ID}_...`)."""
    parts = ref.split(":", 3)
    return len(parts) == 4 and parts[3].startswith(f"{PAGE_ROUND_ID}_")


def reviews_done(events: Iterable[AspectEvent], start: datetime, end: datetime) -> int:
    """Cards answered with start < ts <= end — what counts against `max_per_day`: self-grades
    (recall evidence) and check-mode review answers (check evidence whose evidence_ref starts
    `review:`; a page's own check answers are not reviews), one per answer: the evidence of a
    card's cells shares the answer's evidence_ref. A page's round (考考我这页) is extra study outside the
    batch (user ruling 2026-11-04): not counted."""
    return len({ev.observation.evidence_ref for ev in events
                if isinstance(ev, Observe) and start < ev.ts <= end
                and not _in_page_round(ev.observation.evidence_ref)
                and (ev.observation.kind == "recall"
                     or (ev.observation.kind == "check"
                         and ev.observation.evidence_ref.startswith("review:")))})


def page_round_answers(events: Iterable[AspectEvent], start: datetime, end: datetime) -> int:
    """Answers in 「考考我这页」's rounds with start < ts <= end (shown, never against `max_per_day`)."""
    return len({ev.observation.evidence_ref for ev in events
                if isinstance(ev, Observe) and start < ev.ts <= end
                and _in_page_round(ev.observation.evidence_ref)})


class BandChange(BaseModel):
    """`forgot`: the drop is (at least partly) forgetting — the undecayed band is higher than
    the shown one."""

    model_config = ConfigDict(frozen=True)

    cell: str
    before: Band
    after: Band
    forgot: bool = False


class Digest(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: Period
    start: AwareDatetime
    end: AwareDatetime
    progressed: tuple[BandChange, ...]
    slipped: tuple[BandChange, ...]
    fading: tuple[Fading, ...]
    reviews: dict[Grade, int]
    today: ReviewPlan


def digest(events: Sequence[AspectEvent], config: AspectConfig, as_of: datetime,
           period: Period = "day", sources: tuple[ReviewSource, ...] = (),
           goal_concepts: frozenset[str] = frozenset(),
           settings: ReviewSettings = DEFAULT_SETTINGS,
           day_start: datetime | None = None,
           trusted: frozenset[tuple[str, str]] | None = None,
           postponed: Mapping[str, datetime] | None = None,
           exam_concepts: frozenset[str] = frozenset(),
           deadline_days: Mapping[str, float] | None = None) -> Digest:
    """What improved, what slipped, what is fading, and today's review list over the last day or
    week, computed only from the event log (replay-stable). Improved / slipped means a real tier
    change (gap < shaky < likely < known); a cell with no evidence (NONE) on either side counts
    as neither. `day_start` is the start of the
    user's today (default: UTC midnight of `as_of`); reviews since then (`reviews_done`) count
    against `max_per_day`. `trusted`: the prereq pairs K inference follows (the live K's set).
    `postponed`, `deadline_days`: see `fading`. `exam_concepts`: the frequently examined nodes,
    ranked first in today's list (as in `schedule`)."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    start = as_of - timedelta(days=_PERIOD_DAYS[period])
    before = replay(events, config, start, trusted=trusted)
    after = replay(events, config, as_of, trusted=trusted)
    progressed: list[BandChange] = []
    slipped: list[BandChange] = []
    for c in after.concept_ids():
        for a in after.aspects(c):
            cell = cell_id(c, a)
            try:
                b0 = before.belief(cell, start).band
            except KeyError:
                b0 = Band.NONE
            now_b = after.belief(cell, as_of)
            if b0 not in _RANK or now_b.band not in _RANK:
                continue
            r0, r1 = _RANK[b0], _RANK[now_b.band]
            if r1 > r0:
                progressed.append(BandChange(cell=cell, before=b0, after=now_b.band))
            elif r1 < r0:
                raw = after.belief(cell, as_of, decay=False).band
                slipped.append(BandChange(cell=cell, before=b0, after=now_b.band,
                                          forgot=_RANK.get(raw, -1) > r1))
    if day_start is None:
        a_utc = as_of.astimezone(UTC)
        day_start = datetime(a_utc.year, a_utc.month, a_utc.day, tzinfo=UTC)
    # day_start itself counts as today: shift the open lower bound by a microsecond.
    done = reviews_done(events, day_start - timedelta(microseconds=1), as_of)
    # 快忘了 is what he can review: a cell no card covers is nothing he can act on
    fade = fading(after, as_of, settings, deadline_days, postponed)
    _, no_card = _cards(after, fade, sources, goal_concepts, exam_concepts, {})
    return Digest(period=period, start=start, end=as_of, progressed=tuple(progressed),
                  slipped=tuple(slipped),
                  fading=tuple(f for f in fade if f.cell not in no_card),
                  reviews=reviews_between(events, start, as_of),
                  today=schedule(after, as_of, sources, goal_concepts, settings,
                                 already_reviewed=done, deadline_days=deadline_days,
                                 postponed=postponed, exam_concepts=exam_concepts,
                                 answered=last_answered(events, day_start)))
