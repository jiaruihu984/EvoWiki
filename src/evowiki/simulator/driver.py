from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from evowiki.evaluation.outcomes import QuizOutcome
from evowiki.evidence.models import Event
from evowiki.simulator.curriculum import Curriculum, scripted_curriculum
from evowiki.simulator.frameworks import FrameworkLibrary
from evowiki.simulator.learner import SimLearner, SimMechanics
from evowiki.simulator.params import SimParams
from evowiki.simulator.profiles import LearnerProfile
from evowiki.simulator.relprobe import probeable_relations
from evowiki.simulator.stimulus import PageStimulus, canonical_page
from evowiki.simulator.truth import GroundTruth
from evowiki.simulator.verbalize import TemplateVerbalizer, Verbalizer
from evowiki.simulator.world import SimWorld


@dataclass(frozen=True)
class PageContext:
    """What a page policy may see: observable history, the clock and the session."""

    concept_id: str
    history: tuple[Event, ...]
    now: datetime
    session_id: str
    # sim-relprobe-v1: the observable probe channel so far (empty without relation probes)
    probe_history: tuple[Event, ...] = ()


@dataclass(frozen=True)
class SessionPoint:
    """A point where the system may show view proposals: after a concept or at session end."""

    history: tuple[Event, ...]
    now: datetime
    session_id: str
    session_end: bool
    probe_history: tuple[Event, ...] = ()       # sim-relprobe-v1 channel so far


@dataclass(frozen=True)
class HubProposal:
    """A structural view change shown to the learner for confirmation."""

    suggestion_id: str
    members: frozenset[str]


PagePolicy = Callable[[PageContext], PageStimulus]


class SessionHooks(Protocol):
    def hubs(self, ctx: PageContext) -> Mapping[str, AbstractSet[str]]: ...

    def proposals(self, point: SessionPoint) -> Sequence[HubProposal]: ...


@dataclass
class Trajectory:
    """One learner's run. `events` and `outcomes` are observable; `truth` is hidden."""

    profile: LearnerProfile
    events: list[Event]
    outcomes: list[QuizOutcome]
    truth: GroundTruth
    probe_events: list[Event] = field(default_factory=list)   # sim-relprobe-v1 channel


def run_scripted(
    world: SimWorld,
    library: FrameworkLibrary,
    profile: LearnerProfile,
    *,
    n_sessions: int,
    start: datetime,
    curriculum: Curriculum | None = None,
    page_policy: PagePolicy | None = None,
    verbalizer: Verbalizer | None = None,
    hooks: SessionHooks | None = None,
    posttest: bool = False,
    mechanics: SimMechanics | None = None,
    params: SimParams | None = None,
    relation_probes: bool = False,
) -> Trajectory:
    """Walk the LA → ML → DL script for `n_sessions` sessions.

    Each session studies the next `profile.concepts_per_session` concepts (round-robin review
    once the script is exhausted). For each concept: navigation through `hooks.hubs` (only with
    hooks), the page view built by `page_policy` from a `PageContext`, the page's probe item if
    it has one (probe stream), maybe ask (only if the page has Q&A enabled), one practice item,
    maybe self-report, then responses to `hooks.proposals` (only with hooks). Then one review
    item on an earlier concept and, with hooks, the session-end proposals. With `posttest`, one
    extra session (`<user>-post`) one day after the last answers every world item once, in id
    order, on the post-test stream. `mechanics` sets the simulator world mechanics.
    `world.params` is the single parameter source (attention, relation, post-test); `params`
    is accepted only when equal to `world.params`, otherwise ValueError (fail-closed).
    `truth.final` records the hidden state at the end of the study sessions.

    Session start times depend only on the seed, so every page policy sees the same calendar
    (common random numbers). The defaults reproduce the pre-Plan-4 trajectory exactly.

    With `relation_probes` (sim-relprobe-v1), at the start of every session after the first,
    ONE relation shown (connection / perspective segment, visibility "shown") on the previous
    session's pages is probed, chosen by `_pick_probe`; answers go to `probe_events` and change
    nothing else. Probes due after the last study session are dropped. Off (the default) the
    trajectory is bit-identical to the v5 simulator.
    """
    if n_sessions < 1:
        raise ValueError("n_sessions must be >= 1")
    if params is not None and params != world.params:
        raise ValueError("params differs from world.params; the world is the single parameter "
                         "source, build it with the same SimParams (or pass params=None)")
    curriculum = curriculum or scripted_curriculum(world.graph)
    order = curriculum.order
    if not order:
        raise ValueError("empty curriculum")
    policy = page_policy or (lambda ctx: canonical_page(world, ctx.concept_id))
    verbalizer = verbalizer or TemplateVerbalizer(world.graph)
    lr = SimLearner(world, profile, library, start, mechanics, params)
    k = profile.concepts_per_session
    lo, hi = profile.session_gap_days
    at = start
    phase: str | None = None
    studied: list[str] = []
    # sim-relprobe-v1: relations shown on the previous session's pages -> render plan of their
    # first showing; at the next session start ONE is probed (fixed rule, `_pick_probe`)
    shown: dict[str, str | None] = {}
    for s in range(n_sessions):
        at = at + timedelta(days=lo + (hi - lo) * lr.crn.uniform("gap", s))
        sid = f"{lr.user_id}-s{s:03d}"
        lr.begin_session(sid, at)
        if shown:
            rid = _pick_probe(lr, s - 1, shown)
            lr.answer_relation_probe(rid, shown[rid])
        shown = {}
        if len(studied) < len(order):
            batch = order[len(studied): len(studied) + k]
        else:
            batch = tuple(order[(s * k + j) % len(order)] for j in range(k))
        for cid in batch:
            cid_phase = curriculum.phase_of(cid)
            if cid_phase != phase:
                lr.enter_phase(cid_phase, at)
                phase = cid_phase
            if hooks is not None:
                lr.navigate_to(cid, hooks.hubs(PageContext(cid, tuple(lr.events), lr.now, sid,
                                                           tuple(lr.probe_events))))
            page = policy(PageContext(cid, tuple(lr.events), lr.now, sid,
                                      tuple(lr.probe_events)))
            if relation_probes:
                for rid in sorted(probeable_relations(world, page)):
                    shown.setdefault(rid, page.render_plan_id)
            lr.view_page(page)
            if page.probe_item_id is not None:
                lr.answer_item(page.probe_item_id, page.render_plan_id, stream="probe")
            if page.qa_enabled:
                lr.maybe_ask(cid, verbalizer)
            items = world.items_for_concept(cid, primary_only=True)
            if items:
                lr.answer_item(items[(lr.visit_count(cid) - 1) % len(items)])
            lr.maybe_self_report(cid)
            if hooks is not None:
                _respond(lr, hooks, sid, session_end=False)
            if cid not in studied:
                studied.append(cid)
        earlier = [c for c in studied
                   if c not in batch and world.items_for_concept(c, primary_only=True)]
        if earlier:
            c = lr.crn.rng("review", s).choice(earlier)
            items = world.items_for_concept(c, primary_only=True)
            lr.answer_item(items[lr.crn.rng("review_item", s).randrange(len(items))])
        if hooks is not None:
            _respond(lr, hooks, sid, session_end=True)
        lr.end_session()
    # Hidden state at end of study; the post-test never alters it. `at` is the last study
    # session's begin time, which every page policy shares (post-test clock, spec §4.4).
    lr.truth.final = lr.final_state(last_session_start=at)
    if posttest:
        lr.begin_session(f"{lr.user_id}-post", at + timedelta(days=1))
        for item_id in sorted(world.graph.item_ids):
            lr.answer_item(item_id, stream="posttest")
        lr.end_session()
    return Trajectory(profile, list(lr.events), list(lr.outcomes), lr.truth,
                      list(lr.probe_events))


def _pick_probe(lr: SimLearner, session: int, shown: Mapping[str, str | None]) -> str:
    """The fixed probe rule: a uniform pick among the session's shown relations (sorted ids)
    on a private CRN stream keyed by the session index; it consumes no other draw."""
    return lr.crn.rng("rel_probe_pick", session).choice(sorted(shown))


def _respond(lr: SimLearner, hooks: SessionHooks, sid: str, *, session_end: bool) -> None:
    point = SessionPoint(tuple(lr.events), lr.now, sid, session_end, tuple(lr.probe_events))
    for p in hooks.proposals(point):
        lr.respond_to_hub_proposal(p.suggestion_id, p.members)
