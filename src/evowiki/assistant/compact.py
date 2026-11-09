"""Session compaction (plan rev 3.2 §9.1 point 5): fold earlier turns into a short summary.

The cut only falls on whole-turn boundaries (`plan_cut`, built on `agent.compact.choose_cut`): the
recent turns stay verbatim (question and answer, with any tool calls inside them), everything from
`first_kept` up to the cut is handed, with the previous summary, to one tool-free `converse`.
Errored turns (`answer is None`) are neither summarised nor counted. `maintain` is the caller's
one entry point: it plans the cut, compacts, appends the `compaction` entry, and when the summary
cannot be made but the history is past the hard cap it drops the oldest turns visibly instead.

The request carries `params={"upto": <message id of the turn the previous cut kept, or None>,
"through": <message id of the last folded turn>, "evidence_ids": <the ids a learning item may
cite, sorted>, THINKING_PARAM: False, TEMPERATURE_PARAM: 0}` and the session id as subject, so the same session compacted at the same cut
replays the same summary, and the stored request alone is enough to check a reply again
(`summary.check_reply`: a counterfactual rerun or an evaluator needs no session state).

compact@v7 (the product specification): one structured call
(`gateway.call`, `CompactReply`) returns `handoff` (for the next turns' model) and `learning`
(points he said he didn't / did get, or answered wrong / right, each naming the user message it
rests on). Code keeps a learning item only when its `evidence_id` is a folded user message's id
(or an item the previous summary already checked) — the rest are dropped and logged, never the
whole summary —, and builds 改过哪里 itself from `Turn.edits` (accepted proposals only). Each
folded user message is shown to the model as `[<message id>] 用户：…`. A re-compaction hands the
previous summary in to be merged; the new payload replaces it in `fold`. `worth_summarising` says
whether a closing summary (assistant/wrapup.py) is worth a call at all. Thinking is off
(user ruling 2026-11-03): with it on, a summary cost ~$0.008 and ~25 s, and a reply it starved
paid for a second attempt; a starved reply now skips this compaction like any unusable one.
Nothing is deleted: the raw turns stay in the session log."""

import logging
from collections.abc import Callable, Sequence
from typing import Any

from evowiki.agent.compact import CompactPolicy, choose_cut, overflow_drop
from evowiki.assistant.forgotten import MASKED
from evowiki.assistant.session import SessionState, Turn
from evowiki.assistant.summary import (
    EVIDENCE_PARAM,
    CompactReply,
    Recap,
    check_reply,
    merge_edits,
    recap_payload,
    render_handoff,
    reply_json,
    tidy_handoff,
)
from evowiki.goals.context import estimate_tokens
from evowiki.goals.language import Language, language_rule
from evowiki.llm import run_context
from evowiki.llm.gateway import (
    Gateway,
    LLMParseError,
    ProviderOutputError,
    ProviderUnavailableError,
)
from evowiki.llm.models import TEMPERATURE_PARAM, THINKING_PARAM, LLMCall, LLMRequest
from evowiki.llm.providers import ProviderTransientError
from evowiki.llm.run_context import traced_step
from evowiki.llm.usage import Account

__all__ = ["CLOSE_MIN_TOKENS", "COMPACT_PROMPT_VERSION", "COMPACT_PURPOSE", "COMPACT_SYSTEM",
           "DEFAULT_POLICY", "NO_SUMMARY_STATUSES", "SUMMARY_MAX_CHARS", "compact",
           "compact_request",
           "history_tokens", "maintain", "plan_cut", "turn_tokens", "worth_summarising"]

log = logging.getLogger(__name__)

DEFAULT_POLICY = CompactPolicy()
SUMMARY_MAX_CHARS = 2000       # prompt guidance only: the code never cuts a summary
# A provider/gateway output error with one of these statuses means the model answered with no
# usable summary text (blank, cut off before any text, or a refusal, which is deterministic for
# the same fold range): treated like a blank reply.
NO_SUMMARY_STATUSES = frozenset({"empty_output", "truncated", "refusal"})

COMPACT_PURPOSE = "assistant.compact"
COMPACT_PROMPT_VERSION = "compact@v16"   # the prompt; the payload format stays COMPACT_VERSION
CLOSE_MIN_TOKENS = 40          # a closing summary of less than this (and nothing done) is skipped
STEP = "compact"            # its prompt_steps key: the traced step (llm/run_context.py)

COMPACT_SYSTEM = (
    "你负责将即将折叠的陪读对话整理成下一轮助手可使用的交接信息。最近几轮消息会原样保留，不需"
    "要重复。\n"
    "\n"
    "只依据提供的对话和旧交接，不推测用户的学习状态，也不推测未发生的操作。\n"
    "\n"
    "只输出合法 JSON，不要解释、代码块或额外字段：\n"
    "\n"
    "{\n"
    "  \"handoff\": {\n"
    "    \"summary\": \"\",\n"
    "    \"decisions\": [],\n"
    "    \"active_requests\": []\n"
    "  },\n"
    "  \"learning\": {\n"
    "    \"unclear\": [],\n"
    "    \"understood\": []\n"
    "  }\n"
    "}\n"
    "\n"
    "learning 元素为 {\"concept\":\"…\",\"evidence_id\":\"…\"}。"
    "\n"
    "\n"
    "## 一、学习记录\n"
    "\n"
    "learning 记录的是用户明确表现出来的理解情况，而不是对话涉及了哪些知识点。\n"
    "\n"
    "先寻找用户对自己理解情况的明确表达，或可以核对正误的作答。找不到就不要新增 learni"
    "ng。\n"
    "\n"
    "- unclear：用户明确表示某个知识点不懂、没想明白，或者给出可以核对的错误答案。\n"
    "- understood：用户明确表示某个知识点已经理解，或者给出可以核对的正确答案。\n"
    "- 普通提问不代表不懂，也不代表懂了。\n"
    "- 助手已经解释某个知识点，不代表用户已经理解。\n"
    "- 用户表示「不确定」「有点不放心」，不能直接认定为不懂。\n"
    "- 「懂了」只支持用户明确表示理解的内容，不能推广到同一句话中的其他知识点。\n"
    "- learning 不是长期掌握程度的评价，不记录掌握状态 K。\n"
    "\n"
    "例如：\n"
    "\n"
    "用户：「碱基是什么东西？」\n"
    "\n"
    "learning 两个列表均为空。因为这只是提问。\n"
    "\n"
    "用户：「我还是不懂碱基是什么。」\n"
    "\n"
    "unclear 记录「碱基的含义」。\n"
    "\n"
    "用户：「我懂了负的 z，但还是不明白为什么要除以 σ。」\n"
    "\n"
    "understood 记录「负的 z 的含义」，unclear 记录「为什么计算 z 分"
    "数要除以 σ」。\n"
    "\n"
    "用户：「我懂了 z = −1，但不太放心能否跨科目比较。」\n"
    "\n"
    "understood 只能记录「z = −1 的含义」；跨科目比较不进入 learnin"
    "g，仍需处理就写进 summary。\n"
    "\n"
    "每条 learning 的 concept 必须与 evidence_id 所引用的用户"
    "消息实际表达的理解情况一致。\n"
    "\n"
    "evidence_id 使用消息开头的真实编号。正确或错误的作答可以结合对话核对，但编号"
    "仍引用用户作答的消息。不能用普通提问、助手解释或无关的用户表述充当证据。\n"
    "\n"
    "## 二、旧学习记录的继承\n"
    "\n"
    "旧交接中已有的 learning 是之前保存的证据记录。\n"
    "\n"
    "先保留旧 learning，再判断本轮对话是否有明确的新证据改变它。\n"
    "\n"
    "- 本轮没提到旧知识点：原样保留。\n"
    "- 本轮只是再次提问：不能据此删除或改变旧记录。\n"
    "- 本轮助手重新解释了：不能据此认定问题解决。\n"
    "- 本轮用户明确表示已经理解同一个问题：可以将对应的 unclear 更新为 under"
    "stood。\n"
    "- 本轮明确推翻了之前的理解，或给出可核对的错误答案：依据新证据更新。\n"
    "\n"
    "继承旧记录时，保留原 concept 和 evidence_id，不需要在本轮重新寻找原"
    "始消息。\n"
    "\n"
    "不能因为压缩篇幅、换了话题或无法看到旧消息，就删除仍有效的学习记录。\n"
    "\n"
    "## 三、交接信息\n"
    "\n"
    "handoff 只保留接下来仍有价值的信息，不按对话顺序复述。\n"
    "\n"
    "summary：当前主题、关键解释与结论、后续交流需要知道的背景，包括他交代过的事实和安排（比如家里的事、哪天要做什么）。使用简洁完整的句子，不"
    "评价学习者能力。\n"
    "\n"
    "decisions：用户真正确定的选择、暂缓事项及明确给出的理由。助手建议、未确认草稿不"
    "是已确定决定，不编造理由。\n"
    "\n"
    "active_requests：接下来回答仍需要遵守的会话内要求，例如「先解释公式符号，"
    "再给例子」。普通提问、已完成的一次性请求，以及正在等待确认的操作都不属于这里。要求仅在本"
    "次会话有效。\n"
    "\n"
    "还没答的题、没解决的问题写进 summary。页面和操作的状态（出了草稿、等确认、撤销了）不用写，每轮另外现读。\n"
    "\n"
    "## 四、合并与压缩\n"
    "\n"
    "先继承旧交接中仍有效的信息，再根据本轮对话更新。\n"
    "\n"
    "已经解决的待办要删除；已撤销的决定或要求不再保留。旧学习记录依照第二部分单独处理。\n"
    "\n"
    "⟦g:compact⟧不要逐条复述提问，不记录寒暄和无意义的重复。优先保留当前主题、关键"
    "结论、决定、有效要求、未完成事项及学习证据。⟦/g⟧\n"
    "\n"
    "「页面改了哪里」由代码记录，不在摘要中重复。摘要不构成删除、修改或执行页面操作的授权。\n"
    "\n"
    "不要把 [m2] 等消息编号写进正文，只能出现在 evidence_id 中。\n"
    "\n"
    "没有相关内容时，summary 使用空字符串，列表使用空数组。\n"
    "\n"
    f"全文不超过 {SUMMARY_MAX_CHARS} 字"
    "。遵守系统追加的语言规则。"
)


def _answered(turns: tuple[Turn, ...]) -> list[Turn]:
    """A stopped answer is no answer here: a summary may be handed to the next session."""
    return [t for t in turns if t.answer is not None and not t.stopped]


def _live(state: SessionState) -> list[int]:
    """Absolute indices of the answered turns from `first_kept` on."""
    return [i for i in range(state.first_kept, len(state.turns))
            if state.turns[i].answer is not None and not state.turns[i].stopped]


def turn_tokens(t: Turn) -> int:
    """Estimated tokens of one answered turn as the model sees it: the question, the selection
    quoted with it (`loop._history_user`) and the answer."""
    sel = (t.selection or {}).get("text") or ""
    return estimate_tokens(t.user_text) + estimate_tokens(sel) + estimate_tokens(t.answer or "")


def history_tokens(state: SessionState) -> int:
    """Estimated tokens of the summary plus every answered turn from `first_kept` on."""
    return estimate_tokens(state.summary) + sum(turn_tokens(state.turns[i]) for i in _live(state))


def plan_cut(state: SessionState, policy: CompactPolicy) -> int | None:
    """Absolute index into `state.turns` of the first turn to keep verbatim (`len(turns)` = fold
    everything), or None when the history fits. Decided over the answered turns after
    `first_kept`; errored turns are folded with the turns around them."""
    live = _live(state)
    c = choose_cut([turn_tokens(state.turns[i]) for i in live],
                   estimate_tokens(state.summary), policy)
    if c is None:
        return None
    return live[c] if c < len(live) else len(state.turns)


def worth_summarising(turns: Sequence[Turn]) -> bool:
    """Whether a closing summary of `turns` is worth a model call: some answered turn edited a
    page, asked him something or left ops to confirm, or together they reach
    `CLOSE_MIN_TOKENS` (a lone 「你好」 is not worth one)."""
    answered = _answered(tuple(turns))
    if any(t.edits or t.asks or t.pending_ops for t in answered):
        return True
    return sum(turn_tokens(t) for t in answered) >= CLOSE_MIN_TOKENS


def _evidence_ids(previous: Recap | None, folded: list[Turn]) -> list[str]:
    """What a learning item may cite: a folded user message, or an item the previous v7 summary
    already checked (a merge carries it on)."""
    ids = {t.message_id for t in folded}
    if previous is not None and not previous.legacy:
        ids |= previous.evidence_ids()
    return sorted(ids)


def _prompt(previous: Recap | None, turns: list[Turn]) -> str:
    parts: list[str] = []
    if previous is not None:
        old = previous.legacy_text.strip() if previous.legacy else reply_json(previous)
        if old:
            parts.append(f"以下是之前已确认的交接信息。新对话未明确改变的内容仍然有效。请根据新增信息更新交接，保留尚未解决的学习记录。\n{old}")
    lines = []
    for t in turns:
        if t.user_text == MASKED:
            continue
        lines.append(f"[{t.message_id}] 用户：{t.user_text}")
        lines.append(f"助手：{t.answer}")
        if t.edits:
            lines.append(f"（这一轮改了页面：{'；'.join(t.edits)}；系统已记下）")
    parts.append("要整理的对话（方括号里是用户消息的编号）：\n" + "\n".join(lines))
    return "\n\n".join(parts)


def compact_request(state: SessionState, *, cut: int, language: Language = "zh") -> LLMRequest:
    """The request `compact` sends for `turns[first_kept:cut]` (the fixed evaluation cases and a
    rerun build it the same way). ValueError for a bad cut or nothing to fold (see `compact`)."""
    if cut <= state.first_kept or cut > len(state.turns):
        raise ValueError(f"bad cut {cut}: must be in ({state.first_kept}, {len(state.turns)}]")
    folded = _answered(state.turns[state.first_kept:cut])
    if not folded and not state.summary.strip():
        raise ValueError("nothing to compact: every turn before the cut errored and there is "
                         "no earlier summary")
    upto = state.turns[state.first_kept].message_id if state.first_kept else None
    return LLMRequest(
        purpose=COMPACT_PURPOSE, prompt_version=COMPACT_PROMPT_VERSION,
        subject=state.info.session_id, system=f"{COMPACT_SYSTEM}{language_rule(language)}",
        prompt=_prompt(state.recap, folded),
        params={"upto": upto, "through": state.turns[cut - 1].message_id,
                EVIDENCE_PARAM: _evidence_ids(state.recap, folded), THINKING_PARAM: False,
                TEMPERATURE_PARAM: 0})


def compact(state: SessionState, gateway: Gateway, account: Account | None, *, cut: int,
            policy: CompactPolicy = DEFAULT_POLICY,
            language: Language = "zh") -> tuple[dict[str, Any], LLMCall]:
    """Summarise the answered turns of `turns[first_kept:cut]` (plus the old summary) and return
    the compaction payload (`summary.recap_payload` + `first_kept`, and `dropped` when learning
    items did not check) with the model call (`first_kept` is the message id of `turns[cut]`,
    None when `cut == len(turns)`). Raises ValueError before any model call when `cut` is not in
    `(first_kept, len(turns)]`, or when the fold range holds no answered turn and there is no old
    summary (nothing to fold); and after the call when the reply does not fit `CompactReply`
    (LLMParseError), is blank / cut off / a refusal (ProviderOutputError with a
    NO_SUMMARY_STATUSES status), the provider is unreachable or transient, or the rendered
    summary is over `policy.summary_max_tokens` — a bad reply never wipes the history; the caller
    skips compaction for this turn. Every other failure (provider rejected 401/402/403 — the
    answer call would fail too —, budget, missing recording) propagates and fails the turn. The
    account is passed to the gateway only when it is metered."""
    request = compact_request(state, cut=cut, language=language)
    folded = _answered(state.turns[state.first_kept:cut])
    first_kept_id = state.turns[cut].message_id if cut < len(state.turns) else None
    previous = state.recap
    try:
        reply, call = gateway.call("generator", request, CompactReply,
                                   account=account if gateway.metered else None)
    except LLMParseError as exc:
        raise ValueError(f"compaction reply unusable: not the summary schema: {exc}") from exc
    except ProviderOutputError as exc:
        if exc.status not in NO_SUMMARY_STATUSES and not isinstance(exc, ProviderTransientError):
            raise
        raise ValueError(f"compaction reply unusable: no summary ({exc.status}): "
                         f"{exc}") from exc
    except ProviderUnavailableError as exc:
        raise ValueError(f"compaction reply unusable: provider unavailable: {exc}") from exc
    learning, dropped = check_reply(reply, request.params)
    for d in dropped:
        log.info("compaction %s dropped a %s item without checked evidence: %r (%s)",
                 call.id, d.field, d.concept, d.evidence_id)
    recap = Recap(handoff=tidy_handoff(reply.handoff), learning=learning,
                  edits=merge_edits(previous.edits if previous is not None else (),
                                    (e for t in folded for e in t.edits)))
    tokens = estimate_tokens(render_handoff(recap))
    if tokens > policy.summary_max_tokens:
        raise ValueError(f"compaction reply unusable ({call.id}): summary is {tokens} tokens, "
                         f"over {policy.summary_max_tokens}")
    payload: dict[str, Any] = {**recap_payload(recap), "first_kept": first_kept_id}
    if dropped:
        payload["dropped"] = [d.model_dump(mode="json") for d in dropped]
    return payload, call


def maintain(state: SessionState, gateway: Gateway, account: Account | None, *,
             policy: CompactPolicy, language: Language,
             append: Callable[[dict[str, Any]], None],
             refold: Callable[[], SessionState],
             consolidate: Callable[[SessionState, int, int], list[LLMCall]] | None = None,
             ) -> tuple[SessionState, list[LLMCall]]:
    """Keep the history within `policy`: compact when `plan_cut` says so, and when that fails
    (ValueError — a bad summary, LLMParseError included) or the live turns still exceed the
    hard cap, drop the oldest turns with a visible `omitted` entry (the previous summary stays).
    `append` stores a compaction payload, `refold` re-reads the session after it.
    `consolidate(state, first, cut)` (cross-session memory) runs after a summary landed, over the
    turns it folded, and returns its model calls (it fails the turn only on a budget error).
    Returns the
    state and the successful model calls; a failed attempt raised inside `compact`, so its call
    is in the CallLog only. Afterwards `history_tokens(state) <= policy.hard_cap`."""
    calls: list[LLMCall] = []
    first: int | None = None         # set when a summary landed (its first folded turn)
    cut = plan_cut(state, policy)
    if cut is not None:
        # a traced step (plan 2026-10-29): the call with its full input, an unusable reply as a
        # rejection, and the summary entry appended inside the run; under the session's runs
        # (`assistant:<session>`), so deleting the session deletes it
        with traced_step(STEP, state.info.session_id, state.info.user_id,
                         input={"session_id": state.info.session_id, "cut": cut},
                         under=f"assistant:{state.info.session_id}") as step:
            try:
                payload, call = compact(state, gateway, account, cut=cut, policy=policy,
                                        language=language)
            except ValueError as exc:
                run = run_context.current()
                if run is not None and run.last == "call" and run.last_call is not None:
                    step.reject(str(exc)[:300])     # the reply came back but was unusable
                log.warning("compaction failed for %s, retried next turn",
                            state.info.session_id, exc_info=True)
            else:
                first = state.first_kept
                append(payload)
                state = refold()
                calls.append(call)
        # cross-session memory is a step of its own, after the summary's run
        if first is not None and consolidate is not None:
            calls.extend(consolidate(state, first, cut))
    live = _live(state)
    d = overflow_drop([turn_tokens(state.turns[i]) for i in live],
                      estimate_tokens(state.summary), policy)
    if d:
        kept = state.turns[live[d]].message_id if d < len(live) else None
        old = recap_payload(state.recap) if state.recap is not None else {"summary": ""}
        append({**old, "first_kept": kept, "omitted": d, "reason": "summary_failed"})
        state = refold()
        log.warning("compaction summary failed for %s: dropped the oldest %d turn(s)",
                    state.info.session_id, d)
    return state, calls
