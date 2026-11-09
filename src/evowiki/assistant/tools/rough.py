"""ask_rough: the diagnosis's first step in the chat, the 摸底 grid (stage 3 phase 2, the approved
diagnosis UI): the learner marks each block of his goal 掌握 / 熟悉 / 不会 in one card, before
any single question (next_check / ask_check).

- `ask_rough(draft_id?)` (direct, pausing): the open opening diagnosis (the newest one when no id
  is given) and its blocks to mark — the goal map's top-level blocks the goal record diagnoses
  (`diagnose`), K does not know yet and no earlier grid marked (`wikiapp.goal.rough_blocks`).
  None (a region's diagnosis, a grid already answered): no pause, the result is the next question
  to write (`check.next_line`); no open diagnosis at all (a stated beginner's goal never has one):
  an error, never a silent pass. Else the turn ends on the card, kind "ask_rough",
  payload `RoughPayload {draft_id, blocks: [{id, name}]}`.
- His next message resumes it. The marks come in the message's `reply_data` (`MessageIn`:
  `{marks: {block_id: "know" | "partly" | "dont"}}`, put into `Reply.data` by the turn only when it
  resumes this card); the text is only his readable bubble and is never parsed. An unmarked block
  is skipped. Each marked block is one self-report on its cells through the diagnosis's own path
  (`wikiapp.goal.record_rough`: the self-report table, know → can_do, partly → partly, dont →
  never; idempotent per call id); a block marked dont is then taught as it is (next_check never
  asks about it or its points).

A core contract (RSI constraint 1): no Skill text, tool description or harness key moves the
blocks, the claims or what is recorded."""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evowiki.agent.tool_helpers import fail, run_guarded, spec
from evowiki.agent.tools import Pause, Reply, ToolContext, ToolResult
from evowiki.assistant.tools.check import NO_PICK, next_line, open_pick, start_line
from evowiki.llm.models import LLMRequest
from evowiki.wikiapp.llm import LLM_BUDGET, LLM_FAILURES, LLM_UNAVAILABLE, LLMUnavailable
from evowiki.wikiapp.repo import Repo, WikiConflict, WikiInvalid

log = logging.getLogger(__name__)
NAME = "ask_rough"
PAUSE_KIND = "ask_rough"
NO_GRID = "这次不用先填摸底表（没有要他自己标的块）。"
NO_MARKS = ("他没有在摸底表里标任何一块，什么也没记。他不想摸底就调 skip_diagnosis（draft_id：{draft}）；"
            "他在说别的事就先回答他。其余情况接着下面这题。")
SKIP_NAME = "skip_diagnosis"
TYPED_CALL = "typed:"       # a typed answer's call id on the draft: the grid stays open for the rest (wikiapp.goal.rough_blocks)
GRID_AGAIN = "他话里有问题就先在回答里写出答案；然后调 ask_rough 把摸底表再给他（还有 {n} 块他没说），不要一块块问。"
TYPED_PURPOSE = "goals.rough_typed"
TYPED_VERSION = "rough_typed@v3"
NO_DIAGNOSIS = f"{NO_PICK}：这个目标不用摸底，或者摸底已经结束了"
Mark = Literal["know", "partly", "dont"]
MARK_ZH: dict[str, str] = {"know": "掌握", "partly": "熟悉", "forgot": "学过但忘了", "dont": "不会"}


class RoughArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str | None = Field(default=None, max_length=200,
                                 description="摸底草稿 id；省略=他现在正在进行的摸底")


class RoughBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str


class RoughPayload(BaseModel):
    """`Pause.payload` of kind "ask_rough": the 摸底 card (the blocks he marks)."""

    model_config = ConfigDict(frozen=True)

    draft_id: str
    blocks: tuple[RoughBlock, ...]


class RoughReply(BaseModel):
    """`Reply.data` of an ask_rough card (`MessageIn.reply_data`): block id → his mark."""

    model_config = ConfigDict(extra="forbid")

    marks: dict[str, Mark] = Field(default_factory=dict, max_length=60)


class SkipArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(max_length=200, description="要结束的摸底草稿 id，工具结果或上下文里给的那个")
    start_from: str | None = Field(default=None, max_length=120,
                                   description="他说了从哪一块开始时：那一块的名字，照分块上的写")


class TypedMark(BaseModel):
    model_config = ConfigDict(extra="ignore")

    n: int
    mark: Literal["know", "partly", "forgot", "dont"]


class TypedMarks(BaseModel):
    model_config = ConfigDict(extra="ignore")

    marks: list[TypedMark] = Field(default_factory=list, max_length=60)
    stop: bool = False


def typed_marks(gw: Any, user_id: str, said: str, blocks: list[tuple[str, str]]) -> dict[str, str]:
    return read_typed(gw, user_id, said, blocks)[0]


def read_typed(gw: Any, user_id: str, said: str, blocks: list[tuple[str, str]]) -> tuple[dict[str, str], bool]:
    """What he typed instead of marking the grid, as the grid's marks (the same slots, filled by one constrained call):
    only what he stated, and whether he said to stop asking. A model failure is no marks: the caller says nothing was recorded."""
    listing = "\n".join(f"{i}. {name}" for i, (_, name) in enumerate(blocks, 1))
    request = LLMRequest(
        purpose=TYPED_PURPOSE, prompt_version=TYPED_VERSION, subject=f"user:{user_id}",
        system="你把学习者的一句话对应到学习主题上。只回复 JSON。",
        prompt=f"学习主题：\n{listing}\n\n他说：「{said}」\n\n把他明确说到的主题记下，用序号。mark：know=会、掌握；partly=熟悉、大概知道、"
               "做过一些；forgot=学过但忘了；dont=没学过、不会。他说到一个主题里的一部分，按这部分记这个主题，同一主题说法不同时记更低的。"
               "「其他都没碰过」这样的话算剩下的每一个主题。他没说自己会不会（比如提问、说不确定、说不用摸底、说从哪里开始）就给空列表。"
               "他说不问了、开始吧、直接学，又没说从哪一块开始：stop 写 true。"
               '只回复 JSON：{"marks":[{"n":1,"mark":"dont"}],"stop":false}')
    try:
        reply, _ = gw.call("generator", request, TypedMarks)
    except (ValueError, LLMUnavailable, *LLM_UNAVAILABLE, *LLM_FAILURES, *LLM_BUDGET) as exc:
        log.warning("typed diagnosis marks not read: %s", exc)
        return {}, False
    return {blocks[m.n - 1][0]: m.mark for m in reply.marks if 1 <= m.n <= len(blocks)}, reply.stop


def record_typed(repo: Repo, gateway: Callable[[str], Any], clock: Callable[[], datetime], new_id: Callable[[str], str],
                 ctx: ToolContext, draft_id: str, said: str, *, on_grid: bool = False) -> ToolResult | None:
    """His typed answer recorded through the grid's path (`record_rough`), and what comes next; None when it stated no
    marks or this diagnosis has no grid (a block's own diagnosis). `on_grid`: typed over the grid: what is left goes back
    on a grid, not one question a block. 「不问了」 ends the diagnosis on what is known."""
    from evowiki.wikiapp.goal import known_before, record_rough, rough_blocks, stop_draft
    state = repo.load(ctx.user_id).state
    draft = state.drafts.get(draft_id)
    m = state.maps.get(draft.map_id) if draft is not None and draft.map_id else None
    if m is None or draft.region_id is not None:
        return None
    answered = {*draft.rough, *(r.pick.node for r in draft.rounds if r.pick is not None and r.answers is not None)}
    # 「其他都没碰过」 is about what he has not answered yet: what he already said stays as he said it
    kv = repo.load(ctx.user_id).kview(clock())
    blocks = [(n.id, n.name) for n in m.children(None) if n.kind == "region" and n.diagnose and n.id not in answered
              and not known_before(kv, m, n)]
    if not blocks:
        return None
    gw = gateway(ctx.user_id)
    marks, stop = read_typed(gw, ctx.user_id, said, blocks)
    if not marks and not stop:
        return None
    shown = dict(blocks)
    try:
        res = None
        if marks:
            res = record_rough(repo, gw, ctx.user_id, draft_id, f"{TYPED_CALL}{ctx.call_id or ''}", marks, names=shown, now=clock(), new_id=new_id)
        if stop and (res is None or res.open):
            res = stop_draft(repo, gw, ctx.user_id, draft_id, now=clock(), new_id=new_id)
    except (WikiConflict, WikiInvalid, LookupError) as exc:
        return fail(f"摸底没有记下：{exc}")
    head = f"他打字说：「{said}」。"
    if marks:
        head += ("他打字说的已记下：" + "；".join(f"「{shown[b]}」{MARK_ZH[v]}" for b, v in marks.items())
                 + "。这几块不要再问。他话里还问了别的就先答，答完接着问下面这题。")
    if stop:
        return ToolResult(content=head + "摸底结束了，内容在页面里讲，这里不讲。" + start_line(repo, ctx.user_id, res.map_id, clock()), summary="摸底")
    if res.open and on_grid and (left := rough_blocks(repo.load(ctx.user_id), draft_id, clock())) is not None and left[1]:
        return ToolResult(content=head + GRID_AGAIN.format(n=len(left[1])), summary="摸底", data={"next_question_required": True})
    if res.open:
        return ToolResult(content=head + next_line(repo.load(ctx.user_id), draft_id), summary="摸底", data={"next_question_required": True})
    return ToolResult(content=head + "摸底结束。" + start_line(repo, ctx.user_id, res.map_id, clock()), summary="摸底")


class SkipDiagnosisTool:
    """「不用摸底了，直接开始」: the open diagnosis ends on the evidence so far (`stop_draft`, what the
    card's 跳过 does); the goal goes on from the block `start_line` names."""

    def __init__(self, repo: Repo, gateway: Callable[[str], Any], clock: Callable[[], datetime],
                 new_id: Callable[[str], str]):
        self._repo, self._gateway, self._clock, self._new_id = repo, gateway, clock, new_id
        self.spec = spec(SKIP_NAME, "他说不用摸底、直接开始时：结束这次摸底，已经有的证据都留着。"
                         "他说了从哪一块开始就写进 start_from，那一块排到最前面。", SkipArgs,
                         "propose", "action", acts=True, tier="direct")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(SkipArgs, args, lambda a: self._skip(a, ctx))

    def _skip(self, a: SkipArgs, ctx: ToolContext) -> ToolResult:
        from evowiki.wikiapp.goal import stop_draft
        try:
            res = stop_draft(self._repo, self._gateway(ctx.user_id), ctx.user_id, a.draft_id,
                             now=self._clock(), new_id=self._new_id)
        except (WikiConflict, WikiInvalid, LookupError) as exc:
            return fail(f"摸底没有结束：{exc}")
        moved = ""
        if a.start_from and res.map_id:
            from evowiki.assistant.tools.path_blocks import put_first
            try:
                put_first(self._repo, ctx.user_id, res.map_id, a.start_from, self._clock(), self._new_id)
            except (WikiConflict, WikiInvalid, LookupError) as exc:
                moved = f"「{a.start_from}」没有排到最前面：{exc}。如实告诉他。"
        return ToolResult(content="摸底结束了，内容在页面里讲，这里不讲。" + moved
                          + start_line(self._repo, ctx.user_id, res.map_id, self._clock()), summary="摸底")


class AskRoughTool:
    def __init__(self, repo: Repo, gateway: Callable[[str], Any], clock: Callable[[], datetime],
                 new_id: Callable[[str], str]):
        self._repo, self._gateway, self._clock, self._new_id = repo, gateway, clock, new_id
        self.spec = spec(NAME, "摸底开始时先给他一张摸底表：他自己把每一块标成掌握 / 熟悉 / 不会，"
                         "界面显示，他标完自动记下。回答里不要再列这些块。", RoughArgs, "propose",
                         "action", acts=True, pauses=True, tier="direct")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(RoughArgs, args, lambda a: self._ask(a, ctx))

    def _ask(self, a: RoughArgs, ctx: ToolContext) -> ToolResult:
        from evowiki.wikiapp.goal import rough_blocks
        snap = self._repo.load(ctx.user_id)
        found = rough_blocks(snap, a.draft_id, self._clock())
        if found is None:
            return fail(NO_DIAGNOSIS)
        draft_id, blocks = found
        if not blocks:
            return ToolResult(content=NO_GRID + next_line(snap, draft_id),
                              data={"next_question_required": open_pick(snap, draft_id) is not None})
        payload = RoughPayload(draft_id=draft_id,
                               blocks=tuple(RoughBlock(id=i, name=n) for i, n in blocks))
        return ToolResult(content="已经把摸底表给他了，正在等他标。",
                          summary="、".join(n for _, n in blocks)[:200],
                          pause=Pause.of(PAUSE_KIND, payload))

    def resume(self, args: dict[str, Any], ctx: ToolContext, pause: Pause,
               reply: Reply) -> ToolResult:
        payload = RoughPayload.model_validate(pause.payload)
        try:
            marks = RoughReply.model_validate(reply.data or {}).marks
        except ValidationError:
            return fail("摸底表的标记不对，什么也没记")
        shown = {b.id: b.name for b in payload.blocks}
        marks = {b: m for b, m in marks.items() if b in shown}      # only the blocks it showed
        if not marks:
            said = reply.text.strip()[:200]
            if said and (typed := record_typed(self._repo, self._gateway, self._clock, self._new_id, ctx, payload.draft_id, said, on_grid=True)) is not None:
                return typed
            head = f"他说的是：「{said}」。" if said else ""
            return ToolResult(content=NO_MARKS.format(draft=payload.draft_id) + head
                              + next_line(self._repo.load(ctx.user_id), payload.draft_id), summary="摸底",
                              data={"next_question_required": open_pick(self._repo.load(ctx.user_id), payload.draft_id) is not None})
        from evowiki.wikiapp.goal import record_rough
        try:
            res = record_rough(self._repo, self._gateway(ctx.user_id), ctx.user_id,
                               payload.draft_id, ctx.call_id or "", marks,
                               names=shown, now=self._clock(), new_id=self._new_id)
        except (WikiConflict, WikiInvalid, LookupError) as exc:
            return fail(f"摸底没有记下：{exc}")
        said = "；".join(f"「{shown[b]}」{MARK_ZH[m]}" for b, m in marks.items())
        head = f"他标了：{said}。已记下。"
        if res.open:
            return ToolResult(content=head + next_line(self._repo.load(ctx.user_id),
                                                       payload.draft_id), summary="摸底",
                              data={"next_question_required": True})
        where = ("学习路径排好了" if res.path_id else "页面建好了" if res.page_id
                 else "目标的分块图准备好了" if res.map_id else "摸底结束了")
        return ToolResult(content=head + f"摸底结束，{where}。" + start_line(self._repo, ctx.user_id, res.map_id, self._clock()),
                          summary="摸底")
