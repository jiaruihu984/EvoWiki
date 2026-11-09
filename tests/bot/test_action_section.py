"""ActionOut.section_id: the section a row's change is in (the chat card's 查看详情 opens the page there)."""

from types import SimpleNamespace

from evowiki.bot.bot_schemas import section_of


def test_the_section_comes_from_the_undo_data_the_content_or_the_draft():
    assert section_of(SimpleNamespace(undo={"section_id": "s1"}, content={"section_id": "s2"})) == "s1"
    assert section_of(SimpleNamespace(undo=None, content={"section_id": "s2"})) == "s2"
    # 加交互 (page_practice): the section the interactive goes into is in its draft
    practice = SimpleNamespace(undo=None, content={"page_id": "p1", "draft": {"section_id": "s3", "block_id": "b1"}})
    assert section_of(practice) == "s3"
    assert section_of(SimpleNamespace(undo=None, content={"draft": "not a dict"})) is None
    assert section_of(SimpleNamespace(undo={}, content={})) is None
