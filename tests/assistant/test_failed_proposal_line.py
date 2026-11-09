"""A page-edit card the page moved under is dead: the next turn's model is told, so it does not send him back to it."""
from types import SimpleNamespace

from evowiki.agent.tools import ProposalDraft
from evowiki.assistant.contracts import ProposalOut, ProposalStateOut
from evowiki.assistant.turn_skills import TurnSkills


def lines(status):
    pr = ProposalOut(proposal_id="p1", draft=ProposalDraft(
        kind="rewrite_section", page_id="pg", section_id="s", heading="谁的朋友最多", text="t", why="w"))
    state = SimpleNamespace(actions=(), proposals=(pr,), latest={"p1": ProposalStateOut(
        proposal_id="p1", status=status, detail="页面改过了" if status == "failed" else "")})
    me = SimpleNamespace(undoable_rows=[], session_id="s", start_ops=[], waiting_rows=[], _waiting_now=list, rows={}, state=state)
    return TurnSkills._ops_lines(me)


def test_a_failed_proposal_is_named_with_its_reason_and_an_accepted_one_is_not():
    assert "「谁的朋友最多」没有生效（页面改过了）" in lines("failed")
    assert lines("accepted") == ""


def test_a_cards_state_now_is_read_from_its_row_whatever_the_summary_said():
    """The real case: a handoff said 「等待用户确认拆细草稿」, the card was then confirmed and undone, and
    the next answer said 「草稿之前生成过，但还没确认」: the summary is older than the row."""
    card = SimpleNamespace(action_id="a1", title="把「磨豆机」拆细")
    state = SimpleNamespace(actions=(card,), proposals=(), latest={})
    for status, word in (("undone", "已撤销"), ("rejected", "他取消了，没有执行"), ("proposed", "还等他在卡片上确认")):
        me = SimpleNamespace(undoable_rows=[], session_id="s", start_ops=[], waiting_rows=[], _waiting_now=list,
                             rows={"a1": {"status": status}}, state=state)
        assert f"- a1「把「磨豆机」拆细」{word}" in TurnSkills._ops_lines(me)
