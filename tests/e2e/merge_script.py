"""Test-only hooks to script the 合并页面 flow (page_merge) in a scripted backend (e.g.
tests/e2e/blocks_backend / sweep_backend) for a real-Obsidian walkthrough, without a model.

The merge uses these model purposes:
- `assistant.turn` (the 助手's chat turn): ONE tool call `merge_pages` with
  `{"pages": [<page id>, <page id>, ...], "title": <merged title>}` (`MergePagesArgs`: >= 2 ids,
  title 1-120 chars, optional `why`), then — after the tool's answer — one plain sentence.
  `merge_turn` below answers 「把「A」和「B」合成一页」 (optional 「，叫「C」」) that way, finding the
  ids in the turn's context pack (`- [A](#p:<id>)` lines: the pages must be among the related
  pages it lists).
- the merged page's write after 确认: the writer's outline (`plan_page`, the scripted writer takes
  it: blocks_backend's `organize_reply` answers any page) and the page writer's `goals.write` answers, one per
  non-check section (`{"text": "回想：…\\n\\n<body>"}`, the scripted writer's queue). The write's
  input carries the originals' text (writer instruction 「这一页是把下面几页合成一页…」).
  A page whose domain is unknown gets the embedding's nearest label (no model call).

`install_page_actions(svc, ledger)` registers the real page actions (page_merge among them) and
the write queue whose commit hook settles a merge (originals to the trash, path rows replaced) —
with idle Bot writing off, so the scripted `goals.write` queue is only used by pages the learner
asks for."""

import re
from collections.abc import Sequence

from evowiki.llm.chat import ChatRequest, ChatTurn, ToolCall

_MERGE = re.compile(r"把「(.+?)」和「(.+?)」合[成并]一页(?:[，,]\s*叫「(.+?)」)?")


def _page_ids(texts: Sequence[str]) -> dict[str, str]:
    """title -> page id from the context pack's `- [title](#p:id)` lines (last one wins)."""
    out: dict[str, str] = {}
    for t in texts:
        for title, pid in re.findall(r"\[([^\]\n]+)\]\(#p:([A-Za-z0-9_-]+)\)", t):
            out[title] = pid
    return out


def merge_turn(request: ChatRequest) -> ChatTurn | None:
    """The 助手's turn for 「把「A」和「B」合成一页（，叫「C」）」: the merge_pages call, then one
    sentence after its result. None: not a merge message, or A / B not in the context (the caller
    treats it as unscripted)."""
    msgs = request.messages
    users = [i for i, m in enumerate(msgs) if m.role == "user"]
    if not users:
        return None
    said = msgs[users[-1]].text
    m = _MERGE.search(said)
    if m is None:
        return None
    if any(x.role == "tool" for x in msgs[users[-1] + 1:]):
        return ChatTurn(text="合并的草稿在上面，确认后先写新页，写好后原来两页才会移走。")
    ids = _page_ids([request.system, *(x.text for x in msgs)])
    a, b = m.group(1), m.group(2)
    if a not in ids or b not in ids:
        return None
    title = m.group(3) or f"{a}与{b}"
    return ChatTurn(tool_calls=(ToolCall(id="m1", name="merge_pages", args={
        "pages": [ids[a], ids[b]], "title": title}),))


class _NoIdle:
    """`IdleEnv` that never writes ahead (an assistant turn always counts as running)."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def turn_busy(self, user_id: str) -> bool:
        return True


def install_page_actions(svc, ledger) -> None:
    """The service's own page actions on `ledger` (`register_page_actions`: page_merge, page_split,
    ... and the merge's settle on the write queue's commit hook), over a write queue whose idle
    Bot writing is off. Call before `build_assistant` / `create_app`."""
    from evowiki.service.deps import (
        _cancel_on_path_changes,
        page_deps,
        register_page_actions,
    )
    from evowiki.store.usage import usage_tz
    from evowiki.wikiapp.repo import PgRepo
    from evowiki.wikiapp.write_queue import PgIdleEnv, WriteQueue
    tz = usage_tz()
    svc.write_queue = WriteQueue(
        svc.runs, lambda: page_deps(svc), PgRepo(svc.pool), svc.clock, svc.new_id,
        _NoIdle(PgIdleEnv(svc.pool, tz, svc.new_id)), ledger=ledger, tz=tz,
        can_write=lambda _uid: True)
    _cancel_on_path_changes(svc.write_queue)
    register_page_actions(svc, ledger)
