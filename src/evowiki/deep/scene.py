"""The declarative route of the visualization step: the author writes block markup (`deep.evoui.markup`), not a program. The
block is drawn by the renderer, so nothing the model writes runs as code and the page shows it at once. A reply that does not
compile, validate or pass the render check goes back once with the reason; after that `VisualFailed` (the caller falls back to
the program route or to text). A version that passes is looked at once: the author sees real screenshots and keeps it or writes
one revision; a revision that does not pass goes back once, then what passed stands. A spending limit or a cancel propagates."""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from functools import cache
from threading import Event
from typing import Any

from pydantic import ValidationError

from evowiki.agent.skills import load_skill
from evowiki.deep.contracts import BlockChecker, RenderCheck
from evowiki.deep.evoui import block_json
from evowiki.deep.evoui.contracts import MAX_FALLBACK_CHARS, BlockDraft, EvoUIBlock
from evowiki.deep.evoui.markup import MarkupError, compile_markup
from evowiki.deep.evoui.validate import validate as validate_block
from evowiki.deep.language import messages_in_language, output_rule
from evowiki.deep.visual import Brief, Drawn, VisualFailed, tried_note
from evowiki.goals.language import Language
from evowiki.llm.budget import BudgetExceededError
from evowiki.llm.chat import ChatMessage, ChatRequest, ImagePart, ImagesNotSupported
from evowiki.llm.deadline import DeadlineExceeded, RequestCancelled
from evowiki.llm.gateway import Gateway, ProviderOutputError, ProviderUnavailableError
from evowiki.llm.models import LLMCall
from evowiki.llm.usage import Account

log = logging.getLogger("evowiki.deep.scene")

PURPOSE = "deep.scene"
PROMPT_VERSION = "scene@v4"
REPAIR = "没过检查：{why}\n改好后输出完整的几段。"
REVISE = "\n\n这一段已经有一张图（编译后的 JSON）：\n{block}\n要改的地方：{change}\n其余尽量不动，用标记写出修改后的完整几段。"
LOOK = ("上面是你画的图的真实渲染：依次是刚打开、改变控件之后、最窄的窗格、深色模式。\n{notes}\n"
        "只看图判断：把文字遮住，一个没学过的人能看出这个概念吗？动手前后的变化说明问题吗？哪里挤、哪里被裁、哪里字压着线或者看不清？"
        "图上画的和教程一致吗？\n<review>里写判断。</review>够好就只再写 <keep/>；不够好就按原来的格式输出修改后的完整几段。")
MAX_REASONS = 6


@cache
def system_text() -> str:
    return load_skill("draw-scene").body


def _tag(text: str, tag: str) -> str:
    got = re.findall(rf"<{tag}>(.*?)</{tag}>", text, flags=re.DOTALL)
    return got[-1].strip() if got else ""


def parse_reply(raw: str, b: Brief) -> dict[str, Any]:
    """The block of a reply. `MarkupError` when it has none or it cannot be read."""
    m = re.search(r"<block\b([^>]*)>(.*)</block>", raw, flags=re.DOTALL)
    if m is None:
        raise MarkupError("没有 <block>…</block>")
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
    return compile_markup(m.group(2), title=attrs.get("title", b.heading)[:120] or b.heading[:120] or "图",
                          question=attrs.get("question", b.invisible)[:300] or b.heading[:300] or "图",
                          fallback=(_tag(raw, "fallback") or b.body)[:MAX_FALLBACK_CHARS])


@dataclass
class SceneStep:
    gw: Gateway
    account: Account | None
    checker: BlockChecker
    page_id: str
    title: str
    goal: str
    known: str
    language: Language = "zh"
    calls: list[LLMCall] = field(default_factory=list)
    cancel: Event | None = None

    def _ask(self, messages: list[ChatMessage]) -> Any:
        if self.cancel is not None and self.cancel.is_set():
            raise RequestCancelled("the run was cancelled")
        turn, call = self.gw.converse("generator", ChatRequest(
            purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=self.page_id, system=system_text() + "\n" + output_rule(self.language),
            messages=messages_in_language(messages, self.language)), account=self.account)
        self.calls.append(call)
        return turn

    def _version(self, b: Brief, raw: str) -> tuple[BlockDraft | None, RenderCheck | None, str]:
        try:
            data = parse_reply(raw, b)
        except MarkupError as e:
            return None, None, str(e)
        problems = validate_block(data)
        if problems:
            return None, None, "；".join(problems[:MAX_REASONS])
        try:
            draft = BlockDraft(block_id=b.block_id, section_id=b.section_id, why=b.invisible[:300] or b.heading,
                               block=EvoUIBlock.model_validate(data))
        except ValidationError as e:
            return None, None, str(e.errors(include_url=False)[0]["msg"])
        check = self.checker.check_block(b.block_id, draft.block)
        if not check.ok:
            from evowiki.deep.tools import check_text
            return None, None, check_text(check)[:1500]
        return draft, check, ""

    def draw(self, b: Brief, extra: str = "") -> Drawn:
        try:
            return self._draw(b, extra)
        except (BudgetExceededError, RequestCancelled):
            raise
        except (ProviderOutputError, ProviderUnavailableError, DeadlineExceeded) as exc:
            log.warning("scene %s/%s: %s: %s", self.page_id, b.block_id, type(exc).__name__, exc)
            raise VisualFailed(f"模型没有答复（{type(exc).__name__}）") from exc

    def revise(self, b: Brief, current: BlockDraft, change: str) -> Drawn:
        """A change to a block that exists. VisualFailed: nothing changes."""
        return self.draw(b, REVISE.format(block=block_json(current.block), change=change))

    def _draw(self, b: Brief, extra: str = "") -> Drawn:
        msgs = [ChatMessage(role="user", text=(
            f"页面：{self.title}\n目标：{self.goal}\n学习者：{self.known}\n\n这一段：{b.heading}\n{b.body}\n\n"
            f"写页面的人的回答：\n看不见的：{b.invisible}\n画什么：{b.draw}\n动手：{b.touch}{extra}{tried_note(b)}"))]
        turn = self._ask(msgs)
        draft, check, why = self._version(b, turn.text)
        notes: tuple[str, ...] = ()
        if draft is None:
            notes = (f"第一版没过：{why[:300]}",)
            msgs += [ChatMessage(role="assistant", text=turn.text, echo=turn.echo),
                     ChatMessage(role="user", text=REPAIR.format(why=why))]
            turn = self._ask(msgs)
            draft, check, why = self._version(b, turn.text)
        if draft is None or check is None:
            raise VisualFailed(why[:400])
        design, conflict, review = _tag(turn.text, "design"), _tag(turn.text, "conflict"), ""
        seen = self.checker.look_block(b.block_id, draft.block, ())
        if seen.shots:
            parts = tuple(ImagePart(media_type="image/jpeg", sha256=hashlib.sha256(data.encode()).hexdigest(),
                                    data_b64=data, name=name) for name, data in seen.shots)
            looked = [*msgs, ChatMessage(role="assistant", text=turn.text, echo=turn.echo),
                      ChatMessage(role="user", parts=parts, text=LOOK.format(notes="\n".join(seen.notes)))]
            try:
                again = self._ask(looked)
                review = _tag(again.text, "review")
                if "<block" in again.text:
                    d2, c2, why2 = self._version(b, again.text)
                    if d2 is None:          # it just said what is wrong with what passed: the revision goes back once
                        again = self._ask([*looked, ChatMessage(role="assistant", text=again.text, echo=again.echo),
                                           ChatMessage(role="user", text=REPAIR.format(why=why2))])
                        d2, c2, why2 = self._version(b, again.text)
                    if d2 is not None and c2 is not None:
                        draft, check, design = d2, c2, _tag(again.text, "design") or design
                        conflict = _tag(again.text, "conflict") or conflict
                    else:
                        log.warning("scene %s/%s: the revision is dropped: %s", self.page_id, b.block_id, why2)
            except ImagesNotSupported:
                pass                      # a model without eyes: what passed stands
            except (ProviderOutputError, ProviderUnavailableError, DeadlineExceeded) as exc:
                log.warning("scene %s/%s: no look: %s", self.page_id, b.block_id, type(exc).__name__)
        return Drawn(block=draft, images=(), check=check, design=design, review=review, notes=(*notes, *seen.notes),
                     conflict=conflict)
