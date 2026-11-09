"""Task 5 (K1): review in the wiki app - today's list, self-grading through the one K-change
path, the day / week digest and the review_per_day setting."""

from datetime import timedelta

import pytest

from evowiki.belief.aspects import AddNote, Observe
from evowiki.goals.contracts import Band, Observation
from evowiki.goals.review import schedule
from evowiki.wikiapp.events import PageOpened, PageSaved
from evowiki.wikiapp.page import mark_done, open_page_flow
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiInvalid
from evowiki.wikiapp.review import (
    day_start,
    goal_concepts,
    grade_review,
    review_digest,
    review_today,
    sources,
)
from evowiki.wikiapp.settings import get_settings, put_settings
from evowiki.wikiapp.views import k_grid_out
from evowiki.wikiapp.wiki_schemas import ReviewGradeIn, SettingsIO
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_organize import CHECK_KEY, add_page
from tests.wikiapp.test_page import commit_wiki, organized

# Three quiz-kind `+` (strength 1.0) at NOW take c1.procedure / c1.interpret to 确定会. K's memory
# is FSRS (belief/memory.py): the first answer gives stability 2.3065 days and the two massed
# ones the same moment add nothing (k.memory(cell).stability, checked); recall falls below
# BandParams.recall_min = 0.6 after ≈62.25 days. 63 days is past it, while LATER - 23 h (the day
# digest's start) is still before it (still 确定会 there).
LEARN_EVENTS = 3
LEARNED_STABILITY = 2.3065
LATER = NOW + timedelta(days=63)
CELLS = ("c1.procedure", "c1.interpret")


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def organized_learned_page(env, fold_known: bool = False) -> str:
    """The organized z-score page (s2 cue on both c1 cells, s4 verified check on c1.interpret),
    then LEARN_EVENTS quiz `+` on each c1 cell at NOW (last_tested = NOW). fold_known: open the
    page at NOW so the fold pass folds s2 (its cells are now 确定会)."""
    repo, _, ids = env
    pid = organized(env)
    for i in range(LEARN_EVENTS):
        snap = repo.load("u1")
        repo.commit(snap, Commit(aspect=[
            Observe(event_id=ids("aev"), ts=NOW, observation=Observation(
                cell=c, polarity="+", strength=1.0, kind="recall", evidence_ref=f"quiz:t{i}"))
            for c in CELLS]))
    k = repo.load("u1").k
    assert all(k.belief(c, NOW).band is Band.KNOWN for c in CELLS)
    assert all(k.memory(c).stability == pytest.approx(LEARNED_STABILITY) for c in CELLS)
    assert all(k.belief(c, LATER).recall < k.config.bands.recall_min for c in CELLS)
    if fold_known:
        out = open_page_flow(repo, None, "u1", pid, now=NOW, new_id=ids)
        assert next(x for x in out.sections if x.id == "s2").folded
    return pid


def last_observation(repo):
    return [e for e in repo.aspect_events("u1") if isinstance(e, Observe)][-1].observation


def test_today_orders_by_score_and_respects_the_cap(env):
    repo, _, ids = env
    pid = organized_learned_page(env)
    put_settings(repo, "u1", SettingsIO(review_per_day=1), now=NOW, new_id=ids)
    out = review_today(repo, "u1", now=LATER)
    assert out.limit == 1 and out.due == 1 and len(out.items) == 1   # one cue: one card
    full = schedule(repo.load("u1").k, LATER, sources(repo.load("u1").state),
                    goal_concepts(repo.load("u1").state))
    assert out.items[0].cell == full.items[0].cell          # highest score first
    assert sorted(out.items[0].cells) == sorted(CELLS)
    it = out.items[0]
    assert it.mode == "cue" and it.cue and it.page_title == "z 分数" and it.concept_name
    assert it.answer == "z 分数怎么读的答案"
    assert it.page_id == pid and it.section_id == "s2" and it.heading == "z 分数怎么读"
    assert it.band is Band.SHAKY and it.in_goal and it.days_to_threshold < 0
    assert out.done_today == 0


def test_the_cards_of_a_cell_take_turns_a_new_turn_each_day(env):
    """s2's cue covers both cells, s4's check c1.interpret. A card still due today stays the same
    card; the next due round moves on to the card answered longest ago."""
    repo, _, ids = env
    pid = organized_learned_page(env)
    first = review_today(repo, "u1", now=LATER)
    assert [(i.mode, i.section_id) for i in first.items] == [("cue", "s2")]
    grade_review(repo, None, "u1", ReviewGradeIn(cell=first.items[0].cell, cells=first.items[0].cells, grade="forgot",
                                                 page_id=pid, section_id="s2"), now=LATER, new_id=ids)
    same_day = review_today(repo, "u1", now=LATER + timedelta(minutes=1))
    assert [(i.mode, i.section_id) for i in same_day.items] == [("cue", "s2")]

    next_day = LATER + timedelta(days=1)
    round2 = review_today(repo, "u1", now=next_day)
    assert {i.section_id: (i.mode, i.cells) for i in round2.items}["s4"] == ("check", ["c1.interpret"])
    item = next(i for i in round2.items if i.section_id == "s4")
    assert item.check is not None
    grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", choice=0, page_id=pid, section_id="s4"),
                 now=next_day, new_id=ids)
    later = review_today(repo, "u1", now=next_day + timedelta(days=30))
    assert {i.section_id for i in later.items} == {"s2"}      # s4 answered last: s2's turn again


def test_cap_counts_today_reviews(env):
    repo, _, ids = env
    organized_learned_page(env)
    put_settings(repo, "u1", SettingsIO(review_per_day=2), now=NOW, new_id=ids)
    first = review_today(repo, "u1", now=LATER).items[0]
    grade_review(repo, None, "u1", ReviewGradeIn(cell=first.cell, grade="recalled"), now=LATER,
                 new_id=ids)
    out = review_today(repo, "u1", now=LATER)
    assert out.done_today == 1 and len(out.items) <= 1 and first.cell not in [
        i.cell for i in out.items]


@pytest.mark.parametrize("grade,polarity", [("recalled", "+"), ("fuzzy", "~"), ("forgot", "-")])
def test_grade_writes_recall_evidence(env, grade, polarity):
    repo, _, ids = env
    page_id = organized_learned_page(env)
    out = grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade=grade,
                                                       page_id=page_id, section_id="s2"),
                       now=LATER, new_id=ids)
    obs = last_observation(repo)
    assert (obs.kind, obs.polarity, obs.cell) == ("recall", polarity, "c1.interpret")
    assert obs.evidence_ref.startswith(f"review:{page_id}:s2:")
    note = [e for e in repo.aspect_events("u1") if isinstance(e, AddNote)][-1]
    assert note.text == grade and note.evidence_ref == obs.evidence_ref
    assert out.page_id == page_id and out.band.cell == "c1.interpret"
    assert out.correct is None and out.correct_index is None and out.change.llm_calls == 0


def test_one_card_grade_updates_its_cells_and_counts_once(env):
    """D15: s2's cue covers both c1 cells; one grade is one answer for each, under one ref; the
    prompt does not come back and today's count is one card."""
    repo, _, ids = env
    pid = organized_learned_page(env)
    [card] = review_today(repo, "u1", now=LATER).items
    grade_review(repo, None, "u1", ReviewGradeIn(cell=card.cell, cells=card.cells,
                                                 grade="recalled", page_id=pid,
                                                 section_id=card.section_id),
                 now=LATER, new_id=ids)
    recalls = [e.observation for e in repo.aspect_events("u1")
               if isinstance(e, Observe) and e.observation.evidence_ref.startswith("review:")]
    assert sorted(o.cell for o in recalls) == sorted(CELLS)
    assert len({o.evidence_ref for o in recalls}) == 1
    out = review_today(repo, "u1", now=LATER)
    assert out.items == [] and out.due == 0 and out.done_today == 1


def test_a_grade_cannot_reach_cells_outside_its_cue(env):
    repo, _, ids = env
    pid = organized_learned_page(env)
    k = repo.load("u1").k
    other = next(f"{c}.{a}" for c in k.concept_ids() for a in k.aspects(c)
                 if f"{c}.{a}" not in CELLS)
    for body in (ReviewGradeIn(cell="c1.interpret", cells=["c1.procedure"], grade="recalled"),
                 ReviewGradeIn(cell="c1.interpret", cells=[other], grade="recalled",
                               page_id=pid, section_id="s2")):
        with pytest.raises(WikiInvalid):
            grade_review(repo, None, "u1", body, now=LATER, new_id=ids)


def test_a_page_rounds_grade_moves_the_cell_but_not_the_daily_batch(env):
    repo, _, ids = env
    page_id = organized_learned_page(env)
    before = repo.load("u1").k.memory("c1.interpret").last_tested
    grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="recalled", page_id=page_id,
                                                 section_id="s2", page_round=True), now=LATER, new_id=ids)
    assert repo.load("u1").k.memory("c1.interpret").last_tested != before
    today = review_today(repo, "u1", now=LATER)
    assert (today.done_today, today.page_round_today) == (0, 1)


def test_grade_without_page_uses_current_page_ref(env):
    repo, _, ids = env
    pid = organized_learned_page(env)
    commit_wiki(env, PageOpened(page_id=pid))
    out = grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="fuzzy"),
                       now=LATER, new_id=ids)
    assert out.page_id == pid
    assert last_observation(repo).evidence_ref.startswith("review:-:-:")


def test_forgotten_cell_drops_and_page_unfolds(env):
    repo, _, ids = env
    page_id = organized_learned_page(env, fold_known=True)   # s2 folded because c1 cells known
    # opening long after: recall < 0.6 → band shaky → s2 unfolds (0 LLM calls with gw None)
    out = open_page_flow(repo, None, "u1", page_id, now=LATER, new_id=ids)
    s2 = next(x for x in out.sections if x.id == "s2")
    assert not s2.folded
    assert any(c.cell == "c1.interpret" and c.to == "shaky" for c in out.diff.cell_changes)


def test_grade_forgot_drops_band_and_expands(env):
    repo, _, ids = env
    page_id = organized_learned_page(env, fold_known=True)
    body = ReviewGradeIn(cell="c1.interpret", grade="forgot", page_id=page_id, section_id="s2")
    # Recall evidence weighs like a quiz answer (RECALL_STRENGTH 1.0), so after three strong quiz
    # `+` it takes three 没想起来 to reach a weak band: 确定会 → 可能会 → 可能会 → 不稳. s2 stays
    # folded while the band is 可能会 (unfolding needs a weak band) and unfolds on the third.
    seen = []
    for _ in range(3):
        out = grade_review(repo, None, "u1", body, now=NOW + timedelta(days=1), new_id=ids)
        seen.append((out.band.from_, out.band.to, out.change.unfolded))
    assert seen[:2] == [(Band.KNOWN, Band.LIKELY, []), (Band.LIKELY, Band.LIKELY, [])]
    assert out.band.from_ in ("known", "likely") and out.band.to in ("shaky", "gap")
    assert "s2" in out.change.unfolded
    assert not repo.load("u1").state.pages[page_id].page.state("s2").folded


def test_grade_forgot_on_a_due_cell_unfolds_in_one_step(env):
    repo, _, ids = env
    page_id = organized_learned_page(env, fold_known=True)
    # At LATER recall < 0.6, so decay has already made c1.interpret shaky (from_ is shaky). The
    # relearning cap keeps it weak after one 没想起来, and s2 unfolds on this single grade.
    out = grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="forgot",
                                                       page_id=page_id, section_id="s2"),
                       now=LATER, new_id=ids)
    assert out.band.to in ("shaky", "gap")
    assert "s2" in out.change.unfolded
    assert not repo.load("u1").state.pages[page_id].page.state("s2").folded


def test_grade_check_mode(env):
    repo, _, ids = env
    page_id = organized_learned_page(env)
    out = grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", choice=CHECK_KEY,
                                                       page_id=page_id, section_id="s4"),
                       now=LATER, new_id=ids)
    assert out.correct is True and out.correct_index == CHECK_KEY
    obs = last_observation(repo)
    assert obs.kind == "check" and obs.evidence_ref.startswith(f"review:{page_id}:s4:")
    note = [e for e in repo.aspect_events("u1") if isinstance(e, AddNote)][-1]
    assert note.text == "低一个标准差"
    snap = repo.load("u1")
    grid = k_grid_out(snap.state, snap.k, LATER, user_id="u1")
    c1 = next(c for c in grid.concepts if c.id == "c1")
    assert ("review", "低一个标准差") in [(n.kind, n.text) for n in c1.notes]
    idk = grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", choice=-1,
                                                       page_id=page_id, section_id="s4"),
                       now=LATER, new_id=ids)
    assert idk.correct is None and idk.correct_index == CHECK_KEY
    assert [e for e in repo.aspect_events("u1") if isinstance(e, AddNote)][-1].text == "z=-1 表示？"


def test_grade_validation(env):
    repo, _, ids = env
    page_id = organized_learned_page(env)
    with pytest.raises(LookupError):
        grade_review(repo, None, "u1", ReviewGradeIn(cell="c9.term", grade="fuzzy"),
                     now=LATER, new_id=ids)
    with pytest.raises(LookupError):
        grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="fuzzy",
                                                     page_id=page_id, section_id="s99"),
                     now=LATER, new_id=ids)
    with pytest.raises(LookupError):
        grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="fuzzy",
                                                     page_id="pg_nope"),
                     now=LATER, new_id=ids)
    with pytest.raises(WikiInvalid):                       # s2 is not a check section
        grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", choice=0,
                                                     page_id=page_id, section_id="s2"),
                     now=LATER, new_id=ids)
    with pytest.raises(WikiInvalid):                       # s4 checks c1.interpret, not c1.procedure
        grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.procedure", choice=0,
                                                     page_id=page_id, section_id="s4"),
                     now=LATER, new_id=ids)
    with pytest.raises(WikiInvalid):
        grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", choice=7,
                                                     page_id=page_id, section_id="s4"),
                     now=LATER, new_id=ids)


def test_other_pages_marked_stale(env):
    repo, _, ids = env
    pid = organized_learned_page(env)
    pid2 = add_page(repo, ids, title="z 分数（第二页）")
    pid3 = add_page(repo, ids, title="z 分数（未生成）")
    snap = repo.load("u1")
    rec1 = snap.state.pages[pid]
    rec2 = snap.state.pages[pid2].model_copy(update={"page": rec1.page.model_copy(
        update={"outline": rec1.page.outline.model_copy(update={"page_id": pid2})})})
    commit_wiki(env, PageSaved(record=rec2, action="test"), PageOpened(page_id=pid))
    out = grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="forgot",
                                                       page_id=pid, section_id="s2"),
                       now=NOW + timedelta(days=1), new_id=ids)
    assert out.page_id == pid
    assert out.change.other_pages_stale == [pid2]               # pid3 is not organized
    assert pid3 not in out.change.other_pages_stale
    after = repo.load("u1")
    assert after.state.pages[pid2] == rec2                       # other page: untouched


def test_digest_day_and_week(env):
    repo, _, ids = env
    organized_learned_page(env)
    grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="forgot"),
                 now=LATER, new_id=ids)
    day = review_digest(repo, "u1", "day", now=LATER + timedelta(hours=1))
    assert day.reviews.forgot == 1 and day.period == "day"
    assert any(b.cell == "c1.interpret" for b in day.slipped)
    assert all(b.concept_name for b in [*day.progressed, *day.slipped])
    assert day.today.done_today == 1 and day.today.limit == 10
    assert all(f.concept_name for f in day.fading)
    week = review_digest(repo, "u1", "week", now=LATER + timedelta(hours=1))
    assert week.today.limit == 10 and week.period == "week"
    assert week.end - week.start == timedelta(days=7)


def test_goal_concepts_drop_done_pages(env):
    repo, _, ids = env
    pid = organized_learned_page(env)
    state = repo.load("u1").state
    assert set(state.pages[pid].concepts) <= goal_concepts(state)
    mark_done(repo, "u1", pid, now=NOW, new_id=ids)
    assert goal_concepts(repo.load("u1").state) == frozenset()


def test_day_start_is_local_midnight():
    from datetime import UTC, datetime, timezone
    from zoneinfo import ZoneInfo
    t = datetime(2026, 10, 9, 1, 30, tzinfo=timezone(timedelta(hours=8)))
    assert day_start(t) == datetime(2026, 10, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert day_start(t, "UTC") == datetime(2026, 10, 8, tzinfo=UTC)


def test_check_mode_review_answers_count_and_more_gives_the_next_batch(env):
    repo, _, ids = env
    page_id = organized_learned_page(env)
    put_settings(repo, "u1", SettingsIO(review_per_day=1), now=NOW, new_id=ids)
    grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", choice=CHECK_KEY,
                                                 page_id=page_id, section_id="s4"),
                 now=LATER, new_id=ids)
    out = review_today(repo, "u1", now=LATER)
    assert out.done_today == 1 and out.items == [] and out.due >= 1   # the batch is answered
    more = review_today(repo, "u1", now=LATER, more=True)
    assert len(more.items) == 1 and more.items[0].cell != "c1.interpret"


def test_today_starts_at_local_midnight(env):
    repo, _, ids = env
    organized_learned_page(env)
    grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="fuzzy"),
                 now=LATER, new_id=ids)
    # LATER is 09:00 UTC = 17:00 Shanghai; 8 h later it is 01:00 the next Shanghai day
    assert review_today(repo, "u1", now=LATER + timedelta(hours=8)).done_today == 0
    assert review_today(repo, "u1", now=LATER + timedelta(hours=8),
                        timezone="UTC").done_today == 1


def test_settings_round_trip_review_per_day(env):
    repo, _, ids = env
    out = put_settings(repo, "u1", SettingsIO(review_per_day=4), now=NOW, new_id=ids)
    assert out.review_per_day == 4 and get_settings(repo, "u1").review_per_day == 4
    assert repo.load("u1").state.language == "zh"            # other settings untouched
    n = len(repo.wiki_log("u1"))
    put_settings(repo, "u1", SettingsIO(review_per_day=4), now=NOW, new_id=ids)
    assert len(repo.wiki_log("u1")) == n                      # nothing changed: no write


def test_review_notes_show_in_k_grid(env):
    repo, _, ids = env
    organized_learned_page(env)
    grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="fuzzy"),
                 now=LATER, new_id=ids)
    snap = repo.load("u1")
    grid = k_grid_out(snap.state, snap.k, LATER, user_id="u1")
    c1 = next(c for c in grid.concepts if c.id == "c1")
    assert [(n.kind, n.text) for n in c1.notes if n.kind == "review"] == [("review", "fuzzy")]


def test_a_trashed_page_feeds_no_review_and_a_grade_never_rewrites_it(env):
    from evowiki.wikiapp.events import PagesTrashed
    from evowiki.wikiapp.review import card_id
    from evowiki.wikiapp.review_cards import find_card
    repo, _, ids = env
    pid = organized_learned_page(env)
    assert sources(repo.load("u1").state)
    before = repo.load("u1").state.pages[pid]
    commit_wiki(env, PagesTrashed(page_ids=(pid,)))
    state = repo.load("u1").state
    assert sources(state) == () and goal_concepts(state) == frozenset()
    assert find_card(state, card_id(pid, "s2", "cue")) is None
    assert review_today(repo, "u1", now=LATER).items == []
    out = grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="forgot",
                                                       page_id=pid, section_id="s2"),
                       now=LATER, new_id=ids)
    assert out.change.unfolded == [] and repo.load("u1").state.pages[pid] == before
    assert not any(isinstance(e.body, PageSaved) for e in repo.wiki_log("u1")[-2:])


def test_an_archived_goal_gets_no_goal_bonus():
    from evowiki.wikiapp.events import GoalSettings, GoalSettingsSet, WikiEvent
    from tests.wikiapp.test_path_edits import NOW as T
    from tests.wikiapp.test_path_edits import PATH_ID, seed_events
    from tests.wikiapp.test_path_edits import Ids as PIds
    repo, ids = MemoryRepo(["u1"]), PIds()
    aspect, wiki = seed_events(ids)
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    state = repo.load("u1").state
    path_concepts = {c for p in state.paths[PATH_ID].path.pages for c in p.concepts}
    assert path_concepts and path_concepts <= goal_concepts(state)
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=ids("wev"), ts=T, body=GoalSettingsSet(settings=GoalSettings(
            goal_id=PATH_ID, archived=True)))]))
    assert not path_concepts & goal_concepts(repo.load("u1").state)
