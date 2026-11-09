"""Command sequences through PostgreSQL anchors; the oracle stores raw inputs only.

Reader reconstruction checks consumers, not process recovery (tested separately).
"""

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

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import SessionInfo
from evowiki.memory.anchors import AnchorMemory
from evowiki.memory.forget import suppress_sources
from evowiki.memory.store import PgMemoryStore
from tests.memory.test_anchors import Gateway, T
from tests.runtime.helpers import make_user

_users = count()


class AnchorModel(RuleBasedStateMachine):
    def __init__(self, pool):
        super().__init__()
        self.pool = pool
        self.uid = f"anchor-model-{next(_users)}"
        make_user(pool, self.uid)
        self.sessions = PgSessionStore(pool)
        self.gateway = Gateway()
        self.inputs = {}
        self.blocked = set()
        self.n = 0
        self.clean = False
        self.infos = [SessionInfo(session_id=f"{self.uid}-{i}", user_id=self.uid,
                                 page_id="p", note_concept_id=None, created_at=T)
                      for i in range(2)]
        for info in self.infos:
            self.sessions.create(info)
        self._readers()

    def _readers(self):
        repo = SimpleNamespace(load=lambda uid: SimpleNamespace(
            state=SimpleNamespace(pages={"p": object()}, paths={}, maps={})))
        self.anchors = AnchorMemory(self.pool, self.sessions, PgMemoryStore(self.pool), repo=repo)

    def _append(self, index):
        info = self.infos[index]
        self.n += 1
        mid = f"m{self.n}"
        node = f"turn:{info.session_id}/{mid}"
        text = f"observation {self.n}"
        self.sessions.append(info.session_id, f"{self.uid}-u{self.n}", "user",
                             {"message_id": mid, "text": text}, T)
        self.sessions.append(info.session_id, f"{self.uid}-a{self.n}", "assistant",
                             {"message_id": f"answer{self.n}", "text": "answer", "sources": []}, T)
        self.inputs[node] = text
        self.clean = False

    @precondition(lambda self: self.n < 16)
    @rule(index=st.integers(0, 1))
    def append(self, index):
        self._append(index)

    @rule()
    def consolidate(self):
        for info in self.infos:
            assert self.anchors.update(info, gateway=self.gateway, account=None, now=T)
        self.clean = bool(self.inputs)

    @precondition(lambda self: 0 < self.n < 15)
    @rule(index=st.integers(0, 1))
    def input_arrives_during_generation(self, index):
        self._append(index)
        before = self.n
        self.gateway.callback = lambda: self._append(index)
        assert not self.anchors.update(self.infos[index], gateway=self.gateway, account=None, now=T)
        assert self.n == before + 1
        self.gateway.callback = None

    @precondition(lambda self: bool(self.inputs.keys() - self.blocked))
    @rule(position=st.integers(0, 30))
    def suppress(self, position):
        visible = sorted(self.inputs.keys() - self.blocked)
        source = visible[position % len(visible)]
        with self.pool.connection() as conn:
            suppress_sources(conn, self.uid, "state-model", [source])
        self.blocked.add(source)
        self.clean = False

    @rule()
    def rebuild_readers(self):
        self.sessions = PgSessionStore(self.pool)
        self._readers()

    @invariant()
    def published_provenance_and_content(self):
        row = self.anchors.readable(self.uid, "page:p")
        if row is not None:
            assert not self.blocked.intersection(row.sources)
            for statement in row.payload.get("statements", []):
                assert statement["sources"]
                for source in statement["sources"]:
                    assert source in self.inputs and source not in self.blocked
                    assert statement["text"] == self.inputs[source]
        if self.clean and self.inputs.keys() - self.blocked:
            assert row is not None
            cited = {source for statement in row.payload["statements"]
                     for source in statement["sources"]}
            assert cited == self.inputs.keys() - self.blocked


def test_anchor_command_sequences(pool):
    run_state_machine_as_test(lambda: AnchorModel(pool), settings=settings(
        max_examples=200, stateful_step_count=50, deadline=None))
