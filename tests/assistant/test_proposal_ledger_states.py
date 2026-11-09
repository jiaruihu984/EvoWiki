"""GET session: a proposal card shows the ledger's state; an undone page change wins over the session log's
`accepted` (the card says 已撤销). No DB, no LLM: a fake ledger keyed like the edit executor's rows."""

from types import SimpleNamespace

from evowiki.agent.tools import ProposalDraft
from evowiki.assistant.contracts import ProposalOut, ProposalStateOut, SessionOut
from evowiki.assistant.edit_executor import assistant_key
from evowiki.assistant.service import AssistantService


class _Ledger:
    def __init__(self, rows: dict[str, SimpleNamespace]):
        self.rows = rows

    def get_by_key(self, key: str):
        return self.rows.get(key)

    def get(self, user_id: str, action_id: str):
        return None


def _service(rows: dict[str, SimpleNamespace]) -> AssistantService:
    svc = object.__new__(AssistantService)   # only the ledger is read on this path
    svc._ledger = _Ledger(rows)
    return svc


def _draft() -> ProposalDraft:
    return ProposalDraft(kind="add_section", page_id="p1", heading="h", text="t", why="w")


def _out(*ids: str, decisions: tuple[ProposalStateOut, ...] = ()) -> SessionOut:
    return SessionOut(session_id="s1", page_id="p1", messages=(), decisions=decisions,
                      proposals=tuple(ProposalOut(proposal_id=i, draft=_draft()) for i in ids))


def _row(status: str, applied=("s2",)) -> SimpleNamespace:
    return SimpleNamespace(status=status, result={"applied": list(applied)}, detail="", action_id="act_1",
                           kind="assistant_edit", undo={"section_id": "s2"})


def test_undone_row_overrides_an_accepted_decision_in_the_log():
    svc = _service({assistant_key("pr_a"): _row("undone")})
    out = svc._with_ledger_states("u1", _out("pr_a", decisions=(
        ProposalStateOut(proposal_id="pr_a", status="accepted", applied=("s2",)),)))
    assert [(d.proposal_id, d.status, d.applied) for d in out.decisions] == [("pr_a", "undone", ("s2",))]


def test_undone_row_without_a_log_decision_is_reported_undone():
    svc = _service({assistant_key("pr_a"): _row("undone")})
    out = svc._with_ledger_states("u1", _out("pr_a"))
    assert [(d.proposal_id, d.status) for d in out.decisions] == [("pr_a", "undone")]


def test_executed_row_is_accepted_and_a_logged_decision_stays():
    svc = _service({assistant_key("pr_a"): _row("executed"), assistant_key("pr_b"): _row("executed")})
    logged = ProposalStateOut(proposal_id="pr_b", status="rejected")
    out = svc._with_ledger_states("u1", _out("pr_a", "pr_b", decisions=(logged,)))
    assert [(d.proposal_id, d.status, d.action_id, d.can_undo) for d in out.decisions] == [
        ("pr_b", "rejected", "", False), ("pr_a", "accepted", "act_1", True)]


def test_a_logged_failure_gives_way_to_an_executed_row_the_page_did_change():
    svc = _service({assistant_key("pr_a"): _row("executed")})
    logged = ProposalStateOut(proposal_id="pr_a", status="failed", detail="页面在提议之后改过了")
    out = svc._with_ledger_states("u1", _out("pr_a", decisions=(logged,)))
    assert [(d.status, d.detail, d.applied, d.action_id, d.can_undo) for d in out.decisions] == [
        ("accepted", "", ("s2",), "act_1", True)]


def test_a_logged_decision_stays_while_the_ledger_has_not_executed():
    logged = (ProposalStateOut(proposal_id="pr_a", status="failed", detail="页面已变"),
              ProposalStateOut(proposal_id="pr_b", status="rejected"))
    svc = _service({assistant_key("pr_a"): _row("failed"), assistant_key("pr_b"): _row("rejected")})
    out = svc._with_ledger_states("u1", _out("pr_a", "pr_b", decisions=logged))
    assert out.decisions == logged


def test_a_logged_acceptance_gets_the_row_its_undo_needs():
    svc = _service({assistant_key("pr_a"): _row("executed")})
    logged = ProposalStateOut(proposal_id="pr_a", status="accepted", applied=("s2",))
    out = svc._with_ledger_states("u1", _out("pr_a", decisions=(logged,)))
    assert [(d.status, d.applied, d.action_id, d.can_undo) for d in out.decisions] == [
        ("accepted", ("s2",), "act_1", True)]


def test_no_ledger_rows_leaves_the_session_unchanged():
    svc = _service({})
    session = _out("pr_a")
    assert svc._with_ledger_states("u1", session) is session
