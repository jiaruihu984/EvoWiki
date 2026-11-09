from evowiki.agent.tools import Source, ToolResult
from evowiki.assistant.status_line import EN_OPERATIONS, LINES, StatusNames, status_line
from evowiki.assistant.tool_rows import done_row


def test_all_builtin_tools_have_semantic_english_operations():
    assert EN_OPERATIONS.keys() == LINES.keys()
    for name in LINES:
        line = status_line(name, {}, StatusNames(language="en"))
        assert line and not any("一" <= c <= "鿿" for c in line)


def test_english_row_uses_original_source_names_and_refs():
    names = StatusNames(language="en", pages={"p": "中文原文"})
    result = ToolResult(content="raw history", sources=(Source(title="中文原文", page_id="p"),))
    row = done_row("read_page", {}, result, names)
    assert row.label == "Read 中文原文"
    assert row.refs == result.sources
    assert row.group_label == "Read {k} pages"


def test_failed_read_does_not_claim_success_in_english():
    row = done_row("read_page", {}, ToolResult(content="raw", error="no"), StatusNames(language="en"))
    assert row.label == "Could not complete this operation"
