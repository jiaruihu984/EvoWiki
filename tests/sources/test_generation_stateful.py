"""Real source persistence under reordered generations and late reader results."""

from itertools import count
from types import SimpleNamespace

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from evowiki.sources import store
from evowiki.sources.importer import ImportJob, _Deleted, _Run
from evowiki.sources.models import PageRead
from tests.runtime.helpers import make_user
from tests.sources.test_store import NOW, _src

_ids = count()


class SourceGenerations(RuleBasedStateMachine):
    def __init__(self, pool):
        super().__init__()
        self.pool = pool
        self.uid = f"source-model-{next(_ids)}"
        self.sid = self.uid + "-source"
        self.generation = 0
        self.deleted = False
        self.saved = {}
        make_user(pool, self.uid)
        with pool.connection() as conn:
            store.insert_source(conn, _src(sid=self.sid, user=self.uid, sha=self.sid),
                                b"pdf", "application/pdf")

    @precondition(lambda self: not self.deleted)
    @rule()
    def rerun(self):
        with self.pool.connection() as conn:
            assert store.bump_run(conn, self.uid, self.sid) == self.generation + 1
        self.generation += 1

    @rule(page=st.integers(1, 3), behind=st.integers(0, 4), text=st.sampled_from(["first", "later"]))
    def reader_result(self, page, behind, text):
        generation = max(0, self.generation - behind)
        worker = _Run(ImportJob(user_id=self.uid, source_id=self.sid, gen=generation),
                      lambda *_: None, SimpleNamespace(pool=self.pool, clock=lambda: NOW))
        read = PageRead(page=page, text=text, by="local", figure=False)
        if self.deleted or generation != self.generation:
            try:
                worker._store_read(read)
            except _Deleted:
                pass
            else:
                raise AssertionError("a stale/deleted source accepted a reader result")
        else:
            worker._store_read(read)
            self.saved[page] = text

    @precondition(lambda self: not self.deleted)
    @rule()
    def delete(self):
        with self.pool.connection() as conn:
            assert store.soft_delete(conn, self.uid, self.sid, NOW)
        self.deleted = True
        self.saved.clear()

    @invariant()
    def only_current_generation_can_change_persisted_reads(self):
        with self.pool.connection() as conn:
            reads = store.page_reads(conn, self.uid, self.sid)
        assert {page: read.text for page, read in reads.items()} == self.saved


def test_generation_command_sequences(pool):
    run_state_machine_as_test(lambda: SourceGenerations(pool), settings=settings(
        max_examples=200, stateful_step_count=50, deadline=None))
