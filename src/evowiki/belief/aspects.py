"""Aspect K: concept × fixed-aspect cells (plan rev 3.1 Phase 1, §8.B). Pure and deterministic.

Reference behaviour is the prototype artifact v5 (`stats` / `band` / `observe`,
the product specification on r2-verify), which the user accepted;
its weights are copied literally and are not tuning surfaces. Forgetting (stability, recall) is
FSRS-6 through py-fsrs (`belief.memory`, user ruling 2026-10-24). Every change is an
`AspectEvent`; `AspectK.apply` is the only writer, so replaying the same events gives the same
state.

Replay equals the in-memory run only under the same `AspectConfig` (prior, infer and
BandParams): concept structure lives in the event log, parameters do not. Callers must pin and
version the config they replay with.

Deviation from the prototype: v5 uses `Number(strength)||0.5`, which turns strength 0 into 0.5.
Here strength 0 means weight 0 (`Observation` bounds strength to [0, 1]), so a zero-strength
observation adds no direct evidence and no inferred entry.

Forgetting enters the band (plan rev 3.2 §9.3): a tested cell (one with a quiz / check / recall
event, so `last` is set) whose recall is below `BandParams.recall_min` drops from known / likely to
shaky. Only the band changes; m, u and the counts do not, so a fresh recall restores the band.
Self-reports and 「会」 never set `last`, so they never decay.

Declarations (user decision 2026-11-04). 「会」 (`know`) is kept apart and always wins: the band is
known until it is cleared. 「不会」 (`dont`) is evidence, in the same calculation as everything else:
what the cell had is set aside and it starts again from DONT_EVIDENCE negative pseudo-counts, plus
one missed review (FSRS Again, relearning, so it is due now). Later answers move it like any other:
at the default config 1 and 2 right answers read shaky, 3 likely, 7 known. It stands (`declared`)
until a test's `+` brings the evidence band to likely, or `Declare(None / know)` takes it back: the
evidence set aside returns, and the memory too when no test came after it.

Only a test counts as a test: a quiz / check / recall with effective weight >= MIN_TEST_WEIGHT.
A weaker (or zero-strength) one still adds its pseudo-counts and inference, but does not set
`last` or change stability, so an inference-only cell never starts decaying from it.

Memory (FSRS, `belief.memory`): a counted test from a formal review card or a page's question
(`moves_schedule`; a cell's first test of any origin too) is one FSRS review of the cell, in replay order at its own timestamp: `+` Good (a right answer picked from options: its stability × `CHOICE_RIGHT_DISCOUNT`), `~` Hard, `-` Again. The weight only decides WHETHER it is a
review (w >= MIN_TEST_WEIGHT: a full review at its grade; below: none). FSRS grades are discrete
and its stability update is fitted on whole reviews, so a partial weight is not interpolated into
it; the weight keeps scaling the Beta evidence as before. Massed answers (the same day) take
FSRS's short-term path and barely move stability; 「有点模糊」 (Hard) never lowers it, only a miss
(Again) does. A never-tested cell has no stability (None) and recall 1.0.

Relearning cap (user ruling a): a recall-kind miss or fuzzy answer (`-` / `~`) on a cell whose
recall at that moment is below `recall_min` marks it relearning. That answer resets `last`, so
recall is fresh again, but the band stays capped at shaky until the next counted recall-kind `+`
clears the flag. 「会」 still wins, and 「不会」 sets the flag too. Self, feedback and ask never set or
clear it.

θ_infer,u (`AspectConfig.infer`, an `InferParams` from teach/contracts.py) scales K per user. Every
factor is a ratio to the `InferParams()` default, so at the default each factor is exactly 1.0 and
the outputs are bit-identical to the pre-wiring K (golden: tests/belief/data/aspect_golden.json).
The contract's default numbers are nominal; only the ratio is used:
  - evidence weight: w *= infer.evidence_weight[kind] / default.evidence_weight[kind], after the
    caps and self discounts (`effective_weight`);
  - guess (quiz / check `+`): w *= (1 - g) / (1 - BASE_GUESS), g = min(0.75, guess_scale *
    BASE_GUESS); a correct answer is weaker evidence when guessing is likelier;
  - slip (quiz / check / recall `-` or `~`): w *= (1 - slip) / (1 - BASE_SLIP); a miss is weaker
    evidence when slips are likelier. Guess and slip apply in `_observe`; MIN_TEST_WEIGHT uses the
    final w;
  - forgetting: recall = FSRS retrievability at days / forgetting_scale (an effective stability
    of stored * forgetting_scale); the stored FSRS stability is unscaled and `memory()` reports
    the effective one (stored * forgetting_scale);
  - inference: the propagated weight is w * inference_strength;
  - starting mastery (一开始就会的可能, BKT's P(L0)): the prior odds prior_a / prior_b are multiplied
    by r = odds(start_known) / odds(default start_known) with the strength prior_a + prior_b kept
    (`start_prior`); at the default r is exactly 1.0 and the configured prior is used as it is.

Claims (wrong-answer follow-up): every quiz / check / recall `-` / `~` Observe remembers the direct
counts it added (`misses`). A `slip` Claim on it subtracts slip_claim × those counts; a `learned`
Claim adds learned_claim to `a`. Each claim remembers its exact delta, which a refuted
`ResolveClaim` subtracts again (a confirmed one keeps it). Claims are applied in log order, so a
prediction made while a claim stands sees it (RSI-A learns the two trust levels from the
confirmation questions). An answer can be claimed once.

Trusted prerequisites (relation layers, user ruling 2026-10-14): K cannot drop a stored prereq, so
which prereqs inference may follow is decided when K is built, never written into the log.
`AspectK(config, trusted=pairs)`: inference (the hop up to prereqs, the down-propagation to
dependants) follows prereq p of concept c iff (p, c) is in `pairs` — the node graph's verified
prerequisites as of NOW (`NodeGraph.trusted_pairs`, wired by `wikiapp.repo`), whether or not the
log has stored p as a prereq of c yet. So the relation set is the current one for EVERY event of
the replay: a prerequisite verified later also carries the evidence recorded before it was
verified (retroactive, user ruling 2026-10-24). K is never cached — every load / commit replays
the whole log with the current set — so the next replay after a verification is the re-replay.
Order: c's stored trusted prereqs first (log order), then trusted pairs not stored yet (sorted).
A pair whose concept is not defined yet at an event adds nothing for that event (no cells).
`trusted=None` (the default) follows every stored prereq, exactly as before (golden unchanged).
Replaying the same events with the same config AND the same trusted set gives the same state."""

import hashlib
import json
from datetime import UTC, datetime
from math import sqrt
from typing import Annotated, Literal, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from evowiki.belief import memory
from evowiki.belief.theta import ThetaInfer
from evowiki.goals.contracts import (
    Aspect,
    Band,
    BandParams,
    CellBelief,
    NoteEntry,
    Observation,
    cell_id,
    split_cell,
)
from evowiki.teach.contracts import EvidenceKind, InferParams
from evowiki.timeutil import days_between

SELF_DISCOUNT = 0.8
SELF_MECHANISM_DISCOUNT = 0.5
KIND_CAP = {"ask": 0.6}
FUZZY_A, FUZZY_B = 0.4, 0.8
RECALL_KINDS = frozenset({"quiz", "check", "recall"})
# A quiz / check / recall below this effective weight does not count as a test (no `last`, no
# stability change, no relearning flag). Equals the default BandParams.min_direct_evidence.
MIN_TEST_WEIGHT = 0.25
# Tests that update mastery only (`moves_schedule`): the opening diagnosis, 「我早会了」's confirm question,
# and the chat quiz's answers in old logs.
MASTERY_ONLY_REFS = ("probe:", "known:", "quiz:asst:")
# 「不会」: the negative pseudo-counts a cell restarts from. One right answer must not read as
# learned: (prior_a + 1) / (prior_a + prior_b + DONT_EVIDENCE + 1) = 0.36 at the default prior.
DONT_EVIDENCE = 1.5

# Inference (prototype v5 literal; not tuning surfaces). Stored per source event, revocable.
WITHIN: dict[Aspect, dict[Aspect, float]] = {
    Aspect.PROCEDURE: {Aspect.NOTATION: 0.4, Aspect.TERM: 0.3},
    Aspect.INTERPRET: {Aspect.PURPOSE: 0.3},
    Aspect.MECHANISM: {Aspect.NOTATION: 0.3, Aspect.PROCEDURE: 0.3},
    Aspect.NOTATION: {Aspect.TERM: 0.3},
    Aspect.PURPOSE: {Aspect.TERM: 0.2},
}
HOP_ASPECTS = (Aspect.TERM, Aspect.PURPOSE, Aspect.NOTATION)
HOP_START = 0.5
MAX_HOPS = 2
DOWN_FACTOR = 0.15
DOWN_SOURCE = frozenset({Aspect.NOTATION, Aspect.PROCEDURE})
DOWN_TARGET = (Aspect.PROCEDURE, Aspect.NOTATION)

# θ_infer,u baselines: every InferParams factor is a ratio to these, so the default is exactly 1.0.
_DEFAULT_INFER = InferParams()
BASE_GUESS = 0.25                     # 1 / 4 options
BASE_SLIP = _DEFAULT_INFER.slip
MAX_GUESS = 0.75
GUESS_KINDS = frozenset({"quiz", "check"})
# A right answer picked from options can be recognised or guessed (user ruling 2026-11-04: page, component and
# review choice questions alike): FSRS Good with its new stability, kept, times this; the difficulty stays Good's.
# Only the schedule: the Beta evidence still reads it as right.
CHOICE_RIGHT_DISCOUNT = 0.6


class AspectRejected(ValueError):
    pass


def _require_aware(as_of: datetime) -> None:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")


class AspectConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    prior_a: float = Field(gt=0.0)
    prior_b: float = Field(gt=0.0)
    bands: BandParams = Field(default_factory=BandParams)
    infer: InferParams = Field(default_factory=InferParams)

    @classmethod
    def from_theta(cls, theta: ThetaInfer, bands: BandParams | None = None,
                   infer: InferParams | None = None) -> "AspectConfig":
        p = theta.default_prior
        return cls(prior_a=p.mean * p.strength, prior_b=(1.0 - p.mean) * p.strength,
                   bands=bands or BandParams(),
                   infer=InferParams() if infer is None else infer)

    @model_validator(mode="after")
    def _infer_complete(self) -> "AspectConfig":
        missing = [k for k in get_args(EvidenceKind) if k not in self.infer.evidence_weight]
        if missing:   # fail closed: a partial θ_infer,u never runs with nominal weights
            raise ValueError(f"infer.evidence_weight is missing {missing}")
        return self

    def with_infer(self, infer: InferParams) -> "AspectConfig":
        """A validated copy (model_copy would skip the validators)."""
        return type(self)(**{**dict(self), "infer": infer})


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=200)
    ts: AwareDatetime

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return v.astimezone(UTC)


class DefineConcept(_Event):
    """Define a concept, or extend one. Redefining an existing concept unions its aspects and
    prereqs (first-seen order kept), IGNORES the new difficulty (the first one is kept; it does not
    enter the FSRS memory, which starts from FSRS's own initial state), and silently drops a
    self-prerequisite."""

    type: Literal["define_concept"] = "define_concept"
    concept_id: str = Field(min_length=1)
    aspects: tuple[Aspect, ...] = Field(min_length=1)
    prereqs: tuple[str, ...] = ()
    difficulty: int = Field(default=3, ge=1, le=5)

    @field_validator("concept_id")
    @classmethod
    def _no_dot(cls, v: str) -> str:
        if "." in v:
            raise ValueError("concept ids may not contain '.'")
        return v


class Observe(_Event):
    type: Literal["observe"] = "observe"
    observation: Observation


class RevokeInference(_Event):
    type: Literal["revoke_inference"] = "revoke_inference"
    source_event_id: str = Field(min_length=1)


class Declare(_Event):
    type: Literal["declare"] = "declare"
    cell: str
    value: Literal["know", "dont"] | None


class AddNote(_Event):
    type: Literal["add_note"] = "add_note"
    cell: str
    text: str = Field(min_length=1, max_length=240)
    evidence_ref: str = Field(min_length=1)
    label: str | None = Field(default=None, max_length=600)   # display sentence; old rows: None


class RetireNote(_Event):
    type: Literal["retire_note"] = "retire_note"
    entry_id: str = Field(min_length=1)


class Claim(_Event):
    """The user's reading of a wrong answer (wrong-answer follow-up). `slip` (粗心选错了) sets aside
    the share `infer.evidence_weight["slip_claim"]` of the wrong answer's direct evidence
    (`source_event_id`, a quiz / check / recall `-` or `~` Observe on `cell`); `learned` (我学会了)
    adds a weak positive of weight `infer.evidence_weight["learned_claim"]`. Neither touches
    stability, `last`, relearning or inference. It stands until `ResolveClaim`."""

    type: Literal["claim"] = "claim"
    claim: Literal["slip", "learned"]
    cell: str
    source_event_id: str = Field(min_length=1)


class ResolveClaim(_Event):
    """The claim's confirmation question was answered: confirmed keeps the claim's effect;
    refuted takes it back exactly. Either way the claim is closed."""

    type: Literal["resolve_claim"] = "resolve_claim"
    claim_event_id: str = Field(min_length=1)
    outcome: Literal["confirmed", "refuted"]


class PageRead(_Event):
    """He finished reading a page that has a card on this cell (user ruling 2026-11-04, Anki's new card
    studied once): the cell's FSRS memory starts as after a first good review, so the card comes up in
    about two days. No evidence: m, u, band and inference stay. A cell already tested keeps its memory."""

    type: Literal["page_read"] = "page_read"
    cell: str = Field(min_length=1)


class RetractSelf(_Event):
    """He took back a 「我会」 he had said (a line deleted from 已经会的): that `+` self-report stops
    counting, its direct weight and everything inferred from it. No memory to touch (a self-report is
    not a test). Saying it again is a new `Observe`."""

    type: Literal["retract_self"] = "retract_self"
    source_event_id: str = Field(min_length=1)


AspectEvent = Annotated[
    DefineConcept | Observe | RevokeInference | Declare | AddNote | RetireNote | Claim
    | ResolveClaim | PageRead | RetractSelf,
    Field(discriminator="type")]
AspectEventAdapter: TypeAdapter = TypeAdapter(AspectEvent)


def note_id(event_id: str) -> str:
    return "m_" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:10]


class CellMemory(BaseModel):
    """Forgetting state of one cell: effective FSRS stability in days (recall 0.9 after that
    long; × forgetting_scale) and FSRS difficulty (1..10), both None until the first counted
    test; the last tested time (None: never tested, so it does not decay) and whether it is
    relearning (missed after being forgotten; its band is capped at shaky until the next counted
    recall-kind `+`)."""

    model_config = ConfigDict(frozen=True)

    stability: float | None
    last_tested: AwareDatetime | None
    relearning: bool = False
    difficulty: float | None = None


class ConceptSummary(BaseModel):
    """Concept-level roll-up of its aspect cells (concept-level K is kept as a summary)."""

    model_config = ConfigDict(frozen=True)

    concept_id: str
    bands: dict[Band, int]
    mean_m: float


def render_note(entries: tuple[NoteEntry, ...]) -> str:
    """Markdown note for an LLM prompt. Entry format after DeepTutor services/memory/document.py
    (Apache-2.0; https://github.com/HKUDS/DeepTutor): one bullet per entry, `<!--id-->` anchor,
    footnote citing the evidence. Format only; no code copied."""
    if not entries:
        return ""
    lines = [f"- {e.text} <!--{e.entry_id}-->[^{i}]" for i, e in enumerate(entries, 1)]
    refs = [f"[^{i}]: {e.cell} · {e.evidence_ref}" for i, e in enumerate(entries, 1)]
    return "\n".join(lines) + "\n\n" + "\n".join(refs) + "\n"


def effective_weight(o: Observation, infer: InferParams | None = None) -> float:
    w = min(o.strength, KIND_CAP.get(o.kind, 1.0))
    if o.kind == "self":
        if split_cell(o.cell)[1] is Aspect.MECHANISM:
            w *= SELF_MECHANISM_DISCOUNT
        w *= SELF_DISCOUNT
    if infer is not None:
        w *= infer.evidence_weight[o.kind] / _DEFAULT_INFER.evidence_weight[o.kind]
    return w


def _test_factor(o: Observation, infer: InferParams) -> float:
    """Guess / slip ratio for a test kind (1.0 at the default and for non-test kinds)."""
    if o.polarity == "+":
        if o.kind in GUESS_KINDS:
            g = min(MAX_GUESS, infer.guess_scale * BASE_GUESS)
            return (1.0 - g) / (1.0 - BASE_GUESS)
    elif o.kind in RECALL_KINDS:
        return (1.0 - infer.slip) / (1.0 - BASE_SLIP)
    return 1.0


def observed_weight(o: Observation, infer: InferParams) -> float:
    """The final weight `_observe` applies: effective weight × the guess / slip factor. A quiz /
    check / recall counts as a test iff this is >= MIN_TEST_WEIGHT."""
    return effective_weight(o, infer) * _test_factor(o, infer)


def _studied(o: Observation) -> bool:
    """An answer given after studying the cell: a self-grade, a page / review / confirmation check
    or an EvoUI block question. A `quiz` from the opening probe is not: it only says what he
    already knew, so a miss there is something to learn, not something to review."""
    return o.kind != "quiz" or o.evidence_ref.startswith("block:")


def moves_schedule(o: Observation) -> bool:
    """Whether a counted test is one FSRS review (user rulings 2026-11-04): a formal review card's
    submission, and an answer on a page (a check or an EvoUI block / component item), right or wrong:
    it amounts to a review. The others (`MASTERY_ONLY_REFS`) keep their weight, guess / slip and
    polarity in the Beta evidence, so mastery moves as before; a cell's first test of any origin still
    starts its memory, or nothing would ever come due."""
    return not o.evidence_ref.startswith(MASTERY_ONLY_REFS)


def _page_answer(o: Observation) -> bool:
    """A learner's answer to a question on a page: a page's check or an EvoUI block question."""
    return o.kind in GUESS_KINDS and o.evidence_ref.startswith(("check:", "block:"))


class _SetAside:
    """What a standing 「不会」 put aside, for `Declare(None / know)`. `memory`: (stability, difficulty,
    last, relearning, studied) before it, dropped by the next counted test."""

    __slots__ = ("a", "b", "inferred", "memory", "selfs")

    def __init__(self, a: float, b: float, inferred: dict[str, list[float]], memory: tuple | None,
                 selfs: dict[str, float]):
        self.a, self.b, self.inferred, self.memory, self.selfs = a, b, inferred, memory, selfs

    def clone(self) -> "_SetAside":
        return _SetAside(self.a, self.b, {k: list(v) for k, v in self.inferred.items()},
                         self.memory, dict(self.selfs))


class _Cell:
    __slots__ = ("a", "aside", "b", "claims", "declared", "difficulty", "inferred", "last",
                 "missed_last", "misses", "open_miss", "relearning", "selfs", "stability", "studied")

    def __init__(self):
        self.a = 0.0
        self.b = 0.0
        self.inferred: dict[str, list[float]] = {}   # source event id -> [pos, neg]
        self.declared: str | None = None
        self.stability: float | None = None     # FSRS state, None until the first counted test
        self.difficulty: float | None = None
        self.last: datetime | None = None
        self.relearning = False
        self.studied = False
        self.missed_last = False                # the latest counted test was not right
        self.misses: dict[str, tuple[float, float]] = {}   # wrong test answer id -> (da, db) added
        self.claims: dict[str, tuple[float, float]] = {}   # open claim id -> (da, db) applied
        self.selfs: dict[str, float] = {}       # his `+` self-report id -> da added (`RetractSelf`)
        self.aside: _SetAside | None = None     # only while a 「不会」 stands
        self.open_miss: str | None = None       # see `AspectK.open_miss`

    def dump(self) -> dict:
        d = {"a": self.a, "b": self.b, "inferred": self.inferred, "declared": self.declared,
             "stability": self.stability, "difficulty": self.difficulty,
             "last": self.last.isoformat() if self.last else None,
             "relearning": self.relearning}
        if self.claims:                    # only when used: older fingerprints stay identical
            d["claims"] = {k: list(v) for k, v in self.claims.items()}
        if self.aside is not None:
            d["aside"] = {"a": self.aside.a, "b": self.aside.b, "inferred": self.aside.inferred}
        return d

    def clone(self) -> "_Cell":
        c = _Cell()
        c.stability, c.difficulty = self.stability, self.difficulty
        c.a, c.b, c.declared, c.last = self.a, self.b, self.declared, self.last
        c.relearning, c.studied, c.missed_last = self.relearning, self.studied, self.missed_last
        c.inferred = {k: list(v) for k, v in self.inferred.items()}
        c.misses, c.claims, c.selfs = dict(self.misses), dict(self.claims), dict(self.selfs)
        c.aside = None if self.aside is None else self.aside.clone()
        c.open_miss = self.open_miss
        return c

    def recall(self, now: datetime, scale: float) -> float:
        if self.last is None or self.stability is None:
            return 1.0
        return memory.retrievability(self.stability, days_between(self.last, now), scale)


class _Concept:
    __slots__ = ("aspects", "difficulty", "prereqs")

    def __init__(self, aspects: list[Aspect], prereqs: list[str], difficulty: int):
        self.aspects, self.prereqs, self.difficulty = aspects, prereqs, difficulty


def start_prior(config: AspectConfig) -> tuple[float, float]:
    """(a, b) every cell starts from: the configured prior with its odds scaled by start_known's
    odds ratio to the default and its strength kept. The configured prior itself at the default."""
    s, s0 = config.infer.start_known, _DEFAULT_INFER.start_known
    r = (s / (1.0 - s)) / (s0 / (1.0 - s0))
    if r == 1.0:
        return config.prior_a, config.prior_b
    n, ar = config.prior_a + config.prior_b, config.prior_a * r
    m = ar / (ar + config.prior_b)
    return n * m, n * (1.0 - m)


TrustedPrereqs = frozenset[tuple[str, str]]   # (prereq, concept) pairs inference may follow


class AspectK:
    def __init__(self, config: AspectConfig, *, trusted: TrustedPrereqs | None = None):
        self.config = config
        self.trusted = trusted        # None: every stored prereq (module doc, trusted prereqs)
        self._trusted_of: dict[str, list[str]] = {}   # concept -> its trusted prereqs, sorted
        for p, c in sorted(trusted or ()):
            if p != c:
                self._trusted_of.setdefault(c, []).append(p)
        self._prior = start_prior(config)
        self._concepts: dict[str, _Concept] = {}     # definition order matters (dependants)
        self._cells: dict[str, _Cell] = {}
        self._seen: set[str] = set()
        # concept -> entry id -> note entry (insertion order). Never read for numbers.
        self._notes: dict[str, dict[str, NoteEntry]] = {}
        self._retired_notes: set[str] = set()        # retired entry ids are never reused
        self._claimed: set[str] = set()              # answers (Observe ids) already claimed

    # ---- read ----
    def aspects(self, concept_id: str) -> tuple[Aspect, ...]:
        c = self._concepts.get(concept_id)
        if c is None:
            raise KeyError(concept_id)
        return tuple(c.aspects)

    def concept_ids(self) -> tuple[str, ...]:
        return tuple(self._concepts)

    def prereqs(self, concept_id: str) -> tuple[str, ...]:
        """Every stored prereq (K never drops one); see `inference_prereqs` for the ones
        inference follows."""
        return tuple(self._concepts[concept_id].prereqs)

    def inference_prereqs(self, concept_id: str) -> tuple[str, ...]:
        """The prereqs inference follows: every stored one when `trusted` is None, else the
        trusted pairs of this concept (stored or not yet stored)."""
        return tuple(self._followed(concept_id, self._concepts[concept_id].prereqs))

    def _followed(self, concept_id: str, prereqs: list[str]) -> list[str]:
        if self.trusted is None:
            return prereqs
        stored = [p for p in prereqs if (p, concept_id) in self.trusted]
        return stored + [p for p in self._trusted_of.get(concept_id, ()) if p not in stored]

    def notes(self, concept_id: str) -> tuple[NoteEntry, ...]:
        return tuple(self._notes.get(concept_id, {}).values())

    def memory(self, cell: str) -> CellMemory:
        k = self._cells.get(cell)
        if k is None:
            raise KeyError(cell)
        s = None if k.stability is None else k.stability * self.config.infer.forgetting_scale
        return CellMemory(stability=s, last_tested=k.last, relearning=k.relearning,
                          difficulty=k.difficulty)

    def open_miss(self, cell: str) -> str | None:
        """The wrong answer that still stands on `cell`: the latest counted `-` to a page's check
        or an EvoUI block question, with no right answer after it and no 粗心答错 / 现在会了 on it.
        Left out: the opening probe (what he did not know yet), a review card (the schedule brings
        it back) and a confirmation question (its follow-up already goes back to the page)."""
        return self._cells[cell].open_miss

    def studied(self, cell: str) -> bool:
        """Whether the cell was answered after studying it (`_studied`), not only probed."""
        return self._cells[cell].studied

    def concept_summary(self, concept_id: str, as_of: datetime) -> ConceptSummary:
        bs = [self.belief(cell_id(concept_id, a), as_of) for a in self.aspects(concept_id)]
        counts = {b: 0 for b in Band}
        for b in bs:
            counts[b.band] += 1
        return ConceptSummary(concept_id=concept_id, bands=counts,
                              mean_m=sum(b.m for b in bs) / len(bs))

    def belief(self, cell: str, as_of: datetime, *, decay: bool = True) -> CellBelief:
        """Raises ValueError for a naive `as_of`, KeyError for an unknown cell. With
        ``decay=False`` the band is the pure evidence band: neither forgetting nor the relearning
        cap applies (「会」 still does); `recall` is still the real value."""
        _require_aware(as_of)
        k = self._cells.get(cell)
        if k is None:
            raise KeyError(cell)
        ipos = sum(v[0] for v in k.inferred.values())
        ineg = sum(v[1] for v in k.inferred.values())
        A = self._prior[0] + k.a + ipos
        B = self._prior[1] + k.b + ineg
        n = A + B
        m, u = A / n, sqrt(A * B / (n * n * (n + 1.0)))
        direct = k.a + k.b
        rec = k.recall(as_of, self.config.infer.forgetting_scale)
        return CellBelief(cell=cell, m=m, u=u, recall=rec, direct=direct,
                          inferred_pos=ipos, inferred_neg=ineg, declared=k.declared,
                          band=self._band(m, u, direct, ipos, k.declared,
                                          rec if decay else 1.0, decay and k.relearning, k.missed_last))

    def _band(self, m: float, u: float, direct: float, ipos: float,
              declared: str | None, recall: float, relearning: bool = False,
              missed_last: bool = False) -> Band:
        p = self.config.bands
        if declared == "know":
            return Band.KNOWN
        band = self._evidence_band(m, u, direct, ipos)
        if band in (Band.KNOWN, Band.LIKELY) and (relearning or recall < p.recall_min):
            return Band.SHAKY
        # his latest answer was not right: not known yet, whatever came before it (user ruling 2026-11-04)
        return Band.LIKELY if band is Band.KNOWN and missed_last else band

    def _evidence_band(self, m: float, u: float, direct: float, ipos: float) -> Band:
        p = self.config.bands
        if direct < p.min_direct_evidence:
            return Band.LIKELY if ipos >= p.inferred_likely_min else Band.NONE
        if m >= p.known_min_m and u <= p.known_max_u:
            return Band.KNOWN
        if m >= p.likely_min_m:
            return Band.LIKELY
        if m >= p.shaky_min_m:
            return Band.SHAKY
        return Band.GAP

    def to_json(self) -> str:
        """Canonical fingerprint for replay-equality checks, not a full state identity:
        sort_keys hides concept definition order and per-cell inferred-source order, which
        matter for future inference (dependants are visited in definition order)."""
        return json.dumps({
            "concepts": {c: {"aspects": [a.value for a in v.aspects], "prereqs": v.prereqs,
                             "difficulty": v.difficulty} for c, v in self._concepts.items()},
            "cells": {c: v.dump() for c, v in self._cells.items()},
            "seen": sorted(self._seen),
            "notes": {c: [e.model_dump() for e in v.values()] for c, v in self._notes.items()},
            "retired_notes": sorted(self._retired_notes),
            **({"claimed": sorted(self._claimed)} if self._claimed else {}),
        }, sort_keys=True)

    def copy(self) -> "AspectK":
        out = AspectK(self.config, trusted=self.trusted)
        out._concepts = {c: _Concept(list(v.aspects), list(v.prereqs), v.difficulty)
                         for c, v in self._concepts.items()}
        out._cells = {c: v.clone() for c, v in self._cells.items()}
        out._seen = set(self._seen)
        out._notes = {c: dict(v) for c, v in self._notes.items()}   # entries are frozen
        out._retired_notes = set(self._retired_notes)
        out._claimed = set(self._claimed)
        return out

    def self_reports(self, cell: str) -> tuple[str, ...]:
        """Ids of the `+` self-reports that count on `cell` (set aside by a 「不会」 included)."""
        k = self._require_cell(cell)
        return (*k.selfs, *(k.aside.selfs if k.aside is not None else ()))

    def open_claims(self, cell: str) -> tuple[str, ...]:
        """Ids of the claims on `cell` that are not resolved yet."""
        return tuple(self._require_cell(cell).claims)

    # ---- write ----
    def apply(self, ev: DefineConcept | Observe | RevokeInference | Declare | AddNote
              | RetireNote | Claim | ResolveClaim | RetractSelf) -> None:
        """Apply one event. Validation happens before any mutation, so a rejected event leaves
        the state exactly as it was."""
        if ev.event_id in self._seen:
            raise AspectRejected(f"event {ev.event_id} already applied")
        if isinstance(ev, DefineConcept):
            self._define(ev)
        elif isinstance(ev, Observe):
            self._require_cell(ev.observation.cell)
            self._observe(ev)
        elif isinstance(ev, RevokeInference):
            found = False
            for c in self._cells.values():
                for held in (c.inferred, c.aside.inferred if c.aside is not None else {}):
                    if held.pop(ev.source_event_id, None) is not None:
                        found = True
            if not found:
                raise AspectRejected(f"no inferred evidence from {ev.source_event_id}")
        elif isinstance(ev, RetractSelf):
            self._retract_self(ev.source_event_id)
        elif isinstance(ev, Declare):
            self._declare(self._require_cell(ev.cell), ev)
        elif isinstance(ev, AddNote):
            self._require_cell(ev.cell)
            nid = note_id(ev.event_id)
            if nid in self._retired_notes or any(nid in v for v in self._notes.values()):
                raise AspectRejected(f"note id {nid} already used")   # fail closed
            c, _ = split_cell(ev.cell)
            e = NoteEntry(entry_id=nid, cell=ev.cell, text=ev.text,
                          evidence_ref=ev.evidence_ref, label=ev.label)
            self._notes.setdefault(c, {})[e.entry_id] = e
        elif isinstance(ev, RetireNote):
            owner = next((c for c, v in self._notes.items() if ev.entry_id in v), None)
            if owner is None:
                raise AspectRejected(f"no live note {ev.entry_id}")
            del self._notes[owner][ev.entry_id]
            self._retired_notes.add(ev.entry_id)
        elif isinstance(ev, Claim):
            self._claim(ev)
        elif isinstance(ev, PageRead):
            k = self._require_cell(ev.cell)
            if k.stability is None:
                k.stability, k.difficulty = memory.review(None, None, None, "+", ev.ts)
                k.last, k.studied = ev.ts, True
        elif isinstance(ev, ResolveClaim):
            owner = next((c for c in self._cells.values() if ev.claim_event_id in c.claims), None)
            if owner is None:
                raise AspectRejected(f"no open claim {ev.claim_event_id}")
            da, db = owner.claims.pop(ev.claim_event_id)
            if ev.outcome == "refuted":
                owner.a = max(0.0, owner.a - da)
                owner.b = max(0.0, owner.b - db)
        else:
            raise AspectRejected(f"unknown event {type(ev).__name__}")
        self._seen.add(ev.event_id)

    def _require_cell(self, cell: str) -> _Cell:
        k = self._cells.get(cell)
        if k is None:
            raise AspectRejected(f"cell {cell} is not an aspect of a defined concept")
        return k

    def _retract_self(self, source: str) -> None:
        owner = next((c for c in self._cells.values() if source in c.selfs
                      or (c.aside is not None and source in c.aside.selfs)), None)
        if owner is None:
            raise AspectRejected(f"no standing self-report {source}")
        if source in owner.selfs:
            owner.a = max(0.0, owner.a - owner.selfs.pop(source))
        else:
            owner.aside.a = max(0.0, owner.aside.a - owner.aside.selfs.pop(source))
        for c in self._cells.values():
            c.inferred.pop(source, None)
            if c.aside is not None:
                c.aside.inferred.pop(source, None)

    def _define(self, ev: DefineConcept) -> None:
        c = self._concepts.get(ev.concept_id)
        if c is None:
            c = self._concepts[ev.concept_id] = _Concept([], [], ev.difficulty)
        for a in ev.aspects:
            if a not in c.aspects:
                c.aspects.append(a)
                self._cells[cell_id(ev.concept_id, a)] = _Cell()
        for p in ev.prereqs:
            if p != ev.concept_id and p not in c.prereqs:
                c.prereqs.append(p)

    def _declare(self, k: _Cell, ev: Declare) -> None:
        """「会」 and clearing take a standing 「不会」 back; 「不会」 is the module doc's restart. A second
        「不会」 restarts again, and everything set aside so far stays set aside."""
        if ev.value == "dont":
            old = k.aside
            memory_before = (k.stability, k.difficulty, k.last, k.relearning, k.studied)
            k.aside = _SetAside(k.a, k.b, k.inferred, memory_before, k.selfs) if old is None else _SetAside(
                old.a + k.a, old.b + max(0.0, k.b - DONT_EVIDENCE),
                {**old.inferred, **k.inferred}, old.memory, {**old.selfs, **k.selfs})
            k.a, k.b, k.inferred, k.selfs = 0.0, DONT_EVIDENCE, {}, {}
            k.stability, k.difficulty = memory.review(k.stability, k.difficulty, k.last, "-", ev.ts)
            k.last, k.relearning, k.studied = ev.ts, True, True
            k.open_miss = None
            k.missed_last = False
        elif k.aside is not None:
            aside, k.aside = k.aside, None
            k.a += aside.a
            k.b = max(0.0, k.b - DONT_EVIDENCE) + aside.b
            k.inferred = {**aside.inferred, **k.inferred}
            k.selfs = {**aside.selfs, **k.selfs}
            if aside.memory is not None:
                k.stability, k.difficulty, k.last, k.relearning, k.studied = aside.memory
        k.declared = ev.value

    def _claim(self, ev: Claim) -> None:
        k = self._require_cell(ev.cell)
        miss = k.misses.get(ev.source_event_id)
        if miss is None:
            raise AspectRejected(f"{ev.source_event_id} is not a wrong test answer on {ev.cell}")
        if ev.source_event_id in self._claimed:
            raise AspectRejected(f"answer {ev.source_event_id} is already claimed")
        weights = self.config.infer.evidence_weight
        if ev.claim == "slip":
            s = weights["slip_claim"]
            d = (-s * miss[0], -s * miss[1])
        else:
            d = (weights["learned_claim"], 0.0)
        k.a = max(0.0, k.a + d[0])
        k.b = max(0.0, k.b + d[1])
        k.claims[ev.event_id] = d
        self._claimed.add(ev.source_event_id)
        if k.open_miss == ev.source_event_id:
            k.open_miss = None

    def _observe(self, ev: Observe) -> None:
        o = ev.observation
        k = self._cells[o.cell]
        infer = self.config.infer
        w = observed_weight(o, infer)
        rec = k.recall(ev.ts, infer.forgetting_scale)
        if o.polarity == "+":
            da, db = w, 0.0
        elif o.polarity == "~":
            da, db = FUZZY_A * w, FUZZY_B * w * rec
        else:
            da, db = 0.0, w * rec
        k.a += da
        k.b += db
        if o.polarity == "+" and o.kind == "self":
            k.selfs[ev.event_id] = da
        if o.polarity != "+" and o.kind in RECALL_KINDS:
            k.misses[ev.event_id] = (da, db)
        if o.kind in RECALL_KINDS and w >= MIN_TEST_WEIGHT:
            k.missed_last = o.polarity != "+"
            if moves_schedule(o) or k.stability is None:
                k.stability, k.difficulty = memory.review(k.stability, k.difficulty, k.last,
                                                          o.polarity, ev.ts)
                if o.polarity == "+" and o.kind in GUESS_KINDS:
                    k.stability *= CHOICE_RIGHT_DISCOUNT
                if o.polarity == "+":
                    k.relearning = False
                elif rec < self.config.bands.recall_min:     # forgotten, then missed
                    k.relearning = True
                k.last = ev.ts
                if k.aside is not None:
                    k.aside.memory = None
            k.studied = k.studied or _studied(o)
            if o.polarity == "+":
                k.open_miss = None
            elif o.polarity == "-" and _page_answer(o):
                k.open_miss = ev.event_id
        if k.declared == "dont" and o.polarity == "+" and o.kind in RECALL_KINDS:
            self._close_dont(k)
        self._infer(ev.event_id, o, w)

    def _close_dont(self, k: _Cell) -> None:
        """Enough right answers: the 「不会」 is history. What it set aside is not coming back."""
        ipos = sum(v[0] for v in k.inferred.values())
        ineg = sum(v[1] for v in k.inferred.values())
        A, B = self._prior[0] + k.a + ipos, self._prior[1] + k.b + ineg
        if A / (A + B) >= self.config.bands.likely_min_m:
            k.declared, k.aside = None, None

    def _add_inferred(self, source: str, cell: str, pos: float, neg: float) -> None:
        k = self._cells.get(cell)
        if k is None or (pos == 0.0 and neg == 0.0):   # zero weight: no entry, revoke rejects
            return
        v = k.inferred.setdefault(source, [0.0, 0.0])
        v[0] += pos
        v[1] += neg

    def _infer(self, source: str, o: Observation, w: float) -> None:
        w = w * self.config.infer.inference_strength
        c, a = split_cell(o.cell)
        if o.polarity == "+":
            for b2, f in WITHIN.get(a, {}).items():
                self._add_inferred(source, cell_id(c, b2), f * w, 0.0)
            if a is not Aspect.TERM:
                self._hop(source, self._followed(c, self._concepts[c].prereqs), HOP_START, 1, w)
        elif a in DOWN_SOURCE:
            for d, spec in self._concepts.items():
                if c in self._followed(d, spec.prereqs):
                    for b2 in DOWN_TARGET:
                        self._add_inferred(source, cell_id(d, b2), 0.0, DOWN_FACTOR * w)

    def _hop(self, source: str, ids: list[str], f: float, depth: int, w: float) -> None:
        # Literal port of the prototype: no visited-set; depth bounds it at MAX_HOPS.
        for p in ids:
            for b2 in HOP_ASPECTS:
                self._add_inferred(source, cell_id(p, b2), f * w, 0.0)
            spec = self._concepts.get(p)
            if depth < MAX_HOPS and spec is not None:
                self._hop(source, self._followed(p, spec.prereqs), f / 2, depth + 1, w)


class AspectKView:
    """`contracts.KView` over an `AspectK` snapshot at `as_of` (recall is evaluated there).
    Read-only; callers that keep mutating `k` should pass `k.copy()`.

    A naive `as_of` raises ValueError. `aspects(c)` is `()` for a concept that was never defined,
    so `cells(...)` skips unknown concepts; `cell(x)` still raises KeyError for an unknown cell."""

    def __init__(self, k: AspectK, as_of: datetime):
        _require_aware(as_of)
        self._k, self._as_of = k, as_of

    def open_miss(self, cell: str) -> str | None:
        return self._k.open_miss(cell)

    def aspects(self, concept_id: str) -> tuple[Aspect, ...]:
        try:
            return self._k.aspects(concept_id)
        except KeyError:
            return ()

    def cell(self, cell: str) -> CellBelief:
        return self._k.belief(cell, self._as_of)

    def cells(self, concept_ids: tuple[str, ...]) -> dict[str, CellBelief]:
        return {cell_id(c, a): self.cell(cell_id(c, a))
                for c in concept_ids for a in self.aspects(c)}

    def note(self, concept_id: str) -> tuple[NoteEntry, ...]:
        return self._k.notes(concept_id)
