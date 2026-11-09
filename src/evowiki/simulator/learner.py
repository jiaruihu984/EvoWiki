import hashlib
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import ceil
from statistics import NormalDist
from typing import Any, Literal

from evowiki.content.models import PrereqKind
from evowiki.evaluation.outcomes import QuizOutcome
from evowiki.evidence.models import Event, EventType
from evowiki.evidence.payloads import (
    AskPayload,
    DwellPayload,
    RelationProbePayload,
    SegmentCollapsePayload,
    SegmentExpandPayload,
)
from evowiki.simulator.actr import activation, retrievability
from evowiki.simulator.crn import CRN
from evowiki.simulator.frameworks import FrameworkLibrary, LatentOrganization
from evowiki.simulator.irt import p_correct, z_score
from evowiki.simulator.params import SimParams
from evowiki.simulator.profiles import LearnerProfile
from evowiki.simulator.reactions import (
    anchoring_factor,
    hub_acceptance_probability,
    navigate,
)
from evowiki.simulator.relprobe import relprobe_item
from evowiki.simulator.stimulus import DETAIL_RANK, PageStimulus, SlotStimulus
from evowiki.simulator.truth import (
    AnswerTruth,
    AskTruth,
    AttentionTruth,
    ExternalTruth,
    FrameTruth,
    GroundTruth,
    HiddenFinalState,
    HubTruth,
    LearnTruth,
    MisconceptionTruth,
    NavigationTruth,
    PageTruth,
    RelationTruth,
    RelProbeTruth,
    SelfReportTruth,
    SnapshotTruth,
)
from evowiki.simulator.verbalize import AskIntent, AskKind, Verbalizer
from evowiki.simulator.world import SimWorld
from evowiki.timeutil import days_between

STEP = timedelta(minutes=2)
READINESS_FLOOR = {PrereqKind.FORMAL: 0.25, PrereqKind.PEDAGOGICAL: 0.6}
EXPAND_LEARN_SCALE = 0.5
REFRESHER_COLLAPSE_LEVEL = 0.7
P_COLLAPSE = 0.6
ACQUIRE_SCALE = 0.5
SELF_REPORT_KNOW = 0.6
SELF_REPORT_FORGOT = 0.3
ASK_KC_LEVEL = 0.5
EXTERNAL_LEARN_P = 0.5
SHALLOW_PENALTY = 0.6   # learning lost when a page is 4 levels shallower than the reader needs
SKIM_BASE = 0.5         # seconds on a section the reader does not need (always < SKIM_SECONDS)
SKIM_SPREAD = 2.0
AnswerStream = Literal["practice", "probe", "posttest"]
_STREAMS: frozenset[str] = frozenset({"practice", "probe", "posttest"})
SECTION_SLOTS = frozenset(DETAIL_RANK)


def depth_gain(needed: int, shown: int, penalty: float = SHALLOW_PENALTY) -> float:
    """Learning-rate multiplier for showing depth `shown` to a reader needing `needed` (0..4)."""
    return 1.0 - penalty * max(0, needed - shown) / 4.0


@dataclass(frozen=True)
class SimMechanics:
    """World mechanics that are simulator constructs, not learner traits. Swept in Stage I
    sensitivity analysis (spec §9.4.5)."""

    shallow_penalty: float = SHALLOW_PENALTY

    def __post_init__(self) -> None:
        if not 0.0 <= self.shallow_penalty <= 1.0:
            raise ValueError("shallow_penalty must be in [0, 1]")


def observable_user_id(profile: LearnerProfile) -> str:
    """Opaque, stable user id for events. Learner type and seed are hidden state."""
    digest = hashlib.blake2b(
        f"sim-user|{profile.learner_type.value}|{profile.seed}".encode(), digest_size=6
    )
    return f"u-{digest.hexdigest()}"


class SimLearner:
    """One partially observable simulated learner (spec §9).

    Hidden state: `known`, `traces`, `rel_known`, `rel_traces`, `misconceptions`, `latent`,
    `truth`. The system under test
    may read only `events` and `outcomes`.
    """

    def __init__(
        self,
        world: SimWorld,
        profile: LearnerProfile,
        library: FrameworkLibrary,
        start: datetime,
        mechanics: SimMechanics | None = None,
        params: SimParams | None = None,
    ):
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self.world = world
        self.profile = profile
        self.mechanics = mechanics or SimMechanics()
        self.params = params or world.params  # single parameter source: the world's by default
        self.user_id = observable_user_id(profile)
        self.crn = CRN(profile.seed, namespace=(profile.learner_type.value,))
        self.start = start
        self.now = start
        self.known: set[str] = set()
        self.traces: dict[str, list[datetime]] = {}
        self.misconceptions: set[str] = set()
        self.latent = LatentOrganization(library, dict(profile.scheduled_frames))
        self.events: list[Event] = []
        self.outcomes: list[QuizOutcome] = []
        self.truth = GroundTruth()
        self.probe_events: list[Event] = []   # sim-relprobe-v1 channel, outside `events`
        self._probe_seq = 0
        self._seq = 0
        self._session_id: str | None = None
        self._attempts: Counter[tuple[str, str]] = Counter()
        self._visits: Counter[str] = Counter()
        self._reports: Counter[str] = Counter()
        self._asks: Counter[str] = Counter()
        self._external_day = 0
        self._session_count = 0
        self._session_index = -1
        self._seg_exposures: Counter[tuple[int, str]] = Counter()
        self.rel_known: set[str] = set()
        self.rel_traces: dict[str, list[datetime]] = {}
        self._rel_exposures: Counter[tuple[int, str]] = Counter()
        self._init_hidden_state()
        u = min(max(self.crn.uniform("capacity"), 1e-12), 1.0 - 1e-12)
        ap = self.params.attention
        self.capacity = math.exp(ap.mu_C + ap.sigma_C * NormalDist().inv_cdf(u))

    # ---- hidden state -------------------------------------------------------------------

    def _init_hidden_state(self) -> None:
        g = self.world.graph
        for cid in g.concept_ids:
            frac = self.profile.known_fraction(g.concept(cid).domain)
            for kc in self.world.kcs_of_concept(cid):
                if not self.crn.bernoulli(frac, "init", kc):
                    continue
                self.known.add(kc)
                n = 1 + int(3 * self.crn.uniform("init_n", kc))
                self.traces[kc] = sorted(
                    self.start - timedelta(days=7 + 173 * self.crn.uniform("init_t", kc, j))
                    for j in range(n)
                )
                self.truth.learned.append(LearnTruth(self.start, kc, "initial"))
        for m in self.world.misconception_ids:
            if self.crn.bernoulli(self.profile.misconception_rate, "init_mc", m):
                self.misconceptions.add(m)
                self.truth.misconceptions.append(MisconceptionTruth(self.start, m, "initial"))
        self._update_latent()
        rp = self.world.params.relation
        for e in self.world.relations.entries:
            a, b = e.endpoints
            if not all(k in self.known for c in (a, b) for k in self.world.kcs_of_concept(c)):
                continue
            if not self.crn.bernoulli(rp.p_rel_prior, "init_rel", e.relation_id):
                continue
            t = self.start - timedelta(days=7 + 173 * self.crn.uniform("init_rel_t",
                                                                       e.relation_id))
            self.rel_known.add(e.relation_id)
            self.rel_traces[e.relation_id] = [t]
            self.truth.relations.append(RelationTruth(t, -1, e.relation_id, "", 0, "initial",
                                                      None, True))

    def rho(self, kc: str) -> float:
        """s_uk · ρ(A_uk): 0 for unknown KCs, ACT-R retrievability for known ones."""
        if kc not in self.known:
            return 0.0
        a = activation(self.traces.get(kc, []), self.now, self.profile.decay_d)
        return retrievability(a, self.profile.tau, self.profile.rho_scale)

    def rho_rel(self, rid: str) -> float:
        """ACT-R retrievability of relation `rid`; 0 if the relation was never acquired."""
        if rid not in self.rel_known:
            return 0.0
        d = self.profile.decay_d * self.world.params.relation.rel_decay_mult
        return retrievability(activation(self.rel_traces[rid], self.now, d), self.profile.tau,
                              self.profile.rho_scale)

    def final_state(self, last_session_start: datetime) -> HiddenFinalState:
        """Hidden end-of-study state; `last_session_start` (the last study session's begin
        time, set by the driver) is the policy-independent post-test clock (spec §4.4)."""
        return HiddenFinalState(
            ts=self.now, last_session_start=last_session_start,
            learner_type=self.profile.learner_type.value, seed=self.profile.seed,
            decay_d=self.profile.decay_d, tau=self.profile.tau, rho_scale=self.profile.rho_scale,
            rel_decay_mult=self.world.params.relation.rel_decay_mult,
            known=frozenset(self.known),
            traces=tuple(sorted((k, tuple(v)) for k, v in self.traces.items())),
            rel_known=frozenset(self.rel_known),
            rel_traces=tuple(sorted((r, tuple(v)) for r, v in self.rel_traces.items())),
            misconceptions=frozenset(self.misconceptions))

    def learned_fraction(self, concept_id: str) -> float:
        kcs = self.world.kcs_of_concept(concept_id)
        return sum(k in self.known for k in kcs) / len(kcs)

    def true_level(self, concept_id: str) -> float:
        kcs = self.world.kcs_of_concept(concept_id)
        return sum(self.rho(k) for k in kcs) / len(kcs)

    def needed_depth(self, concept_id: str) -> int:
        """Hidden: how much detail this reader needs now, 0 (none) .. 4 (everything)."""
        return min(4, max(0, ceil(4.0 * (1.0 - self.true_level(concept_id)) - 1e-9)))

    def visit_count(self, concept_id: str) -> int:
        return self._visits[concept_id]

    def _learn(self, kc: str, cause: str) -> None:
        self.known.add(kc)
        self.truth.learned.append(LearnTruth(self.now, kc, cause))  # type: ignore[arg-type]

    def _practice(self, kcs: Iterable[str]) -> None:
        for k in kcs:
            if k in self.known:
                self.traces.setdefault(k, []).append(self.now)

    def _update_latent(self) -> None:
        for fid in self.latent.on_progress(self.learned_fraction):
            self.truth.frames.append(FrameTruth(self.now, fid, "progress"))

    def enter_phase(self, phase: str, at: datetime | None = None) -> None:
        """Enter `phase`; the driver passes the session start as `at` so phase-entry truth is policy-independent."""
        stamp = self.now if at is None else at
        for fid in self.latent.on_phase(phase):
            self.truth.frames.append(FrameTruth(stamp, fid, "phase"))

    # ---- clock and sessions ---------------------------------------------------------------

    def advance_to(self, at: datetime) -> None:
        """Move the clock forward, applying invisible external learning events on the way."""
        if at < self.now:
            raise ValueError("cannot move the clock backwards")
        last_day = int(days_between(self.start, at))
        for day in range(self._external_day + 1, last_day + 1):
            if not self.crn.bernoulli(self.profile.external_rate_per_day, "external", day):
                continue
            self.now = max(self.start + timedelta(days=day), self.now)
            cid = self.crn.rng("external_pick", day).choice(self.world.concept_ids)
            learned = []
            for kc in self.world.kcs_of_concept(cid):
                if kc not in self.known and self.crn.bernoulli(EXTERNAL_LEARN_P, "external_learn", kc, day):
                    self._learn(kc, "external")
                    learned.append(kc)
            self._practice(self.world.kcs_of_concept(cid))
            self.truth.external.append(ExternalTruth(self.now, cid, tuple(learned)))
        self._external_day = max(self._external_day, last_day)
        self.now = at
        self._update_latent()

    def begin_session(self, session_id: str, at: datetime) -> None:
        if self._session_id is not None:
            raise RuntimeError("a session is already open")
        self._session_index = self._session_count
        self._session_count += 1
        self.advance_to(at)
        self._session_id = session_id

    def end_session(self) -> None:
        sid = self._require_session()
        self.truth.snapshots.append(
            SnapshotTruth(
                ts=self.now,
                session_id=sid,
                known=frozenset(self.known),
                retrievability=tuple((k, self.rho(k)) for k in sorted(self.known)),
                misconceptions=frozenset(self.misconceptions),
                active_frames=self.latent.active_frames,
            )
        )
        self._session_id = None

    def _require_session(self) -> str:
        if self._session_id is None:
            raise RuntimeError("no open session")
        return self._session_id

    def _emit(
        self, etype: EventType, payload: dict[str, Any], render_plan_id: str | None = None
    ) -> Event:
        ev = Event(
            event_id=f"{self.user_id}:{self._seq:06d}",
            user_id=self.user_id,
            session_id=self._require_session(),
            seq=self._seq,
            ts=self.now,
            type=etype,
            payload=payload,
            render_plan_id=render_plan_id,
        )
        self._seq += 1
        self.events.append(ev)
        self.now += STEP
        return ev

    # ---- observable actions ---------------------------------------------------------------

    def answer_item(self, item_id: str, render_plan_id: str | None = None,
                    stream: AnswerStream = "practice") -> QuizOutcome:
        """Answer a quiz item from hidden state + 3PL IRT only (no LLM). Each stream has its own
        attempt counter and CRN key, so probes and post-tests never shift practice draws."""
        self._require_session()
        if stream not in _STREAMS:
            raise ValueError(f"unknown answer stream {stream!r}")
        n = self._attempts[(stream, item_id)]
        self._attempts[(stream, item_id)] += 1
        params = self.world.item_params(item_id)
        kcs = self.world.kcs_of_item(item_id)
        rho = {k: self.rho(k) for k in kcs if k in self.known}
        z = z_score(params, self.known, rho, self.misconceptions,
                    self.world.item_misconceptions(item_id), self.world.deltas)
        p = p_correct(params, z)
        key = "answer" if stream == "practice" else stream
        correct = self.crn.bernoulli(p, key, item_id, n)
        self._practice(kcs)
        ev = self._emit(EventType.QUIZ_ANSWER, {"item_id": item_id, "correct": correct},
                        render_plan_id)
        outcome = QuizOutcome(item_id=item_id, ts=ev.ts, correct=correct, event_id=ev.event_id)
        self.outcomes.append(outcome)
        self.truth.answers.append(AnswerTruth(ev.event_id, item_id, z, p, stream))
        return outcome

    def view_page(self, page: PageStimulus) -> list[Event]:
        """Read one page: behaviour events, learning, misconception change, dwell."""
        self._require_session()
        if page.slots is not None:
            return self._view_slot_page(page)
        c = page.concept_id
        v = self._visits[c]
        self._visits[c] += 1
        first = len(self.events)
        rp = page.render_plan_id
        page_ts = self.now
        self._emit(EventType.PAGE_VIEW, {"concept_id": c}, rp)
        needed = self.needed_depth(c)
        depth = page.shown_depth()

        for p in sorted(page.expandables):
            p_expand = self.profile.curiosity + (1.0 - self.profile.curiosity) * (1.0 - self.true_level(p))
            if not self.crn.bernoulli(p_expand, "expand", c, p, v):
                continue
            self._emit(EventType.SEGMENT_EXPAND, SegmentExpandPayload(
                segment_id=page.expandable_segment(p), concept_id=p, role="prerequisite",
            ).model_dump(), rp)
            for kc in self.world.kcs_of_concept(p):
                p_kc = self.profile.p_learn * EXPAND_LEARN_SCALE
                if kc not in self.known and self.crn.bernoulli(p_kc, "learn_expand", kc, c, v):
                    self._learn(kc, "expand")
            self._practice(self.world.kcs_of_concept(p))

        for r in sorted(page.refreshers):
            level = self.true_level(r)
            self._practice(self.world.kcs_of_concept(r))
            if level > REFRESHER_COLLAPSE_LEVEL and self.crn.bernoulli(P_COLLAPSE, "collapse", c, r, v):
                self._emit(EventType.SEGMENT_COLLAPSE, SegmentCollapsePayload(
                    segment_id=page.refresher_segment(r), concept_id=r, role="refresher",
                ).model_dump(), rp)

        expansions = 0
        for seg, rank in sorted(page.section_expandables, key=lambda t: (t[1], t[0])):
            if depth >= needed:
                break
            if rank <= depth:
                continue
            p_open = self.profile.curiosity + (1.0 - self.profile.curiosity) * (needed - depth) / 4.0
            if self.crn.bernoulli(p_open, "expand_section", c, seg, v):
                self._emit(EventType.SEGMENT_EXPAND, SegmentExpandPayload(
                    segment_id=seg, concept_id=c, role="section",
                ).model_dump(), rp)
                depth = max(depth, rank)
                expansions += 1

        readiness = 1.0
        for p, kind in self.world.graph.direct_prerequisites(c):
            floor = READINESS_FLOOR[kind]
            readiness *= floor + (1.0 - floor) * self.true_level(p)
        boost = anchoring_factor(page, self.latent, self.learned_fraction, self.profile.anchoring_gain)
        gain = depth_gain(needed, depth, self.mechanics.shallow_penalty)
        p_learn = min(1.0, self.profile.p_learn * readiness * boost * gain)
        for kc in self.world.kcs_of_concept(c):
            if kc not in self.known and self.crn.bernoulli(p_learn, "learn", kc, v):
                self._learn(kc, "page")
        self._practice(self.world.kcs_of_concept(c))

        for m in self.world.misconceptions_of_concept(c):
            if m.id in self.misconceptions:
                if m.id in page.misconception_fixes and self.crn.bernoulli(
                    self.profile.p_repair, "mc_fix", m.id, v
                ):
                    self.misconceptions.discard(m.id)
                    self.truth.misconceptions.append(MisconceptionTruth(self.now, m.id, "fixed"))
            elif m.id not in page.misconception_fixes and self.crn.bernoulli(
                self.profile.misconception_rate * ACQUIRE_SCALE, "mc_acquire", m.id, v
            ):
                self.misconceptions.add(m.id)
                self.truth.misconceptions.append(MisconceptionTruth(self.now, m.id, "acquired"))

        level = self.true_level(c)
        ranks = dict(page.section_ranks)
        redundant = 0
        for seg in page.section_ids():
            rank = ranks.get(seg)
            u = self.crn.uniform("dwell", c, v, seg)
            if rank is not None and rank > needed:
                seconds = round(SKIM_BASE + SKIM_SPREAD * u, 1)
                redundant += 1
            else:
                seconds = round(1.0 + 59.0 * (1.0 - level) + 4.0 * u, 1)
            self._emit(EventType.DWELL, DwellPayload(segment_id=seg, seconds=seconds).model_dump(), rp)
        self.truth.pages.append(PageTruth(page_ts, c, rp, needed, depth, redundant, expansions,
                                          visit_index=v))
        self._update_latent()
        return self.events[first:]

    def _expand_slot(self, slot: SlotStimulus, c: str, v: int, needed: int, depth: int,
                     rp: str | None) -> bool:
        cur = self.profile.curiosity
        seg0 = slot.segment_ids[0]
        if slot.slot_type == "prereq_link":
            p = self.world.segment_concept(seg0)
            if not self.crn.bernoulli(cur + (1.0 - cur) * (1.0 - self.true_level(p)),
                                      "expand", c, p, v):
                return False
            self._emit(EventType.SEGMENT_EXPAND, SegmentExpandPayload(
                segment_id=seg0, concept_id=p, role="prerequisite").model_dump(), rp)
            return True
        if slot.slot_type in SECTION_SLOTS:
            rank = DETAIL_RANK[slot.slot_type]
            if depth >= needed or rank <= depth:
                return False
            prob = cur + (1.0 - cur) * (needed - max(depth, 0)) / 4.0
            key: tuple[object, ...] = ("expand_section", c, seg0, v)
            role = "section"
        else:
            # connection / perspective: the payload rejects any other slot type (fail-closed)
            prob, key, role = cur, ("expand_slot", c, seg0, v), slot.slot_type
        if not self.crn.bernoulli(prob, *key):
            return False
        self._emit(EventType.SEGMENT_EXPAND, SegmentExpandPayload(
            segment_id=seg0, concept_id=c, role=role).model_dump(), rp)
        return True

    def _view_slot_page(self, page: PageStimulus) -> list[Event]:
        """Stage I-B reading model (spec §4.2): slots are read in order; each displayed slot is
        attended with P = σ(a0 − a_pos·(i−1) − a_load·L_{<i}/C_u), where L counts only attended
        load. Every page-induced effect requires attention; external learning does not."""
        ap = self.params.attention
        c = page.concept_id
        v = self._visits[c]
        self._visits[c] += 1
        first, rp, page_ts = len(self.events), page.render_plan_id, self.now
        self._emit(EventType.PAGE_VIEW, {"concept_id": c}, rp)
        needed = self.needed_depth(c)
        depth, load, position, expansions = -1, 0.0, 0, 0
        core_read, persp_read = False, False
        fixes_read: set[str] = set()
        attended_shown: dict[str, bool] = {}
        for slot in page.slots or ():
            position += 1
            expanded = False
            seg0 = slot.segment_ids[0]
            if slot.visibility == "collapsed":
                if not self._expand_slot(slot, c, v, needed, depth, rp):
                    # displayed but never opened: logged for gate M9 accounting, no draw
                    self.truth.attention.append(AttentionTruth(
                        self.now, self._session_index, c, rp, position, slot.slot_type, seg0,
                        False, 0.0, False, self.capacity, tuple(slot.segment_ids),
                        visit_index=v, u_attend=None, visibility=slot.visibility))
                    continue
                expanded = True
                expansions += slot.slot_type in SECTION_SLOTS
            ell = sum(self.world.segment_load(s, ap.load_unit_tokens) for s in slot.segment_ids)
            p_att = 1.0 / (1.0 + math.exp(-(ap.a0 - ap.a_pos * (position - 1)
                                            - ap.a_load * load / self.capacity)))
            n = self._seg_exposures[(self._session_index, seg0)]
            self._seg_exposures[(self._session_index, seg0)] += 1
            u = self.crn.uniform("attend", self._session_index, seg0, n)
            attended = u < p_att  # bit-identical to crn.bernoulli(p_att, "attend", ...)
            self.truth.attention.append(AttentionTruth(
                self.now, self._session_index, c, rp, position, slot.slot_type, seg0, expanded,
                p_att, attended, self.capacity, tuple(slot.segment_ids),
                visit_index=v, u_attend=u, visibility=slot.visibility))
            if slot.visibility == "shown":
                for sid in slot.segment_ids:
                    attended_shown[sid] = attended
            if not attended:
                continue
            load += ell
            self._read_slot(slot, c, v, rp)
            if slot.slot_type in SECTION_SLOTS:
                depth = max(depth, DETAIL_RANK[slot.slot_type])
                core_read = True
                if slot.slot_type == "misconception_fix":
                    for sid in slot.segment_ids:
                        fixes_read |= self.world.fix_misconceptions(sid)
            elif slot.slot_type == "perspective":
                persp_read = True
        shown_depth = max(depth, 0)
        if core_read:
            readiness = 1.0
            for p, kind in self.world.graph.direct_prerequisites(c):
                floor = READINESS_FLOOR[kind]
                readiness *= floor + (1.0 - floor) * self.true_level(p)
            # No hub anchoring on slot pages: hubs serve navigation only (spec §4.2).
            seen = replace(page, hub_members=None,
                           perspective_concepts=page.perspective_concepts if persp_read else None)
            boost = anchoring_factor(seen, self.latent, self.learned_fraction,
                                     self.profile.anchoring_gain)
            gain = depth_gain(needed, shown_depth, self.mechanics.shallow_penalty)
            p_learn = min(1.0, self.profile.p_learn * readiness * boost * gain)
            for kc in self.world.kcs_of_concept(c):
                if kc not in self.known and self.crn.bernoulli(p_learn, "learn", kc, v):
                    self._learn(kc, "page")
            self._practice(self.world.kcs_of_concept(c))
        for m in self.world.misconceptions_of_concept(c):
            if m.id in self.misconceptions:
                if m.id in fixes_read and self.crn.bernoulli(self.profile.p_repair, "mc_fix",
                                                             m.id, v):
                    self.misconceptions.discard(m.id)
                    self.truth.misconceptions.append(MisconceptionTruth(self.now, m.id, "fixed"))
            elif core_read and m.id not in fixes_read and self.crn.bernoulli(
                    self.profile.misconception_rate * ACQUIRE_SCALE, "mc_acquire", m.id, v):
                self.misconceptions.add(m.id)
                self.truth.misconceptions.append(MisconceptionTruth(self.now, m.id, "acquired"))
        level = self.true_level(c)
        ranks = dict(page.section_ranks)
        redundant = 0
        for seg, read in attended_shown.items():
            rank = ranks.get(seg)
            u = self.crn.uniform("dwell", c, v, seg)
            if not read or (rank is not None and rank > needed):
                seconds = round(SKIM_BASE + SKIM_SPREAD * u, 1)
                redundant += int(read)
            else:
                seconds = round(1.0 + 59.0 * (1.0 - level) + 4.0 * u, 1)
            self._emit(EventType.DWELL, DwellPayload(segment_id=seg, seconds=seconds).model_dump(),
                       rp)
        self.truth.pages.append(PageTruth(page_ts, c, rp, needed, shown_depth, redundant,
                                          expansions, visit_index=v))
        self._update_latent()
        return self.events[first:]

    def _read_slot(self, slot: SlotStimulus, c: str, v: int, rp: str | None) -> None:
        """Effects of an attended non-section slot: prerequisites and relation exposure."""
        seg0 = slot.segment_ids[0]
        if slot.slot_type == "prereq_refresher":
            r = self.world.segment_concept(seg0)
            level = self.true_level(r)
            self._practice(self.world.kcs_of_concept(r))
            if level > REFRESHER_COLLAPSE_LEVEL and self.crn.bernoulli(P_COLLAPSE, "collapse",
                                                                      c, r, v):
                self._emit(EventType.SEGMENT_COLLAPSE, SegmentCollapsePayload(
                    segment_id=seg0, concept_id=r, role="refresher").model_dump(), rp)
        elif slot.slot_type == "prereq_link":
            p = self.world.segment_concept(seg0)
            for kc in self.world.kcs_of_concept(p):
                p_kc = self.profile.p_learn * EXPAND_LEARN_SCALE
                if kc not in self.known and self.crn.bernoulli(p_kc, "learn_expand", kc, c, v):
                    self._learn(kc, "expand")
            self._practice(self.world.kcs_of_concept(p))
        elif slot.slot_type in ("connection", "perspective"):
            self._expose_relations(slot, c, rp)

    def _expose_relations(self, slot: SlotStimulus, c: str, rp_id: str | None) -> None:
        """Relation KCs (Stage I-B spec §4.1): an attended connection/perspective segment bound
        to relation e is one exposure. Unknown e is acquired with clipped probability
        p_rel_base·κ·level(a)·level(b)·(1 − d_e); known e gains an ACT-R trace."""
        rp = self.world.params.relation
        kappa = 1.0 if slot.slot_type == "connection" else rp.kappa_persp
        for seg in slot.segment_ids:
            for rid in self.world.relations_of_segment(seg):
                k = self._rel_exposures[(self._session_index, rid)]
                self._rel_exposures[(self._session_index, rid)] += 1
                if rid in self.rel_known:
                    self.rel_traces[rid].append(self.now)
                    self.truth.relations.append(RelationTruth(
                        self.now, self._session_index, rid, seg, k, "trace", None, False,
                        render_plan_id=rp_id, concept_id=c))
                    continue
                a, b = self.world.relation_endpoints(rid)
                p = min(1.0, max(0.0, rp.p_rel_base * kappa * self.true_level(a)
                                 * self.true_level(b) * (1.0 - self.world.relation_difficulty(rid))))
                got = self.crn.bernoulli(p, "rel_acquire", self._session_index, rid, k)
                if got:
                    self.rel_known.add(rid)
                    self.rel_traces[rid] = [self.now]
                self.truth.relations.append(RelationTruth(
                    self.now, self._session_index, rid, seg, k, "acquire_try", p, got,
                    render_plan_id=rp_id, concept_id=c))

    def answer_relation_probe(self, relation_id: str,
                              render_plan_id: str | None = None) -> Event:
        """Answer the relation probe of `relation_id` (sim-relprobe-v1) with the transfer
        post-test's model at the current time:
            z = w_r · r · ρ_e + w_end · (level(a) + level(b)) / 2 − penalty.
        Measurement only (spec S1a): no trace, acquisition or practice; the clock and the main
        event sequence do not advance; the event goes to `probe_events`, the hidden inputs to
        `truth.relprobe_log`. Its own CRN stream ("rel_probe") shifts no other draw."""
        sid = self._require_session()
        item = relprobe_item(self.world, relation_id)
        n = self._attempts[("relprobe", item.item_id)]
        self._attempts[("relprobe", item.item_id)] += 1
        a_end, b_end = self.world.relation_endpoints(relation_id)
        known = relation_id in self.rel_known
        rho_e = self.rho_rel(relation_id)
        penalty = sum(self.world.misconception(m).delta for m in self.misconceptions
                      if self.world.misconception(m).concept_id in (a_end, b_end))
        z = (item.w_r * (1.0 if known else 0.0) * rho_e
             + item.w_end * (self.true_level(a_end) + self.true_level(b_end)) / 2.0 - penalty)
        p = item.c + (1.0 - item.c) / (1.0 + math.exp(-item.a * (z - item.b)))
        correct = self.crn.bernoulli(p, "rel_probe", relation_id, n)
        ev = Event(event_id=f"{self.user_id}:rp{self._probe_seq:05d}", user_id=self.user_id,
                   session_id=sid, seq=self._probe_seq, ts=self.now,
                   type=EventType.RELATION_PROBE_ANSWER,
                   payload=RelationProbePayload(relation_id=relation_id, item_id=item.item_id,
                                                correct=correct).model_dump(),
                   render_plan_id=render_plan_id)
        self._probe_seq += 1
        self.probe_events.append(ev)
        self.truth.relprobe_log.append(RelProbeTruth(
            ev.event_id, self.now, self._session_index, relation_id, known, rho_e, z, p,
            correct, render_plan_id))
        return ev

    def maybe_self_report(self, concept_id: str) -> Event | None:
        """Self-report = true level + bias_u + noise (spec §9.2); emitted only when decisive."""
        self._require_session()
        n = self._reports[concept_id]
        self._reports[concept_id] += 1
        if not self.crn.bernoulli(self.profile.self_report_rate, "sr_gate", concept_id, n):
            return None
        level = self.true_level(concept_id)
        noise = self.crn.rng("sr_noise", concept_id, n).gauss(0.0, 1.0)
        reported = level + self.profile.self_report_bias + self.profile.self_report_noise * noise
        if reported >= SELF_REPORT_KNOW:
            claim = "know"
        elif reported < SELF_REPORT_FORGOT and self.learned_fraction(concept_id) > 0.0:
            claim = "forgot"
        else:
            return None
        ev = self._emit(EventType.SELF_REPORT, {"concept_id": concept_id, "claim": claim})
        self.truth.self_reports.append(SelfReportTruth(ev.event_id, concept_id, level, reported))
        return ev

    def maybe_ask(self, concept_id: str, verbalizer: Verbalizer) -> Event | None:
        """Maybe ask a question: structured intent first, then verbalized text."""
        self._require_session()
        n = self._asks[concept_id]
        self._asks[concept_id] += 1
        if not self.crn.bernoulli(self.profile.ask_rate, "ask_gate", concept_id, n):
            return None
        intent = self._ask_intent(concept_id, n)
        if intent is None:
            return None
        if intent.kind is AskKind.RELATION:
            other = intent.other_concept_id or ""
            about, relation = (concept_id, other), (concept_id, other)
        else:
            about, relation = (concept_id,), None
        payload = AskPayload(text=verbalizer.verbalize(intent), about_concept_ids=about,
                             relation=relation).model_dump()
        ev = self._emit(EventType.ASK, payload)
        self.truth.asks.append(AskTruth(ev.event_id, intent, verbalizer.version))
        return ev

    def _ask_intent(self, c: str, n: int) -> AskIntent | None:
        active = [m.id for m in self.world.misconceptions_of_concept(c) if m.id in self.misconceptions]
        if active:
            return AskIntent(AskKind.MISCONCEPTION, c, misconception_id=active[0])
        missing = [k for k in self.world.kcs_of_concept(c) if self.rho(k) < ASK_KC_LEVEL]
        if missing:
            return AskIntent(AskKind.KC_CONFUSION, c, kc=self.crn.rng("ask_kc", c, n).choice(missing))
        neighbors = [p for p, _ in self.world.graph.direct_prerequisites(c)]
        if neighbors:
            other = self.crn.rng("ask_rel", c, n).choice(neighbors)
            return AskIntent(AskKind.RELATION, c, other_concept_id=other)
        return None

    def navigate_to(self, target: str, hubs: Mapping[str, AbstractSet[str]]) -> list[Event]:
        """Find `target` by browsing the user's hubs; one hub page view per hub opened.

        The anchor is the active latent frame containing the target, else the target's
        G-neighbourhood (prerequisite_of/related_to)."""
        self._require_session()
        frame = self.latent.frame_for(target)
        g = self.world.graph
        anchor = frame.member_set if frame is not None else frozenset(
            {target} | {c for c in g.concept_ids if c != target and g.adjacent(target, c)})
        trace = navigate(target, hubs, anchor)
        self.truth.navigation.append(NavigationTruth(
            self.now, target, trace.found_via, trace.cost, trace.clicks, trace.branching,
            trace.backtracks))
        return [
            self._emit(EventType.PAGE_VIEW, {"hub_id": h, "navigation_target": target, "hop": i})
            for i, h in enumerate(trace.opened)
        ]

    def respond_to_hub_proposal(self, suggestion_id: str, members: AbstractSet[str]) -> Event:
        """Accept or reject a proposed hub; P(accept) rises with overlap against active frames."""
        self._require_session()
        p = hub_acceptance_probability(members, self.latent)
        accepted = self.crn.bernoulli(p, "hub", suggestion_id)
        etype = EventType.VIEW_SUGGESTION_ACCEPTED if accepted else EventType.VIEW_SUGGESTION_REJECTED
        ev = self._emit(etype, {"suggestion_id": suggestion_id, "members": sorted(members)})
        self.truth.hubs.append(HubTruth(ev.event_id, suggestion_id, p, accepted))
        return ev
