"""The diagnosis in the chat (stage 3 T11, design B6.1): code picks what to ask, the assistant writes
the question, his answer is recorded on the cell code picked.

- The open pick of a diagnosis draft (`next_line`, in ask_rough's and ask_check's results): the node, the item kind and
  the cell (`goals.probe.CheckPick`, KST, no model call). The pick is the draft's open round, stored with the draft, so a
  replay finds the same one. No tool lists it on its own: a diagnosis comes back only through ask_rough on its draft.
- `ask_check(pick_id, quiz | self_report)` (direct, pausing): the assistant's typed question on that
  pick, checked in the tool step on the PICK's cell (`assistant.tools.items`: gradable, plain
  text; a self-report: options with aligned meanings), then asked as the chat's ask card (the
  options, plus 不知道 for a quiz; never the key). The turn ends on the question (agent.loop); his
  next chat message resumes it (an option clicked is sent as his message: its text, 不知道 = IDK)
  and is recorded through `wikiapp.goal.answer_check` (the pick's cell, or the cells a
  self-report covers, typed `self` evidence; the round log; the next pick or the end). Free text
  that is no option records nothing: the pick stays open.
  Its grounds are its own (`grounding.SELF_CHECKING`): a pick the draft still has open.

Both are core contracts (RSI constraint 1): no Skill text, tool description or harness key moves
the cell, the strength or what is recorded."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evowiki.agent.tool_helpers import fail, run_guarded, spec
from evowiki.agent.tools import Pause, Reply, ToolContext, ToolResult
from evowiki.assistant.tools.items import ItemArg, ItemRefused, checked_item
from evowiki.goals.contracts import split_cell
from evowiki.goals.diagnostic import IDK, latex_in
from evowiki.goals.guardrails import ASPECT_ZH
from evowiki.goals.probe import CheckPick, Means, ProbeQuestion, answered_question
from evowiki.teach.textsim import squash
from evowiki.wikiapp.repo import Repo, WikiConflict, WikiInvalid

ASK = "ask_check"
PAUSE_KIND = "ask_check"
IDK_TEXT = "不知道"
NO_PICK = "现在没有在进行的摸底"
NO_REPLY = "他这次没有回答，这题没有记。不要再问这一题，按已有的信息继续。"
INTENT_ZH = {"self_report": "自评（问他学没学过）", "goal_use": "小测（目标用到的这一点）",
             "mastery": "小测（只有真正学会这一块才答得对）"}


def start_line(repo: Repo, user_id: str, map_id: str | None, now: datetime) -> str:
    """The diagnosis's last tool result names where he starts: the step /wiki/progress gives this goal
    (`progress.goal_step`), so the chat line, 今天 and the row 学点新的 highlights say the same. No map, or nothing left: a plain line."""
    from evowiki.wikiapp.progress import goal_step
    snap = repo.load(user_id)
    m = snap.state.maps.get(map_id) if map_id else None
    if m is None:
        return "用一句话告诉他接下来从哪里开始。"
    step = goal_step(snap.state, snap.kview(now), m.map_id)
    if step.kind == "none":
        return "每一块都不用再学了：用一句话告诉他这个目标已经学完，想再看哪一块可以在左侧点开始学习。不要说接下来从哪一块开始。"
    name = step.title or step.region_name
    return f"接下来从「{name}」开始：用一句话告诉他从「{name}」开始，不要说页面已经在写。"


def _draft_of(pick_id: str) -> str:
    return pick_id.rsplit(":", 1)[0]


def open_pick(snap: Any, draft_id: str | None = None) -> CheckPick | None:
    """The open pick of `draft_id` (else of the newest open draft that has one), or None."""
    state = snap.state
    ids = [draft_id] if draft_id else list(reversed(list(state.drafts)))
    for d in ids:
        draft = state.drafts.get(d)
        if draft is None or d in state.closed_drafts or not draft.rounds:
            continue
        r = draft.rounds[-1]
        if not r.done and r.pick is not None and r.answers is None:
            return r.pick
    return None


def pick_text(pick: CheckPick) -> str:
    """What the assistant writes the question from."""
    lines = [f"pick_id：{pick.pick_id}", f"要问的：{INTENT_ZH[pick.intent]}",
             f"知识点 / 块：「{pick.name}」" + (f"（{pick.scope}）" if pick.scope else "")]
    if pick.members:
        lines.append("它包含：" + "、".join(f"{m} {n}" for m, n in pick.members))
    if pick.cell:
        _, aspect = split_cell(pick.cell)
        lines.append(f"测的方面：{ASPECT_ZH[aspect]}（格子 {pick.cell}）")
    return "\n".join(lines)


def next_line(snap: Any, draft_id: str | None = None) -> str:
    """The open pick as the model reads it: what a recorded answer (ask_rough, ask_check) says with the diagnosis still open."""
    pick = open_pick(snap, draft_id)
    if pick is None:
        return f"{NO_PICK}：没有要问的题了。"
    return "下一题：\n" + pick_text(pick) + "\n用 ask_check 问他（带上 pick_id），题不要写在回答里。"


Option = Annotated[str, Field(min_length=1, max_length=60)]


class QuizCheck(ItemArg):
    presupposes: list[str] = Field(default_factory=list, max_length=20,
                                   description="区域掌握题：答对它必须会的成员 id（可以为空）")


class SelfCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stem: str = Field(min_length=1, max_length=200, description="问他学没学过，一句平常的话")
    options: list[Option] = Field(min_length=2, max_length=6, description="从会到不会")
    means: list[Literal["know", "partly", "forgot", "dont", "unsure"]] = Field(
        min_length=2, max_length=6,
        description="和选项一一对应：know=会，partly=熟悉，还记得大概，forgot=学过但忘了、记不清，"
                    "dont=不会/没学过，unsure=说不清")


class AskCheckArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pick_id: str = Field(min_length=3, max_length=240, description="工具结果里「下一题」给的 pick_id")
    quiz: QuizCheck | None = Field(default=None, description="小测时：你写的单选题")
    self_report: SelfCheck | None = Field(default=None, description="自评时：你写的问题")

    @model_validator(mode="after")
    def _one(self) -> "AskCheckArgs":
        if (self.quiz is None) == (self.self_report is None):
            raise ValueError("quiz 和 self_report 只给一个")
        return self


class CheckPayload(BaseModel):
    """`Pause.payload` of kind "ask_check": the ask card's shape (`{question, options}`), plus the
    goal map's top-level block the pick's node belongs to (the node itself when it is one):
    `block_id` / `block_name` (the 细问 card names it; 学点新的 shows it as 正在问). None: no map
    (a flat goal) or the node is on no block. `draft_id`: the diagnosis it belongs to (the plugin's goal follows it)."""

    model_config = ConfigDict(frozen=True)

    question: str
    options: tuple[str, ...]
    draft_id: str | None = None
    block_id: str | None = None
    block_name: str | None = None


def block_of(snap: Any, pick: CheckPick) -> tuple[str, str] | None:
    """(id, name) of the top-level block on the pick's goal map that holds the pick's node: the
    node's own block chain upwards, else the block whose members (or their parts) include it."""
    state = snap.state
    draft = state.drafts.get(_draft_of(pick.pick_id))
    m = state.maps.get(draft.map_id) if draft is not None and draft.map_id else None
    if m is None:
        return None
    tops = [n for n in m.children(None) if n.kind == "region"]
    node, seen = m.node(pick.node), set()
    while node is not None and node.parent is not None and node.id not in seen:
        seen.add(node.id)
        node = m.node(node.parent)
    if node is not None and any(t.id == node.id for t in tops):
        return node.id, node.name
    from evowiki.goals.regions import covered
    hit = next((t for t in tops if pick.node in covered(m, t)), None)
    return (hit.id, hit.name) if hit is not None else None


class AskCheckTool:
    def __init__(self, repo: Repo, gateway: Callable[[str], Any], clock: Callable[[], datetime],
                 new_id: Callable[[str], str]):
        self._repo, self._gateway, self._clock, self._new_id = repo, gateway, clock, new_id
        self.spec = spec(ASK, "问他摸底的这一题：题由你按工具结果里「下一题」的说明写，界面把选项显示给他，他选完答案自动记下。"
                         "回答里不要再写题目或答案。他这句说的是不问了、开始吧、直接学，就不要出题，调 skip_diagnosis。", AskCheckArgs, "propose", "action",
                         acts=True, pauses=True, tier="direct")

    # the question as it is recorded; ValueError (the tool error's text) when it does not fit
    def _question(self, a: AskCheckArgs, pick: CheckPick) -> ProbeQuestion:
        if pick.intent == "self_report":
            if a.self_report is None:
                raise ValueError("这一题是自评：用 self_report")
            sr = a.self_report
            options = [" ".join(o.split()) for o in sr.options]
            if len(set(options)) != len(options):
                raise ValueError("选项重复了")
            if (found := latex_in(sr.stem, *options)) is not None:
                raise ValueError(f"用了 LaTeX 记号「{found}」，只用纯文本")
            means: list[Means] = list(sr.means)
            return answered_question(pick, stem=" ".join(sr.stem.split()), options=options,
                                     means=means)
        if a.quiz is None:
            raise ValueError("这一题是小测：用 quiz")
        cell = pick.cell or f"{pick.node}.term"       # a cell-less mastery item: no cell recorded
        try:
            item = checked_item(cell, a.quiz)
        except ItemRefused as exc:
            raise ValueError(str(exc)) from exc
        return answered_question(pick, item, presupposes=a.quiz.presupposes)

    def _pick(self, a: AskCheckArgs, ctx: ToolContext) -> CheckPick:
        snap = self._repo.load(ctx.user_id)
        draft_id = _draft_of(a.pick_id)
        pick = open_pick(snap, draft_id)
        if pick is None or pick.pick_id != a.pick_id:
            raise ValueError("这个 pick_id 不是现在要问的题。" + next_line(snap, draft_id))
        return pick

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(AskCheckArgs, args, lambda a: self._ask(a, ctx))

    def _ask(self, a: AskCheckArgs, ctx: ToolContext) -> ToolResult:
        snap = self._repo.load(ctx.user_id)
        try:
            pick = self._pick(a, ctx)
            q = self._question(a, pick)
        except ValueError as exc:
            return fail(str(exc))
        options = (*q.options, IDK_TEXT) if q.kind == "quiz" else q.options
        block = block_of(snap, pick)
        payload = CheckPayload(question=q.stem, options=options, draft_id=_draft_of(pick.pick_id),
                               block_id=block[0] if block else None,
                               block_name=block[1] if block else None)
        return ToolResult(content="已经把题给他了，正在等他选。", summary=q.stem[:200],
                          pause=Pause.of(PAUSE_KIND, payload))

    @staticmethod
    def _choice(q: ProbeQuestion, reply: Reply) -> int | None:
        text = squash(reply.text)
        if q.kind == "quiz" and text == squash(IDK_TEXT):
            return IDK
        hits = [i for i, o in enumerate(q.options) if squash(o) == text]
        return hits[0] if len(hits) == 1 else None

    def resume(self, args: dict[str, Any], ctx: ToolContext, pause: Pause,
               reply: Reply) -> ToolResult:
        a = AskCheckArgs.model_validate(args)
        try:
            pick = self._pick(a, ctx)
            q = self._question(a, pick)
        except ValueError as exc:            # the pick closed while he was asked: nothing recorded
            return fail(str(exc))
        if not reply.text.strip():
            return ToolResult(content=NO_REPLY, summary=q.stem[:200])
        choice = self._choice(q, reply)
        if choice is None:
            from evowiki.assistant.tools.rough import record_typed
            typed = record_typed(self._repo, self._gateway, self._clock, self._new_id, ctx, _draft_of(pick.pick_id), reply.text.strip()[:200])
            if typed is not None:
                return typed
            return ToolResult(content=f"他没有选选项，回答是：「{reply.text.strip()[:200]}」。这题没有记。"
                                      "他在说别的事就先回答他，答完用 ask_check 把同一题原样再问一次；他不想继续摸底就调 skip_diagnosis"
                                      f"（draft_id：{_draft_of(pick.pick_id)}）。", summary=q.stem[:200])
        from evowiki.wikiapp.goal import answer_check
        try:
            res = answer_check(self._repo, self._gateway(ctx.user_id), ctx.user_id,
                               _draft_of(pick.pick_id), pick.pick_id, q, choice,
                               now=self._clock(), new_id=self._new_id)
        except (WikiConflict, WikiInvalid, LookupError) as exc:
            return fail(f"这一题没有记下：{exc}")
        said = (IDK_TEXT if choice == IDK else q.options[choice])
        verdict = ("" if q.kind == "self_report" else
                   "，答对了" if choice == q.answer else "，没答对")
        head = f"他选了「{said}」{verdict}，已记下。"
        if res.open:
            return ToolResult(content=head + "还有" + next_line(self._repo.load(ctx.user_id),
                                                               _draft_of(pick.pick_id)),
                              summary=q.stem[:200], data={"next_question_required": True})
        where = ("学习路径排好了" if res.path_id else "页面建好了" if res.page_id
                 else "目标的分块图准备好了" if res.map_id else "摸底结束了")
        return ToolResult(content=head + f"摸底结束，{where}。" + start_line(self._repo, ctx.user_id, res.map_id, self._clock()),
                          summary=q.stem[:200])
