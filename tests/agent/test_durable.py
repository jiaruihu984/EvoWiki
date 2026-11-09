"""durable: steps are JSON in and out, exceptions cross as JSON and are re-raised with their type,
a replayed step reports ran=False, ids / clock are steps, and outside a workflow everything is a
plain call. Virtual mode needs no database; the DBOS tests run on the throwaway Postgres DB."""
from datetime import UTC, datetime

import pytest

from evowiki.agent import dbos_app, durable
from evowiki.llm.gateway import ProviderOutputError
from evowiki.llm.usage import Account, BudgetExceededError
from evowiki.wikiapp.repo import WikiConflict


def test_outside_a_workflow_everything_is_a_plain_call():
    assert not durable.in_workflow()
    marker = object()
    assert durable.step("x", lambda: marker) is marker          # no JSON round trip
    assert durable.ids(lambda p: f"{p}_1")("wev") == "wev_1"
    t = datetime(2026, 10, 14, tzinfo=UTC)
    assert durable.now(lambda: t) is t


def test_virtual_step_round_trips_json():
    with durable.virtual_workflow("wf-1"):
        assert durable.in_workflow() and durable.workflow_id() == "wf-1"
        assert durable.step("s", lambda: {"a": (1, 2)}) == {"a": [1, 2]}   # tuples become lists
        value, ran = durable.step_ran("s", lambda: 3)
        assert (value, ran) == (3, True)


def test_a_non_json_step_value_fails_closed():
    with durable.virtual_workflow("wf-2"), pytest.raises(durable.StepFailed, match="not JSON"):
        durable.step("bad", lambda: object())


def test_in_workflow_is_false_inside_a_step():
    with durable.virtual_workflow("wf-3"):
        assert durable.step("s", durable.in_workflow) is False


def test_step_key_is_stable_and_only_inside_a_step():
    with durable.virtual_workflow("wf-4"):
        keys = [durable.step("a", durable.step_key), durable.step("b", durable.step_key)]
    assert keys == ["wf-4:0", "wf-4:1"]
    with pytest.raises(RuntimeError):
        durable.step_key()


@pytest.mark.parametrize("exc", [
    WikiConflict(),
    ProviderOutputError("cut", status="truncated", raw_output="x", cost=0.25),
    BudgetExceededError(Account(user_id="u", group="page"), "day", 1.0),
    LookupError("no page"),
])
def test_known_exceptions_cross_with_their_type_and_attributes(exc):
    with durable.virtual_workflow("wf-5"), pytest.raises(type(exc)) as got:
        durable.step("boom", lambda: (_ for _ in ()).throw(exc))
    assert str(got.value) == str(exc)
    if isinstance(exc, ProviderOutputError):
        assert got.value.status == "truncated" and got.value.cost == 0.25


def test_foreign_exceptions_become_step_failed():
    from pydantic import BaseModel

    class M(BaseModel):
        n: int

    with durable.virtual_workflow("wf-6"), pytest.raises(durable.StepFailed, match="ValidationError"):
        durable.step("v", lambda: M(n="x"))


def test_ids_and_now_are_steps_in_a_workflow():
    seq = iter(range(100))
    with durable.virtual_workflow("wf-7"):
        a = durable.ids(lambda p: f"{p}_{next(seq)}")("wev")
        t = durable.now(lambda: datetime(2026, 10, 14, 8, tzinfo=UTC))
    assert a == "wev_0" and t == datetime(2026, 10, 14, 8, tzinfo=UTC)


# ---- real DBOS (Postgres): a replayed step reports ran=False and returns the recorded value ----

@pytest.fixture
def dbos_db(db_url):
    yield db_url
    dbos_app.shutdown()


def test_dbos_replay_returns_recorded_values_and_ran_false(dbos_db):
    from dbos import DBOS, SetWorkflowID

    runs: list[str] = []

    def body() -> dict:
        got = [durable.step_ran(n, lambda n=n: runs.append(n) or {"n": n}) for n in ("one", "two")]
        try:
            durable.step("bad", lambda: (_ for _ in ()).throw(WikiConflict()))
            conflict = False
        except WikiConflict:
            conflict = True
        return {"values": [v for v, _ in got], "ran": [r for _, r in got], "conflict": conflict,
                "key": durable.step("key", durable.step_key)}

    @DBOS.workflow(name="test_durable_replay")
    def wf() -> dict:
        return body()

    dbos_app.launch(dbos_db, app_version="t")
    assert dbos_app.launched()
    with SetWorkflowID("dur-1"):
        first = wf()
    assert first["values"] == [{"n": "one"}, {"n": "two"}] and first["ran"] == [True, True]
    assert first["conflict"] and first["key"].startswith("dur-1:")
    # the same id again: DBOS returns the recorded result, nothing runs
    runs.clear()
    with SetWorkflowID("dur-1"):
        again = DBOS.start_workflow(wf).get_result()
    assert runs == [] and again == first


def test_dbos_recovery_replays_steps_with_ran_false(dbos_db):
    """A workflow re-executed from step 3 (a fork, as after a crash; DBOS numbers steps from 1,
    so ids 1-2 are kept) replays the first two steps with
    ran=False and the recorded values, and runs the rest."""
    from dbos import DBOS, SetWorkflowID

    runs: list[str] = []

    @DBOS.workflow(name="test_durable_recovery")
    def wf() -> dict:
        got = [durable.step_ran(n, lambda n=n: runs.append(n) or {"n": n})
               for n in ("one", "two", "three")]
        return {"values": [v for v, _ in got], "ran": [r for _, r in got]}

    dbos_app.launch(dbos_db, app_version="t")
    with SetWorkflowID("dur-2"):
        first = wf()
    assert first["ran"] == [True, True, True] and runs == ["one", "two", "three"]
    runs.clear()
    forked = DBOS.fork_workflow("dur-2", 3).get_result()
    assert forked["ran"] == [False, False, True]
    assert forked["values"] == first["values"]
    assert runs == ["three"]


def test_non_json_exception_attributes_are_none_on_the_rebuilt_exception(caplog):
    class Odd:
        pass

    exc = ValueError("odd")
    exc.draft = Odd()                       # not JSON: cannot cross the step boundary
    exc.code = "x"
    with durable.virtual_workflow("wf-7"), caplog.at_level("WARNING", logger="evowiki.agent.durable"), \
            pytest.raises(ValueError) as got:
        durable.step("boom", lambda: (_ for _ in ()).throw(exc))
    assert got.value.code == "x" and got.value.draft is None     # no AttributeError
    assert any(r.exc_info and r.exc_info[1] is exc for r in caplog.records)   # original logged
