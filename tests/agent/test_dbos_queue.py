"""A DBOS queue partitioned by user runs at most one workflow per user at a time (spec §1:
每用户 Bot 任务并发 1), while two users run concurrently."""
import threading
import time

import pytest

from evowiki.agent import dbos_app


@pytest.fixture
def dbos_db(db_url):
    yield db_url
    dbos_app.shutdown()


def test_partitioned_queue_serialises_one_user(dbos_db):
    from dbos import DBOS, SetEnqueueOptions

    spans: list[tuple[str, float, float]] = []
    lock = threading.Lock()

    @DBOS.workflow()
    def job(user: str) -> None:
        t0 = time.monotonic()
        time.sleep(0.3)
        with lock:
            spans.append((user, t0, time.monotonic()))

    dbos_app.launch(dbos_db, app_version="b0-test")
    queue = DBOS.register_queue("bot-probe", partition_concurrency=1)
    handles = []
    for user in ("u1", "u1", "u1", "u2"):
        with SetEnqueueOptions(queue_partition_key=user):
            handles.append(queue.enqueue(job, user))
    for h in handles:
        h.get_result()

    def overlap(a, b):
        return a[1] < b[2] and b[1] < a[2]

    u1 = [s for s in spans if s[0] == "u1"]
    assert len(u1) == 3
    assert not any(overlap(a, b) for i, a in enumerate(u1) for b in u1[i + 1:])
    u2 = next(s for s in spans if s[0] == "u2")
    assert any(overlap(u2, a) for a in u1)          # another user is not blocked


def test_listen_queues_leaves_other_queues_alone(dbos_db):
    """A Bot-off server listens to `pages` only: a workflow on another queue (the `bot` queue a
    Bot-on process enqueued) stays enqueued for that process instead of being taken here."""
    from dbos import DBOS

    @DBOS.workflow()
    def echo(x: str) -> str:
        return x

    dbos_app.launch(dbos_db, app_version="b0-test", queues=["listened"])
    listened = DBOS.register_queue("listened")
    other = DBOS.register_queue("not-listened")
    assert listened.enqueue(echo, "a").get_result() == "a"
    h = other.enqueue(echo, "b")
    time.sleep(3)
    assert h.get_status().status == "ENQUEUED"
