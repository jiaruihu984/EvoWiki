"""`revise_page(page_id?, source_ids?)` (materials spec 2026-10-20 §7 D): the assistant drafts a
whole-page revision of a wiki page from the learner's materials.

Chat-first: the learner, on a page, pastes or attaches a material or says 「按这个改」「老师讲的不
一样」; the model imports a new url / text with `add_source` and then calls this tool. With no
`source_ids` the run picks the materials itself (`source_revise._pick`); the learner is never asked
to choose from a list.

The tool only starts the work: `ActionDeps.revise` is `wikiapp.source_revise.start_revise` (the same
function as the page route and the Bot's 更新), which proposes the `page_revise` ledger row (always
`ask`, `state="writing"`) and starts the `source_revise` run that writes the draft into it. The
draft shows in the page as track changes; 确认替换 approves the row, 取消 rejects it. Nothing
changes before that.

- The key is `revise:<user>:<step key>`: a replayed step finds its row (whatever its run did, even
  when the run never started and the row has no run id: that row's card, no second run).
- A page with an open draft returns that draft's card; a user note, an unknown page or material
  is `fail(...)`."""

from collections.abc import Sequence
from typing import Any

from pydantic import Field

from evowiki.agent.tool_helpers import fail, run_guarded, spec
from evowiki.agent.tools import ToolContext, ToolResult
from evowiki.assistant.tools.actions import _A, ActionDeps, _step_key, card_of
from evowiki.bot.actions import ActionRow
from evowiki.wikiapp.repo import WikiInvalid
from evowiki.wikiapp.source_revise import NOT_STARTED, RevisePending

NO_PAGE = "要说明改哪一页（page_id），或者在那一页上打开助手"
NO_REVISE = "现在不能按资料改页面"

DESCRIPTION = (
    "按资料改一页：EvoWiki 按用户的资料把这一页整页重写成一份草稿，草稿在页面里用修订标记显示，"
    "用户点「确认替换」才换成新的，点「取消」就不改。省略 page_id = 用户当前打开的页。"
    "用户在某一页上贴了 / 附了资料，或者说「按这个改」「按讲义改」「老师讲的不一样」时用："
    "有新的网址或文字就先 add_source，再把它给的资料 id 放进 source_ids；用户没说是哪份资料就"
    "不给 source_ids（EvoWiki 自己找讲到这一页的资料）。不要让用户从资料列表里挑。"
    "用户自己的笔记不能改。没有资料、只是要重写或重讲，不用这个，用 propose_page_edit。")


class RevisePageArgs(_A):
    page_id: str | None = Field(default=None, description="要改的页；省略 = 当前页")
    source_ids: list[str] = Field(default_factory=list, max_length=10,
                                  description="资料 id（add_source / list_sources 给的）；省略 = 自动找")


def _say(row: ActionRow, *, pending: bool = False) -> str:
    lead = "这一页已经有一份草稿了" if pending else "正在按资料写草稿"
    return (f"{lead}（{row.action_id}）：{row.summary}。写好后会在页面里用修订标记显示，用户点"
            "「确认替换」才会换成新的，点「取消」就不改；确认前页面不会变。草稿还没写好，不要说已经生成或已经改好了。")


class RevisePageTool:
    def __init__(self, deps: ActionDeps):
        self._d = deps
        self.spec = spec("revise_page", DESCRIPTION, RevisePageArgs, "propose", "action", acts=True)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(RevisePageArgs, args, lambda a: self._run(a, ctx))

    def _run(self, a: RevisePageArgs, ctx: ToolContext) -> ToolResult:
        d = self._d
        key = f"revise:{ctx.user_id}:{_step_key(ctx)}"
        found = d.ledger.get_by_key(key)
        if found is not None and found.user_id == ctx.user_id:
            return self._result(found)       # a replay: that row as it is now, never a second run
        page_id = a.page_id or ctx.page_id
        if not page_id:
            return fail(NO_PAGE)
        if d.revise is None:
            return fail(NO_REVISE)
        ids: Sequence[str] = tuple(dict.fromkeys(s.strip() for s in a.source_ids if s.strip()))
        try:
            row, _run_id = d.revise(ctx.user_id, page_id, ids, key)
        except RevisePending as e:
            return self._result(e.row, pending=True)
        except (LookupError, WikiInvalid) as e:
            return fail(str(e))
        except Exception:  # noqa: BLE001 — the run did not start: start_revise failed the row
            row = d.ledger.get_by_key(key)
            detail = row.content.get("detail") if row is not None else None
            return fail(str(detail or NOT_STARTED))
        return self._result(row)

    @staticmethod
    def _result(row: ActionRow, *, pending: bool = False) -> ToolResult:
        if row.status == "failed" or row.content.get("state") == "failed":
            # the row says why (its card too: it is in 参数 either way)
            out = fail(str(row.content.get("detail") or row.detail or NOT_STARTED))
            return out.model_copy(update={"action": card_of(row)})
        return ToolResult(content=_say(row, pending=pending), action=card_of(row))
