from datetime import UTC, datetime, timedelta

from evowiki.belief.aspects import DefineConcept, Observe, PageRead
from evowiki.goals.contracts import Observation
from evowiki.wikiapp.repo import MemoryRepo

NOW = datetime(2026, 11, 5, tzinfo=UTC)


def answer(i, day, polarity="+", kind="check", ref=None):
    return Observe(event_id=i, ts=NOW + timedelta(days=day), observation=Observation(
        cell="c.term", polarity=polarity, strength=1, kind=kind,
        evidence_ref=ref or f"review:p:s:{i}"))


def test_delayed_reviews_exclude_initial_answers_and_separate_self_grades():
    from evowiki.wikiapp.learning_outcomes import learning_outcomes

    events = [DefineConcept(event_id="d", ts=NOW, concept_id="c", aspects=("term",)),
              PageRead(event_id="p", ts=NOW, cell="c.term"),
              answer("a", 1), answer("b", 8, "-"),
              answer("c", 9, kind="recall"),
              answer("d2", 10, ref="check:p:s")]
    repo = MemoryRepo.of_logs("u", [], events)
    out = learning_outcomes(repo.load("u"), events, [], now=NOW + timedelta(days=11))
    assert out["delayed_review"]["1-7天"]["correct"] == 1
    assert out["delayed_review"]["7-30天"]["rate"] == 0
    assert out["delayed_review"]["30天及以上"]["rate"] is None
    assert out["recall_self_report"]["1-7天"]["total"] == 1
    assert sum(out["k_distribution"].values()) == 1
    assert out["reteach"]["rate"] is None


def test_no_data_is_explicit():
    from evowiki.wikiapp.learning_outcomes import learning_outcomes

    repo = MemoryRepo(["u", "other"])
    out = learning_outcomes(repo.load("u"), [], [], now=NOW)
    assert out["k_distribution"] is None
    assert out["reteach"]["status"] == "没有数据"
    assert all(v["status"] == "没有数据" for v in out["delayed_review"].values())


def test_followup_counts_only_answered_learned_claims_and_does_not_mutate():
    from evowiki.goals.contracts import CheckItem
    from evowiki.wikiapp.events import Followup, FollowupResolved, FollowupScheduled, WikiEvent
    from evowiki.wikiapp.learning_outcomes import learning_outcomes

    wiki = []
    for i, choice in enumerate(("learned", "learned", "learned", "slip")):
        fid = str(i)
        f = Followup(followup_id=fid, choice=choice, cell="c.term", page_id="p",
                     section_id="s", answer_ref="wrong", source_event_id="wrong",
                     claim_event_id="claim" + fid, due=NOW + timedelta(hours=1), created_at=NOW,
                     item=CheckItem(cell="c.term", stem="哪一个", options=("一", "二"), answer=0))
        wiki.append(WikiEvent(event_id="s" + fid, ts=NOW, body=FollowupScheduled(followup=f)))
        if i != 2:
            wiki.append(WikiEvent(event_id="r" + fid, ts=NOW + timedelta(hours=2),
                                  body=FollowupResolved(followup_id=fid, choice=i % 2,
                                                        outcome="refuted" if i == 1 else "confirmed")))
    repo = MemoryRepo(["u"])
    snap = repo.load("u")
    before = [e.model_dump() for e in wiki]
    out = learning_outcomes(snap, [], wiki, now=NOW + timedelta(days=1))
    assert out["reteach"]["rate"] == .5
    assert out["reteach"]["total"] == 2
    assert out["reteach"]["pending"] == 1
    assert before == [e.model_dump() for e in wiki]
    assert learning_outcomes(snap, [], [], now=NOW)["reteach"]["rate"] is None


def test_first_answer_unknown_interval_and_exact_bucket_boundaries():
    from evowiki.wikiapp.learning_outcomes import learning_outcomes

    events = [DefineConcept(event_id="def", ts=NOW, concept_id="c", aspects=("term",)),
              answer("first", 0), answer("same_time", 0), answer("one", 1),
              answer("seven", 8), answer("thirty", 38),
              answer("page_round", 39, ref="review:p:s:rvp_1")]
    repo = MemoryRepo.of_logs("u", [], events)
    out = learning_outcomes(repo.load("u"), events, [], now=NOW + timedelta(days=40))
    assert out["review_without_interval"] == 1
    assert [b["total"] for b in out["delayed_review"].values()] == [0, 1, 1, 1]
