"""ask_user: the assistant asks the learner one question and waits for his answer.

The turn ends on the question (agent.loop `Paused`): nothing runs and nothing waits while he
thinks. His next message, typed or an option he clicked, resumes that turn (assistant.loop) and is
this call's result, in protocol. It may not answer the question at all: the result says so, and
the model then answers what he said instead (the question is dropped, never asked again by
itself). Shape after DeepTutor's ask_user (HKUDS/DeepTutor, Apache-2.0, tools/ask_user.py), cut to
what the chat needs: one question in one plain sentence, options only when the model gives them
(no catch-all 「其他」: the learner can always type)."""

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import run_guarded, spec
from evowiki.agent.tools import Pause, Reply, ToolContext, ToolResult

NAME = "ask_user"
DESCRIPTION = ("向学习者问一个问题，等他回答后再继续。只在不问就答不好时用（比如不知道他想达到什么、"
               "基础到哪），能从页面、K 或对话里看出来的不要问。一次只问一件事，用一句平常的话问。"
               "只有答案就是几个固定选项之一时才给 options，否则不给，让他直接打字回答。")
ASKED = "已经问了学习者，正在等他回答。"
# model-facing: his next message, as the ask_user call's result
REPLIED = ("你问了他：「{q}」。他的下一条消息：\n{a}\n"
           "如果这是在回答你的问题，就按他的回答接着做；如果不是，就不要再追问这个问题，直接回应他说的话。")


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=300, description="一句话的问题")
    options: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list, max_length=6, description="可选；只在答案就是这几个之一时给")


class AskUserPayload(BaseModel):
    """`Pause.payload` of kind "ask_user" (the plugin's ask card)."""

    model_config = ConfigDict(frozen=True)

    question: str
    options: tuple[str, ...] = ()


class AskUserTool:
    def __init__(self) -> None:
        self.spec = spec(NAME, DESCRIPTION, Args, "read", pauses=True)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(Args, args, self._ask)

    @staticmethod
    def _ask(a: Args) -> ToolResult:
        q = " ".join(a.question.split())
        options = tuple(dict.fromkeys(" ".join(o.split()) for o in a.options))
        return ToolResult(content=ASKED, summary=q[:200],
                          pause=Pause.of(NAME, AskUserPayload(question=q, options=options)))

    def resume(self, args: dict[str, Any], ctx: ToolContext, pause: Pause,
               reply: Reply) -> ToolResult:
        q = str(pause.payload.get("question", ""))
        return ToolResult(content=REPLIED.format(q=q, a=reply.text.strip()), summary=q[:200])
