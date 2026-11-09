"""Learning path commands drive native edit/start/split/undo consumers and event replay."""

from collections import Counter

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from evowiki.wikiapp.path_edits import ROWS_UNDO, edit_path, split_row, undo_split_row
from evowiki.wikiapp.paths import next_path_page, path_items
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict, WikiInvalid
from evowiki.wikiapp.state import replay
from evowiki.wikiapp.wiki_schemas import PathAddIn, PathMoveIn, PathRemoveIn, PathRetitleIn
from tests.wikiapp.test_path_edits import NOW, PATH_ID, Ids, seed_events


def test_path_edit_start_and_undo_sequences():
    coverage = Counter()

    class Path(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.repo, self.ids = MemoryRepo(["u1"]), Ids()
            aspect, wiki = seed_events(self.ids)
            self.repo.commit(self.repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
            self.order = [f"{PATH_ID}-p{i}" for i in range(1, 4)]
            self.titles = {key: f"第 {i} 页" for i, key in enumerate(self.order, 1)}
            self.hidden = set()
            self.started = {}
            self.sequence = 0
            self.children = None

        def visible(self):
            return [key for key in self.order if key not in self.hidden]

        @rule(first=st.integers(0, 12), last=st.integers(0, 12))
        def move(self, first, last):
            coverage["move"] += 1
            visible = self.visible()
            call = lambda: edit_path(self.repo, "u1", PATH_ID,
                PathMoveIn(op="move", **{"from": first}, to=last), now=NOW, new_id=self.ids)
            if max(first, last) >= len(visible):
                with pytest.raises(WikiInvalid):
                    call()
                return
            call()
            if first != last:
                key = visible.pop(first)
                self.order.remove(key)
                # Insert before the row at the requested visible position. Hidden split anchors
                # remain between their neighbours until their own undo restores them.
                at = self.order.index(visible[last]) if last < len(visible) else len(self.order)
                self.order.insert(at, key)

        @rule(index=st.integers(0, 12))
        def remove(self, index):
            coverage["remove"] += 1
            call = lambda: edit_path(self.repo, "u1", PATH_ID, PathRemoveIn(op="remove", page_key=self.visible()[index] if index < len(self.visible()) else "missing"),
                                     now=NOW, new_id=self.ids)
            if index >= len(self.visible()):
                with pytest.raises(WikiInvalid):
                    call()
            else:
                key = self.visible()[index]
                call()
                self.hidden.add(key)

        @rule()
        def add(self):
            coverage["add"] += 1
            self.sequence += 1
            title = f"new {self.sequence}"
            out = edit_path(self.repo, "u1", PATH_ID, PathAddIn(op="add", request_id=title, title=title),
                            now=NOW, new_id=self.ids)
            key = out.path[-1].key
            self.order.append(key)
            self.titles[key] = title

        @rule(index=st.integers(0, 12))
        def rename(self, index):
            coverage["rename"] += 1
            keys = [*self.visible(), "unknown"]
            key = keys[index % len(keys)]
            self.sequence += 1
            title = f"renamed {self.sequence}"
            call = lambda: edit_path(self.repo, "u1", PATH_ID,
                PathRetitleIn(op="retitle", page_key=key, title=title), now=NOW, new_id=self.ids)
            if key == "unknown":
                with pytest.raises(WikiInvalid):
                    call()
            else:
                call()
                self.titles[key] = title

        @rule()
        def start(self):
            coverage["start"] += 1
            waiting = [key for key in self.visible() if key not in self.started]
            if not waiting:
                with pytest.raises(WikiConflict):
                    next_path_page(self.repo, "u1", PATH_ID, now=NOW, new_id=self.ids)
            else:
                key = waiting[0]
                out = next_path_page(self.repo, "u1", PATH_ID, now=NOW, new_id=self.ids)
                self.started[key] = out.page_id
                assert self.repo.load("u1").state.pages[out.page_id].title == self.titles[key]

        @rule()
        def split(self):
            coverage["split"] += 1
            key = f"{PATH_ID}-p3"
            call = lambda: split_row(self.repo, None, "u1", PATH_ID, key, now=NOW, new_id=self.ids)
            if key in self.hidden:
                with pytest.raises(WikiInvalid):
                    call()
            else:
                before = set(self.order)
                out = call()
                kids = [item for item in out.path if item.key not in before]
                assert len(kids) == 2 and out.undo == ROWS_UNDO
                at = self.order.index(key)
                self.order[at + 1:at + 1] = [item.key for item in kids]
                self.titles.update({item.key: item.title for item in kids})
                self.hidden.add(key)
                self.children = [item.key for item in kids]

        @rule()
        def undo(self):
            coverage["undo_split"] += 1
            key = f"{PATH_ID}-p3"
            call = lambda: undo_split_row(self.repo, "u1", PATH_ID, key, ROWS_UNDO, now=NOW, new_id=self.ids)
            if self.children is None:
                with pytest.raises(WikiInvalid):
                    call()
            else:
                call()
                self.hidden.discard(key)
                self.hidden.update(self.children)
                self.children = None

        @rule()
        def reload(self):
            coverage["replay"] += 1
            assert replay(self.repo.wiki_log("u1")).to_json() == self.repo.load("u1").state.to_json()

        @invariant()
        def rendered_path_and_pages_agree(self):
            snap = self.repo.load("u1")
            items = path_items(snap.state, snap.state.paths[PATH_ID], snap.k, NOW)
            assert [(i.key, i.title) for i in items] == [(key, self.titles[key]) for key in self.visible()]
            for key, pid in self.started.items():
                assert snap.state.pages[pid].title == self.titles[key]

    run_state_machine_as_test(Path, settings=settings(max_examples=200, stateful_step_count=50, deadline=None))
    print("S5 path command coverage", dict(coverage))
