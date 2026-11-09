"""Page images read by Claude through the gateway (the vision reader for the "claude" side).

One converse call per page, so the usage meter and the llm_call log see every page. A 402 or a
budget stop is raised as is: the import decides what a failed page means.

`printed_only()` is the variant the exam workflow re-reads untrusted pages with
(source-vision-printed@v1): only printed content is transcribed; handwritten letters, ticks,
corrections and scratch work are not copied at all. The import reads with the plain prompt."""

import os
from collections.abc import Callable

from evowiki.llm.chat import ChatMessage, ChatRequest
from evowiki.llm.gateway import Gateway, ProviderOutputError
from evowiki.llm.usage import Account
from evowiki.sources.images import image_part
from evowiki.sources.vision import PageImageRead

PURPOSE = "source.vision"
PROMPT_VERSION = "source-vision@v2"
PRINTED_PROMPT_VERSION = "source-vision-printed@v1"
DEFAULT_VISION_MODEL = "claude-sonnet-5-5"   # Q5

SYSTEM = ("把这页图里的文字原样转成 Markdown，公式用 LaTeX；图表和几何图用文字描述；"
          "读不清或拿不准的地方（包括上下标）写〔看不清〕，不按常识补；只输出内容。")
PRINTED_SYSTEM = (
    "这页是印好的试卷或资料拍的照片，上面可能有人用笔写过。只把印刷的内容原样转成 Markdown，"
    "公式用 LaTeX；图表和几何图用文字描述。手写的内容一律不抄：手写的字母、数字、勾叉、批改、"
    "涂改、圈画和草稿算式都不要写出来，题目括号里手写的答案也不抄（括号照印刷的样子留空）。"
    "手写盖住了印刷的字、读不清的地方写〔看不清〕；只输出内容。")


class ClaudePages:
    name = "claude"

    def __init__(self, gateway: Gateway | None, account_for: Callable[[], Account | None], *,
                 printed: bool = False):
        self._gateway = gateway
        self._account_for = account_for
        self.printed = printed
        self.system = PRINTED_SYSTEM if printed else SYSTEM
        self.prompt_version = PRINTED_PROMPT_VERSION if printed else PROMPT_VERSION

    def available(self) -> bool:
        return self._gateway is not None

    def for_user(self, user_id: str) -> "ClaudePages":
        """The same reader with every page metered to `user_id` (imports read for one user)."""
        from evowiki.wikiapp.llm import purpose_group
        account = Account(user_id=user_id, group=purpose_group(PURPOSE))
        return ClaudePages(self._gateway, lambda: account, printed=self.printed)

    def printed_only(self) -> "ClaudePages":
        """The same reader transcribing printed content only (the exam re-read)."""
        return ClaudePages(self._gateway, self._account_for, printed=True)

    def read(self, png_pages: list[bytes]) -> list[PageImageRead]:
        if self._gateway is None:
            raise RuntimeError("Claude 读页需要模型网关")
        out: list[PageImageRead] = []
        for n, png in enumerate(png_pages, 1):
            request = ChatRequest(
                purpose=PURPOSE, prompt_version=self.prompt_version, subject=f"page-{n}",
                system=self.system,
                messages=(ChatMessage(role="user", text=f"第 {n} 页",
                                      parts=(image_part(png, f"第{n}页.png"),)),))
            try:
                turn, _ = self._gateway.converse("generator", request,
                                                 account=self._account_for())
            except ProviderOutputError as exc:
                if exc.status != "empty_output":   # 402, refusal, budget...: the import decides
                    raise
                out.append(PageImageRead(None, False, "empty"))   # the gateway rejects empty turns
                continue
            text = turn.text.strip()
            out.append(PageImageRead(text, False) if text
                       else PageImageRead(None, False, "empty"))
        return out


def vision_model(env: "os._Environ[str] | dict[str, str] | None" = None) -> str:
    """The model that reads page images at import (Q5): EVOWIKI_VISION_MODEL."""
    return (env if env is not None else os.environ).get("EVOWIKI_VISION_MODEL",
                                                         DEFAULT_VISION_MODEL)
