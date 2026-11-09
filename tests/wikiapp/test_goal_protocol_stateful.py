"""Confirmed goals, diagnosis retries, scoped entry and reversible map edits through native APIs."""

from collections import Counter

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from evowiki.goals.blocks import Block
from evowiki.wikiapp.goal import answer_check, create_goal_from_blocks, enter_region, stop_draft
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.map_edits import edit_map
from evowiki.wikiapp.repo import MemoryRepo, WikiConflict, WikiInvalid
from evowiki.wikiapp.state import replay
from evowiki.wikiapp.wiki_schemas import MapEditIn
from tests.wikiapp.goal_script import auto_probe, build_question
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids


def test_goal_diagnosis_entry_and_edit_sequences():
    coverage = Counter()

    class Goal(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.repo, self.script, self.ids = MemoryRepo(["u1"]), Script(), Ids()
            auto_probe(self.script)
            out = create_goal_from_blocks(self.repo, self.gateway(), "u1", "learn statistics",
                tuple(Block(name=f"block{i}", covers=(f"topic{i}",)) for i in range(3)),
                now=NOW, new_id=self.ids)
            self.map_id = out.draft.map_id
            self.roots = [(f"c{i + 1}", f"block{i}") for i in range(3)]
            self.drafts = {out.draft.draft_id: None}
            self.closed = set()
            self.expanded = set()
            self.children = {}
            self.undo = []
            self.previous_answer = None
            self.sequence = 0

        def gateway(self):
            return CountingGateway(self.script.gateway())

        def edit(self, ops):
            return edit_map(self.repo, "u1", self.map_id, MapEditIn.model_validate({"ops": ops}).ops,
                            now=NOW, new_id=self.ids)

        @rule(index=st.integers(0, 12), correct=st.booleans(), stop=st.booleans())
        def diagnose(self, index, correct, stop):
            coverage["diagnose"] += 1
            keys = [*self.drafts, "unknown"]
            active = [did for did in self.drafts if did not in self.closed]
            if active and index % 3:
                keys = active
            did = keys[index % len(keys)]
            if did == "unknown":
                with pytest.raises(LookupError):
                    stop_draft(self.repo, self.gateway(), "u1", did, now=NOW, new_id=self.ids)
                return
            if did in self.closed:
                with pytest.raises(WikiConflict):
                    stop_draft(self.repo, self.gateway(), "u1", did, now=NOW, new_id=self.ids)
                return
            if stop:
                out = stop_draft(self.repo, self.gateway(), "u1", did, now=NOW, new_id=self.ids)
                self.closed.add(did)
                coverage["skip_applied"] += 1
            else:
                draft = self.repo.load("u1").state.drafts[did]
                pick = draft.rounds[-1].pick
                question = build_question(pick)
                answer = ((question.answer if correct else (question.answer + 1) % len(question.options))
                          if question.kind == "quiz" else 0 if correct else 1)
                out = answer_check(self.repo, self.gateway(), "u1", did, pick.pick_id, question,
                                   answer, now=NOW, new_id=self.ids)
                self.previous_answer = did, pick.pick_id, question, answer
                if not out.open:
                    self.closed.add(did)
                coverage["answer_applied"] += 1
            assert out.open == (did not in self.closed)

        @rule()
        def finish_opening(self):
            coverage["finish_opening"] += 1
            did = next(iter(self.drafts))
            for _ in range(16):
                if did in self.closed:
                    return
                self.diagnose(0, False, False)
            raise AssertionError("opening diagnosis did not terminate after 16 answered picks")

        @rule()
        def duplicate_answer(self):
            coverage["duplicate_answer"] += 1
            if self.previous_answer is None:
                return
            did, pick, question, answer = self.previous_answer
            before = len(self.repo.wiki_log("u1")), len(self.repo.aspect_events("u1"))
            with pytest.raises(WikiConflict):
                answer_check(self.repo, self.gateway(), "u1", did, pick, question, answer,
                             now=NOW, new_id=self.ids)
            assert before == (len(self.repo.wiki_log("u1")), len(self.repo.aspect_events("u1")))

        @rule(index=st.integers(0, 12), leaf=st.booleans())
        def enter(self, index, leaf):
            coverage["enter"] += 1
            root = self.roots[index % len(self.roots)][0]
            leaves = self.children.get(root, [])
            node = leaves[index % len(leaves)] if leaf and leaves else root
            if any(region is None and did not in self.closed for did, region in self.drafts.items()):
                with pytest.raises(WikiConflict):
                    enter_region(self.repo, self.gateway(), "u1", self.map_id, node, now=NOW, new_id=self.ids)
                return
            pending = next((did for did, region in self.drafts.items() if region == node and did not in self.closed), None)
            if node == root and root not in self.expanded:
                self.script.push("goals.intake", {"parts": [
                    {"name": f"{root} first", "atomic": True}, {"name": f"{root} second", "atomic": True}]})
            out = enter_region(self.repo, self.gateway(), "u1", self.map_id, node, now=NOW, new_id=self.ids)
            if pending:
                assert out.open and out.draft.draft_id == pending
                coverage["entry_deduplicated"] += 1
            elif node == root:
                self.expanded.add(root)
                children = self.repo.load("u1").state.maps[self.map_id].children(root)
                assert {child.name for child in children} == {f"{root} first", f"{root} second"}
                self.children[root] = [child.id for child in children]
            elif out.open:
                self.drafts[out.draft.draft_id] = node
                coverage["point_diagnosis_open"] += 1
                before = len(self.repo.wiki_log("u1"))
                again = enter_region(self.repo, self.gateway(), "u1", self.map_id, node,
                                     now=NOW, new_id=self.ids)
                assert again.open and again.draft.draft_id == out.draft.draft_id
                assert len(self.repo.wiki_log("u1")) == before
                coverage["entry_deduplicated"] += 1

        @rule(index=st.integers(0, 12), operation=st.sampled_from(["rename", "drop", "add", "reorder"]))
        def edit_blocks(self, index, operation):
            coverage[operation] += 1
            self.sequence += 1
            before = list(self.roots)
            at = index % len(self.roots)
            key, _name = self.roots[at]
            if operation == "rename":
                ops = [{"op": "rename", "id": key, "name": f"renamed{self.sequence}"}]
            elif operation == "drop":
                ops = [{"op": "drop", "id": key}]
                if key in self.expanded or len(self.roots) == 1:
                    with pytest.raises(WikiConflict if key in self.expanded else WikiInvalid):
                        self.edit(ops)
                    return
            elif operation == "add":
                ops = [{"op": "add", "name": f"added{self.sequence}", "covers": ["new topic"]}]
            else:
                ops = [{"op": "reorder", "ids": [key for key, _ in reversed(self.roots)]}]
            out = self.edit(ops)
            if operation == "rename":
                self.roots[at] = key, f"renamed{self.sequence}"
            elif operation == "drop":
                self.roots.pop(at)
            elif operation == "add":
                added = out.after.nodes[-1]
                self.roots.append((added.id, f"added{self.sequence}"))
            else:
                self.roots.reverse()
            if out.undo:
                self.undo.append((before, out.undo))

        @rule()
        def undo_edit(self):
            coverage["undo_edit"] += 1
            if not self.undo:
                return
            before, ops = self.undo[-1]
            if any(op.op == "drop" and op.id in self.expanded for op in ops):
                with pytest.raises(WikiConflict):
                    self.edit([op.model_dump(mode="json") for op in ops])
                return
            self.edit([op.model_dump(mode="json") for op in ops])
            self.roots = before
            self.undo.pop()

        @rule()
        def reload(self):
            coverage["replay"] += 1
            assert replay(self.repo.wiki_log("u1")).to_json() == self.repo.load("u1").state.to_json()

        @invariant()
        def map_and_closed_drafts_agree(self):
            state = self.repo.load("u1").state
            assert [(n.id, n.name) for n in state.maps[self.map_id].nodes if n.parent is None] == self.roots
            assert self.closed <= set(state.closed_drafts)
            assert all(did not in state.closed_drafts for did in self.drafts if did not in self.closed)
            assert len({event.event_id for event in self.repo.wiki_log("u1")}) == len(self.repo.wiki_log("u1"))

    # A block diagnosed at opening can pass its evidence to its children. Add an
    # undiagnosed block so this prefix actually opens a point diagnosis every run.
    sample = Goal()
    sample.finish_opening()
    sample.edit_blocks(0, "add")
    sample.enter(3, False)
    sample.enter(3, True)
    sample.map_and_closed_drafts_agree()
    sample.teardown()
    assert coverage["point_diagnosis_open"] and coverage["entry_deduplicated"]
    run_state_machine_as_test(Goal, settings=settings(max_examples=200, stateful_step_count=50, deadline=None))
    assert coverage["point_diagnosis_open"] and coverage["entry_deduplicated"]
    print("S5 goal command coverage", dict(coverage))
