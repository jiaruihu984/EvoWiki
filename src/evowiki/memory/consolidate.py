"""Session consolidation (spec §2.3): one structured model call over the turns not yet
consolidated, then code decides what may land.

When: after a compaction folded turns (`compact.maintain`) and after the previous session was
closed (`carryover.close_previous`). The segment is the turns after the session's watermark (the
latest `consolidation` entry's through_message_id; the session's first turn when there is none)
up to `through`, at most SEGMENT_TURNS_MAX answered turns (the rest goes to the next call), so a
failed call is retried by the next one.
The model sees only that segment's user / assistant log entries (a recall message is never in
the log), a few earlier turns as context, and the user's active decisions / open questions (to
say what a new one supersedes or resolves).

What code checks, per candidate (the reason it is dropped in brackets):
  0. a preference / about_me the same message's remember tool wrote in that turn [already_landed].
  1. evidence: every message is found (`find_message`) [evidence_missing] and is this user's
     [evidence_foreign]; a user message's quote is verbatim in it (`verify_quote`)
     [quote_mismatch]; at least one is in this segment [evidence_missing]. Others may be his
     earlier messages, of this or another session. An answer a forgotten session's content
     reached reads as forget.PLACEHOLDER: it is not shown and backs nothing [evidence_removed].
  2. preference / about_me: a user message backs it [not_from_user] and the line's words are in
     what it quotes [unbacked]; it goes to the
     `PreferenceLander` (S3). Deferred -> kept in the entry's `pending_landing` for
     `retry_pending_landings`, not dropped. An about_me said of one domain carries it in `topic`
     (required in the schema, null when it is about him), and its line names it (`scoped_line`).
  3. decision: his own words back it [not_from_user]; an assistant line is background only. When
     all he said is a bare yes (`_assent`: short, nothing in common with the decision), the
     background is the assistant message right before it in the log, taken by code, not the
     model's pick [no_content when there is none]. A decision that says what a preference landed
     in this run says (same message of his, close text) is left to that preference, which goes to
     S3 [same_as_preference]. The other way round, by scope: a preference from a message of his
     that also backs a kept page / goal / concept decision is that decision, not a rule for
     every page (S3 keeps no page rule either) [same_as_decision].
  4. open_question: a user message backs it [not_from_user].
  5. a forgotten item (`memory_suppression`) of the same kind shares an evidence message
     [suppressed].
  6. a preference (a would-be rule) goes through the rule guard (`check_auto_rule`): blocked
     [blocked_rule], or it would have to ask him [needs_confirm]; consolidation never asks. One
     that restates a rule of his, pending ones included (the judge's "same"), lands as that rule
     (S3 returns it and its card: one preference, one card).
  7. corrected (his correction of an answer, this segment's or one it was reminded of): that
     episode's answer stops being recalled (`index.mark_answer_corrected`); the log keeps it.
     withdrawn: an active decision of his goes out of force, nothing replacing it.
  8. supersedes / resolves name an active item of the same user, kind and scope, else the field
     is ignored.

Commit: the model call and every check run without any lock; then one short transaction
(`MemoryStore.commit_consolidation`) takes the session's consolidation lock, re-reads the
watermark and writes the items, the status changes and the `consolidation` entry together, or
nothing when the watermark moved since the run read it (another run got there first, whatever
range it covered) or a forget request of the session came in meanwhile (a preference's landing
too: `MemoryStore.unless_forgotten`). A crash rolls it back: the watermark does not move and
the next time tries again."""

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evowiki.assistant.forgotten import MASKED, Forgotten, forgotten_of
from evowiki.assistant.session import Entry, SessionState, SessionStore
from evowiki.goals.language import Language, language_rule
from evowiki.llm.gateway import Gateway, ProviderOutputError, ProviderUnavailableError
from evowiki.llm.models import THINKING_PARAM, LLMCall, LLMRequest
from evowiki.llm.usage import Account
from evowiki.memory import forget
from evowiki.memory.contracts import EvidenceRef, ItemKind, MemoryScope, ScopeType
from evowiki.memory.index import CORRECTED_ANSWER, mark_answer_corrected
from evowiki.memory.lander import (
    Deferred,
    Landed,
    PreferenceLander,
    Refused,
    RuleGuard,
    Same,
    evidence_refs,
)
from evowiki.memory.search import UNDONE_NOTE, UndoneOf
from evowiki.memory.store import Episode, Item, MemoryStore, StatusChange, tag, watermark_seq
from evowiki.memory.turns import verify_quote
from evowiki.profile.note import scoped_line
from evowiki.retrieval.embed import Embedder
from evowiki.teach.textsim import bigram_jaccard, jaccard, normalize

CONSOLIDATE_PURPOSE = "assistant.consolidate"
CONSOLIDATE_PROMPT_VERSION = "consolidate@v13"
CONTEXT_TURNS = 2                  # answered turns before the segment, shown as context only
ACTIVE_MAX = 20                    # active decisions / open questions shown for supersedes
CANDIDATES_MAX = 12
SEGMENT_TURNS_MAX = 40             # answered turns per call; a longer backlog takes several
TEXT_MAX = 1500                    # characters of one message in the prompt
ASSENT_MAX = 8                     # characters of a bare yes (「可以，就这样」), punctuation aside
SAME_AS_PREF = 0.5                 # bigram Jaccard of a decision restating a landed preference
SAME_CHARS = 0.6                   # share of the shorter text's characters: what a remember tool wrote
BACKED_CHARS = 0.4                 # share of a card's characters found in the words of his it quotes
LANDING_TOOLS = {"preference": "remember_preference", "about_me": "remember_about_me"}

CONSOLIDATE_SYSTEM = (
    "从下面这段陪读对话里，找出过了这次对话还用得着的东西。用户的话才算数，助手说的只是它当时的说法，可能是错的。\n"
    "可以记四类，拿不准就不记。想学什么不用记，他说「记一下」也不记，目标有自己的记录。\n"
    "- 偏好：他明说以后讲解都要怎样（怎么讲、页面怎么排），对所有页面都适用。讲解和页面之外的事（每天推几条、多久提醒一次）不是偏好；"
    "只对这一次有效的要求（「这题讲简单点」「一句话回答就行」「这次别用表格，我在手机上看」）也不是，他说「这次」「今天」或者给的是眼下的情况，就是这一次。\n"
    "- 关于他自己：他明说的、过了这次还成立的事（是谁、做过什么、会什么、每天能花多少时间，家人、宠物这类生活事实，比如「我的狗叫豆豆」）。他说的是「这个目标」「学这个」或这段时间的，一律不算（「这个目标下周考试」"
    "「学这个我是零基础」「这周很忙」）；他问的问题、他对讲解的反应、他回答摸底题或选的选项（学过、忘了、没听过某一块）也不算，那些是这一块的学习记录，有自己的地方。"
    "这类和偏好会原样写进他的档案：content 写成他自己会写的一句话（「机械工程研究生」），不写「用户」。"
    "只说某个领域或技能的（「python 什么都不会」），把它写进 topic。\n"
    "- 决定：他和助手定下的学习或内容上的取舍，之后的对话要照着来。让助手做一件事（改页面、拆页、建目标、改设置、推迟复习）"
    "是操作请求，不是决定，不管做没做、撤没撤：操作有自己的记录。\n"
    "- 问题：他明说还没弄懂或留到以后再看的问题。只是问了一句、要个例子、要讲详细，都不是（他问「σ 是什么」，不记）；下面已有的问题不再记一遍。\n"
    "他问起以前说过的事，不是又说了一件新事。"
    "他说「这一页」「这个目标」「这个知识点」时，范围就填那里（id 不知道可以不填）：只对那里有效的长期要求记成那里的决定，不记成偏好。"
    "每条都要给出证据：消息 id 和角色；用户的话要照抄原文作为 quote。证据写用户自己的话；他只是同意了助手的方案时，再附上那条方案。\n"
    "他纠正了助手的说法，或助手后来自己更正了前面的话时，把说错的助手消息 id 写进 corrected；下面列出的旧回答里和这次说法矛盾的也写进去。助手其实没说错的不写。"
    "他收回了已有的一条决定、又没有定新的，把它的 id 写进 withdrawn。"
)

log = logging.getLogger(__name__)


class EvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=200)
    role: Literal["user", "assistant"]
    # a piece of a message as the prompt shows it (cut at TEXT_MAX); the model often quotes a
    # whole assistant plan (live run D: a 793-character plan failed the old 500 limit)
    quote: str | None = Field(default=None, max_length=TEXT_MAX)
    session_id: str | None = Field(default=None, max_length=200,
                                   description="另一段对话的消息才填；默认是这段对话")


class Candidate(BaseModel):
    # topic has a default for what is already stored (a waiting landing) yet is required of the
    # model: an optional field is what a model leaves out (the 2026-11-03 reply left out scope too)
    model_config = ConfigDict(extra="forbid",
                              json_schema_extra=lambda s: s["required"].append("topic"))

    kind: ItemKind
    content: str = Field(min_length=1, max_length=300)
    topic: str | None = Field(default=None, max_length=60, description=(
        "关于他自己的一条只说某个领域或技能时写它（Python、线性代数）；说他整个人或不是这一类时写 null"))
    scope_type: ScopeType = "global"
    scope_id: str | None = Field(default=None, max_length=200)
    evidence: list[EvidenceIn] = Field(min_length=1, max_length=6)
    supersedes: str | None = Field(default=None, max_length=200)
    resolves: str | None = Field(default=None, max_length=200)


class Candidates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Candidate] = Field(default_factory=list, max_length=CANDIDATES_MAX)
    corrected: list[str] = Field(default_factory=list, max_length=CANDIDATES_MAX,
                                 description="他纠正了的助手消息 id")
    withdrawn: list[str] = Field(default_factory=list, max_length=CANDIDATES_MAX,
                                 description="他收回的决定 id")


@dataclass
class ConsolidateResult:
    items_written: list[Item] = field(default_factory=list)
    dropped: list[tuple[Candidate, str]] = field(default_factory=list)
    call: LLMCall | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)
    committed: bool = False


def candidate_key(kind: str, message_ids: Sequence[str], content: str) -> str:
    """Stable id of a candidate: its kind, its evidence messages (sorted) and its text (one message
    of his can say two things: each is its own card)."""
    blob = "\n".join([kind, *sorted(set(message_ids)), " ".join(content.split())]).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _key_of(c: Candidate, session_id: str) -> str:
    return candidate_key(c.kind, [f"{e.session_id or session_id}/{e.message_id}"
                                  for e in c.evidence], c.content)


# ---- the segment ----

@dataclass(frozen=True)
class _Msg:
    message_id: str
    role: Literal["user", "assistant"]
    text: str
    at: datetime
    seq: int


def _messages(entries: Sequence[Entry]) -> list[list[_Msg]]:
    """Per turn (in log order): its user message and, when answered, its answer. An answer
    removed by a forgotten session (forget.PLACEHOLDER) counts as no answer: nothing is taken
    from it."""
    turns: list[list[_Msg]] = []
    for e in sorted(entries, key=lambda x: x.seq):
        if e.type == "user":
            turns.append([_Msg(str(e.payload["message_id"]), "user", e.payload.get("text") or "",
                               e.at, e.seq)])
        elif (e.type == "assistant" and turns and e.payload.get("text") != forget.PLACEHOLDER
              and not e.payload.get("stopped")):      # a stopped answer: only its question counts
            turns[-1].append(_Msg(str(e.payload["message_id"]), "assistant",
                                  e.payload.get("text") or "", e.at, e.seq))
    return turns


def _render(msgs: Sequence[_Msg]) -> str:
    who = {"user": "用户", "assistant": "助手"}
    return "\n".join(f"[{m.message_id}] {who[m.role]}：{m.text[:TEXT_MAX]}" for m in msgs)


def _active_line(it: Item) -> str:
    name = {"decision": "决定", "open_question": "问题"}[it.kind]
    scope = it.scope_type if it.scope_id is None else f"{it.scope_type}:{it.scope_id}"
    return f"- {it.item_id} [{name}，{scope}] {it.content}"


def _recalled_answers(entries: Sequence[Entry], segment: Sequence[_Msg],
                      episodes: Sequence[Episode]) -> list[Episode]:
    """The old answers the segment's turns were reminded of (the `recalled` of their answers)."""
    ids = {m.message_id for m in segment}
    keys = {str(r.get("id"))[3:] for e in entries
            if e.type == "assistant" and e.payload.get("message_id") in ids
            for r in e.payload.get("recalled") or () if str(r.get("id")).startswith("ep:")}
    return [e for e in episodes if f"{e.session_id}/{e.message_id}" in keys and e.answer_text
            and not e.answer_text.startswith(CORRECTED_ANSWER)]


def _shown(turns: Sequence[Sequence[_Msg]], forgotten: Forgotten,
           undone: Callable[[str], Sequence[str]]) -> list[_Msg]:
    """The turns as the model reads them: what he confirmed forgetting is not there to take from, and
    an answer saying 「已改好」 says too that he undid it since (run 110422344bfc: an undone limit stored
    as a decision in one trial of two, the prompt had no way to tell)."""
    out: list[_Msg] = []
    for t in turns:
        hidden = t[0].message_id in forgotten.messages
        titles = undone(t[0].message_id)
        note = f"{UNDONE_NOTE.format(titles='；'.join(titles))}\n" if titles else ""
        out += [replace(m, text=MASKED if hidden else m.text if m.role == "user"
                        else note + forgotten.answer(t[0].text, m.text)) for m in t]
    return out


def _prompt(context: Sequence[_Msg], segment: Sequence[_Msg], active: Sequence[Item],
            recalled: Sequence[Episode] = ()) -> str:
    parts = []
    if active:
        parts.append("已有的决定和问题（新的一条替代或解决了其中一条时，在 supersedes / resolves "
                     "里写它的 id）：\n" + "\n".join(_active_line(i) for i in active))
    if context:
        parts.append("上文（已经整理过，只作参考）：\n" + _render(context))
    if recalled:
        parts.append("这段对话里助手想起的旧回答（只用来看他有没有纠正，不能当证据）：\n"
                     + "\n".join(f"[{e.answer_message_id}] {e.answer_text[:TEXT_MAX]}"
                                 for e in recalled))
    parts.append("这次要整理的对话：\n" + _render(segment))
    return "\n\n".join(parts)


# ---- the checks ----

@dataclass(frozen=True)
class _Ev:
    ref: EvidenceRef
    at: datetime
    seq: int
    in_range: bool


def _check_evidence(c: Candidate, *, user_id: str, session_id: str, sessions: SessionStore,
                    in_range: set[str]) -> list[_Ev] | str:
    out: list[_Ev] = []
    for e in c.evidence:
        sid = e.session_id or session_id
        hit = sessions.find_message(sid, e.message_id)
        if hit is None or hit[1].type != e.role:
            return "evidence_missing"
        info, entry = hit
        if info.user_id != user_id:
            return "evidence_foreign"
        if e.role == "assistant" and entry.payload.get("text") == forget.PLACEHOLDER:
            return "evidence_removed"
        if e.role == "user" and not (
                e.quote and verify_quote(sessions, user_id, sid, e.message_id, e.quote)):
            return "quote_mismatch"
        out.append(_Ev(EvidenceRef(message_id=e.message_id, session_id=sid, role=e.role,
                                   quote=e.quote if e.role == "user" else None),
                       entry.at, entry.seq, sid == session_id and e.message_id in in_range))
    if not any(x.in_range for x in out):
        return "evidence_missing"
    return out


def _chars(text: str) -> set[str]:
    return {ch for ch in normalize(text) if ch.isalnum()}


def _already_landed(c: Candidate, evs: Sequence[_Ev], entries: Sequence[Entry]) -> bool:
    """That turn's remember tool already wrote this from the same message of his (a card now would
    ask for what is in force). Compared by the characters both use, so another order of the same
    words matches and a second fact in the message does not."""
    tool = LANDING_TOOLS.get(c.kind)
    if tool is None:
        return False
    mine = {x.ref.message_id for x in evs if x.ref.role == "user"}
    said, want = None, _chars(c.content)
    for e in entries:
        if e.type == "user":
            said = e.payload.get("message_id")
        elif e.type == "tool" and said in mine and e.payload.get("name") == tool and e.payload.get("ok"):
            got = _chars(str((e.payload.get("args") or {}).get("text") or ""))
            if got and want and len(got & want) / min(len(got), len(want)) >= SAME_CHARS:
                return True
    return False


def _unbacked(c: Candidate, evs: Sequence[_Ev]) -> bool:
    """A line for his profile or rules whose words are not in the words of his it quotes: the
    model took it from elsewhere (an answer it was reminded of) and pinned on a quote of something
    else (live 2026-11-05: 「口腔科住院医」 on 「我习惯先看结论，再看推导」)."""
    if c.kind not in LANDING_TOOLS:
        return False
    want = _chars(c.content)
    said = _chars(" ".join(x.ref.quote or "" for x in evs if x.ref.role == "user"))
    return bool(want) and len(want & said) / len(want) < BACKED_CHARS


def _assent(quote: str, content: str) -> bool:
    """A bare yes: his quote, without punctuation, is short and shares no character pair with
    the decision (「可以，就这样」 against 「atomic 由知识本身决定」)."""
    q = normalize(quote)
    return len(q) <= ASSENT_MAX and bigram_jaccard(q, normalize(content)) == 0


def _check_kind(c: Candidate, evs: list[_Ev], *, sessions: SessionStore, session_id: str,
                in_range: set[str]) -> list[_Ev] | str:
    """The evidence a candidate keeps, or why it is dropped (module doc, checks 2-4)."""
    users = [x for x in evs if x.ref.role == "user"]
    if not users:
        return "not_from_user"
    if c.kind != "decision" or not all(_assent(x.ref.quote or "", c.content) for x in users):
        return evs
    yes = max(users, key=lambda x: x.seq)
    sid = yes.ref.session_id or session_id
    before = [e for e in sessions.entries(sid) if e.type == "assistant" and e.seq < yes.seq
              and e.payload.get("text") != forget.PLACEHOLDER and not e.payload.get("stopped")]
    if not before:
        return "no_content"
    e = max(before, key=lambda x: x.seq)
    mid = str(e.payload["message_id"])
    plan = _Ev(EvidenceRef(message_id=mid, session_id=sid, role="assistant"), e.at, e.seq,
               sid == session_id and mid in in_range)
    return [*users, plan]


def unconsolidated(entries: Sequence[Entry], through: int) -> bool:
    """Whether an answered turn before turn index `through` is past the watermark."""
    turns = _messages(entries)
    done = watermark_seq(entries)
    return any(len(t) == 2 and t[0].seq > done for t in turns[:through])


def candidate_scope(c: Candidate, page_id: str | None = None) -> MemoryScope | None:
    """The candidate's scope; a page scope without an id is the session's page (the model is
    never shown page ids: 「这一页」 is the page the chat is on). A decision or question of a goal
    or concept without an id (the model is shown none) stays, as a global item: its text names it."""
    sid = c.scope_id or (page_id if c.scope_type == "page" else None)
    if sid is None and c.kind in ("decision", "open_question"):
        return MemoryScope(scope_type="global")
    try:
        return MemoryScope(scope_type=c.scope_type,
                           scope_id=None if c.scope_type == "global" else sid)
    except ValueError:
        return None


def _order(c: Candidate) -> int:
    if c.kind == "decision" and c.scope_type != "global":
        return 0
    return 1 if c.kind == "preference" else 2


# ---- consolidate ----

def consolidate(state: SessionState, *, sessions: SessionStore, store: MemoryStore,
                gateway: Gateway, account: Account | None, lander: PreferenceLander,
                guard: RuleGuard, first: int, through: int, now: datetime,
                new_id: Callable[[str], str], language: Language = "zh",
                embedder: Embedder | None = None, undone: UndoneOf | None = None) -> ConsolidateResult:
    """Consolidate `state`'s turns from the watermark up to `through` (exclusive turn index).
    `first` (the caller's fold start) is not used: the watermark alone says what is done, so a
    segment whose call failed is taken again by the next one. Nothing to do (already consolidated, no answered turn) -> an empty result and
    no model call. A bad reply raises ValueError (the caller logs it; the next fold retries);
    a budget error propagates."""
    info = state.info
    sid, uid = info.session_id, info.user_id
    hidden = store.hidden(uid)
    if hidden.paused or hidden.session(sid):
        return ConsolidateResult()          # being forgotten: nothing is taken from it
    entries = sessions.entries(sid)
    turns = _messages(entries)
    if through <= 0 or through > len(turns):
        raise ValueError(f"bad through {through}: the session has {len(turns)} turns")
    done = watermark_seq(entries)
    start = next((i for i, t in enumerate(turns) if t[0].seq > done), len(turns))
    answered = [i for i in range(start, through) if len(turns[i]) == 2]
    if len(answered) > SEGMENT_TURNS_MAX:
        through = answered[SEGMENT_TURNS_MAX - 1] + 1
        answered = answered[:SEGMENT_TURNS_MAX]
    segment_turns = [turns[i] for i in answered]
    if start >= through or not segment_turns:
        return ConsolidateResult()
    through_mid = turns[through - 1][0].message_id
    segment = [m for t in segment_turns for m in t]
    forgotten = forgotten_of(store, uid)

    def undone_here(message_id: str) -> Sequence[str]:
        return undone(sid, message_id) if undone is not None else ()

    context_turns = [t for t in turns[:start] if len(t) == 2][-CONTEXT_TURNS:]
    context = [m for t in context_turns for m in t]
    from evowiki.memory.lexical import rank
    candidates = list(store.items(uid, kinds=["decision", "open_question"], statuses=["active"]))
    ranked = rank(uid, 'active-items', [(i.item_id, i.content) for i in candidates],
                       '\n'.join(m.text for m in segment), k=ACTIVE_MAX)
    by_id = {i.item_id: i for i in candidates}
    related = [by_id[key] for key, _ in ranked]
    related_ids = {i.item_id for i in related}
    active = (related + [i for i in reversed(candidates)
                         if i.item_id not in related_ids and i.page_id == state.info.page_id])[:ACTIVE_MAX]
    all_episodes = list(store.episodes(uid))
    recalled = [e for e in _recalled_answers(entries, segment, all_episodes)
                if e.message_id not in forgotten.messages]
    request = LLMRequest(
        purpose=CONSOLIDATE_PURPOSE, prompt_version=CONSOLIDATE_PROMPT_VERSION, subject=sid,
        system=f"{CONSOLIDATE_SYSTEM}{language_rule(language)}",
        prompt=_prompt(_shown(context_turns, forgotten, undone_here), _shown(segment_turns, forgotten, undone_here),
                       active, recalled),
        params={"upto": segment[0].message_id, "through": through_mid, THINKING_PARAM: False})
    fast = None if account is None else account.model_copy(update={"tier_pin": "fast"})
    try:
        parsed, call = gateway.call("generator", request, Candidates,
                                    account=fast if gateway.metered else None)
    except ProviderOutputError as exc:
        raise ValueError(f"consolidation reply unusable ({exc.status}): {exc}") from exc
    except ProviderUnavailableError as exc:
        raise ValueError(f"consolidation provider unavailable: {exc}") from exc

    result = ConsolidateResult(call=call)
    in_range = {m.message_id for m in segment}
    suppressed = store.suppressions(uid)
    by_id = {i.item_id: i for i in active}
    episodes = {e.message_id: e for e in all_episodes if e.session_id == sid}
    items: list[Item] = []
    changes: list[StatusChange] = []
    prefs: list[tuple[set[str], str]] = []    # landed / waiting preferences: his messages, text
    questions = [i.content for i in active if i.kind == "open_question"]   # the same question is one card
    local: set[str] = set()                   # his messages behind kept non-global decisions
    # non-global decisions, then preferences, then the rest: a global preference from a local
    # decision's message is that decision; a global decision restating a preference is left to it
    for c in sorted(parsed.items, key=_order):
        if c.kind == "about_me":
            c = c.model_copy(update={"content": scoped_line(c.topic, c.content)})
        evs = _check_evidence(c, user_id=uid, session_id=sid, sessions=sessions,
                              in_range=in_range)
        if not isinstance(evs, str):
            evs = _check_kind(c, evs, sessions=sessions, session_id=sid, in_range=in_range)
        reason = evs if isinstance(evs, str) else None
        if reason is None and not any(x.in_range for x in evs):
            reason = "evidence_missing"
        if reason is None and _unbacked(c, evs):
            reason = "unbacked"
        if reason is None and _already_landed(c, evs, entries):
            reason = "already_landed"
        if reason is None and c.kind == "decision":
            mine = {x.ref.message_id for x in evs if x.ref.role == "user"}
            if any(mine & ids and jaccard(c.content, text) >= SAME_AS_PREF
                   for ids, text in prefs):
                reason = "same_as_preference"
        if reason is None and c.kind == "preference" and any(
                x.ref.role == "user" and x.ref.message_id in local for x in evs):
            reason = "same_as_decision"
        if reason is None and c.kind == "open_question":
            if any(jaccard(c.content, q) >= SAME_AS_PREF for q in questions):
                reason = "same_question"
            else:
                questions.append(c.content)
        scope = candidate_scope(c, info.page_id)
        if reason is None and scope is None:
            reason = "bad_scope"
        if reason is None:
            ids = {x.ref.message_id for x in evs}
            if any(s.kind in (c.kind, "episode") and ids & s.evidence_message_ids for s in suppressed):
                reason = "suppressed"
        same_as: str | None = None
        if reason is None and c.kind == "preference":     # last: the judge may be a model call
            got = guard.check(uid, c.content, scope)
            same_as, reason = (got.item_id, None) if isinstance(got, Same) else (None, got)
        if reason is not None:
            result.dropped.append((c, reason))
            continue
        assert not isinstance(evs, str) and scope is not None
        refs = tuple(x.ref for x in evs)
        concepts = tuple(dict.fromkeys(
            cid for x in evs if x.in_range and x.ref.message_id in episodes
            for cid in episodes[x.ref.message_id].concept_ids))
        valid_from = max(x.at for x in evs if x.in_range)
        landed: Landed | None = None
        if c.kind in ("preference", "about_me"):
            key = _key_of(c, sid)
            with store.unless_forgotten(uid, sid, evidence=refs, kind=c.kind) as allowed:
                if not allowed:
                    result.pending.append({"candidate_key": key, "candidate": c.model_dump(),
                                           "valid_from": valid_from.isoformat(),
                                           "page_id": info.page_id, "concept_ids": list(concepts),
                                           "reason": "suppressed"})
                    continue
                out = lander.land(uid, c, session_id=sid, provenance="explicit_history",
                                  idempotency_key=key, same_as=same_as, page_id=info.page_id)
            if c.kind == "preference" and not isinstance(out, Refused):
                prefs.append(({x.ref.message_id for x in evs if x.ref.role == "user"},
                              c.content))
            if isinstance(out, Deferred):
                result.pending.append({"candidate_key": key, "candidate": c.model_dump(),
                                       "valid_from": valid_from.isoformat(),
                                       "page_id": info.page_id, "concept_ids": list(concepts),
                                       "reason": out.reason})
                continue
            if isinstance(out, Refused):
                result.dropped.append((c, f"refused:{out.reason}"))
                continue
            landed = out
        item = Item(item_id=new_id("mem"), user_id=uid, kind=c.kind, content=c.content,
                    provenance="explicit_history", evidence=refs, scope_type=scope.scope_type,
                    scope_id=scope.scope_id, page_id=info.page_id, concept_ids=concepts,
                    session_id=sid, valid_from=valid_from, created_at=now,
                    status="active" if c.kind in ("decision", "open_question") else None,
                    spec_item_id=landed.spec_item_id if landed else None,
                    profile_ref=landed.profile_ref if landed else None,
                    pending_action_id=landed.pending_action_id if landed else None)
        items.append(item)
        if c.kind == "decision" and scope.scope_type != "global":
            local.update(x.ref.message_id for x in evs if x.ref.role == "user")
        for target, status in ((c.supersedes, "superseded"), (c.resolves, "resolved")):
            old = by_id.get(target) if target else None
            if (old is not None and old.kind == c.kind and old.scope_type == scope.scope_type
                    and old.scope_id == scope.scope_id
                    and all(ch.item_id != old.item_id for ch in changes)):
                changes.append(StatusChange(item_id=old.item_id, status=status,  # type: ignore[arg-type]
                                            by="consolidation",
                                            superseded_by=item.item_id
                                            if status == "superseded" else None))
    for target in parsed.withdrawn:
        old = by_id.get(target)
        if (old is not None and old.kind == "decision"
                and all(ch.item_id != old.item_id for ch in changes)):
            changes.append(StatusChange(item_id=old.item_id, status="superseded",
                                        by="consolidation", superseded_by=None))
    shown = {m.message_id for m in (*context, *segment) if m.role == "assistant"}
    shown |= {e.answer_message_id for e in recalled}
    wrong = [e for e in all_episodes
             if e.answer_message_id in set(parsed.corrected) & shown]
    payload = {"through_message_id": through_mid, "item_ids": [i.item_id for i in items],
               "dropped": [[c.kind, r] for c, r in result.dropped],
               "pending_landing": result.pending}
    result.committed = store.commit_consolidation(
        sessions, session_id=sid, entry_id=new_id("ent"), payload=payload, items=items,
        status_changes=changes, at=now, read_watermark=done)
    if not result.committed:
        log.info("consolidation of %s through %s already done by another run", sid, through_mid)
        return ConsolidateResult(call=call)
    result.items_written = items
    _embed(store, embedder, items)
    for e in wrong:
        mark_answer_corrected(store, embedder, e)
    return result


def _embed(store: MemoryStore, embedder: Embedder | None, items: Sequence[Item]) -> None:
    if embedder is None or not items:
        return
    try:
        vecs = embedder.embed_documents([i.content for i in items])
        for it, v in zip(items, vecs, strict=True):
            store.set_item_vector(it.item_id, v, embedder)
    except Exception:
        log.warning("memory item vectors failed (%s)", tag(embedder), exc_info=True)


# ---- pending preferences ----

def retry_pending_landings(store: MemoryStore, sessions: SessionStore, lander: PreferenceLander,
                           user_id: str, now: datetime,
                           new_id: Callable[[str], str], guard: RuleGuard | None = None) -> int:
    """Try every pending preference of this user's sessions again (a preference through `guard`
    first, as consolidation does: a rule he rejected while it waited is refused with the guard's
    reason, never landed); a landed or refused one gets
    a `consolidation` entry {"landed": [key...], "refused": [[key, reason]...]} in its session
    (and a landed one its memory item), so it is never tried again; the commit re-reads what is
    settled under the session's lock, so a concurrent retry that settled one first wins and this
    one writes nothing for that session. Returns how many landed."""
    landed_n = 0
    for info in sessions.user_sessions(user_id):
        entries = sessions.entries(info.session_id)
        settled: set[str] = set()
        pending: dict[str, dict[str, Any]] = {}
        for e in sorted(entries, key=lambda x: x.seq):
            if e.type != "consolidation":
                continue
            for p in e.payload.get("pending_landing") or ():
                pending.setdefault(p["candidate_key"], p)
            settled.update(e.payload.get("landed") or ())
            settled.update(k for k, _ in e.payload.get("refused") or ())
        landed: list[str] = []
        refused: list[list[str]] = []
        items: list[Item] = []
        for key, p in pending.items():
            if key in settled:
                continue
            c = Candidate.model_validate(p["candidate"])
            scope = candidate_scope(c, p.get("page_id"))
            why = (guard.check(user_id, c.content, scope)
                   if guard is not None and c.kind == "preference" and scope is not None
                   else None)
            same_as = why.item_id if isinstance(why, Same) else None
            if isinstance(why, str):
                refused.append([key, why])
                continue
            if scope is None:
                refused.append([key, "bad_scope"])
                continue
            with store.unless_forgotten(user_id, info.session_id,
                                        evidence=evidence_refs(c, info.session_id), kind=c.kind) as allowed:
                if not allowed:
                    continue
                out = lander.land(user_id, c, session_id=info.session_id,
                                  provenance="explicit_history", idempotency_key=key,
                                  same_as=same_as, page_id=p.get("page_id"))
            if isinstance(out, Deferred):
                continue
            if isinstance(out, Refused):
                refused.append([key, out.reason])
                continue
            landed.append(key)
            items.append(Item(
                item_id=new_id("mem"), user_id=user_id, kind=c.kind, content=c.content,
                provenance="explicit_history",
                evidence=evidence_refs(c, info.session_id),
                scope_type=scope.scope_type, scope_id=scope.scope_id,
                page_id=p.get("page_id"), concept_ids=tuple(p.get("concept_ids") or ()),
                session_id=info.session_id, valid_from=datetime.fromisoformat(p["valid_from"]),
                created_at=now, spec_item_id=out.spec_item_id, profile_ref=out.profile_ref,
                pending_action_id=out.pending_action_id))
        if not landed and not refused:
            continue
        if store.commit_consolidation(sessions, session_id=info.session_id,
                                      entry_id=new_id("ent"),
                                      payload={"landed": landed, "refused": refused},
                                      items=items, status_changes=(), at=now):
            landed_n += len(landed)
    return landed_n
