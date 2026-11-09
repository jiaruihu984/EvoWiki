"""Wrong-answer follow-up: after a wrong answer (a page check, or a review card in check or cue
mode) the learner says why — 粗心选错了 (slip), 我学会了 (learned) or 看不懂 (unclear).

POST /wiki/pages/{id}/checks/{sid}/followup finds the wrong answer (`answer_ref`, else the latest
wrong quiz / check / recall answer whose evidence_ref is `check:{page}:{sid}` or
`review:{page}:{sid}:…`); an answer gets one follow-up (again → 409). An EvoUI block answer
(`block:…`, wikiapp.block_results) is named by its answer_ref; its question comes from the
answer's note label.

- slip / learned: ONE generator call writes a NEW question on the same cell and the same construct
  (never the original: an unchanged stem or option set is refused), shuffled, no verifier call.
  The prompt carries what he picked, why he says he missed it and K on the cell (rule ASK), the
  item-writing rules (goals.itemflaws.ITEM_RULES_ZH) and the previous item; a question with an
  item-writing flaw (repeated options included: 「选项重复」, `itemflaws.judge_check`) or too close to
  the previous one (`near_duplicate`, rule FOLLOWUP_NEW) gets
  ONE more call with the reasons (params attempt 2); still flawed -> LLMFailed (502, the reasons
  in its detail, logged), nothing recorded.
  Then the K `Claim` (slip: a share of the wrong answer is set aside; learned: a weak positive,
  「待确认」) and the follow-up with its confirmation question, due CONFIRM_DELAY later in
  GET /wiki/review/today `confirms`. Fail closed: no gateway (503), a failed call (502) or a
  question that is unusable / not verified (502) records nothing. 不知道 cannot be a slip (422).
- unclear: 0 calls. A `-` self observation (「看不懂」), a `check_unclear` signal on the section that
  teaches the cell (RSI-B: that teaching did not land), and what the UI needs: where to go back to
  (`review`; `suggest` when no page teaches the cell) and the question as a quote for the
  assistant (`quote`).

POST /wiki/review/confirms/{id} answers the confirmation question: a normal `check` observation
(evidence_ref `confirm:{id}`), then `ResolveClaim` — correct → confirmed (the claim's effect
stays), else refuted (a slip's set-aside evidence comes back; a learned positive is taken away).
A refuted 「我学会了」 means 看懂没学会: a `check_unclear` signal on the teaching section and
`reteach` for the UI. Both answers run the one K-change pass on the page of the original answer.

RSI-A learns how far to trust this user's claims: the two trust levels are θ_infer,u paths
(`evidence_weight.slip_claim` / `.learned_claim`) and confirmation questions are predicted under
their claim kind (teach/predict.py)."""

import logging
from datetime import datetime, timedelta

from evowiki.belief.aspects import (
    AddNote,
    AspectEvent,
    AspectKView,
    Claim,
    Observe,
    ResolveClaim,
)
from evowiki.goals.contracts import (
    CheckItem,
    Observation,
    OutlineSection,
    SectionRole,
    split_cell,
)
from evowiki.goals.diagnostic import (
    IDK,
    _options,
    _shuffled,
    answer_observation,
    is_idk,
    item_latex,
)
from evowiki.goals.guardrails import section_of
from evowiki.goals.itemflaws import ITEM_RULES_ZH, flaws_prompt, judge_check, near_duplicate
from evowiki.goals.language import language_rule
from evowiki.i18n import current_language
from evowiki.llm.run_context import traced_entry
from evowiki.wikiapp.events import (
    Followup,
    FollowupResolved,
    FollowupScheduled,
    PageRecord,
    WikiEvent,
)
from evowiki.wikiapp.kchange import k_changed
from evowiki.wikiapp.kwrite import Ids, apply_in_memory, concept_name, observation_events
from evowiki.wikiapp.llm import (
    CountingGateway,
    GatewayJsonLLM,
    LLMFailed,
    LLMUnavailable,
)
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict, WikiInvalid
from evowiki.wikiapp.review_cards import _CheckOut, cell_evidence
from evowiki.wikiapp.signals import IDK_TEXT, signal_event
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.views import page_title
from evowiki.wikiapp.wiki_schemas import (
    AssistantQuoteOut,
    CellChangeOut,
    CheckFollowupIn,
    CheckFollowupOut,
    ConfirmAnswerOut,
    ReviewTargetOut,
    SuggestedPageOut,
)

log = logging.getLogger(__name__)

PURPOSE = "review.followup_check"
FOLLOWUP_ACTION = "followup"
COMMIT_TRIES = 3
CONFIRM_DELAY = timedelta(hours=1)      # 「稍后」: the confirmation question is not shown at once
TEST_KINDS = frozenset({"quiz", "check", "recall"})
UNCLEAR_QUOTE = "看不懂"
BODY_MAX = 1500
ALREADY = "这道题已经选过「为什么答错」了"
QUESTION_ATTEMPTS = 2                   # the question, then once more with its flaws
# why he says he missed it, as the confirmation prompt states it (rule ASK)
CLAIM_ZH = {"slip": "粗心选错了——他说自己其实会", "learned": "刚才不会，看了讲解现在学会了"}


# ---- finding things ----

def _wrong_answer(events: list[AspectEvent], page_id: str, sid: str,
                  answer_ref: str | None) -> Observe:
    refs = (f"check:{page_id}:{sid}", f"review:{page_id}:{sid}:")

    def mine(o: Observation) -> bool:
        if answer_ref is not None:
            return o.evidence_ref == answer_ref
        return o.evidence_ref == refs[0] or o.evidence_ref.startswith(refs[1])

    hits = [e for e in events if isinstance(e, Observe) and mine(e.observation)]
    if not hits:
        raise LookupError(f"这一段没有找到这次作答：{answer_ref or sid}")
    ev = hits[-1]
    o = ev.observation
    if o.kind not in TEST_KINDS or o.polarity == "+":
        raise WikiInvalid("这次作答没有答错")
    return ev


def _note(events: list[AspectEvent], ref: str) -> AddNote | None:
    """The answer's note: text = the chosen option (the stem for 不知道, the self-grade); for an
    EvoUI block answer the label also carries the question and the key."""
    return next((e for e in reversed(events) if isinstance(e, AddNote) and e.evidence_ref == ref),
                None)


def _block_label(note: AddNote | None, ref: str) -> str | None:
    return note.label if note is not None and ref.startswith("block:") else None


def _teaching(state: WikiState, page_id: str, cell: str) -> tuple[PageRecord, OutlineSection] | None:
    """The prose section that teaches `cell`: on the answer's page first, then any organized page
    in page order."""
    order = [page_id, *(p for p in state.pages if p != page_id)]
    for pid in order:
        rec = state.pages.get(pid)
        if rec is None or rec.page is None:
            continue
        for s in rec.page.outline.sections:
            if s.role is not SectionRole.CHECK and cell in s.cells:
                return rec, s
    return None


def _target(state: WikiState, rec: PageRecord, s: OutlineSection | None,
            user_id: str) -> ReviewTargetOut:
    return ReviewTargetOut(page_id=rec.page_id,
                           page_title=page_title(state, rec.page_id, user_id=user_id),
                           section_id=s.id if s else None, heading=s.heading if s else "")


def _quote(item: CheckItem | None, chosen: str, cue: str, heading: str | None,
           block: str | None = None) -> AssistantQuoteOut:
    if block is not None:                   # an EvoUI block question: its note label
        text = block
    elif item is not None:
        options = "\n".join(f"{j + 1}) {o}" for j, o in enumerate(item.options))
        text = (f"题目：{item.stem}\n选项：\n{options}\n我选的：{chosen or IDK_TEXT}\n"
                f"正确答案：{item.options[item.answer]}")
    else:                                   # a cue card: the recall cue and the self-grade
        text = f"回想提示：{cue}\n我的回想：{chosen}"
    if current_language() == "en" and block is None:
        if item is not None:
            text = (f"Question: {item.stem}\nOptions:\n{options}\nMy answer: {chosen or 'I do not know'}\n"
                    f"Correct answer: {item.options[item.answer]}")
        else:
            text = f"Recall prompt: {cue}\nMy recall: {chosen}"
    return AssistantQuoteOut(kind="wrong_answer" if item is not None else "text", text=text[:4000], heading=(heading or None) and heading[:300])


def _band(snap: Snapshot, k_after, cell: str, now: datetime) -> CellChangeOut:
    return CellChangeOut(cell=cell, **{"from": snap.kview(now).cell(cell).band},
                         to=AspectKView(k_after, now).cell(cell).band)


def _wev(new_id: Ids, now: datetime, body) -> WikiEvent:
    return WikiEvent(event_id=new_id("wev"), ts=now, body=body)


# ---- the confirmation question ----

def _picked(item: CheckItem | None, note: AddNote | None, idk: bool, block: str | None) -> str:
    """What he answered on the original question, as one prompt line."""
    if idk:
        return f"他选的：{IDK_TEXT}"
    text = note.text if note is not None else "（没有记录）"
    return f"他的回想：{text}" if item is None and block is None else f"他选的：{text}"


def _confirm_prompt(state: WikiState, rec: PageRecord, item: CheckItem | None, cell: str,
                    teach: tuple[PageRecord, OutlineSection] | None,
                    block: str | None = None, flaws: tuple[str, ...] = (), *, picked: str,
                    claim: str, evidence: str) -> str:
    """`picked` is `_picked`, `claim` a CLAIM_ZH value, `evidence` `cell_evidence` for the cell
    (read before the claim is recorded); `flaws` what code found in the previous attempt."""
    cid, aspect = split_cell(cell)
    body = ""
    if teach is not None and teach[0].page is not None:
        t = teach[0].page.texts.get(teach[1].id)
        body = t.body[:BODY_MAX] if t is not None else ""
    old = (block if block is not None else "（原题是一张回想卡，没有选项）" if item is None else
           f"{item.stem}\n" + "\n".join(f"{j}) {o}" for j, o in enumerate(item.options)))
    return f"""他刚才在「{rec.title}」答错了一道关于「{concept_name(state, cid)}」的 {aspect} 的题，过一会儿要用一道新题确认他是否真的会。

相关正文：
{body or "（无）"}

原来的题（新题不能和它一样，也不要只是换个说法或调换选项）：
{old}
{picked}
他说为什么答错：{claim}

他在这个知识点上已有的证据（新题不要再测这些作答已经说明了的东西）：
{evidence}

出一道新的单选题：⟦g:followup⟧测的仍是同一个知识点，换一个角度或情境，要能看出他的说法是否属实；考的是同一件事（同一个格子、同一个要点，难度相当），但题干要换、选项要换，原题里的错误选项不要再用；⟦/g⟧题干说清楚问的是什么，只有一个明确正确的选项，其余选项是常见的错误想法；3 到 6 个选项。

{ITEM_RULES_ZH}{flaws_prompt(flaws)}
{language_rule(state.language)}
输出格式：只回复 JSON {{"stem": "…", "options": ["…", "…", "…"], "answer": 正确选项的序号（从 0 开始）}}。"""


def _new_question(gw: CountingGateway, state: WikiState, rec: PageRecord, sid: str,
                  item: CheckItem | None, cell: str,
                  teach: tuple[PageRecord, OutlineSection] | None,
                  block: str | None = None, *, picked: str, claim: str,
                  evidence: str) -> CheckItem:
    llm = GatewayJsonLLM(gw, subject=f"followup:{rec.page_id}:{sid}")
    flaws: tuple[str, ...] = ()
    for attempt in range(1, QUESTION_ATTEMPTS + 1):
        out = llm("generator", PURPOSE,
                  _confirm_prompt(state, rec, item, cell, teach, block, flaws, picked=picked,
                                 claim=claim, evidence=evidence), _CheckOut,
                  params={"attempt": attempt})
        options = _options(out.options)
        if options is None:
            raise LLMFailed("确认题格式不对")
        # repeated options come back as a flaw (one more attempt), not as a format error
        built, flaws = judge_check(cell, out.stem, options, out.answer)
        if built is None and not flaws:
            raise LLMFailed("确认题格式不对")
        if built is not None:
            new = built
            if item_latex(new) is not None:             # plain text only (diagnostic.latex_in)
                flaws = flaws + ("题目里用了 LaTeX 记号",)
            # near_duplicate also refuses the original itself (same stem, same option set)
            flaws = flaws + (near_duplicate(new, item) if item is not None else ())
        if not flaws:
            break
        log.warning("follow-up %s:%s attempt %d: flawed question: %s", rec.page_id, sid,
                    attempt, "；".join(flaws))
    else:
        raise LLMFailed("确认题两次都不合格：" + "；".join(flaws))
    # structural + item-flaw checks only (CheckItem above); no separate verification call
    return _shuffled(new).model_copy(update={"verified": True})


def _known_prompt(state: WikiState, node_id: str, cell: str, evidence: str,
                  flaws: tuple[str, ...]) -> str:
    cid, aspect = split_cell(cell)
    rec = state.graph.node(node_id)
    definition = ("\n它的定义：" + rec.definition) if rec is not None and rec.definition else ""
    return f"""他说「{concept_name(state, cid)}」他早就会了。要用一道题确认他是不是真的会，测的是这个知识点的 {aspect}。{definition}

他在这个知识点上已有的证据（新题不要再测这些作答已经说明了的东西）：
{evidence}

出一道单选题：只测这一个知识点的这一个方面，不考别的；题干说清楚问的是什么，只有一个明确正确的选项，其余选项是常见的错误想法；3 到 6 个选项。真的会的人应该一眼答得对，没学过的人会被错误选项吸引。

{ITEM_RULES_ZH}{flaws_prompt(flaws)}
{language_rule(state.language)}
输出格式：只回复 JSON {{"stem": "…", "options": ["…", "…", "…"], "answer": 正确选项的序号（从 0 开始）}}。"""


def node_confirm_item(gw: CountingGateway, snap: Snapshot, node_id: str, cell: str, *,
                      now: datetime) -> CheckItem:
    """The ONE question after the assistant records 「这个我早会了」 (assistant action
    `node_known`): a verified `CheckItem` on `cell` of `node_id`, written by one generator call
    (a second one with the reasons when it has item-writing flaws, like the follow-up question;
    still flawed or unusable -> LLMFailed). K is read as it is BEFORE the declaration is
    recorded (rule ASK)."""
    llm = GatewayJsonLLM(gw, subject=f"known:{node_id}")
    evidence = cell_evidence(snap.kview(now), cell)
    flaws: tuple[str, ...] = ()
    for attempt in range(1, QUESTION_ATTEMPTS + 1):
        out = llm("generator", PURPOSE, _known_prompt(snap.state, node_id, cell, evidence, flaws),
                  _CheckOut, params={"attempt": attempt})
        options = _options(out.options)
        if options is None:
            llm.refused("确认题格式不对")
            raise LLMFailed("确认题格式不对")
        built, flaws = judge_check(cell, out.stem, options, out.answer)
        if built is None and not flaws:
            llm.refused("确认题格式不对")
            raise LLMFailed("确认题格式不对")
        if built is not None and item_latex(built) is not None:
            flaws = flaws + ("题目里用了 LaTeX 记号",)
        if not flaws:
            assert built is not None
            return _shuffled(built).model_copy(update={"verified": True})
        llm.refused("；".join(flaws))
        log.warning("known-node confirm %s attempt %d: flawed question: %s", node_id, attempt,
                    "；".join(flaws))
    raise LLMFailed("确认题两次都不合格：" + "；".join(flaws))


# ---- the two endpoints ----

@traced_entry("followup", subject="page_id")
def followup(repo: Repo, gw: CountingGateway | None, user_id: str, page_id: str, sid: str,
             body: CheckFollowupIn, *, now: datetime, new_id: Ids) -> CheckFollowupOut:
    """See the module doc. 404 unknown page / section / answer; 422 not a wrong answer, or a slip
    on 不知道; 409 this answer already has a follow-up; 503 / 502 slip / learned without a usable
    confirmation question (nothing recorded)."""
    snap = repo.load(user_id)
    state = snap.state
    rec = state.pages.get(page_id)
    if rec is None:
        raise LookupError(f"没有这个页面：{page_id}")
    if rec.page is None or all(s.id != sid for s in rec.page.outline.sections):
        raise LookupError(f"没有这一段：{sid}")
    section = section_of(rec.page.outline, sid)
    events = list(repo.aspect_events(user_id))
    wrong = _wrong_answer(events, page_id, sid, body.answer_ref)
    o = wrong.observation
    if any(f.source_event_id == wrong.event_id for f in state.followups.values()):
        raise WikiConflict(ALREADY)
    idk = is_idk(o)
    if body.choice == "slip" and idk:
        raise WikiInvalid("选了「不知道」不算粗心")
    cell = o.cell
    cid, _ = split_cell(cell)
    item = section.check if section.check is not None and section.check.cell == cell else None
    teach = _teaching(state, page_id, cell)
    note = _note(events, o.evidence_ref)
    block_q = _block_label(note, o.evidence_ref)
    fid = new_id("fu")
    calls0 = gw.calls if gw is not None else 0
    base = {"followup_id": fid, "choice": body.choice, "cell": cell, "page_id": page_id, "section_id": sid,
                "answer_ref": o.evidence_ref, "source_event_id": wrong.event_id, "created_at": now,
                "teach_page_id": teach[0].page_id if teach else None,
                "teach_section_id": teach[1].id if teach else None}
    name = concept_name(state, cid, user_id=user_id)

    if body.choice == "unclear":
        obs = Observation(cell=cell, polarity="-", strength=1.0, kind="self",
                          evidence_ref=f"followup:{fid}", quote=UNCLEAR_QUOTE)
        aspect = observation_events([obs], ts=now, new_id=new_id,
                                    note_text=lambda _o: UNCLEAR_QUOTE)
        k_after = apply_in_memory(snap, aspect)
        kc = k_changed(snap, k_after, page_id, gw, skip=None, now=now,
                       new_id=new_id, action=FOLLOWUP_ACTION)
        chosen = IDK_TEXT if idk else note.text if note is not None else ""
        cue = rec.page.texts[sid].cue if sid in rec.page.texts else ""
        heading = teach[1].heading if teach else section.heading
        quote = _quote(item, chosen, cue, heading, block_q)
        signal = signal_event(state, "check_unclear", now=now, new_id=new_id,
                              page_id=teach[0].page_id if teach else page_id,
                              section_id=teach[1].id if teach else sid, text=quote.text[:2000],
                              target=body.artifact_id)
        repo.commit(snap, Commit(aspect=aspect, wiki=[
            *kc.wiki, _wev(new_id, now, FollowupScheduled(followup=Followup(**base))), signal]))
        sig = signal.body
        assert hasattr(sig, "signal")
        return CheckFollowupOut(
            followup_id=fid, choice="unclear", cell=cell, concept_name=name, status="done",
            band=_band(snap, k_after, cell, now),
            review=_target(state, *teach, user_id) if teach else None,
            suggest=None if teach else SuggestedPageOut(concept_id=cid, concept_name=name,
                                                        goal=f"弄懂「{name}」"),
            quote=quote, signal_id=sig.signal.signal_id,
            llm_calls=(gw.calls - calls0) if gw is not None else 0)

    if gw is None:
        raise LLMUnavailable("请切换模型重试")
    question = _new_question(gw, state, rec, sid, item, cell, teach, block_q,
                             picked=_picked(item, note, idk, block_q),
                             claim=CLAIM_ZH[body.choice],
                             evidence=cell_evidence(snap.kview(now), cell))
    claim = Claim(event_id=new_id("aev"), ts=now, claim=body.choice, cell=cell,
                  source_event_id=wrong.event_id)
    due = now + CONFIRM_DELAY
    f = Followup(**base, claim_event_id=claim.event_id, item=question, due=due)
    # The question took seconds and the next card may have been answered meanwhile: the K-change pass
    # makes no model call, so it is done again on the moved log, and the question is kept.
    for attempt in range(COMMIT_TRIES):
        k_after = apply_in_memory(snap, [claim])
        kc = k_changed(snap, k_after, page_id, gw, skip=None, now=now,
                       new_id=new_id, action=FOLLOWUP_ACTION)
        try:
            repo.commit(snap, Commit(aspect=[claim],
                                     wiki=[*kc.wiki, _wev(new_id, now, FollowupScheduled(followup=f))]))
            break
        except WikiConflict:
            if attempt + 1 == COMMIT_TRIES:
                raise
            snap = repo.load(user_id)
            if any(x.source_event_id == wrong.event_id for x in snap.state.followups.values()):
                raise WikiConflict(ALREADY) from None
    return CheckFollowupOut(followup_id=fid, choice=body.choice, cell=cell, concept_name=name,
                            status="pending", confirm_due=due, band=_band(snap, k_after, cell, now),
                            llm_calls=gw.calls - calls0)


def confirm_answer(repo: Repo, gw: CountingGateway | None, user_id: str, followup_id: str,
                   choice: int, *, now: datetime, new_id: Ids,
                   shown: str | None = None) -> ConfirmAnswerOut:
    """Answer a confirmation question. 404 unknown follow-up (or an unclear one, which has no
    question); 409 already answered; 422 no such option. Works without a gateway."""
    snap = repo.load(user_id)
    state = snap.state
    f = state.followups.get(followup_id)
    if f is None or f.item is None or f.claim_event_id is None:
        raise LookupError(f"没有这道确认题：{followup_id}")
    if followup_id in state.followup_outcomes:
        raise WikiConflict("这道确认题已经答过了")
    item = f.item
    if choice != IDK and not 0 <= choice < len(item.options):
        raise WikiInvalid(f"没有这个选项：{choice}")
    correct = None if choice == IDK else choice == item.answer
    outcome = "confirmed" if correct else "refuted"
    ref = f"confirm:{followup_id}"
    obs = answer_observation(item, choice, kind="check", evidence_ref=ref)
    text = item.stem if choice == IDK else item.options[choice]
    aspect: list[AspectEvent] = list(observation_events(
        [obs] if obs is not None else [], ts=now, new_id=new_id, note_text=lambda _o: text))
    aspect.append(ResolveClaim(event_id=new_id("aev"), ts=now, claim_event_id=f.claim_event_id,
                               outcome=outcome))
    k_after = apply_in_memory(snap, aspect)
    target = f.page_id if f.page_id in state.pages else state.current_page
    kc = k_changed(snap, k_after, target, gw, skip=None, now=now,
                   new_id=new_id, action=FOLLOWUP_ACTION)
    wiki = [*kc.wiki, _wev(new_id, now, FollowupResolved(followup_id=followup_id,
                                                          outcome=outcome, choice=choice)),
            signal_event(state, "review_grade", now=now, new_id=new_id, page_id=f.page_id,
                         section_id=f.section_id, text=IDK_TEXT if choice == IDK else text,
                         outcome=1.0 if correct else 0.0, target=shown)]
    reteach, signal_id = None, None
    if f.choice == "learned" and outcome == "refuted":       # 看懂没学会: change how it is taught
        teach = _teaching(state, f.page_id, f.cell)
        sig = signal_event(state, "check_unclear", now=now, new_id=new_id,
                           page_id=teach[0].page_id if teach else f.page_id,
                           section_id=teach[1].id if teach else f.section_id,
                           text=f"说学会了，确认题没答对：{item.stem}"[:2000], target=shown)
        wiki.append(sig)
        assert hasattr(sig.body, "signal")
        signal_id = sig.body.signal.signal_id
        if teach is not None:
            reteach = _target(state, *teach, user_id)
    repo.commit(snap, Commit(aspect=aspect, wiki=wiki))
    return ConfirmAnswerOut(followup_id=followup_id, correct=correct, correct_index=item.answer,
                            outcome=outcome, band=_band(snap, k_after, f.cell, now),
                            reteach=reteach, signal_id=signal_id)
