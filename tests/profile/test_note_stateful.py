"""Profile copies: stale saves add; only a shown baseline permits deletions."""

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from evowiki.profile.note import GAPS, WHO, Profile, add_fact, merge_edit, render, shown, with_sent


class ProfileCopies(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.profile = Profile()
        self.owned = set()
        self.gaps = set()
        self.hidden = set()
        self.baseline_owned = set()
        self.baseline_gaps = set()
        self.version = 0

    def derived(self):
        return {GAPS: tuple(sorted(self.gaps))}

    def note(self, removed=None):
        return render({WHO: tuple(sorted(self.owned - {removed})),
                       GAPS: tuple(sorted(self.gaps - self.hidden - {removed}))})

    @rule(n=st.integers(0, 10))
    def server_fact(self, n):
        line = f"fact {n}"
        self.profile = add_fact(self.profile, WHO, line)
        self.owned.add(line)

    @rule(n=st.integers(0, 10))
    def derived_gap(self, n):
        self.gaps.add(f"gap {n}")

    @rule()
    def export_copy(self):
        self.version += 1
        self.profile = with_sent(self.profile, self.derived(), str(self.version))
        self.baseline_owned = set(self.owned)
        self.baseline_gaps = set(self.gaps)

    @precondition(lambda self: self.version > 0)
    @rule(n=st.integers(0, 10), gap=st.booleans())
    def save_matching_copy(self, n, gap):
        removed = f"{'gap' if gap else 'fact'} {n}"
        self.profile = merge_edit(self.profile, self.derived(), self.note(removed),
                                  str(self.version)).profile
        # An empty note says nothing, even when its version matches. It is not
        # evidence that the learner intended to retract every remaining line.
        if not (self.owned - {removed} or self.gaps - self.hidden - {removed}):
            return
        if gap and removed in self.baseline_gaps:
            self.hidden.add(removed)
        elif not gap and removed in self.baseline_owned:
            self.owned.discard(removed)

    @rule(n=st.integers(0, 10))
    def save_stale_copy(self, n):
        self.profile = merge_edit(self.profile, self.derived(), self.note(f"fact {n}"),
                                  "old-copy").profile

    @rule()
    def reload_serialized_profile(self):
        self.profile = Profile.model_validate_json(self.profile.model_dump_json())

    @invariant()
    def facts_and_visibility_follow_the_copy_contract(self):
        assert set(self.profile.owned.get(WHO, ())) == self.owned
        assert set(shown(self.profile, self.derived()).get(GAPS, ())) == self.gaps - self.hidden


TestProfileCopies = ProfileCopies.TestCase
TestProfileCopies.settings = settings(max_examples=200, stateful_step_count=50, deadline=None)
