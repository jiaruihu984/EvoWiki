from evowiki.agent.tools import Source, ToolResult
from evowiki.assistant.status_line import NO_NAMES, StatusNames
from evowiki.assistant.tool_rows import done_row

NAMES = StatusNames(pages={"p1": "均值和方差"}, current_page="p2", sections={"s2": 2})


def test_a_fact_row_says_what_happened_to_it():
    """remember_about_me's quiet row: written, waiting for his yes (a card), or not kept."""
    from evowiki.agent.tools import ActionCard
    card = ActionCard(action_id="a1", kind="profile_fact", title="记下了：护士", status="executed")
    assert done_row("remember_about_me", {}, ToolResult(content="ok", action=card),
                    NO_NAMES).label == "记进了你的档案「我」"
    waiting = card.model_copy(update={"status": "proposed", "confirm": True})
    assert done_row("remember_about_me", {}, ToolResult(content="ok", action=waiting),
                    NO_NAMES).label == "等你确认要不要记进档案"
    assert done_row("remember_about_me", {}, ToolResult(content="只对这一次有效"),
                    NO_NAMES).label == "这次照着用，没有记进档案"


def res(*sources: Source, error: str | None = None) -> ToolResult:
    return ToolResult(content="x", sources=sources, error=error)


def test_a_wiki_search_says_what_it_searched_and_how_many_pages_titles_only():
    r = res(Source(title="基因配对 › 配对有什么用：复制", page_id="pg"),
            Source(title="均值和方差 › 对比：方差到底改变了什么", page_id="p1"),
            Source(title="基因配对 › 另一节", page_id="pg"),
            Source(title="方差 · one_line", segment_id="seg1"),         # content-library hits: never shown
            Source(title="概率论 · example", segment_id="seg2"))
    row = done_row("search_wiki", {"query": "协方差"}, r, NAMES)
    assert row.label == "在 Wiki 里搜了「协方差」 · 2 页"
    assert row.items == ("基因配对", "均值和方差")            # page titles, no 「› 节」, no aspect names
    assert (row.group, row.group_label, row.refs) == ("search_wiki", "在 Wiki 里搜了 {n} 次", ())
    assert done_row("search_wiki", {"query": "x"}, res(), NAMES).label == "在 Wiki 里搜了「x」 · 没找到"


def test_a_page_read_is_cited_by_its_own_title():
    r = res(Source(title="z 分数 › 怎么算", page_id="p2"))
    row = done_row("read_page", {"section_id": "s2"}, r, StatusNames(pages={"p2": "z 分数"}, current_page="p2",
                                                                     sections={"s2": 2}))
    assert row.label == "读了《z 分数》第 2 节"
    assert (row.group_label, row.items) == ("读了 {k} 页", ("z 分数",))
    assert row.refs == (Source(title="z 分数", page_id="p2"),)
    # not named in the context: the source's own title, without the section
    assert done_row("read_page", {"page_id": "p9"}, res(Source(title="协方差 › 定义", page_id="p9")),
                    NO_NAMES).label == "读了《协方差》"


def test_web_rows_list_the_hits_and_cite_only_a_page_opened():
    hits = res(Source(title="协方差 - 维基百科", url="https://w/a"), Source(title="知乎", url="https://z/b"))
    row = done_row("web_search", {"query": "协方差 直观"}, hits, NO_NAMES)
    assert row.label == "联网搜了「协方差 直观」 · 2 条" and row.items == ("协方差 - 维基百科", "知乎")
    assert row.refs == ()
    page = done_row("fetch_url", {"url": "https://w/a"}, res(Source(title="协方差 - 维基百科", url="https://w/a")), NO_NAMES)
    assert page.label == "打开了网页 w" and page.refs == (Source(title="协方差 - 维基百科", url="https://w/a"),)


def test_other_reads_say_what_was_done_and_a_failure_says_so():
    assert done_row("read_k", {}, res(), NO_NAMES).label == "看了你的掌握情况"
    assert done_row("read_path", {}, res(), NO_NAMES).label == "看了学习路径"
    assert done_row("search_source", {"query": "特征值"}, res(), NO_NAMES).label == "在资料里搜了「特征值」"
    # a read that failed is said plainly, not as an alarm (the model goes on: 「读了页面，没成功」 read as broken)
    assert done_row("read_k", {}, res(error="boom"), NO_NAMES).label == "没看到你的掌握情况"
    assert done_row("read_page", {}, res(error="没有当前页"), NO_NAMES).label == "没读到页面"
    assert done_row("my_forged_tool", {}, res(), NO_NAMES).label == "处理好了"
    assert done_row("search_wiki", "not a dict", res(), NO_NAMES).label  # never raises


def test_the_open_review_row_reads_as_chinese():
    assert done_row("open_review", {}, ToolResult(content="ok"), NO_NAMES).label == "打开了复习"
    assert done_row("open_review", {}, ToolResult(content="x", error="x"),
                    NO_NAMES).label == "现在没有要复习的卡"


def test_a_precise_read_says_so_when_done():
    r = ToolResult(content="x")
    row = done_row("read_source", {"source_id": "s", "page_from": 7, "precise": True}, r, NO_NAMES)
    assert row.label == "精确读了资料第 7 页"
