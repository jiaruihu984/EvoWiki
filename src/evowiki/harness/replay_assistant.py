"""Assistant-turn replays (harness 3b follow-up 3, plan 2026-10-23 §2, rules R1–R3): a traced
assistant turn answered again in process under the base pack + a candidate, tools from the trace's
cassette. The reference replay of a case is `replay_turn(case, PackDiff())`, as for deep replays.

The trace keeps the turn's first request **as sent**: H1 guidance applied, tools after H2 / H3,
history after H5 / H6, the per-turn context pack (H5 sizes) and any H9 notes already in its last
user message. A replay rebuilds only what a pack controls and the trace can rebuild, and keeps
everything else verbatim:

- **H1** (`assistant` step): the guidance sits in `assistant.loop.SYSTEM` between `⟦g:assistant⟧`
  markers, and SYSTEM is the head of every turn's system prompt (`loop._context`: SYSTEM, then the
  action / attachment rules, the language rule, the carry-over and the summary, none of which has
  a marker). So SYSTEM rendered with the guidance of the pack the run recorded (`pack_version`,
  read from the harness history) must be a prefix of the traced system; it is swapped for SYSTEM
  rendered with the candidate's guidance. Rendering is `prompt_steps.apply_marks`, the function
  `Gateway.as_sent` applies (`as_sent` itself drops the pinned guidance without an account or a
  harness source, so it cannot render for a past pack here). A traced system that does not start
  with it is unusable (`ReplaySourceMissing`, 「这次回答的系统提示对不上，不能重放」): the turn ran
  without an account (no guidance applied), or SYSTEM changed since.
- **H2**: offered tool descriptions and schemas are the recorded definitions, with only the
  candidate's explicit H2 text changes applied. The trace does not retain pre-override defaults;
  removing a historical override makes a case unavailable rather than using today's default.
- **H3**: the tools the pack switches off are dropped from the traced list (`apply.toolset`); a
  tool the trace did not offer is never added (no definition, no cassette).
- **Skill turns** (stage 3, user ruling 2026-10-28): a turn's tool list changes when a Skill loads
  mid-turn, so the replayed request number k offers the tools the traced request number k offered
  (rebuilt as above; past the traced requests, the last one's), and only those may run (the loop's
  execution gate, as live). load_skill / read_ref / offer / confirm are built-in tools
  (`builtin_specs`), answered from the cassette like every tool.
- **H4 / H5 elide**: the loop limits and the elide policy are the loop's own builders
  (`assistant.loop.turn_limits` / `turn_elide`) on the pack.
- **The model** (spec §4.1): the replay runs on the model the real turn used. The turn's traced
  model calls name it (`llm_call` provider / model of each traced model step); a turn whose
  calls used more than one model (a fallback) is unusable. The replay account is the round's
  group "rsi" (its spend is the round's), with the first tier pin (none, good, fast) under which
  the gateway routes `assistant.turn` to that model; when none does (the user's own rsi-tier
  choice wins over a pin, or the tier configs changed) the case is unusable
  (「重放用的模型和那次回答不一样，不能比」; the models go to the log only). A replay call answered by
  another model (a fallback) fails the replay (ReplayFailed, with its spend). The request params
  (H10 thinking among them) are the traced ones.
- H10, H9, the context sizes and H6 are not candidate material (rule R1, `diff.unmeasured`): a
  candidate that changes one is refused here too (HarnessInvalid), never replayed as if it had
  been measured.

The evaluation snapshot is the immutable first request, offered tool lists, and exact
(name, canonical arguments) tool results stored by the original trace. No tool reads current
product state or today's skill library. A missing historical result makes the case unavailable,
not a tool-error answer for the judge to score. Current deletion/suppression still vetoes a
historical snapshot; traces are not a way to recover forgotten data. Recorded context includes
memory, rules and profile as actually supplied at that time. Trace retention remains authoritative.
Replay model calls use the round's recording gateway and are charged even if a later tool read
makes the snapshot unavailable.

`ReplayResult.text`: the answer (the texts of every model call, joined as the loop joins them),
then one line per proposal or ledger action the replayed turn's tool calls returned, all marked 模拟."""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from evowiki.agent.errors import classify
from evowiki.agent.loop import Paused, ToolFinished, run_loop
from evowiki.agent.spec import AgentSpec, RequestState
from evowiki.agent.tools import ActionCard, ProposalDraft, Tool, ToolContext, ToolResult, ToolSpec
from evowiki.agent.trace import (
    REPLAY_RUN_PREFIX,
    REPLAY_TRIGGER,
    TraceSink,
    finish_failed,
)
from evowiki.harness import apply
from evowiki.harness.diff import PackDiff, apply_diff, unmeasured
from evowiki.harness.pack import HarnessInvalid, HarnessPack
from evowiki.harness.replay import Case, ReplayResult, candidate_ref
from evowiki.harness.replay_cases import (
    MISSING_RECORD,
    CassetteTool,
    cassette_tools,
    encode_assistant_case,
)
from evowiki.harness.snapshot import SnapshotBoundary, recorded_at
from evowiki.llm.chat import ChatRequest, ChatTurn, ToolDef
from evowiki.llm.gateway import USER_VERSION, Gateway
from evowiki.llm.models import LLMCall, ModelRef
from evowiki.llm.prompt_steps import apply_marks
from evowiki.llm.usage import Account
from evowiki.wikiapp.refine_service import ReplayFailed

log = logging.getLogger(__name__)

GROUP = "rsi"                       # the account group of a replay's model calls (round.GROUP)
SYSTEM_MISMATCH = "这次回答的系统提示对不上，不能重放"
NO_TRACE_RUN = "找不到这次回答的执行记录，不能重放"
NOT_A_TURN = "这条执行记录不是一次助手回答，不能重放"
OTHER_USER = "这次回答不是这个用户的，不能重放"
TRACE_EXPIRED = "这次回答的执行记录已过保留期，原文已清除，不能完整重放"
H2_UNKNOWN = "自己造的工具的说明改过，默认说明不明，不能重放"
MODEL_MISMATCH = "重放用的模型和那次回答不一样，不能比"
MODEL_UNKNOWN = "找不到这次回答用的模型，不能重放"
MODEL_MIXED = "这次回答中途换过模型，不能重放"
PINS: tuple[str | None, ...] = (None, "good", "fast")   # tier pins tried, in order
STEP = "assistant"                  # the H1 step whose markers sit in SYSTEM
SIMULATED = "【模拟】调用 {name}：重放不执行，没有写入"
SNAPSHOT_UNAVAILABLE = "历史快照没有这次工具读取，不能评估这次回答"
SNAPSHOT_WITHDRAWN = "历史来源已删除或抑制，不能重放"
SHOWS_CARD = frozenset({"open_review"})   # a read tool whose result the learner sees as a card


def _def(sp: ToolSpec) -> ToolDef:
    return ToolDef(name=sp.name, description=sp.description, parameters=sp.parameters)


class _ReplayTool:
    """One offered tool of a replay (module doc, The sandbox). `called(name)`: a tool without a real
    result was called, so the replay's text must show it."""

    def __init__(self, tape: CassetteTool, called: Callable[[str], None]) -> None:
        self.spec, self._tape, self._called = tape.spec, tape, called

    def resume(self, *_: Any) -> ToolResult:
        """A pausing tool's: a replay ends on the question (the loop's `Paused`), nobody replies."""
        raise RuntimeError("a replay never resumes")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> Any:
        out = self._answer(args, ctx)
        if out.error is None and (self.spec.effect != "read" or self.spec.name in SHOWS_CARD):
            self._called(self.spec.name)
        if out.pause is not None:       # a traced tool's spec knows no `pauses`; its result does
            self.spec = self.spec.model_copy(update={"pauses": True})
        return out

    def _answer(self, args: dict[str, Any], ctx: ToolContext) -> Any:
        got = self._tape.run(args, ctx)
        if got.content != MISSING_RECORD:
            return got
        return ToolResult(content=SNAPSHOT_UNAVAILABLE, error="snapshot_unavailable")


class ReplaySourceMissing(LookupError):
    """The traced turn cannot be the source of a faithful replay (`reason`: plain text). A
    LookupError, so a round treats it like a missing snapshot: the case is unusable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TracedTurn:
    """What a replay reads of one traced turn: its first request as sent, the tool-result
    cassette, the pack version the run recorded, and the models its model calls were answered by
    (distinct, in order)."""

    run_id: str
    user_id: str
    pack_version: int
    request: ChatRequest
    cassette: tuple[Mapping[str, Any], ...]
    models: tuple[ModelRef, ...]
    # the tools each traced model call offered, in order (() = the first request's for all)
    tool_lists: tuple[tuple[ToolDef, ...], ...] = ()
    page_id: str | None = None       # the page open as he sent it
    recorded_at: datetime | None = None
    sources: frozenset[str] = frozenset()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


@dataclass(frozen=True)
class AssistantReplayDeps:
    """`turn(run_id)`: the traced turn (`load_turn` over a connection); `pack_at(user_id,
    version)`: the user's pack at a past version (`store.harness.pack_at`); `trace`: where the
    replay's own run is traced (None: not traced; the run id is still reported)."""

    gateway: Gateway
    turn: Callable[[str], TracedTurn]
    pack_at: Callable[[str, int], HarnessPack]
    trace: TraceSink | None = None
    new_id: Callable[[str], str] = _new_id
    cutoff: datetime | None = None
    excluded_sources: frozenset[str] = frozenset()
    validate: Callable[[str], None] | None = None



def source_ids(run_id: str) -> frozenset[str]:
    """Canonical dependencies for a traced assistant turn; opaque run ids stay opaque."""
    if not run_id.startswith("assistant:"):
        return frozenset()
    sid, sep, mid = run_id.removeprefix("assistant:").partition(":")
    return frozenset({f"session:{sid}", f"turn:{sid}/{mid}"}) if sep else frozenset()


def trace_source_closure(conn: Any, user_id: str, run_id: str, inputs: Any = None) -> frozenset[str]:
    """A chat trace inherits preceding turns, not every future turn of its session."""
    from evowiki.harness.experience import dependency_closure, source_nodes
    from evowiki.store.db import fetch_all

    nodes = {f'trace:{run_id}', *source_ids(run_id), *source_nodes(inputs)}
    if run_id.startswith('assistant:'):
        sid, _, mid = run_id.removeprefix('assistant:').partition(':')
        previous = fetch_all(conn, "select e.payload->>'message_id' as message_id "
            "from assistant_entry e join assistant_session s using(session_id) "
            "where s.user_id=%s and e.session_id=%s and e.type='user' and e.seq <= "
            "(select seq from assistant_entry where session_id=%s and type='user' "
            "and payload->>'message_id'=%s) order by e.seq", (user_id, sid, sid, mid))
        nodes.update(f"turn:{sid}/{row['message_id']}" for row in previous)
    return frozenset(dependency_closure(conn, user_id, sorted(nodes)))


def _current_access(conn: Any, row: Mapping[str, Any]) -> None:
    """A frozen trace remains subject to today's privacy boundary, including pending deletes.

    Old traces lack a complete source graph. While any suppression is active they cannot prove
    that paraphrased context is safe, so they are unavailable rather than keyword-redacted.
    """
    from evowiki.store.bot import forgotten_words
    from evowiki.store.db import fetch_one
    uid = row["user_id"]
    blocked = fetch_one(conn, "select exists(select 1 from forget_request where user_id=%s "
                        "and state <> 'committed') or exists(select 1 from memory_suppression "
                        "where user_id=%s) as blocked", (uid, uid))
    if blocked["blocked"] or forgotten_words(conn, uid):
        raise ReplaySourceMissing(SNAPSHOT_WITHDRAWN)


def load_turn(conn: Any, run_id: str) -> TracedTurn:
    """The traced turn `run_id` (an assistant turn run, not a replay). ReplaySourceMissing when
    the run is gone, is not one or its texts expired (store.retention); LookupError / `agent.trace.NotReplayable` from
    `encode_assistant_case` (no model step, attachments sent)."""
    from evowiki.assistant.loop import PURPOSE
    from evowiki.store.db import fetch_all
    from evowiki.store.trace import run
    row = run(conn, run_id)
    if row is None:
        raise ReplaySourceMissing(NO_TRACE_RUN)
    if row["agent"] != PURPOSE or run_id.startswith(REPLAY_RUN_PREFIX):
        raise ReplaySourceMissing(NOT_A_TURN)
    if row["text_expired_at"] is not None:          # store.retention emptied its texts
        raise ReplaySourceMissing(TRACE_EXPIRED)
    _current_access(conn, row)
    enc = encode_assistant_case(conn, run_id)
    rows = fetch_all(conn, "select c.provider, c.model from agent_step s join llm_call c "
                           "on c.id = s.call_id where s.run_id = %s and s.kind = 'model' "
                           "order by s.seq", (run_id,))
    models = tuple(dict.fromkeys(ModelRef(provider=r["provider"], model=r["model"])
                                 for r in rows))
    from evowiki.store.trace import steps
    lists = tuple(tuple(ToolDef.model_validate(t) for t in s.get("tools") or ())
                  for s in steps(conn, run_id) if s["kind"] == "model")
    return TracedTurn(run_id=run_id, user_id=row["user_id"], pack_version=int(row["pack_version"]),
                      request=ChatRequest.model_validate(enc["request"]),
                      cassette=tuple(enc["cassette"]), models=models, tool_lists=lists,
                      page_id=(row.get("input") or {}).get("page_id"),
                      recorded_at=recorded_at(conn, row["user_id"], "agent_run", run_id),
                      sources=trace_source_closure(conn, row['user_id'], run_id, row.get('input')))


def pg_deps(pool: Any, gateway: Gateway, *, trace: TraceSink | None = None) -> AssistantReplayDeps:
    """Deps reading traces and pack history from Postgres (one connection per read)."""
    from evowiki.store.harness import pack_at

    def turn(run_id: str) -> TracedTurn:
        with pool.connection() as conn:
            return load_turn(conn, run_id)

    def at(user_id: str, version: int) -> HarnessPack:
        with pool.connection() as conn:
            return pack_at(conn, user_id, version)

    def validate(run_id: str) -> None:
        from evowiki.store.trace import run
        with pool.connection() as conn:
            row = run(conn, run_id)
            if row is None:
                raise ReplaySourceMissing(NO_TRACE_RUN)
            _current_access(conn, row)

    return AssistantReplayDeps(gateway=gateway, turn=turn, pack_at=at, trace=trace,
                               validate=validate)


def builtin_specs() -> dict[str, ToolSpec]:
    """The built-in assistant tools' specs (their default texts), by name. The tool classes take
    their deps at construction and read them only when run, so none is given here: nothing here
    can read or write anything (and no tool built here is ever run)."""
    from evowiki.assistant.tools import build_tools
    from evowiki.assistant.tools.actions import build_action_tools
    from evowiki.assistant.tools.read_more import ReadDeps, build_read_tools
    inert = object()          # stands in for the search provider / fetcher: never called
    more = [*build_read_tools(ReadDeps(None, None)),  # type: ignore[arg-type]
            *build_action_tools(None)]  # type: ignore[arg-type]
    tools = build_tools(pages=None, kview=None, search=inert, fetcher=inert,  # type: ignore[arg-type]
                        more=more, sources=inert)  # type: ignore[arg-type]
    # stage 3: the Skill / confirmation tools every action turn adds (assistant.turn_skills)
    from evowiki.assistant.turn_skills import inert_tools
    return {**{n: t.spec for n, t in tools.items()}, **{t.spec.name: t.spec for t in inert_tools()}}


class _Spec:
    """A tool as `apply.toolset` / `apply.describe` see it: a spec only (never run)."""

    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec

    def run(self, args: dict[str, Any], ctx: ToolContext) -> Any:  # pragma: no cover
        raise RuntimeError("a replay never runs a tool")


def system_of(traced: ChatRequest, recorded: HarnessPack, pack: HarnessPack) -> tuple[str, str]:
    """(system, prompt_version) of the replayed request: SYSTEM rendered with `recorded`'s
    guidance must head the traced system (else ReplaySourceMissing); it is replaced by SYSTEM
    rendered with `pack`'s guidance, and USER_VERSION follows whether that applied an override."""
    from evowiki.assistant.loop import SYSTEM
    old, _ = apply_marks(SYSTEM, recorded.guidance())
    if not traced.system.startswith(old):
        raise ReplaySourceMissing(SYSTEM_MISMATCH)
    new, applied = apply_marks(SYSTEM, pack.guidance())
    version = traced.prompt_version.removesuffix(USER_VERSION) + (USER_VERSION if applied else "")
    return new + traced.system[len(old):], version


def _h2(pack: HarnessPack, tool: str) -> dict[str, Any]:
    return {n: v for n, v in pack.with_prefix(f"H2.assistant.{tool}").items()
            if n == f"H2.assistant.{tool}" or n.startswith(f"H2.assistant.{tool}.")}


def tool_specs(traced: ChatRequest, recorded: HarnessPack, pack: HarnessPack) -> list[ToolSpec]:
    """Recorded tool definitions, minus disabled tools, with explicit candidate H2 edits."""
    builtin = builtin_specs()
    base: list[Tool] = []
    for d in traced.tools:
        old, new = _h2(recorded, d.name), _h2(pack, d.name)
        # The trace stores the rendered spec, not the pre-override default. Removing a
        # historical override cannot safely reconstruct that default from today's code.
        if any(key not in new for key in old):
            raise ReplaySourceMissing(H2_UNKNOWN)
        current = builtin.get(d.name)
        spec = ToolSpec(name=d.name, description=d.description, parameters=d.parameters,
                        effect=current.effect if current is not None else "read")
        base.append(_Spec(spec))       # type: ignore[arg-type]
    offered = apply.describe(pack, "assistant", apply.toolset(pack, "assistant", base))
    return [t.spec for t in offered]


def _summary(proposal: ProposalDraft | None, action: ActionCard | None) -> str | None:
    if proposal is not None:
        return f"【模拟修改建议】{proposal.heading}（{proposal.why}）\n{proposal.text}"
    if action is not None:
        items = "、".join(i.title for i in action.items)
        return f"【模拟动作】{action.title}" + (f"：{items}" if items else "")
    return None


def replay_account(gw: Gateway, turn: TracedTurn) -> Account | None:
    """The account the replay's calls go on (module doc, "The model"): group "rsi" with the
    first tier pin under which `gw` routes `assistant.turn` to the traced turn's model (None on
    an unmetered gateway whose model is that one). ReplaySourceMissing when the turn's model is
    unknown or mixed, or when no pin reaches it."""
    from evowiki.assistant.loop import PURPOSE
    if not turn.models:
        raise ReplaySourceMissing(MODEL_UNKNOWN)
    if len(turn.models) > 1:
        raise ReplaySourceMissing(MODEL_MIXED)
    (want,) = turn.models
    accounts: list[Account | None] = (
        [Account(user_id=turn.user_id, group=GROUP, tier_pin=p) for p in PINS]  # type: ignore[arg-type]
        if gw.metered else [None])
    routed = [gw.route_ref("generator", a, PURPOSE) for a in accounts]
    for acct, got in zip(accounts, routed, strict=True):
        if (got.provider, got.model) == (want.provider, want.model):
            return acct
    log.info("assistant replay of %s: the turn ran on %s:%s, the replay gateway routes to %s",
             turn.run_id, want.provider, want.model,
             sorted({f"{r.provider}:{r.model}" for r in routed}))
    raise ReplaySourceMissing(MODEL_MISMATCH)


def check_candidate(candidate: PackDiff) -> None:
    """HarnessInvalid when the candidate changes an assistant key (or H6) a replay cannot rebuild
    (rule R1): such a change is never replayed as if it had been measured."""
    for op in candidate.ops:
        if (why := unmeasured(op)) is not None:
            raise HarnessInvalid(why)


def _traced(case: Case, deps: AssistantReplayDeps) -> TracedTurn:
    from evowiki.assistant.loop import PURPOSE
    traced = deps.turn(case.run_id)
    if traced.user_id != case.user_id:
        raise ReplaySourceMissing(OTHER_USER)
    if traced.request.purpose != PURPOSE:
        raise ReplaySourceMissing(NOT_A_TURN)
    if deps.cutoff is not None and not SnapshotBoundary(deps.cutoff).allows(
            traced.recorded_at, traced.sources):
        raise ReplaySourceMissing("历史快照晚于评估截止时间或缺少记录时间")
    if traced.sources & deps.excluded_sources:
        raise ReplaySourceMissing("历史快照包含留出来源，不能提供给改进者")
    return traced


def check_turn(case: Case, *, base_pack: HarnessPack, deps: AssistantReplayDeps) -> None:
    """The checks `replay_turn(case, PackDiff(), base_pack=base_pack, deps=deps)` makes before its
    first model call, without making one: the traced turn is there and is the user's, the
    recorded pack's SYSTEM heads its system, its tools can be rebuilt, and the replay gateway
    routes to the turn's model. Raises what `replay_turn` raises for those (ReplaySourceMissing,
    `agent.trace.NotReplayable`, LookupError); reads only (the trace and the pack history)."""
    traced = _traced(case, deps)
    recorded = deps.pack_at(traced.user_id, traced.pack_version)
    system_of(traced.request, recorded, base_pack)
    tool_specs(traced.request, recorded, base_pack)
    replay_account(deps.gateway.with_harness(lambda _uid: base_pack), traced)


def replay_turn(case: Case, candidate: PackDiff, *, base_pack: HarnessPack,
                deps: AssistantReplayDeps) -> ReplayResult:
    """The traced turn `case.run_id` answered again under `base_pack` + `candidate` (module doc).
    Raises ReplaySourceMissing (a LookupError) for a turn that cannot be rebuilt,
    `agent.trace.NotReplayable` for one that sent attachments, HarnessInvalid for a candidate that
    does not apply or is not measurable, and ReplayFailed (with the spend and the run) when the
    replayed turn fails after a model call was made."""
    from evowiki.assistant.loop import PURPOSE, SEGMENT_SEP, turn_elide, turn_limits
    check_candidate(candidate)
    pack = apply_diff(base_pack, candidate)
    traced = _traced(case, deps)
    recorded = deps.pack_at(traced.user_id, traced.pack_version)
    system, version = system_of(traced.request, recorded, pack)
    # each traced request's own tools (a Skill turn's list changes mid-turn), rebuilt
    lists = traced.tool_lists or (traced.request.tools,)
    per_call = [tool_specs(traced.request.model_copy(update={"tools": ts}), recorded, pack)
                for ts in lists]
    every = {sp.name: sp for specs in per_call for sp in specs}
    union = traced.request.model_copy(update={"tools": tuple(_def(sp) for sp in every.values())})
    tools = {t.spec.name: t for t in cassette_tools(union, traced.cassette)}
    notes: list[str] = []

    actions: list[str] = []

    def called(name: str) -> None:
        if name not in actions:
            actions.append(name)
            notes.append(SIMULATED.format(name=name))
    offered: list[Tool] = []
    for sp in every.values():
        ct = tools[sp.name]
        ct.spec = sp                               # the rebuilt texts; answers stay the cassette's
        offered.append(_ReplayTool(ct, called))
    defs_of = [tuple(_def(sp) for sp in specs) for specs in per_call]
    shown: list[frozenset[str]] = [frozenset()]    # what the latest request offered (the gate)
    limits = turn_limits(pack)
    elide = turn_elide(pack, offered)
    params = traced.request.params

    replay_id = deps.new_id("hr")
    run_id = f"{REPLAY_RUN_PREFIX}{replay_id}:{case.run_id}"
    gw = deps.gateway.with_harness(lambda _uid: pack).salted(replay_id)
    account = replay_account(gw, traced)          # before any call: the traced turn's model
    (want,) = traced.models
    calls: list[LLMCall] = []
    parts: list[str] = []
    seen_actions: set[str] = set()

    def request(state: RequestState) -> ChatRequest:
        defs = defs_of[min(state.turn, len(defs_of)) - 1]
        shown[0] = frozenset(d.name for d in defs)
        return traced.request.model_copy(update={
            "system": system, "prompt_version": version, "tools": defs,
            "messages": state.messages, "tool_choice": state.tool_choice, "params": params})

    def converse(req: ChatRequest) -> tuple[ChatTurn, LLMCall]:
        if deps.validate is not None:
            deps.validate(case.run_id)
        turn, c = gw.converse("generator", req, account=account)
        calls.append(c)
        if turn.text.strip():
            parts.append(turn.text)
        return turn, c

    trace = deps.trace
    spec = AgentSpec(name=PURPOSE, tools=offered, limits=limits, request=request, elide=elide,
                     fence_reads=True, allowed=lambda name: name in shown[0],
                     sent=lambda r: gw.as_sent(r, account, pack.guidance()),
                     **({"trace": trace, "run_id": run_id} if trace is not None else {}))
    ctx = ToolContext(user_id=case.user_id, session_id=traced.request.subject, page_id=traced.page_id)
    if trace is not None:
        trace.start(run_id, user_id=case.user_id, agent=PURPOSE, subject=traced.request.subject,
                    pack_version=pack.version,
                    input={"source_run": case.run_id,
                           "sources": sorted(traced.sources),
                           "snapshot": {"recorded_at": (traced.recorded_at.isoformat()
                                        if traced.recorded_at is not None else None),
                                        "pack_version": traced.pack_version},
                           "job": {"trigger": REPLAY_TRIGGER, "job_id": replay_id,
                                   "candidate": candidate_ref(candidate)}})
    try:
        gen = run_loop(spec, converse, ctx, traced.request.messages)
        while True:
            try:
                ev = next(gen)
            except StopIteration as stop:
                result = stop.value
                break
            if isinstance(ev, ToolFinished) and ev.result.error == "snapshot_unavailable":
                raise ReplaySourceMissing(SNAPSHOT_UNAVAILABLE)
            if isinstance(ev, Paused):
                notes.append(f"【问他】{ev.pause.payload.get('question', '')}")
            if isinstance(ev, ToolFinished) and ev.result.error is None:
                act = ev.result.action
                if act is not None and act.action_id in seen_actions:
                    act = None
                if act is not None:
                    seen_actions.add(act.action_id)
                if (line := _summary(ev.result.proposal, act)) is not None:
                    notes.append(line)
        if deps.validate is not None:
            deps.validate(case.run_id)
    except Exception as exc:
        if trace is not None:
            finish_failed(trace, run_id, exc)
        if not calls:
            raise
        code = "snapshot_unavailable" if isinstance(exc, ReplaySourceMissing) else classify(exc).code
        raise ReplayFailed(code, str(exc)[:300], cost=_cost(calls),
                           run_ids=(run_id,) if trace is not None else ()) from exc
    if trace is not None:
        trace.finish(run_id, stop=result.stop, error=None)
    other = [c for c in calls if (c.provider, c.model) != (want.provider, want.model)]
    if other:
        log.info("assistant replay %s: %d call(s) answered by %s, not %s:%s", run_id, len(other),
                 sorted({f"{c.provider}:{c.model}" for c in other}), want.provider, want.model)
        raise ReplayFailed("model_mismatch", MODEL_MISMATCH, cost=_cost(calls),
                           run_ids=(run_id,) if trace is not None else ())
    answer = SEGMENT_SEP.join(parts) or result.text
    text = SEGMENT_SEP.join(p for p in (answer, *notes) if p)
    return ReplayResult(case_id=case.case_id, text=text, writer_provider=calls[-1].provider,
                        run_id=run_id, cost=_cost(calls), actions=tuple(actions))


def _cost(calls: Sequence[LLMCall]) -> float:
    """What the calls were charged (USD); an unpriced call (cost None) counts 0."""
    return round(sum(c.cost or 0.0 for c in calls), 6)
