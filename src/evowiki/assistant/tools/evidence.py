"""record_evidence (stage 3 T9, design B6.1 / Q8): the assistant records, in its own turn, a bounded
objective learning event this message shows — a question on a cell, 看不懂 on a section, a
self-report — instead of a separate observer call after the answer.

What code enforces (a core contract: no Skill text, tool description or harness key changes it):
- the cell is a K cell of this turn's scope (the session page's concepts, else the note's concept);
- `quote` is a verbatim piece of THIS message (≥ 2 characters after whitespace is removed);
- at most `MAX_EVENTS` events per message, one per (cell, kind);
- the strength comes from code: ask → `ASK_DEFAULT_STRENGTH`; feedback → the section feedback
  weights (`goals.page_update.FEEDBACK_STRENGTH`); self → the self-report table (`goals.selfreport`);
- there is no free-text field: no judgment about the person can be written;
- K is only appended to (`Observe` + `AddNote`, label built from the quote); the band is K's
  aggregate, never set here.

`evidence_ref = assistant:{session}:{message}#{i}`. A failed write is a tool error, never 「记下了」.
The result carries what changed (`ToolResult.observed`): the loop logs one `observed` entry per
turn and streams the `observed` event (the plugin's cell-change line)."""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evowiki.agent.tool_helpers import fail, run_guarded, spec
from evowiki.agent.tools import ToolContext, ToolResult
from evowiki.assistant.tools.grounding import current_words
from evowiki.belief.aspects import AspectK, AspectKView, Observe
from evowiki.goals.contracts import Observation, split_cell
from evowiki.goals.observe import ASK_DEFAULT_STRENGTH
from evowiki.goals.page_update import FEEDBACK_STRENGTH
from evowiki.goals.selfreport import CLAIM_ZH, SelfClaim, self_observation
from evowiki.teach.textsim import squash
from evowiki.wikiapp.kwrite import observation_events
from evowiki.wikiapp.repo import Commit, Repo, WikiConflict

NAME = "record_evidence"
MAX_EVENTS = 3
MIN_QUOTE = 2
COMMIT_TRIES = 3
REF_PREFIX = "assistant:"
# feedback polarity -> strength (the section feedback weights: 懂了 +0.6, 看不懂 / 再讲深 -0.4)
FEEDBACK_BY_POLARITY = {"+": FEEDBACK_STRENGTH["know"][1], "-": FEEDBACK_STRENGTH["deeper"][1],
                        "~": FEEDBACK_STRENGTH["deeper"][1]}
KIND_ZH = {"ask": "问", "feedback": "说", "self": "说"}
DESCRIPTION = ("把他这条消息里一件客观的学习事件记进 K：在某个格子上提了问题、说某段看不懂、"
               "说自己会或没学过这一页的某个方面。只记他说了什么，不记你对他的判断；看不出是哪个格子就不记。"
               "没开着页面、或他说的是图谱或目标里的一个点早会了：不用这个，用 assess 的 mark_known。")


class EvidenceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cell: str = Field(min_length=3, max_length=120,
                      description="K 的格子 id（知识点.方面），只能是本页知识点的格子")
    kind: Literal["ask", "feedback", "self"] = Field(
        description="ask：他问的问题说明这一方面还没懂或分不清；feedback：他对页面这一段的反应"
                    "（看不懂、懂了）；self：他说自己会不会")
    polarity: Literal["+", "-", "~"] | None = Field(
        default=None, description="ask / feedback 必填：+ 懂了，- 没懂，~ 分不太清；self 不填")
    self_claim: SelfClaim | None = Field(
        default=None, description="self 必填：studied 学过（包括学过但忘了、记不清），partly 熟悉，can_do 会用，"
                                  "fluent 熟练，never 没学过")
    quote: str = Field(min_length=1, max_length=200,
                       description="这件事在他这条消息里的原话，一字不改")

    @model_validator(mode="after")
    def _shape(self) -> "EvidenceEvent":
        if self.kind == "self":
            if self.self_claim is None:
                raise ValueError("self 要给 self_claim")
            if self.polarity is not None:
                raise ValueError("self 不填 polarity（由 self_claim 决定）")
        else:
            if self.polarity is None:
                raise ValueError(f"{self.kind} 要给 polarity")
            if self.self_claim is not None:
                raise ValueError("只有 self 才给 self_claim")
        return self


class RecordEvidenceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[EvidenceEvent] = Field(min_length=1, max_length=MAX_EVENTS,
                                        description=f"1 到 {MAX_EVENTS} 件，每个格子每种只记一次")


def turn_ref(ctx: ToolContext) -> str:
    return f"{REF_PREFIX}{ctx.session_id}:{ctx.message_id}#"


def scope_cells(snap: Any, ctx: ToolContext) -> tuple[str, ...]:
    """The K cells this turn may record on: the session page's concepts, else the note's
    concept (cells K defines, in order)."""
    state, k = snap.state, snap.k
    rec = state.pages.get(ctx.page_id) if ctx.page_id else None
    concepts = list(rec.concepts) if rec is not None else (
        [ctx.note_concept_id] if ctx.note_concept_id else [])
    defined = set(k.concept_ids())
    return tuple(f"{c}.{a.value}" for c in dict.fromkeys(concepts) if c in defined
                 for a in k.aspects(c))


def _bands(k: AspectK, as_of: datetime) -> dict[str, str]:
    view = AspectKView(k, as_of)
    return {c: b.band.value for c, b in view.cells(k.concept_ids()).items()}


def _observation(e: EvidenceEvent, ref: str) -> Observation:
    quote = " ".join(e.quote.split())
    if e.kind == "self":
        assert e.self_claim is not None
        return self_observation(e.self_claim, e.cell, quote=quote, evidence_ref=ref)
    assert e.polarity is not None
    strength = ASK_DEFAULT_STRENGTH if e.kind == "ask" else FEEDBACK_BY_POLARITY[e.polarity]
    return Observation(cell=e.cell, polarity=e.polarity, strength=strength, kind=e.kind,
                       evidence_ref=ref, quote=quote)


def _label(e: EvidenceEvent) -> str:
    """The note label, built by code from the quote (no model text)."""
    q = " ".join(e.quote.split())
    if e.kind == "self":
        assert e.self_claim is not None
        return f"你说「{q}」（{CLAIM_ZH[e.self_claim]}）"
    return f"你{KIND_ZH[e.kind]}「{q}」"


class RecordEvidenceTool:
    def __init__(self, repo: Repo, clock: Callable[[], datetime],
                 new_id: Callable[[str], str]):
        self._repo, self._clock, self._new_id = repo, clock, new_id
        self.spec = spec(NAME, DESCRIPTION, RecordEvidenceArgs, "read", "action", tier="observe")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(RecordEvidenceArgs, args, lambda a: self._run(a.events, ctx))

    def _refused(self, events: Sequence[EvidenceEvent], ctx: ToolContext, scope: set[str],
                 earlier: Sequence[Observation]) -> str | None:
        words = current_words(ctx)
        if len(earlier) + len(events) > MAX_EVENTS:
            return f"这条消息最多记 {MAX_EVENTS} 件，已经记了 {len(earlier)} 件"
        seen = {(o.cell, o.kind) for o in earlier}
        for e in events:
            try:
                split_cell(e.cell)
            except ValueError:
                return f"没有这个格子：{e.cell}。他说一个点早会了，用 assess 的 mark_known（点的名字即可）"
            if e.cell not in scope:
                return f"「{e.cell}」不在这一页的知识点里，不能记。他说一个点早会了，用 assess 的 mark_known（点的名字即可）"
            q = squash(e.quote)
            if len(q) < MIN_QUOTE or not any(q in squash(w) for w in words):
                return f"「{e.quote}」不是他这条消息里的原话"
            if (e.cell, e.kind) in seen:
                return f"「{e.cell}」的这种事件这条消息里已经记过了"
            seen.add((e.cell, e.kind))
        return None

    def _run(self, events: Sequence[EvidenceEvent], ctx: ToolContext) -> ToolResult:
        if not ctx.message_id:
            return fail("这一轮没有消息 id，记不了")
        prefix = turn_ref(ctx)
        for attempt in range(1, COMMIT_TRIES + 1):
            snap = self._repo.load(ctx.user_id)
            earlier = [e.observation for e in self._repo.aspect_events(ctx.user_id)
                       if isinstance(e, Observe) and e.observation.evidence_ref.startswith(prefix)]
            why = self._refused(events, ctx, set(scope_cells(snap, ctx)), earlier)
            if why is not None:
                return fail(why)
            obs = [_observation(e, f"{prefix}{len(earlier) + i}") for i, e in enumerate(events)]
            labels = {o.evidence_ref: _label(e) for o, e in zip(obs, events, strict=True)}
            now = self._clock()
            aspect = observation_events(obs, ts=now, new_id=self._new_id,
                                        note_text=lambda o: o.quote or "",
                                        note_label=lambda o, _l=labels: _l[o.evidence_ref])
            after = snap.k.copy()
            for ev in aspect:
                after.apply(ev)
            try:
                self._repo.commit(snap, Commit(aspect=aspect))
                break
            except WikiConflict:
                if attempt == COMMIT_TRIES:
                    return fail("K 正在被别的操作改动，这次没有记下")
        b0, b1 = _bands(snap.k, now), _bands(after, now)
        changes = [{"cell": c, "from_band": b0[c], "to_band": b1[c]}
                   for c in sorted(b0) if b0[c] != b1.get(c, b0[c])]
        return ToolResult(content=f"已记下 {len(obs)} 件（{'、'.join(o.cell for o in obs)}）。这只是记录，不算回答他。",
                          observed={"observations": len(obs), "changes": changes})
