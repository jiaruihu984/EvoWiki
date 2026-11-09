"""Feedback on one review card (spec obsidian-flow §4.4). No route calls this any more (反馈 on a
card goes to the assistant's fix_card / drop_card); it stays with the 复习卡生成 step of the 参数 tab
until that step is retired. The card's 这张卡有问题… box sent the
user's own words with no kind (kind None); the older fixed kinds 题目没说清楚 (unclear), 答案不对
(wrong), 太简单了 (too_easy), 不想再看到这张卡 (drop) are still accepted.

A card is a section's recall cue (mode "cue") or its verified check (mode "check");
`review.card_id` names it. Every feedback records a `card_feedback` Signal (text = the Chinese
kind label, then the user's words; with kind None just the words; page_id / section_id of the
card) in the same commit as the action. The signal is all that is kept as evidence: no K evidence is invented (too_easy does not
count as a correct answer).

- too_easy / drop: `review_card_hidden` -> the card is skipped by today / digest / the scheduler
  for good (replayed into `WikiState.hidden_cards`). 0 LLM calls.
- kind None: the prompt states only the user's words (no fixed label) and the same ONE generator
  call decides: rewrite (below), or {"action": "hide"} when the words clearly ask to never see
  the card again -> `review_card_hidden` (kind "drop"), like drop. Any other action is
  `invalid_output` (fail closed: nothing hidden, nothing rewritten).
- unclear / wrong: ONE generator call rewrites the cue with its answer (a 「回想：」 prefix is
  stripped; one specific point of the section, a summary-style cue such as 「这一段讲的是什么」 is
  unusable) or the check (stem, options, key; options shuffled like every generated check). The
  check prompt carries the item-writing rules (goals.itemflaws.ITEM_RULES_ZH); a rewritten check
  with an item-writing flaw or too close to the old check (`near_duplicate`, rule FOLLOWUP_NEW;
  after 「答案不对」 / own words an old distractor may become the corrected key, `rekey`)
  gets ONE more call with the reasons (params attempt 2); still flawed -> `not_verified`, the old
  card kept. The new text is saved into the page (`page_saved`, action
  "card_rewrite") so the next review shows it; a rewritten check is unanswered again on the
  page. Fail closed: without a gateway, with a failed call, unusable output, an unconfirmed
  check or a commit conflict, the old card stays untouched and nothing is hidden; the signal is
  still recorded (its own small commit) and the answer says `rewritten=false` with a `reason`
  (HTTP 200: the feedback itself was taken). The cue's cache_key is kept on purpose."""

import logging
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from evowiki.belief.aspects import AspectK, AspectKView
from evowiki.goals.contracts import CheckItem, OutlineSection, SectionRole, split_cell
from evowiki.goals.diagnostic import _options, _shuffled, item_latex
from evowiki.goals.fold import Page
from evowiki.goals.guardrails import BAND_ZH
from evowiki.goals.itemflaws import ITEM_RULES_ZH, flaws_prompt, item_flaws, near_duplicate
from evowiki.goals.language import language_rule
from evowiki.goals.text import ANSWER_MAX, CUE_MAX
from evowiki.llm.run_context import traced_entry
from evowiki.teach.contracts import CardFeedbackKind
from evowiki.teach.standard import section
from evowiki.wikiapp.events import PageRecord, PageSaved, ReviewCardHidden, WikiEvent
from evowiki.wikiapp.kwrite import Ids, concept_name
from evowiki.wikiapp.lineage import card_artifact
from evowiki.wikiapp.llm import (
    LLM_BUDGET,
    LLM_FAILURES,
    LLM_UNAVAILABLE,
    CountingGateway,
    GatewayJsonLLM,
)
from evowiki.wikiapp.repo import Commit, Repo, WikiConflict, commit_signal
from evowiki.wikiapp.review import card_id, goal_concepts
from evowiki.wikiapp.sectionlog import partial_write
from evowiki.wikiapp.signals import signal_event
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.views import page_title
from evowiki.wikiapp.wiki_schemas import (
    CardFeedbackIn,
    CardFeedbackOut,
    ReviewCheckOut,
    ReviewItemOut,
)

log = logging.getLogger(__name__)

PURPOSE = "review.card_rewrite"
CARD_ACTION = "card_rewrite"
# teach.standard's review_card says a cue makes the reader recall 「这一段讲的是什么」 — the
# summary-style cue CUE_RULES forbids and `summary_cue` refuses. This module's copy replaces that
# clause with the one-specific-point wording (card-rewrite@v6); teach/ itself is not changed here.
STANDARD_CUE_CLAUSE = "不超过 35 字，读到它的人能想起这一段讲的是什么"
CUE_CLAUSE = f"不超过 {CUE_MAX} 字，读到它的人能想起正文里一个具体的点（一个结论、一个机制或一个公式的含义），答案只有一个"


def _review_card_standard() -> str:
    return section("review_card").replace(STANDARD_CUE_CLAUSE, CUE_CLAUSE)


# Teaching standard injected per prompt: the cue gets the review-card rule + spoken-first; the
# check gets the review-card rule only (its recall rule is written in the prompt).
CUE_STANDARD = "\n\n".join([_review_card_standard(), section("spoken_first")])
CHECK_STANDARD = _review_card_standard()
CUE_PREFIXES = ("回想：", "回想:")
# A cue that asks for a summary of the section, not one specific point (Matuschak / SuperMemo:
# one thing per card, a precise cue with one stable answer) is unusable.
SUMMARY_CUE_WORDS = ("讲的是什么", "讲了什么", "主要内容", "这一段", "这一节", "本段", "本节")
CHECK_ATTEMPTS = 2                             # the rewrite, then once more with its flaws
# Matuschak / SuperMemo card rules, paraphrased (wheels-reuse spec §三 复习卡).
CUE_RULES = """回想提示的规则：
⟦g:card⟧- 一张卡只问一件事：只针对这一段里一个具体的点（一个结论、一个机制、一个公式的含义），不要概括整段。
- 问法要精确：懂的人看到它，能想起的答案只有一个，而且每次复习想起的都是同一个答案。
- 不要写「这一段讲的是什么」「本节主要内容」这类总结式问题，也不要只给一个术语让他认。
- 几天后单独看到也能读懂、能回答：不靠这一页的例子和编号，不自带答案。⟦/g⟧"""
BODY_MAX = 1500
EVIDENCE_NOTES = 5                             # the latest answers / notes shown per cell
KIND_LABEL: dict[CardFeedbackKind, str] = {
    "unclear": "题目没说清楚", "wrong": "答案不对", "too_easy": "太简单了", "drop": "不想再看到这张卡"}
Mode = Literal["cue", "check"]
Reason = Literal["llm_unavailable", "llm_failed", "budget_exceeded", "invalid_output",
                 "not_verified", "conflict"]


class Unusable(Exception):
    """The rewrite is not usable; carries the response `reason`."""

    def __init__(self, reason: Reason):
        super().__init__(reason)
        self.reason = reason


class _CueOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str = "rewrite"                    # own words only: "rewrite" | "hide"
    cue: str = ""
    answer: str = ""


class _CheckOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str = "rewrite"                    # own words only: "rewrite" | "hide"
    stem: str = ""
    options: list[str | int | float] = []
    answer: int = -1


class Card:
    """A resolved, not hidden card of an organized page."""

    def __init__(self, rec: PageRecord, page: Page, section: OutlineSection, mode: Mode):
        self.rec, self.page, self.section, self.mode = rec, page, section, mode

    @property
    def cell(self) -> str:
        if self.mode == "check" and self.section.check is not None:
            return self.section.check.cell
        return self.section.cells[0] if self.section.cells else ""


def find_card(state: WikiState, cid: str) -> Card | None:
    """The card with this id over every organized, not trashed page (a cue card needs a cue and an
    answer, a check card a verified item); None when unknown or hidden."""
    if cid in state.hidden_cards:
        return None
    for pid in state.page_order:
        rec = state.pages[pid]
        page = rec.page
        if page is None or pid in state.trashed_pages:
            continue
        for s in page.outline.sections:
            t = page.texts.get(s.id)
            if (t is not None and t.cue.strip() and t.answer.strip()
                    and card_id(pid, s.id, "cue") == cid):
                return Card(rec, page, s, "cue")
            if s.check is not None and s.check.verified and card_id(pid, s.id, "check") == cid:
                return Card(rec, page, s, "check")
    return None


class Hide(Exception):
    """Own words only: the model read them as a clear ask to never show the card again."""


def signal_text(body: CardFeedbackIn) -> str:
    words = body.text.strip()
    if body.kind is None:
        return words
    label = KIND_LABEL[body.kind]
    return f"{label}：{words}" if words else label


def _complaint(body: CardFeedbackIn) -> str:
    words = body.text.strip()
    if body.kind is None:                      # the user's own words, no fixed label
        return f"他对这张卡说的话：{words}"
    return f"他的反馈：{KIND_LABEL[body.kind]}" + (f"。他的话：{words}" if words else "")


def _decide(body: CardFeedbackIn) -> str:
    """Own words only: the model also decides between rewriting and hiding the card."""
    if body.kind is not None:
        return ""
    return ("\n先读懂他的话，再决定怎么做：通常是按他的话重写这张卡；只有他明确说不想再看到、"
            "要删掉这张卡时，才不重写，只回复 JSON {\"action\": \"hide\"}。")


def _action(out: "_CueOut | _CheckOut", body: CardFeedbackIn) -> None:
    if body.kind is not None or out.action == "rewrite":
        return
    if out.action == "hide":
        raise Hide()
    raise Unusable("invalid_output")


def _cue_format(body: CardFeedbackIn) -> str:
    if body.kind is None:
        return '{"action": "rewrite", "cue": "…", "answer": "…"}'
    return '{"cue": "…", "answer": "…"}'


def _check_format(body: CardFeedbackIn) -> str:
    fields = '"stem": "…", "options": ["…", "…", "…"], "answer": 正确选项的序号（从 0 开始）'
    return "{" + ('"action": "rewrite", ' if body.kind is None else "") + fields + "}"


def _body_of(card: Card, sid: str) -> str:
    t = card.page.texts.get(sid)
    return t.body[:BODY_MAX] if t is not None else ""


def cell_evidence(kv: AspectKView, cell: str) -> str:
    """What K already holds on one cell, for a prompt that asks the learner again (rule ASK): the
    band and his latest answers / notes on that cell (a note's display label when it has one)."""
    cid, _ = split_cell(cell)
    notes = [n.label or n.text for n in kv.note(cid) if n.cell == cell][-EVIDENCE_NOTES:]
    return (f"系统对他这一格的判断：{BAND_ZH[kv.cell(cell).band]}\n他在这一格上的作答和记录："
            + ("".join(f"\n- {x}" for x in notes) if notes else "（暂无）"))


def _cue_prompt(state: WikiState, card: Card, body: CardFeedbackIn) -> str:
    t = card.page.texts[card.section.id]
    return f"""你在改一张复习卡。这张卡是 Wiki 页面「{card.rec.title}」中「{card.section.heading}」一段的回想提示：复习时先只给他看这一句，他自己回想出答案，再对照答案自评。

这一段的正文：
{_body_of(card, card.section.id)}

现在的回想提示：{t.cue}
现在的答案：{t.answer or "（没有）"}
{_complaint(body)}

{CUE_STANDARD}

{CUE_RULES}
{_decide(body)}
重写这张卡。cue 问的是“你能不能自己说出来”：从正文里挑一个具体的点，让他回想并开口说出那个机制或结论，而不是认出一个术语或照着念一个定义。不超过 {CUE_MAX} 字，只写一句话。answer 用一两句话回答 cue，不超过 {ANSWER_MAX} 字。都要和正文一致，不要和现在的一模一样。
{language_rule(state.language)}
输出格式：只回复 JSON {_cue_format(body)}。"""


def _check_prompt(state: WikiState, card: Card, body: CardFeedbackIn, evidence: str,
                  flaws: tuple[str, ...] = ()) -> str:
    """`evidence` is `cell_evidence` for the card's cell; `flaws` what code found in the previous
    attempt."""
    item = card.section.check
    assert item is not None
    cid, aspect = split_cell(item.cell)
    related = "\n\n".join(
        f"【{s.heading}】{_body_of(card, s.id)}" for s in card.page.outline.sections
        if s.role is not SectionRole.CHECK and item.cell in s.cells and _body_of(card, s.id))
    options = "\n".join(f"{j}) {o}" for j, o in enumerate(item.options))
    return f"""你在改一道复习用的单选确认题。它所在的页面是「{card.rec.title}」，测的是「{concept_name(state, cid)}」的 {aspect}。

相关正文：
{related or "（无）"}

现在的题：
{item.stem}
{options}
（现在的标准答案是第 {item.answer} 项）
{_complaint(body)}

他在这个知识点上已有的证据（新题不要再测这些作答已经说明了的东西）：
{evidence}

{CHECK_STANDARD}

{ITEM_RULES_ZH}
{_decide(body)}
重写这道题：它仍是单选，但考的是“你能不能自己说出来”，不是认词：题干给一个具体的情境或输入，让他先在心里自己推出结果，再去选；不要问“下列哪个是 X 的定义”。只考一个知识点；题干说清楚问的是什么，只有一个明确正确的选项，其余选项是常见的错误想法（对应真实的误解）；3 到 6 个选项；测的仍是同一个知识点。他的反馈可能对也可能不对：请自己判断什么才是正确的，不要只顺着他说。不要和现在的题一模一样：{_reuse_rule(body)}{flaws_prompt(flaws)}
{language_rule(state.language)}
输出格式：只回复 JSON {_check_format(body)}。"""


def _rekey(body: CardFeedbackIn) -> bool:
    """「答案不对」 or the learner's own words (they may say the key is wrong): the rewrite may
    correct the key, so an old distractor may become the right answer (rule FOLLOWUP_NEW)."""
    return body.kind != "unclear"


def _reuse_rule(body: CardFeedbackIn) -> str:
    if not _rekey(body):
        return "题干和选项都要换，现在的错误选项不要再用。"
    return ("如果他说得对、现在的标准答案确实错了，可以把原来的某个错误选项改成正确答案（这时题干和选项可以保留，"
            "只改答案序号）；否则题干和选项都要换。新题里的错误选项不要沿用现在的错误选项。")


def summary_cue(cue: str) -> bool:
    """A summary-style cue (「这一段讲的是什么」), not one specific point."""
    return any(w in cue for w in SUMMARY_CUE_WORDS)


def rewrite_cue(llm: GatewayJsonLLM, state: WikiState, card: Card,
                 body: CardFeedbackIn) -> Page:
    out = llm("generator", PURPOSE, _cue_prompt(state, card, body), _CueOut)
    _action(out, body)
    cue = " ".join(out.cue.split())
    for p in CUE_PREFIXES:
        if cue.startswith(p):
            cue = cue[len(p):].strip()
    answer = out.answer.strip()
    old = card.page.texts[card.section.id]
    if (not cue or len(cue) > CUE_MAX or not answer or len(answer) > ANSWER_MAX
            or (cue, answer) == (old.cue.strip(), old.answer.strip()) or summary_cue(cue)):
        raise Unusable("invalid_output")
    texts = {**card.page.texts, old.section_id: old.model_copy(update={"cue": cue, "answer": answer})}
    return card.page.model_copy(update={"texts": texts})


def rewrite_check(llm: GatewayJsonLLM, state: WikiState, card: Card,
                   body: CardFeedbackIn, kv: AspectKView) -> Page:
    old = card.section.check
    assert old is not None
    evidence = cell_evidence(kv, old.cell)
    flaws: tuple[str, ...] = ()
    for attempt in range(1, CHECK_ATTEMPTS + 1):
        out = llm("generator", PURPOSE, _check_prompt(state, card, body, evidence, flaws), _CheckOut,
                  params={"attempt": attempt})
        _action(out, body)
        options = _options(out.options)
        if options is None:
            raise Unusable("invalid_output")
        try:
            item = CheckItem(cell=old.cell, stem=out.stem.strip(), options=options,
                             answer=out.answer, verified=False)
        except ValidationError:
            raise Unusable("invalid_output") from None
        if (item.stem, sorted(item.options), item.options[item.answer]) == (
                old.stem, sorted(old.options), old.options[old.answer]):
            raise Unusable("invalid_output")            # nothing changed
        flaws = item_flaws(item) + near_duplicate(item, old, rekey=_rekey(body))
        if item_latex(item) is not None:                # plain text only (diagnostic.latex_in)
            flaws = flaws + ("题目里用了 LaTeX 记号",)
        if not flaws:
            break
        log.warning("card rewrite %s attempt %d: flawed check: %s", card.section.id, attempt,
                    "；".join(flaws))
    else:
        raise Unusable("not_verified")                  # flawed twice: the old card stays
    # the key is not always first; structural + item-flaw checks only, no verification call
    item = _shuffled(item).model_copy(update={"verified": True})
    sections = tuple(s.model_copy(update={"check": item}) if s.id == card.section.id else s
                     for s in card.page.outline.sections)
    return card.page.model_copy(update={
        "outline": card.page.outline.model_copy(update={"sections": sections})})


def _wev(new_id: Ids, now: datetime, body: ReviewCardHidden | PageSaved) -> WikiEvent:
    return WikiEvent(event_id=new_id("wev"), ts=now, body=body)


def card_out(state: WikiState, k: AspectK, card: Card, now: datetime,
             user_id: str = "") -> ReviewItemOut:
    """The card as the review list shows it. recall_now is the cell's current number; the card
    is not scheduled here, so days_to_threshold is 0.0 and cells is its lead cell only."""
    cell = card.cell
    cid, aspect = split_cell(cell)
    b = AspectKView(k, now).cell(cell)
    item = card.section.check if card.mode == "check" else None
    return ReviewItemOut(
        card_id=card_id(card.rec.page_id, card.section.id, card.mode), cell=cell, cells=[cell],
        concept_id=cid,
        concept_name=concept_name(state, cid, user_id=user_id), aspect=aspect, band=b.band,
        mode=card.mode, page_id=card.rec.page_id,
        page_title=page_title(state, card.rec.page_id, user_id=user_id),
        section_id=card.section.id, heading=card.section.heading,
        cue=(card.page.texts[card.section.id].cue if card.mode == "cue" else ""),
        answer=(card.page.texts[card.section.id].answer if card.mode == "cue" else ""),
        check=(None if item is None
               else ReviewCheckOut(stem=item.stem, options=list(item.options))),
        recall_now=b.recall, days_to_threshold=0.0, in_goal=cid in goal_concepts(state),
        artifact_id=(card_artifact(user_id, card.rec.page_id, card.section.id,
                                   card.section.heading, card.mode, check=item,
                                   cue=(card.page.texts[card.section.id].cue.strip()
                                        if card.mode == "cue" else ""))
                     if user_id else None))


@traced_entry("card", subject="cid")
def card_feedback(repo: Repo, gw: CountingGateway | None, user_id: str, cid: str,
                  body: CardFeedbackIn, *, now: datetime, new_id: Ids) -> CardFeedbackOut:
    """One feedback on one card. Unknown / hidden card -> LookupError (404). See the module doc
    for what each kind does and for the fail-closed rewrite."""
    snap = repo.load(user_id)
    card = find_card(snap.state, cid)
    if card is None:
        raise LookupError(f"没有这张复习卡：{cid}")
    page_id, sid = card.rec.page_id, card.section.id
    signal = signal_event(snap.state, "card_feedback", now=now, new_id=new_id, page_id=page_id,
                          section_id=sid, text=signal_text(body), target=body.artifact_id)
    sig = signal.body
    assert hasattr(sig, "signal")
    sig_id = sig.signal.signal_id

    def out(*, hidden: bool = False, rewritten: bool = False, **kw) -> CardFeedbackOut:
        return CardFeedbackOut(card_id=cid, kind=body.kind, signal_id=sig_id, hidden=hidden,
                               rewritten=rewritten, **kw)

    if body.kind in ("too_easy", "drop"):
        repo.commit(snap, Commit(wiki=[
            _wev(new_id, now, ReviewCardHidden(card_id=cid, kind=body.kind)), signal]))
        return out(hidden=True)
    if gw is None:
        commit_signal(repo, user_id, signal)
        return out(reason="llm_unavailable")
    llm = GatewayJsonLLM(gw, subject=f"review-card:{page_id}:{sid}")
    try:
        page = (rewrite_cue(llm, snap.state, card, body) if card.mode == "cue"
                else rewrite_check(llm, snap.state, card, body, snap.kview(now)))
        record = card.rec.model_copy(update={"page": page})
        if card.mode == "check":                         # a new question: unanswered again
            record = record.model_copy(update={
                "checks": {k: v for k, v in card.rec.checks.items() if k != sid}})
        record, logs = partial_write(snap, record, [sid], kind="card", ref=cid,
                                     producer="writer", now=now, new_id=new_id)
        snap2 = repo.commit(snap, Commit(wiki=[
            _wev(new_id, now, PageSaved(record=record, action=CARD_ACTION)), *logs, signal]))
    except Hide:
        try:
            repo.commit(snap, Commit(wiki=[
                _wev(new_id, now, ReviewCardHidden(card_id=cid, kind="drop")), signal]))
        except WikiConflict:
            commit_signal(repo, user_id, signal)
            return out(reason="conflict")
        return out(hidden=True)
    except Unusable as exc:
        commit_signal(repo, user_id, signal)
        return out(reason=exc.reason)
    except WikiConflict:
        commit_signal(repo, user_id, signal)
        return out(reason="conflict")
    except LLM_BUDGET:
        commit_signal(repo, user_id, signal)
        return out(reason="budget_exceeded")
    except LLM_UNAVAILABLE:
        commit_signal(repo, user_id, signal)
        return out(reason="llm_unavailable")
    except LLM_FAILURES:
        commit_signal(repo, user_id, signal)
        return out(reason="llm_failed")
    fresh = find_card(snap2.state, cid)
    return out(rewritten=True,
               card=None if fresh is None else card_out(snap2.state, snap2.k, fresh, now, user_id))
