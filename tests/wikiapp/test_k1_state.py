from datetime import UTC, datetime

import pytest

from evowiki.goals.path import LearningPath, PathPage
from evowiki.goals.review import ReviewSettings
from evowiki.wikiapp.events import (
    PathRecord,
    PathSaved,
    ReviewSettingsSet,
    WikiEvent,
)
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.state import WikiState, apply_event, replay

T = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def lp(n=2) -> LearningPath:
    pages = tuple(PathPage(page_id=f"path-k-p{i}", targets=(f"c{i}",), concepts=(f"c{i}",),
                           objective=f"学完这页你能用 c{i}", new_cells=4) for i in range(1, n + 1))
    return LearningPath(path_id="path-k", goal="学统计", goal_key="k", split=True, source="llm",
                        capacity=12, new_cells=8, concepts=(), pages=pages)


def ev(i, body, ts=T):
    return WikiEvent(event_id=f"wev_{i}", ts=ts, body=body)


def test_path_saved_upserts_and_keeps_order():
    rec = PathRecord(path=lp(), page_ids=(None, None), created_at=T)
    s = apply_event(WikiState(), ev(1, PathSaved(record=rec, action="planned")))
    started = rec.model_copy(update={"page_ids": ("pg_1", None)})
    s = apply_event(s, ev(2, PathSaved(record=started, action="page_started")))
    assert s.path_order == ("path-k",) and s.paths["path-k"].page_ids == ("pg_1", None)


def test_path_record_page_ids_must_align():
    with pytest.raises(ValueError):
        PathRecord(path=lp(2), page_ids=(None,), created_at=T)


def test_review_settings():
    s = apply_event(WikiState(), ev(1, ReviewSettingsSet(settings=ReviewSettings(max_per_day=3))))
    assert s.review.max_per_day == 3


def test_new_events_replay_and_round_trip_json():
    rec = PathRecord(path=lp(), page_ids=(None, None), created_at=T)
    evs = [ev(1, PathSaved(record=rec, action="planned")),
           ev(2, ReviewSettingsSet(settings=ReviewSettings(max_per_day=5)))]
    wire = [WikiEvent.model_validate(e.model_dump(mode="json")) for e in evs]
    assert replay(wire).to_json() == replay(evs).to_json()


def test_memory_repo_aspect_events():
    repo = MemoryRepo(["u1"])
    assert repo.aspect_events("u1") == []
    # commit one aspect event through the normal path and read it back
    from evowiki.belief.aspects import DefineConcept
    d = DefineConcept(event_id="aev_1", ts=T, concept_id="c1", aspects=["term"], prereqs=[])
    repo.commit(repo.load("u1"), Commit(aspect=[d]))
    assert repo.aspect_events("u1") == [d]
