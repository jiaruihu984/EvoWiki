"""`proposal_change`: what the 看改动 of a page-edit card is made from."""
from evowiki.agent.tools import ProposalDraft
from evowiki.assistant.ports import PageSectionView, PageView
from evowiki.bot.diffs import proposal_change


class Data:
    def page(self, user_id, page_id):
        body = '第一段。\n\n```evoui\n{"title": "拖一拖"}\n```\n\n回到[上一页](#p:pg_1)。'
        return PageView(page_id="p", title="t", concepts=(),
                        sections=(PageSectionView(section_id="s", heading="旧标题", body=body),))


def draft(kind, section_id="s"):
    return ProposalDraft(kind=kind, page_id="p", section_id=section_id, heading="新标题",
                         text="第一段。", why="w")


def test_a_rewrite_shows_the_section_as_it_is_with_an_interactive_as_one_line():
    assert proposal_change(Data(), "u", draft("rewrite_section")) == {
        "old_heading": "旧标题", "old_body": "第一段。\n\n〔互动：拖一拖〕\n\n回到上一页。", "new_body": "第一段。"}


def test_an_added_section_replaces_nothing():
    assert proposal_change(Data(), "u", draft("add_section")) == {
        "old_heading": None, "old_body": "", "new_body": "第一段。"}
