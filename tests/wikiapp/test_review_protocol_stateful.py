"""Review batches, calendar boundaries, retries and wrong-answer followups through product APIs."""

from collections import Counter
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from evowiki.wikiapp.followup import PURPOSE, confirm_answer, followup
from evowiki.wikiapp.kchange import answer_check
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import MemoryRepo, WikiConflict, WikiInvalid
from evowiki.wikiapp.review import grade_review, review_today
from evowiki.wikiapp.settings import put_settings
from evowiki.wikiapp.state import replay
from evowiki.wikiapp.wiki_schemas import CheckFollowupIn, ReviewGradeIn, SettingsIO
from tests.wikiapp.script import Script
from tests.wikiapp.test_followup import NEW_Q
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_review_flow import LATER, organized_learned_page


def test_review_day_and_followup_sequences():
    coverage = Counter()

    class Review(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.repo, self.script, self.ids = MemoryRepo(["u1"]), Script(), Ids()
            self.pid = organized_learned_page((self.repo, self.script, self.ids))
            self.script.always(PURPOSE, lambda _: NEW_Q)
            self.now = LATER
            self.answers = []
            self.limit = 10
            self.wrong = False
            self.page_checked = False
            self.followed = False
            self.followups = {}

        @rule(hours=st.sampled_from([0, 1, 8, 24, 48]))
        def time_passes(self, hours):
            coverage["clock"] += 1
            self.now += timedelta(hours=hours)

        @rule(limit=st.integers(1, 15))
        def batch_size(self, limit):
            coverage["limit"] += 1
            put_settings(self.repo, "u1", SettingsIO(review_per_day=limit), now=self.now, new_id=self.ids)
            self.limit = limit

        @rule(grade=st.sampled_from(["recalled", "fuzzy", "forgot"]), page_round=st.booleans(), both=st.booleans())
        def self_grade(self, grade, page_round, both):
            coverage[grade] += 1
            grade_review(self.repo, None, "u1", ReviewGradeIn(
                cell="c1.interpret", cells=["c1.procedure"] if both else [], grade=grade,
                page_id=self.pid, section_id="s2", page_round=page_round), now=self.now, new_id=self.ids)
            self.answers.append((self.now, page_round, "cue"))

        @rule(correct=st.booleans(), page_round=st.booleans())
        def check_grade(self, correct, page_round):
            coverage["check"] += 1
            item = self.repo.load("u1").state.pages[self.pid].page.outline.sections[3].check
            choice = item.answer if correct else (item.answer + 1) % len(item.options)
            grade_review(self.repo, None, "u1", ReviewGradeIn(
                cell="c1.interpret", choice=choice, page_id=self.pid, section_id="s4",
                page_round=page_round), now=self.now, new_id=self.ids)
            self.answers.append((self.now, page_round, "check"))
            self.wrong, self.followed = not correct, False

        @rule()
        def wrong_page_check(self):
            coverage["page_check"] += 1
            item = self.repo.load("u1").state.pages[self.pid].page.outline.sections[3].check
            if self.page_checked:
                with pytest.raises(WikiConflict):
                    answer_check(self.repo, None, "u1", self.pid, "s4", 0,
                                 now=self.now, new_id=self.ids)
                return
            answer_check(self.repo, None, "u1", self.pid, "s4", (item.answer + 1) % len(item.options),
                         now=self.now, new_id=self.ids)
            self.page_checked = True
            self.wrong, self.followed = True, False

        @rule(choice=st.sampled_from(["slip", "learned", "unclear"]))
        def follow_wrong_answer(self, choice):
            coverage["followup"] += 1
            before = len(self.repo.wiki_log("u1"))
            call = lambda: followup(self.repo, CountingGateway(self.script.gateway()), "u1", self.pid,
                                    "s4", CheckFollowupIn(choice=choice), now=self.now, new_id=self.ids)
            if not self.wrong:
                with pytest.raises((WikiInvalid, LookupError)):
                    call()
                assert len(self.repo.wiki_log("u1")) == before
            elif self.followed:
                with pytest.raises(WikiConflict):
                    call()
                assert len(self.repo.wiki_log("u1")) == before
            else:
                out = call()
                self.followed = True
                due = None if choice == "unclear" else self.now + timedelta(hours=1)
                assert out.confirm_due == due
                self.followups[out.followup_id] = {"due": due, "done": choice == "unclear"}
                coverage["followup_created"] += 1

        @rule(index=st.integers(0, 10), choice=st.integers(-1, 4))
        def confirm(self, index, choice):
            coverage["confirm"] += 1
            keys = [*self.followups, "unknown"]
            fid = keys[index % len(keys)]
            expected = self.followups.get(fid)
            call = lambda: confirm_answer(self.repo, None, "u1", fid, choice,
                                          now=self.now, new_id=self.ids)
            if expected is None or expected["due"] is None:
                with pytest.raises(LookupError):
                    call()
            elif expected["done"]:
                with pytest.raises(WikiConflict):
                    call()
            elif choice >= 3:
                with pytest.raises(WikiInvalid):
                    call()
            else:
                item = self.repo.load("u1").state.followups[fid].item
                out = call()
                correct = None if choice == -1 else choice == item.answer
                assert out.correct == correct
                assert out.outcome == ("confirmed" if correct else "refuted")
                expected["done"] = True
                coverage["confirmed"] += 1

        @rule()
        def invalid_grade(self):
            coverage["invalid_grade"] += 1
            before = len(self.repo.aspect_events("u1"))
            with pytest.raises(WikiInvalid):
                grade_review(self.repo, None, "u1", ReviewGradeIn(cell="c1.interpret", choice=99,
                    page_id=self.pid, section_id="s4"), now=self.now, new_id=self.ids)
            assert len(self.repo.aspect_events("u1")) == before

        @rule()
        def reload(self):
            coverage["replay"] += 1
            assert replay(self.repo.wiki_log("u1")).to_json() == self.repo.load("u1").state.to_json()

        @invariant()
        def calendar_counts_and_cards_agree(self):
            day = self.now.astimezone(ZoneInfo("Asia/Shanghai")).date()
            today = [a for a in self.answers if a[0].astimezone(ZoneInfo("Asia/Shanghai")).date() == day]
            batch = sum(not a[1] for a in today)
            extra = sum(a[1] for a in today)
            out = review_today(self.repo, "u1", now=self.now)
            assert (out.done_today, out.page_round_today, out.limit) == (batch, extra, self.limit)
            assert len(out.items) <= max(0, self.limit - batch)
            assert len({i.card_id for i in out.items}) == len(out.items)
            more = review_today(self.repo, "u1", now=self.now, more=True)
            assert len(more.items) <= self.limit
            prior = {mode: max((a[0] for a in self.answers if a[2] == mode and
                     a[0].astimezone(ZoneInfo("Asia/Shanghai")).date() < day), default=LATER - timedelta(days=1000))
                     for mode in ("cue", "check")}
            expected_mode = "cue" if prior["cue"] <= prior["check"] else "check"
            for item in more.items:
                if "c1.interpret" in item.cells:
                    assert item.mode == expected_mode
            due = {fid for fid, f in self.followups.items() if not f["done"] and f["due"] <= self.now}
            assert {c.followup_id for c in out.confirms} == due

    run_state_machine_as_test(Review, settings=settings(max_examples=200, stateful_step_count=50, deadline=None))
    assert coverage["followup_created"] and coverage["confirmed"]
    print("S5 review command coverage", dict(coverage))
