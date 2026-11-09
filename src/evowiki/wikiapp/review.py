"""Review in the wiki app (spec §9.3): today's list, self-grading, the digest. Scheduling,
recall evidence and the digest are goals/review.py; this module only gathers the inputs (every
organized page's cues and verified checks, the active goals' concepts, today's count) and sends
a grade down the one K-change path (kchange.k_changed): the given page — else the current one —
folds / unfolds / patches, other pages become 待更新. today and digest make 0 LLM calls.

Note text is raw (D21): a self-grade is its grade value (recalled / fuzzy / forgot); a check
answer is the chosen option text, or the stem for 不知道. evidence_ref is
`review:{page_id or '-'}:{section_id or '-'}:{id}` for both modes. A self-grade is `recall`
Observe evidence; a check-mode answer is `check` Observe evidence. Both notes show as kind
"review" in the K grid, because the note kind comes from the evidence_ref prefix."""

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from evowiki.belief.aspects import AspectEvent, AspectK, AspectKView, Observe
from evowiki.goals.contracts import SectionRole, split_cell
from evowiki.goals.diagnostic import IDK, answer_observation
from evowiki.goals.guardrails import section_of
from evowiki.goals.review import (
    PAGE_ROUND_ID,
    BandChange,
    Fading,
    Period,
    ReviewPlan,
    ReviewSource,
    digest,
    last_answered,
    page_cards,
    page_round_answers,
    recall_observation,
    reviews_done,
    schedule,
    sources_from_page,
)
from evowiki.wikiapp.kchange import _require_cell, k_changed
from evowiki.wikiapp.kwrite import Ids, apply_in_memory, concept_name, observation_events
from evowiki.wikiapp.lineage import card_artifact, followup_artifact
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import Commit, Repo, WikiInvalid
from evowiki.wikiapp.signals import GRADE_OUTCOME, GRADE_TEXT, IDK_TEXT, signal_event
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.views import page_title
from evowiki.wikiapp.wiki_schemas import (
    BandChangeOut,
    CellChangeOut,
    ConfirmCardOut,
    DigestOut,
    FadingOut,
    ReviewCheckOut,
    ReviewCountsOut,
    ReviewGradeIn,
    ReviewGradeOut,
    ReviewItemOut,
    ReviewTodayOut,
)

REVIEW_ACTION = "review"


def card_id(page_id: str, section_id: str, mode: str) -> str:
    """Stable, URL-safe id of a review card: a section's recall cue (mode "cue") or its verified
    check (mode "check"). Independent of the cell and of the card's text, so it survives a
    rewrite; two cells of one section share the cue card."""
    blob = f"{page_id}\x1f{section_id}\x1f{mode}".encode()
    return "rc_" + hashlib.sha256(blob).hexdigest()[:16]


def sources(state: WikiState) -> tuple[ReviewSource, ...]:
    """Every organized page's recall cues and verified checks, in page order, minus trashed pages
    and the cards the user hid (too easy / dropped): a hidden cue or check is blanked, a source
    with neither left is skipped."""
    out: list[ReviewSource] = []
    hidden = state.hidden_cards
    for p in state.page_order:
        page = state.pages[p].page
        if page is None or p in state.trashed_pages:
            continue
        for src in sources_from_page(page.outline, page.texts):
            if hidden:
                upd: dict = {}
                if src.cue and card_id(src.page_id, src.section_id, "cue") in hidden:
                    upd["cue"] = ""
                if src.check is not None and card_id(src.page_id, src.section_id,
                                                     "check") in hidden:
                    upd["check"] = None
                if upd:
                    src = src.model_copy(update=upd)
                    if not src.cue and src.check is None:
                        continue
            out.append(src)
    return tuple(out)


def goal_concepts(state: WikiState) -> frozenset[str]:
    """Concepts of the active goals: every page not done, not trashed and not collapsed to a
    reference card, plus every page's concepts of a learning path that is not archived and still
    has a page not done (not started, or its wiki page not marked done)."""
    out: set[str] = set()
    for p in state.page_order:
        rec = state.pages[p]
        if not rec.done and not rec.shelved and p not in state.trashed_pages:
            out.update(rec.concepts)
    for pid, rec in state.paths.items():
        st = state.goal_settings.get(pid)
        if st is not None and st.archived:
            continue
        unfinished = any(pid is None or pid not in state.pages or not state.pages[pid].done
                         for pid in rec.page_ids)
        if unfinished:
            out.update(c for page in rec.path.pages for c in page.concepts)
    return frozenset(out)


def deadline_days(state: WikiState, now: datetime, timezone: str) -> dict[str, float]:
    """Days left until the nearest exam date of a goal, per concept of that goal's pages (goals
    with a deadline still ahead, not archived; paused goals count: reviews go on). A deadline
    ends at 23:59:59 of that date in `timezone` (the Bot setting's zone; an unknown one raises).
    The review scheduler pulls what would be forgotten before then (`goals.review.fading`)."""
    zone = ZoneInfo(timezone)
    out: dict[str, float] = {}
    for gid, st in state.goal_settings.items():
        rec = state.paths.get(gid)
        if st.deadline is None or st.archived or rec is None:
            continue
        end = datetime(st.deadline.year, st.deadline.month, st.deadline.day, 23, 59, 59,
                       tzinfo=zone)
        days = (end - now).total_seconds() / 86400.0
        if days <= 0:
            continue
        for page in rec.path.pages:
            for c in page.concepts:
                out[c] = min(days, out.get(c, days))
    return out


DEFAULT_TZ = "Asia/Shanghai"          # the Bot setting's default time zone


def day_start(now: datetime, timezone: str = DEFAULT_TZ) -> datetime:
    """Local midnight (in `timezone`, the Bot setting's zone) of `now`'s local day."""
    zone = ZoneInfo(timezone)
    d = now.astimezone(zone).date()
    return datetime(d.year, d.month, d.day, tzinfo=zone)


def _done_today(events: Sequence[AspectEvent], now: datetime, timezone: str = DEFAULT_TZ) -> int:
    """Review answers since local midnight (`goals.review.reviews_done`)."""
    # day_start itself counts as today: shift the open lower bound by a microsecond.
    return reviews_done(events, day_start(now, timezone) - timedelta(microseconds=1), now)


def _page_round_today(events: Sequence[AspectEvent], now: datetime, timezone: str = DEFAULT_TZ) -> int:
    return page_round_answers(events, day_start(now, timezone) - timedelta(microseconds=1), now)


def streak_days(events: Sequence[AspectEvent], now: datetime, timezone: str = DEFAULT_TZ) -> int:
    """Consecutive local days (in `timezone`) with at least one review grade or check answer,
    ending today. Today counts once it has one; with none yet the streak ending yesterday is
    shown (the day is not over). 0 when neither today nor yesterday has any."""
    zone = ZoneInfo(timezone)
    days = {ev.ts.astimezone(zone).date() for ev in events
            if isinstance(ev, Observe) and ev.observation.kind in ("recall", "check")
            and ev.ts <= now}
    day = now.astimezone(zone).date()
    if day not in days:
        day -= timedelta(days=1)
    n = 0
    while day in days:
        n += 1
        day -= timedelta(days=1)
    return n


def today_out(state: WikiState, k: AspectK, plan: ReviewPlan, done: int, now: datetime, *,
              user_id: str = "", streak: int = 0, page_round: int = 0) -> ReviewTodayOut:
    kview = AspectKView(k, now)
    items = []
    for it in plan.items:
        cid, aspect = split_cell(it.cell)
        items.append(ReviewItemOut(
            card_id=card_id(it.page_id, it.section_id, it.mode), cell=it.cell,
            cells=list(it.cells), concept_id=cid, concept_name=concept_name(state, cid, user_id=user_id),
            aspect=aspect, band=kview.cell(it.cell).band, mode=it.mode, page_id=it.page_id,
            page_title=page_title(state, it.page_id, user_id=user_id),
            section_id=it.section_id, heading=it.heading, cue=it.cue, answer=it.answer,
            check=(ReviewCheckOut(stem=it.check.stem, options=list(it.check.options))
                   if it.check is not None else None),
            recall_now=it.recall_now, days_to_threshold=it.days_to_threshold,
            in_goal=it.in_goal, exam=it.exam,
            artifact_id=(card_artifact(user_id, it.page_id, it.section_id, it.heading, it.mode,
                                       cue=it.cue, check=it.check) if user_id else None)))
    return ReviewTodayOut(items=items, due=plan.due, done_today=done, page_round_today=page_round,
                          limit=state.review.max_per_day, streak_days=streak)


def review_today(repo: Repo, user_id: str, *, now: datetime, timezone: str = DEFAULT_TZ,
                 exam_concepts: frozenset[str] = frozenset(), more: bool = False
                 ) -> ReviewTodayOut:
    """GET today's review list (0 calls). `timezone`: the Bot settings' zone (the local day of
    the done count and of the learning streak). `exam_concepts`: the frequently examined nodes
    (`exam_store.frequent_nodes`, read by the caller with a connection). `max_per_day` is a batch
    size: the list holds what is left of today's batch; `more` asks for the next full batch
    (cards already answered are no longer due, so these are the next ones)."""
    snap = repo.load(user_id)
    events = repo.aspect_events(user_id)
    done = _done_today(events, now, timezone)
    plan = schedule(snap.k, now, sources(snap.state), goal_concepts(snap.state),
                    snap.state.review, already_reviewed=0 if more else done,
                    deadline_days=deadline_days(snap.state, now, timezone),
                    postponed=snap.state.review_postponed, exam_concepts=exam_concepts,
                    answered=last_answered(events, day_start(now, timezone)))
    out = today_out(snap.state, snap.k, plan, done, now, user_id=user_id,
                    streak=streak_days(events, now, timezone),
                    page_round=_page_round_today(events, now, timezone))
    return out.model_copy(update={"confirms": confirm_cards(snap.state, now, user_id)})


def review_page(repo: Repo, user_id: str, page_id: str, *, now: datetime, timezone: str = DEFAULT_TZ,
                exam_concepts: frozenset[str] = frozenset()) -> ReviewTodayOut:
    """「考考我这页」 (0 calls): the page's cards, due or not, in the shape of today's list. LookupError:
    no such page."""
    snap = repo.load(user_id)
    if page_id not in snap.state.pages or page_id in snap.state.trashed_pages:
        raise LookupError(f"没有这个页面：{page_id}")
    events = repo.aspect_events(user_id)
    plan = page_cards(snap.k, now, tuple(s for s in sources(snap.state) if s.page_id == page_id),
                      goal_concepts(snap.state), exam_concepts,
                      last_answered(events, day_start(now, timezone)))
    return today_out(snap.state, snap.k, plan, _done_today(events, now, timezone), now, user_id=user_id,
                     streak=streak_days(events, now, timezone),
                     page_round=_page_round_today(events, now, timezone))


def grade_review(repo: Repo, gw: CountingGateway | None, user_id: str, body: ReviewGradeIn, *,
                 now: datetime, new_id: Ids) -> ReviewGradeOut:
    """One answered card: a self-grade (recall evidence for each cell of the card, `cell` plus
    `cells`, one shared evidence_ref) or a check answer (check evidence on its one cell), then
    the K-change pass on the given page, else the current one. Unknown cell / page / section →
    LookupError; a choice on a section that is not this cell's verified check, an option out of
    range, or cells beyond `cell` that are not all cells of the given section's cue →
    WikiInvalid. Works without a gateway (degrade path: fold only)."""
    snap = repo.load(user_id)
    graded = list(dict.fromkeys([body.cell, *body.cells]))
    for c in graded:
        _require_cell(snap.k, c)
    rec, section = None, None
    if body.page_id is not None:
        rec = snap.state.pages.get(body.page_id)
        if rec is None:
            raise LookupError(f"没有这个页面：{body.page_id}")
    if body.section_id is not None:
        if rec is None:                                # ReviewGradeIn: section_id needs page_id
            raise WikiInvalid("给出 section_id 时也要给出 page_id")
        if rec.page is None or all(s.id != body.section_id for s in rec.page.outline.sections):
            raise LookupError(f"没有这一段：{body.section_id}")
        section = section_of(rec.page.outline, body.section_id)
    if len(graded) > 1 and (body.grade is None or section is None
                            or not set(graded) <= set(section.cells)):
        raise WikiInvalid("一张卡只能评这一段回想提示里的格子")
    ref = (f"review:{body.page_id or '-'}:{body.section_id or '-'}:"
           f"{new_id(PAGE_ROUND_ID if body.page_round else 'rv')}")
    correct: bool | None = None
    correct_index: int | None = None
    if body.choice is not None:
        item = section.check if section is not None else None
        if (section is None or section.role is not SectionRole.CHECK or item is None
                or not item.verified or item.cell != body.cell):
            raise WikiInvalid(f"这一段不是这个格子的确认题：{body.section_id}")
        if body.choice != IDK and not 0 <= body.choice < len(item.options):
            raise WikiInvalid(f"没有这个选项：{body.choice}")
        obs = answer_observation(item, body.choice, kind="check", evidence_ref=ref)
        observations = [obs] if obs is not None else []
        text = item.stem if body.choice == IDK else item.options[body.choice]
        correct = None if body.choice == IDK else body.choice == item.answer
        correct_index = item.answer
        sig_text, outcome = (IDK_TEXT if body.choice == IDK else text), 1.0 if correct else 0.0
    else:
        if body.grade is None:                         # ReviewGradeIn: grade xor choice
            raise WikiInvalid("请给出 grade，或 choice")
        observations = [recall_observation(c, body.grade, ref) for c in graded]
        text = body.grade
        sig_text, outcome = GRADE_TEXT[body.grade], GRADE_OUTCOME[body.grade]
    aspect = observation_events(observations, ts=now, new_id=new_id,
                                note_text=lambda _o: text)
    k_after = apply_in_memory(snap, aspect)
    target = body.page_id or snap.state.current_page
    # a trashed page is never rewritten: the grade still records, other pages only go stale
    kc = k_changed(snap, k_after, None if target in snap.state.trashed_pages else target, gw,
                   skip=None, now=now,
                   new_id=new_id, action=REVIEW_ACTION)
    signal = signal_event(snap.state, "review_grade", now=now, new_id=new_id,
                          page_id=body.page_id, section_id=body.section_id, text=sig_text,
                          outcome=outcome, target=body.artifact_id)
    repo.commit(snap, Commit(aspect=aspect, wiki=[*kc.wiki, signal]))
    before = snap.kview(now).cell(body.cell).band
    after = AspectKView(k_after, now).cell(body.cell).band
    return ReviewGradeOut(band=CellChangeOut(cell=body.cell, **{"from": before}, to=after),
                          correct=correct, correct_index=correct_index, page_id=target,
                          change=kc.change, answer_ref=ref)


def _band_change_out(state: WikiState, b: BandChange, user_id: str) -> BandChangeOut:
    cid, aspect = split_cell(b.cell)
    return BandChangeOut(cell=b.cell, concept_id=cid,
                         concept_name=concept_name(state, cid, user_id=user_id), aspect=aspect,
                         **{"from": b.before}, to=b.after, forgot=b.forgot)


def _fading_out(state: WikiState, f: Fading, user_id: str) -> FadingOut:
    cid, aspect = split_cell(f.cell)
    return FadingOut(cell=f.cell, concept_id=cid,
                     concept_name=concept_name(state, cid, user_id=user_id), aspect=aspect,
                     band=f.band, recall_now=f.recall_now,
                     days_to_threshold=f.days_to_threshold, relearning=f.relearning)


def review_digest(repo: Repo, user_id: str, period: Period, *, now: datetime,
                  timezone: str = DEFAULT_TZ,
                  exam_concepts: frozenset[str] = frozenset()) -> DigestOut:
    """The day / week digest (0 calls), replayed from the aspect event log. `exam_concepts`: as
    in `review_today` (常考 nodes rank first in its today list)."""
    snap = repo.load(user_id)
    events = repo.aspect_events(user_id)
    d = digest(events, snap.k.config, now, period, sources(snap.state),
               goal_concepts(snap.state), snap.state.review,
               day_start=day_start(now, timezone), trusted=snap.k.trusted,
               postponed=snap.state.review_postponed, exam_concepts=exam_concepts,
               deadline_days=deadline_days(snap.state, now, timezone))
    st = snap.state
    return DigestOut(
        period=d.period, start=d.start, end=d.end,
        progressed=[_band_change_out(st, b, user_id) for b in d.progressed],
        slipped=[_band_change_out(st, b, user_id) for b in d.slipped],
        fading=[_fading_out(st, f, user_id) for f in d.fading],
        reviews=ReviewCountsOut(**d.reviews),
        today=today_out(st, snap.k, d.today, _done_today(events, now, timezone), now,
                        user_id=user_id,
                        streak=streak_days(events, now, timezone),
                        page_round=_page_round_today(events, now, timezone)))


def confirm_cards(state: WikiState, now: datetime, user_id: str = "") -> list[ConfirmCardOut]:
    """Due confirmation questions, oldest first (GET /wiki/review/today `confirms`)."""
    out = []
    for f in sorted(state.open_followups(), key=lambda f: (f.due, f.followup_id)):
        if f.due is None or f.due > now or f.item is None or f.choice == "unclear":
            continue
        cid, aspect = split_cell(f.cell)
        out.append(ConfirmCardOut(
            followup_id=f.followup_id, kind=f.choice, cell=f.cell, concept_id=cid,
            concept_name=concept_name(state, cid, user_id=user_id), aspect=aspect,
            page_id=f.page_id, page_title=page_title(state, f.page_id, user_id=user_id),
            check=ReviewCheckOut(stem=f.item.stem, options=list(f.item.options)), due=f.due,
            artifact_id=followup_artifact(user_id, f.followup_id, f.item) if user_id else None))
    return out
